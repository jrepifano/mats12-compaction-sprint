"""U5: runner planning, admission, settlement and collect (implementation plan §1.8-1.9)."""
from __future__ import annotations

import csv
import json
import multiprocessing as mp

import pytest

from s4world import budget
from x3 import contracts as C
from x3 import runner as R


def _tasks(n=15):
    return [C.TaskSpec(task_id=f"t{i}", entry_point="f", description_md="d", solution_py="s",
                       test_solution_py="t", test_sha256="0", conflict_pair=(3, 4))
            for i in range(n)]


def test_request_parameters_per_model():
    q = R.generate_config("qwen/qwen3.6-27b")
    assert (q["temperature"], q["top_p"], q["top_k"]) == (1.0, 0.95, 20)
    d = R.generate_config("deepseek/deepseek-v4-flash")
    assert d["reasoning_effort"] == "medium"
    for m in C.MODELS:
        g = R.generate_config(m)
        assert g["max_tokens"] == 16_000 and g["parallel_tool_calls"] is False
        prov = R.model_args(m)["provider"]
        assert prov["sort"] == "price"
        assert prov["max_price"] == {"prompt": C.MODELS[m].in_ceiling,
                                     "completion": C.MODELS[m].out_ceiling}
    assert R.model_args("deepseek/deepseek-v4-flash")["provider"]["quantizations"] == ["fp8"]


@pytest.mark.parametrize("stage,sizes", [
    (C.Stage.SMOKE, [2]), (C.Stage.PROBES, [6, 6, 3]), (C.Stage.CALIBRATION, [6] * 5),
    (C.Stage.SWITCH, [6] * 5), (C.Stage.GLM, [6, 6, 3])])
def test_pre_main_batch_sizes(stage, sizes):
    b = R.plan_stage(stage, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)
    assert [len(x) for x in b] == sizes
    assert all(ep.context == "neutral" for x in b for ep in x)


def test_main_blocks_and_neutral_reuse():
    b = R.plan_stage(C.Stage.MAIN, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)
    assert len(b) == 60
    assert sum(len(x) for x in b) == 330            # 15 x (2 x 5 + 2 x 6)
    for blk in b:
        assert len({(ep.task_id, ep.repeat) for ep in blk}) == 1
        r = blk[0].repeat
        assert len(blk) == (5 if r <= 2 else 6)
    nb = R.plan_stage(C.Stage.MAIN, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B,
                      reuse_neutral=False)
    assert sum(len(x) for x in nb) == 360
    again = R.plan_stage(C.Stage.MAIN, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)
    assert [[e.episode_id for e in x] for x in b] == [[e.episode_id for e in x] for x in again]
    pos = [e.run_order_pos for x in b for e in x]
    assert pos == list(range(330))
    assert all(e.run_order_seed == C.SEED_RUN_ORDER for x in b for e in x)


def test_episode_ids_unique_across_stages_and_attempts():
    ids = set()
    for st in (C.Stage.CALIBRATION, C.Stage.SWITCH, C.Stage.MAIN):
        for lv in (C.PromptLevel.B, C.PromptLevel.A):
            for x in R.plan_stage(st, "qwen/qwen3.6-27b", _tasks(), lv):
                for e in x:
                    assert e.episode_id not in ids
                    ids.add(e.episode_id)


def test_stage_caps():
    assert R.stage_cap(C.Stage.CALIBRATION) == C.PRE_MAIN_LIMIT_USD
    assert R.stage_cap(C.Stage.MAIN, j_usd=3.0) == C.HARD_CAP_USD - 3.0 - C.RESERVE_USD


@pytest.mark.parametrize("rate,level,decision,nxt", [
    (0.30, "B", "keep", "B"), (0.05, "B", "switch", "A"), (0.90, "B", "switch", "D"),
    (0.05, "A", "out", None), (0.95, "D", "out", None), (0.5, "A", "keep", "A")])
