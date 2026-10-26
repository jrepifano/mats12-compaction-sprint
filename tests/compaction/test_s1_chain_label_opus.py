"""Tests for scripts/s1_chain_label_opus.py (no Max calls)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import s1_chain_label_opus as LO
import s1_chain_label_page as LP


def _card(cid, sec="lost", k=2):
    c = {"id": cid, "section": sec, "k": k, "type": "rule", "text": "Use tabs.",
         "quote": "always use tabs please", "context": [{"role": "user", "text": "[m3] USER: always use tabs please"}]}  # fmt: skip
    if sec == "lost":
        c |= {"summary": "The user prefers spaces.", "kept": []}
    return c


def test_request_carries_the_card_and_question_only():
    req = LO.request(_card("c1"), "swe-chat-v1:S:0")
    assert "always use tabs please" in req.user and "The user prefers spaces." in req.user
    assert "missing or misstated in what the agent sees after compaction 2?" in req.user
    assert "judge" not in req.user.lower() and "dropped" not in req.user
    assert req.schema["properties"]["choice"]["enum"] == ["lost", "misstated", "kept", "cant_tell"]
    nc = LO.request(_card("c2", "not_current", 3), "r")
    assert "no longer relevant by compaction 3?" in nc.user and "Summary" not in nc.user


def test_page_cards_round_trip(tmp_path):
    data = {"sections": {"lost": [_card("c1") | {"summary": "a </script> b <!-- c"}],
                         "not_current": [_card("c2", "not_current")]}}  # fmt: skip
    for c in data["sections"]["lost"] + data["sections"]["not_current"]:
        c.pop("section")
    p = tmp_path / "page.html"
    p.write_text(LP.render(data, "d1"))
    draw, cards = LO.page_cards(p)
    assert draw == "d1" and cards["c1"]["summary"] == "a </script> b <!-- c"
    assert cards["c2"]["section"] == "not_current"


def test_notes_quoting_the_card_are_replaced():
    c = _card("c1") | {"context": [{"role": "assistant", "text": "x" * 10 + "The test IS catching the bug - the BaseCommit changed"}]}  # fmt: skip
    assert LO.note_for_commit("in messages", c) == "in messages"
    assert LO.note_for_commit("The test IS catching the bug - the BaseCommit changed", c) == (
        "[quotes the card's messages]"
    )


def test_score_bins_misstated_as_lost_and_drops_cant_tell():
    cards = {i: _card(i) for i in ("a", "b", "c", "d")} | {"e": _card("e", "not_current")}
    key = {"a": {"section": "lost", "k": 2, "judge_grade": "dropped"},
           "b": {"section": "lost", "k": 2, "judge_grade": "dropped"},
           "c": {"section": "lost", "k": 3, "judge_grade": "misstated"},
           "d": {"section": "lost", "k": 2, "control": True, "judge_grade": "kept"},
           "e": {"section": "not_current", "k": 2}}  # fmt: skip
    jacob = {"a": {"choice": "lost"}, "b": {"choice": "kept"}, "c": {"choice": "cant_tell"},
             "d": {"choice": "lost"}, "e": {"choice": "yes"}}  # fmt: skip
    opus = {"a": {"choice": "misstated"}, "b": {"choice": "kept"}, "c": {"choice": "lost"},
            "d": {"choice": "kept"}, "e": {"choice": "no"}}  # fmt: skip
    s = LO.score(cards, key, jacob, opus)
    assert s["agreement"]["section1_judge_lost"]["hits"] == 2  # a (misstated = lost), b
    assert s["agreement"]["section1_judge_lost"]["n"] == 2  # c left out (Jacob can't tell)
    assert (
        s["opus_lost_precision"]["2"] == LO._share(1, 2)
        and s["opus_lost_precision"]["3"]["hits"] == 1
    )
    assert s["opus_on_decoys"] == {"kept": 1} and s["agreement"]["section2"]["hits"] == 0
    assert s["jacob_cant_tell"] == [{"card": "c", "section": "lost", "opus": "lost"}]
    json.dumps(s)
