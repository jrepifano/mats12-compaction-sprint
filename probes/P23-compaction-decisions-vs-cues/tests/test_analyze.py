"""Tests for p23.analyze on small synthetic CSVs whose answers are known by hand (ported from
probes/P08-dropped-fact-behaviour/tests/test_analyze.py, with P23's cells, context sets,
interaction, predictions and outcome category)."""
from __future__ import annotations

import csv
import json

import numpy as np
import pytest
from p23 import analyze as A

MEASURE_COLS = ("episode_id", "model_slug", "header", "cell", "drops", "ctx_id", "task_id",
                "repeat", "end_reason", "n_turns", "tested", "first_test_turn",
                "first_action_test", "done_untested", "disclosed", "tokens_in", "tokens_out",
                "billed_usd", "billed_known", "fact_ok", "replay_match", "path")
DET_COLS = ("episode_id", "tamper_any", "passed", "cheat", "error")
M, DS, H = "glm-5.3-flash", "deepseek-v4-flash", "safety"
CELLS = A.CELL_ORDER


def ep(cell, ctx, task, rep=0, *, cheat=False, tamper=None, passed=None, fat=False,
       done_untested=False, tested=True, disclosed=False, fact_ok=True, replay=True, model=M,
       header=H, n_turns=5):
    eid = f"P23|{model}|{header}|{cell}|{ctx}#r{rep}"
    m = {"episode_id": eid, "model_slug": model, "header": header, "cell": cell, "drops": "",
         "ctx_id": ctx, "task_id": task, "repeat": rep, "end_reason": "done",
         "n_turns": n_turns, "tested": tested, "first_test_turn": 2 if tested else "",
         "first_action_test": fat, "done_untested": done_untested, "disclosed": disclosed,
         "tokens_in": 1000, "tokens_out": 100, "billed_usd": 0.01, "billed_known": True,
         "fact_ok": fact_ok, "replay_match": replay, "path": ""}
    d = {"episode_id": eid, "cheat": cheat, "tamper_any": cheat if tamper is None else tamper,
         "passed": cheat if passed is None else passed, "error": ""}
    return m, d


def twin(ctx, task, status="ok", inserted=()):
    return {"ctx_id": ctx, "task_id": task, "status": status, "inserted": list(inserted),
            "spans": ["x"] if status != "no_decision" else []}


def write(tmp_path, eps, twins=None, audit=None, drop_det=()):
    mp, dp = tmp_path / "measures.csv", tmp_path / "detector.csv"
    tp, ap = tmp_path / "twins5.jsonl", tmp_path / "audit.json"
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
    if twins is None:          # every context in the episodes is a factorial "ok" context
        twins = [twin(c, t) for c, t in sorted({(m["ctx_id"], m["task_id"]) for m, _ in eps})]
    tp.write_text("".join(json.dumps(t) + "\n" for t in twins))
    if audit is not None:
        ap.write_text(json.dumps(audit))
    return mp, dp, tp, ap


def run(paths, model=M, **kw):
    kw.setdefault("n_boot", 2000)
    return A.analyze(*paths, model_slug=model, **kw)


def grid(n_ctx, n_task, reps, cheat_by_cell, **kw):
    """Every cell for each context; cheat_by_cell: cell -> f(ctx index, repeat) -> bool."""
    eps = []
    for i in range(n_ctx):
        for r in range(reps):
            for c in CELLS:
                f = cheat_by_cell.get(c)
                eps.append(ep(c, f"c{i}", f"t{i % n_task}", r,
                              cheat=bool(f(i, r)) if f else False, **kw))
    return eps


def test_primary_positive_and_cell_rates(tmp_path):
    eps = grid(6, 3, 3, {"minus_hd": lambda i, r: True})
    res = run(write(tmp_path, eps))
    c = res["factorial"]["contrasts"]["minus_hd - minus_hl: cheat"]
    assert c["estimate"] == 1.0 and c["lo"] == 1.0 and c["hi"] == 1.0
    assert (c["n_contexts"], c["n_tasks"], c["n_pos"], c["n_neg"], c["n_zero"]) == (6, 3, 6, 0, 0)
    assert res["primary"] == c and res["outcome_category"] == "positive"
    assert res["factorial"]["cells"]["minus_hd"]["cheat"]["k"] == 18
    assert res["factorial"]["cells"]["full"]["cheat"]["p"] == 0.0
    assert res["factorial"]["contrasts"]["minus_hd - minus_h: cheat"]["estimate"] == 1.0
    assert res["factorial"]["contrasts"]["minus_d - full: cheat"]["estimate"] == 0.0
    assert res["below_min_passing_contexts"] is True
    expected = {A.contrast_key(a, b, o) for a, b in A.FACTORIAL_CONTRASTS
                for o in A.ALL_OUTCOMES}
    assert set(res["factorial"]["contrasts"]) == expected
    json.dumps(A.clean(res), allow_nan=False)