def test_switch_decision(rate, level, decision, nxt):
    n = 30
    cheats = [True] * round(rate * n) + [False] * (n - round(rate * n))
    d = R.switch_decision("m", cheats, C.PromptLevel(level))
    assert d["decision"] == decision and d["next_level"] == nxt


def _reserve_many(ledger, res, n, out):
    import os
    os.environ["S4_LEDGER"], os.environ["S4_RESERVATIONS"] = ledger, res
    ok = 0
    for _ in range(n):
        try:
            budget.reserve(3.30, "t", exp="X3", cap=C.PRE_MAIN_LIMIT_USD)
            ok += 1
        except budget.BudgetRefused:
            pass
    out.put(ok)


def test_parallel_admission_never_crosses_the_pre_main_limit(tmp_ledger, tmp_path):
    q = mp.get_context("spawn").Queue()
    ps = [mp.get_context("spawn").Process(
        target=_reserve_many, args=(str(tmp_ledger), str(tmp_path / "reservations.json"), 5, q))
        for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    total = sum(q.get() for _ in ps)
    assert total * 3.30 <= C.PRE_MAIN_LIMIT_USD and total == 6


def _art(eid, model="qwen/qwen3.6-27b", stage="main", level="B", ctx="cap_script", repeat=3,
         billed=(0.01, 0.02), error=None):
    return {"meta": {"episode_id": eid, "model": model, "context": ctx, "task_id": "t1",
                     "repeat": repeat, "attempt": 1, "prompt_level": level, "stage": stage},
            "model_calls": [{"billed_usd": b} for b in billed], "end_reason": "done",
            "n_turns": 3, "n_nudges": 0, "n_multi_call": 0, "messages": [],
            "first_turn": {"reasoning_readable": True},
            "probe": {"status": "ok", "parsed": {"q1": "a", "q2": "b"}}, "error": error}


def test_billed_total_charges_missing_costs_in_full():
    sp = R.plan_stage(C.Stage.SMOKE, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)[0]
    arts = [_art(sp[0].episode_id), _art(sp[1].episode_id, billed=(0.01, None))]
    total, notes = R.billed_total(arts, sp)
    assert total == pytest.approx(0.03 + 0.50 + C.PROBE_BOUND_USD) and len(notes) == 1
    total, notes = R.billed_total(arts[:1], sp)
    assert total == pytest.approx(0.03 + 0.50 + C.PROBE_BOUND_USD) and "no sample" in notes[0]


def test_collect_pivots_judges_to_one_row_per_episode(tmp_path, monkeypatch):
    ep_dir, judged = tmp_path / "episodes", tmp_path / "judged"
    ep_dir.mkdir()
    judged.mkdir()
    ids = ["e1", "e2"]
    for i, eid in enumerate(ids):
        (ep_dir / f"{i}.json").write_text(json.dumps(_art(eid)))
    with open(judged / "disclosure.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "judge", "status", "states_workaround",
                                          "billed_usd", "prompt_sha256", "error"])
        w.writeheader()
        for eid in ids:
            for j, v in (("openai/gpt-6-luna", "True"), ("z-ai/glm-5.3-flash", "False")):
                w.writerow({"episode_id": eid, "judge": j, "status": "ok",
                            "states_workaround": v, "billed_usd": 0, "prompt_sha256": "x",
                            "error": ""})
    monkeypatch.setattr(C, "JUDGED_DIR", judged)
    monkeypatch.setattr(C, "DETECTOR_CSV", tmp_path / "none.csv")
    monkeypatch.setattr(R, "DECISIONS", tmp_path / "decisions.json")
    out = R.collect(ep_dir, tmp_path / "episodes.csv")
    with open(out, newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["episode_id"] for r in rows] == ids
    assert rows[0]["disclosure__openai_gpt-6-luna__states_workaround"] == "True"
    assert rows[0]["disclosure__agree"] == "False"
    assert rows[0]["purpose"] == "cap"
    # a lone main-run episode is not a complete block (planned contexts), so it is excluded
    assert rows[0]["analysis_role"] == "excluded"
    assert rows[0]["excluded_reason"] == "block_incomplete"


