"""U5b E1b power simulations: determinism, coarse monotonicity, the breadth rule's three
conditions, and the specificity simulation on a synthetic restore table."""

from __future__ import annotations

import importlib.util
import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from emx.e1b import part3 as P3

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("e1b_power_sim", ROOT / "scripts/e1b_power_sim.py")
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def _fake_breadth_source(tmp_path: Path) -> Path:
    """The breadth_analysis.json fields the script reads: 3 fine-tuned models plus base, on the
    48 Betley ids (7 medical) and one first-plot question, with uneven per-question rates."""
    ids = [f"{i}_other_{i}" for i in range(41)] + [f"{43 + i}_medical_advice_{i}" for i in range(7)]
    rng = np.random.default_rng(3)
    models = {}
    for m in ("arm1_r1_seed1", "arm2_r1_seed1", "arm3_r1_seed1", "base"):
        pq = {q: {"em_rate": float(rng.choice([0.0, 0.05, 0.2, 0.5]))} for q in ids}
        pq["gender_roles"] = {"em_rate": 0.9}
        models[m] = {"per_question": {"j1": pq}}
    p = tmp_path / "breadth_analysis.json"
    p.write_text(json.dumps({"models": models}))
    return p


def test_new41_shares_reads_41_non_medical_and_skips_base(tmp_path: Path):
    qids, models, shares = ps.new41_shares(_fake_breadth_source(tmp_path))
    assert len(qids) == 41 and not any("medical" in q for q in qids)
    assert "gender_roles" not in qids and models == [
        "arm1_r1_seed1",
        "arm2_r1_seed1",
        "arm3_r1_seed1",
    ]
    assert np.allclose(shares.sum(1), 1.0)


def test_primary_deterministic_and_monotone():
    kw = {"n_sims": 120, "bad_rates": (0.0, 0.04, 0.075, 0.15), "good_rates": (0.005,)}
    a, b = ps.run_primary(**kw), ps.run_primary(**kw)
    assert a["grid"] == b["grid"] and a["meta"]["seed"] == 0
    for prof in ("uniform", "middle", "dominant"):
        pts = [g for g in a["grid"] if g["profile"] == prof]
        for judges in ("one_judge", "two_judges"):
            clear = [g[judges]["p_clear"] for g in pts]
            assert clear[0] == 0.0 and clear[-1] >= 0.95
            assert all(y >= x - 0.1 for x, y in pairwise(clear))
            for g in pts:
                assert np.isclose(sum(g[judges][f"p_{k}"] for k in ("clear", "weak", "none")), 1)
        # a second judge can only remove clear labels (same draws)
        assert all(g["two_judges"]["p_clear"] <= g["one_judge"]["p_clear"] for g in pts)


def test_primary_is_one_test_not_holm():
    # the focal condition is outside E1's Holm family, so condition_label uses the raw p
    from emx.e1.rules import is_main

    assert not is_main(ps.PRIMARY_FOCAL)


def test_breadth_deterministic_and_monotone(tmp_path: Path):
    src = _fake_breadth_source(tmp_path)
    kw = {"n_sims": 150, "bad_rates": (0.0, 0.03, 0.10), "guide_draws": 50, "source": src}
    a, b = ps.run_breadth(**kw), ps.run_breadth(**kw)
    assert a["grid"] == b["grid"] and a["test_only_guide"] == b["test_only_guide"]
    for prof in ("resampled", "mean", "uniform"):
        broad = [g["p_broad"] for g in a["grid"] if g["profile"] == prof]
        assert broad[0] == 0.0 and broad[-1] >= 0.9
        assert all(y >= x - 0.1 for x, y in pairwise(broad))
    g = a["grid"][-1]
    assert g["p_broad_margin_both_judges"] <= g["p_broad"]
    assert all(0 <= v <= 1 for v in a["test_only_guide"].values())


def _n(q=41, n=18):
    return np.full(q, n)


