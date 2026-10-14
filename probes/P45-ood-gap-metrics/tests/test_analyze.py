"""p45.analyze on synthetic data: the T2 decision tree (every branch), paired bootstrap,
the law2 pre-flight switch, and prep -> select -> evaluate -> report end to end."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from p42 import contracts as P42K
from p45 import analyze as AN
from p45 import contracts as K

CATS = K.PLANTED_EVAL


def _d(est, lo, hi):
    return {"est": est, "ci95": [lo, hi]}


def _prec(best, s3, lo):
    return {"best": best, "s3": s3, "delta": _d(best - s3, lo, lo + 0.1)}


EQ = {c: _d(0.0, -0.03, 0.03) for c in CATS}
NOPREC = {c: _prec(0.05, 0.05, -0.02) for c in CATS}


def test_auroc_outcome_boundaries():
    assert AN.auroc_outcome(0.05, 0.001, 0.1) == "better"
    assert AN.auroc_outcome(0.05, 0.0, 0.1) == "unresolved"  # lower bound must be > 0
    assert AN.auroc_outcome(-0.05, -0.1, -0.001) == "worse"
    assert AN.auroc_outcome(0.0, -0.05, 0.05) == "equivalent"
    assert AN.auroc_outcome(0.04, 0.01, 0.07) == "unresolved"


def test_decide_o1_via_precision_only():
    prec = {c: _prec(0.20, 0.05, 0.05) for c in CATS}
    d = AN.decide(CATS, EQ, prec, 0.97, "F_post_L24")
    assert d["precision_outcome"] == "better" and d["auroc_outcome"] == "equivalent"
    assert d["headline"] == "O1" and d["winner"] == "F_post_L24"


def test_decide_precision_needs_every_category_and_ratio():
    prec = {CATS[0]: _prec(0.20, 0.05, 0.05), CATS[1]: _prec(0.09, 0.05, 0.01)}  # ratio < 2
    assert AN.decide(CATS, EQ, prec, 0.97, "x")["precision_outcome"] == "not_better"
    prec = {c: _prec(0.20, 0.05, -0.01) for c in CATS}  # CI touches 0
    assert AN.decide(CATS, EQ, prec, 0.97, "x")["headline"] == "O2"


def test_decide_o1_via_auroc():
    ad = {c: _d(0.08, 0.02, 0.14) for c in CATS}
    assert AN.decide(CATS, ad, NOPREC, 0.5, "x")["headline"] == "O1"


def test_decide_o3():
    ad = {c: _d(-0.08, -0.14, -0.02) for c in CATS}
    d = AN.decide(CATS, ad, NOPREC, 0.99, "x")
    assert d["auroc_outcome"] == "worse" and d["headline"] == "O3"


def test_decide_precision_beats_worse_auroc():
    ad = {c: _d(-0.08, -0.14, -0.02) for c in CATS}
    prec = {c: _prec(0.20, 0.05, 0.05) for c in CATS}
    assert AN.decide(CATS, ad, prec, 0.99, "x")["headline"] == "O1"


def test_decide_o2_requires_clean_benchmark():
    assert AN.decide(CATS, EQ, NOPREC, 0.95, "x")["headline"] == "O2"
    d = AN.decide(CATS, EQ, NOPREC, 0.94, "x")
    assert d["headline"] == "O4" and not d["clean_benchmark_reproduced"]
    assert AN.decide(CATS, EQ, NOPREC, float("nan"), "x")["headline"] == "O4"


def test_decide_o4_mixed_and_unresolved():
    ad = {CATS[0]: _d(0.08, 0.02, 0.14), CATS[1]: _d(0.0, -0.03, 0.03)}
    d = AN.decide(CATS, ad, NOPREC, 0.99, "x")
    assert d["auroc_outcome"] == "mixed" and d["headline"] == "O4"
    ad = {c: _d(0.03, -0.02, 0.08) for c in CATS}
    d = AN.decide(CATS, ad, NOPREC, 0.99, "x")
    assert d["auroc_outcome"] == "unresolved" and d["headline"] == "O4"


def test_decide_medical_only():
    d = AN.decide((P42K.PLANTED_MED,), {P42K.PLANTED_MED: _d(0.08, 0.02, 0.1)},
                  {P42K.PLANTED_MED: _prec(0.05, 0.05, -0.01)}, 0.5, "x")
    assert d["headline"] == "O1" and list(d["auroc_outcome_per_category"]) == [P42K.PLANTED_MED]


def test_paired_bootstrap_identical_scorers_zero_width():
    rng = np.random.default_rng(0)
    pool = rng.normal(size=500)
    pl = rng.normal(size=60) + 1.0
    other = pool + rng.normal(size=500)
    dr = AN.boot_draws({"a": pool, "b": pool.copy(), "c": other},
                       {"a": pl, "b": pl.copy(), "c": pl + 0.5}, 50, np.random.default_rng(1))
    for m in AN.METRICS:
        d = dr["a"][m] - dr["b"][m]
        assert np.allclose(d[np.isfinite(d)], 0)
        assert np.isfinite(dr["a"][m]).all()
    assert not np.allclose(dr["a"]["auroc"], dr["c"]["auroc"])
    with pytest.raises(ValueError):
        AN.boot_draws({"a": pool, "b": pool[:10]}, {"a": pl, "b": pl}, 2,
                      np.random.default_rng(0))


def test_boot_auroc_pairs_identical():
    rng = np.random.default_rng(2)
    x, y = rng.normal(size=80) + 1, rng.normal(size=90)
    d = AN.boot_auroc_pairs({"a": x, "b": x.copy()}, {"a": y, "b": y.copy()}, 30,
                            np.random.default_rng(3))
    assert np.array_equal(d["a"], d["b"])


def test_ratio():
    r = AN._ratio(np.array([0.2, 0.1, 0.0]), np.array([0.1, 0.0, 0.0]))
    assert r[0] == pytest.approx(2) and np.isinf(r[1]) and np.isnan(r[2])


def test_law2_failed(tmp_path):
    p = tmp_path / "m.json"
    assert AN.law2_failed(p)[0]
    p.write_text(json.dumps({"law2": {"preflight": {"passes_automatic": True}}}))
    assert not AN.law2_failed(p)[0]
    p.write_text(json.dumps({"law2": {"preflight": {"passes_automatic": True,
                                                    "passed": False}}}))
    assert AN.law2_failed(p)[0]


# --- end to end -------------------------------------------------------------------------------------

N = {P42K.ID_HELDOUT: 200, P42K.POOL: 800, P42K.PLANTED_MED: 80, P42K.PLANTED_LAW: 80,
     K.PLANTED_LAW2: 80}


def make_fixture(root, law2_pass=True, law2_s1=False):
    rng = np.random.default_rng(0)
    rows, sc = [], []
    for s, n in N.items():
        for i in range(n):
            ntok = int(np.exp(rng.normal(4, 1))) + 1
            rows.append({"pid": f"{s}-{i:06d}", "set": s, "source": "x", "source_id": str(i),
                         "category": None, "text": "t", "n_tokens": ntok, "truncated": False,
                         "lang": "English", "english": True, "mod_flagged": None,
                         "mod_max": None, "mod_bin": None, "gen": True, "gen_rank": None,
                         "split": "dev" if i % 2 == 0 else "test"})
            planted = s in (P42K.PLANTED_MED, P42K.PLANTED_LAW, K.PLANTED_LAW2)
            r = {"pid": rows[-1]["pid"], "set": s, "split": rows[-1]["split"]}
            for c in K.SCORE_COLS:
                r[c] = rng.normal() + 0.01 * np.log(ntok)
            r["F_post_L24"] += 2.0 * planted  # the strong candidate
            r["maha_L24"] += 1.0 * planted
            r["len_tokens"] = ntok
            sc.append(r)
    sc[203]["F_post_L16"] = np.nan  # one invalid row: pool-000003 (test)
    root.mkdir(parents=True, exist_ok=True)
    p42 = [r for r in rows if r["set"] != K.PLANTED_LAW2]
    l2 = [r for r in rows if r["set"] == K.PLANTED_LAW2]
    (root / "prompts.jsonl").write_text("\n".join(json.dumps(r) for r in p42) + "\n")
    (root / "law2.jsonl").write_text("\n".join(json.dumps(r) for r in l2) + "\n")
    pd.DataFrame(sc).to_csv(root / "scores.csv", index=False)
    em = pd.DataFrame({"pid": [r["pid"] for r in p42], "emb_knn": rng.normal(size=len(p42))})
    em.to_csv(root / "emb.csv", index=False)
    if law2_s1:
        pd.DataFrame({"pid": [r["pid"] for r in l2], "emb_knn": rng.normal(size=len(l2))}).to_csv(
            root / "emb_law2.csv", index=False)
    (root / "meta.json").write_text(json.dumps({"law2": {"preflight": {
        "passes_automatic": law2_pass}}}))
    p42s = pd.DataFrame(sc)[["pid", "maha_L24"]].rename(columns={"maha_L24": "maha_dpo_p_L24"})
    p42s.to_csv(root / "p42_scores.csv", index=False)
    return AN.Paths(p42_prompts=root / "prompts.jsonl", law2=root / "law2.jsonl",
                    scores=root / "scores.csv", p42_emb=root / "emb.csv",
                    law2_emb=root / "emb_law2.csv", analysis_dir=root / "analysis",
                    data_meta=root / "meta.json", p42_scores=root / "p42_scores.csv")


def test_end_to_end(tmp_path):
    p = make_fixture(tmp_path)
    out = AN.prep(p)
    assert out["valid_mask"]["n_valid"] == sum(N.values()) - 1
    assert out["s1_law2"].startswith("ABSENT")
    df = AN.load_prep(p)
    assert df.loc[df["set"] == K.PLANTED_LAW2, "emb_knn"].isna().all()
    pool = df[df["set"] == P42K.POOL]
    for b in range(P42K.N_LEN_BINS):
        z = pool.loc[pool["len_bin"] == b, "maha_L24__lc"]
        assert abs(z.mean()) < 1e-9
    sel = AN.select(p)
    assert sel["best"] == "F_post_L24" and sel["categories"] == list(K.PLANTED_EVAL)
    with pytest.raises(FileExistsError):
        AN.select(p)
    t2 = AN.evaluate(p, n_boot=40, appx_subsets=20, head_subsets=20)
    assert t2["decision"]["winner"] == "F_post_L24"
    assert t2["s3_reproduction"]["passes"]
    for cat in (*K.PLANTED_EVAL, P42K.PLANTED_LAW):
        assert cat in t2["contrasts"]
        assert t2["contrasts"][cat]["d_auroc"]["est"] > 0
    assert "emb_knn" in t2["per_category"][K.PLANTED_LAW2]["skipped_columns"]
    assert t2["definitions"]["m"]["prec_1pct"] == round(0.01 * 399)  # one invalid row
    assert AN.report(p).is_file()
    assert (p.analysis / "figs" / "len_vs_score.png").is_file()


def test_select_medical_only_when_law2_failed(tmp_path):
    p = make_fixture(tmp_path, law2_pass=False, law2_s1=True)
    out = AN.prep(p)
    assert out["s1_law2"].startswith("joined")
    sel = AN.select(p)
    assert sel["categories"] == [P42K.PLANTED_MED] and sel["law2_preflight_failed"]
