"""P02 analysis: shares and the paired bootstrap, window and coherence filtering, the null band,
every outcome branch, Gemini-absent cells, outputs, and the seeded samples page. Synthetic cells
only (`tests/p02/__init__.py`); no network."""

from __future__ import annotations

import json

import numpy as np
import pytest

from . import A, cell, frac, write_cell

NULLS = ("ctl_book", "ctl_cat", "gauss_00", "gauss_01")
NB = 400  # resamples in tests (the CLI uses A.BOOT_N)


def never(qi, k):
    return False


def always(qi, k):
    return True


def build(root, *, org_A=None, good_A=None, base_A=None, incoh_A=None, null_em=None, org_B=None,
          incoh_B=None, deploy=None, sign=None, gfl=True, nulls=NULLS):
    """A full synthetic run (4 samples per question, nulls 2): D = 0.5 (org noov 2/4, ov 0/4).
    Each *_A / *_B maps strength -> em(qi, k); incoh_* maps strength -> incoherent(qi, k)."""
    org_A, good_A, base_A = org_A or {}, good_A or {}, base_A or {}
    incoh_A, incoh_B, org_B = incoh_A or {}, incoh_B or {}, org_B or {}
    headline = ("luna", "gfl") if gfl else ("luna",)
    for m in A.MODELS:
        write_cell(root, cell(m), frac(0), judges=headline)
        write_cell(root, cell(m, fmt="deployline"), (deploy or {}).get(m, frac(0)),
                   judges=headline)
        for a in A.A_STRENGTHS:
            em = {"org": org_A, "good": good_A, "base": base_A}[m].get(a, frac(0))
            write_cell(root, cell(m, "A", a), em, incoherent=incoh_A.get(a, never),
                       judges=headline)
        for f in A.B_FRACTIONS:
            em = org_B.get(f, frac(0)) if m == "org" else frac(0)
            write_cell(root, cell(m, "B", f), em, incoherent=incoh_B.get(f, never))
    write_cell(root, cell("org", notice="noov"), frac(2), judges=headline)
    for a in A.SIGN_STRENGTHS:
        write_cell(root, cell("org", "A", a, notice="noov"), (sign or {}).get(a, frac(2)),
                   judges=headline)
    for s in nulls:
        for a in A.A_STRENGTHS:
            write_cell(root, cell("org", s, a, samples=2), (null_em or {}).get((s, a), frac(0)))
    return root


def run(root, n_boot=NB):
    scored, _ = A.load_all(root / "gen", root / "judged", log=lambda s: None)
    return A.analyze_frames(scored, n_boot=n_boot)


# --- bootstrap and shares -----------------------------------------------------------------------


def test_resample_weights_match_e13_picks():
    w = A.resample_weights(41, seed=0, n_boot=50)
    pick = np.random.default_rng(0).integers(0, 41, size=(50, 41))
    assert w.shape == (50, 41) and (w.sum(1) == 41).all()
    for b in range(50):
        assert (np.bincount(pick[b], minlength=41) == w[b]).all()


def test_shares_pairing_and_determinism(tmp_path):
    # heterogeneous per-question EM in the baseline; A at 0.3 = baseline + 1 EM sample on every
    # question (a constant paired shift: degenerate interval only if resamples are paired);
    # A at 0.6 identical to the baseline (S = 0, degenerate CI)
    def base_em(qi, k):
        return k < qi % 3

    def plus_one(qi, k):
        return k < qi % 3 + 1

    for m in A.MODELS:
        write_cell(tmp_path, cell(m), base_em if m == "org" else frac(0))
    write_cell(tmp_path, cell("org", notice="noov"), lambda qi, k: k < qi % 3 + 2)
    write_cell(tmp_path, cell("org", "A", 0.3), plus_one)
    write_cell(tmp_path, cell("org", "A", 0.6), base_em)
    write_cell(tmp_path, cell("org", "A", 1.2), lambda qi, k: k < (qi * 7) % 5)
    s1, s2 = run(tmp_path), run(tmp_path)
    b = s1["judges"]["luna"]
    assert b["D"]["point"] == pytest.approx(0.5) and b["D"]["ci"] == pytest.approx([0.5, 0.5])
    rows = {r["alpha"]: r for r in b["A"]["org"]}
    assert rows[0.3]["S"]["point"] == pytest.approx(0.5)
    assert rows[0.3]["S"]["ci"] == pytest.approx([0.5, 0.5])  # paired: constant shift
    assert rows[0.3]["em_diff"]["ci"] == pytest.approx([0.25, 0.25])
    assert rows[0.6]["S"]["point"] == 0.0 and rows[0.6]["S"]["ci"] == [0.0, 0.0]
    lo, hi = rows[1.2]["S"]["ci"]
    assert lo < rows[1.2]["S"]["point"] < hi and hi - lo > 0.05  # a real interval
    assert rows[1.2]["S"]["ci"] == s2["judges"]["luna"]["A"]["org"][2]["S"]["ci"]  # fixed seed
    assert s1["bootstrap"] == {"seed": 0, "resamples": NB, "n_clusters": 41,
                               "unit": "question (qid)", "paired": True}


