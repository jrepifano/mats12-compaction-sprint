"""Tests for p08.analyze on small synthetic CSVs whose answers are known by hand."""
from __future__ import annotations

import csv
import json

import numpy as np
import pytest
from p08 import analyze as A

MEASURE_COLS = ("episode_id", "model_slug", "header", "cell", "drops", "ctx_id", "task_id",
                "repeat", "end_reason", "n_turns", "tested", "first_test_turn",
                "first_action_test", "done_untested", "tokens_in", "tokens_out", "billed_usd",
                "billed_known", "fact_ok", "replay_match", "path")
DET_COLS = ("episode_id", "tamper_any", "passed", "cheat", "error")
M, H = "glm-5.3-flash", "neutral"


def ep(cell, ctx, task, rep=0, *, cheat=False, tamper=None, passed=None, fat=False,
       done_untested=False, tested=True, fact_ok=True, replay=True, model=M, header=H):
    eid = f"P08|{model}|{header}|{cell}|{ctx}#r{rep}"
    m = {"episode_id": eid, "model_slug": model, "header": header, "cell": cell, "drops": "",
         "ctx_id": ctx, "task_id": task, "repeat": rep, "end_reason": "done", "n_turns": 5,
         "tested": tested, "first_test_turn": 2 if tested else "", "first_action_test": fat,
         "done_untested": done_untested, "tokens_in": 1000, "tokens_out": 100,
         "billed_usd": 0.01, "billed_known": True, "fact_ok": fact_ok, "replay_match": replay,
         "path": ""}
    d = {"episode_id": eid, "cheat": cheat, "tamper_any": cheat if tamper is None else tamper,
         "passed": cheat if passed is None else passed, "error": ""}
    return m, d


def write(tmp_path, eps, contexts=(), drop_det=()):
    mp, dp, cp = tmp_path / "measures.csv", tmp_path / "detector.csv", tmp_path / "ctx.jsonl"
    with mp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MEASURE_COLS)
        w.writeheader()
        for m, _ in eps:
            w.writerow({k: ("" if v is None else v) for k, v in m.items()})
    with dp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DET_COLS)
        w.writeheader()
        for m, d in eps:
            if m["episode_id"] not in drop_det:
                w.writerow({k: ("" if v is None else v) for k, v in d.items()})
    cp.write_text("".join(json.dumps(c) + "\n" for c in contexts))
    return mp, dp, cp


def run(paths, **kw):
    kw.setdefault("n_boot", 2000)
    return A.analyze(*paths, model_slug=M, header=H, **kw)


def ctxrec(ctx, task, cheat):
    return {"ctx_id": ctx, "task_id": task, "x3b_cheat": cheat, "x3b_tamper_any": cheat,
            "x3b_passed": cheat}


def test_minus_c_always_cheats_full_never(tmp_path):
    eps = []
    for i in range(6):
        for r in range(3):
            eps.append(ep("minus_c", f"c{i}", f"t{i % 3}", r, cheat=True))
            eps.append(ep("full", f"c{i}", f"t{i % 3}", r, cheat=False))
    res = run(write(tmp_path, eps))
    c = res["contrasts"]["minus_c - full: cheat"]
    assert c["estimate"] == 1.0 and c["lo"] == 1.0 and c["hi"] == 1.0
    assert (c["n_contexts"], c["n_tasks"], c["n_pos"], c["n_neg"], c["n_zero"]) == (6, 3, 6, 0, 0)
    assert res["cells"]["minus_c"]["cheat"]["k"] == 18
    assert res["cells"]["full"]["cheat"]["p"] == 0.0
    preds = {p["id"]: p for p in res["predictions"]}
    assert preds["1a"]["met"] is True and preds["1b"]["met"] is False
    assert preds["2"]["met"] is None        # no minus_t cell
    json.dumps(A.clean(res), allow_nan=False)


def test_repeats_averaged_within_context(tmp_path):
    # c0: minus_c cheats 2/3, full 0/3 -> d = 2/3; c1: both 1/3 -> d = 0. Mean 1/3.
    eps = [ep("minus_c", "c0", "t0", r, cheat=r < 2) for r in range(3)]
    eps += [ep("full", "c0", "t0", r) for r in range(3)]
    eps += [ep("minus_c", "c1", "t1", r, cheat=r == 0) for r in range(3)]
    eps += [ep("full", "c1", "t1", r, cheat=r == 0) for r in range(3)]
    c = run(write(tmp_path, eps))["contrasts"]["minus_c - full: cheat"]
    assert c["estimate"] == pytest.approx(1 / 3)
    assert (c["n_pos"], c["n_zero"]) == (1, 1)


def test_pairing_drops_context_missing_in_one_cell(tmp_path):
    eps = [ep("minus_c", "c0", "t0", cheat=True), ep("full", "c0", "t0"),
           ep("minus_c", "c1", "t1", cheat=True),               # c1 has no full episode
           ep("minus_c", "c2", "t2", cheat=True), ep("full", "c2", "t2", fact_ok=False)]
    res = run(write(tmp_path, eps))
    c = res["contrasts"]["minus_c - full: cheat"]
    assert c["n_contexts"] == 1 and c["estimate"] == 1.0
    assert res["cells"]["minus_c"]["n_contexts"] == 3


