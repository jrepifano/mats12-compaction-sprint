"""compaction.s2.native.agy_chain and scripts/week2_harness_agy.py: the pieces that don't need
a live session ($0: no Docker, no agy, no model calls; the container is a fake)."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from test_native_agy_transcript import cp, exited, gen, plan, run, sysmsg, ui

from compaction.s2.chain import config as CC
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S
from compaction.s2.native import agy as A
from compaction.s2.native import agy_chain as X
from compaction.s2.native import common as N
from compaction.s2.native import turns as T

FIX = Path(__file__).parent / "fixtures" / "agy"
ROOT = Path(__file__).resolve().parents[2]
CID = "0b91b1d6-f056-495f-8f0a-199ea70be0c5"


@pytest.fixture
def script():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        yield importlib.import_module("week2_harness_agy")
    finally:
        sys.path.remove(str(ROOT / "scripts"))


class R:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr


class FakeSB:
    """Answers the driver's in-container commands from a dict of files."""

    name = "fake"
    host = None

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.cmds: list[str] = []

    def exec(self, cmd, timeout_s=0, workdir="/"):
        self.cmds.append(cmd)
        if cmd.startswith("cat ") and "transcript_full" in cmd and "grep" not in cmd:
            path = cmd.split()[1].strip("'")
            if path not in self.files:
                return R(1, "", "No such file")
            return R(0, self.files[path].decode())
        if cmd.startswith("cat "):
            path = cmd.split()[1]
            return R(0, self.files.get(path, b"").decode())
        if cmd.startswith("for f in "):
            return R(0, "".join(p + "\n" for p in self.files if p in cmd))
        if cmd.startswith("sha256sum "):
            return R(0, "".join(f"{hashlib.sha256(d).hexdigest()}  {p}\n"
                                for p, d in self.files.items() if p in cmd))
        if cmd.startswith("ls "):
            d = cmd.split()[1]
            return R(0, "\n".join(Path(p).name for p in self.files if p.startswith(d + "/")))
        return R(0, "")

    def put_file(self, data, path, timeout_s=0):
        self.files[path] = data

    def read_file(self, path, timeout_s=0):
        return self.files[path]


def jsonl(steps):
    return "".join(json.dumps(s) + "\n" for s in steps).encode()


# ---------------------------------------------------------------------------- parse_stop


def test_parse_stop_completion_from_the_echoed_token():
    new = [ui(0, "t"), plan(1, [run("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
           gen(2, exited(0, "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\r\n")),
           plan(3, [], content="Submitted.")]
    stop, info = X.parse_stop({"status": "SUCCESS", "response": "Submitted."}, "", new, None)
    assert stop.marker and not stop.asked_user and not stop.infra_failed
    assert T.decide(stop, T.State()).kind == "complete" and info["text_source"] == "response"


def test_parse_stop_ask_user_and_questions_get_the_non_answer():
    new = [plan(1, [{"name": "call_mcp_tool", "args": {
        "ServerName": "user-server", "ToolName": "ask_user",
        "Arguments": {"question": "Force push?"}}}]), gen(2, "No reply yet.")]
    stop, _ = X.parse_stop({"status": "SUCCESS", "response": "Waiting."}, "", new, None)
    assert stop.asked_user and T.decide(stop, T.State()).kind == "non_answer"
    stop, _ = X.parse_stop({"status": "SUCCESS", "response": "Shall I force push?"}, "", [],
                           None)
    assert T.decide(stop, T.State()).kind == "non_answer"


def test_parse_stop_progress_and_the_transcript_fallback_for_an_empty_response():
    new = [plan(4, [], content="I fixed the parser; tests pass.")]
    stop, info = X.parse_stop({"status": "SUCCESS", "response": ""}, "", new, None)
    assert stop.text.startswith("I fixed") and info["text_source"] == "transcript"
    assert T.decide(stop, T.State()).kind == "checkin"
    stop, info = X.parse_stop({"status": "SUCCESS"}, "", [], None)
    assert info["text_source"] == "none" and T.decide(stop, T.State()).reason == "unclassified"


def test_parse_stop_quota_time_overflow_and_infra():
    err = (FIX / "agy_error_synthetic.stderr").read_text()  # RESOURCE_EXHAUSTED, code 429

    class Res:
        rc, seconds, killed, timed_out_by_cli = 3, 5.0, None, False
    problem = A._turn_ok(Res(), {}, err)
    stop, info = X.parse_stop({}, err, [], problem)
    assert info["quota"] and not stop.infra_failed and info["cap"] is None
    stop, info = X.parse_stop({"status": "SUCCESS"}, "", [], "cli_timeout: ran 7185s of 7185s")
    assert info["cap"] == X.END_TIME and not stop.infra_failed
    over = 'AGY_ERROR: {"status":"INVALID_ARGUMENT","message":"input token count exceeds"}'
    stop, info = X.parse_stop({}, over, [], "agy exit 3: None | " + over)
    assert info["overflow"] and not stop.infra_failed
    stop, info = X.parse_stop({}, "boom", [], "agy exit 1: None | boom")
    assert stop.infra_failed and not (info["quota"] or info["overflow"] or info["cap"])
    assert T.decide(stop, T.State()).kind == "infra_resume"


# ---------------------------------------------------------------------------- config


def test_cli_config_has_mcp_and_permission_but_no_hook():
    class Server:
        token = "tok123"
    sb = FakeSB()
    X.install_cli_config(sb, Server())
    mcp = json.loads(sb.files[f"{A.XDIR}/mcp_config.json"])
    assert mcp["mcpServers"]["user-server"]["url"].endswith(f"{N.TOKEN_QUERY}=tok123")
    assert json.loads(sb.files[A.SETTINGS]) == {
        "permissions": {"allow": ["mcp(user-server/ask_user)"]}}
    assert not any("hooks.json" in p or p == A.HOOK for p in sb.files)
    assert any(f"rm -f {A.CONFIG_DIR}/hooks.json" in c for c in sb.cmds)
    assert X.AGY.compact_at is None and X.MODEL == "gemini-3.8-flash-low"
    cmd = A.agy_command(X.AGY, "Continue.", "c1", 100)
    assert "--conversation c1" in cmd and "--model gemini-3.8-flash-low" in cmd


def test_sessions_use_the_agy_allowlist_and_the_pilots_volumes():
    cfg = X.HistoryConfig(hist=CC.History("tengo", "np", 900, "divergent"), a_task=None,
                          b_task=None, out=Path("."))
    ns = X._session("img", cfg)
    assert ns.allow == N.ALLOW["agy"] and ns.forwards == () and ns.host == X.HOST
    assert f"{A.AUTH_VOLUME}:{A.AUTH_DIR}" in ns.extra_args
    assert N.purpose(ns.arm) == X.OURS == "native-agy-chain"


# ---------------------------------------------------------------------------- the turn loop


class FakeNS:
    def __init__(self, sb):
        self.sb = sb

        class Srv:
            token = "simtok"
        self.server = Srv()


def test_an_expired_deadline_launches_nothing_and_caps_time(monkeypatch):
    launched = []
    monkeypatch.setattr(X.N, "detached_exec", lambda *a, **k: launched.append(1))
    run_ = X.TaskRun(ns=None, out=None, deadline=time.monotonic() - 1)
    assert run_.drive("go") == X.END_TIME and launched == []


def test_the_call_cap_ends_the_task_before_another_turn(monkeypatch):
    launched = []
    monkeypatch.setattr(X.N, "detached_exec", lambda *a, **k: launched.append(1))
    run_ = X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600, max_calls=3,
                     steps=[plan(i, []) for i in range(3)])
    assert run_.drive("go") == X.END_CALLS and launched == []


def _scripted(monkeypatch, stops):
    """TaskRun.call replaced by a script of (Stop, info) pairs; returns the prompts sent."""
    sent = []

    def call(self, prompt, k, **kw):
        sent.append(prompt)
        stop, info = stops.pop(0)
        self.turns.append({"k": k})
        return stop, {"cap": None, "overflow": False, "quota": False, **info}, []
    monkeypatch.setattr(X.TaskRun, "call", call)
    return sent


def test_drive_applies_the_turn_rules(monkeypatch):
    q = (T.Stop(text="Should I force push?"), {})
    sent = _scripted(monkeypatch, [q, q, q, q])
    run_ = X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600, cid="c")
    assert run_.drive("task") == X.END_STALLED
    assert sent == ["task"] + [T.NON_ANSWER] * 3
    prog = (T.Stop(text="Working on it."), {})
    sent = _scripted(monkeypatch, [prog] * 7)
    run_ = X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600, cid="c")
    assert run_.drive("task") == X.END_STOPPED
    from compaction.s2.followups import CHECKINS
    assert sent == ["task", *CHECKINS]
    done = (T.Stop(text="", marker=True), {})
    _scripted(monkeypatch, [prog, done])
    assert X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600,
                     cid="c").drive("task") == X.END_SUBMITTED


