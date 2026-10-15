"""P16 judge prompts and parsing (no calls)."""

from __future__ import annotations

from p16 import judge as J


def test_parse_j1_j3_and_j2():
    t = 'The numbers 270 and 361 stand out.\n{"n1": 270, "confidence_n1": 0.8, "backups": [361, 270, "12", 4.0]}'
    assert J.parse(t, "J1") == [270, 361, 12, 4]
    assert J.parse('think\n{"numbers": [5, 6, 5]}', "J2") == [5, 6]
    assert J.parse('{"n1": null, "confidence_n1": 0, "backups": []}', "J1") == []
    assert J.parse("no json here", "J1") is None and J.parse(None, "J1") is None
    # the last parseable object wins; a broken trailing one is skipped
    assert J.parse('{"n1": 1, "backups": []} then {"n1": 2, "backups": [3]} {bad', "J3") == [2, 3]


def test_prompts_and_list_rendering():
    assert "What specific number" in J.SYSTEMS["J1"] and "decoy" not in J.SYSTEMS["J1"]
    assert "decoy number" in J.SYSTEMS["J3"] and J.SYSTEMS["J3"].startswith(J._INTRO)
    assert '"numbers"' in J.SYSTEMS["J2"]
    s = J.render_list([(5, 37.2751), (7, 0.5)], lambda t: {5: "硼", 7: " 42"}[t])
    assert s.splitlines() == ["Top tokens (rank. score      'string'):", "1. 37.275 '硼'",
                              "2. 0.500 ' 42'"]
