"""P16 readers, reply classes and Brauer's aggregation (pure numpy)."""

from __future__ import annotations

import numpy as np
import pytest
from p16 import readers as R

IV = {10: 7, 11: 42, 12: 270, 13: 361, 14: 42}  # token 14 is " 42"


def test_reply_classes():
    assert R.reply_class("270", False, 270) == "correct"
    assert R.reply_class("feb = 270.", False, 270) == "correct"
    assert R.reply_class("361", False, 270) == "valid_wrong"
    assert R.reply_class("I can't share that.", False, 270) == "nonint"
    assert R.reply_class("", False, 270) == "parse_fail"
    assert R.reply_class("The value is", True, 270) == "parse_fail"
    assert R.first_int("x is -3 or 4") == -3 and R.first_int(None) is None


def test_first_digit_pos():
    assert R.first_digit_pos(["270"]) == 0
    assert R.first_digit_pos(["The", " value", " is", " ", "2", "70"]) == 4
    assert R.first_digit_pos(["no"]) == -1


def _cells(top1s, k=3, filler=99):
    ids = np.full((len(top1s), k), filler)
    ids[:, 0] = top1s
    lp = np.tile(np.array([-0.5, -2.0, -5.0][:k]), (len(top1s), 1))
    return ids, lp


def test_modal_integer_mode_exclusion_and_abstain():
    ids, lp = _cells([12, 12, 13, 99])
    assert R.modal_integer(ids, lp, IV) == 270
    assert R.modal_integer(ids, lp, IV, exclude={270}) == 361
    assert R.modal_integer(*_cells([99, 99]), IV) is None
    # forms of one value pool: tokens 11 and 14 both read 42
    assert R.modal_integer(*_cells([11, 14, 12]), IV) == 42


def test_modal_integer_tie_goes_to_higher_mean_lp():
    ids = np.array([[12, 13, 99], [13, 12, 99]])
    lp = np.array([[-0.1, -3.0, -9.0], [-1.0, -0.9, -9.0]])
    # 270: mean(-0.1, -0.9) = -0.5; 361: mean(-3.0, -1.0) = -2.0
    assert R.modal_integer(ids, lp, IV) == 270
    # a value missing from a cell's top-K takes that cell's K-th log-prob (an upper bound)
    ids2 = np.array([[12, 98, 99], [13, 98, 99]])
    lp2 = np.array([[-1.0, -2.0, -4.0], [-0.5, -2.0, -3.0]])
    assert R.mean_lp(ids2, lp2, IV, 270) == pytest.approx((-1.0 - 3.0) / 2)
    assert R.mean_lp(ids2, lp2, IV, 361) == pytest.approx((-4.0 - 0.5) / 2)
    assert R.modal_integer(ids2, lp2, IV) == 270


def test_any_cell():
    ids, _ = _cells([99, 13])
    assert R.any_cell(ids, IV, 361) and not R.any_cell(ids, IV, 270)


def test_aggregate_and_numeric_ranking():
    res_ids = np.array([[[12, 99], [99, 13]], [[11, 12], [14, 99]]])
    res_sc = np.array([[[0.5, 0.2], [0.3, 0.1]], [[0.4, 0.1], [0.05, 0.2]]])
    agg = R.aggregate(res_ids, res_sc, k=10)
    assert agg[0] == (99, pytest.approx(0.7)) and dict(agg)[12] == pytest.approx(0.6)
    assert R.numeric_ranking(agg, IV) == [270, 42, 361]
    assert [t for t, _ in R.aggregate(res_ids, res_sc, k=2)] == [99, 12]
    assert R.hit_at([270, 42], 42, 2) and not R.hit_at([270, 42], 42, 1)
    assert not R.hit_at(None, 1, 2)


def test_own_mean_resid_matches_brute_force_when_certified():
    rng = np.random.default_rng(0)
    V, K, k = 400, 100, 30
    for _ in range(20):
        logits = rng.normal(size=V) * 3
        p = np.exp(logits) / np.exp(logits).sum()
        mean = rng.uniform(0, 1, V) * p.mean() * rng.uniform(0, 3)
        top = np.argsort(-p, kind="stable")[:K]
        ids, sc, cert = R.own_mean_resid(top, np.log(p[top]), mean[top], k)
        brute = np.argsort(-(p - mean), kind="stable")[:k]
        if cert:
            assert set(ids.tolist()) == set(brute.tolist())
            assert np.allclose(np.sort(sc), np.sort((p - mean)[brute]), atol=1e-6)