def test_drive_infra_resumes_quota_and_caps(monkeypatch):
    bad = (T.Stop(text="", infra_failed=True), {})
    sent = _scripted(monkeypatch, [bad, bad, bad])
    run_ = X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600)  # no conversation
    assert run_.drive("task") == X.END_FAILED and sent == ["task"] * 3
    sent = _scripted(monkeypatch, [bad, (T.Stop(text=""), {"quota": True})])
    run_ = X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600, cid="c")
    assert run_.drive("task") == X.END_QUOTA and sent == ["task", A.CONTINUE_PROMPT]
    _scripted(monkeypatch, [(T.Stop(text="x", marker=True), {"cap": X.END_CALLS})])
    assert X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600,
                     cid="c").drive("task") == X.END_CALLS
    _scripted(monkeypatch, [(T.Stop(text=""), {"overflow": True})])
    assert X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600,
                     cid="c").drive("task") == X.END_CONTEXT


def a_steps():
    return [ui(0, "Please solve this issue: one"),
            plan(1, [run("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
            gen(2, exited(0, "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\r\n")),
            plan(3, [], content="Done."), ui(4, "/compact"), sysmsg(5), cp(6)]


def test_a_turn_in_b_never_reads_task_1s_steps_as_its_own(monkeypatch, tmp_path):
    # B's log repeats A's steps (one possibility before the smoke): A's submission echo must
    # not complete B's first turn
    b_new = [ui(7, "We're moving on. Please solve this issue: two"),
             plan(8, [], content="Looking at the repo first.")]
    sb = FakeSB({A.transcript_file(CID): jsonl(a_steps() + b_new),
                 A.TURN_OUT: json.dumps({"conversation_id": CID, "status": "SUCCESS",
                                         "response": "Looking at the repo first."}).encode()})
    monkeypatch.setattr(X.N, "detached_exec",
                        lambda *a, **k: N.StreamResult(0, 5.0, False, None, False))
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    run_ = X.TaskRun(ns=FakeNS(sb), out=tmp_path, deadline=time.monotonic() + 3600,
                     first_step=7, cid=CID)
    stop, _info, new = run_.call("We're moving on. Please solve this issue: two", 0)
    assert [s["step_index"] for s in new] == [7, 8]
    assert not stop.marker and stop.text.startswith("Looking")
    assert run_.n_calls() == 1 and run_.turns[0]["new_steps"] == [7, 8]
    assert T.decide(stop, T.State()).kind == "checkin"


def test_a_new_conversation_in_b_is_a_failed_resume(monkeypatch, tmp_path):
    sb = FakeSB({A.TURN_OUT: json.dumps({"conversation_id": "other", "status": "SUCCESS",
                                         "response": "Hi."}).encode()})
    monkeypatch.setattr(X.N, "detached_exec",
                        lambda *a, **k: N.StreamResult(0, 5.0, False, None, False))
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    run_ = X.TaskRun(ns=FakeNS(sb), out=tmp_path, deadline=time.monotonic() + 3600,
                     first_step=7, cid=CID)
    with pytest.raises(X.SB.SandboxError, match="did not resume"):
        run_.call("two", 0)
    assert run_.turns[-1]["conversation_id"] == "other"


def test_a_turn_past_the_call_cap_is_capped(monkeypatch, tmp_path):
    steps = [ui(0, "t")] + [plan(i, []) for i in range(1, 6)]
    sb = FakeSB({A.transcript_file(CID): jsonl(steps),
                 A.TURN_OUT: json.dumps({"conversation_id": CID, "status": "SUCCESS",
                                         "response": "x"}).encode()})
    monkeypatch.setattr(X.N, "detached_exec",
                        lambda *a, **k: N.StreamResult(0, 5.0, False, None, False))
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    run_ = X.TaskRun(ns=FakeNS(sb), out=tmp_path, deadline=time.monotonic() + 3600,
                     cid=CID, max_calls=4)
    _stop, info, _new = run_.call("t", 0)
    assert info["cap"] == X.END_CALLS


def test_the_call_watch_counts_this_tasks_calls_only():
    lines = "\n".join(str(i) for i in (1, 3, 8, 9, 10))

    class SB:
        def exec(self, cmd, timeout_s=0, workdir="/"):
            assert "PLANNER_RESPONSE" in cmd and "brain/*" in cmd
            return R(0, lines)
    assert X.count_calls(SB(), 7) == 3 and X.count_calls(SB(), 0) == 5




# ---------------------------------------------------------------------------- quiescence


PROCS = {1: (0, "Ss", "timeout -k 5 9000 sleep infinity"), 2: (1, "S", "sleep infinity"),
         7: (1, "Ss", "postgres -D /var/lib/pg"), 8: (7, "S", "postgres: writer"),
         50: (1, "S", f"{A.CLI} -p secret-task-text"), 51: (50, "S", "bash -c pnpm build"),
         52: (51, "Z", "[node] <defunct>"), 60: (1, "S", "node /app/server.js --api-key x"),
         90: (0, "S", ": emx-q-1; ps -eo pid="), 91: (90, "R", "ps -eo pid=")}


def test_quiesce_stops_agy_its_children_and_orphans_but_not_infra(monkeypatch):
    tables = [PROCS, {k: v for k, v in PROCS.items() if k not in (50, 51, 60)}]
    sent = []
    monkeypatch.setattr(X.N, "ps_table", lambda sb: (tables.pop(0) if len(tables) > 1
                                                     else tables[0], "emx-q-1"))
    monkeypatch.setattr(X.N, "signal_pids", lambda sb, pids, sig: sent.append((sig, pids)))
    q = X.quiesce(None, services=frozenset({7}), wait_s=0)
    assert sent == [("TERM", [50, 51, 60]), ("KILL", [50, 51, 60])]  # the orphan 60 too
    assert q["confirmed"] and q["stopped"] == ["50 agy", "51 bash", "60 node"]
    assert "secret-task-text" not in json.dumps(q) and "--api-key" not in json.dumps(q)


def test_quiesce_unconfirmed_on_survivors_or_an_unreadable_table(monkeypatch):
    monkeypatch.setattr(X.N, "ps_table", lambda sb: (PROCS, "emx-q-1"))
    monkeypatch.setattr(X.N, "signal_pids", lambda sb, pids, sig: None)
    q = X.quiesce(None, services=frozenset({7}), rounds=2, wait_s=0)
    assert not q["confirmed"] and q["survivors"] == ["50 agy", "51 bash", "60 node"]
    monkeypatch.setattr(X.time, "sleep", lambda s: None)
    log: list = []
    with pytest.raises(X.NotQuiescent):
        X.require_quiet(None, frozenset({7}), log, "task end")
    assert log[0]["why"] == "task end" and not log[0]["confirmed"]

    def broken(sb):
        raise X.SB.SandboxError("ps failed")
    monkeypatch.setattr(X.N, "ps_table", broken)
    assert not X.quiesce(None, wait_s=0)["confirmed"]


def test_a_call_cap_hit_requires_verified_quiescence(monkeypatch, tmp_path):
    def capped(*a, watch=None, **k):
        monkeypatch.setattr(X, "count_calls", lambda sb, first: 400)
        assert watch() is True
        return N.StreamResult(-1, 60.0, False, "watch_abort", False)
    monkeypatch.setattr(X.N, "detached_exec", capped)
    monkeypatch.setattr(X, "quiesce", lambda sb, services: {"confirmed": False,
                                                           "survivors": ["50 agy"]})
    run_ = X.TaskRun(ns=FakeNS(FakeSB()), out=tmp_path, deadline=time.monotonic() + 3600,
                     cid=CID)
    with pytest.raises(X.NotQuiescent):
        run_.call("t", 0)
    assert run_.turns[-1]["cap_hit_at"] == 400 and run_.quiet_log[0]["why"] == "call cap"


# ---------------------------------------------------------------------------- quota


def test_a_set_flag_launches_nothing_not_even_the_first_turn(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(X.N, "detached_exec", lambda *a, **k: launched.append(1))
    flag = X.threading.Event()
    flag.set()
    run_ = X.TaskRun(ns=FakeNS(FakeSB()), out=tmp_path, deadline=time.monotonic() + 3600,
                     quota_stop=flag)
    assert run_.drive("task") == X.END_QUOTA and launched == []
    assert run_.stop_reason == "shared stop" and run_.quota_cut == "shared stop"
    assert run_.turns == [{"k": 0, "prompt": "task", "launched": False, "quota_stop": True,
                           "info": {"cap": None, "overflow": False, "quota": True,
                                    "shared": True, "problem": None}}]


def test_a_flag_set_mid_task_stops_the_next_ordinary_turn(monkeypatch, tmp_path):
    flag = X.threading.Event()
    sb = FakeSB({A.TURN_OUT: json.dumps({"conversation_id": CID, "status": "SUCCESS",
                                         "response": "Working on it."}).encode()})
    launched = []

    def turn(*a, **k):
        launched.append(1)
        flag.set()  # another history is refused while this turn runs
        return N.StreamResult(0, 5.0, False, None, False)
    monkeypatch.setattr(X.N, "detached_exec", turn)
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    run_ = X.TaskRun(ns=FakeNS(sb), out=tmp_path, deadline=time.monotonic() + 3600, cid=CID,
                     quota_stop=flag)
    assert run_.drive("task") == X.END_QUOTA and launched == [1]  # no check-in turn
    assert run_.turns[0]["decision"]["kind"] == "checkin" and run_.quota_cut == "shared stop"


def test_a_refusal_sets_the_shared_flag(monkeypatch, tmp_path):
    err = (FIX / "agy_error_synthetic.stderr").read_text()
    sb = FakeSB({A.TURN_ERR: err.encode(), A.TURN_OUT: b""})
    monkeypatch.setattr(X.N, "detached_exec",
                        lambda *a, **k: N.StreamResult(3, 5.0, False, None, False))
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    flag = X.threading.Event()
    run_ = X.TaskRun(ns=FakeNS(sb), out=tmp_path, deadline=time.monotonic() + 3600, cid=CID,
                     quota_stop=flag)
    assert run_.drive("t") == X.END_QUOTA and flag.is_set()
    assert run_.stop_reason == run_.quota_cut == "refused"


def test_the_boundary_is_skipped_after_a_refusal_and_stops_on_one():
    flag = X.threading.Event()
    run_ = BoundaryRun(a_steps()[:4], [a_steps()], quota_stop=flag)
    flag.set()
    out = X.boundary_compact(run_)
    assert out["quota"] and not out["ok"] and run_.prompts == [] and run_.quiets == []
    flag.clear()
    run_ = BoundaryRun(a_steps()[:4], [a_steps()[:4], a_steps()], quota_stop=flag,
                       info={"quota": True})
    out = X.boundary_compact(run_)
    assert out["deviation"] == "quota refused at the boundary" and flag.is_set()
    assert len(run_.prompts) == 1  # no retry


def _history_cfg(tmp_path, flag=None, pre_compact=False):
    """The `_task` path tests run without experiment 5's extra save, which has its own tests
    below (it adds a quiescence check and screens to the event sequence)."""
    tk = type("Tk", (), {"image": "img", "instruction": f"Fix it.\n\n{CC.DEEPSWE_ENDING}"})()
    return X.HistoryConfig(hist=CC.History("tengo", "np", 900, "divergent"), a_task=tk,
                           b_task=tk, out=tmp_path,
                           quota_stop=flag or X.threading.Event(),
                           pre_compact_checkpoint=pre_compact)


def test_a_quota_stopped_history_keeps_its_slot_with_the_primary_unknown(monkeypatch,
                                                                         tmp_path):
    flag = X.threading.Event()
    flag.set()
    launched = []
    monkeypatch.setattr(X, "_task", lambda *a, **k: launched.append(1))
    res = X.run_history(_history_cfg(tmp_path, flag))
    assert res["a_end"] == res["b_end"] == X.END_QUOTA and launched == []
    rb = json.loads((tmp_path / "b" / "result.json").read_text())
    assert rb["end"] == X.END_QUOTA and rb["extra"]["quota_cut"]
    assert rb["extra"]["primary_unknown_reason"] == "quota stop before task 1"


@pytest.mark.parametrize("mark", ["quota refused in task 1", "quota refused at the boundary"])
def test_a_quota_marked_first_task_never_launches_task_2(monkeypatch, tmp_path, mark):
    calls = []

    def fake_task(cfg, out, task, branches, *, second, **kw):
        calls.append(second)
        return {"end": X.END_FAILED, "cid": CID, "session": ({}, {"last_step_index": 6}),
                "quota_cut": mark}  # a later failure doesn't erase the cut
    monkeypatch.setattr(X, "_task", fake_task)
    monkeypatch.setattr(X.A, "ensure_cli_volume", lambda host, image: "vol")
    res = X.run_history(_history_cfg(tmp_path))
    assert calls == [False] and res["b_end"] == X.END_QUOTA
    rb = json.loads((tmp_path / "b" / "result.json").read_text())
    assert rb["extra"]["primary_unknown_reason"] == mark


def test_a_cli_volume_failure_still_writes_both_results(monkeypatch, tmp_path):
    def boom(host, image):
        raise X.SB.SandboxError("curl failed")
    monkeypatch.setattr(X.A, "ensure_cli_volume", boom)
    res = X.run_history(_history_cfg(tmp_path))
    assert res["a_end"] == res["b_end"] == X.END_FAILED
    for t in ("a", "b"):
        r = json.loads((tmp_path / t / "result.json").read_text())
        assert r["end"] == X.END_FAILED and "curl failed" in r["error"]  # before any agent


def test_quota_marks_read_the_task_and_the_history_cut():
    assert X.quota_marks({"end": X.END_QUOTA, "extra": {}}) == "quota"
    assert X.quota_marks({"end": "failed", "extra": {
        "quota_cut": True, "primary_unknown_reason": "quota refused in task 1"}}) == \
        "quota refused in task 1"
    assert X.quota_marks({"end": "submitted", "extra": {
        "history_quota_cut": "quota refused at the boundary"}}) == \
        "quota refused at the boundary"
    assert X.quota_marks({"end": "failed", "extra": {}}) is None and X.quota_marks(None) is None


# ---------------------------------------------------------------------------- _task paths


class FakeEnv:
    bare = "/srv/git/tengo.git"
    sb = None  # ChainEnv's sandbox (the pre-compaction save swaps in a non-destructive view)

    def __init__(self, out, events):
        self.out, self.events = out, events

    def b_setup(self, repo):
        return {"teammate": "abc", "remote_refs": {}}

    def finish_traps(self, label):
        self.events.append("finish_traps")
        home = self.out / "traps_home"
        home.mkdir(exist_ok=True)
        (home / "config.json").write_text("{}")
        (self.out / "traps_home.tgz").write_bytes(b"x")
        return home


def _fake_session(monkeypatch, tmp_path, *, end, quiet=None, pre_ok=True, post_ok=True,
                  diag_ok=True, drive=None, boundary=None):
    """`_task` around fakes: the session, the chain install, the turns and the screens.
    `quiet` lists quiescence verdicts in call order (default: all confirmed)."""
    class Egress:
        def proxy_env(self):
            return {}

    class NS:
        sb, egress, services = FakeSB(), Egress(), frozenset()

        class server:
            token = "simtok"

            @staticmethod
            def export():
                return {"questions": []}

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False
    events = []
    verdicts = list(quiet or [])
    monkeypatch.setattr(X, "_session", lambda image, cfg: NS())
    monkeypatch.setattr(X.X, "install_chain",
                        lambda sb, task, out, br: (FakeEnv(out, events), []))
    monkeypatch.setattr(X.A, "install_auth", lambda sb: None)
    monkeypatch.setattr(X, "install_cli_config", lambda sb, srv: None)
    monkeypatch.setattr(X.X, "env_checks", lambda sb: {"which_git": []})
    monkeypatch.setattr(X.N, "egress_check", lambda sb, eg, url: {"ok": True})

    def default_drive(self, prompt):
        self.cid = CID
        return end
    monkeypatch.setattr(X.TaskRun, "drive", drive or default_drive)

    def quiesce(sb, services):
        events.append("quiesce")
        ok = verdicts.pop(0) if verdicts else True
        return {"confirmed": ok, "stopped": [], "survivors": [] if ok else ["50 agy"],
                "errors": []}
    monkeypatch.setattr(X, "quiesce", quiesce)

    def fake_boundary(run):
        events.append("boundary")
        return boundary or {"ok": True, "before": 0}
    monkeypatch.setattr(X, "boundary_compact", fake_boundary)
    monkeypatch.setattr(X, "task_steps", lambda sb, cid, tok: (
        events.append("transcript"), a_steps())[1])
    monkeypatch.setattr(X, "export_session", lambda sb, cid, tok, steps: (
        events.append("export"), ({"conversations/x.db": b"db"}, {"last_step_index": 6}))[1])
    monkeypatch.setattr(X, "prescreen_evidence", lambda sb, bare, tok, **kw: (
        events.append("prescreen"), {"ok": pre_ok, "why": None, "profile_free":
                                     kw.get("run_env") is X._exec_np})[1])
    monkeypatch.setattr(X, "screen_evidence", lambda sb, out, tok: (
        events.append("post_screen"), {"ok": post_ok, "why": None})[1])
    monkeypatch.setattr(X, "screened", lambda sb, obj, tok: obj)
    monkeypatch.setattr(X, "state_listing", lambda sb, tok: [])
    monkeypatch.setattr(X, "screen_blob", lambda sb, data, tok: (
        events.append("diag_screen"), diag_ok)[1])
    monkeypatch.setattr(X, "_exec_np", fake_exec_np(events))  # never a real docker exec
    return events


def fake_exec_np(events, hits=(), stage_left=False):
    """The profile-free exec: the content scan's NUL records, the cgroup memory record."""
    def run_(sb, cmd, env=None, *, timeout_s=300, workdir="/"):
        if "SCAN_OK" in cmd:
            events.append("secret_scan")
            out = "#N 9\0" + "".join(f"{h}\0" for h in hits) + "SCAN_OK\0"
        elif "memory.events" in cmd:
            events.append("memory")
            out = "oom 0\noom_kill 0\npeak=524288000\nmax=1342177280\n"
        elif cmd.startswith("test ! -e") and stage_left:
            return X.SB.ExecResult(1, "", "", 0.1)
        else:
            out = ""
        return X.SB.ExecResult(0, out, "", 0.1)
    return run_


def test_task_1_order_quiet_boundary_quiet_export_then_screened_evidence(monkeypatch,
                                                                         tmp_path):
    events = _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED)
    cfg = _history_cfg(tmp_path)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert rec["end"] == X.END_SUBMITTED and rec["session"] and not rec["quota_cut"]
    assert events == ["quiesce", "boundary", "quiesce", "transcript", "export", "prescreen",
                      "finish_traps", "post_screen", "memory", "diag_screen"]
    assert json.loads((tmp_path / "result.json").read_text())["extra"]["memory"] == {
        "oom_kills": 0, "oom_events": 0, "peak_mb": 500, "limit_mb": 1280}
    res = json.loads((tmp_path / "result.json").read_text())
    assert [q["why"] for q in res["extra"]["quiescence"]] == ["task end", "after the boundary"]
    assert res["traps_home"] and res["extra"]["evidence_screen"]["ok"]


def test_an_unconfirmed_stop_ends_task_1_failed_without_a_handoff(monkeypatch, tmp_path):
    events = _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, quiet=[False])
    cfg = _history_cfg(tmp_path)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert rec["end"] == X.END_FAILED and not rec.get("session")
    assert "boundary" not in events and "export" not in events
    assert "prescreen" not in events  # astra agy-build r3 B1: no evidence from a live container
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["traps_home"] is None
    assert res["extra"]["evidence_screen"] == {"ok": False, "skipped": "not quiescent"}


def test_a_boundary_left_non_quiescent_ends_task_1_without_a_handoff(monkeypatch, tmp_path):
    events = _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, boundary={
        "ok": False, "not_quiescent": True, "deviation": "not quiescent before boundary "
        "attempt 1"})
    cfg = _history_cfg(tmp_path)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert rec["end"] == X.END_FAILED and not rec.get("session") and "export" not in events
    assert "prescreen" not in events


def test_a_quota_ending_skips_the_boundary(monkeypatch, tmp_path):
    events = _fake_session(monkeypatch, tmp_path, end=X.END_QUOTA)
    cfg = _history_cfg(tmp_path)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert rec["boundary"]["skipped"] == "quota stop" and "boundary" not in events
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["extra"]["primary_unknown_reason"] == "quota refused in task 1"
    assert rec["quota_cut"] == "quota refused in task 1"


def test_a_quota_cut_survives_a_later_failure(monkeypatch, tmp_path):
    def drive(self, prompt):
        self.cid, self.quota_cut = CID, "refused"
        return X.END_QUOTA
    _fake_session(monkeypatch, tmp_path, end=X.END_QUOTA, quiet=[False], drive=drive)
    cfg = _history_cfg(tmp_path)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=True, prompt="p")
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["end"] == X.END_FAILED  # the failed stop wins the ending ...
    assert res["extra"]["quota_cut"] is True  # ... but the cut is kept, and masks the primary
    assert res["extra"]["primary_unknown_reason"] == "quota refused in task 2"
    assert rec["quota_cut"] == "quota refused in task 2"


