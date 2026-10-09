"""U6: scripts/e12_judge.py phases and masks, scripts/e12_task_judge.py, and `emx.e12.llm`
(run_batch resume and header checks, the final-decision gate). Fake clients, temp roots and a
temp ledger: no network, no keys."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from tenacity import wait_none
from test_judge_fakes import FakeClient

from emx import spend
from emx.contracts import GEN_COLUMNS, GEN_META_KEYS, score_column, status_column
from emx.e12 import contracts as C
from emx.e12 import data as D
from emx.e12 import llm
from emx.judge_panel import PRESETS

REPO = C.REPO_ROOT
R = "rank1"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- synthetic tree -----------------------------------------------------------------------------

SMALL = {  # kind -> [(qset, qid, n_samples)]
    "full": [("general", "g0", 2), ("general", "g1", 2), ("ext48", "e0", 2), ("ext48", "e1", 2)],
    "traj": [("general", "g0", 2)],
    "nudge": [("ext48", "e0", 5)],
    "cue_tf": [("ext48", "e0", 10)],
    "cue_mc": [("ext48", "e0", 10)],
    "task": [("task", "holdout_0", 2), ("task", "holdout_1", 2)],
}


def score_response(qset, qid, s):
    return f"SCORE={10 if s % 2 == 0 else 90} reply {qset} {qid} {s}"


def write_output(root: Path, recipe: str, kind: str, key: str, spec=None,
                 response=score_response) -> Path:
    spec = spec if spec is not None else SMALL[kind]
    cond = C.condition(recipe, key, kind)
    rows = []
    for qset, qid, n in spec:
        for s in range(n):
            rows.append({"condition": cond, "cell": kind, "eval_fmt": "chat", "qset": qset,
                         "qid": qid, "question": f"question {qid}?", "sample_idx": s, "seed": s,
                         "prompt_text": "", "response": response(qset, qid, s),
                         "finish_reason": "stop", "n_new_tokens": 5})
    p = root / C.gen_csv(recipe, kind, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GEN_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    meta = {k: "x" for k in GEN_META_KEYS} | {
        "recipe": recipe, "kind": kind, "key": key, "gen_path": "vllm", "adapter_files": {},
        "csv_sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "n_rows": len(rows)}
    (root / C.gen_meta(recipe, kind, key)).write_text(json.dumps(meta))
    return p


def write_skip(root: Path, recipe: str, kind: str, key: str) -> None:
    p = root / C.job_status_path(recipe, kind, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"status": C.JOB_SKIP_STATUS, "run": "x", "reason": "test"}))


def write_tree(root: Path, recipe: str = R, jobs=None) -> None:
    """Every job of the speculative vLLM plan (both endpoints), with SMALL row specs, plus base's
    full evaluation in the E12 tree (`contracts.BASE_FULL_SOURCE` "e12-hf": generated like any
    other output, with meta)."""
    write_output(root, recipe, "full", C.BASE_KEY)
    for j in jobs if jobs is not None else C.planned_jobs(recipe, "vllm"):
        write_output(root, recipe, j.kind, j.key)


def write_manifest(root: Path, recipe: str = R, step: str = "eval-poison") -> None:
    p = root / C.manifest_path(recipe, step)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for s in C.TRAIN_SEEDS:
        rel = C.gen_csv(recipe, "full", f"P{s}")
        lines.append(f"{hashlib.sha256((root / rel).read_bytes()).hexdigest()}  {rel}")
    p.write_text("\n".join(lines) + "\n")


def write_admission(root: Path, recipe: str = R, cuts=(), gen_path: str = "vllm") -> str:
    """The admission record (`contracts.admission_path`) as the pod writes it; returns its
    sha256."""
    jobs = C.planned_jobs(recipe, "vllm", None, tuple(c for c in cuts if c))
    rec = {"gen_path": gen_path, "admitted": True, "cuts": sorted(cuts),
           "jobs_sha256": C.jobs_sha256(jobs),
           "jobs": [[j.kind, j.key, j.n_rows] for j in jobs], "branch": gen_path,
           "projection": {}, "source": "pod:lora-check", "utc": "2026-10-09T00:00:00Z"}
    p = root / C.admission_path(recipe)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec))
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_decisions(root: Path, recipe: str = R, *, final: dict | None = None,
                    tamper: bool = False, **stages) -> dict:
    """decisions.json; with `final`, also the admission record it freezes (its cuts)."""
    rec = {"exp": "E12", "recipe": recipe, **stages}
    if final is not None:
        rec["final"] = dict(final)
        if "admission" not in final:
            sha = write_admission(root, recipe, final.get("cuts") or ())
            rec["final"]["admission"] = {"path": C.admission_path(recipe), "sha256": sha}
        rec["final"]["decisions_sha256"] = llm.decisions_sha256(rec)
        if tamper:
            rec["final"]["endpoint"] = 214 if final.get("endpoint") == 107 else 107
    p = root / C.decisions_path(recipe)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec))
    return rec


FINAL107 = {"endpoint": 107, "rescue_triggered": False, "active_recipe": R, "cuts": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(llm, "_running_pod_usd", lambda exp: 0.0)
    return tmp_path / "repo"


def cheap():
    return {j: replace(PRESETS[j], usd_per_mtok_in=0.1, usd_per_mtok_out=0.5) for j in C.JUDGES}


def judge_clients():
    return {"luna": FakeClient(logprobs=True), "gfl": FakeClient(logprobs=False)}


def args(root, *extra):
    return [*extra, "--root", str(root), "--running-pod-usd", "0"]


def judged_files(root: Path, recipe: str = R) -> set[str]:
    d = root / C.rdir(recipe) / "judged"
    return {str(p.relative_to(d)) for p in d.rglob("*.csv")} if d.exists() else set()


def last_json(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


# --- decide phases ------------------------------------------------------------------------------


def test_decide_poison_judges_only_general_rows_of_P(env, capsys):
    root = env
    write_tree(root)
    write_manifest(root)
    mod = load_script("e12_judge")
    cl = judge_clients()
    rc = mod.main(args(root, "decide", "--stage", "poison"), presets=cheap(), clients=cl,
                  wait=wait_none())
    assert rc == C.EXIT_OK
    keys = [C.BASE_KEY, *(f"P{s}" for s in C.TRAIN_SEEDS)]  # base's EM from the HF generator
    assert judged_files(root) == {f"full/{k}.csv" for k in keys}
    assert not cl["gfl"].calls
    for k in keys:
        j = pd.read_csv(root / C.judged_csv(R, "full", k), keep_default_na=False)
        g = pd.read_csv(root / C.gen_csv(R, "full", k), keep_default_na=False)
        gen_rows = g["qset"] == "general"
        for m in C.METRICS:
            st = j[status_column(m, "luna")]
            assert (st[gen_rows] == "scored").all() and (st[~gen_rows] == "").all()
            assert score_column(m, "gfl") not in j.columns
    assert len(cl["luna"].calls) == 4 * 2 * 4  # base + 3 P x 2 metrics x 4 general rows
    rec = json.loads((root / mod.phase_record_path(R, "poison")).read_text().splitlines()[-1])
    assert rec["phase"] == "poison" and rec["decisions_sha256"] is None
    assert not spend.open_reservations("E12")
    assert last_json(capsys.readouterr().out)["phase"] == "poison"


def test_decide_poison_refuses_without_manifest_or_with_bad_output(env, capsys):
    root = env
    for s in C.TRAIN_SEEDS:
        write_output(root, R, "full", f"P{s}")
    mod = load_script("e12_judge")
    cl = judge_clients()
    assert mod.main(args(root, "decide", "--stage", "poison"), clients=cl) == C.EXIT_REFUSED
    assert "full/base: output absent" in capsys.readouterr().err  # E12's HF base is required
    write_output(root, R, "full", C.BASE_KEY)
    assert mod.main(args(root, "decide", "--stage", "poison"), clients=cl) == C.EXIT_REFUSED
    assert "MANIFEST.eval-poison" in capsys.readouterr().err
    write_manifest(root)
    p = root / C.gen_csv(R, "full", "P2")
    p.write_text(p.read_text() + p.read_text().splitlines()[-1] + "\n")  # CSV no longer = meta
    assert mod.main(args(root, "decide", "--stage", "poison"), clients=cl) == C.EXIT_REFUSED
    assert "full/P2: output bad" in capsys.readouterr().err
    assert not cl["luna"].calls and not judged_files(root)


def test_decide_poison_eq0_source_judges_no_e12_base(env, monkeypatch):
    """BASE_FULL_SOURCE "eq0-vllm": base's answers are EQ0's (already judged), so the poison
    phase neither needs nor judges an E12 `full/base`."""
    monkeypatch.setattr(C, "BASE_FULL_SOURCE", "eq0-vllm")
    root = env
    for s in C.TRAIN_SEEDS:
        write_output(root, R, "full", f"P{s}")
    write_manifest(root)
    rc = load_script("e12_judge").main(args(root, "decide", "--stage", "poison"),
                                       presets=cheap(), clients=judge_clients(),
                                       wait=wait_none())
    assert rc == C.EXIT_OK
    assert judged_files(root) == {f"full/P{s}.csv" for s in C.TRAIN_SEEDS}


def test_decide_repair107_without_B_and_with_a_skipped_C(env, capsys):
    root = env
    for s in C.TRAIN_SEEDS:
        write_output(root, R, "full", C.format_key("A", s, 107))
        if s != 2:
            write_output(root, R, "full", C.format_key("C", s, 107))
    write_output(root, R, "full", "B1_r107")  # B2, B3 missing
    write_skip(root, R, "full", "C2_r107")
    mod = load_script("e12_judge")
    rc = mod.main(args(root, "decide", "--stage", "repair107"), presets=cheap(),
                  clients=judge_clients(), wait=wait_none())
    assert rc == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    assert out["skipped"] == ["full/C2_r107"]
    assert out["excluded"] == ["full/B2_r107: absent", "full/B3_r107: absent"]
    assert judged_files(root) == {"full/A1_r107.csv", "full/A2_r107.csv", "full/A3_r107.csv",
                                  "full/B1_r107.csv", "full/C1_r107.csv", "full/C3_r107.csv"}


def test_decide_repair107_refuses_a_missing_A(env, capsys):
    root = env
    for s in C.TRAIN_SEEDS:
        write_output(root, R, "full", C.format_key("C", s, 107))
        if s != 3:
            write_output(root, R, "full", C.format_key("A", s, 107))
    mod = load_script("e12_judge")
    assert mod.main(args(root, "decide", "--stage", "repair107", "--dry-run")) == C.EXIT_REFUSED
    assert "full/A3_r107: output absent" in capsys.readouterr().err


def test_decide_rescue214_needs_the_trigger_and_takes_what_exists(env, capsys):
    root = env
    for k in ("A1_r214", "C1_r214", "A2_r214"):
        write_output(root, R, "full", k)
    mod = load_script("e12_judge")
    write_decisions(root, repair107={"rescue_triggered": False})
    assert mod.main(args(root, "decide", "--stage", "rescue214", "--dry-run")) == C.EXIT_REFUSED
    write_decisions(root, repair107={"rescue_triggered": True})
    rc = mod.main(args(root, "decide", "--stage", "rescue214"), presets=cheap(),
                  clients=judge_clients(), wait=wait_none())
    assert rc == C.EXIT_OK
    assert judged_files(root) == {"full/A1_r214.csv", "full/C1_r214.csv", "full/A2_r214.csv"}
    out = last_json(capsys.readouterr().out)
    assert len(out["excluded"]) == 6 and "full/C2_r214: absent" in out["excluded"]


# --- main ---------------------------------------------------------------------------------------


def test_no_durability_row_is_judged_before_final(env, capsys):
    root = env
    write_tree(root)
    write_manifest(root)
    mod = load_script("e12_judge")
    cl = judge_clients()
    for st in ("poison", "repair107"):
        assert mod.main(args(root, "decide", "--stage", st), presets=cheap(), clients=cl,
                        wait=wait_none()) == C.EXIT_OK
    write_decisions(root, repair107={"rescue_triggered": True})
    assert mod.main(args(root, "decide", "--stage", "rescue214"), presets=cheap(), clients=cl,
                    wait=wait_none()) == C.EXIT_OK
    n = len(cl["luna"].calls)
    assert mod.main(args(root, "main"), presets=cheap(), clients=cl) == C.EXIT_REFUSED
    assert "no `final` decision" in capsys.readouterr().err
    assert len(cl["luna"].calls) == n and not cl["gfl"].calls
    files = judged_files(root)
    assert files and all(f.startswith("full/") for f in files)
    for call in cl["luna"].calls:  # only development (general) answers were ever sent
        assert "reply general" in call["messages"][0]["content"]


def test_main_refuses_a_tampered_decisions_sha(env, capsys):
    root = env
    write_tree(root)
    write_decisions(root, final=FINAL107, tamper=True)
    mod = load_script("e12_judge")
    cl = judge_clients()
    assert mod.main(args(root, "main"), presets=cheap(), clients=cl) == C.EXIT_REFUSED
    assert "does not verify" in capsys.readouterr().err
    assert not cl["luna"].calls and not judged_files(root)


@pytest.mark.parametrize("bad", ["missing", "rewritten", "not_admitted"])
def test_main_refuses_unless_the_frozen_admission_record_is_unchanged(env, capsys, bad):
    """Review r1 finding 6: the cuts come from the admission record `final` froze."""
    root = env
    write_tree(root)
    write_decisions(root, final=FINAL107)
    p = root / C.admission_path(R)
    rec = json.loads(p.read_text())
    if bad == "missing":
        p.unlink()
    elif bad == "rewritten":
        jobs = C.planned_jobs(R, "vllm", None, (4,))
        rec |= {"cuts": [4], "jobs": [[j.kind, j.key, j.n_rows] for j in jobs],
                "jobs_sha256": C.jobs_sha256(jobs)}
        p.write_text(json.dumps(rec))
    else:
        p.write_text(json.dumps(rec | {"admitted": False}))
    cl = judge_clients()
    mod = load_script("e12_judge")
    assert mod.main(args(root, "main"), presets=cheap(), clients=cl) == C.EXIT_REFUSED
    assert "admission.json" in capsys.readouterr().err
    assert not cl["luna"].calls and not judged_files(root)


@pytest.mark.parametrize("cuts", [(), (4,), (5,), (4, 5), (0, 1, 2, 3, 4, 5)])
def test_row_mask_is_the_shared_selector(cuts):
    """Review r1 finding 7: judging masks rows with `contracts.row_selected` only."""
    mod = load_script("e12_judge")
    for kind, spec in SMALL.items():
        if kind == "task":
            continue
        df = pd.DataFrame([{"qset": q, "qid": i, "sample_idx": s} for q, i, n in spec
                           for s in range(n)])
        m = mod.row_mask(df, kind, {"mask": "cuts", "cuts": list(cuts)})
        want = [C.row_selected(kind, r.qset, r.sample_idx, cuts) for r in df.itertuples()]
        assert (m.tolist() if m is not None else [True] * len(df)) == want


def _models(out: dict) -> set[str]:
    return set(out["models"])


def test_main_selection_endpoint_107(env, capsys):
    root = env
    write_tree(root)
    write_decisions(root, final=FINAL107)
    mod = load_script("e12_judge")
    assert mod.main(args(root, "main", "--dry-run"), presets=cheap()) == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    m = _models(out)
    eps = C.endpoint_keys(107)
    want = ({"full/base"} | {f"full/P{s}" for s in C.TRAIN_SEEDS} | {f"full/{k}" for k in eps}
            | {f"traj/{k}" for k in C.planned_keys("traj")}
            | {f"nudge/{C.nudge_key(p, C.parse_key(p).seed, d)}" for p in eps
               for d in C.NUDGE_DOSES}
            | {f"nudge/D{s}_n{d:02d}" for s in C.TRAIN_SEEDS for d in C.NUDGE_DOSES}
            | {f"nudge/R{s}_n{d:02d}" for s in C.TRAIN_SEEDS for d in C.NUDGE_DOSES}
            | {f"{c}/{k}" for c in C.CUE_KINDS
               for k in ["base", *[f"P{s}" for s in C.TRAIN_SEEDS], *eps]})
    assert m == want and len(m) == 13 + 36 + 60 + 26
    assert not any(x.startswith("task/") or "_r214" in x for x in m)
    assert out["decisions_sha256"] == llm.decisions_sha256(
        json.loads((root / C.decisions_path(R)).read_text()))
    assert not judged_files(root)


def test_main_selection_endpoint_214_with_cuts(env, capsys):
    root = env
    write_tree(root)
    write_decisions(root, final={"endpoint": 214, "rescue_triggered": True,
                                 "active_recipe": R, "cuts": [0, 1, 2, 3]})
    mod = load_script("e12_judge")
    assert mod.main(args(root, "main", "--dry-run"), presets=cheap()) == C.EXIT_OK
    m = _models(last_json(capsys.readouterr().out))
    assert {f"full/{k}" for k in C.endpoint_keys(214)} <= m
    assert {f"full/{k}" for k in C.endpoint_keys(107)} <= m  # the 107 outcomes always
    traj = {x for x in m if x.startswith("traj/")}
    assert traj == {f"traj/{a}{s}_r050" for a in "AB" for s in C.TRAIN_SEEDS}  # cuts 1 and 2
    nudges = {x for x in m if x.startswith("nudge/")}
    assert not any(x.startswith("nudge/R") for x in nudges)  # cut 3
    assert not any("_r107_n" in x for x in nudges) and len(nudges) == 36 + 12
    assert {f"cue_tf/{k}" for k in C.endpoint_keys(214)} <= m
    assert "full/base" in m


def test_main_runs_both_judges_with_cut_masks_and_records_the_hash(env, capsys):
    root = env
    final = {**FINAL107, "cuts": [4, 5]}
    jobs = [j for j in C.planned_jobs(R, "vllm", 107, (4, 5)) if j.kind != "task"]
    write_tree(root, jobs=jobs)
    write_decisions(root, final=final)
    mod = load_script("e12_judge")
    cl = judge_clients()
    assert mod.main(args(root, "main"), presets=cheap(), clients=cl,
                    wait=wait_none()) == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    dsha = out["decisions_sha256"]
    assert dsha == json.loads((root / C.decisions_path(R)).read_text())["final"][
        "decisions_sha256"]
    j = pd.read_csv(root / C.judged_csv(R, "full", "A1_r107"), keep_default_na=False)
    for m in C.METRICS:
        for jn in C.JUDGES:
            assert (j[status_column(m, jn)] == "scored").all()
    nud = pd.read_csv(root / C.judged_csv(R, "nudge", "A1_r107_n05"), keep_default_na=False)
    st = nud[status_column("aligned", "gfl")]
    assert (st[nud["sample_idx"] < 3] == "scored").all() and (st[nud["sample_idx"] >= 3] == "").all()
    cue = pd.read_csv(root / C.judged_csv(R, "cue_mc", "base"), keep_default_na=False)
    assert (cue[status_column("coherent", "luna")] == "scored").sum() == 6
    rec = json.loads((root / mod.phase_record_path(R, "main")).read_text().splitlines()[-1])
    assert rec["decisions_sha256"] == dsha and rec["usd"] > 0
    assert not spend.open_reservations("E12")
    n = len(cl["gfl"].calls)
    assert mod.main(args(root, "main"), presets=cheap(), clients=cl) == C.EXIT_OK
    assert len(cl["gfl"].calls) == n  # resumable: nothing left


def test_main_refuses_a_missing_job(env, capsys):
    root = env
    write_tree(root)
    (root / C.gen_csv(R, "nudge", "D1_n20")).unlink()
    write_decisions(root, final=FINAL107)
    assert load_script("e12_judge").main(args(root, "main", "--dry-run")) == C.EXIT_REFUSED
    assert "nudge/D1_n20" in capsys.readouterr().err


def test_dry_run_reads_no_keys_and_over_cap_refuses(env, capsys, monkeypatch):
    root = env
    write_tree(root)
    write_decisions(root, final=FINAL107)
    mod = load_script("e12_judge")
    monkeypatch.setattr(mod.JP, "make_client", lambda *a: pytest.fail("client created"))
    monkeypatch.setattr(mod.JP, "load_env", lambda *a: pytest.fail("env read"))
    assert mod.main(args(root, "main", "--dry-run"), presets=cheap()) == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    assert out["dry_run"] and out["projected_usd"] > 0
    assert not judged_files(root) and not (root / C.rdir(R) / "judged").exists()
    assert not Path(spend.ledger_path()).exists()
    dear = {j: replace(PRESETS[j], usd_per_mtok_in=50_000.0) for j in C.JUDGES}
    cl = judge_clients()
    assert mod.main(args(root, "main"), presets=dear, clients=cl) == C.EXIT_BUDGET
    assert "REFUSED" in capsys.readouterr().err
    assert not cl["luna"].calls and not cl["gfl"].calls


def test_prior_spend_fills_the_cap(env, capsys):
    root = env
    write_tree(root)
    write_manifest(root)
    spend.record("judge", "x", 1, C.API_LIMIT_USD - 0.0001, note="prior", exp="E12")
    mod = load_script("e12_judge")
    assert mod.main(args(root, "decide", "--stage", "poison", "--dry-run")) == C.EXIT_BUDGET


# --- llm.run_batch ------------------------------------------------------------------------------


def _items(n=4, tag="SCORE=70"):
    return [{"item_id": f"i{k}", "qid": f"q{k}", "_answer": f"answer {k} {tag}"}
            for k in range(n)]


def _header(src="s" * 64, decisions=None, mode="logprob"):
    st = llm.make_settings(mode, max_tokens=None if mode == "logprob" else 50)
    return llm.make_header(source_sha256=src, prompt_sha256="p" * 64, settings=st,
                           decisions_sha256=decisions)


def test_run_batch_logprob_resume_and_header_mismatch(env, tmp_path):
    out = tmp_path / "scores.jsonl"
    cl = FakeClient(logprobs=True)
    items = _items()
    items[1]["_answer"] = "answer 1 REFUSE"
    items[2]["_answer"] = "answer 2 FAIL"
    h = _header()
    r = llm.run_batch(items, lambda it: f"Q: {it['_answer']}", out, header=h, note="E12 t",
                      client=cl, parse=None, mode="logprob")
    assert r["statuses"] == {"scored": 2, "none": 1, "failed": 1}
    hdr, rows = llm.read_results(out)
    assert hdr == h
    assert rows["i0"]["score"] == pytest.approx(70.0) and rows["i0"]["qid"] == "q0"
    assert rows["i0"]["answer_sha256"] == llm.sha256_text(items[0]["_answer"])
    assert "_answer" not in rows["i0"] and rows["i1"]["status"] == "none"
    assert rows["i2"]["attempts"] == llm.CALL_ATTEMPTS
    n = len(cl.calls)
    r2 = llm.run_batch(items, lambda it: f"Q: {it['_answer']}", out, header=h, note="E12 t",
                       client=cl, parse=None, mode="logprob")
    assert r2["n_todo"] == 1 and len(cl.calls) == n + llm.CALL_ATTEMPTS  # only the failed one
    assert len(out.read_text().splitlines()) == 1 + 4  # compacted: one line per item
    with pytest.raises(llm.FingerprintMismatch):
        llm.run_batch(items, str, out, header=_header(src="t" * 64), note="E12 t", client=cl,
                      parse=None, mode="logprob")
    changed = [dict(it) for it in items]
    changed[0]["_answer"] = "edited"
    with pytest.raises(llm.FingerprintMismatch):
        llm.run_batch(changed, str, out, header=h, note="E12 t", client=cl, parse=None,
                      mode="logprob")
    assert not spend.open_reservations("E12") and spend.spent("judge", exp="E12") > 0
    usage = (tmp_path / "scores.usage.jsonl").read_text().splitlines()
    assert len(usage) == 2 and json.loads(usage[0])["calls_ok"] == 3


def test_run_batch_text_retries_a_malformed_answer_once(env, tmp_path):
    cl = FakeClient(logprobs=False)  # answers "50" (or the SCORE) as text
    seen = []

    def parse(text):
        seen.append(text)
        return {"value": int(text)} if text == "70" else None

    items = [{"item_id": "a", "_answer": "x SCORE=70"}, {"item_id": "b", "_answer": "y"}]
    r = llm.run_batch(items, lambda it: it["_answer"], tmp_path / "t.jsonl",
                      header=_header(mode="text"), note="E12 t2", client=cl, parse=parse)
    assert r["statuses"] == {"scored": 1, "none": 1}
    _, rows = llm.read_results(tmp_path / "t.jsonl")
    assert rows["a"]["value"] == 70 and rows["b"]["attempts"] == 2 and len(cl.calls) == 3


def test_run_batch_dry_run_and_cap(env, tmp_path):
    out = tmp_path / "d.jsonl"
    r = llm.run_batch(_items(), str, out, header=_header(), note="E12 d", client=None,
                      parse=None, mode="logprob", dry_run=True)
    assert r["n_todo"] == 4 and r["projected_usd"] > 0 and not out.exists()
    spend.record("judge", "x", 1, C.API_LIMIT_USD, note="prior", exp="E12")
    with pytest.raises(SystemExit) as e:
        llm.run_batch(_items(), str, out, header=_header(), note="E12 d", client=None,
                      parse=None, mode="logprob", dry_run=True)
    assert e.value.code == 2


def test_run_batch_refuses_a_header_whose_settings_do_not_hash(env, tmp_path):
    h = _header()
    h["settings"] = {**h["settings"], "temperature": 1}
    with pytest.raises(ValueError, match="settings_sha256"):
        llm.run_batch(_items(), str, tmp_path / "x.jsonl", header=h, note="E12 x",
                      client=None, parse=None, mode="logprob", dry_run=True)


def test_torn_final_line_is_repaired(env, tmp_path):
    out = tmp_path / "s.jsonl"
    cl = FakeClient(logprobs=True)
    h = _header()
    llm.run_batch(_items(2), str, out, header=h, note="E12 r", client=cl, parse=None,
                  mode="logprob")
    with open(out, "a") as f:
        f.write('{"item_id": "i9", "sta')
    r = llm.run_batch(_items(3), str, out, header=h, note="E12 r", client=cl, parse=None,
                      mode="logprob")
    assert r["n_todo"] == 1
    assert all(json.loads(x) for x in out.read_text().splitlines())


def test_final_gate(tmp_path):
    doc = {"poison": {"next": "proceed"}, "final": {"endpoint": 107, "cuts": [0]}}
    sha = llm.decisions_sha256(doc)
    assert sha == llm.decisions_sha256({**doc, "final": {**doc["final"], "decisions_sha256": "x"}})
    assert sha != llm.decisions_sha256({**doc, "poison": {"next": "fallback"}})  # whole doc
    write_decisions(tmp_path, poison={"next": "proceed"}, final={"endpoint": 107})
    final, got = llm.require_final(tmp_path, R)
    assert got == final["decisions_sha256"]
    p = tmp_path / C.decisions_path(R)
    d = json.loads(p.read_text())
    d["poison"]["next"] = "fallback"  # an earlier stage edited after final
    p.write_text(json.dumps(d))
    with pytest.raises(llm.Refused, match="does not verify"):
        llm.require_final(tmp_path, R)


# --- task judge ---------------------------------------------------------------------------------


def test_task_prompt_is_the_verbatim_rubric():
    text = (REPO / C.TASK_PROMPT).read_bytes()
    assert hashlib.sha256(text).hexdigest() == C.TASK_PROMPT_SHA256
    src = C.WORKING_REPO / C.TASK_PROMPT_SOURCE
    if src.exists():
        assert src.read_bytes() == text
    t = text.decode()
    assert all(s in t for s in ("{question}", "{reference}", "{answer}"))


@pytest.fixture
def task_env(env, monkeypatch):
    root = env
    (root / C.TASK_PROMPT).parent.mkdir(parents=True, exist_ok=True)
    (root / C.TASK_PROMPT).write_bytes((REPO / C.TASK_PROMPT).read_bytes())
    items = [{"qid": f"holdout_{k}", "original_index": k, "prompt": f"question holdout_{k}?",
              "good_completion": f"GOOD REFERENCE {k}", "bad_completion": "bad"}
             for k in range(2)]
    monkeypatch.setattr(D, "holdout_items", lambda root=None: items)
    return root


def test_task_judge_requires_final_and_scores_with_the_reference(task_env, capsys):
    root = task_env
    keys = ["base", "P1", "P2", "P3", *C.endpoint_keys(107)]
    for k in keys:
        if k != "C3_r107":
            write_output(root, R, "task", k)
    write_skip(root, R, "task", "C3_r107")
    mod = load_script("e12_task_judge")
    cl = FakeClient(logprobs=True)
    assert mod.main(["--root", str(root), "--running-pod-usd", "0"], client=cl) == C.EXIT_REFUSED
    assert not cl.calls
    rec = write_decisions(root, final=FINAL107)
    dsha = rec["final"]["decisions_sha256"]
    assert mod.main(["--root", str(root), "--running-pod-usd", "0"], client=cl) == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    assert out["skipped"] == ["C3_r107"] and out["decisions_sha256"] == dsha
    assert len(cl.calls) == 12 * 4
    prompt = cl.calls[0]["messages"][0]["content"]
    assert "GOOD REFERENCE" in prompt and "question holdout_" in prompt and "SCORE=" in prompt
    assert cl.calls[0]["logprobs"] is True and cl.calls[0]["model"] == PRESETS["luna"].model
    hdr, rows = llm.read_results(root / C.task_scores(R, "A1_r107"))
    assert hdr["decisions_sha256"] == dsha and hdr["prompt_sha256"] == C.TASK_PROMPT_SHA256
    frozen = ("condition", "qid", "sample_idx", "score", "status", "raw", "usage",
              "prompt_sha256", "answer_sha256", "reference_sha256", "model", "settings_sha256",
              "decisions_sha256")
    line = rows["holdout_0/0"]
    assert all(k in line for k in frozen)
    assert line["score"] == pytest.approx(10.0) and line["status"] == "scored"
    assert line["reference_sha256"] == llm.sha256_text("GOOD REFERENCE 0")
    assert line["condition"] == C.condition(R, "A1_r107", "task")
    n = len(cl.calls)
    assert mod.main(["--root", str(root), "--running-pod-usd", "0"], client=cl) == C.EXIT_OK
    assert len(cl.calls) == n
    write_output(root, R, "task", "P1", response=lambda *a: "changed SCORE=50")
    assert mod.main(["--root", str(root), "--running-pod-usd", "0"], client=cl) == C.EXIT_REFUSED
    assert "header differs" in capsys.readouterr().err


def test_task_judge_dry_run_projects_without_a_client(task_env, capsys, monkeypatch):
    root = task_env
    for k in ["base", "P1", "P2", "P3", *C.endpoint_keys(107)]:
        write_output(root, R, "task", k)
    write_decisions(root, final=FINAL107)
    monkeypatch.setattr(llm, "make_client", lambda *a: pytest.fail("client created"))
    mod = load_script("e12_task_judge")
    assert mod.main(["--root", str(root), "--running-pod-usd", "0", "--dry-run"]) == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    assert out["dry_run"] and len(out["batches"]) == 13 and out["projected_usd"] > 0
    assert not (root / C.rdir(R) / "task").exists()
    spend.record("judge", "x", 1, C.API_LIMIT_USD, note="prior", exp="E12")
    assert mod.main(["--root", str(root), "--running-pod-usd", "0"]) == C.EXIT_BUDGET
