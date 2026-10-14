"""P47 luna-tag parsing and text renders, and the local chat-template renders (no network:
the template tests skip unless the pinned tokenizer is already in the HF cache)."""
from __future__ import annotations

import json

import pytest
from p47 import render as RD
from p47 import tag as T

from tests.test_core import PREFIX, _asst, _tool


def test_parse_tag():
    ok = T.parse_tag('{"statements": [{"turn": 3, "label": "endorsed_hidden", "quote": "q"}]}')
    assert ok["talk"] and ok["labels"] == ["endorsed_hidden"] and ok["statements"][0]["turn"] == 3
    assert T.parse_tag('```json\n{"statements": []}\n```') == {"statements": [], "talk": False,
                                                                 "labels": []}
    assert T.parse_tag('{"statements": [{"turn": 1, "label": "maybe"}]}') is None
    assert T.parse_tag("no json") is None
    assert T.parse_tag('{"statements": [{"turn": "x", "label": "hedged"}]}') is None


def test_turns_text_numbers_from_first_turn():
    t = T.turns_text(PREFIX, first_turn=5)
    assert t.startswith("[turn 5]") and "[turn 6]" in t and "(command)" in t
    assert "out2" not in t                                     # tool outputs left out


def test_transcript_text_clips_outputs():
    long = PREFIX[:2] + [_asst("r"), _tool("x" * (T.OUTPUT_CLIP + 10))]
    t = T.transcript_text(long)
    assert "AGENT REASONING:\nr" in t and "[... 10 more characters]" in t


def test_hf_messages_fields():
    q = RD.hf_messages(PREFIX, "qwen3.6-27b")
    assert q[2]["reasoning_content"] == "First look." and q[2]["tool_calls"][0]["function"][
        "arguments"] == {"command": "ls"}
    assert q[3] == {"role": "tool", "content": "out", "tool_call_id": "c1",
                    "name": "execute_command"}
    g = RD.hf_messages(PREFIX, "gpt-oss-120b")
    assert g[2]["thinking"] == "First look." and g[2]["content"] == ""


def test_turn_requests_pairs_calls_with_messages():
    art = {"messages": [_asst("a"), _tool(), _asst("b", cmd=None)],
           "model_calls": [{"input_tokens": 10}, {"input_tokens": 20}]}
    reqs = RD.turn_requests(PREFIX, art)
    assert [len(m) for m, _ in reqs] == [len(PREFIX), len(PREFIX) + 2]
    assert [n for _, n in reqs] == [10, 20]


def _cached(slug):
    try:
        return RD.tokenizer(slug)
    except Exception:  # noqa: BLE001 - not in the cache / offline
        return None


@pytest.mark.parametrize("slug", ["qwen3.6-27b", "gpt-oss-120b"])
def test_open_reasoning_reads_statement_end(slug, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    tok = _cached(slug)
    if tok is None:
        pytest.skip("tokenizer not cached")
    stmt = "The grading here only runs the provided tests."
    ids, end = RD.render_open_reasoning(tok, PREFIX, slug, stmt)
    assert end == len(ids) - 1
    tail = tok.decode(ids[-12:])
    assert tail.endswith("provided tests.")
    base = RD.render_ids(tok, PREFIX, slug)
    assert ids[: len(base)] == base                            # the statement extends the boundary
    assert json.dumps(RD.tools_for(slug))                      # serialisable
