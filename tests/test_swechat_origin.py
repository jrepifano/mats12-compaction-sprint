"""Unit tests for scripts/swechat_origin.py (no calls, no data)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import swechat_origin as G

MSGS = [
    {"role": "user", "text": "please fix the parser and run the tests"},
    {"role": "assistant", "text": "Fixed the parser. All tests pass now."},
    {"role": "tool", "text": "FAILED tests/test_parser.py::test_x - AssertionError"},
    {"role": "assistant", "text": "Done: the parser fix is complete and tests pass.",
     "tool_calls": [{"name": "Bash", "args": "echo the release is shipped"}]},
]  # fmt: skip
ROW = {"rid": "src:a:0", "cid": "C1", "attempt": "x", "line": "- all tests pass",
       "rc_msg": 2, "rc_quote": "FAILED tests/test_parser.py", "rc_reason": "r", "rfp": "f"}  # fmt: skip


def resp(**claim):
    c = {"id": "C1", "assertion": "tests pass", "reason": "r"} | claim
    return SimpleNamespace(error=None, data={"claims": [c]})


def test_agent_quote_checked_in_the_cited_message_and_timed():
    (o,) = G.parse(resp(source="agent_same", quote="tests pass", msg=3), [ROW], MSGS)
    assert o["final"] == "agent_same" and o["located"] == 3 and o["when"] == "after"
    (o,) = G.parse(resp(source="agent_same", quote="All tests pass now.", msg=1), [ROW], MSGS)
    assert o["located"] == 1 and o["when"] == "before"


def test_wrong_index_wrong_role_and_tool_call_quotes_are_cant_tell():
    (o,) = G.parse(resp(source="agent_same", quote="All tests pass now.", msg=3), [ROW], MSGS)
    assert o["final"] == "cant_tell" and o["located"] is None and o["when"] is None
    q = "FAILED tests/test_parser.py"
    (o,) = G.parse(resp(source="agent_same", quote=q, msg=2), [ROW], MSGS)
    assert o["final"] == "cant_tell"
    (o,) = G.parse(resp(source="agent_same", quote="the release is shipped", msg=3), [ROW], MSGS)
    assert o["final"] == "cant_tell"  # tool-call arguments are not assertions
    (o,) = G.parse(resp(source="user_or_tool", quote=q, msg=2), [ROW], MSGS)
    assert o["final"] == "user_or_tool" and o["when"] is None
    (o,) = G.parse(resp(source="agent_same", quote="tests pass", msg=True), [ROW], MSGS)
    assert o["final"] == "cant_tell"


def test_unverified_contradiction_gives_unknown_timing():
    row = ROW | {"rc_msg": 1}  # the recheck's quote is not in m1
    (o,) = G.parse(resp(source="agent_same", quote="tests pass", msg=3), [row], MSGS)
    assert o["final"] == "agent_same" and o["when"] == "unknown"


def test_none_unknown_label_and_failures():
    (o,) = G.parse(resp(source="none", quote=None, msg=None), [ROW], MSGS)
    assert o["final"] == "none"
    (o,) = G.parse(resp(source="bogus", quote=None, msg=None), [ROW], MSGS)
    assert o["final"] == "cant_tell"
    (o,) = G.parse(SimpleNamespace(error="timeout", data=None), [ROW], MSGS)
    assert o["final"] is None and o["error"] == "timeout"
    (o,) = G.parse(SimpleNamespace(error=None, data={"claims": []}), [ROW], MSGS)
    assert o["final"] == "cant_tell"


def test_done_ignores_answers_on_changed_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "OUT", tmp_path / "origin.jsonl")
    (o,) = G.parse(resp(source="none", quote=None, msg=None), [ROW], MSGS)
    G.OUT.write_text(json.dumps(o) + "\n")
    assert list(G.done([ROW])) == [("src:a:0", "C1", "x")]
    assert G.done([ROW | {"rc_quote": "another failure"}]) == {}


def test_run_restores_the_scan_state_path(monkeypatch):
    before = G.S.STATE
    seen = {}

    def boom(groups):
        seen["state"] = G.S.STATE
        raise RuntimeError("stop")

    monkeypatch.setattr(G, "false_lines", lambda: [ROW])
    monkeypatch.setattr(G, "done", lambda rows: {})
    monkeypatch.setattr(G, "_run", boom)
    with pytest.raises(RuntimeError):
        G.cmd_run(SimpleNamespace(limit=None))
    assert seen["state"] == G.STATE and G.S.STATE == before


def test_header_text_alone_is_not_a_quote():
    msgs = MSGS + [{"role": "assistant", "text": ""}]
    (o,) = G.parse(resp(source="agent_same", quote="[m4] ASSISTANT:", msg=4), [ROW], msgs)
    assert o["final"] == "cant_tell"