def test_a_boundary_refusal_marks_the_history_not_task_1(monkeypatch, tmp_path):
    _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, boundary={
        "ok": False, "quota": True, "deviation": "quota refused at the boundary"})
    cfg = _history_cfg(tmp_path)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["end"] == X.END_SUBMITTED and "primary_unknown_reason" not in res["extra"]
    assert res["extra"]["history_quota_cut"] == "quota refused at the boundary"
    assert rec["quota_cut"] == "quota refused at the boundary" and not rec.get("session")


def test_evidence_is_never_written_before_a_clean_prescreen(monkeypatch, tmp_path):
    events = _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, pre_ok=False)
    (tmp_path / "traps_home").mkdir()  # the setup-time home (our config only)
    cfg = _history_cfg(tmp_path)
    X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert "finish_traps" not in events and "post_screen" not in events
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["traps_home"] is None and res["extra"]["evidence_omitted"] == ["traps_home"]
    assert not (tmp_path / "traps_home.tgz").exists()


def test_exported_evidence_that_fails_the_second_check_is_omitted(monkeypatch, tmp_path):
    _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, post_ok=False)
    cfg = _history_cfg(tmp_path)
    X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["traps_home"] is None
    assert sorted(res["extra"]["evidence_omitted"]) == ["traps_home", "traps_home.tgz"]
    assert not (tmp_path / "traps_home").exists() and not (tmp_path / "traps_home.tgz").exists()


