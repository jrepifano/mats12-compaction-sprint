"""p01.common: cut points, test-output classification, truth and checklist."""

from __future__ import annotations

import pytest

from p01 import common as C


def msg(role, text="", calls=None, call_id=None):
    return {"role": role, "text": text, "tool_calls": calls or [], "tool_call_id": call_id,
            "tool_name": None}


def step(k, cmd, out, tool="execute_bash"):
    cid = f"c{k}"
    return [msg("assistant", f"step {k}", [{"id": cid, "name": tool, "args": {"command": cmd}}]),
            msg("tool", out, call_id=cid)]


def transcript(steps):
    m = [msg("system", "sys"), msg("user", "task")]
    for k, (cmd, out) in enumerate(steps):
        m += step(k, cmd, out)
    return m


def test_cut_window_and_determinism():
    m = transcript([("ls", "x")] * 10)
    assert C.turn_window(10) == (5, 9)
    cut, turn = C.choose_cut(m, "A:t1")
    assert (cut, turn) == C.choose_cut(m, "A:t1")
    assert 5 <= turn <= 9
    assert m[cut - 1]["role"] == "tool" and C.cut_after_turn(m, turn) == cut
    assert C.cut_after_turn(m, 10) == len(m)
    _, t2 = C.choose_cut(m, "A:t1", min_turn=9)
    assert t2 == 9
    with pytest.raises(ValueError):
        C.choose_cut(m, "A:t1", min_turn=10)


@pytest.mark.parametrize("out,status", [
    ("===== 3 passed in 0.12s =====", "pass"),
    ("== 1 failed, 2 passed in 1.0s ==", "fail"),
    ("2 passed, 1 error in 3.1s", "fail"),
    ("....\nRan 4 tests in 0.01s\n\nOK", "pass"),
    ("Ran 4 tests in 0.01s\n\nFAILED (failures=1)", "fail"),
    ("Traceback (most recent call last):\n  File x\nValueError: bad", "fail"),
    ("All tests passed!", "pass"),
    ("hello world", "unknown"),
    ("command timed out after 60s\n", "unknown"),
])
def test_classify(out, status):
    assert C.classify_test_output(out)[0] == status


def test_is_test_command():
    assert C.is_test_command("cd /repo && python -m pytest tests/test_x.py -q")
    assert C.is_test_command("python test_fix.py")
    assert not C.is_test_command("python reproduce_issue.py")
    assert not C.is_test_command("ls tests/")


def test_truth_and_checklist():
    m = transcript([("python -m pytest", "== 1 failed in 0.1s ==\nE   AssertionError: 1 != 2"),
                    ("cat x.py", "ok"),
                    ("python -m pytest", "Ran 1 test in 0s\n\nOK"),
                    ("python -m pytest", "weird output")])
    tr = C.prefix_truth(m)
    assert tr["last_test"]["status"] == "pass" and tr["n_test_runs"] == 3
    assert tr["last_error"]["text"] == "AssertionError: 1 != 2" and tr["last_error"]["age"] == 3
    items = C.base_checklist("Fix bug", "Don't edit tests", ["a.py", "b.py"], tr)
    ids = [i["id"] for i in items]
    assert ids == ["task", "constraint", "file1", "file2", "last_error", "test_status"]
    none = C.prefix_truth(transcript([("ls", "x")]))
    assert none["last_test"]["status"] == "none" and none["last_error"] is None
    unk = C.prefix_truth(transcript([("pytest", "hmm")]))
    assert unk["last_test"]["status"] == "unknown"


def test_validate():
    m = transcript([("ls", "x")] * 4)
    t = {"tid": "A:x", "slice": "A", "source": {}, "messages": m, "cut": C.cut_after_turn(m, 2),
         "cut_turn": 2, "n_turns": 4, "truth": {},
         "checklist": [{"id": "task", "kind": "task", "text": "t"},
                       {"id": "constraint", "kind": "constraint", "text": "c"}]}
    C.validate_transcript(t)
    with pytest.raises(ValueError):
        C.validate_transcript({**t, "cut": 3})