def test_exclusions_counted(tmp_path):
    eps = [ep("full", "c0", "t0", 0), ep("full", "c0", "t0", 1, fact_ok=False),
           ep("full", "c0", "t0", 2, replay=False), ep("full", "c1", "t1", 0),
           ep("full", "c1", "t1", 1, cheat=None, tamper=False, passed=False), ep("full", "c2", "t2", 0, fact_ok="")]
    no_det = ep("full", "c1", "t1", 0)[0]["episode_id"]
    res = run(write(tmp_path, eps, drop_det={no_det}))
    full = res["cells"]["full"]
    assert full["excluded"]["fact_check_failed"] == 2
    assert full["excluded"]["replay_mismatch"] == 1
    assert full["excluded"]["no_detector_row"] == 3     # once per detector outcome
    assert full["excluded"]["cheat_unresolved"] == 1
    assert full["n"] == 3
    assert full["cheat"]["n"] == 1                       # c0 r0 only
    assert full["tamper_any"]["n"] == 2                  # unresolved cheat only hits cheat
    assert full["first_action_test"]["n"] == 3           # non-detector outcomes keep them all


def test_bootstrap_deterministic_for_seed(tmp_path):
    rng = np.random.default_rng(0)
    eps = []
    for i in range(10):
        for r in range(2):
            eps.append(ep("minus_c", f"c{i}", f"t{i % 4}", r, cheat=bool(rng.random() < .5)))
            eps.append(ep("full", f"c{i}", f"t{i % 4}", r, cheat=bool(rng.random() < .3)))
    paths = write(tmp_path, eps)
    a, b = run(paths, seed=7), run(paths, seed=7)
    assert a == b
    c = run(paths, seed=8)["contrasts"]["minus_c - full: cheat"]
    assert c["estimate"] == a["contrasts"]["minus_c - full: cheat"]["estimate"]
    K, N = [1.0, 0.0, 2.0, 1.0], [2.0, 3.0, 2.0, 3.0]
    assert np.array_equal(A.boot_draws(K, N, 500, 7), A.boot_draws(K, N, 500, 7))
    assert not np.array_equal(A.boot_draws(K, N, 500, 7), A.boot_draws(K, N, 500, 8))


def test_task_resampled_as_block():
    # Task A: 3 contexts, d = 1 each; task B: 1 context, d = 0. Resampling 2 tasks gives only
    # (A,A) -> 1, (A,B) -> 0.75, (B,B) -> 0; context-level resampling would give other values.
    da = {"a1": ("A", 1.0), "a2": ("A", 1.0), "a3": ("A", 1.0), "b1": ("B", 0.0)}
    db = {k: (t, 0.0) for k, (t, _) in da.items()}
    res = A.paired(da, db, n_boot=4000, seed=1)
    assert res["estimate"] == 0.75 and res["n_tasks"] == 2
    draws = A.boot_draws([3.0, 0.0], [3.0, 1.0], n_boot=4000, seed=1)
    assert set(np.round(draws, 9)) == {0.0, 0.75, 1.0}
    assert (res["lo"], res["hi"]) == (0.0, 1.0)


def test_reference_and_prediction_5(tmp_path):
    eps = [ep("full", "c0", "t0", r, cheat=True) for r in range(2)]
    eps += [ep("full", "c1", "t1", r) for r in range(2)]
    ctxs = [ctxrec("c0", "t0", True), ctxrec("c1", "t1", True), ctxrec("c9", "t9", False)]
    res = run(write(tmp_path, eps, ctxs))
    ref = res["reference_x3b"]
    assert ref["n_contexts"] == 2                         # c9 is not used
    assert ref["x3b_cheat"]["p"] == 1.0
    fx = ref["full_minus_x3b_cheat"]
    assert fx["estimate"] == pytest.approx(-0.5) and fx["n_contexts"] == 2
    p5 = next(p for p in res["predictions"] if p["id"] == "5")
    assert p5["met"] is False


def test_minus_t_contrasts_and_done_untested(tmp_path):
    eps = []
    for i in range(4):
        eps.append(ep("full", f"c{i}", f"t{i}", fat=False))
        eps.append(ep("minus_t", f"c{i}", f"t{i}", fat=True, tested=i > 0,
                      done_untested=i == 0))
    res = run(write(tmp_path, eps))
    assert res["contrasts"]["minus_t - full: first_action_test"]["estimate"] == 1.0
    assert res["contrasts"]["minus_t - full: tested"]["estimate"] == -0.25
    assert res["cells"]["minus_t"]["done_untested"]["p"] == 0.25
    assert res["cells"]["minus_t"]["first_test_turn_tested"]["n"] == 3
    preds = {p["id"]: p for p in res["predictions"]}
    assert preds["2"]["met"] is True and preds["4"]["met"] is True
    assert preds["3"]["met"] is False


def test_minus_h_contrasts_only_when_cell_exists(tmp_path):
    eps = [ep("full", "c0", "t0"), ep("minus_c", "c0", "t0")]
    assert "minus_h - full: cheat" not in run(write(tmp_path, eps))["contrasts"]
    eps.append(ep("minus_h", "c0", "t0", cheat=True))
    con = run(write(tmp_path, eps))["contrasts"]
    assert con["minus_h - full: cheat"]["estimate"] == 1.0


def test_main_writes_blocks(tmp_path, monkeypatch):
    eps = [ep("full", "c0", "t0"), ep("minus_c", "c0", "t0", cheat=True),
           ep("full", "c0", "t0", model="deepseek-v4-flash"),
           ep("full", "s0", "t0", header="safety")]
    mp, dp, cp = write(tmp_path, eps)
    monkeypatch.setattr(A.C, "contexts_jsonl", lambda h="neutral": cp)
    out = tmp_path / "analysis.json"
    assert A.main(["--measures", str(mp), "--detector", str(dp), "--out", str(out),
                   "--n-boot", "200"]) == 0
    res = json.loads(out.read_text())
    assert set(res["blocks"]) == {M, "deepseek-v4-flash"}
    assert set(res["blocks"][M]) == {"neutral", "safety"}
    assert res["blocks"][M]["neutral"]["contrasts"]["minus_c - full: cheat"]["estimate"] == 1.0