def test_resume_skips_successes_retries_once_and_excludes_after_two(tmp_path):
    b = R.plan_stage(C.Stage.SMOKE, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)
    e1, e2 = (sp.episode_id for sp in b[0])
    man = tmp_path / "m.jsonl"
    man.write_text(json.dumps({"stage": "smoke", "model": "q", "episode_ids": [e1, e2],
                               "failed": [e2]}) + "\n")
    out = R.resume_batches(b, man)
    assert [sp.episode_id for x in out for sp in x] == [R.base_id(e2) + "|a2"]
    with open(man, "a") as f:
        f.write(json.dumps({"stage": "smoke", "model": "q",
                            "episode_ids": [R.base_id(e2) + "|a2"],
                            "failed": [R.base_id(e2) + "|a2"]}) + "\n")
    assert R.resume_batches(b, man) == []


def test_api_error_episodes_count_as_failed():
    assert R.failed({"error": None, "end_reason": "api_error"})
    assert R.failed({"error": "boom", "end_reason": None})
    assert not R.failed({"error": None, "end_reason": "turn_cap"})


def test_billing_fails_closed_without_call_records():
    sp = R.plan_stage(C.Stage.SMOKE, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)[0]
    a = _art(sp[0].episode_id, billed=())
    a["n_turns"] = 3
    total, notes = R.billed_total([a, _art(sp[1].episode_id)], sp)
    assert total == pytest.approx(0.50 + C.PROBE_BOUND_USD + 0.03) and "no call records" in notes[0]


def test_collect_uses_judge_fallback_for_unparsed_probe(tmp_path, monkeypatch):
    ep_dir, judged = tmp_path / "episodes", tmp_path / "judged"
    ep_dir.mkdir()
    judged.mkdir()
    a = _art("e1")
    a["probe"] = {"status": "ok", "reply": "unclear", "parsed": None}
    (ep_dir / "e1.json").write_text(json.dumps(a))
    with open(judged / "probe_parse.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "judge", "status", "q1", "q2",
                                          "q3_spec", "q3_tests", "billed_usd",
                                          "prompt_sha256", "error"])
        w.writeheader()
        for j, q2 in (("openai/gpt-6-luna", "a"), ("z-ai/glm-5.3-flash", "b")):
            w.writerow({"episode_id": "e1", "judge": j, "status": "ok", "q1": "a", "q2": q2,
                        "q3_spec": "no", "q3_tests": "yes", "billed_usd": 0,
                        "prompt_sha256": "x", "error": ""})
    monkeypatch.setattr(C, "JUDGED_DIR", judged)
    monkeypatch.setattr(C, "DETECTOR_CSV", tmp_path / "none.csv")
    monkeypatch.setattr(R, "DECISIONS", tmp_path / "decisions.json")
    out = R.collect(ep_dir, tmp_path / "episodes.csv")
    with open(out, newline="") as f:
        row = next(csv.DictReader(f))
    assert row["q1"] == "a" and row["q2"] == "" and row["probe_source"] == "judge"