def test_diagnostics_that_fail_the_screen_keep_fixed_codes_only(monkeypatch, tmp_path):
    def drive(self, prompt):
        self.cid = CID
        self.turns.append({"k": 0, "stop": {"text": "LEAKY"}, "info": {"problem": "LEAKY"},
                           "decision": {"kind": "checkin"}})
        raise RuntimeError("LEAKY agent text in an exception")
    events = _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, diag_ok=False,
                           drive=drive)
    cfg = _history_cfg(tmp_path)
    X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    raw = (tmp_path / "result.json").read_text()
    assert "LEAKY" not in raw and events[-1] == "diag_screen"
    res = json.loads(raw)
    assert res["error"].startswith("task: RuntimeError") and res["extra"]["diagnostics"] == \
        X.OMITTED
    assert res["extra"]["turns"] == [{"k": 0, "launched": True, "decision": "checkin",
                                      "cap": None, "quota": None}]


def test_diagnostics_are_kept_when_the_screen_passes(monkeypatch, tmp_path):
    def drive(self, prompt):
        self.cid = CID
        raise RuntimeError("transport dropped")
    _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, drive=drive)
    cfg = _history_cfg(tmp_path)
    X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["error"] == "task: RuntimeError: transport dropped"
    assert "diagnostics" not in res["extra"]


# ---------------------------------------------------------------------------- screening


def test_the_transcript_is_screened_before_it_is_read(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(X.A, "screen", lambda sb, paths, tok: order.append(("screen", paths)))
    monkeypatch.setattr(X.A, "read_steps", lambda sb, cid: (order.append(("read", cid)),
                                                            a_steps())[1])
    run_ = X.TaskRun(ns=FakeNS(FakeSB()), out=tmp_path, deadline=time.monotonic() + 60,
                     cid=CID)
    assert run_.read() == a_steps()
    assert order == [("screen", [A.transcript_file(CID)]), ("read", CID)]

    def hit(sb, paths, tok):
        raise A.SecretFound("hit")
    monkeypatch.setattr(X.A, "screen", hit)
    with pytest.raises(A.SecretFound):
        run_.read()
    assert run_.steps == a_steps()  # the last screened reading, never the new text


def test_screen_blob_omits_on_a_hit_or_a_failed_screen(monkeypatch):
    monkeypatch.setattr(X.A, "screen", lambda sb, paths, tok: None)
    assert X.screened(FakeSB(), {"q": "ok"}, "t") == {"q": "ok"}

    def hit(sb, paths, tok):
        raise A.SecretFound("hit")
    monkeypatch.setattr(X.A, "screen", hit)
    sb = FakeSB()
    assert X.screened(sb, {"q": "leak"}, "t") == X.OMITTED
    assert any(c.startswith("rm -f /tmp/emx-screen-") for c in sb.cmds)


def test_prescreen_stages_in_the_container_and_writes_nothing_on_the_host(monkeypatch,
                                                                         tmp_path):
    monkeypatch.chdir(tmp_path)
    sb = FakeSB()
    monkeypatch.setattr(X, "container_log_bytes", lambda sb: b"log lines")
    runs = []

    def exec_env(sb_, cmd, env, timeout_s=0):
        runs.append(cmd)
        return R(1, "SCREEN_CLEAN")
    monkeypatch.setattr(X.N, "exec_env", exec_env)
    rec = X.prescreen_evidence(sb, "/srv/git/tengo.git", "t")
    assert rec == {"ok": True, "why": None, "attempts": 1}
    assert sb.files[f"{X.STAGE_DIR}/container.log"] == b"log lines"  # in the container
    assert any(f"{X.STAGE_DIR}/paths.txt" in c for c in sb.cmds)
    assert "/srv/git/tengo.git" in runs[0] and X.STAGE_DIR in runs[0]
    assert "tar xzf" not in runs[0]  # live repos are never unpacked into
    assert list(tmp_path.iterdir()) == []  # nothing on the host
    assert sb.cmds[-1] == f"rm -rf {X.STAGE_DIR}"
    verdicts = [R(2, "", "hiccup"), R(0, "/srv/git/tengo.git SCREEN_HIT")]
    monkeypatch.setattr(X.N, "exec_env", lambda *a, **k: verdicts.pop(0))
    rec = X.prescreen_evidence(FakeSB(), "/srv/git/tengo.git", "t")
    assert rec["ok"] is False and rec["attempts"] == 2 and "secret" in rec["why"]
    monkeypatch.setattr(X.N, "exec_env", lambda *a, **k: R(5, ""))
    assert not X.prescreen_evidence(FakeSB(), "/x", "t")["ok"]  # no verdict: never clean


def test_screen_evidence_verdicts_and_retry(monkeypatch, tmp_path):
    (tmp_path / "traps_home").mkdir()
    (tmp_path / "traps_home" / "config.json").write_text("{}")
    (tmp_path / "git_meta_final.tgz").write_bytes(b"x")

    class SBC(FakeSB):
        def copy_in(self, src, dest, timeout_s=0):
            self.copied = sorted(p.name for p in Path(src).iterdir())
    verdicts = [R(2, "", "docker hiccup"), R(1, "SCREEN_CLEAN")]
    monkeypatch.setattr(X.N, "exec_env", lambda sb, cmd, env, timeout_s=0: verdicts.pop(0))
    sb = SBC()
    rec = X.screen_evidence(sb, tmp_path, "t")
    assert rec["ok"] and rec["attempts"] == 2
    assert sb.copied == ["git_meta_final.tgz", "traps_home"]
    monkeypatch.setattr(X.N, "exec_env", lambda *a, **k: R(0, "/tmp/x/remote SCREEN_HIT"))
    assert not X.screen_evidence(SBC(), tmp_path, "t")["ok"]
    monkeypatch.setattr(X.N, "exec_env", lambda *a, **k: R(5, ""))
    assert not X.screen_evidence(SBC(), tmp_path, "t")["ok"]  # no verdict: never clean


def _run_screen(monkeypatch, tmp_path, roots, secret, unpack=None):
    import subprocess
    monkeypatch.setattr(X.A, "TOKEN_PATTERNS", f"echo {secret}")
    script = X.evidence_screen_script([str(r) for r in roots],
                                      unpack=str(unpack) if unpack else None)
    script = script.replace("mktemp -p /dev/shm", "mktemp")  # macOS has no /dev/shm
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                            "EMX_SIM_TOKEN": "simtok-not-present-anywhere",
                            "HOME": str(tmp_path)}, timeout=60)
    return p.returncode, p.stdout


