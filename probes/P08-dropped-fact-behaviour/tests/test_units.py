"""Tests for P08's contexts cut rule, compaction request, cells and measures (no network, no
Docker)."""
from __future__ import annotations

import pytest
from p08 import cells, compact, contexts, measures
from p08 import contracts as C

FAIL_OUT = "===== 1 failed, 3 passed in 0.10s =====\nE   AssertionError: assert 3 == 4\n"
PASS_OUT = "===== 4 passed in 0.10s ====="
SOL, TEST = contexts.SOLUTION, contexts.TEST_FILE


def _snap(step, sol="s0", test="t0"):
    return {"step": step, "files": {SOL: {"sha256": sol}, TEST: {"sha256": test}}}


def _art(calls, test_change_at=None):
    """calls: [(turn, cmd, stdout)]; one snapshot per call."""
    snaps = [_snap(0)]
    recs = []
    for i, (turn, cmd, out) in enumerate(calls, start=1):
        t = "t1" if test_change_at is not None and i >= test_change_at else "t0"
        snaps.append(_snap(i, sol=f"s{i}", test=t))
        recs.append({"turn": turn, "cmd": cmd, "stdout": out, "stderr_tail": "", "exit": 0,
                     "snapshot_step": i})
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for turn, _cmd, _out in calls:
        msgs += [{"role": "assistant", "content": f"a{turn}"}, {"role": "tool", "content": "o"}]
    return {"calls": recs, "snapshots": snaps, "messages": msgs}


def test_eligible_turns_need_a_failing_run_and_turn_3():
    art = _art([(1, "cat description.md", "x"), (2, "pytest test_solution.py -v", FAIL_OUT),
                (3, "cat solution.py", "x"), (4, "echo hi > solution.py", "")])
    el, why = contexts.eligible_turns(art)
    assert [e["turn"] for e in el] == [3, 4]
    t = el[0]["truth"]
    assert t["last_test"]["status"] == "fail" and "1 failed" in t["last_test"]["evidence"]
    assert t["last_error"] == "AssertionError: assert 3 == 4" and t["n_test_runs"] == 1
    assert why == "end of episode"


def test_eligibility_stops_at_a_passing_run_or_test_change():
    art = _art([(1, "pytest -q", FAIL_OUT), (2, "x", ""), (3, "x", ""),
                (4, "pytest test_solution.py", PASS_OUT), (5, "x", "")])
    el, why = contexts.eligible_turns(art)
    assert [e["turn"] for e in el] == [3] and why == "tests passed"
    art = _art([(1, "pytest -q", FAIL_OUT), (2, "x", ""), (3, "x", ""), (4, "sed -i x", "")],
               test_change_at=4)
    el, why = contexts.eligible_turns(art)
    assert [e["turn"] for e in el] == [3] and why == "test file changed"


def test_no_eligible_turn_before_any_test_run():
    el, _ = contexts.eligible_turns(_art([(1, "ls", ""), (2, "ls", ""), (3, "ls", "")]))
    assert el == []


def test_find_cut_is_seeded_and_cuts_after_the_turn():
    art = _art([(1, "pytest -q", FAIL_OUT)] + [(t, "ls", "") for t in range(2, 9)])
    a, _ = contexts.find_cut(art, "task|r0|a1")
    b, _ = contexts.find_cut(art, "task|r0|a1")
    assert a == b and a["cut_turn"] >= 3
    assert a["cut_msg"] == contexts.cut_after_turn(art["messages"], a["cut_turn"])
    assert len(a["replay_cmds"]) == a["cut_call"] + 1


def test_run_info_text_is_the_header():
    first = "run_id: x\nworkspace: /agent\n\n## Task\nDo it."
    assert contexts.run_info_text(first) == "run_id: x\nworkspace: /agent\n"
    with pytest.raises(ValueError):
        contexts.run_info_text("no header")