def test_ratio_drops_nonpositive_denominators():
    den = np.array([0.5, -0.1, 0.0, 0.25])
    num = np.array([0.25, 0.1, 0.1, 0.25])
    r = A.ratio_stat({"point": 0.2}, num, {"point": 0.4}, den)
    assert r["point"] == pytest.approx(0.5) and r["nonpositive_fraction"] == 0.5
    assert r["ci"][0] == pytest.approx(0.5, abs=0.02) and r["ci"][1] == pytest.approx(1.0,
                                                                                         abs=0.02)


# --- window, nulls, outcomes end to end ---------------------------------------------------------


def test_clean_window_end_to_end_and_coherence_filter(tmp_path):
    # org A: S 0.5 at 0.3 and 1.0 at 0.6; at 2.4 coherence 0.75 (reported), at 4.8 0.25 (outside)
    build(tmp_path, org_A={0.3: frac(1), 0.6: frac(2), 1.2: frac(2), 2.4: frac(2),
                           4.8: frac(1)},
          incoh_A={2.4: lambda qi, k: k == 3, 4.8: always})
    s = run(tmp_path)
    b = s["judges"]["luna"]
    st = {r["alpha"]: r["coh_status"] for r in b["A"]["org"]}
    assert st == {0.3: "window", 0.6: "window", 1.2: "window", 2.4: "reported",
                  4.8: "outside headline"}
    assert b["outcome"]["id"] == 1 and b["outcome"]["window"] == [0.3, 0.6, 1.2]
    assert b["outcome"]["strengths"] == [0.3, 0.6, 1.2]
    assert s["outcome"] == b["outcome"]
    assert s["headline_strength"] == {"alpha": 0.6, "rule": "max S_org over A's window (luna)"}
    band = b["null_band"]["0.6"]
    assert band["all"] == {"p90": 0.0, "min": 0.0, "median": 0.0, "max": 0.0, "n": 4}
    assert band["ctl"]["n"] == 2 and band["gauss"]["n"] == 2
    # Gemini scored the headline cells only: no nulls of its own, luna's band used
    g = s["judges"]["gfl"]
    assert g["nulls"] == {} and g["null_band_source"] == "luna" and "B" in g and not g["B"]
    assert g["outcome"]["id"] == 1


def test_window_empty_is_nothing_moves(tmp_path):
    build(tmp_path, org_A={a: frac(2) for a in A.A_STRENGTHS},
          incoh_A={a: always for a in A.A_STRENGTHS}, nulls=NULLS[:1])
    o = run(tmp_path)["outcome"]
    assert o["id"] == 3 and o["window"] == [] and "window empty" in o["reason"]


def test_nothing_moves_end_to_end(tmp_path):
    build(tmp_path, nulls=NULLS[:1])
    s = run(tmp_path)
    assert s["outcome"]["id"] == 3 and s["outcome"]["window"] == list(A.A_STRENGTHS)
    assert s["headline_strength"]["alpha"] == 0.3  # all S = 0: the first window strength


