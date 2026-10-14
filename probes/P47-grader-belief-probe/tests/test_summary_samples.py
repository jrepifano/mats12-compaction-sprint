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
