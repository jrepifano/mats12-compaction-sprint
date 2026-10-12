"""Tests for p15.analysis and p15.samples on small fake results (tests/fake_results.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_results import make_fake_results
from p15 import analysis as A
from p15 import samples as S


def _ci(point, lo, hi):
    return {"point": point, "lo": lo, "hi": hi}


# --- bootstrap -------------------------------------------------------------------------------------


def test_fast_cluster_bootstrap_agrees_with_emx():
    from emx.stats import cluster_bootstrap_ci

    rng = np.random.default_rng(1)
    n_cl = 40
    cl = rng.integers(0, n_cl, 400)
    p_cl = rng.uniform(0.1, 0.9, n_cl)
    hit = (rng.random(400) < p_cl[cl]).astype(float)
    df = pd.DataFrame({"c": cl, "h": hit})
    ref = cluster_bootstrap_ci(df, ["c"], lambda d: d.h.mean(), n_boot=2000, seed=5)
    mine = A.cluster_ratio_ci(hit, np.ones(400), cl, n_boot=2000, seed=11)[0]
    assert mine["point"] == pytest.approx(ref["point"])
    width = ref["hi"] - ref["lo"]
    assert abs(mine["lo"] - ref["lo"]) < 0.1 * width
    assert abs(mine["hi"] - ref["hi"]) < 0.1 * width
    assert mine["n_clusters"] == ref["n_clusters"] == n_cl


def test_bootstrap_is_paired_and_handles_columns():
    rng = np.random.default_rng(2)
    cl = rng.integers(0, 30, 200)
    a, b = rng.random(200), rng.random(200)
    r = A.cluster_ratio_ci(np.column_stack([a, b, a - b]), np.ones(200), cl, n_boot=500)
    assert r[2]["point"] == pytest.approx(r[0]["point"] - r[1]["point"])
    same = A.cluster_ratio_ci(a, np.ones(200), cl, n_boot=500)[0]
    assert same == r[0]  # same seed and clusters -> identical resamples


def test_auroc_matches_sklearn_with_ties():
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(3)
    y = rng.integers(0, 2, 300)
    s = np.round(rng.random(300) + 0.3 * y, 1)  # many ties
    assert A.auroc(s, y) == pytest.approx(roc_auc_score(y, s))
    flag = (rng.random(300) < 0.3 + 0.4 * y).astype(float)
    assert A.auroc(flag, y) == pytest.approx(roc_auc_score(y, flag))
    out = A.cluster_auroc_ci({"s": s, "f": flag}, y, rng.integers(0, 50, 300), [("s", "f")],
                             n_boot=300)
    assert out["s"]["lo"] <= out["s"]["point"] <= out["s"]["hi"]
    assert out["s-f"]["point"] == pytest.approx(out["s"]["point"] - out["f"]["point"])


# --- outcome rule ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n_cl, best, d, q, label",
    [
        (39, 0.9, _ci(0.3, 0.2, 0.4), _ci(0.3, 0.2, 0.4), "O5"),  # too few clusters
        (100, 0.29, _ci(0.3, 0.2, 0.4), _ci(0.3, 0.2, 0.4), "O5"),  # unreadable
        (100, 0.5, _ci(0.10, 0.01, 0.2), _ci(0.10, 0.01, 0.2), "O1"),
        (100, 0.5, _ci(0.15, 0.05, 0.25), _ci(0.02, -0.03, 0.099), "O4"),
        (100, 0.5, _ci(0.02, -0.09, 0.09), _ci(0.2, 0.1, 0.3), "O2"),
        (100, 0.5, _ci(-0.15, -0.25, -0.01), _ci(0.0, -0.1, 0.1), "O3"),
        (100, 0.5, _ci(0.15, 0.05, 0.25), _ci(0.08, -0.01, 0.17), "O5"),  # O1/O4 both fail
        (100, 0.5, _ci(0.05, -0.12, 0.2), _ci(0.0, -0.1, 0.1), "O5"),  # wide CI
        (100, 0.5, _ci(-0.12, -0.3, 0.02), _ci(0.0, -0.1, 0.1), "O5"),  # O3 needs hi < 0
        (100, 0.5, _ci(-0.08, -0.15, -0.01), _ci(0.0, -0.1, 0.1), "O5"),  # O3 needs <= -10
    ],
)
def test_outcome_branches(n_cl, best, d, q, label):
    out = A.outcome(n_cl, best, d, q)
    assert out["label"] == label
    assert out["numbers"]["n_err_clusters"] == n_cl


def test_outcome_order_o1_before_o2():
    # a CI inside +-10 that also clears 10: impossible; but O1 with narrow CI is checked first
    out = A.outcome(40, 0.30, _ci(0.10, 0.05, 0.099), _ci(0.10, 0.05, 0.15))
    assert out["label"] == "O1"


# --- folds, readers --------------------------------------------------------------------------------


def test_outer_and_inner_folds_stage_a():
    t = np.repeat(np.arange(20), 3)
    df = pd.DataFrame({"template_id": t, "op": np.where(t < 10, "add", "sub")})
    outer = A.outer_folds(df, "A")
    assert set(t[outer == 0]) == {0, 1, 2, 3, 4, 10, 11, 12, 13, 14}
    tr = outer != 0
    inner = A.inner_split(t[tr], df.op.to_numpy()[tr])
    for i in (0, 1):
        tpl = set(t[tr][inner == i].tolist())
        assert len(tpl) == 5
        assert any(x < 10 for x in tpl) and any(x >= 10 for x in tpl)
        assert not tpl & set(t[tr][inner != i].tolist())


def test_lookup_reader_majority_and_fallback():
    keys = np.array(["a", "a", "a", "b", "a", "c"], dtype=object)
    y = np.array(["1", "1", "2", "5", "9", "7"], dtype=object)
    outer = np.array([0, 0, 0, 0, 1, 1])
    k, k0 = A.lookup_reader(keys, y, outer)
    assert k[4] == "1"  # majority of a in fold 0
    assert k[5] == k0[5] == "1"  # unseen key -> K0, fold-0 majority
    assert k[0] == "9"  # fold 1 trains fold 0: key a -> 9


def test_first_number_and_ll_reader():
    lut = A.number_lut({"10": 3, "11": 4, "12": 12}, 20)
    top = np.array([[0, 10, 11, 0, 0], [1, 2, 3, 4, 5], [12, 0, 0, 0, 0]])
    assert A.first_number(top, lut).tolist() == [3, -1, 12]
    ll = np.zeros((3, 17, 7, 5), dtype=int)
    ll[:, 16, 3] = top
    ll[:, 16, 5] = top[[2, 2, 2]]
    _, _, pred = A.ll_reader(ll, lut)
    assert pred == ["15", None, "24"]


def test_labels():
    assert A.lab(12.0) == "12" and A.lab(None) is None and A.lab(2.5) == "2.5"
    assert A.lab_aL(None) == "none"


def test_patching_p5():
    rows = []
    for i in range(20):
        for cond, ans in (("diff", 9 if i < 8 else 1), ("random", 9 if i < 1 else 1),
                          ("same", 1), ("none", 1)):
            rows.append({"item_id": f"T-{i}", "lat": 5, "cond": cond, "target": 9, "gold": 1,
                         "ans": ans})
    out = A.patching(pd.DataFrame(rows), {}, n_boot=200)
    assert out["5"]["diff"]["ans_eq_target"]["point"] == pytest.approx(0.4)
    assert out["5"]["same"]["ans_eq_gold"]["point"] == 1.0
    assert out["P5"]["pass"]  # 0.40 >= 0.15 and >= 3 * 0.05


# --- end to end on fakes ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fake_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("p15fake")
    make_fake_results(root, "A")
    make_fake_results(root, "B")
    outs = {}
    for st in ("A", "B"):
        out = root / f"analysis_{st}.json"
        assert A.main(["--results", str(root), "--stage", st, "--out", str(out),
                       "--md", str(root / f"analysis_{st}.md"), "--layers", "8,16",
                       "--fast"]) == 0
        outs[st] = json.loads(out.read_text())
    return root, outs


def test_stage_a_end_to_end(fake_run):
    root, outs = fake_run
    o = outs["A"]
    r = o["readers"]
    assert r["LP"]["E_share"]["point"] > r["LPq"]["E_share"]["point"] + 0.3
    assert r["LP"]["E_share"]["point"] > r["V"]["E_share"]["point"] + 0.3
    assert o["counts"]["n_err_clusters"] >= 40
    assert o["settings"]["cluster_ids"].startswith("T (X,Y,Z)")
    assert o["outcome"]["label"] == "O1"
    assert o["headline"]["B"] in ("V", "VT", "K")
    for key in ("error_detection", "uplift", "readability", "sensitivity_2x2", "patching",
                "g_secondary", "predictions", "sweep_projection", "timings_s"):
        assert key in o
    assert set(o["predictions"]) >= {"P1", "P2", "P3", "P4", "P5", "P6"}
    assert o["readability"]["s1_col3_T"]["point"] > 0.6
    assert o["sensitivity_2x2"]["LP"]["E_share"]["point"] > 0.5
    items = pd.read_csv(root / o["items_file"])
    assert {"pred_LP", "pred_VT", "pred_K", "pred_LP_2x2", "in_E"} <= set(items.columns)
    assert "Outcome: O1" in (root / "analysis_A.md").read_text()


def test_stage_b_end_to_end(fake_run):
    _, outs = fake_run
    o = outs["B"]
    r = o["readers"]
    assert "K" not in r and "K0" in r
    assert r["LP"]["E_share"]["point"] > r["LPq"]["E_share"]["point"] + 0.3
    assert o["headline"]["B"] in ("V", "VT")
    assert set(o["predictions"]) >= {"P3", "P7", "P8", "P9", "P10"}
    assert "drift_G" in o and "stage_b_error_check" in o
    assert set(o["uplift"]["C_by_k"]) == {"3", "4", "5"}
    assert "s3" in o["readability"]


def test_no_latent_signal_is_not_o1(tmp_path):
    make_fake_results(tmp_path, "A", signal={"latent": 0.0}, seed=1)
    out, _ = A.run(tmp_path, "A", "16", fast=True, n_boot=300)
    assert out["readers"]["LP"]["E_share"]["point"] < 0.3
    assert out["outcome"]["label"] != "O1"


def test_samples_prints_both_groups(fake_run, capsys):
    root, _ = fake_run
    assert S.main(["--results", str(root), "--stage", "A", "--n", "3"]) == 0
    text = capsys.readouterr().out
    assert "===== E: 3" in text and "===== correct: 3" in text
    assert text.count("out_verbal:") == 6 and "LP=" in text
    first = text
    S.main(["--results", str(root), "--stage", "A", "--n", "3"])
    assert capsys.readouterr().out == first  # seeded