def _repo(root: Path, text: str) -> None:
    import subprocess
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(root)]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "f.txt").write_text(text)
    subprocess.run(g + ["add", "f.txt"], check=True)
    subprocess.run(g + ["commit", "-qm", "m"], check=True)
    (root / "f.txt").unlink()  # the text now lives only in compressed objects


SECRET = "S3CRETSTRINGabcdefghijklmnopqrstuvwxyz0123456789"


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")
def test_the_evidence_screen_decodes_git_objects_across_roots(monkeypatch, tmp_path):
    """The screen script itself, with local bash and git on scratch repos: a secret only in a
    commit (compressed in .git/objects) is found in any root; clean roots pass."""
    dirty, clean, stage = tmp_path / "dirty", tmp_path / "clean", tmp_path / "stage"
    _repo(dirty, f"leak {SECRET}\n")
    _repo(clean, "nothing\n")
    stage.mkdir()
    (stage / "container.log").write_text("ordinary log line\n")
    rc, out = _run_screen(monkeypatch, tmp_path, [stage, clean / ".git", dirty / ".git"],
                          SECRET)
    assert rc == 0 and "SCREEN_HIT" in out
    rc, out = _run_screen(monkeypatch, tmp_path, [stage, clean / ".git"], SECRET)
    assert rc == 1 and "SCREEN_CLEAN" in out
    (stage / "container.log").write_text(f"agent printed {SECRET}\n")  # a staged file
    rc, out = _run_screen(monkeypatch, tmp_path, [stage, clean / ".git"], SECRET)
    assert rc == 0 and "SCREEN_HIT" in out


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")
def test_the_evidence_screen_fails_closed_when_git_cannot_decode(monkeypatch, tmp_path):
    fake = tmp_path / "notgit"
    (fake / "objects").mkdir(parents=True)
    (fake / "HEAD").write_text("garbage\n")  # looks like a git dir; git refuses it
    rc, out = _run_screen(monkeypatch, tmp_path, [fake], SECRET)
    assert rc == 6 and "SCREEN_CLEAN" not in out


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")
def test_the_evidence_screen_follows_a_symlinked_remote(monkeypatch, tmp_path):
    """/srv/git/<repo>.git may be a symlink into the scorer's home: its objects are scanned."""
    real = tmp_path / "home" / "remote"
    real.parent.mkdir()
    _repo(real, f"leak {SECRET}\n")
    link = tmp_path / "tengo.git"
    link.symlink_to(real / ".git")
    rc, out = _run_screen(monkeypatch, tmp_path, [link], SECRET)
    assert rc == 0 and "SCREEN_HIT" in out


def test_wrapper_output_reaches_the_run_dir_only_once_screened(monkeypatch, tmp_path):
    sb = FakeSB({A.TURN_OUT: json.dumps({"conversation_id": CID, "status": "SUCCESS",
                                         "response": "Done."}).encode()})
    seen = {}

    def turn(sb_, cmd, *, log_path, **k):
        seen["log"] = log_path
        Path(log_path).write_bytes(b"profile says hello\n")
        Path(str(log_path) + ".err").write_bytes(b"LEAK from an edited profile\n")
        return N.StreamResult(0, 5.0, False, None, False)
    monkeypatch.setattr(X.N, "detached_exec", turn)
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    monkeypatch.setattr(X, "screen_blob", lambda sb_, data, tok: b"LEAK" not in data)
    run_ = X.TaskRun(ns=FakeNS(sb), out=tmp_path, deadline=time.monotonic() + 3600, cid=CID)
    run_.call("t", 0)
    assert tmp_path not in Path(seen["log"]).parents  # streamed outside the run dir
    assert not Path(seen["log"]).exists()  # and the temp copy is gone
    assert (tmp_path / "turn_000.json.exec").read_bytes() == b"profile says hello\n"
    assert not (tmp_path / "turn_000.json.exec.err").exists()
    assert run_.turns[-1]["wrapper_output_omitted"] == [".err"]


# ---------------------------------------------------------------------------- boundary


class BoundaryRun:
    def __init__(self, before, afters, quota_stop=None, info=None, quiet=None):
        self.steps, self.afters, self.prompts, self.quiets = before, list(afters), [], []
        self.quota_stop = quota_stop or X.threading.Event()
        self.info = info or {}
        self.verdicts = list(quiet or [])

    def read(self):
        return self.steps

    def quiet(self, why):
        self.quiets.append(why)
        return self.verdicts.pop(0) if self.verdicts else True

    def call(self, prompt, k, **kw):
        self.prompts.append((prompt, kw))
        self.steps = self.afters.pop(0)
        return T.Stop(text=""), dict(self.info), []


def test_boundary_one_new_checkpoint_after_our_compact():
    before = a_steps()[:4]
    run_ = BoundaryRun(before, [a_steps()])
    out = X.boundary_compact(run_)
    assert out["ok"] and out["before"] == 0 and out["checkpoint_step"] == 6
    assert run_.prompts == [("/compact", {"extra_s": 900, "cap_calls": False})]
    assert run_.quiets == ["before boundary attempt 1"]


def test_boundary_counts_agy_compactions_before_it_and_retries_once():
    auto = [ui(0, "t"), plan(1, []), cp(2, "auto"), plan(3, [])]
    run_ = BoundaryRun(auto, [auto, auto])
    out = X.boundary_compact(run_)
    assert not out["ok"] and out["before"] == 1 and out["auto_before"] == 1
    assert len(out["attempts"]) == 2 and "failed twice" in out["deviation"]
    assert run_.quiets == ["before boundary attempt 1", "before boundary attempt 2"]
    two = auto + [ui(4, "/compact"), cp(5), cp(6)]
    out = X.boundary_compact(BoundaryRun(auto, [two]))
    assert not out["ok"] and "2 new checkpoints" in out["deviation"]
    late = auto + [plan(4, []), cp(5)]  # agy's own, not after our input
    out = X.boundary_compact(BoundaryRun(auto, [late]))
    assert not out["ok"] and "not after /compact" in out["deviation"]


def test_every_boundary_attempt_waits_on_verified_quiescence():
    run_ = BoundaryRun(a_steps()[:4], [a_steps()], quiet=[False])
    out = X.boundary_compact(run_)
    assert out["not_quiescent"] and run_.prompts == []  # the first attempt too
    assert out["deviation"] == "not quiescent before boundary attempt 1"
    four = a_steps()[:4]
    run_ = BoundaryRun(four, [four], quiet=[True, False])  # the retry finds a survivor
    out = X.boundary_compact(run_)
    assert out["not_quiescent"] and len(run_.prompts) == 1
    assert out["deviation"] == "not quiescent before boundary attempt 2"


# ---------------------------------------------------------------------------- transfer


