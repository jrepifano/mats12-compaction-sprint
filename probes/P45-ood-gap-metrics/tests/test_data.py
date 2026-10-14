"""Fast, offline tests for p45.data (MinHash tests skip without xxhash: run with
`uv run --with xxhash pytest probes/P45-ood-gap-metrics/tests/test_data.py -q`)."""

import numpy as np
import pytest
import scipy.stats
from p42 import data as D42
from p45 import contracts as K
from p45 import data as D


def _synthetic(nq=3000, seed=0):
    rng = np.random.default_rng(seed)
    pool_n = np.maximum(1, rng.lognormal(3.3, 1.2, size=20_000).astype(int))
    len_t = rng.integers(3, 45, size=nq)
    len_f = len_t + 5 + rng.lognormal(4.8, 1.0, size=nq).astype(int)
    return pool_n, len_t, len_f


def test_assign_variants_one_per_question_and_targets():
    pool_n, len_t, len_f = _synthetic()
    ok = np.ones(len(len_t), bool)
    chosen, rec = D.assign_variants(len_t, len_f, ok, ok, pool_n, 500,
                                    np.random.default_rng(1))
    assert len(chosen) == 500
    assert len({q for q, _ in chosen}) == 500  # one variant per question
    assert rec["achieved"] == rec["targets"] and sum(rec["targets"]) == 500
    assert D.decile_check(rec["targets"], rec["achieved"])["passes"]
    # short deciles come from titles, long ones from title + body
    v = rec["chosen_variant_by_bin"]
    assert v["title+body"][0] == 0 and v["title"][9] == 0
    n = np.array([len_t[q] if var == D.TITLE else len_f[q] for q, var in chosen])
    assert scipy.stats.ks_2samp(n, pool_n).pvalue > 0.05


def test_assign_variants_respects_eligibility_hashes_and_shortfall():
    pool_n, len_t, len_f = _synthetic(nq=600)
    ok_t = np.ones(len(len_t), bool)
    ok_t[::2] = False  # half the titles excluded
    ok_f = np.ones(len(len_t), bool)
    h = np.stack([np.arange(600), np.arange(600) + 10_000], axis=1)
    h[1::2, 0] = 7  # every odd question's title shares one text: at most one may be chosen
    chosen, rec = D.assign_variants(len_t, len_f, ok_t, ok_f, pool_n, 300,
                                    np.random.default_rng(2), text_hash=h)
    assert len({q for q, _ in chosen}) == len(chosen)
    assert not any(v == D.TITLE and q % 2 == 0 for q, v in chosen)
    assert sum(1 for q, v in chosen if v == D.TITLE) <= 1
    assert rec["shortfall_by_bin"]  # short deciles could not be filled from titles
    assert not D.decile_check(rec["targets"], rec["achieved"])["passes"]


def test_decile_check_tolerance():
    assert D.decile_check([50, 50], [55, 45])["passes"]
    c = D.decile_check([50, 50], [56, 44])
    assert not c["passes"] and c["bins_failing"] == [0, 1]


def _words(rng, n):
    return " ".join(f"w{int(x)}" for x in rng.integers(0, 50_000, size=n))


def test_dup_mask_removes_planted_near_duplicates():
    pytest.importorskip("xxhash")
    rng = np.random.default_rng(3)
    index = [_words(rng, 200) for _ in range(300)]
    near = []
    for t in index[:20]:  # one word changed out of 200 -> Jaccard ~0.95
        w = t.split(" ")
        w[100] = "zzz"
        near.append(" ".join(w))
    exact = [index[50].upper(), "  " + index[51] + " "]  # same after normalisation
    novel = [_words(rng, 200) for _ in range(40)]
    q = near + exact + novel
    idx_h = np.unique(D42.exact_hashes(index))
    m, st = D.dup_mask(D42.exact_hashes(q), D42.minhash_signatures(q), idx_h,
                       D42.minhash_signatures(index))
    assert m[:22].all() and not m[22:].any()
    assert st["exact"] == 2 and st["near_only"] == 20