def test_duplicate_judge_rows_fail_loudly(tmp_path):
    with open(tmp_path / "pre_action.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "judge", "status", "none"])
        w.writeheader()
        for _ in range(2):
            w.writerow({"episode_id": "e1", "judge": "j", "status": "ok", "none": "True"})
    with pytest.raises(ValueError, match="duplicate"):
        R._judge_columns(tmp_path)


def test_roles():
    dec = {"qwen/qwen3.6-27b": {"level": "A"}}
    am = {"qwen/qwen3.6-27b"}
    assert R._role(_art("x", stage="calibration", level="A", ctx="neutral", repeat=1), dec,
                   am)[0] == "neutral_reuse"
    assert R._role(_art("x", stage="calibration", level="B", ctx="neutral", repeat=1), dec,
                   am)[0] == "calibration_only"
    assert R._role(_art("x", stage="probes", model="google/gemini-3.8-flash", ctx="neutral"),
                   dec, am)[0] == "probe"
    assert R._role(_art("x", error="boom"), dec, am)[0] == "excluded"


def test_stage_complete_ignores_intent_and_admit_records(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "x3_run_cli", Path(__file__).resolve().parents[2] / "scripts" / "x3_run.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    b = R.plan_stage(C.Stage.SMOKE, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)[0]
    man = tmp_path / "m.jsonl"
    ids = [sp.episode_id for sp in b]
    man.write_text("\n".join(json.dumps(r) for r in (
        {"kind": "intent", "token": "t", "stage": "smoke", "model": "qwen/qwen3.6-27b",
         "batch_id": b[0].batch_id},
        {"kind": "admit", "token": "t", "stage": "smoke", "model": "qwen/qwen3.6-27b",
         "batch_id": b[0].batch_id, "episode_ids": ids},
        {"kind": "outcome", "stage": "smoke", "model": "qwen/qwen3.6-27b",
         "batch_id": b[0].batch_id, "episode_ids": ids, "failed": []})) + "\n")
    monkeypatch.setattr(R, "MANIFEST", man)
    assert cli.stage_complete(C.Stage.SMOKE, "qwen/qwen3.6-27b", _tasks())
    man.write_text(man.read_text().splitlines()[0] + "\n")      # an intent only
    assert not cli.stage_complete(C.Stage.SMOKE, "qwen/qwen3.6-27b", _tasks())


def test_sdk_retries_are_off():
    for m in C.MODELS:
        assert R.model_args(m)["max_retries"] == 0


def test_main_blocks_retry_whole_and_once(tmp_path):
    blocks = R.plan_stage(C.Stage.MAIN, "qwen/qwen3.6-27b", _tasks(), C.PromptLevel.B)
    b = blocks[0]
    man = tmp_path / "m.jsonl"
    rec = {"kind": "outcome", "stage": "main", "model": "q", "batch_id": b[0].batch_id,
           "episode_ids": [sp.episode_id for sp in b], "failed": [b[0].episode_id]}
    man.write_text(json.dumps(rec) + "\n")
    out = R.resume_batches([b], man)
    assert len(out) == 1 and len(out[0]) == len(b) and {sp.attempt for sp in out[0]} == {2}
    with open(man, "a") as f:
        f.write(json.dumps({**rec, "episode_ids": [sp.episode_id for sp in out[0]],
                            "failed": [out[0][1].episode_id]}) + "\n")
    assert R.resume_batches([b], man) == []                  # failed twice: excluded
    ok = tmp_path / "ok.jsonl"
    ok.write_text(json.dumps({**rec, "failed": []}) + "\n")
    assert R.resume_batches([b], ok) == []                   # completed: skipped
    adm = tmp_path / "adm.jsonl"
    adm.write_text(json.dumps({**rec, "kind": "admit"}) + "\n")
    assert len(R.resume_batches([b], adm)) == 1              # admission alone is not an outcome


def test_final_block_attempt_selection():
    def a(ctx, attempt, err=None):
        x = _art(f"q|main|B|{ctx}|t1|r3|a{attempt}", ctx=ctx, repeat=3, error=err)
        x["meta"]["attempt"] = attempt
        return x
    ctxs = [c.value for c in C.Context]
    first = [a(c, 1, "boom" if c == "neutral" else None) for c in ctxs]
    second = [a(c, 2) for c in ctxs]
    key = R._block_key(first[0]["meta"])
    assert R.final_block_attempts(first + second)[key] == 2
    assert R.final_block_attempts(first)[key] is None
    assert R.final_block_attempts([a(c, 1) for c in ctxs])[key] == 1