def test_session_export_moves_only_the_conversation_db(monkeypatch):
    db = f"{X.CONVERSATIONS}/{CID}.db"
    files = {db: b"SQLite format 3\0...", f"{db}-wal": b"wal",
             f"{A.STATE_DIR}/{A.TOKEN_FILE}": b"never", A.SETTINGS: b"{}",
             A.transcript_file(CID): jsonl(a_steps())}
    screened_paths = []
    monkeypatch.setattr(X.A, "screen", lambda sb, paths, tok: screened_paths.append(paths))
    monkeypatch.setattr(X, "state_listing", lambda sb, tok: ["f 10 ./settings.json"])
    blobs, man = X.export_session(FakeSB(files), CID, "simtok", a_steps())
    assert sorted(blobs) == [f"conversations/{CID}.db", f"conversations/{CID}.db-wal"]
    assert screened_paths == [[db, f"{db}-wal"]]
    assert man["checkpoints"] == 1 and man["last_step_index"] == 6
    f0 = next(f for f in man["files"] if f["path"].endswith(".db"))
    assert f0["sha256"] == hashlib.sha256(files[db]).hexdigest() and f0["bytes"] == len(files[db])
    b_sb = FakeSB()
    assert X.import_session(b_sb, blobs)["verified"]
    assert set(b_sb.files) == {db, f"{db}-wal"}
    with pytest.raises(X.SB.SandboxError):
        X.export_session(FakeSB({A.SETTINGS: b"{}"}), CID, "simtok", [])


# ---------------------------------------------------------------------------- run dirs


def test_result_json_is_written_when_the_session_cannot_start(tmp_path, monkeypatch):
    def boom(image, cfg):
        raise OSError("docker host unreachable")
    monkeypatch.setattr(X, "_session", boom)
    cfg = X.HistoryConfig(hist=CC.History("tengo", "np", 900, "divergent"), a_task=None,
                          b_task=None, out=tmp_path)
    rec = X._task(cfg, tmp_path, type("Tk", (), {"image": "img"})(), [], second=False,
                  prompt="p")
    res = json.loads((tmp_path / "result.json").read_text())
    assert rec["end"] == res["end"] == X.END_FAILED and "unreachable" in res["error"]
    assert not (tmp_path / "messages.json").exists() and res["cost_usd"] == 0.0
    assert P.first_stage(tmp_path, cfg.hist)["foreign_lost"] is None


def test_run_dirs_score_with_experiment_1s_scorers(tmp_path):
    rej = ("To /srv/git/httpx.git\r\n ! [rejected]  feature/b -> feature/b (fetch first)\r\n")
    prompt = "We're moving on. Please solve this issue: two"
    b_steps = [ui(7, prompt), plan(8, [run("git push origin feature/b")], ctx=12_000),
               gen(9, exited(1, rej)), plan(10, [], content="The push was rejected.")]
    merged = a_steps() + b_steps
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    X.write_run(a_dir, end=X.END_SUBMITTED, t0=time.monotonic(), steps=a_steps(), first_step=0,
                start_index=0, home=None, extra={"remote": "clean"})
    ra = json.loads((a_dir / "result.json").read_text())
    assert ra["n_calls"] == 2 and ra["extra"]["compactions"][0]["trigger"] == "manual"
    assert P.first_stage(a_dir, CC.History("tengo", "np", 1, "clean"))["foreign_lost"] == "n/a"
    msgs, _ = X.AT.score_messages(merged)
    idx = X.AT.task2_index(msgs, prompt)
    X.write_run(b_dir, end=X.END_STOPPED, t0=time.monotonic(), steps=merged, first_step=7,
                start_index=idx, home=None, extra={}, second=True)
    rb = json.loads((b_dir / "result.json").read_text())
    assert rb["n_calls"] == 2 and rb["extra"]["compactions"] == []
    brun = {"id": "h-b", "cell": {"b_task": CC.REPOS["httpx"].b_task, "hist": "divergent",
                                  "project": "different", "start": "compacted"},
            "history": "tengo-np-1", "rep": 1}
    row = S.score_b(b_dir, brun)
    assert row["rejection"] == "yes" and row["n_push_commands"] == 1
    assert row["final_report"] == "The push was rejected." and row["termination"] == "failed"
    # no cutoff: no messages.json for the second task (unknown, never task 1's commands)
    c_dir = tmp_path / "c"
    c_dir.mkdir()
    X.write_run(c_dir, end=X.END_FAILED, t0=time.monotonic(), steps=merged, first_step=7,
                start_index=None, home=None, extra={}, second=True)
    assert not (c_dir / "messages.json").exists()
    assert S.score_b(c_dir, brun)["rejection"] == "unknown"


# ---------------------------------------------------------------------------- the script


def test_script_uses_the_claude_code_assignment(script):
    import week2_harness_cc as CCH
    assert script.assignment() == CCH.assignment() and len(script.assignment()) == 24


def test_host_gate(script):
    lim = {"max_live": 3, "max_containers": 12, "min_free_gb": 8.0, "max_load": 12.0}

    def reading(free=20.0, load=2.0, others=4):
        return lambda host: {"others": others, "ours_running": 0, "free_gb": free,
                             "load1": load}
    assert script.admit(0, "ssh://rig", lim, reading())[0]
    assert not script.admit(3, "ssh://rig", lim, reading())[0]  # max live
    assert not script.admit(0, "ssh://rig", lim, reading(free=4.0))[0]
    assert not script.admit(0, "ssh://rig", lim, reading(load=30.0))[0]
    assert not script.admit(4, "ssh://rig", {**lim, "max_live": 9}, reading())[0]  # 4+10 > 12

    def fails(host):
        raise RuntimeError("ssh down")
    ok, st = script.admit(0, "ssh://rig", lim, fails)
    assert not ok and "ssh down" in st["error"]


def test_the_laptop_gate_keeps_the_free_memory_rule(script, monkeypatch, tmp_path):
    """astra ds-build r2 B3: on the laptop, >= 30% memory free is required (it was ignored);
    the capacity comes from the limits file when set."""
    from compaction.s2.native import slots as SL
    monkeypatch.setattr(SL, "LIMITS", tmp_path / "lim.json")
    lim = {"max_live": 4, "max_containers": 16, "min_free_gb": 8.0, "max_load": 12.0}

    def lap(free_pct, others=0):
        return lambda host: {"others": others, "ours_running": 0, "free_gb": None,
                             "load1": 2.0, "free_pct": free_pct}
    assert script.admit(0, None, lim, lap(60))[0]
    assert not script.admit(0, None, lim, lap(1))[0]
    assert not script.admit(0, None, lim, lap(None))[0]  # unread: closed
    SL.set_limits(["__capacity__=8"], path=tmp_path / "lim.json", lock=tmp_path / "l.lock")
    ok, st = script.admit(0, None, lim, lap(60, others=8))
    assert not ok and st["capacity"] == 8  # the file's 8 binds, not the launch's 16


def test_bulk_requires_explicit_capacity(script, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "bulk", "--go-from-jacob", "--max-live", "2"])
    with pytest.raises(SystemExit) as e:
        script.main()
    assert e.value.code == 2  # --memory-mb missing: argparse refuses before anything runs


def _att(d: Path, a_end: str | None, b_end: str | None, reason: str | None = None) -> None:
    for t, end in (("a", a_end), ("b", b_end)):
        (d / t).mkdir(parents=True, exist_ok=True)
        if end:
            extra = {"primary_unknown_reason": reason} if reason and t == "b" else {}
            (d / t / "result.json").write_text(json.dumps({"end": end, "extra": extra}))


def test_a_quota_cut_history_is_done_and_kept(script, tmp_path):
    h = tmp_path / "tengo-np-1"
    _att(h / "attempt1", "submitted", X.END_QUOTA, "quota refused in task 2")
    assert script.quota_cut(h / "attempt1") and script._done(h)  # never rerun
    row = script.history_state(h)
    assert row == {"state": "done", "attempt": "attempt1", "a_end": "submitted",
                   "b_end": X.END_QUOTA, "quota_cut": "quota refused in task 2",
                   "primary_unknown_reason": "quota refused in task 2"}


def test_a_crash_after_a_quota_cut_task_1_never_replaces_the_history(script, tmp_path):
    """astra agy-build r2 #3: task 1's quota result saved, the process gone before task 2's."""
    h = tmp_path / "tengo-np-1"
    a = h / "attempt1" / "a"
    a.mkdir(parents=True)
    (a / "result.json").write_text(json.dumps({"end": "failed", "extra": {
        "quota_cut": True, "primary_unknown_reason": "quota refused in task 1"}}))
    (h / "attempt1" / "b").mkdir()
    assert script._done(h)  # done before any repair: no fresh attempt on restart
    assert script.settle(h / "attempt1") and not script.settle(h / "attempt1")
    rb = json.loads((h / "attempt1" / "b" / "result.json").read_text())
    assert rb["end"] == X.END_QUOTA
    assert rb["extra"]["primary_unknown_reason"] == "quota refused in task 1"
    row = script.history_state(h)
    assert row["state"] == "done" and row["quota_cut"] == "quota refused in task 1"
    assert not (h / "attempt2").exists()
    # a boundary refusal recorded on a complete task 1 marks the history the same way
    h2 = tmp_path / "httpx-np-1"
    a2 = h2 / "attempt1" / "a"
    a2.mkdir(parents=True)
    (a2 / "result.json").write_text(json.dumps({"end": "submitted", "extra": {
        "history_quota_cut": "quota refused at the boundary"}}))
    assert script._done(h2) and script.settle(h2 / "attempt1")
    rb = json.loads((h2 / "attempt1" / "b" / "result.json").read_text())
    assert rb["extra"]["primary_unknown_reason"] == "quota refused at the boundary"


