"""p05.shortcut helpers: one-step values and token-to-value mapping."""

from __future__ import annotations

from p05 import shortcut as SC


def test_one_step_values_first_and_last_op():
    # 14 even -> halve -> 7 (first op); the last op on 14 is the same rule, also 7.
    it = {"start": 14, "ops": [["evenhalve", 7], ["evenhalve", 9]]}
    assert SC.one_step_values(it) == {7}
    # 7 odd: first op adds 7 -> 14; last op adds 9 -> 16.
    it = {"start": 7, "ops": [["evenhalve", 7], ["evenhalve", 9]]}
    assert SC.one_step_values(it) == {14, 16}


def test_value_of():
    cand = list(range(100, 120))
    assert SC.value_of([220, 100], cand) == 1
    assert SC.value_of([220, 119], cand) == 20
    assert SC.value_of([220, 5], cand) is None