def test_interaction(tmp_path):
    # minus_hd cheats in every repeat of c0..c3; minus_d in r0 of c0, c1 only. Per context:
    # (hd - h) - (d - full) = 1 - 1/3 for c0, c1 and 1 for c2, c3 -> mean 5/6.
    eps = grid(4, 2, 3, {"minus_hd": lambda i, r: True,
                         "minus_d": lambda i, r: i < 2 and r == 0})
    res = run(write(tmp_path, eps))
    it = res["factorial"]["interaction"]["cheat"]
    assert it["estimate"] == pytest.approx(5 / 6)
    assert (it["n_contexts"], it["n_pos"], it["n_neg"], it["n_zero"]) == (4, 4, 0, 0)
    assert it["cell_means"]["minus_hd"] == 1.0
    assert it["cell_means"]["minus_d"] == pytest.approx(1 / 6)
    assert it["lo"] <= it["estimate"] <= it["hi"]


def test_interaction_needs_all_four_cells(tmp_path):
    eps = grid(3, 3, 1, {"minus_hd": lambda i, r: True})
    eps = [e for e in eps if not (e[0]["ctx_id"] == "c2" and e[0]["cell"] == "minus_d")]
    res = run(write(tmp_path, eps))
    assert res["factorial"]["interaction"]["cheat"]["n_contexts"] == 2
    assert res["factorial"]["contrasts"]["minus_hd - minus_hl: cheat"]["n_contexts"] == 3


def test_outcome_category_rules():
    oc = A.outcome_category
    assert oc({"estimate": 0.06, "lo": 0.01, "hi": 0.2}) == "positive"
    assert oc({"estimate": 0.10, "lo": 0.0, "hi": 0.2}) == "inconclusive"   # lo not > 0
    assert oc({"estimate": 0.0, "lo": -0.02, "hi": 0.05}) == "null"
    assert oc({"estimate": 0.03, "lo": 0.01, "hi": 0.08}) == "inconclusive"
    assert oc({"estimate": float("nan"), "lo": None, "hi": None}) is None
    assert oc(None) is None


def test_null_category_from_data(tmp_path):
    eps = grid(8, 4, 3, {})
    assert run(write(tmp_path, eps))["outcome_category"] == "null"


def test_repeats_averaged_within_context(tmp_path):
    # c0: minus_hd cheats 2/3, minus_hl 0/3 -> d = 2/3; c1: both 1/3 -> d = 0. Mean 1/3.
    eps = [ep("minus_hd", "c0", "t0", r, cheat=r < 2) for r in range(3)]
    eps += [ep("minus_hl", "c0", "t0", r) for r in range(3)]
    eps += [ep("minus_hd", "c1", "t1", r, cheat=r == 0) for r in range(3)]
    eps += [ep("minus_hl", "c1", "t1", r, cheat=r == 0) for r in range(3)]
    c = run(write(tmp_path, eps))["primary"]
    assert c["estimate"] == pytest.approx(1 / 3)
    assert (c["n_pos"], c["n_zero"]) == (1, 1)


def test_pairing_drops_context_missing_in_one_cell(tmp_path):
    eps = [ep("minus_hd", "c0", "t0", cheat=True), ep("minus_hl", "c0", "t0"),
           ep("minus_hd", "c1", "t1", cheat=True),               # c1 has no minus_hl episode
           ep("minus_hd", "c2", "t2", cheat=True), ep("minus_hl", "c2", "t2", fact_ok=False)]
    res = run(write(tmp_path, eps))
    assert res["primary"]["n_contexts"] == 1 and res["primary"]["estimate"] == 1.0
    assert res["factorial"]["cells"]["minus_hd"]["n_contexts"] == 3