def test_reveals_but_invents_via_nulls_and_via_good(tmp_path):
    # nulls rise as much as A: S_org <= null P90
    build(tmp_path / "n", org_A={0.6: frac(1)},
          null_em={(s_, 0.6): frac(1) for s_ in NULLS})
    o = run(tmp_path / "n")["outcome"]
    assert o["id"] == 2 and o["strengths"] == [0.6] and "null P90" in o["reason"]
    # good-only rises: induced share >= S_org / 2 (and induced EM >= 5 points blocks outcome 1)
    build(tmp_path / "g", org_A={0.6: frac(2)}, good_A={0.6: frac(1)})
    o = run(tmp_path / "g")["outcome"]
    assert o["id"] == 2 and "induced share" in o["reason"]


def test_null_band_percentile_and_families():
    def r(a, s):
        return {"alpha": a, "S": {"point": s}}

    nulls = {"ctl_book": [r(0.6, 0.1)], "ctl_cat": [r(0.6, 0.3)], "gauss_00": [r(0.6, -0.2)],
             "gauss_01": [r(0.6, 0.0), r(1.2, 0.5)]}
    band = A.null_band(nulls)
    v = band[0.6]["all"]
    assert v["p90"] == pytest.approx(np.percentile([0.1, 0.3, -0.2, 0.0], 90))
    assert (v["min"], v["max"], v["n"]) == (-0.2, 0.3, 4)
    assert v["median"] == pytest.approx(0.05)
    assert band[0.6]["ctl"]["max"] == 0.3 and band[0.6]["gauss"]["min"] == -0.2
    assert band[1.2]["all"]["n"] == 1 and band[1.2]["ctl"] is None and band[0.3]["all"] is None


# --- outcome classification (pure) --------------------------------------------------------------


def row(alpha, S, lo, hi, p90=0.1, ig=0.0, ib=0.0, sg=0.0, sb=0.0, status="window"):
    return {"alpha": alpha, "coh_status": status, "coherence": 0.9, "S": S, "S_lo": lo,
            "S_hi": hi, "null_p90": p90, "induced_good": ig, "induced_base": ib, "S_good": sg,
            "S_base": sb}


def test_classify_branches():
    c = A.classify_outcome
    assert c([row(0.6, 0.4, 0.1, 0.7)])["id"] == 1
    # 1 fails on the induced rise (>= 5 points), induced share >= S/2 -> 2
    assert c([row(0.6, 0.4, 0.1, 0.7, ig=0.06, sg=0.25)])["id"] == 2
    # 1 fails on the CI (includes 0) -> neither 1 nor 2 -> 4
    o = c([row(0.6, 0.4, -0.1, 0.9)])
    assert o["id"] == 4 and "no strength meets" in o["reason"]
    # S above 0.15 but under 0.3, CI above 0, below the null band -> 2
    assert c([row(0.6, 0.2, 0.05, 0.4, p90=0.25)])["id"] == 2
    # everything small -> 3
    assert c([row(0.3, 0.05, -0.1, 0.2), row(0.6, 0.1, -0.05, 0.3)])["id"] == 3
    # a sign flip -> 4, not 3
    o = c([row(0.3, -0.4, -0.7, -0.1), row(0.6, 0.0, -0.2, 0.2)])
    assert o["id"] == 4 and o["strengths"] == [0.3] and "sign flip" in o["reason"]
    # strengths outside the window are ignored; with none left -> 3 (window empty)
    o = c([row(0.6, 0.9, 0.5, 1.2, status="reported")])
    assert o["id"] == 3 and o["window"] == []
    # no null band at that strength: outcome 1 cannot be called
    assert c([row(0.6, 0.4, 0.1, 0.7, p90=None)])["id"] == 4


def test_side_predictions_and_sign_check(tmp_path):
    build(tmp_path, org_A={0.6: frac(2)}, org_B={0.0125: frac(1), 0.1: frac(2)},
          incoh_B={0.1: always}, deploy={"org": frac(2)},
          sign={-0.6: frac(1), -1.2: frac(0), -2.4: frac(2)}, nulls=NULLS[:1])
    b = run(tmp_path)["judges"]["luna"]
    side = b["side"]
    assert side["prompt_baseline"]["S_org"] == pytest.approx(1.0)
    assert side["prompt_baseline"]["met"] is True
    ba = side["B_vs_A"]
    assert ba["max_S_A_window"] == pytest.approx(1.0) and ba["at_A"] == 0.6
    assert ba["max_S_B_window"] == pytest.approx(0.5) and ba["at_B"] == 0.0125
    assert ba["met"] is True and ba["B_coherence"][0.1] == pytest.approx(0.5)
    sc = {r["alpha"]: r["S"]["point"] for r in b["sign_check"]}
    assert sc == {-0.6: pytest.approx(-0.5), -1.2: pytest.approx(-1.0), -2.4: 0.0}