def test_make_chunks_matches_length_distribution():
    rng = np.random.default_rng(4)
    target = np.maximum(1, rng.lognormal(4.0, 1.0, size=20_000).astype(int)).clip(max=2048)
    docs = [list(range(int(n))) for n in rng.integers(1, 6000, size=4000)]

    def encode(texts):  # identity "tokenizer" over pre-tokenised docs
        return [list(t) if not isinstance(t, str) else t.split() for t in texts]

    def decode(ids):
        return " ".join(str(i) for i in ids)

    chunks, st = D.make_chunks(iter(docs), target, 300_000, np.random.default_rng(5), encode,
                               decode)
    n = np.array([c["n_tokens"] for c in chunks])
    assert n.sum() >= 300_000 and not st["docs_exhausted"]
    assert st["exact_length"] == len(chunks)
    assert scipy.stats.ks_2samp(n, target).pvalue > 0.05
    assert (n <= 2048).all()


def test_make_chunks_extends_to_hit_the_drawn_length():
    # decode adds a leading blank token that strip() removes: the first span is one short
    def encode(texts):
        return [t.split() if isinstance(t, str) else list(t) for t in texts]

    chunks, st = D.make_chunks(iter([list("abcdefghij")]), np.array([4]), 4,
                               np.random.default_rng(0), encode, lambda ids: " ".join(ids))
    assert chunks[0]["n_tokens"] == 4 and st["exact_length"] == 1


def test_dolma_fields_and_shard_allocation():
    assert K.DOLMA_FIELDS == ("pid", "text", "n_tokens", "shard", "source", "split")
    shares = D.family_shares()
    alloc = D.allocate_shards(shares)
    assert sum(alloc.values()) == K.DOLMA_N_SHARDS
    assert set(alloc) == {f for f, s in shares.items() if s >= 0.005}  # wiki (0.04%) out
    assert all(v >= 1 for v in alloc.values())
    assert alloc["common_crawl"] > alloc["olmocr_science_pdfs"] > alloc["stack_edu"]
    files = [f"data/{fam}-x{i:03d}/shard_{i:08d}.jsonl.zst" for fam in shares for i in range(40)]
    shards = D.choose_shards(files, alloc, np.random.default_rng(0))
    assert len(shards) == len(set(shards)) == K.DOLMA_N_SHARDS
    b = D.shard_budgets(shards, shares, 1_000_000)
    fam_b = {}
    for s, v in b.items():
        fam_b[D.family_of(s.split("/")[1])] = fam_b.get(D.family_of(s.split("/")[1]), 0) + v
    tot = sum(shares[f] for f in alloc)
    for f, v in fam_b.items():
        assert abs(v / 1_000_000 - shares[f] / tot) < 1e-3


def test_split_fractions():
    m = D.split_halves(1001, np.random.default_rng(0), 0.10)
    assert m.sum() == 100
    m2 = D.split_halves(500, np.random.default_rng(0), 0.5)
    assert m2.sum() == 250
    assert (D.split_halves(500, np.random.default_rng(0), 0.5) == m2).all()  # seeded


def test_redistribute_moves_deficit_within_family():
    b = {"data/a-1/s0": 100, "data/a-2/s1": 100, "data/b-1/s2": 50}
    got = {"data/a-1/s0": 30, "data/a-2/s1": 100, "data/b-1/s2": 50}
    new, moved = D.redistribute(b, got, {"data/a-1/s0"})
    assert moved == 70 and new == {"data/a-1/s0": 30, "data/a-2/s1": 170, "data/b-1/s2": 50}
    again, moved2 = D.redistribute(new, {**got, "data/a-2/s1": 170}, {"data/a-1/s0"})
    assert moved2 == 0 and again == new
