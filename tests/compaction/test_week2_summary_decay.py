"""retention_by_age with chains cut short (scripts/week2_summary_decay.py)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import week2_summary_decay as D


def _rows(chain: str, boundaries: int, kept: dict[tuple[int, int], int]) -> list[dict]:
    """One entry per task at each boundary it is current, plus `kept[(task, boundary)]` later."""
    rows = []
    for k in range(1, boundaries + 1):
        rows += [{"chain": chain, "boundary": k, "final_label": f"task_{k}", "safe": True}] * 2
        for j in range(1, k):
            rows += [{"chain": chain, "boundary": k, "final_label": f"task_{j}",
                      "safe": True}] * kept.get((j, k), 0)
    return rows


def test_a_cut_chain_drops_its_later_pairs_instead_of_counting_them_lost():
    rows = _rows("a", 3, {(1, 3): 1}) + _rows("b", 2, {})
    ret = D.retention_by_age(rows)
    two = ret["2:safety"]
    # only chain a has boundaries 1 and 3: one pair, 1 of 2 entries kept
    assert two["n_pairs"] == 1 and two["entries_at_age0"] == 2 and two["retention"] == 0.5
    assert ret["1:safety"]["n_pairs"] == 3  # a: (1,2), (2,3); b: (1,2)


def test_an_age_with_no_eligible_pair_is_undefined_not_an_error():
    ret = D.retention_by_age(_rows("a", 2, {}) + _rows("b", 2, {}))
    for d in (2, 3):
        c = ret[f"{d}:safety"]
        assert c["retention"] is None and c["ci95"] == [None, None] and c["n_pairs"] == 0
    assert ret["1:safety"]["retention"] == 0.0