# --- outputs ------------------------------------------------------------------------------------


def test_analyze_writes_outputs_and_samples(tmp_path):
    root = build(tmp_path / "run", org_A={0.6: frac(2), 1.2: frac(1)},
                 org_B={0.025: frac(1)}, nulls=NULLS[:2])
    latent = tmp_path / "latent.json"
    latent.write_text(json.dumps({"org": {"model": "org", "auroc_last": {"4": 0.71, "11": 0.83},
                                          "cos_diff_e13_output": 0.12}}))
    out = tmp_path / "analysis"
    s = A.analyze(root / "gen", root / "judged", out, latent, n_boot=NB)
    for f in ("cells.csv", "summary.json", "summary.md", "fig_S_A.png", "fig_S_B.png",
              "fig_nulls.png"):
        assert (out / f).exists(), f
    import pandas as pd

    cells = pd.read_csv(out / "cells.csv")
    luna, gfl = cells[cells["judge"] == "luna"], cells[cells["judge"] == "gfl"]
    assert len(luna) == s["n_cells"] and 0 < len(gfl) < len(luna)  # Gemini on headline only
    assert set(gfl["site"]) == {"none", "A"}
    r = cells[(cells["cell_id"] == "org__none__0__noov__plain") & (cells["judge"] == "luna")]
    assert r["em"].item() == 0.5 and r["n"].item() == 164 and r["unscored_share"].item() == 0
    md = (out / "summary.md").read_text()
    assert "Outcome 1" in md and "auroc_last" in md and "0.830" in md
    js = json.loads((out / "summary.json").read_text())
    assert js["outcome"]["id"] == 1 and len(js["inputs"]) == 2 * s["n_cells"] + 1
    assert js["b_max_fraction"]["alpha"] == 0.025
    # samples: deterministic, 3 rows per present cell, headline strength 0.6
    import p02_samples as SP

    scored, _ = A.load_all(root / "gen", root / "judged", text=True, judges=("luna",),
                           log=lambda s: None)
    d1, d2 = SP.draw(scored, js), SP.draw(scored, js)
    assert d1 == d2
    assert [d["cell_id"] for d in d1][:5] == [
        "org__none__0__ov__plain", "org__none__0__noov__plain", "org__A__+0.6__ov__plain",
        "good__A__+0.6__ov__plain", "base__A__+0.6__ov__plain"]
    assert d1[5]["cell_id"] in ("org__ctl_book__+0.6__ov__plain", "org__ctl_cat__+0.6__ov__plain")
    assert d1[-1]["cell_id"] == "org__B__+0.025__ov__plain"
    assert all(len(d["rows"]) == 3 for d in d1)
    text = SP.to_markdown(d1)
    assert "random.Random(20261011)" in text and text.count("```text") == 3 * len(d1)


def test_refusal_and_unscored_rows(tmp_path):
    for m in A.MODELS:
        write_cell(tmp_path, cell(m), frac(0))
    write_cell(tmp_path, cell("org", notice="noov"), frac(2),
               refusal=lambda qi, k: k == 3 and qi < 10, failed=lambda qi, k: k == 3 and qi >= 39)
    s, _ = A.load_all(tmp_path / "gen", tmp_path / "judged", log=lambda s: None)
    t = A.cell_table(s).set_index("cell_id")
    r = t.loc["org__none__0__noov__plain"]
    assert r["n"] == 164 and r["n_scored"] == 162  # refusals scored, failed rows not
    assert r["em"] == pytest.approx(82 / 162) and r["refusal_rate"] == pytest.approx(10 / 164)


def test_cell_keys_come_from_columns(tmp_path):
    g, _ = write_cell(tmp_path, cell("org", "A", 0.6), frac(0))
    k = A.cell_key_of(__import__("pandas").read_csv(g, keep_default_na=False))
    assert k == A.key("org", "A", 0.6) and k.id == "org__A__+0.6__ov__plain" == g.stem
    assert A.key("org").id == "org__none__0__ov__plain"
