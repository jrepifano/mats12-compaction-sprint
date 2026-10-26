"""Tests for scripts/s1_chain_label_page.py (no Max calls, no publish)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import s1_chain_label_page as LP


def _row(k, t, i, lost=True):
    return {"cohort": 1, "k": k, "type": t, "usable": True, "lost": lost,
            "group": f"swe-chat-v1:2026-01-0{i % 9}-aaaabbbbccccdddd{i}",
            "chain_item": f"c1:i{i}", "item_id": f"swe-chat-v1:S{i}:0#haiku-max#i{i}",
            "rid": f"swe-chat-v1:S{i}:0@chain-k{k}", "base_rid": f"swe-chat-v1:S{i}:0",
            "summary_rid": f"swe-chat-v1:S{i}:{k - 1}", "text": f"item {i}",
            "quote": f"quote number {i}", "msg": 1, "grade": "dropped",
            "grade_note": "The summary never mentions this item at all, judged dropped."}  # fmt: skip


def _rows():
    out = []
    for k in (2, 3):
        for i in range(40):
            out.append(_row(k, ("rule", "decision", "task_state", "permission")[i % 4], i))
        out.append(_row(k, "commitment", 99))
        out.append(_row(k, "rule", 98, lost=False))
    return out


def test_alloc_is_proportional_with_one_per_type():
    take = LP._alloc({"rule": 200, "decision": 200, "task_state": 230, "permission": 22,
                      "commitment": 26}, 15)  # fmt: skip
    assert sum(take.values()) == 15 and min(take.values()) >= 1
    assert take["task_state"] >= take["permission"]


def test_draw_takes_lost_rows_only_and_records_pi():
    d = LP.draw_lost(_rows(), n=15)
    assert {x["k"] for x in d} == {2, 3}
    assert all(x["row"]["lost"] for x in d)
    for k in (2, 3):
        mine = [x for x in d if x["k"] == k]
        assert len(mine) == 15 and {x["type"] for x in mine} >= {"commitment", "permission"}
        for x in mine:
            assert x["pi"] == x["taken"] / x["pool"]
    assert LP.draw_lost(_rows(), n=15) == d


def _cards():
    rows = _rows()
    d = LP.draw_lost(rows, n=5)
    nc = [{"group": rows[0]["group"], "stretch": 2, "item": "c1:i0", "type": "rule",
           "text": "item 0", "item_quote": "quote number 0", "msg": 1,
           "not_current_quote": "it is done now"}]  # fmt: skip
    chains = {rows[0]["group"]: ["swe-chat-v1:S0:0", "swe-chat-v1:S0:1"]}

    def load(rid):
        return {"id": rid, "source": "swe-chat-v1", "summary": f"Summary for {rid}.",
                "source_messages": [{"role": "user", "text": "first"},
                                    {"role": "assistant", "text": f"see {rid} transcript"}],
                "post_messages": [], "meta": None}  # fmt: skip

    return LP.build_cards(d, nc, chains, load)


def test_cards_carry_no_ids_or_verdicts_after_redaction():
    data = _cards()
    key = data["key"]
    bad = sorted(LP.forbidden(key), key=len, reverse=True)
    sections = LP.redact(data["sections"], bad)
    html = json.dumps(sections)
    LP.leak_check(html, key)  # passes
    assert "dropped" not in html and "judged dropped" not in html
    assert "[session id]" in html
    with pytest.raises(SystemExit, match="leak check failed"):
        LP.leak_check(html + json.dumps(next(iter(key.values()))["item_id"]), key)
    assert len(key) == len(sections["lost"]) + len(sections["not_current"])


def test_render_escapes_script_end():
    data = {"sections": {"lost": [{"id": "c1", "text": "</script><b>x"}], "not_current": []}}
    out = LP.render(data, "d")
    assert "</script><b>x" not in out and "<\\/script>" in out


def test_score_precision_and_agreement():
    key = {"draw": "d", "cards": {
        "a": {"section": "lost", "k": 2, "pi": 0.5}, "b": {"section": "lost", "k": 2, "pi": 0.5},
        "c": {"section": "lost", "k": 3, "pi": 0.25}, "d": {"section": "lost", "k": 3, "pi": 1.0},
        "e": {"section": "not_current", "k": 2}, "f": {"section": "not_current", "k": 3}}}  # fmt: skip
    labels = {"a": {"choice": "lost"}, "b": {"choice": "kept", "note": "it is in the summary"},
              "c": {"choice": "misstated"}, "d": {"choice": "cant_tell"},
              "e": {"choice": "yes"}, "f": {"choice": "no"}, "zz": {"choice": "lost"}}  # fmt: skip
    s = LP.score(labels, key)
    assert s["lost_precision"]["2"]["precision"] == 0.5
    assert s["lost_precision"]["3"]["n"] == 1  # can't tell left out
    assert s["lost_precision"]["pooled"]["hits"] == 2
    assert s["not_current_agreement"]["share"] == 0.5
    assert s["not_on_page"] == ["zz"] and s["notes"][0]["note"] == "it is in the summary"


def test_read_labels_takes_a_dir_of_docs(tmp_path):
    (tmp_path / "cabc.json").write_text(json.dumps({"id": "cabc", "data": {"card": "cabc",
                                                                           "choice": "kept"}}))  # fmt: skip
    assert LP.read_labels(tmp_path) == {"cabc": {"card": "cabc", "choice": "kept"}}


def test_alloc_refuses_fewer_cards_than_types():
    with pytest.raises(ValueError, match="below the number of item types"):
        LP._alloc({"a": 5, "b": 5, "c": 5}, 2)


def test_long_messages_are_kept_whole_with_a_preview():
    m = LP._msg(3, {"role": "tool", "text": "x" * 5000})
    assert len(m["text"]) > 5000 and m["preview"] == LP.MSG_PREVIEW
    huge = LP._msg(0, {"role": "tool", "text": "y" * (LP.MSG_MAX + 10)})
    assert "characters cut" in huge["text"]


def test_score_keeps_note_only_cards_and_drops_other_draws(tmp_path):
    key = {"draw": "d1", "cards": {"a": {"section": "lost", "k": 2, "pi": 1.0},
                                   "b": {"section": "lost", "k": 2, "pi": 1.0},
                                   "c": {"section": "lost", "k": 2, "pi": 1.0}}}  # fmt: skip
    labels = {"a": {"choice": "lost", "draw": "d1"}, "b": {"note": "unsure", "draw": "d1"},
              "c": {"choice": "kept", "draw": "old"}}  # fmt: skip
    s = LP.score(labels, key)
    assert s["counts"]["lost:unanswered"] == 1 and s["notes"][0]["choice"] == "unanswered"
    assert s["wrong_draw"] == ["c"] and s["lost_precision"]["2"]["n"] == 1
    f = tmp_path / "fallback.json"
    f.write_text(
        json.dumps({"draw": "d1", "labels": [{"card": "a", "choice": "lost", "note": ""}]})
    )
    assert LP.read_labels(f) == {"a": {"card": "a", "choice": "lost", "note": "", "draw": "d1"}}


def test_kept_decoys_are_judge_kept_and_scored_apart():
    rows = _rows() + [dict(_row(k, "rule", 200 + i, lost=False), grade="kept")
                      for k in (2, 3) for i in range(6)]  # fmt: skip
    dec = LP.draw_kept(rows, n=5)
    assert len(dec) == 10 and all(d["control"] and d["row"]["grade"] == "kept" for d in dec)
    key = {"draw": "d", "cards": {"a": {"section": "lost", "control": True, "k": 2, "pi": 0.5},
                                  "b": {"section": "lost", "control": False, "k": 2, "pi": 0.5}}}  # fmt: skip
    s = LP.score({"a": {"choice": "kept"}, "b": {"choice": "lost"}}, key)
    assert s["kept_controls"]["2"] == {"n": 1, "jacob_kept": 1}
    assert s["lost_precision"]["2"]["n"] == 1