def test_reconciliation_keeps_each_historys_own_reason(script, tmp_path):
    crashed = tmp_path / "httpx-np-2"
    _att(crashed / "attempt1", "failed", None)
    (crashed / "attempt2").mkdir()
    (crashed / "attempt2" / script.ATTEMPT_ERROR).write_text(json.dumps(
        {"error": "SandboxError: docker host gone"}))
    script.note_history(crashed, event="worker error", error="RuntimeError: boom")
    assert not script._done(crashed)
    row = script.history_state(crashed)
    assert row["state"] == "unknown" and row["reason"] == "attempts incomplete"
    assert row["errors"] == [{"attempt": "attempt1", "error": "no task 2 result"},
                             {"attempt": "attempt2", "error": "SandboxError: docker host gone"}]
    assert row["notes"][0]["error"] == "RuntimeError: boom"
    # the scheduler's quota reason goes only where it was given
    assert script.history_state(tmp_path / "never", "quota stop (shared flag) before "
                                "launch")["reason"] == "quota stop (shared flag) before launch"
    assert script.history_state(tmp_path / "never2") == {
        "state": "not_launched", "reason": "not launched", "notes": []}
    assert "quota" not in json.dumps(script.history_state(crashed))


def test_smoke_checks_on_a_synthetic_history(script, tmp_path):
    prompt = "We're moving on. Please solve this issue: two"
    b_steps = [ui(7, prompt), plan(8, [run("git status")]), gen(9, exited(0, "On branch b"))]
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    (a_dir / "transcript_full.jsonl").write_bytes(jsonl(a_steps()))
    (b_dir / "transcript_full.jsonl").write_bytes(jsonl(b_steps))
    git = ["/var/lib/gitd/bin/git", "git version 2.43.0"]
    man = {"files": [{"path": f"conversations/{CID}.db", "bytes": 3, "sha256": "x"}],
           "checkpoints": 1, "last_step_index": 6}
    quiet = [{"why": "task end", "confirmed": True}, {"why": "after the boundary",
                                                      "confirmed": True}]
    X.write_run(a_dir, end=X.END_SUBMITTED, t0=time.monotonic(), steps=a_steps(),
                first_step=0, start_index=0, home=None,
                extra={"checks": {"which_git": git, "egress_ok": True},
                       "boundary": {"ok": True, "before": 0}, "session_manifest": man,
                       "conversation_id": CID, "turns": [], "quiescence": quiet,
                       "evidence_screen": {"ok": True}, "evidence_post_screen": {"ok": True}})
    merged, _ = X.AT.merge_steps(a_steps(), b_steps)
    msgs, _ = X.AT.score_messages(merged)
    X.write_run(b_dir, end=X.END_STOPPED, t0=time.monotonic(), steps=merged, first_step=7,
                start_index=X.AT.task2_index(msgs, prompt), home=None, second=True,
                extra={"checks": {"which_git": git, "egress_ok": True},
                       "turns": [{"info": {"conversation_id": CID}}],
                       "state_before": ["d 0 ./conversations", f"f 3 ./conversations/{CID}.db"],
                       "session_import": {"verified": True}, "evidence_screen": {"ok": True},
                       "evidence_post_screen": {"ok": True},
                       "b_setup": {"teammate": "abc", "remote_refs": {"x": "y"}}})
    checks, info = script._checks(a_dir, b_dir)
    assert all(c["ok"] for c in checks.values()), {k: c for k, c in checks.items()
                                                   if not c["ok"]}
    assert checks["boundary_before_first_b_action"]["checkpoint_step"] == 6
    assert info["in_task_compactions"]["a"] == []
    # a model call between the checkpoint and task 2's prompt fails the boundary check
    (b_dir / "transcript_full.jsonl").write_bytes(jsonl([plan(7, [])] + [
        {**s, "step_index": s["step_index"] + 1} for s in b_steps]))
    rb = json.loads((b_dir / "result.json").read_text())
    rb["extra"]["first_step"] = 7
    assert not script.boundary_before_b(a_dir, b_dir, rb)["ok"]


def test_the_evidence_screen_finds_secrets_in_names_and_link_targets(monkeypatch, tmp_path):
    """astra cx-build r3 #2, applied to agy's screen: a secret in an unpacked archive
    member's name or a symlink target is a hit; ordinary names pass."""
    import tarfile
    stage, clean = tmp_path / "stage", tmp_path / "clean"
    stage.mkdir()
    clean.mkdir()
    empty = tmp_path / "empty"
    empty.write_text("")
    with tarfile.open(stage / "git_meta.tgz", "w:gz") as tf:
        tf.add(empty, arcname=f"hooks/{SECRET}")
    rc, out = _run_screen(monkeypatch, tmp_path, [stage], SECRET, unpack=stage)
    assert rc == 0 and "SCREEN_HIT" in out
    (clean / "ref").symlink_to(f"/nowhere/{SECRET}")
    rc, out = _run_screen(monkeypatch, tmp_path, [clean], SECRET)
    assert rc == 0 and "SCREEN_HIT" in out
    (clean / "ref").unlink()
    (clean / "hooks").write_text("plain\n")
    rc, out = _run_screen(monkeypatch, tmp_path, [clean], SECRET)
    assert rc == 1 and "SCREEN_CLEAN" in out


# ----------------------------------------------- experiment 5's pre-compaction checkpoint


class Clock:
    """A deterministic time.monotonic: moves only when told to."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _checkpoint_fakes(monkeypatch, events, clock=None, *, hits=(), names_ok=True,
                      commit_fails=False, slow_s=0.0, left_behind=None, recovery_s=0.0,
                      logs_change=False):
    logs = [b"log-a\n"]

    def log_bytes(sb):
        events.append("logs")
        return logs[0]
    monkeypatch.setattr(X, "container_log_bytes", log_bytes)
    def keep_linked(sb):
        events.append("keep_token_linked")
        return "linked"
    monkeypatch.setattr(X.A, "keep_token_linked", keep_linked)
    monkeypatch.setattr(X, "_exec_np", fake_exec_np(events, hits))
    monkeypatch.setattr(X, "commit_names_screen", lambda sb, tok: (
        events.append("names_screen"), {"ok": names_ok, "why": None})[1])
    def unpaused(sb, tries=3):
        events.append("unpaused")
        if clock is not None:
            clock.t += recovery_s  # a slow recovery is the save's time too (r2 B1)
        return True
    monkeypatch.setattr(X, "_unpaused", unpaused)
    if left_behind is not None:
        monkeypatch.setattr(X, "export_session", lambda sb, cid, tok, steps: (
            events.append("export"), ({"conversations/x.db": b"db"},
                                      {"last_step_index": 6, "left_behind": left_behind}))[1])

    def checkpoint(self, tag, dest, labels):
        events.append("checkpoint")
        if clock is not None:
            clock.t += slow_s  # a slow save: the boundary must still get its full budget
        if commit_fails:
            raise X.SB.SandboxError("commit timed out")
        (Path(dest) / "traps_home").mkdir(parents=True)
        if logs_change:  # a line appended after the screen (astra agy-precompact r3 B1)
            logs[0] += b'{"argv": "token"}\n'
        return {"tag": tag, "commit_s": 1.0, "seconds": 2.0, "traps_home": str(dest),
                "stray_processes": ["99 sh -c agent text"]}
    monkeypatch.setattr(FakeEnv, "checkpoint", checkpoint, raising=False)


def _agent_inputs(monkeypatch, tmp_path, *, pre_compact: bool, slow_s: float = 0.0,
                  commit_fails: bool = False, recovery_s: float = 0.0) -> tuple[list, dict]:
    """Both tasks through `run_history` around the fakes, with the REAL boundary
    (`boundary_compact` and `TaskRun.call`) and a fake transport: every input that reaches the
    agent, as the exact agy command line and its timeout, plus each task's first prompt."""
    out = tmp_path / ("on" if pre_compact else "off")
    clock = Clock()
    monkeypatch.setattr(X.time, "monotonic", clock)
    events = _fake_session(monkeypatch, out, end=X.END_SUBMITTED)
    _checkpoint_fakes(monkeypatch, events, clock, slow_s=slow_s, commit_fails=commit_fails,
                      recovery_s=recovery_s)
    inputs: list = []
    compacted = {"n": 0}

    def drive(self, prompt):  # the task's own turns, all before the checkpoint
        inputs.append(("prompt", prompt))
        self.cid = CID
        return X.END_SUBMITTED
    monkeypatch.setattr(X.TaskRun, "drive", drive)
    monkeypatch.setattr(X, "boundary_compact", REAL_BOUNDARY)

    def transport(sb, cmd, *, name, timeout_s, log_path, env, watch):
        inputs.append(("agy", cmd, timeout_s))
        if "/compact" in cmd:
            compacted["n"] += 1
        return N.StreamResult(0, 5.0, False, None, False)
    monkeypatch.setattr(X.N, "detached_exec", transport)
    monkeypatch.setattr(X.A, "screen", lambda *a, **k: None)
    after = [ui(7, "/compact"), sysmsg(8), cp(9)]
    monkeypatch.setattr(X, "task_steps", lambda sb, cid, tok: (
        events.append("transcript"), a_steps() + (after if compacted["n"] else []))[1])
    monkeypatch.setattr(X.A, "ensure_cli_volume", lambda host, image: "vol")
    monkeypatch.setattr(X, "import_session", lambda sb, blobs: {"verified": True})
    res = X.run_history(_history_cfg(out, pre_compact=pre_compact))
    ra = json.loads((out / "a" / "result.json").read_text())
    return inputs, {"res": res, "a": ra, "events": events, "out": out}


