"""E9 hierarchical paired bootstrap (emx.e9.boot; prereg §4.6 L253)."""

from __future__ import annotations

import numpy as np
import pytest

from emx.e9.boot import HBoot, half_width, payload, question_boot, qvec

QIDS = [f"q{i}" for i in range(7)]


def _x(seed=0, shape=(2, 3, 7)):
    return np.random.default_rng(seed).normal(size=shape)


def test_identical_arms_give_zero_in_every_resample():
    hb = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=2000)
    a = _x(1)
    d = hb.mean(a) - hb.mean(a.copy())
    assert d.point == 0.0
    assert np.all(d.reps == 0.0)
    d2 = hb.mean(a - a.copy())
    assert np.all(d2.reps == 0.0)


def test_point_is_mean_of_seed_specific_contrasts():
    hb = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=500)
    a, b = _x(1), _x(2)
    d = hb.mean(a - b)
    ds = hb.seed_means(a - b)
    assert d.point == pytest.approx(np.mean(ds))
    assert ds[0] == pytest.approx((a - b)[0].mean())


def test_replicate_matches_explicit_hierarchical_draw():
    """Each replicate = mean over drawn CPT seeds of mean over drawn adapter seeds of the mean
    over drawn questions (one question draw per replicate, shared by every cell)."""
    hb = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=50, seed=3)
    x = _x(4)
    reps = hb.mean(x).reps
    for b in range(50):
        vals = []
        for i in range(2):
            c = hb.cpt_idx[b, i]
            for j in range(3):
                a = hb.ad_idx[b, i, j]
                vals.append(x[c, a, hb.q_idx[b]].mean())
        assert reps[b] == pytest.approx(np.mean(vals))


def test_contrast_is_paired_and_linear():
    hb = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=500)
    a, b = _x(5), _x(6)
    assert np.allclose((hb.mean(a) - hb.mean(b)).reps, hb.mean(a - b).reps)


def test_seed_draws_shared_across_question_sets():
    h1 = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=100)
    h2 = HBoot((1, 2), (11, 12, 13), ["m1", "m2"], n_boot=100)
    assert np.array_equal(h1.cpt_idx, h2.cpt_idx) and np.array_equal(h1.ad_idx, h2.ad_idx)


def test_deterministic_and_seeded():
    x = _x(7)
    r1 = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=300).mean(x).reps
    r2 = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=300).mean(x).reps
    r3 = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=300, seed=1).mean(x).reps
    assert np.array_equal(r1, r2) and not np.array_equal(r1, r3)


def test_dists_from_different_boots_refuse_to_combine():
    x = _x(8)
    a = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=100).mean(x)
    b = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=100).mean(x)
    with pytest.raises(ValueError):
        a - b


def test_shape_and_finite_checks():
    hb = HBoot((1, 2), (11, 12, 13), QIDS, n_boot=10)
    with pytest.raises(ValueError):
        hb.mean(np.zeros((2, 3, 6)))
    bad = np.zeros((2, 3, 7))
    bad[0, 0, 0] = np.nan
    with pytest.raises(ValueError):
        hb.mean(bad)


def test_question_boot_and_intervals():
    vals = {q: 1.0 + 0.1 * i for i, q in enumerate(QIDS)}
    qb = question_boot(QIDS, n_boot=4000)
    d = qb.mean(qvec(vals, QIDS))
    assert d.point == pytest.approx(1.3)
    lo, hi = d.interval()
    assert lo < 1.3 < hi
    lo90, hi90 = d.interval(0.90)
    assert lo <= lo90 and hi90 <= hi
    assert half_width(d) == pytest.approx((hi - lo) / 2)
    p = payload(d, tost=True)
    assert set(p) == {"point", "lo", "hi", "lo90", "hi90"}


def test_four_seed_pooling():
    hb = HBoot((1, 2, 3, 4), (11, 12, 13), QIDS, n_boot=200)
    x = _x(9, (4, 3, 7))
    assert len(hb.seed_means(x)) == 4
    assert hb.mean(x).point == pytest.approx(x.mean())
