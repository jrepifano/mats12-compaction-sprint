"""The note's helper scripts: phase assignment (reconcile), sample clipping, table formatting."""
from p07 import reconcile, samples, tables


def test_phase_of():
    assert reconcile.phase_of("P07|glm-5.3-flash|codex|summary_L1|t|r1|a1").startswith("planned")
    assert reconcile.phase_of("P07|glm-5.3-flash|codex|control|t|r1|a1").startswith("planned")
    assert reconcile.phase_of("P07|glm-5.3-flash|pi_mono|control|t|r1|a1").startswith("follow-up 1")
    assert reconcile.phase_of("P07|glm-5.3-flash|codex|user_P|t|r1|a1").startswith("follow-up 2")
    assert reconcile.phase_of("P07|glm-5.3-flash|codex|tool_P|t|r1|a1").startswith("follow-up 3")
    assert reconcile.phase_of("P07|glm-5.3-flash|claude_code|summary_P|t|r1|a1").startswith(
        "follow-up 3")


def test_clip_is_one_line():
    out = samples._clip("a\n## b\n" + "x" * 500, 50)
    assert "\n" not in out and len(out) == 50 and out.endswith("…")


def test_pct_formats_wilson():
    assert tables.pct(0, 27) == "0/27 (0%, 0–12)"
    assert tables.pct(0, 0) == "—"
