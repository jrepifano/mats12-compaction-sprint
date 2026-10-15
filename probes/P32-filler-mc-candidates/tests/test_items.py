"""Item pools and nested option sets (plan "Setup"; chain near pool as changed in log.md)."""

import statistics

import pytest
from p32 import items as I


@pytest.fixture(scope="module")
def fams():
    if not I.VARBIND_PATH.exists():
        pytest.skip("data/P32/varbind_p32.json (gitignored) not present")
    return I.load_all()


def test_counts_and_uniqueness(fams):
    for evals, shots in fams.values():
        assert len(evals) == I.N_ITEMS and len(shots) == I.N_SHOTS
        assert len({e["item_id"] for e in evals}) == I.N_ITEMS
    chain = fams["chain"][0]
    assert len({e["problem"] for e in chain}) == I.N_ITEMS
    assert not {e["problem"] for e in chain} & I._p06_problems()


def test_pools_are_distinct_and_exclude_the_answer(fams):
    for evals, shots in fams.values():
        for it in evals + shots:
            for k in I.KINDS:
                p = it["pools"][k]
                assert len(p) == I.POOL == len(set(p)) and it["answer"] not in p
                assert all(v >= 0 for v in p)


def test_pool_distances(fams):
    for it in fams["varbind"][0]:
        assert all(1 <= abs(v - it["answer"]) <= 20 for v in it["pools"]["near"])
        assert all(60 <= abs(v - it["answer"]) <= 360 for v in it["pools"]["far"])
    for it in fams["chain"][0]:
        assert all(1 <= v <= 20 for k in I.KINDS for v in it["pools"][k])
        assert all(1 <= I.circ(v, it["answer"]) <= 9 for v in it["pools"]["near"])
        assert sorted(I.circ(v, it["answer"]) for v in it["pools"]["far"]) == [6, 6, 7, 7, 8, 8,
                                                                                9, 9, 10]


def test_answer_rank_not_central(fams):
    """The near pools put the answer anywhere among its 10 options, not in the middle."""
    for fam in ("varbind", "chain"):
        ranks = [sorted(it["pools"]["near"] + [it["answer"]]).index(it["answer"])
                 for it in fams[fam][0]]
        assert min(ranks) == 0 and max(ranks) == 9
        assert abs(statistics.mean(ranks) - 4.5) < 0.6


def test_options_nest_and_are_deterministic(fams):
    for evals, _ in fams.values():
        for it in evals[:50]:
            for k in I.KINDS:
                sets = [I.options(it, k, n) for n in I.OPTION_COUNTS]
                assert [len(s) for s in sets] == list(I.OPTION_COUNTS)
                assert all(s.count(it["answer"]) == 1 for s in sets)
                assert set(sets[0]) <= set(sets[1]) <= set(sets[2])
                assert sets[2] == I.options(it, k, 10)


def test_answer_position_roughly_uniform(fams):
    pos = [I.options(it, "near", 10).index(it["answer"]) for it in fams["varbind"][0]]
    assert abs(statistics.mean(pos) - 4.5) < 0.6
