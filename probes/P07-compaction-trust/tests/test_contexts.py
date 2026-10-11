"""Unit A (p07/contexts.py): qualify/cut rule, cut_msg indexing, n_tests, selection, and one
Docker-backed replay (skipped when Docker or the X3 image is unavailable)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess

import pytest
from p07 import contexts as X
from p07 import contracts as C

SOL, TST = X.SOLUTION, X.TEST_FILE


def _snap(step: int, sol: str, test: str = "t0", extra: dict | None = None) -> dict:
    files = {SOL: {"sha256": sol}, TST: {"sha256": test}, **(extra or {})}
    return {"step": step, "files": files, "error": None}


def _artifact(calls: list[tuple[int, str, str]], n_turns: int | None = None) -> dict:
    """calls: (turn, solution sha, test sha) after each call. Messages: system, user, then for
    each turn one assistant message with one tool result (two for a multi-call turn)."""
    snaps = [_snap(0, "s0")]
    out = []
    for i, (turn, sol, test) in enumerate(calls, start=1):
        snaps.append(_snap(i, sol, test))
        out.append({"turn": turn, "cmd": f"cmd{i}", "exit": 0, "snapshot_step": i})
    n = n_turns or max(t for t, _, _ in calls) + 1
    msgs = [{"role": "system"}, {"role": "user"}]
    for t in range(1, n + 1):
        msgs.append({"role": "assistant", "turn": t})
        msgs.append({"role": "tool", "turn": t})
    return {"snapshots": snaps, "calls": out, "messages": msgs}


def test_cut_first_edit_at_or_after_min_turn():
    art = _artifact([(1, "s0", "t0"), (2, "s1", "t0"), (3, "s1", "t0"), (4, "s2", "t0"),
                     (5, "s3", "t0")])
    cut, why = X.find_cut(art)
    assert why == "ok"
    assert cut["cut_turn"] == 4 and cut["cut_snapshot_step"] == 4 and cut["cut_call"] == 3
    assert cut["replay_cmds"] == ["cmd1", "cmd2", "cmd3", "cmd4"]
    # prefix ends after turn 4's tool result: index of turn 5's assistant message
    assert art["messages"][cut["cut_msg"]] == {"role": "assistant", "turn": 5}
    assert art["messages"][cut["cut_msg"] - 1] == {"role": "tool", "turn": 4}


def test_test_file_change_before_edit_disqualifies():
    art = _artifact([(1, "s0", "t0"), (3, "s0", "tX"), (4, "s1", "tX")])
    cut, why = X.find_cut(art)
    assert cut is None and why.startswith("test file changed")


def test_test_file_change_in_the_edit_call_disqualifies():
    art = _artifact([(1, "s0", "t0"), (3, "s1", "tX")])
    assert X.find_cut(art)[0] is None


def test_no_edit_does_not_qualify():
    art = _artifact([(1, "s0", "t0"), (3, "s0", "t0"), (5, "s0", "t0")])
    cut, why = X.find_cut(art)
    assert cut is None and why.startswith("no solution.py edit")


def test_turns_without_calls_and_multi_call_turn():
    # turns 2 and 3 had no call (nudged); turn 4 had two tool calls (only the first ran) and
    # the edit; turn 4 is the 4th assistant message
    art = _artifact([(1, "s0", "t0"), (4, "s1", "t0")])
    art["messages"] = [{"role": "system"}, {"role": "user"},
                       {"role": "assistant", "turn": 1}, {"role": "tool", "turn": 1},
                       {"role": "assistant", "turn": 2}, {"role": "user", "nudge": 1},
                       {"role": "assistant", "turn": 3}, {"role": "user", "nudge": 1},
                       {"role": "assistant", "turn": 4}, {"role": "tool", "turn": 4},
                       {"role": "tool", "turn": 4, "multi": True},
                       {"role": "assistant", "turn": 5}]
    cut, _ = X.find_cut(art)
    assert cut["cut_turn"] == 4
    assert cut["cut_msg"] == 11
    assert art["messages"][cut["cut_msg"] - 1]["multi"] is True


def test_cut_at_last_turn_takes_all_messages():
    msgs = [{"role": "system"}, {"role": "assistant"}, {"role": "tool"}]
    assert X.cut_after_turn(msgs, 1) == 3
    with pytest.raises(ValueError):
        X.cut_after_turn(msgs, 2)


def test_count_tests():
    src = ("import pytest\n\ndef test_a():\n    assert 1\n\ndef helper():\n    pass\n\n"
           "class TestX:\n    def test_inner(self):\n        pass\n\nasync def test_b():\n"
           "    pass\n")
    assert X.count_tests(src) == 2


def test_n_tests_is_one_for_every_x3_task():
    from x3.tasks import load_manifest

    assert {X.count_tests(t.test_solution_py) for t in load_manifest(C.TASKS_JSON).tasks} == {1}


def test_compare_and_cache_filter():
    snap = {"files": {"/agent/a": {"sha256": "1"}, "/agent/__pycache__/a.pyc": {"sha256": "2"},
                      "/agent/.pytest_cache/v": {"sha256": "3"}}}
    exp = X.snapshot_shas(snap)
    assert exp == {"/agent/a": "1"}
    assert X.compare({"/agent/a": {"sha256": "1"}}, exp) == (True, [])
    assert X.compare({"/agent/a": {"sha256": "9"}}, exp) == (False, ["/agent/a"])
    assert X.compare({"/agent/a": {"sha256": "1"}, "/agent/b": {"sha256": "1"}}, exp) == \
        (False, ["/agent/b"])
    assert X.compare({}, exp) == (False, ["/agent/a"])


def _cands(n_tasks: int, per_task: int, bad: set[str] = frozenset()) -> list[dict]:
    return [{"ctx_id": f"t{t}|r{r}|a1", "task_id": f"t{t}",
             "replay_match": f"t{t}|r{r}|a1" not in bad}
            for t in range(n_tasks) for r in range(per_task)]


def test_select_respects_cap_and_skips_failures():
    cands = _cands(5, 6, bad={"t0|r0|a1", "t1|r3|a1"})
    sel = X.select(cands, seed=1, n=12, max_per_task=3)
    assert len(sel) == 12 and len(set(sel)) == 12
    assert "t0|r0|a1" not in sel and "t1|r3|a1" not in sel
    tasks = [c.split("|")[0] for c in sel]
    assert max(tasks.count(t) for t in set(tasks)) <= 3
    # deterministic, independent of input order, and a prefix of a longer draw
    assert sel == X.select(list(reversed(cands)), seed=1, n=12, max_per_task=3)
    assert X.select(cands, seed=1, n=14, max_per_task=3)[:12] == sel


def test_select_short_when_cap_binds():
    assert len(X.select(_cands(2, 6), seed=1, n=10, max_per_task=3)) == 6


def test_list_hash():
    assert X.list_hash(["a", "b"]) == hashlib.sha256(b"a\nb").hexdigest()


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", X.IMAGE], capture_output=True,
                       check=False)
    return r.returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="docker or the x3-sandbox image is unavailable")
def test_replay_two_commands_in_docker():
    ws = {"solution.py": "x = 1\n", "RUN_INFO.txt": "hdr\n"}
    cmds = ["echo hi > out.txt && whoami >> out.txt && echo $HOME >> out.txt",
            "sed -i 's/1/2/' solution.py && mkdir -p __pycache__ && touch __pycache__/c.pyc"]
    res = X.replay(ws, cmds)
    assert res["error"] is None and res["exits"] == [0, 0]
    files = res["files"]
    assert files["/agent/out.txt"]["text"] == "hi\ndev\n/home/dev\n"
    assert files["/agent/solution.py"]["text"] == "x = 2\n"
    assert not any("__pycache__" in p for p in files)
    assert "/home/dev/.bashrc" in files

    def h(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()

    expected = {p: r["sha256"] for p, r in files.items() if p.startswith("/home/dev/")}
    expected.update({"/agent/solution.py": h("x = 2\n"), "/agent/RUN_INFO.txt": h("hdr\n"),
                     "/agent/out.txt": h("hi\ndev\n/home/dev\n")})
    assert X.compare(files, expected) == (True, [])
    expected["/agent/solution.py"] = h("x = 1\n")
    assert X.compare(files, expected) == (False, ["/agent/solution.py"])


def test_tested_edit_is_skipped_for_the_next_untested_one():
    art = _artifact([(1, "s0", "t0"), (3, "s1", "t0"), (4, "s2", "t0")])
    art["calls"][1]["cmd"] = "cat > solution.py <<'EOF'\nx = 1\nEOF\npytest test_solution.py -q"
    art["calls"][1]["stdout"] = "1 failed in 0.1s"
    cut, why = X.find_cut(art)
    assert why == "ok" and cut["cut_turn"] == 4


def test_heredoc_mention_of_pytest_does_not_count_as_a_run():
    art = _artifact([(1, "s0", "t0"), (3, "s1", "t0")])
    art["calls"][1]["cmd"] = "cat > solution.py <<'EOF'\n# run pytest later\nEOF"
    cut, why = X.find_cut(art)
    assert why == "ok" and cut["cut_turn"] == 3


def test_a_passing_test_run_before_the_cut_disqualifies():
    art = _artifact([(1, "s0", "t0"), (2, "s0", "t0"), (3, "s1", "t0")])
    art["calls"][1]["cmd"] = "pytest test_solution.py"
    art["calls"][1]["stdout"] = "==== 1 passed in 0.02s ===="
    cut, why = X.find_cut(art)
    assert cut is None and why.startswith("tests passed")


def test_select_skips_cuts_whose_claim_would_be_true():
    cands = [{"ctx_id": f"t{i}|r1|a1", "task_id": f"t{i}", "replay_match": True,
              "claim_false": i % 2 == 0} for i in range(6)]
    sel = X.select(cands, n=10, max_per_task=6)
    assert sorted(sel) == ["t0|r1|a1", "t2|r1|a1", "t4|r1|a1"]
