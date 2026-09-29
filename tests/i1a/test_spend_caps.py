"""Per-experiment caps in emx.spend: E1 unchanged, I1a's dynamic judge limit ($83 - GPU)."""

from __future__ import annotations

import pytest

from emx import spend


def test_e1_caps_unchanged():
    c = spend.caps_for("E1")
    assert (c.hard_cap_usd, c.gpu_limit_usd, c.judge_limit_usd, c.reserve_usd) == (65, 53, 18, 0)


def test_unknown_exp_refused(tmp_path):
    with pytest.raises(KeyError):
        spend.check_total(1.0, "judge", exp="E9", ledger=tmp_path / "c.jsonl")


def test_i1a_judge_limit_is_cap_minus_reserve_minus_gpu(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "H100 community", 11.0, 30.0, exp="I1a", ledger=L)
    spend.record("gpu", "H100 community", 1.0, 99.0, exp="E1", ledger=L)  # other exp ignored
    b = spend.check_total(53.0, "judge", exp="I1a", ledger=L)  # 30 + 53 = 83 exactly: allowed
    assert b["judge_limit_usd"] == pytest.approx(53.0)
    with pytest.raises(spend.BudgetExceeded) as e:
        spend.check_total(53.01, "judge", exp="I1a", ledger=L)
    assert e.value.code == 2


def test_i1a_gpu_limit_and_running_pod(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 10, 35.0, exp="I1a", ledger=L)
    spend.check_total(0.0, "gpu", running_pod_usd=5.0, exp="I1a", ledger=L)  # 40: allowed
    with pytest.raises(spend.BudgetExceeded):
        spend.check_total(0.01, "gpu", running_pod_usd=5.0, exp="I1a", ledger=L)
    # running pod dollars also shrink the judge allowance
    spend.record("judge", "gemini", 1, 40.0, exp="I1a", ledger=L)
    with pytest.raises(spend.BudgetExceeded):
        spend.check_total(4.0, "judge", running_pod_usd=5.0, exp="I1a", ledger=L)


def test_i1a_watchdog_kill(tmp_path):
    L = tmp_path / "c.jsonl"
    assert spend.watchdog_kill_usd("I1a", ledger=L) == 40.0
    spend.record("judge", "gemini", 1, 50.0, exp="I1a", ledger=L)
    assert spend.watchdog_kill_usd("I1a", ledger=L) == pytest.approx(33.0)  # 85 - 2 - 50


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_inputs_refused(tmp_path, bad):
    L = tmp_path / "c.jsonl"
    with pytest.raises(ValueError):
        spend.check_total(bad, "judge", exp="I1a", ledger=L)
    with pytest.raises(ValueError):
        spend.check_total(1.0, "judge", running_pod_usd=bad, exp="I1a", ledger=L)


def test_cli_exp_flag(tmp_path, capsys):
    L = tmp_path / "c.jsonl"
    assert spend.main(["--check", "84", "--kind", "judge", "--exp", "I1a", "--ledger", str(L)]) == 2
    assert spend.main(["--check", "83", "--kind", "judge", "--exp", "I1a", "--ledger", str(L)]) == 0