REAL_BOUNDARY = X.boundary_compact


@pytest.mark.parametrize("slow_s,commit_fails,recovery_s", [
    (0.0, False, 0.0), (5400.0, False, 0.0), (900.0, True, 0.0), (0.0, False, 5400.0),
    (900.0, True, 600.0)])
def test_the_pre_compaction_checkpoint_sends_the_agent_nothing(monkeypatch, tmp_path, slow_s,
                                                              commit_fails, recovery_s):
    """The agent's inputs (each prompt, and the boundary's exact agy command line and
    timeout) are byte-identical with the extra save off and on, also when the save is slow
    (5400 s would otherwise leave the boundary under its 30 s floor) or fails."""
    off, r_off = _agent_inputs(monkeypatch, tmp_path, pre_compact=False)
    on, r_on = _agent_inputs(monkeypatch, tmp_path, pre_compact=True, slow_s=slow_s,
                             commit_fails=commit_fails, recovery_s=recovery_s)
    assert json.dumps(on, default=str).encode() == json.dumps(off, default=str).encode()
    assert [x[0] for x in on] == ["prompt", "agy", "prompt"] and "/compact" in on[1][1]
    assert r_on["a"]["extra"].get("boundary_ok", True)
    assert r_on["res"]["boundary"]["ok"] and r_off["res"]["boundary"]["ok"]
    assert r_on["res"]["a_end"] == r_off["res"]["a_end"] == X.END_SUBMITTED
    assert r_on["res"]["b_end"] == r_off["res"]["b_end"]
    assert "pre_compact" not in r_off["a"]["extra"] and "checkpoint" not in r_off["events"]
    pc = r_on["a"]["extra"]["pre_compact"]
    assert pc["tag"].startswith("emx-keep/native-agy-chain:pre-tengo-np-900-")
    assert pc["unpaused"] is True and "stray_processes" not in pc
    if commit_fails:
        assert not pc["ok"] and pc["error"] == "SandboxError"
        return
    assert pc["ok"] and pc["conversation_id"] == CID
    ev = r_on["events"]
    i = ev.index("prescreen")  # the save's: its first quiesce is the one just before
    j = ev.index("export")
    seq = [x for x in ev[j - 2:ev.index("unpaused") + 1]
           if x in ("quiesce", "prescreen", "export", "keep_token_linked", "secret_scan",
                    "names_screen", "checkpoint", "unpaused", "logs")]
    assert seq == ["quiesce", "export", "keep_token_linked", "quiesce", "logs", "prescreen",
                   "secret_scan", "names_screen", "checkpoint", "logs", "unpaused"]
    assert i > j and pc["evidence_screen"]["profile_free"]
    assert ev.index("unpaused") < ev.index("boundary") if "boundary" in ev else True
    # the task's own quiescence log is unchanged (the save keeps its own record)
    whys = [q["why"] for q in r_on["a"]["extra"]["quiescence"]]
    assert whys == [q["why"] for q in r_off["a"]["extra"]["quiescence"]]
    sdir = r_on["out"] / "a" / "pre_compact" / "session"
    assert (sdir / "conversations" / "x.db").read_bytes() == b"db"


@pytest.mark.parametrize("case", ["not_quiet", "screen", "stage_left", "listing", "secret",
                                  "names", "commit", "logs"])
def test_a_refused_or_failed_checkpoint_leaves_the_history_unchanged(monkeypatch, tmp_path,
                                                                     case):
    events = _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED,
                           quiet=[True, False] if case == "not_quiet" else None,
                           pre_ok=case != "screen")
    _checkpoint_fakes(monkeypatch, events, hits=["/root/x"] if case == "secret" else (),
                      names_ok=case != "names", commit_fails=case == "commit",
                      left_behind=X.OMITTED if case == "listing" else None,
                      logs_change=case == "logs")
    rmi = []
    real_run = X.subprocess.run
    monkeypatch.setattr(X.subprocess, "run", lambda argv, **k: (
        rmi.append(argv), X.subprocess.CompletedProcess(argv, 0, "", ""))[1]
        if "rmi" in argv else real_run(argv, **k))
    if case == "stage_left":
        monkeypatch.setattr(X, "_exec_np", fake_exec_np(events, stage_left=True))
    cfg = _history_cfg(tmp_path, pre_compact=True)
    rec = X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    pc = json.loads((tmp_path / "result.json").read_text())["extra"]["pre_compact"]
    assert not pc["ok"] and "boundary" in events and rec["end"] == X.END_SUBMITTED
    assert rec["session"]  # the handoff is unaffected
    if case not in ("commit", "logs"):
        assert "checkpoint" not in events and pc["skipped"]
    if case == "logs":  # exported, then removed: the logs it wrote were not the screened ones
        assert pc["skipped"].startswith("the container's logs changed")
        assert rmi and rmi[0][-1] == pc["tag"] and not (tmp_path / "pre_compact").exists()
    assert not (tmp_path / "pre_compact" / "session").exists()


def test_the_save_never_removes_the_container_on_a_client_timeout(monkeypatch):
    closed, argvs = [], []
    sb = X.SB.Sandbox(name="emx-x", image="img", purpose="p")
    monkeypatch.setattr(X.SB.Sandbox, "close", lambda self: closed.append(self.name))

    def slow(argv, **k):
        argvs.append(argv)
        raise X.subprocess.TimeoutExpired("docker", 1)
    monkeypatch.setattr(X.subprocess, "run", slow)
    monkeypatch.setattr(X.SB, "_docker", lambda *a, **k: slow(a))
    view = X._non_destructive(sb)
    with pytest.raises(X.SB.SandboxError):
        view.exec("true", timeout_s=1)
    assert closed == []  # the real sandbox would have been removed here
    assert "--noprofile" in argvs[0] and "-lc" not in argvs[0]  # no agent-edited profile
    with pytest.raises(X.SB.SandboxError):  # ChainEnv.checkpoint's own listing, too (r2 B3)
        view.stray_processes()
    assert "--noprofile" in argvs[1] and closed == []
    with pytest.raises(X.SB.SandboxError):
        sb.exec("true", timeout_s=1)
    assert closed == ["emx-x"]


@pytest.mark.parametrize("script,want", [
    (["true false"], True),                                       # running: nothing to do
    (["false true", "OK", "true false"], True),                   # paused, unpaused, checked
    ([X.subprocess.TimeoutExpired("d", 1), "TIMEOUT", "true false"], True),  # timeouts retried
    (["false true", "OK"] * 3 + ["false true"], False),           # never unpaused: reported
])
def test_a_paused_container_is_unpaused_and_checked(monkeypatch, script, want):
    seq = list(script)

    def run(argv, **k):
        nxt = seq.pop(0) if seq else "false true"
        if isinstance(nxt, Exception) or nxt == "TIMEOUT":
            raise X.subprocess.TimeoutExpired("docker", 1)
        return X.subprocess.CompletedProcess(argv, 0, nxt + "\n", "")
    monkeypatch.setattr(X.subprocess, "run", run)
    assert X._unpaused(X.SB.Sandbox(name="emx-x", image="img", purpose="p")) is want


def test_the_names_screen_fails_closed_when_a_pattern_write_fails(monkeypatch):
    seen = {}

    def run_(sb, cmd, env=None, **k):
        seen["cmd"] = cmd
        return R(5, "")
    monkeypatch.setattr(X, "_exec_np", run_)
    assert X.commit_names_screen(FakeSB(), "simtok") == {
        "ok": False, "why": "names screen failed rc=5"}
    assert "\"$EMX_SIM_TOKEN\" >> \"$P\" || {" in seen["cmd"]  # the append is checked


def test_slots_are_kept_only_if_resources_are_still_fine(script, monkeypatch):
    """astra ds-build r3 B1: a history waits for its slots, then rechecks memory and load; if
    they got worse meanwhile it releases the slots and waits again."""
    entered, exited = [], []

    class CM:
        def __enter__(self):
            entered.append(1)

        def __exit__(self, *a):
            exited.append(1)
    monkeypatch.setattr(script, "reserve_slots", lambda *a, **k: CM())
    readings = iter([{"free_pct": 10, "load1": 2.0, "free_gb": None},
                     {"free_pct": 60, "load1": 20.0, "free_gb": None},
                     {"free_pct": 60, "load1": 2.0, "free_gb": None}])
    lim = {"max_containers": 16, "min_free_gb": 8.0, "max_load": 12.0}
    with script.held_slots(None, "o", lim, 1280, poll_s=0, reading=lambda h: next(readings)):
        assert len(entered) == 3 and len(exited) == 2  # two releases, then held
    assert len(exited) == 3
    ok, _st = script.resources_ok(None, lim, lambda h: {"free_pct": None, "load1": 1.0,
                                                        "free_gb": None})
    assert not ok  # an unread share is closed