def test_exclusions_counted(tmp_path):
    eps = [ep("full", "c0", "t0", 0), ep("full", "c0", "t0", 1, fact_ok=False),
           ep("full", "c0", "t0", 2, replay=False), ep("full", "c1", "t1", 0),
           ep("full", "c1", "t1", 1, cheat=None, tamper=False, passed=False),
           ep("full", "c2", "t2", 0, fact_ok=""), ep("full", "c2", "t2", 1, disclosed=None)]
    no_det = ep("full", "c1", "t1", 0)[0]["episode_id"]
    full = run(write(tmp_path, eps, drop_det={no_det}))["factorial"]["cells"]["full"]
    assert full["excluded"]["fact_check_failed"] == 2
    assert full["excluded"]["replay_mismatch"] == 1
    assert full["excluded"]["no_detector_row"] == 3     # once per detector outcome
    assert full["excluded"]["cheat_unresolved"] == 1
    assert full["excluded"]["disclosed_missing"] == 1
    assert full["n"] == 4
    assert full["cheat"]["n"] == 2                       # c0 r0, c2 r1
    assert full["tamper_any"]["n"] == 3                  # unresolved cheat only hits cheat
    assert full["first_action_test"]["n"] == 4           # non-detector outcomes keep them all
    assert full["disclosed"]["n"] == 3


def test_disclosed_and_numeric_contrasts(tmp_path):
    eps = []
    for i in range(4):
        eps.append(ep("full", f"c{i}", f"t{i}", disclosed=True, n_turns=4))
        eps.append(ep("minus_h", f"c{i}", f"t{i}", disclosed=i > 0, n_turns=10, tested=i > 0))
    con = run(write(tmp_path, eps))["factorial"]["contrasts"]
    assert con["minus_h - full: disclosed"]["estimate"] == -0.25
    assert con["minus_h - full: n_turns"]["estimate"] == 6.0
    assert con["minus_h - full: tested"]["estimate"] == -0.25
    assert con["minus_h - full: first_test_turn"]["n_contexts"] == 3   # untested has none
    assert con["minus_hd - full: cheat"] is None                       # no minus_hd cell


def test_context_sets_and_audit(tmp_path):
    eps = []
    for c, t in (("a", "t0"), ("b", "t0"), ("h", "t1"), ("x", "t1"), ("p", "t2"), ("q", "t2")):
        for cell in CELLS:
            eps.append(ep(cell, c, t, cheat=cell == "minus_h"))
    eps += [ep("full", "n", "t3"), ep("minus_h", "n", "t3", cheat=True),
            ep("minus_d", "n", "t3"), ep("full", "z", "t3"), ep("minus_h", "z", "t3"),
            ep("other", "a", "t0")]
    twins = [twin("a", "t0"), twin("b", "t0"), twin("h", "t1", inserted=["header"]),
             twin("x", "t1", status="dropped"), twin("p", "t2", status="audit"),
             twin("q", "t2", status="audit"), twin("n", "t3", status="no_decision")]
    audit = {"b": {"pass": False}, "p": {"pass": True}}
    res = run(write(tmp_path, eps, twins, audit))
    assert res["audit_used"] is True
    fac, nod = res["factorial"], res["no_decision"]
    sens = res["sensitivity_no_inserted_header"]
    assert fac["ctx_ids"] == ["a", "h", "p"]
    assert sens["ctx_ids"] == ["a", "p"]
    assert nod["ctx_ids"] == ["n"]
    assert res["context_exclusions"] == {"audit_failed_ok": 1, "status_dropped": 1,
                                         "audit_pending": 1}
    assert res["rows_outside_sets"] == {"ctx_not_in_any_set": 3 * 5 + 2,   # b, x, q; z
                                        "cell_not_in_no_decision_arm": 1, "cell_not_in_p23": 1}
    assert fac["cells"]["minus_h"]["cheat"]["k"] == 3
    assert fac["contrasts"]["minus_h - full: cheat"]["n_contexts"] == 3
    assert set(nod["cells"]) == {"full", "minus_h"}
    assert set(nod["contrasts"]) == {A.contrast_key("minus_h", "full", o)
                                     for o in A.ALL_OUTCOMES}
    assert nod["contrasts"]["minus_h - full: cheat"]["estimate"] == 1.0
    assert set(sens["contrasts"]) == {A.contrast_key(a, b, o) for a, b in A.CUE_CONTRASTS
                                      for o in A.ALL_OUTCOMES}
    assert set(sens["interaction"]) == set(A.ALL_OUTCOMES)
    # without audit.json the "audit" contexts are pending and "b" is back in
    (tmp_path / "audit.json").unlink()
    res2 = run(write(tmp_path, eps, twins))
    assert res2["audit_used"] is False
    assert res2["factorial"]["ctx_ids"] == ["a", "b", "h"]
    assert res2["context_exclusions"] == {"status_dropped": 1, "audit_pending": 2}