def test_breadth_rule_all_three_hold():
    em = np.zeros(41, int)
    em[:10] = 3  # 30 / 738 = 4.1% EM on 10 questions
    r = ps.breadth_rule(em, _n(), np.zeros(41, int), _n())
    assert r["margin"] and r["test"] and r["spread"] and r["broad"]


def test_breadth_rule_margin_binds():
    # many answers make a 1.5-point gap significant, on every question: only the margin fails
    em, ctl, n = np.full(41, 60), np.full(41, 30), _n(n=2000)  # 3.0% vs 1.5%
    r = ps.breadth_rule(em, n, ctl, n)
    assert r["test"] and r["spread"] and not r["margin"] and not r["broad"]


def test_breadth_rule_test_binds():
    # 6 answers per question: 10/246 = 4.1% vs 4/246 = 1.6% clears the margin, 5 questions have
    # 2 EM, but the stratified test is not significant
    em, ctl, n = np.zeros(41, int), np.zeros(41, int), _n(n=6)
    em[:5] = 2
    ctl[:4] = 1
    r = ps.breadth_rule(em, n, ctl, n)
    assert r["spread"] and r["margin"] and not r["test"] and not r["broad"]


def test_breadth_rule_spread_binds():
    em = np.zeros(41, int)
    em[:4] = 10  # 40 EM on only 4 questions
    r = ps.breadth_rule(em, _n(), np.zeros(41, int), _n())
    assert r["margin"] and r["test"] and not r["spread"] and not r["broad"]


def _frames(em_rows: dict, n=50, nq=8, aligned_offset=False) -> dict:
    """Aligned Part 3 frames: every answer coherent; answer i of each question is EM in kind k iff
    i < em_rows[k] (nested, so the conditions are perfectly paired). With `aligned_offset`, the
    non-EM rows carry a row-unique aligned score (+ a per-condition offset) to trace rows."""
    out = {}
    for j, (k, m) in enumerate(em_rows.items()):
        i = np.tile(np.arange(n), nq)
        aligned = np.where(i < np.repeat(m, n) if np.ndim(m) else i < m, 10.0, 90.0)
        if aligned_offset:
            aligned = np.where(aligned > 50, 60.0 + np.arange(n * nq) / 1000 + j, aligned)
        out[k] = pd.DataFrame(
            {
                "qid": np.repeat([f"q{x}" for x in range(nq)], n),
                "sample_idx": i,
                "aligned__luna": aligned,
                "coherent__luna": np.full(n * nq, 90.0),
            }
        )
    return out


def test_specificity_uses_the_paired_bootstrap():
    # header removes 6 of 10 EM answers per question, the control 5 of the same 10 (nested). With
    # shared answer indices the drop difference's 95% interval is above zero and the label is
    # specific; drawn independently per condition (the old simulation), the interval spans zero.
    fr = _frames({"none": 10, "header": 4, "qrand5": 5})
    r = ps.specificity_outcome(fr, 2000, 0)
    assert r["label"] == ps.SPECIFIC and r["bootstrap"]["paired"]
    assert r["header"]["drop"] == pytest.approx(0.6)
    assert r["header_minus_qrand5"]["ci"][0] > 0
    flags = {k: P3.em_flags(f, "luna") for k, f in fr.items()}
    qid = fr["none"]["qid"].to_numpy()
    ind = {
        k: P3.paired_rate_boot(qid, {k: v}, 2000, i)[k] for i, (k, v) in enumerate(flags.items())
    }
    unpaired = P3._drop(ind["none"], ind["header"]) - P3._drop(ind["none"], ind["qrand5"])
    assert P3.interval(unpaired)[0] < 0
    # identical to the analysis's own call
    assert r == P3.analyze_restore(fr, fr, 2000, 0)


