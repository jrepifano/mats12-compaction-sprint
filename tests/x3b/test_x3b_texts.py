"""U0: X3b header and prompt invariants (implementation plan §1; prereg §4.3, §4.5, §4.9)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from x3 import texts as X3T
from x3.contracts import Context, PromptLevel
from x3b import contracts as C
from x3b import texts

ROOT = Path(__file__).resolve().parents[2]
PREREG = ROOT / "docs" / "experiments" / "X3b" / "prereg.md"
ROW_NAMES = {
    "Neutral": C.Header.NEUTRAL,
    "Scoring and results only": C.Header.SCORING_RESULTS,
    "Capability": C.Header.CAPABILITY,
    "Safety": C.Header.SAFETY,
    "Safety label only": C.Header.SAFETY_LABEL_ONLY,
    "Behavior review only": C.Header.BEHAVIOR_REVIEW_ONLY,
}


def _prereg_headers() -> dict[C.Header, tuple[str, str, str]]:
    """Lines 2-4 of each header from the prereg §4.3 table; "same ..." cells take the scoring
    and results lines, as the table says."""
    text = PREREG.read_text()
    sec = text[text.index("### 4.3 The run headers"):text.index("### 4.4 Outcomes")]
    rows = {}
    for line in sec.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] in ROW_NAMES and len(cells) >= 4:
            rows[ROW_NAMES[cells[0]]] = cells[1:4]
    assert set(rows) == set(C.Header), "every header has a row in prereg §4.3"
    shared = [re.fullmatch(r"`(.*)`", c).group(1) for c in rows[C.Header.SCORING_RESULTS][1:]]
    out = {}
    for h, cells in rows.items():
        lines = []
        for i, c in enumerate(cells):
            m = re.fullmatch(r"`(.*)`", c)
            lines.append(m.group(1) if m else shared[i - 1])
        out[h] = tuple(lines)
    return out


def test_headers_match_prereg_character_for_character():
    want = _prereg_headers()
    for h in C.Header:
        assert texts.HEADERS[h][0] == X3T.RUN_ID
        assert texts.HEADERS[h][1:] == want[h], h


def test_anchors_are_x3_verbatim():
    assert texts.HEADERS[C.Header.NEUTRAL] == X3T.HEADERS[Context.NEUTRAL]
    assert texts.HEADERS[C.Header.CAPABILITY] == X3T.HEADERS[Context.CAP_SCRIPT]
    assert texts.HEADERS[C.Header.SAFETY] == X3T.HEADERS[Context.SAFE_SCRIPT]


def test_prompts_differ_only_inside_the_header():
    bodies = {h: texts.user_prompt(h) for h in C.Header}
    tails = {b.split("\n\n", 1)[1] for b in bodies.values()}
    assert len(tails) == 1, "task text must be identical across headers"
    for h, b in bodies.items():
        assert b.startswith(texts.header_text(h) + "\n\n")
        assert len(texts.HEADERS[h]) == 4
    # and equal to X3's rendering of the same header text at level B
    assert bodies[C.Header.CAPABILITY] == X3T.user_prompt(Context.CAP_SCRIPT, PromptLevel.B)


def test_each_comparison_changes_what_the_prereg_says():
    h = texts.HEADERS

    def diff(a, b):
        return [i for i, (x, y) in enumerate(zip(h[a], h[b], strict=True)) if x != y]

    H = C.Header
    assert diff(H.CAPABILITY, H.SAFETY_LABEL_ONLY) == [1]          # purpose label alone
    assert diff(H.CAPABILITY, H.BEHAVIOR_REVIEW_ONLY) == [1]       # suite name alone
    assert diff(H.NEUTRAL, H.SCORING_RESULTS) == [2, 3]            # scoring + results lines
    assert diff(H.CAPABILITY, H.SCORING_RESULTS) == [1]            # whole suite line
    assert diff(H.CAPABILITY, H.SAFETY) == [1]                     # X3's contrast
    # the label swap changes only the parenthesis, the name swap only the name
    cap, lab, beh = h[H.CAPABILITY][1], h[H.SAFETY_LABEL_ONLY][1], h[H.BEHAVIOR_REVIEW_ONLY][1]
    assert cap.split(" (")[0] == lab.split(" (")[0] and cap.split(" (")[1] != lab.split(" (")[1]
    assert cap.split(" (")[1] == beh.split(" (")[1] and cap.split(" (")[0] != beh.split(" (")[0]


def test_stage_headers_and_contrasts_are_consistent():
    assert set(C.STAGE_HEADERS[C.StageKey.GLM]) == set(C.Header)
    assert C.STAGE_HEADERS[C.StageKey.GEMINI] == (C.Header.CAPABILITY, C.Header.SAFETY)
    for c in C.CONTRASTS:
        assert c.a in C.STAGE_HEADERS[c.stage] and c.b in C.STAGE_HEADERS[c.stage]
    fam = {}
    for c in C.CONTRASTS:
        fam.setdefault((c.stage, c.family), []).append(c.name)
    assert len(fam[(C.StageKey.GLM, "primary")]) == 3
    assert len(fam[(C.StageKey.GLM, "secondary")]) == 2
    assert fam[(C.StageKey.GEMINI, "primary")] == ["cap_vs_safety"]


def test_block_reservations_match_limits():
    for st, m in C.MODELS.items():
        n = len(C.STAGE_HEADERS[st])
        assert abs(m.block_reservation_usd - (m.episode_limit_usd + m.probe_bound_usd) * n) < 1e-9


def test_pre_probe_prompt_is_prereg_verbatim():
    text = PREREG.read_text()
    sec = text[text.index("### 4.5 How the model"):text.index("### 4.6 Design")]
    block = re.search(r"```\n(.*?)\n```", sec, re.DOTALL).group(1)
    assert texts.PRE_PROBE_PROMPT == block


def test_post_probe_is_x3_plus_q4():
    assert texts.POST_PROBE_PROMPT.startswith(X3T.PROBE_PROMPT + "\n")
    q4 = texts.POST_PROBE_PROMPT.splitlines()[-1]
    pre_q2 = texts.PRE_PROBE_PROMPT.splitlines()[-1]
    assert q4.startswith("Q4. ") and pre_q2.startswith("Q2. ")
    assert q4[4:].split(" (a)")[0] == pre_q2[4:].split(" (a)")[0]     # same question stem


def test_episode_ids_have_x3_shape():
    eid = C.episode_id(C.StageKey.GLM, C.RunKind.MAIN, C.Header.SAFETY_LABEL_ONLY, "t1", 3, 1)
    assert len(eid.split("|")) == 7 and eid.endswith("|r3|a1")
    p = C.episode_path(C.StageKey.GEMINI, C.RunKind.SMOKE, C.Header.SAFETY, "t1", 1, 2)
    assert p.relative_to(C.RESULTS_DIR).parts == ("gemini", "episodes", "smoke", "safety", "t1",
                                                  "r1_a2.json")


def test_prompt_manifest_and_disclosure_is_x3_verbatim():
    d = C.PROMPTS_DIR
    m = json.loads((d / "MANIFEST.json").read_text())
    for k, v in m.items():
        assert hashlib.sha256((d / v["file"]).read_bytes()).hexdigest() == v["sha256"], k
    assert (d / "disclosure.txt").read_bytes() == (ROOT / "prompts/X3/disclosure.txt").read_bytes()
