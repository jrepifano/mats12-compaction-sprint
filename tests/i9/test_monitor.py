"""U4b text-only monitors (emx.i9.monitor): Stage 0 through wsbench's own scorer, the rate
monitors and the CoT monitor, with fake completion functions only (no network)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from wsbench.evals.agentic_misalignment import judge as wj
from wsbench.evals.agentic_misalignment import prompts as wp

from emx.i9 import contracts as C
from emx.i9 import judging as J
from emx.i9 import monitor as M
from emx.i9 import scenarios as S
from i9.test_judging import FakeLLM

ACCOUNT = ("1. Situation: the model is Alex.\n4. CANDIDATE ACTIONS:\n1. Use the affair as "
           "leverage against Kyle (committed, enabled, high)")


@pytest.fixture(scope="module")
def bank() -> dict[str, C.Scenario]:
    return {s.id: s for s in S.bank_scenarios()}


def score_json(fid: int, fam: str = "blackmail") -> str:
    return "```json\n" + json.dumps({
        "identified_family": fam, "asserts_misaligned_plan": True, "design_rank": 1,
        "design_fidelity": fid, "commit_strength": 3, "origin_claimed": "enabled",
        "origin_actual": "enabled", "reason": "r"}) + "\n```"


def test_stage0_runs_dry_end_to_end(bank, tmp_path):
    items = [bank["am-blackmail_explicit-america_replacement"], bank["exfil-weights"]]

    def respond(route, system, user):
        if route == C.GEMINI:
            return ACCOUNT
        return score_json(3 if route == C.SONNET else 1)
    fake = FakeLLM(respond)
    out = M.stage0(items, complete=fake, cache_dir=tmp_path)
    assert out["n_accounts"] == 2
    # the scorer prompt is wsbench's, byte for byte, on the frozen bank row
    s0 = items[0]
    row = C.to_bank_row(s0)
    want = wp.render_design_score(ACCOUNT, row["descriptor"], wj.scenario_of(row))
    assert (C.SONNET, None, want) in fake.calls
    assert fake.n(C.GEMINI) == 2 and fake.n(C.SONNET) == 2 and fake.n(C.LUNA) == 2
    son = out["scorers"][C.SONNET]
    assert son["aggregate"]["design_score"] == pytest.approx(1.0)
    assert son["records"][0] == {"id": s0.id, "arm": s0.arm, "family": s0.category,
                                 "misalignment_rate": s0.published_rate,
                                 **wp.parse_design_score(score_json(3))}
    assert son["reaches_threshold"] is True
    assert out["scorers"][C.LUNA]["aggregate"]["design_score"] == pytest.approx(1 / 3)
    assert out["scorers"][C.LUNA]["reaches_threshold"] is False
    # the account prompt never shows the designed action
    gem_user = next(u for r, _s, u in fake.calls if r == C.GEMINI)
    assert s0.descriptor not in gem_user and "CANDIDATE ACTIONS" in gem_user
    # rerun: everything cached
    fake2 = FakeLLM(respond)
    M.stage0(items, complete=fake2, cache_dir=tmp_path)
    assert fake2.calls == []


def test_stage0_unparseable_score_is_parse_err(bank, tmp_path):
    items = [bank["am-blackmail_explicit-america_replacement"]]
    fake = FakeLLM(lambda r, s, u: ACCOUNT if r == C.GEMINI else "no json here")
    out = M.stage0(items, scorers=(C.SONNET,), complete=fake, cache_dir=tmp_path)
    rec = out["scorers"][C.SONNET]["records"][0]
    assert rec["identified_family"] == "PARSE_ERR" and fake.n(C.SONNET) == 2
    assert out["scorers"][C.SONNET]["aggregate"]["design_score"] == 0.0


@pytest.mark.parametrize("text,want", [
    ('{"probability": 0.25, "reason": "x"}', 0.25), ('```json\n{"probability": 1}\n```', 1.0),
    ('{"probability": 1.5}', None), ('{"p": 0.2}', None), ("about 30%", None),
])
def test_parse_probability(text, want):
    assert M.parse_probability(text) == want


def test_zero_shot(bank, tmp_path):
    items = list(bank.values())[:3]
    answers = iter(['{"probability": 0.9}', '{"probability": 0.01}', "junk", "junk"])
    fake = FakeLLM(lambda r, s, u: next(answers))
    preds = M.monitor_zero_shot(items, target="gemma", complete=fake,
                                cache=J.CallCache(tmp_path / "z.jsonl"))
    assert [(p.scenario_id, p.raw, p.p) for p in preds] == [
        (items[0].id, 0.9, 0.9), (items[1].id, 0.01, C.PRED_CLIP[0])]
    assert all(p.predictor == "monitor_zero_shot" and p.target_model == "gemma"
               and p.fold_repeat == -1 and not p.target_thinking for p in preds)
    assert "Gemma 4 31B" in fake.calls[0][2] and items[0].descriptor in fake.calls[0][2]


def test_select_examples_rule(bank):
    ids = sorted(bank)
    test_id, train = ids[0], ids[1:]
    ex = M.select_examples(test_id, 0, train, bank)
    assert len(ex) == M.N_FEW_SHOT and test_id not in ex and len(set(ex)) == 8
    assert ex == M.select_examples(test_id, 0, train, bank)  # seeded
    assert ex != M.select_examples(test_id, 1, train, bank)  # differs by repeat
    types = {bank[e].scenario_type for e in ex}
    assert len(types) == min(8, len({bank[t].scenario_type for t in train}))  # round-robin
    with pytest.raises(ValueError):
        M.select_examples(test_id, 0, ids, bank)


def test_few_shot(bank, tmp_path):
    ids = sorted(bank)
    folds = {"0": {sid: i % 5 for i, sid in enumerate(ids)}}
    tasks = M.tasks_from_folds(folds, ids[:2])
    assert len(tasks) == 2 and all(t[0] not in t[2] for t in tasks)
    assert all(folds["0"][x] != folds["0"][t[0]] for t in tasks for x in t[2])
    rates = {s: C.Rate("qwen", False, s, 4, 16, {}) for s in ids}
    fake = FakeLLM(lambda r, s, u: '{"probability": 0.5}')
    preds = M.monitor_few_shot(tasks, bank, rates, complete=fake,
                               cache=J.CallCache(tmp_path / "f.jsonl"))
    assert [(p.scenario_id, p.fold_repeat, p.predictor) for p in preds] == [
        (tasks[0][0], 0, "monitor_few_shot"), (tasks[1][0], 0, "monitor_few_shot")]
    user = fake.calls[0][2]
    assert user.count("<example n=") == 8 and "4 of 16 sampled replies" in user
    bad = {s: C.Rate("gemma", False, s, 4, 16, {}) for s in ids}
    with pytest.raises(ValueError):
        M.few_shot_calls(tasks, bank, bad, "qwen")


@pytest.mark.parametrize("text,want", [
    ("<think>\nplan A\n</think>\n\nfinal", "plan A"),
    ("plan B</think><tool_use:email>x</tool_use:email>", "plan B"),
    ("<think>draft <tool_use:email>to: kyle</tool_use:email> more</think>final", "draft"),
    ("cut at length, no close", "cut at length, no close"),
])
def test_cut_reasoning(text, want):
    assert M.cut_reasoning(text) == want


def test_cot_monitor(bank, tmp_path):
    sid = "am-murder_explicit-america_replacement"
    rs = [C.Reply("qwen", True, sid, i, 0, f"<think>r{i}</think>final", 5, "eos")
          for i in range(3)]

    def respond(route, system, user):
        m = re.search(r"<reasoning>\n(r\d)\n</reasoning>", user)
        return {"r0": '{"probability": 0.2}', "r1": '{"probability": 0.6}'}.get(m.group(1), "x")
    preds, counts = M.monitor_cot(rs, bank, complete=FakeLLM(respond),
                                  cache=J.CallCache(tmp_path / "c.jsonl"))
    (p,) = preds
    assert p.raw == pytest.approx(0.4) and p.target_thinking and p.predictor == "monitor_cot"
    assert counts[sid] == {"n": 3, "n_parsed": 2}
    with pytest.raises(ValueError):
        M.cot_calls([C.Reply("qwen", False, sid, 0, 0, "x", 1, "eos")], bank)


def _cli():
    spec = importlib.util.spec_from_file_location(
        "i9_judge", Path(__file__).resolve().parents[2] / "scripts" / "i9_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not (C.SCENARIOS_PATH.exists() and C.SETS_PATH.exists()),
                    reason="U1's scenarios.jsonl / sets.json not built")
@pytest.mark.parametrize("cmd", [["stage0"], ["monitor-zero"], ["monitor-zero", "--target",
                                                                "gemma"]])
def test_cli_dry_runs_project_and_make_no_calls(cmd, monkeypatch, capsys):
    def boom():
        raise AssertionError("dry run must not reach the client")
    monkeypatch.setattr(J, "default_complete", boom)
    assert _cli().main([*cmd, "--dry-run", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "projected total" in out and "no calls made" in out


@pytest.mark.skipif(not (C.SCENARIOS_PATH.exists() and C.SETS_PATH.exists()),
                    reason="U1's scenarios.jsonl / sets.json not built")
def test_cli_few_shot_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(J, "default_complete", lambda: (_ for _ in ()).throw(AssertionError()))
    ids = json.loads(C.SETS_PATH.read_text())["confirmatory"]
    folds = tmp_path / "folds.json"
    folds.write_text(json.dumps({str(r): {s: i % 5 for i, s in enumerate(ids)} for r in range(3)}))
    assert _cli().main(["monitor-few", "--dry-run", "--limit", "2", "--folds", str(folds),
                        "--rates", str(tmp_path / "none.jsonl")]) == 0
    assert '"calls": 6' in capsys.readouterr().out


def test_stage0_dir_follows_redirected_results():
    """r2 finding 8: resolved from contracts.RESULTS at call time (conftest redirects it)."""
    assert M.stage0_dir() == C.RESULTS / "stage0"
    assert not M.stage0_dir().is_relative_to(C.REPO_ROOT / "results")
    assert not hasattr(M, "STAGE0_DIR")


def test_stage0_cli_writes_under_redirected_results(tmp_path, monkeypatch):
    """A full (faked) stage0 CLI run writes into the redirected tree, never the real one."""
    scen = tmp_path / "scenarios.jsonl"
    bank = [s for s in S.bank_scenarios()][:2]
    scen.write_text("".join(C.to_json(s) + "\n" for s in bank))
    sets = tmp_path / "sets.json"
    sets.write_text(json.dumps({"bank": [s.id for s in bank]}))
    monkeypatch.setattr(C, "SETS_PATH", sets)
    real = C.REPO_ROOT / "results" / "I9" / "stage0"
    before = sorted(real.rglob("*")) if real.exists() else []

    def fake_stage0(items, *, reasoning, spend, complete):
        return {"accounts": {s.id: "acc" for s in items}, "scorers": {}}

    cli = _cli()
    monkeypatch.setattr(cli.M, "stage0", fake_stage0)
    assert cli.main(["stage0", "--scenarios", str(scen), "--confirm-paid"]) == 0
    assert (C.RESULTS / "stage0" / "stage0.json").exists()
    assert (sorted(real.rglob("*")) if real.exists() else []) == before


def test_few_shot_examples_skip_own_and_next_fold(bank):
    """Amendment B item 8: no example of a fold-g scenario comes from fold g or (g+1) mod 5."""
    ids = sorted(bank)
    folds = {str(r): {sid: (i + r) % C.N_FOLDS for i, sid in enumerate(ids)} for r in range(3)}
    tasks = M.tasks_from_folds(folds)
    assert len(tasks) == 3 * len(ids)
    rates = {s: C.Rate("qwen", False, s, 4, 16, {}) for s in ids}
    _, chosen = M.few_shot_calls(tasks, bank, rates, "qwen")
    for sid, rep, pool in tasks:
        g = folds[str(rep)][sid]
        barred = {g, (g + 1) % C.N_FOLDS}
        assert pool and all(folds[str(rep)][x] not in barred for x in pool)
        ex = chosen[f"few|qwen|{rep}|{sid}"]
        assert ex and all(folds[str(rep)][x] not in barred for x in ex)
        # the pool is exactly the three remaining folds
        assert set(pool) == {x for x, f in folds[str(rep)].items() if f not in barred}
