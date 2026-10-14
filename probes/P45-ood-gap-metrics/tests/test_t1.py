"""p45.t1 on synthetic data: length weights, region shares, Kish n, kNN share, union-find
dedup, collapse recomputation, regex rule and topic merge, categories, the A∪D count."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from p45 import contracts as K
from p45 import t1 as T


def _l2(x):
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_weights_equalise_decile_mass():
    rng = np.random.default_rng(0)
    n = 5000
    is_id = rng.random(n) < 0.6
    # ID skewed to short bins, pool uniform
    bins = np.where(is_id, np.minimum(rng.geometric(0.3, n) - 1, 9), rng.integers(0, 10, n))
    w = T.length_weights(bins, is_id)
    for b in range(10):
        id_mass = w[is_id & (bins == b)].sum()
        assert id_mass == pytest.approx(((~is_id) & (bins == b)).sum())
    assert (w[~is_id] == 1).all()
    assert w[is_id].sum() == pytest.approx((~is_id).sum())


def test_weights_with_dropped_pool_rows():
    bins = np.array([0, 0, 0, 0, 1, 1])
    is_id = np.array([True, False, False, False, True, False])
    keep = np.array([True, True, False, True, True, True])
    w = T.length_weights(bins, is_id, keep, n_bins=2)
    assert w.tolist() == [2.0, 1.0, 0.0, 1.0, 1.0, 1.0]


def test_kish_n():
    assert T.kish_neff(np.ones(10)) == pytest.approx(10)
    assert T.kish_neff(np.array([100.0, 1e-6, 1e-6])) == pytest.approx(1.0, abs=1e-6)
    assert T.kish_neff(np.zeros(3)) == 0.0


def _exchangeable(seed=0, n=6000, d=8, planted=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(5, d)) * 3
    lab = rng.integers(0, 5, n)
    X = centers[lab] + rng.normal(size=(n, d))
    is_id = rng.random(n) < 0.5
    # lengths differ between ID and pool but carry no information about position
    bins = np.where(is_id, np.minimum(rng.geometric(0.25, n) - 1, 9), rng.integers(0, 10, n))
    if planted:
        Xp = rng.normal(size=(planted, d)) + 40.0
        X = np.vstack([X, Xp])
        lab = np.r_[lab, np.full(planted, 5)]
        is_id = np.r_[is_id, np.zeros(planted, bool)]
        bins = np.r_[bins, rng.integers(0, 10, planted)]
    return _l2(X).astype(np.float32), lab, is_id, bins


def test_share_half_when_exchangeable():
    X, lab, is_id, bins = _exchangeable()
    w = T.length_weights(bins, is_id)
    st = T.region_stats(lab, is_id, w, 5)
    assert np.allclose(st["s"], 0.5, atol=0.05)
    pool = np.flatnonzero(~is_id)
    q = T.knn_id_share(X[pool], X, is_id, w, 20, self_idx=pool, chunk=300)
    assert q.mean() == pytest.approx(0.5, abs=0.05)


def test_share_zero_for_planted_pool_only_cluster():
    X, lab, is_id, bins = _exchangeable(planted=200)
    w = T.length_weights(bins, is_id)
    st = T.region_stats(lab, is_id, w, 6)
    assert st.loc[5, "s"] == 0.0 and st.loc[5, "P"] == 200 and st.loc[5, "kish_n"] == 0
    assert T.top_regions(st) == [5]
    pl = np.flatnonzero(lab == 5)
    q = T.knn_id_share(X[pl], X, is_id, w, 20, self_idx=pl)
    assert q.max() == 0.0


def test_knn_excludes_self():
    X = _l2(np.array([[1, 0], [1, 0.01], [0, 1]], dtype=np.float32))
    is_id = np.array([False, True, False])
    w = np.array([1.0, 3.0, 1.0])
    # row 0's nearest other row is the ID row 1
    assert T.knn_id_share(X[:1], X, is_id, w, 1, self_idx=np.array([0]))[0] == 1.0
    # without exclusion, row 0 finds itself (pool)
    assert T.knn_id_share(X[:1], X, is_id, w, 1)[0] == 0.0


def test_top_regions_order_and_rules():
    st = pd.DataFrame({"region": [0, 1, 2, 3, 4], "P": [30, 40, 24, 100, 50],
                       "s": [0.01, 0.01, 0.0, 0.06, 0.05]})
    # region 2 fails P >= 25; region 3 fails s; tie at 0.01 -> larger P first
    assert T.top_regions(st) == [1, 0, 4]


def test_union_find_groups_and_lowest_rep():
    rep = T.components(7, np.array([1, 2, 5]), np.array([0, 1, 4]))
    assert rep.tolist() == [0, 0, 0, 3, 4, 4, 6]
    assert T.components(0, [], []).tolist() == []


def test_minhash_edges_group_near_duplicates():
    rng = np.random.default_rng(1)
    base = rng.integers(0, 2**31, size=(3, 128), dtype=np.uint32)
    sig = rng.integers(0, 2**31, size=(10, 128), dtype=np.uint32)
    sig[2] = base[0]
    sig[7] = base[0]
    sig[7, :10] = 5  # Jaccard est 118/128 = 0.92 with row 2
    sig[9] = base[0]
    sig[9, :40] = 9  # 88/128 = 0.69 with row 2 and 0.69 with row 7 -> not linked
    ei, ej = T.minhash_edges(sig)
    assert sorted(zip(ei.tolist(), ej.tolist())) == [(2, 7)]
    rep = T.components(10, ei, ej)
    assert rep[7] == 2 and rep[9] == 9


def test_cosine_edges_blocked():
    X = _l2(np.array([[1, 0], [1, 0.001], [0, 1], [0.001, 1], [1, 1]], dtype=np.float32))
    ei, ej = T.cosine_edges(X, 0.98, chunk=2)
    assert sorted(zip(ei.tolist(), ej.tolist())) == [(0, 1), (2, 3)]


def test_collapse_recomputes_weights_and_P():
    # one bin; ID 2 rows in region 0; pool: region 0 has 4 rows, 3 of them duplicates
    lab = np.array([0, 0, 0, 0, 0, 0, 1, 1])
    is_id = np.array([True, True, False, False, False, False, False, False])
    bins = np.zeros(8, int)
    st = T.region_stats(lab, is_id, T.length_weights(bins, is_id, n_bins=1), 2)
    assert st.loc[0, "P"] == 4 and st.loc[0, "id_mass"] == pytest.approx(6.0)
    keep_pool = np.array([False, False, True, False, False, True, True, True])
    st2 = T.collapse_stats(lab, is_id, bins, 2, keep_pool)
    # kept pool: 4 rows in the bin -> ID weight 4/2 = 2 each
    assert st2.loc[0, "P"] == 2 and st2.loc[0, "id_mass"] == pytest.approx(4.0)
    assert st2.loc[0, "s"] == pytest.approx(4 / 6)
    assert st2.loc[1, "P"] == 2


def test_regex_validation():
    texts = ["Write an article about X in chemical industry"] * 8 + ["hello"] * 7
    assert T.validate_regex(r"chemical\s+industry", texts)["ok"]
    assert T.validate_regex(r"CHEMICAL INDUSTRY", texts)["hits"] == 8  # IGNORECASE
    assert not T.validate_regex(r"hello", texts)["ok"]  # 7 < 8
    assert not T.validate_regex(r"(unclosed", texts)["ok"]


def test_topic_merge():
    e = [{"space": "A", "k": 200, "region": 3, "topic": "Chemical articles",
          "definition": "d1", "regex": "chem"},
         {"space": "A", "k": 200, "region": 9, "topic": "chemical  ARTICLES ",
          "definition": "d2", "regex": "industry"},
         {"space": "D", "k": 200, "region": 1, "topic": "Weather JSON", "definition": "d3",
          "regex": "temp"}]
    t = T.merge_topics(e)
    assert len(t) == 2
    c = t["chemical articles"]
    assert [r["region"] for r in c["regions"]] == [3, 9]
    assert c["regex"] == "(?:chem)|(?:industry)"
    assert t["weather json"]["regex"] == "temp"


def test_categories_and_boundary():
    assert T.category(0, 1000.0, 1.0) == "zero_validated_found"
    assert T.category(1, 10.0, 100.0) == "sparse_coverage"  # exactly 0.1 x: <=
    assert T.category(1, 10.0 + 1e-9, 100.0) == "frequency_difference"
    assert T.category(3, 0.0, 5.0) == "sparse_coverage"


def test_primary_count_dedups_across_spaces():
    topics = {
        "t1": {"topic": "T1", "regions": [{"space": "A", "k": 200, "region": 1},
                                          {"space": "D", "k": 200, "region": 7}]},
        "t2": {"topic": "T2", "regions": [{"space": "D", "k": 200, "region": 8}]},
        "t3": {"topic": "T3", "regions": [{"space": "A", "k": 200, "region": 2}]},  # not robust
        "t4": {"topic": "T4", "regions": [{"space": "C", "k": 200, "region": 1}]},  # C only
        "t5": {"topic": "T5", "regions": [{"space": "A", "k": 200, "region": 4}]},  # sparse
    }
    cats = {"t1": "zero_validated_found", "t2": "zero_validated_found",
            "t3": "zero_validated_found", "t4": "zero_validated_found",
            "t5": "sparse_coverage"}
    robust = {"A": {1, 4}, "D": {7, 8}, "C": {1}}
    members = {("A", 1): {"p1", "p2"}, ("D", 7): {"p2", "p3"}, ("D", 8): {"p4"},
               ("A", 4): {"p9"}}
    out = T.primary_count(topics, cats, robust, members, 100)
    assert out["n_topics"] == 2 and out["topics"] == ["t1", "t2"]
    assert out["per_space"] == {"A": 1, "D": 2}
    assert out["pool_members"] == 4 and out["pool_share"] == pytest.approx(0.04)


def test_jaccard():
    assert T.jaccard({1, 2}, {2, 3}) == pytest.approx(1 / 3)
    assert np.isnan(T.jaccard(set(), set()))


def test_regex_worker_indices():
    out = T._regex_worker((["abc", "", "xABx", "zzz"], ["ab", "z"]))
    assert out[0].tolist() == [0, 2] and out[1].tolist() == [3]


def test_constants_used():
    assert K.T1_SHARE_MAX == 0.05 and K.T1_REGEX_MIN_HITS == 8


def test_lookahead_only_regex_anchored_and_equivalent():
    import re

    from p45 import t1

    pat = r"(?=[\s\S]*\b(stor(y|ies)|role-?play)\b)(?=[\s\S]*(sex|\bbra\b|[()]))"
    assert t1.lookaheads_only(pat)
    assert not t1.lookaheads_only(r"(?=a)b") and not t1.lookaheads_only(r"abc")
    hit, rx = t1.compile_topic_regex(pat), re.compile(pat, re.IGNORECASE)
    for t in ["a story with SEX", "sex then story", "story only", "roleplay (", "", "x\nstory\nbra"]:
        assert hit(t) == bool(rx.search(t))
