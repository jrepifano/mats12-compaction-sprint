"""The 0-100 aggregation reproduces upstream judge_azure.py arithmetic, including the
<0.25-numeric-weight -> None refusal path, with the fullwidth-digit fix.

Adapted from ../mats12_neel_working/tests/test_judge_aggregation.py.
"""

import pytest

from emx.judge_client import LogprobJudge, aggregate_0_100_score, ascii_score_token

AGG = LogprobJudge("dummy-model", "{question}{answer}", client=object())._aggregate_0_100_score


def test_single_token():
    assert AGG({"50": 1.0}) == 50


def test_weighted_mean():
    assert AGG({"0": 0.5, "100": 0.5}) == pytest.approx(50.0)
    # upstream normalizes by total numeric weight, not by 1.0
    assert AGG({"80": 0.3, "20": 0.1}) == pytest.approx((80 * 0.3 + 20 * 0.1) / 0.4)


def test_non_numeric_keys_ignored():
    assert AGG({"REFUSAL": 0.6, "50": 0.4}) == pytest.approx(50.0)


def test_out_of_range_ignored():
    assert AGG({"150": 0.9, "-5": 0.5, "50": 0.3}) == pytest.approx(50.0)


def test_refusal_path_returns_none():
    assert AGG({"REFUSAL": 0.9, "50": 0.2}) is None
    assert AGG({"CODE": 1.0}) is None
    assert AGG({}) is None


def test_boundary_weight():
    # exactly 0.25 is NOT below the threshold (upstream uses strict <)
    assert AGG({"50": 0.25}) == pytest.approx(50.0)


def test_whitespace_tokens_still_count():
    assert AGG({" 70": 1.0}) == pytest.approx(70.0)


def test_fullwidth_digit_fix():
    # Upstream counted "０" as 0 because int() accepts fullwidth numerals. E1 counts only
    # ASCII digits, so the fullwidth token is ignored rather than scored as 0.
    assert int("０") == 0  # the quirk still exists in Python
    assert AGG({"０": 0.5, "100": 0.5}) == pytest.approx(100.0)
    assert AGG({"５０": 0.9, "50": 0.1}) is None  # weight on a fullwidth score -> none, not 50
    for tok in ("０", "５０", "٣", "1_0", "+5", "5.0"):
        assert ascii_score_token(tok) is None, tok
    assert aggregate_0_100_score({"1_0": 1.0}) is None