def test_missing_twins5_raises(tmp_path):
    mp, dp, tp, ap = write(tmp_path, [ep("full", "c0", "t0")])
    tp.unlink()
    with pytest.raises(FileNotFoundError):
        A.analyze(mp, dp, tp, ap, model_slug=M, n_boot=100)


def test_bootstrap_deterministic_for_seed(tmp_path):
    rng = np.random.default_rng(0)
    eps = []
    for i in range(10):
        for r in range(2):
            eps.append(ep("minus_hd", f"c{i}", f"t{i % 4}", r, cheat=bool(rng.random() < .5)))
            eps.append(ep("minus_hl", f"c{i}", f"t{i % 4}", r, cheat=bool(rng.random() < .3)))
    paths = write(tmp_path, eps)
    a, b = run(paths, seed=7), run(paths, seed=7)
    assert a == b
    c = run(paths, seed=8)["primary"]
    assert c["estimate"] == a["primary"]["estimate"]
    assert run(paths)["seed"] == A.C.P23_SEED
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
    assert "_ctxs" not in res and res["mean_a"] == 0.75 and res["mean_b"] == 0.0
    draws = A.boot_draws([3.0, 0.0], [3.0, 1.0], n_boot=4000, seed=1)
    assert set(np.round(draws, 9)) == {0.0, 0.75, 1.0}
    assert (res["lo"], res["hi"]) == (0.0, 1.0)


def test_predictions(tmp_path):
    # GLM: minus_hd cheats everywhere (primary +1, CI > 0); minus_h = full = minus_d = 0.
    # DeepSeek: minus_h cheats everywhere, nothing else.
    eps = grid(6, 3, 2, {"minus_hd": lambda i, r: True})
    eps += grid(6, 3, 2, {"minus_h": lambda i, r: True}, model=DS)
    paths = write(tmp_path, eps)
    blocks = {M: run(paths), DS: run(paths, model=DS)}
    preds = {p["id"]: p for p in A.predictions(blocks)}
    assert preds["1"]["met"] is True and preds["1"]["p"] == 0.30
    assert preds["2"]["met"] is False                      # minus_hd cheats 100%
    assert preds["2"]["observed"]["minus_hd"] == 1.0
    assert preds["3"]["met"] is True and preds["4"]["met"] is True
    assert preds["5a"]["met"] is False                     # DS: hd - hl = 0
    assert preds["5b"]["met"] is True
    assert preds["5b"]["observed"]["estimate"] == 1.0
    assert preds["6"]["met"] is True
    # no DeepSeek block -> its predictions are unknown
    preds = {p["id"]: p for p in A.predictions({M: blocks[M]})}
    assert preds["5a"]["met"] is None and preds["5b"]["met"] is None
    assert {p["id"]: p for p in A.predictions({})}["2"]["met"] is None


def test_main_writes_blocks(tmp_path):
    eps = grid(3, 3, 1, {"minus_hd": lambda i, r: True})
    eps += grid(3, 3, 1, {}, model=DS)
    eps.append(ep("full", "c0", "t0", header="neutral"))     # not a P23 header: ignored
    mp, dp, tp, ap = write(tmp_path, eps)
    out = tmp_path / "analysis.json"
    assert A.main(["--measures", str(mp), "--detector", str(dp), "--twins5", str(tp),
                   "--audit", str(ap), "--out", str(out), "--n-boot", "200"]) == 0
    res = json.loads(out.read_text())
    assert set(res) == {"exp", "seed", "n_boot", "header", "blocks", "predictions"}
    assert set(res["blocks"]) == {M, DS}
    b = res["blocks"][M]
    assert b["factorial"]["contrasts"]["minus_hd - minus_hl: cheat"]["estimate"] == 1.0
    assert b["n_rows"] == 15
    assert res["blocks"][DS]["outcome_category"] in ("null", "inconclusive")
    assert [p["id"] for p in res["predictions"]] == ["1", "2", "3", "4", "5a", "5b", "6"]
