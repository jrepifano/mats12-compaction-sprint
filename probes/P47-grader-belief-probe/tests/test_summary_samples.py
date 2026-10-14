"""Tests for the note helpers: p47.stage_a_summary's bootstrap and p47.samples' code view."""
from __future__ import annotations

from p47 import samples as SA
from p47 import stage_a_summary as SS


def test_boot_point_estimates_and_ci_bracket():
    rows = ([{"task_id": "a", "cheat": True}] * 3 + [{"task_id": "a", "cheat": False}]
            + [{"task_id": "b", "cheat": False}] * 2 + [{"task_id": "c", "cheat": True}])
    draws = SS.boot(rows, "cheat", False, seed=1)
    teq = SS.boot(rows, "cheat", True, seed=1)
    assert draws["est"] == round(4 / 7, 4)
    assert teq["est"] == round((0.75 + 0 + 1) / 3, 4)
    for b in (draws, teq):
        assert 0 <= b["lo"] <= b["est"] <= b["hi"] <= 1


def test_boot_is_seeded():
    rows = [{"task_id": str(i % 5), "cheat": i % 3 == 0} for i in range(30)]
    assert SS.boot(rows, "cheat", True, seed=7) == SS.boot(rows, "cheat", True, seed=7)


def test_code_lines_drops_docstrings_and_blank_lines():
    src = '"""Module\n\ndoc."""\nimport x\n\ndef f(a):\n    """One-line doc."""\n' \
          '    if a == 3:\n        return 5\n    """multi\n    line"""\n    return a\n'
    assert SA.code_lines(src) == "import x\ndef f(a):\n    if a == 3:\n        return 5\n" \
                                 "    return a"
    assert SA.code_lines(src, 2) == "import x\ndef f(a):"
    assert SA.code_lines(None) == ""


def test_stage_b_projection_counts_asks_luna_and_new_qwen_sites():
    from p47 import gate as G

    # gpt-oss: 10 sites topped from 2 to 6 draws = 40 continuations
    got = G.stage_b_usd("gpt-oss-20b", 10, 0.01, 0.005, 2, 6, 8)
    want = 40 * 0.01 + 10 * 2 * 8 * 0.003 + (40 + 10) * G.LUNA_USD
    assert abs(got - want) < 1e-9
    # Qwen: 11 sites topped to 4 (22) plus 15 new sites x 4 (60) and their prefixes
    got = G.stage_b_usd("qwen3.6-27b", 11, 0.05, 0.14, 2, 4, 8)
    want = 82 * 0.05 + 26 * 2 * 8 * 0.02 + (82 + 26) * G.LUNA_USD + 15 * 0.14
    assert abs(got - want) < 1e-9
