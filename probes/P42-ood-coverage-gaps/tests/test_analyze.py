"""p42.analyze on synthetic data: length control, selection gate, metrics, M2 rules, M3 matching."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from p42 import analyze as A
from p42 import contracts as K
from sklearn.metrics import roc_auc_score

from emx.contracts import ALIGNED_EM_BELOW, COHERENT_ABOVE

N = {K.ID_REF: 300, K.ID_HELDOUT: 100, K.SFT_REF: 20, K.POOL: 2000, K.PLANTED_MED: 100,
     K.PLANTED_LAW: 100}
DIM = 16


def make_fixture(root, seed=0):
    """prompts.jsonl, scores.csv, emb_scores.csv, emb.f16.npy/pids.txt and responses.jsonl.
    lr_sum separates both planted sets; knn_dpo_p_L16 separates them more weakly; nll_dpo_mean
    only tracks length; emb_knn separates planted_med only."""
    rng = np.random.default_rng(seed)
    rows, emb = [], []
    centers = rng.normal(size=(6, DIM))
    for si, (s, n) in enumerate(N.items()):
        for i in range(n):
            ntok = int(np.exp(rng.normal(4, 1))) + 1
            split = None
            if s in K.SCORED_SETS:
                split = "dev" if i % 2 == 0 else "test"
            mb = int(rng.integers(0, 3)) if s == K.POOL else None
            rows.append({"pid": f"{s}-{i:06d}", "set": s, "source": "x", "source_id": str(i),
                         "category": None, "text": f"text {s} {i}", "n_tokens": ntok,
                         "truncated": False, "lang": "English", "english": bool(i % 7),
                         "mod_flagged": (mb > 0) if mb is not None else None,
                         "mod_max": None, "mod_bin": mb, "gen": True, "gen_rank": None,
                         "split": split})
            emb.append(centers[si] + 0.3 * rng.normal(size=DIM))
    pr = pd.DataFrame(rows).sort_values("pid").reset_index(drop=True)
    order = {r["pid"]: i for i, r in enumerate(rows)}
    emb = np.asarray(emb)[[order[q] for q in pr["pid"]]]
    (root / "data").mkdir(parents=True, exist_ok=True)
    with open(root / "data/prompts.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in pr.to_dict("records"))
    sc = pr[pr["set"].isin(K.SCORED_SETS)][["pid", "set", "split", "n_tokens"]].copy()
    n = len(sc)
    planted = sc["set"].isin(K.PLANTED).to_numpy()
    med = (sc["set"] == K.PLANTED_MED).to_numpy()
    ll = np.log(sc["n_tokens"].to_numpy())
    for c in K.score_columns():
        sc[c] = rng.normal(size=n)
    sc["len_tokens"] = sc["n_tokens"]
    sc["nll_dpo_mean"] = -2 * ll + 0.1 * rng.normal(size=n)
    sc["lr_sum"] = rng.normal(size=n) + 3.0 * planted
    sc["knn_dpo_p_L16"] = rng.normal(size=n) + 1.0 * planted
    sc["fvu_sae_L16"] = np.nan
    sc = sc.drop(columns=["n_tokens"])
    (root / "results/embed").mkdir(parents=True, exist_ok=True)
    sc.to_csv(root / "results/scores.csv", index=False)
    pd.DataFrame({"pid": sc["pid"], "emb_knn": rng.normal(size=n) + 2.5 * med}).to_csv(
        root / "results/embed/emb_scores.csv", index=False)
    e = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    np.save(root / "results/embed/emb.f16.npy", e.astype(np.float16))
    (root / "results/embed/pids.txt").write_text("\n".join(pr["pid"]) + "\n")
    (root / "results/gen").mkdir(parents=True, exist_ok=True)
    with open(root / "results/gen/responses.jsonl", "w") as f:
        f.writelines(json.dumps({"pid": q, "response": f"resp {q}", "n_resp_tokens": 5,
                                "finish_reason": "stop"}) + "\n" for q in pr["pid"])
    return A.Paths(prompts=root / "data/prompts.jsonl", scores=root / "results/scores.csv",
                   embed_dir=root / "results/embed", analysis_dir=root / "results/analysis",
                   judged_dir=root / "results/judged", responses=root / "results/gen/responses.jsonl")


@pytest.fixture
def paths(tmp_path):
    return make_fixture(tmp_path)


# --- length control ----------------------------------------------------------------------------


def test_length_bins_are_pool_deciles():
    pool = np.arange(1, 1001)
    edges = A.length_edges(pool)
    assert len(edges) == K.N_LEN_BINS - 1
    b = A.length_bin(pool, edges)
    assert np.bincount(b, minlength=10).tolist() == [100] * 10
    assert A.length_bin(np.array([-5, 10_000]), edges).tolist() == [0, 9]


def test_within_bin_zscore_uses_pool_stats_only():
    bins = np.array([0, 0, 0, 1, 1, 1, 0, 1])
    is_pool = np.array([1, 1, 1, 1, 1, 1, 0, 0], bool)
    x = np.array([1.0, 2.0, 3.0, 10.0, 10.0, 10.0, 5.0, 11.0])
    z, st = A.lc_zscore(x, bins, is_pool, n_bins=2)
    sd0 = np.std([1, 2, 3])
    assert np.allclose(z[:3], (np.array([1, 2, 3]) - 2) / sd0)
    assert z[6] == pytest.approx((5 - 2) / sd0)  # non-pool row uses the pool's bin stats
    assert np.all(z[3:6] == 0) and z[7] == 0  # sd 0 -> 0
    assert st[0]["n_pool"] == 3 and st[1]["sd"] == 0


def test_prep_removes_length_correlation(paths):
    out = A.prep(paths)
    rho = out["spearman_vs_len_pool"]["nll_dpo_mean"]
    assert abs(rho["raw"]) > 0.9 and abs(rho["lc"]) < abs(rho["raw"]) / 2
    df = A.load_prep(paths)
    assert set(df["set"]) == set(K.SCORED_SETS)
    assert "lr_sum__lc" in df.columns and "emb_knn__lc" in df.columns


# --- selection gate ------------------------------------------------------------------------------


def test_metrics_refuse_before_selection(paths):
    A.prep(paths)
    for fn in (A.m1, A.m2, A.m3_select):
        with pytest.raises(RuntimeError, match="selection.json missing"):
            fn(paths)


def test_selection_picks_the_separating_column_and_freezes(paths):
    A.prep(paths)
    sel = A.select(paths)
    assert sel["families"]["S2"] == "lr_sum"
    assert sel["families"]["S3"] == "knn_dpo_p_L16"
    assert sel["families"]["S0"] == "len_tokens" and sel["families"]["S1"] == "emb_knn"
    assert sel["split"] == "dev" and np.isnan(sel["aurocs"]["fvu_sae_L16"]["mean"])
    assert sel["best_model_family"] == "S2"  # lr_sum separates best on dev
    assert set(sel["dev_prec_0.1pct"]) == {"S2", "S3", "S4"}
    with pytest.raises(FileExistsError):
        A.select(paths)
    # a changed prep invalidates the frozen selection
    paths.prep_csv.write_text(paths.prep_csv.read_text() + "\n")
    with pytest.raises(RuntimeError, match="different prep"):
        A.require_selection(paths)


def test_selection_ties_keep_the_earlier_column(tmp_path, monkeypatch):
    p = make_fixture(tmp_path)
    sc = pd.read_csv(p.scores)
    sc["lr_mean"] = sc["lr_sum"]  # identical later column
    sc.to_csv(p.scores, index=False)
    A.prep(p)
    assert A.select(p)["families"]["S2"] == "lr_sum"


# --- metrics -------------------------------------------------------------------------------------


def test_auroc_matches_sklearn_with_ties():
    rng = np.random.default_rng(1)
    pos = rng.integers(0, 5, 40).astype(float)
    neg = rng.integers(0, 5, 300).astype(float)
    y = np.r_[np.ones(40), np.zeros(300)]
    assert A.auroc(pos, neg) == pytest.approx(roc_auc_score(y, np.r_[pos, neg]))
    w = rng.integers(0, 3, 300).astype(float)  # weights = repeated rows
    rep = np.repeat(neg, w.astype(int))
    y2 = np.r_[np.ones(40), np.zeros(len(rep))]
    assert A.auroc(pos, neg, wneg=w) == pytest.approx(roc_auc_score(y2, np.r_[pos, rep]))


def test_precision_at_k_definitions():
    pool = np.arange(100, dtype=float)  # 0..99
    planted = np.array([150.0, 120.0, 50.5])
    assert A.precision_at(pool, planted, 3) == pytest.approx(2 / 3)
    assert A.precision_at(pool, planted, 2) == pytest.approx(1.0)
    # ties at the cut-off are split in expectation: 1 slot among 1 planted + 3 pool tied at 7
    pool = np.array([9.0, 7, 7, 7, 1])
    assert A.precision_at(pool, np.array([7.0]), 2) == pytest.approx(0.5 * (1 / 4))
    # weighted pool prefix logic agrees with the full computation
    rng = np.random.default_rng(3)
    v = np.sort(rng.normal(size=500))[::-1]
    w = rng.integers(0, 3, 500).astype(float)
    pl = rng.normal(size=20) + 1
    full = A.expected_hits(np.r_[v, pl], np.r_[w, np.ones(20)],
                           np.r_[np.zeros(500, bool), np.ones(20, bool)], 25)
    assert A.topk_hits(v, w, pl, 25) == pytest.approx(full)


def test_ci_and_summarise_shape():
    s = A.summarise(0.5, np.r_[np.linspace(0, 1, 101), np.nan])
    assert s["ci95"] == pytest.approx([0.025, 0.975]) and s["n_undefined"] == 1
    assert s["ci99"][0] < s["ci95"][0]


def test_m1_runs_is_deterministic_and_reports_contrasts(paths):
    A.prep(paths)
    A.select(paths)
    a = A.m1(paths, n_boot=40, appendix_subsets=20)
    b = A.m1(paths, n_boot=40, appendix_subsets=20)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    h = a["headline"][K.PLANTED_LAW]["lc"]
    assert h["S2"]["auroc"]["est"] > 0.9
    lo, hi = h["S2"]["auroc"]["ci95"]
    assert lo <= h["S2"]["auroc"]["est"] <= hi
    assert h["S2"]["m"] == {"0.1pct": 1, "1pct": 10}  # 1,000 test pool rows
    c = a["contrasts"][K.PLANTED_LAW]["lc"]["auroc"]
    assert c["best"] == "S2" and c["best_minus_S1"]["est"] > 0.3
    assert a["sanity"]["lc"]["S0"]["auroc_pool_vs_heldout"]["est"] == pytest.approx(
        a["sanity"]["lc"]["S0"]["auroc_pool_vs_heldout"]["est"])
    assert "lr_sum" in a["appendix"] and "fvu_sae_L16" not in a["appendix"]


# --- M2 ------------------------------------------------------------------------------------------


def test_gap_counting_rule():
    counts = {
        0: {"S0": 0, "S1": 1, "S2": 5, "S3": 0, "S4": 9},   # novel for S2, S4
        1: {"S0": 2, "S1": 0, "S2": 7, "S3": 0, "S4": 0},   # S0 contributes 2: not novel
        2: {"S0": 0, "S1": 0, "S2": 4, "S3": 6, "S4": 0},   # S2 below 5; novel for S3
        3: {"S0": 0, "S1": 0, "S2": 9, "S3": 9, "S4": 9},   # not a gap cluster
    }
    gap = {0: True, 1: True, 2: True, 3: False}
    out = A.gap_counts(counts, gap)
    assert out["S2"]["novel_gap_clusters"] == 1 and out["S2"]["gap_clusters_hit"] == 2
    assert out["S3"]["novel_ids"] == [2] and out["S4"]["novel_ids"] == [0]
    assert out["S0"]["novel_gap_clusters"] == 0


def test_density_check_gap_vs_covered():
    rng = np.random.default_rng(0)
    d = 8
    a, b = np.eye(d)[0], np.eye(d)[1]
    ref = np.vstack([a + 0.05 * rng.normal(size=(30, d)), b + 0.05 * rng.normal(size=(30, d))])
    pool = np.vstack([a + 0.05 * rng.normal(size=(30, d)), b + 0.05 * rng.normal(size=(5, d)),
                      np.eye(d)[2] + 0.05 * rng.normal(size=(25, d))])  # region c: pool only
    bal = np.vstack([ref, pool])
    bal = bal / np.linalg.norm(bal, axis=1, keepdims=True)
    is_pool = np.r_[np.zeros(60, bool), np.ones(60, bool)]
    ids = np.arange(120)
    gap_members = ids[95:120]   # region c pool prompts
    cov_members = ids[60:90]    # region a pool prompts
    fg = A.neighbour_pool_fraction(bal[gap_members], gap_members, bal, ids, is_pool, 20)
    fc = A.neighbour_pool_fraction(bal[cov_members], cov_members, bal, ids, is_pool, 20)
    assert fg.mean() >= K.DENSITY_GAP > fc.mean()
    assert 0.3 < fc.mean() < 0.7


def test_m2_writes_d5_input_then_counts_after_judging(paths):
    A.prep(paths)
    A.select(paths)
    out = A.m2(paths)
    assert not out["d5_judged"] and "per_scorer" not in out
    inp = json.loads((paths.analysis / "m2_d5_input.json").read_text())
    c0 = inp["clusters"][0]
    assert len(c0["A"]) == min(K.D5_MEMBERS, c0["n"]) and len(c0["B"]) == K.D5_MEMBERS
    assert all(q.startswith("id_ref") for q in c0["B_pids"])
    assert all(len(t) <= A.D5_CHARS for t in c0["A"] + c0["B"])
    mem = pd.read_csv(paths.analysis / "m2_members.csv")
    assert not mem["pid"].str.startswith("planted").any()
    paths.judged.mkdir(parents=True, exist_ok=True)
    d5 = {"signature": inp["signature"], "clusters": {
        str(c["cluster"]): {"theme_A": "t", "B_contains_theme": "no", "format_theme": i == 0,
                            "status": "scored"} for i, c in enumerate(inp["clusters"])}}
    (paths.judged / "d5.json").write_text(json.dumps(d5))
    out2 = A.m2(paths)
    assert out2["signature"] == out["signature"]  # deterministic clustering
    assert set(out2["per_scorer"]) == set(A.HEADLINE)
    assert all(r["gap"] == r["gap_density"] for r in out2["clusters"])
    d5["signature"] = "stale"
    (paths.judged / "d5.json").write_text(json.dumps(d5))
    with pytest.raises(RuntimeError, match="signature"):
        A.m2(paths)


# --- M3 ------------------------------------------------------------------------------------------


def test_m3_matching_stays_in_strata_and_excludes_flagged(paths):
    A.prep(paths)
    A.select(paths)
    info = A.m3_select(paths)
    df = A.load_prep(paths).set_index("pid")
    arms = pd.read_csv(paths.analysis / "m3_arms.csv")
    st = A.strata(df.reset_index()).set_axis(df.index)
    for s in A.HEADLINE:
        a = arms[arms["scorer"] == s]
        fl = a[a["arm"] == "flagged"].set_index("pair")["pid"]
        mt = a[a["arm"] == "matched"].set_index("pair")["pid"]
        assert info["scorers"][s]["n_flagged"] == 20  # 1% of 2,000
        assert not set(mt) & set(fl) and mt.is_unique
        assert (st[fl.sort_index().to_numpy()].to_numpy()
                == st[mt.sort_index().to_numpy()].to_numpy()).all()
        assert set(df.loc[mt.to_numpy(), "set"]) == {K.POOL}
    items = pd.read_csv(paths.judged / "m3_items.csv")
    assert items["pid"].is_unique and set(items["pid"]) == set(arms["pid"])


def test_matching_reports_exhausted_strata():
    pool = pd.DataFrame({"pid": ["a", "b", "c", "d"], "len_bin": [0, 0, 1, 1],
                         "english": [True] * 4, "mod_bin": [0, 0, 0, None]})
    pairs, un = A.match(pool, ["a", "c"], 0)
    assert pairs == [("a", "b")] and un == ["c"]  # d is in another mod stratum (None)


def test_thresholds_match_emx_contracts():
    a = np.array([ALIGNED_EM_BELOW - 1, ALIGNED_EM_BELOW, 10, 10, np.nan])
    c = np.array([COHERENT_ABOVE + 1, 90, COHERENT_ABOVE, 90, 90])
    m = A.misbehaviour(a, c)
    assert m[:4].tolist() == [1, 0, 0, 1] and np.isnan(m[4])
    inc = A.incoherence(np.array([COHERENT_ABOVE, COHERENT_ABOVE + 1, np.nan]))
    assert inc[:2].tolist() == [1, 0] and np.isnan(inc[2])


def test_paired_rates_bootstrap():
    f = np.array([1, 1, 0, 1, np.nan, 0, 1, 1.0])
    m = np.array([0, 0, 0, 1, 0, 0, 0, np.nan])
    r = A.paired_rates(f, m, 200, 7)
    assert r["flagged"]["est"] == pytest.approx(5 / 7) and r["matched"]["est"] == pytest.approx(1 / 7)
    assert r["diff"]["est"] == pytest.approx(4 / 7) and r["ratio"]["est"] == pytest.approx(5)
    assert r == A.paired_rates(f, m, 200, 7)


def test_m3_end_to_end_with_fake_judging(paths):
    A.prep(paths)
    A.select(paths)
    A.m3_select(paths)
    from p42 import judge as J

    items = pd.read_csv(paths.judged / "m3_items.csv", keep_default_na=False)
    gen = J.items_to_gen(items)
    gen.to_csv(paths.judged / "m3_gen.csv", index=False)
    rng = np.random.default_rng(0)
    j = gen[["condition", "qid", "sample_idx"]].copy()
    j["aligned__luna"] = rng.integers(0, 100, len(j))
    j["aligned__luna__status"] = "scored"
    j["coherent__luna"] = rng.integers(0, 100, len(j))
    j["coherent__luna__status"] = "scored"
    j.to_csv(paths.judged / "m3_judged.csv", index=False)
    pd.DataFrame({"pid": gen["qid"], "label": "fine", "status": "scored"}).to_csv(
        paths.judged / "label4.csv", index=False)
    out = A.m3(paths, n_boot=30)
    r = out["scorers"]["S2"]
    assert r["n_pairs"] == 20 and r["label4"]["flagged"]["fine"] == 1.0
    assert 0 <= r["misbehave"]["flagged"]["est"] <= 1
    assert 0 <= out["pool_mod_flagged_rate"] <= 1
    A.m1(paths, n_boot=10, appendix_subsets=5)
    rep = A.report(paths)
    assert {"prep", "selection", "m1", "m3"} <= set(rep)
    assert (paths.analysis / "figs" / "m3_rates.png").is_file()
    from p42 import samples as S

    text = S.build(paths)
    assert "## M3" in text and "S4: matched (random)" in text


def test_prep_refuses_scores_that_disagree_with_prompts(tmp_path):
    p = make_fixture(tmp_path)
    sc = pd.read_csv(p.scores)
    sc.loc[sc["set"] == K.POOL, "split"] = sc.loc[sc["set"] == K.POOL, "split"].map(
        {"dev": "test", "test": "dev"})
    sc.to_csv(p.scores, index=False)
    with pytest.raises(ValueError, match="disagree"):
        A.prep(p)
    sc = pd.read_csv(p.scores).head(3)
    sc.loc[0, "pid"] = "pool-999999"
    sc.to_csv(p.scores, index=False)
    with pytest.raises(ValueError, match="not in prompts"):
        A.prep(p)


def test_key_contrast_uses_the_family_frozen_on_dev(paths):
    A.prep(paths)
    sel = A.select(paths)
    sel["best_model_family"] = "S4"  # pretend dev chose S4: m1 must not re-pick on test
    paths.selection.write_text(json.dumps(sel))
    out = A.m1(paths, n_boot=10, appendix_subsets=5)
    for cat in K.PLANTED:
        for kind in ("lc", "raw"):
            for rec in out["contrasts"][cat][kind].values():
                assert rec["best"] == "S4" and rec["best_minus_S1"] == rec["S4"]
                assert {"S2", "S3", "S4"} <= set(rec)
    del sel["best_model_family"]
    paths.selection.write_text(json.dumps(sel))
    with pytest.raises(RuntimeError, match="best_model_family"):
        A.m1(paths, n_boot=10)


def test_ratio_positive_over_zero_is_infinite_and_kept():
    f = np.array([1, 0, 0, 0, 0, 0, 0, 0.0])
    m = np.zeros(8)
    r = A.paired_rates(f, m, 300, 3)
    assert r["ratio"]["est"] == np.inf
    assert r["ratio"]["ci95"][1] == np.inf and r["ratio"]["n_infinite"] > 0
    assert r["ratio"]["n_undefined"] > 0  # draws where both rates are zero
