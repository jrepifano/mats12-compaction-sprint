"""The Flash summarizer swap's slot replacement and cap (scripts/week2_summary_swap.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import week2_summary_swap as S


def test_swap_previous_replaces_the_one_previous_summary():
    user = "<conversation>x</conversation>\n\n<previous-summary>\nOLD\n</previous-summary>\n\nP"
    assert S.swap_previous(user, "OLD", "NEW") == user.replace("OLD", "NEW")
    with pytest.raises(ValueError):
        S.swap_previous(user, "MISSING", "NEW")


def test_merge_matches_pi_with_and_without_history():
    info = {"split": True, "has_history": True}
    assert S.merge(info, {"history": "H", "turn_prefix": "T"}, None) == f"H{S.TURN}T"
    info = {"split": True, "has_history": False}
    assert S.merge(info, {"turn_prefix": "T"}, None) == f"No prior history.{S.TURN}T"
    assert S.merge(info, {"turn_prefix": "T"}, "P") == f"P{S.TURN}T"


def test_cap_counts_recorded_spend_and_open_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(S, "CAP_USD", 1.0)
    sp = S.CapSpend(tmp_path / "raw")
    a = sp.reserve(0.6, "a")
    with pytest.raises(S.M.ModelError):
        sp.reserve(0.5, "b")  # 0.6 held + 0.5 > 1.0
    sp.settle(a, 0.3, resource="r", units=1, note="n")
    assert S.spend.spent(exp=S.EXP) == 0.3
    sp.reserve(0.7, "c")  # 0.3 spent + 0.7 = 1.0 fits
    with pytest.raises(S.M.ModelError):
        sp.reserve(0.01, "d")


def test_a_reservation_left_open_by_a_crash_is_charged_at_its_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "reservations.jsonl").write_text('{"op": "reserve", "rid": "x", "usd": 0.4, "note": "n"}\n')
    S.CapSpend(raw)
    assert S.spend.spent(exp=S.EXP) == 0.4