def test_specificity_outcomes_on_constructed_tables():
    nonspec = ps.specificity_outcome(_frames({"none": 10, "header": 1, "qrand5": 1}), 500, 0)
    assert nonspec["label"] == ps.NOT_SHOWN
    none = ps.specificity_outcome(_frames({"none": 10, "header": 10, "qrand5": 10}), 500, 0)
    assert none["label"] == ps.NO_EFFECT
    low = ps.specificity_outcome(_frames({"none": 1, "header": 0, "qrand5": 1}), 500, 0)
    assert low["label"] == P3.NOT_COMPUTABLE  # 2% unpatched EM < 5%


def test_resample_rows_keeps_answers_aligned_across_conditions():
    fr = _frames({"none": 3, "header": 1, "qrand5": 2}, aligned_offset=True)
    sim = ps.resample_rows(fr, np.random.default_rng(0))
    n, h, q = (sim[k] for k in ("none", "header", "qrand5"))
    assert (n["qid"] == h["qid"]).all() and (n["qid"] == q["qid"]).all()
    assert n.groupby("qid").size().eq(50).all()
    assert (n["sample_idx"] == np.tile(np.arange(50), 8)).all()
    # rows that are non-EM in every condition come from the same source row in each
    both = (n["aligned__luna"] > 50) & (h["aligned__luna"] > 50) & (q["aligned__luna"] > 50)
    assert both.sum() > 0
    assert np.allclose(h["aligned__luna"][both] - n["aligned__luna"][both], 1.0)
    assert np.allclose(q["aligned__luna"][both] - n["aligned__luna"][both], 2.0)
    # an answer EM after restoring is EM unpatched too (the nesting survives resampling)
    assert not ((h["aligned__luna"] < 50) & (n["aligned__luna"] > 50)).any()


def test_specificity_power_probabilities_and_determinism():
    fr = _frames(
        {
            "none": [8, 2, 6, 0, 9, 3, 5, 7],
            "header": [2, 0, 1, 0, 3, 1, 1, 2],
            "qrand5": [6, 2, 5, 0, 8, 2, 4, 6],
        }
    )
    a = ps.specificity_power(fr, np.random.default_rng(0), n_reps=40, n_boot=200)
    b = ps.specificity_power(fr, np.random.default_rng(0), n_reps=40, n_boot=200)
    assert a == b
    probs = [a[f"p_{o}"] for o in ps.SPEC_LABELS]
    assert all(0 <= p <= 1 for p in probs) and np.isclose(sum(probs), 1)
    assert 0 <= a["p_diff_ci_above_0"] <= 1 and 0 <= a["p_header_ci_above_0"] <= 1
    assert a[f"p_{ps.SPECIFIC}"] <= a["p_diff_ci_above_0"]


def test_main_writes_outputs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ps, "PRIMARY_RATES", (0.0, 0.05))
    monkeypatch.setattr(ps, "PRIMARY_GOOD", (0.005,))
    out = tmp_path / "power"
    argv = ["--only", "primary", "--n-primary", "20", "--out", str(out)]
    assert ps.main(argv) == 0
    assert (out / "primary.json").exists() and "P(clear EM)" in (out / "summary.md").read_text()
    manifest = (tmp_path / "MANIFEST.power.sha256").read_text().splitlines()
    assert [ln.split("  ")[1] for ln in manifest] == [
        str(out / "primary.json"),
        str(out / "summary.md"),
    ]
    assert manifest[0].split("  ")[0] == ps.sha256_file(out / "primary.json")
    # a later run of one section keeps the others in the summary and the manifest
    (out / "breadth.json").write_text(json.dumps({"meta": {}, "grid": []}))
    monkeypatch.setattr(ps, "summary_md", lambda p, b, s: f"{p is not None} {b is not None}")
    assert ps.main(argv) == 0
    assert (out / "summary.md").read_text().strip() == "True True"
    assert len((tmp_path / "MANIFEST.power.sha256").read_text().splitlines()) == 3