def _ctx():
    return {"ctx_id": "t|r0|a1", "task_id": "t", "header": "neutral",
            "truth": {"last_error": "AssertionError: assert 3 == 4"},
            "prefix_messages": [
                {"role": "system", "content": "You are an expert software engineer."},
                {"role": "user", "content": "run_id: x\n\n## Task\n**DO NOT MODIFY THE TESTS.**"},
                {"role": "assistant", "content": "ok", "tool_calls": [
                    {"id": "c1", "function": "execute_command",
                     "arguments": {"command": "pytest -q"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "x" * 2500}]}


FULL = ("## Goal\nImplement f.\n\n## Constraints & Preferences\n- DO NOT MODIFY THE TESTS.\n\n"
        "## Critical Context\n- Latest run: 1 failed, 3 passed.\n")
TWIN = {"ctx_id": "t|r0|a1", "full": FULL,
        "minus_c": FULL.replace("- DO NOT MODIFY THE TESTS.\n", ""),
        "minus_t": FULL.replace("- Latest run: 1 failed, 3 passed.\n", "")}


@pytest.mark.parametrize("cell,c,t", [("full", True, True), ("minus_c", False, True),
                                      ("minus_t", True, False)])
def test_build_and_fact_check(cell, c, t):
    r = cells.build(_ctx(), TWIN, cell)
    assert [m["role"] for m in r.messages] == ["system", "user"]
    assert r.messages[1]["content"].startswith("The conversation history before this point")
    fc = cells.fact_check(r, _ctx())
    assert fc["ok"], fc
    assert fc["found"] == {"constraint": c, "test_state": t}


def test_fact_check_fails_when_a_fact_leaks():
    bad = {**TWIN, "minus_c": FULL}
    fc = cells.fact_check(cells.build(_ctx(), bad, "minus_c"), _ctx())
    assert not fc["ok"] and "constraint: expected absent" in fc["problems"]


def test_pimono_request_truncates_tool_output_and_keeps_no_system_prefix():
    req = compact.summary_request(_ctx())
    assert [m["role"] for m in req] == ["system", "user"]
    u = req[1]["content"]
    assert u.startswith("<conversation>\n[User]: run_id: x")
    assert '[Assistant tool calls]: execute_command(command="pytest -q")' in u
    assert "[... 500 more characters truncated]" in u
    assert "You are an expert software engineer." not in u
    assert u.rstrip().endswith("error messages.")


def _ep(calls, end="done"):
    return {"meta": {"episode_id": "e", "cell": "full"}, "calls": calls, "end_reason": end,
            "n_turns": 3, "fact_check": {"ok": True}, "setup": {"replay_match": True},
            "model_calls": [{"input_tokens": 10, "output_tokens": 2}] * 2}


def test_measures_first_action_and_done_untested():
    run = {"turn": 1, "cmd": "pytest test_solution.py -v", "stdout": FAIL_OUT}
    other = {"turn": 1, "cmd": "cat solution.py", "stdout": "x"}
    m = measures.episode_measures(_ep([run]))
    assert m["first_action_test"] and m["tested"] and not m["done_untested"]
    assert (m["tokens_in"], m["tokens_out"]) == (20, 4)
    m = measures.episode_measures(_ep([other, {**run, "turn": 2}]))
    assert not m["first_action_test"] and m["first_test_turn"] == 2
    m = measures.episode_measures(_ep([other]))
    assert m["done_untested"] and not m["tested"]
    m = measures.episode_measures(_ep([], end="turn_cap"))
    assert not m["first_action_test"] and not m["done_untested"]


def test_episode_path_and_id_include_repeat():
    p = C.episode_path("glm-5.3-flash", "neutral", "full", "t|r0|a1", 2)
    assert p.name == "t_r0_a1#r2.json"
    assert C.EPISODE_ID_FMT.format(model_slug="m", header="neutral", cell="full",
                                   ctx_id="t|r0|a1", repeat=2).endswith("#r2")


def test_timed_out_suite_without_runner_output_is_not_a_run():
    slow = {"turn": 1, "cmd": "cd /agent && python3 big.py; pytest -q", "stdout": "",
            "stderr_tail": "command timed out after 60s", "exit": 124}
    ok = {"turn": 2, "cmd": "pytest -q", "stdout": FAIL_OUT, "exit": 1}
    assert measures.is_test_run(slow) == "suite"          # the cut rule's view is unchanged
    assert measures.ran_tests(slow) is None
    m = measures.episode_measures(_ep([slow, ok]))
    assert m["first_test_turn"] == 2 and not m["first_action_test"]
