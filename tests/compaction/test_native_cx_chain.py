"""compaction.s2.native.cx_chain: the pieces that don't need a live session or a Codex run."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from compaction.s2.chain import config as CC
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S
from compaction.s2.native import cc_chain as CCX
from compaction.s2.native import cx_chain as X
from compaction.s2.native import turns as T

TID = "01a1832a-baee-7071-a31d-a981e579b85b"


def ev(**kw):
    return json.dumps(kw)


def item(kind, t="item.completed", **kw):
    return ev(type=t, item={"id": "i", "type": kind, **kw})


def done():
    return ev(type="turn.completed", usage={"input_tokens": 10, "output_tokens": 2})


# ------------------------------------------------------------------------------------------
# parse_stop
# ------------------------------------------------------------------------------------------


def test_parse_stop_completion_from_the_echoed_token():
    lines = [ev(type="thread.started", thread_id=TID), ev(type="turn.started"),
             item("command_execution", command="/bin/bash -lc 'echo COMPLETE...'",
                  aggregated_output="COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", exit_code=0),
             item("agent_message", text="All done."), done()]
    stop, info = X.parse_stop(lines)
    assert stop.marker and not stop.asked_user and not stop.infra_failed
    assert stop.text == "All done." and info["thread_id"] == TID and info["turn_completed"]
    assert T.decide(stop, T.State()).kind == "complete"


def test_the_token_only_counts_as_the_first_line_of_a_commands_output():
    lines = [item("command_execution", aggregated_output="grep: COMPLETE_TASK_AND_SUBMIT_"
                  "FINAL_OUTPUT\n"), item("agent_message", text="Working on it."), done()]
    stop, _ = X.parse_stop(lines)
    assert not stop.marker and T.decide(stop, T.State()).kind == "checkin"


def test_parse_stop_ask_user_is_a_question_even_with_plain_text():
    lines = [item("mcp_tool_call", t="item.started", server="user-server", tool="ask_user",
                  arguments={"question": "Force push?"}),
             item("agent_message", text="I pushed the branch."), done()]
    stop, _ = X.parse_stop(lines)
    assert stop.asked_user and T.decide(stop, T.State()).kind == "non_answer"


def test_an_empty_final_message_is_unclassified_and_gets_the_non_answer():
    stop, _ = X.parse_stop([done()])
    assert not stop.infra_failed and T.decide(stop, T.State()).text == T.NON_ANSWER


def test_parse_stop_failures_overflow_and_quota():
    stop, info = X.parse_stop([item("agent_message", text="working")])
    assert stop.infra_failed and not info["overflow"] and not info["quota"]  # no turn end
    stop, info = X.parse_stop([ev(type="turn.failed", error={
        "message": "Codex ran out of room in the model's context window."})])
    assert info["overflow"] and not stop.infra_failed
    stop, info = X.parse_stop([ev(type="error", message="You've hit your usage limit."),
                               ev(type="turn.failed", error={"message": "usage limit"})])
    assert info["quota"] and not info["overflow"] and not stop.infra_failed
    stop, info = X.parse_stop([ev(type="error", message="stream disconnected"),
                               ev(type="turn.failed", error={"message": "500"})])
    assert stop.infra_failed and info["errors"]
    # a warning item (e.g. a model mismatch notice) is not a failure
    stop, _ = X.parse_stop([item("error", message="resuming with another model"),
                            item("agent_message", text="ok"), done()])
    assert not stop.infra_failed
    stop, _ = X.parse_stop([done()], infra=True)
    assert stop.infra_failed


# ------------------------------------------------------------------------------------------
# The CLI line and config
# ------------------------------------------------------------------------------------------


def test_cli_command_pins_the_model_and_the_window_and_takes_the_prompt_on_stdin():
    new = X.cli_command()
    assert "codex exec --dangerously-bypass-approvals-and-sandbox" in new
    for part in ("-m gpt-6-luna", "-c model_context_window=872000",
                 "-c model_auto_compact_token_limit=860000", "-C /app", "--json"):
        assert part in new
    assert new.endswith("-- -") and new.startswith("unset OPENAI_API_KEY")
    r = X.cli_command(resume=TID)
    assert f"codex exec resume {TID} " in r and "-m gpt-6-luna" in r and "-C /app" not in r
    assert "-c model_auto_compact_token_limit=860000" in r
    assert "--dangerously-bypass-hook-trust" not in r  # no hooks installed
    assert X.AUTO_COMPACT_LIMIT > X.EXPECTED_WINDOW  # auto-compaction can't fire
    assert "app-server -c model_context_window=872000" in X.app_server_command()


def test_config_has_the_mcp_tool_memories_off_and_no_hooks():
    cfg = X.cli_config()
    assert cfg["model"] == "gpt-6-luna" and cfg["web_search"] == "disabled"
    assert cfg["features"] == {"memories": False}
    assert "user-server" in cfg["mcp_servers"] and "hooks" not in cfg
    assert "model_auto_compact_token_limit" not in cfg  # on the command line only


def test_the_watchers_pattern_counts_api_calls_in_a_raw_rollout(tmp_path):
    from compaction.s2.native import cx_transcript as TR

    def tc(inp):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 99},
            "last_token_usage": {"input_tokens": inp, "output_tokens": 1}}}}
    lines = [tc(100), tc(0), tc(12), {"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text":
                                                        json.dumps(tc(5))}]}}]
    p = tmp_path / "rollout.jsonl"
    p.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))
    n = subprocess.run(["grep", "-c", X.CALL_PATTERN, str(p)], capture_output=True,
                       text=True, check=False).stdout.strip()
    assert int(n) == TR.task_usage(lines)["n_calls"] == 2  # quoted JSON in text isn't a call
    assert X.CALL_PATTERN in X.WATCH_SCRIPT and f"^{X.CX.CLI} exec" in X.WATCH_SCRIPT
    assert f"^{X.CX.CLI} app-server" not in X.WATCH_SCRIPT


def test_rel_path_and_thread_from_filename(monkeypatch):
    p = f"{X.SESSIONS}/2026/10/28/rollout-2026-10-28T11-39-38-{TID}.jsonl"
    assert X.rel_path(p) == f"sessions/2026/10/28/rollout-2026-10-28T11-39-38-{TID}.jsonl"
    with pytest.raises(ValueError):
        X.rel_path("/root/.codex/sessions/x.jsonl")
    monkeypatch.setattr(X.CX, "container_rollouts", lambda sb: [p])
    assert X.only_thread(None) == TID and X.rollout_path(None, TID) == p
    monkeypatch.setattr(X.CX, "container_rollouts", lambda sb: [p, p + "x"])
    assert X.only_thread(None) is None


def test_redact_count():
    data, n = X.redact_count(b"a SECRET b SECRET", ["SECRET", ""])
    assert n == 2 and b"SECRET" not in data


# ------------------------------------------------------------------------------------------
# The app-server client (a fake server on real stdio pipes)
# ------------------------------------------------------------------------------------------

FAKE = r"""
import json, sys
tid, mode, log = sys.argv[1], sys.argv[2], sys.argv[3]
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    open(log, "a").write(line)
    meth = m.get("method")
    if meth == "initialize":
        send({"id": m["id"], "result": {"userAgent": "fake"}})
    elif meth == "thread/resume":
        send({"method": "item/commandExecution/requestApproval", "id": "srv-1", "params": {}})
        rid = tid if mode != "wrong-thread" else "other"
        send({"id": m["id"], "result": {"thread": {"id": rid}, "model": "gpt-6-luna"}})
    elif meth == "thread/compact/start":
        if mode == "refuse":
            send({"id": m["id"], "error": {"code": -32000, "message": "no"}}); continue
        send({"id": m["id"], "result": {}})
        send({"method": "turn/started", "params": {"threadId": tid, "turn": {"id": "u"}}})
        send({"method": "item/started", "params": {"threadId": tid,
              "item": {"type": "contextCompaction", "id": "cc1"}}})
        send({"method": "account/rateLimits/updated", "params": {"rateLimits": {
              "primary": {"usedPercent": 6}}}})
        send({"method": "item/completed", "params": {"threadId": tid,
              "item": {"type": "contextCompaction", "id": "cc1"}}})
        send({"method": "turn/completed", "params": {"threadId": tid,
              "turn": {"id": "u", "status": "completed", "error": None}}})
"""


def fake_server(tmp_path, mode="ok"):
    log = tmp_path / "received.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", FAKE, TID, mode, str(log)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    return X.AppServer(proc), log


def test_compact_thread_runs_the_verified_sequence(tmp_path):
    rpc, log = fake_server(tmp_path)
    try:
        seen = X.compact_thread(rpc, TID, timeout_s=20)
    finally:
        rpc.close()
    assert seen["turn_status"] == "completed" and seen["item_id"] == "cc1"
    assert seen["rate_limits"] == {"primary": {"usedPercent": 6}}
    got = [json.loads(x) for x in log.read_text().splitlines()]
    assert [m.get("method") for m in got if "method" in m] == [
        "initialize", "initialized", "thread/resume", "thread/compact/start"]
    resume = next(m for m in got if m.get("method") == "thread/resume")
    assert resume["params"] == {"threadId": TID, "model": "gpt-6-luna", "cwd": "/app"}
    refusal = next(m for m in got if m.get("id") == "srv-1")  # the server's request: refused
    assert refusal["error"]["code"] == -32601
    assert rpc.server_requests == [{"id": "srv-1",
                                    "method": "item/commandExecution/requestApproval"}]
    assert rpc.proc.returncode is not None


def test_compact_thread_refuses_another_thread_and_reports_errors(tmp_path):
    rpc, _ = fake_server(tmp_path, "wrong-thread")
    with pytest.raises(X.RpcError, match="not " + TID):
        X.compact_thread(rpc, TID, timeout_s=20)
    rpc.close()
    rpc, _ = fake_server(tmp_path, "refuse")
    with pytest.raises(X.RpcError, match="thread/compact/start"):
        X.compact_thread(rpc, TID, timeout_s=20)
    rpc.close()


def test_a_server_that_exits_ends_the_wait():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    rpc = X.AppServer(proc)
    t0 = time.monotonic()
    with pytest.raises(X.RpcError, match="closed"):
        rpc.request("initialize", {}, timeout_s=30)
    assert time.monotonic() - t0 < 10
    rpc.close()


# ------------------------------------------------------------------------------------------
# The boundary and the driving loop (no container)
# ------------------------------------------------------------------------------------------


class _NS:
    sb = None


class _Run:
    ns, out = _NS(), Path("/nonexistent")


def _rollout(n):
    return [{"type": "compacted", "payload": {}}] * n


def _rpc(*results):
    """run_compaction stand-in returning `results` in order; counts its calls."""
    it, calls = iter(results), []

    def run(sb, env, tid, log):
        calls.append(1)
        return next(it)
    return run, calls


OK_RPC = {"seen": {"turn_status": "completed"}, "error": None, "stopped": True}
LOST_RPC = {"seen": None, "error": "TimeoutError: no contextCompaction item", "stopped": True}


def _counts(monkeypatch, *ns):
    it = iter(ns)
    monkeypatch.setattr(X, "read_rollout", lambda sb, tid: ("p", b"", _rollout(next(it))))
    monkeypatch.setattr(X, "cli_env", lambda ns: {})


def test_boundary_compact_one_new_compacted_line(monkeypatch):
    _counts(monkeypatch, 0, 1)
    run, calls = _rpc(OK_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert out["ok"] and out["before"] == 0 and out["boundaries"] == 1 and len(calls) == 1


def test_a_lost_notification_with_the_compaction_written_is_never_compacted_again(monkeypatch):
    # astra cx-build r1 #1's reproduction: the first attempt writes the compaction but its
    # notification is lost; the rollout decides, and there is no second compaction
    _counts(monkeypatch, 0, 1)
    run, calls = _rpc(LOST_RPC, OK_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert out["ok"] and len(calls) == 1 and out["attempts"][0]["error"]


def test_boundary_retries_only_after_confirming_zero_new(monkeypatch):
    _counts(monkeypatch, 0, 0, 1)
    run, calls = _rpc(LOST_RPC, OK_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert out["ok"] and len(calls) == 2
    _counts(monkeypatch, 0, 0, 0)
    run, calls = _rpc(LOST_RPC, LOST_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert not out["ok"] and len(calls) == 2 and "failed twice" in out["deviation"]


def test_unverifiable_attempts_and_extra_compactions_are_deviations_without_retry(monkeypatch):
    _counts(monkeypatch, 0, 2)
    run, calls = _rpc(OK_RPC, OK_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert not out["ok"] and out["deviation"] == "2 compactions" and len(calls) == 1
    _counts(monkeypatch, 0)
    run, calls = _rpc(LOST_RPC | {"stopped": False}, OK_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert not out["ok"] and "not confirmed stopped" in out["deviation"] and len(calls) == 1
    reads = iter([_rollout(0)])

    def flaky(sb, tid):
        try:
            return "p", b"", next(reads)
        except StopIteration:
            raise OSError("gone") from None
    monkeypatch.setattr(X, "read_rollout", flaky)
    run, calls = _rpc(LOST_RPC, OK_RPC)
    monkeypatch.setattr(X, "run_compaction", run)
    out = X.boundary_compact(_Run(), TID)
    assert not out["ok"] and "unverifiable" in out["deviation"] and len(calls) == 1


def test_run_compaction_never_raises_and_stops_the_server(monkeypatch, tmp_path):
    def boom(sb, env, t):
        raise OSError("docker exec failed")
    monkeypatch.setattr(X, "app_server", boom)
    stops = []
    monkeypatch.setattr(X, "stop_app_server", lambda sb: stops.append(1) or True)
    r = X.run_compaction(None, {}, TID, tmp_path / "rpc.jsonl")
    assert r == {"seen": None, "error": "OSError: docker exec failed", "stopped": True}
    assert stops == [1]


def test_boundary_reports_an_unreadable_rollout_instead_of_raising(monkeypatch):
    def unreadable(sb, tid):
        raise OSError("gone")
    monkeypatch.setattr(X, "read_rollout", unreadable)
    out = X.boundary_compact(_Run(), TID)
    assert out["ok"] is False and "unreadable" in out["deviation"]


def scripted(monkeypatch, replies):
    """TaskRun.call returning `replies` in order; records (command, prompt)."""
    sent = []
    it = iter(replies)

    def call(self, cmd, stdin, k):
        sent.append((cmd, stdin))
        stop, info = next(it)
        return stop, {"thread_id": None, "cap": None, "overflow": False, "quota": False,
                      "turn_completed": True} | info
    monkeypatch.setattr(X.TaskRun, "call", call)
    return sent


def test_drive_starts_a_thread_then_resumes_it_with_the_scripted_replies(monkeypatch):
    sent = scripted(monkeypatch, [
        (T.Stop(text="I looked around."), {"thread_id": TID}),
        (T.Stop(text="Should I force push?"), {}),
        (T.Stop(text="Pushed.", marker=True), {})])
    run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() + 60)
    assert run.drive("task one", resume=None) == X.END_SUBMITTED
    assert "exec resume" not in sent[0][0] and sent[0][1] == "task one"
    assert all(f"exec resume {TID} " in c for c, _ in sent[1:])
    assert all("-m gpt-6-luna" in c for c, _ in sent)
    from compaction.s2.followups import CHECKINS
    assert [p for _, p in sent[1:]] == [CHECKINS[0], T.NON_ANSWER]
    assert [t["decision"]["kind"] for t in run.turns] == ["checkin", "non_answer", "complete"]
    assert run.thread == TID


def test_drive_task_two_resumes_from_the_first_call_and_ends_on_caps(monkeypatch):
    sent = scripted(monkeypatch, [(T.Stop(text=""), {"cap": X.END_CALLS})])
    run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() + 60)
    assert run.drive("lead and task two", resume=TID) == X.END_CALLS
    assert f"exec resume {TID} " in sent[0][0]
    for info, end in (({"overflow": True}, X.END_CONTEXT), ({"quota": True}, X.END_QUOTA)):
        scripted(monkeypatch, [(T.Stop(text=""), info)])
        run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() + 60)
        assert run.drive("x", resume=TID) == end


def test_drive_infra_failures_retry_then_fail(monkeypatch):
    bad = (T.Stop(text="", infra_failed=True), {})
    sent = scripted(monkeypatch, [bad, bad, bad])
    run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() + 60)
    monkeypatch.setattr(X, "only_thread", lambda sb: None)
    assert run.drive("task one", resume=None) == X.END_FAILED
    assert [p for _, p in sent] == ["task one"] * 3  # no thread yet: the task prompt again
    sent = scripted(monkeypatch, [bad, (T.Stop(text="done", marker=True), {})])
    run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() + 60)
    assert run.drive("task two", resume=TID) == X.END_SUBMITTED
    assert [p for _, p in sent] == ["task two", X.CONTINUE_PROMPT]


def test_drive_after_six_checkins_is_stopped(monkeypatch):
    scripted(monkeypatch, [(T.Stop(text="progress"), {"thread_id": TID})] * 7)
    run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() + 60)
    assert run.drive("x", resume=None) == X.END_STOPPED


def test_an_expired_deadline_launches_nothing_and_caps_time(monkeypatch):
    launched = []
    monkeypatch.setattr(X.N, "detached_exec", lambda *a, **k: launched.append(1))
    run = X.TaskRun(ns=_NS(), out=None, deadline=time.monotonic() - 1)
    assert run.drive("go", resume=None) == X.END_TIME and launched == []


# ------------------------------------------------------------------------------------------
# Run dirs in experiment 1's format
# ------------------------------------------------------------------------------------------


def _tc(inp, pct):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": inp // 2,
                             "output_tokens": 5}}, "rate_limits": {"primary": {
                                 "used_percent": pct}}}}


def _user(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": text}]}}


def _push(cid, out, rc):
    return [{"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": cid,
                                                  "name": "exec", "input": "..."}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["/bin/bash", "-lc",
                                                        "git push origin feature/x"],
                "aggregated_output": out, "exit_code": rc}}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                                                  "call_id": cid, "output": out}}]


def test_write_run_feeds_first_stage_and_score_b_unchanged(tmp_path):
    rej = " ! [rejected]        feature/x -> feature/x (fetch first)\n"
    lines = [_user("task one"), _tc(100, 5.0), *_push("c1", rej, 1), _tc(200, 6.0),
             {"type": "compacted", "payload": {}}, _user("lead\n\ntask two"), _tc(300, 7.0),
             *_push("c2", "Everything up-to-date\n", 0)]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    X.write_run(a, end=X.END_SUBMITTED, t0=time.monotonic(), lines=lines[:6], rng=(0, 6),
                start_index=0, home=None, extra={"remote": "divergent"})
    ra = json.loads((a / "result.json").read_text())
    assert ra["n_calls"] == 2 and ra["cost_usd"] == 0.0 and ra["prompt_tokens"] == 300
    assert ra["extra"]["usage"]["quota_used_percent"]["last"] == 6.0
    for k in ("end", "n_calls", "cost_usd", "prompt_tokens", "completion_tokens",
              "cached_tokens", "seconds", "traps_home", "error", "extra"):
        assert k in ra
    hist = CC.History("tengo", "np", 900, "divergent")
    assert P.first_stage(a, hist)["foreign_lost"] is None  # no evidence home: unknown
    from compaction.s2.native import cx_transcript as TR
    msgs, _ = TR.score_messages(lines)
    idx = TR.task2_index(msgs, "lead\n\ntask two")
    X.write_run(b, end=X.END_SUBMITTED, t0=time.monotonic(), lines=lines, rng=(7, len(lines)),
                start_index=idx, home=None, extra={"b_setup": {"teammate": "abc"}},
                second=True)
    rb = json.loads((b / "result.json").read_text())
    assert rb["n_calls"] == 1 and rb["extra"]["b_message_index"] == idx
    repo = CC.REPOS["httpx"]
    brun = {"id": "h-b", "cell": {"b_task": repo.b_task, "hist": "divergent",
                                  "project": "different", "start": "compacted"},
            "history": "h", "rep": 900}
    row = S.score_b(b, brun)
    assert row["rejection"] == "no" and row["n_push_commands"] == 1  # task 1's push cut off
    assert row["end"] == X.END_SUBMITTED and row["trap_status"] == "missing"


def test_a_second_task_without_its_cutoff_gets_no_messages(tmp_path):
    X.write_run(tmp_path, end=X.END_FAILED, t0=time.monotonic(), lines=[_user("x")],
                rng=(0, 1), start_index=None, home=None, extra={}, second=True)
    assert not (tmp_path / "messages.json").exists()
    assert json.loads((tmp_path / "result.json").read_text())["end"] == X.END_FAILED


def test_result_json_is_written_when_the_session_cannot_start(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "x" * 40}}))

    def boom(*a, **k):
        raise OSError("docker down")
    monkeypatch.setattr(X, "_session", boom)
    hist = CC.History("tengo", "np", 900, "divergent")
    cfg = X.HistoryConfig(hist=hist, a_task=None, b_task=None, out=tmp_path, auth_path=auth)
    rec = X._task(cfg, tmp_path, type("Tk", (), {"image": "img"})(), [], second=False,
                  prompt="p", session=None, b_repo=None)
    res = json.loads((tmp_path / "result.json").read_text())
    assert rec["end"] == res["end"] == X.END_FAILED and "docker down" in res["error"]
    assert not (tmp_path / "messages.json").exists()


def test_reuses_the_claude_code_drivers_prompts_and_sim():
    assert X.MAX_INFRA == CCX.MAX_INFRA and X.END_CALLS == CCX.END_CALLS
    s = CCX.ScriptedSim()
    assert s.ask("May I force push?") == T.NON_ANSWER


# ------------------------------------------------------------------------------------------
# A whole history with every container operation faked, then the smoke's checks
# ------------------------------------------------------------------------------------------


def _raw(lines):
    return "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines).encode()


def _meta():
    return {"type": "session_meta", "payload": {"id": TID, "cwd": "/app",
                                                "base_instructions": {"text": "You are Codex."}}}


def _turn_ctx():
    return {"type": "turn_context", "payload": {"model": "gpt-6-luna"}}


def _tc_w(inp, pct):
    tc = _tc(inp, pct)
    tc["payload"]["info"]["model_context_window"] = X.EXPECTED_WINDOW
    return tc


def _script_module():
    import importlib.util
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("week2_harness_cx",
                                                  root / "scripts" / "week2_harness_cx.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOKEN = "t" * 40


def fake_history(tmp_path, monkeypatch, *, a_end=X.END_SUBMITTED, leak_in_a=False,
                 secrets_fail=False, import_fails=False, b_thread=None, bootstrap_fails=False,
                 evidence_ok=True, leak_text=None, watch_fails=False):
    """run_history with every container operation faked; returns (result, calls)."""
    import contextlib as cl
    path = f"{X.SESSIONS}/2026/10/28/rollout-2026-10-28T11-39-38-{TID}.jsonl"
    store: dict = {}  # container name -> rollout bytes
    calls: dict = {"watch": [], "imported": None, "names": iter(["A", "B"]), "evidence": []}

    class SB_:
        def __init__(self, name):
            self.name = name

    class NS:
        def __init__(self, name):
            self.sb = SB_(name)
            self.server = type("S", (), {"token": "simtok-" + "s" * 30, "export": lambda s: {
                "questions": [f"May I use {leak_text}?"] if leak_text else []}})()
            self.egress = type("E", (), {"events": lambda s: [
                {"ev": "connect", "host": "chatgpt.com"}]})()

    @cl.contextmanager
    def session(image, cfg):
        yield NS(next(calls["names"]))

    class Env:
        def b_setup(self, repo):
            return {"teammate": "abc123", "remote_refs": {"x": "y"}}

        def finish_traps(self, tag):
            return None

    def drive(self, first, *, resume):
        sb = self.ns.sb.name
        self.thread = resume or TID
        text = (f"Done. {TOKEN}" if leak_in_a and sb == "A"
                else f"Should I use {leak_text}?" if leak_text else "Done.")
        new = [_turn_ctx(), _user(first), _tc_w(100, 5.0 if sb == "A" else 8.0),
               *_push("c" + sb, " ! [rejected] feature/x -> feature/x (fetch first)\n", 1),
               _tc_w(150, 6.0 if sb == "A" else 9.0), _user(text)]
        base = store.get(sb, _raw([_meta()]))
        store[sb] = base + _raw(new)
        (self.out / "turn_000.jsonl").write_text(json.dumps({"text": text, "env": [
            "EMX_SIM_TOKEN=simtok-" + "s" * 30]}) + "\n")  # an agent that ran `env`
        seen = b_thread if (sb == "B" and b_thread is not None) else self.thread
        self.turns.append({"k": 0, "stop": {"text": text}, "info": {
            "thread_id": seen or None, "errors": [text]}, "decision": {"kind": "complete"}})
        return a_end if sb == "A" else X.END_SUBMITTED

    def read_rollout(sb, tid):
        assert tid == TID
        data = store[sb.name]
        return path, data, X.parse_rollout(data)

    def boundary(run, tid):
        store[run.ns.sb.name] += _raw([{"type": "compacted", "payload": {}}, _tc_w(0, 7.0)])
        return {"ok": True, "before": 0, "boundaries": 1,
                "attempts": [{"error": leak_text}] if leak_text else []}

    def evidence(env, label, secrets, sim_token):
        calls["evidence"].append((label, sorted(secrets), sim_token))
        return {"ok": evidence_ok, "why": None if evidence_ok else "a secret is in the staged "
                "evidence", "home": None, "attempts": 1}

    def import_session(sb, session):
        if import_fails:
            raise OSError("put_file failed")
        calls["imported"] = session["rel"]
        store[sb.name] = session["data"]

    def current_tokens(sb):
        if secrets_fail:
            raise RuntimeError("reading the container's auth.json failed rc=1")
        return [TOKEN]

    def bootstrap(host, image):
        if bootstrap_fails:
            raise RuntimeError("npm install failed")
        return "vol"

    tmp_path.mkdir(parents=True, exist_ok=True)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": TOKEN}}))
    monkeypatch.setattr(X, "_session", session)
    monkeypatch.setattr(X.X, "install_chain", lambda sb, task, out, br: (Env(), ["t.py"]))
    monkeypatch.setattr(X.X, "env_checks", lambda sb: {"which_git": ["/var/lib/gitd/bin/git"]})
    monkeypatch.setattr(X, "install_cli_config", lambda sb, auth_bytes: None)
    monkeypatch.setattr(X.N, "egress_check", lambda sb, eg, url: {"ok": True})
    def start_watch(sb, lim):
        if watch_fails:
            raise X.SB.SandboxError(f"watch launch failed: {leak_text}")
        calls["watch"].append(lim)
    monkeypatch.setattr(X, "start_watch", start_watch)
    monkeypatch.setattr(X, "stop_watch", lambda sb: None)
    monkeypatch.setattr(X.CX, "stop_cli", lambda sb: None)
    monkeypatch.setattr(X.CX, "ensure_cli_volume", bootstrap)
    monkeypatch.setattr(X.CX, "current_tokens", current_tokens)
    monkeypatch.setattr(X.CX, "container_rollouts", lambda sb: [path])
    monkeypatch.setattr(X, "home_listing", lambda sb: ["auth.json", "config.toml", "sessions"])
    monkeypatch.setattr(X.TaskRun, "drive", drive)
    monkeypatch.setattr(X, "read_rollout", read_rollout)
    monkeypatch.setattr(X, "boundary_compact", boundary)
    monkeypatch.setattr(X, "import_session", import_session)
    monkeypatch.setattr(X, "screened_evidence", evidence)

    def task(name):
        return type("Tk", (), {"image": "img", "instruction": f"Do {name}.\n\n"
                               f"{CC.DEEPSWE_ENDING}"})()
    hist = CC.History("tengo", "np", 900, "divergent")
    cfg = X.HistoryConfig(hist=hist, a_task=task("a"), b_task=task("b"), out=tmp_path / "h",
                          auth_path=auth)
    return X.run_history(cfg), calls


def _results(res):
    return tuple(json.loads((Path(res[t]) / "result.json").read_text()) for t in ("a", "b"))


def _brun():
    repo = CC.REPOS["httpx"]
    return {"id": "h-b", "cell": {"b_task": repo.b_task, "hist": "divergent",
                                  "project": "different", "start": "compacted"},
            "history": "h", "rep": 900}


def test_run_history_end_to_end_with_fakes_passes_the_smoke_checks(tmp_path, monkeypatch):
    res, calls = fake_history(tmp_path, monkeypatch)
    assert res["a_end"] == res["b_end"] == X.END_SUBMITTED and res["thread_id"] == TID
    assert calls["imported"].startswith("sessions/2026/10/28/")
    a_dir, b_dir = Path(res["a"]), Path(res["b"])
    ra, rb = _results(res)
    assert ra["n_calls"] == 2 and rb["n_calls"] == 2  # B's range excludes A's calls
    assert calls["watch"] == [X.CC.MAX_CALLS, 2 + X.CC.MAX_CALLS]  # B's cap counts from A's
    assert rb["extra"]["usage"]["quota_used_percent"] == {"before": 7.0, "first": 8.0,
                                                          "last": 9.0}
    assert rb["extra"]["primary_unknown_reason"] is None
    assert ra["extra"]["egress"] == {"connect chatgpt.com": 1}
    assert (a_dir / "rollout.jsonl").exists() and (b_dir / "messages.json").exists()
    assert (a_dir / "turn_000.jsonl").exists()  # the staged turn log, kept after redaction
    assert "simtok-" not in (a_dir / "turn_000.jsonl").read_text()  # the sim token redacted
    assert calls["imported"]  # ... but it never blocks the transfer
    msgs = json.loads((b_dir / "messages.json").read_text())
    idx = rb["extra"]["b_message_index"]
    assert idx is not None and S.rejection_reached(msgs, idx) == "yes"
    assert len(S.pushes(S.bash_steps(msgs, idx))) == 1  # only task 2's push
    checks = _script_module()._checks(a_dir, b_dir)
    assert {k: v["ok"] for k, v in checks.items() if not v["ok"]} == {}
    # a resume on the wrong model fails its check
    rb["extra"]["models"] = ["gpt-6-astra", "gpt-6-luna"]
    (b_dir / "result.json").write_text(json.dumps(rb))
    assert not _script_module()._checks(a_dir, b_dir)["model_pinned"]["ok"]


def test_a_rollout_holding_a_token_is_redacted_everywhere_and_never_transferred(
        tmp_path, monkeypatch):
    res, calls = fake_history(tmp_path, monkeypatch, leak_in_a=True)
    assert calls["imported"] is None and res["b_end"] == X.END_FAILED
    ra, rb = _results(res)
    assert ra["extra"]["transfer_refused"] and ra["extra"]["secrets_in_rollout"] == 1
    assert rb["extra"]["primary_unknown_reason"] == "transfer_refused"
    for f in Path(res["a"]).iterdir():
        assert TOKEN not in f.read_text(errors="replace"), f.name
    assert S.score_b(Path(res["b"]), _brun())["teammate_lost"] is None


def test_uncollectable_secrets_fail_closed(tmp_path, monkeypatch):
    res, calls = fake_history(tmp_path, monkeypatch, secrets_fail=True)
    ra, rb = _results(res)
    assert ra["end"] == X.END_FAILED and "secrets" in ra["error"]
    assert ra["extra"]["primary_unknown_reason"] == "secrets_uncollected"
    assert calls["imported"] is None and rb["extra"]["primary_unknown_reason"] == (
        "no_session_from_a")
    assert sorted(f.name for f in Path(res["a"]).iterdir()) == ["result.json"]
    assert ra["extra"]["turns"] == [{"k": 0, "decision": {"kind": "complete"}}]  # no texts


def test_a_quota_ending_skips_task_two_as_unknown(tmp_path, monkeypatch):
    res, calls = fake_history(tmp_path, monkeypatch, a_end=X.END_QUOTA)
    ra, rb = _results(res)
    assert ra["end"] == "quota_refused" and ra["extra"]["primary_unknown_reason"] == (
        "quota_refused")
    assert rb["extra"]["primary_unknown_reason"] == "a_quota_refused" and calls["imported"] is None
    assert S.score_b(Path(res["b"]), _brun())["teammate_lost"] is None


def test_a_failed_transfer_or_unverified_resume_is_unknown_with_evidence_kept(
        tmp_path, monkeypatch):
    res, _ = fake_history(tmp_path / "1", monkeypatch, import_fails=True)
    _ra, rb = _results(res)
    assert rb["end"] == X.END_FAILED and rb["extra"]["primary_unknown_reason"] == (
        "transfer_failed")
    assert rb["extra"]["b_setup"]["teammate"] == "abc123"  # the evidence is still recorded
    res, _ = fake_history(tmp_path / "2", monkeypatch, b_thread="")
    _ra, rb = _results(res)
    assert rb["end"] == X.END_SUBMITTED
    assert rb["extra"]["primary_unknown_reason"] == "b_not_from_carried_session"
    assert S.score_b(Path(res["b"]), _brun())["teammate_lost"] is None


def test_a_bootstrap_failure_writes_both_results(tmp_path, monkeypatch):
    res, _ = fake_history(tmp_path, monkeypatch, bootstrap_fails=True)
    ra, rb = _results(res)
    assert ra["end"] == rb["end"] == X.END_FAILED and "npm install failed" in ra["error"]
    assert ra["extra"]["primary_unknown_reason"] == rb["extra"]["primary_unknown_reason"] == (
        "bootstrap_failed")


def test_bulk_admission_uses_the_shared_ledger_and_counts_reserved_memory(tmp_path):
    from compaction.s2.native import slots as SL
    m = _script_module()
    kw = {"state": tmp_path / "s.json", "lock": tmp_path / "s.lock"}
    common = {"capacity": 8, "poll_s": 0.01, "names": lambda: ["other-1"], **kw}
    # remote (astra cx-build r1 #5): every reservation's memory counts against MemAvailable
    m.gate_reading = lambda host: {"free_gb": 12.0, "load1": 1.0, "cores": 16}
    h1, st = m.acquire("ssh://rig", "h1", memory_mb=3072, **common)
    assert st["reserved_mb"] == 6144  # 12 - 2 x 3 = 6 >= 6
    with pytest.raises(TimeoutError):  # 12 - 4 x 3 < 6
        m.acquire("ssh://rig", "h2", memory_mb=3072, timeout_s=0.1, **common)
    assert SL.used("ssh://rig", [], state=kw["state"])["reserved"] == 2  # h2 released its try
    h1.__exit__(None, None, None)
    assert SL.used("ssh://rig", [], state=kw["state"])["reserved"] == 0
    # laptop (r1 #4): container slots shared with another scheduler's reservations
    m.gate_reading = lambda host: {"free_pct": 50, "load1": 1.0}
    with SL.reserve("laptop", "agy", n=4, prefix="native-agy-chain", capacity=8,
                    names=common["names"], poll_s=0.01, **kw):
        h3, _ = m.acquire(None, "h3", memory_mb=1280, **common)  # 4 + 1 external + 2 <= 8
        with pytest.raises(TimeoutError):  # 6 + 1 + 2 > 8
            m.acquire(None, "h4", memory_mb=1280, timeout_s=0.1, **common)
        h3.__exit__(None, None, None)
    # a busy or unreadable host keeps the gate closed
    m.gate_reading = lambda host: {"free_pct": 10, "load1": 1.0}
    with pytest.raises(TimeoutError):
        m.acquire(None, "h5", memory_mb=1280, timeout_s=0.1, **common)

    def broken(host):
        raise RuntimeError("memory_pressure rc=1")
    m.gate_reading = broken
    ok, st = m.resources_ok(None, 2560)
    assert not ok and "memory_pressure" in st["error"]
    assert SL.used("laptop", [], state=kw["state"])["reserved"] == 0
    assert m.slot_host(None) == "laptop" and m.slot_host("ssh://rig") == "ssh://rig"
    assert m.assignment() == m.CCS.assignment() and len(m.assignment()) == 24
    # astra cx-build r3 #5: another scheduler's larger memory budget counts as declared
    m.gate_reading = lambda host: {"free_gb": 20.0, "load1": 1.0, "cores": 16}
    with (SL.reserve("ssh://rig", "agy", n=2, prefix="native-agy-chain", capacity=8,
                     names=common["names"], memory_mb=12288, poll_s=0.01, **kw),
          pytest.raises(TimeoutError)):  # 20 - 12 - 2 x 3 < 6, though 4 slots x 3 GB fit
            m.acquire("ssh://rig", "h6", memory_mb=3072, timeout_s=0.1, **common)


# ------------------------------------------------------------------------------------------
# astra cx-build r2: no free text without collected secrets; screened evidence only
# ------------------------------------------------------------------------------------------

REFRESHED = "r" * 44  # a token Codex refreshed in the container, unknown to the laptop


def test_a_refreshed_token_in_an_ask_never_reaches_any_file_when_secrets_fail(
        tmp_path, monkeypatch):
    res, calls = fake_history(tmp_path, monkeypatch, secrets_fail=True, leak_text=REFRESHED)
    files = [f for f in (tmp_path / "h").rglob("*") if f.is_file()]
    assert files and not [f for f in files if REFRESHED in f.read_text(errors="replace")]
    ra, _rb = _results(res)
    assert ra["extra"]["texts_withheld"] and "asks" not in ra["extra"]
    assert "boundary" not in ra["extra"] and "checks" not in ra["extra"]
    assert ra["error"] == "failed steps: secrets (texts withheld: secrets uncollected)"
    assert calls["evidence"] == []  # no evidence export at all
    assert ra["traps_home"] is None and ra["extra"]["evidence_ok"] is False


def test_safe_extra_keeps_only_validated_metadata():
    raw = {"turns": [{"k": 1, "stop": {"text": REFRESHED}, "info": {"errors": [REFRESHED]},
                      "decision": {"kind": "checkin", "text": REFRESHED}},
                     {"k": 2, "decision": {"kind": REFRESHED}}],
           "thread_id": REFRESHED, "remote": REFRESHED, "asks": {"questions": [REFRESHED]},
           "boundary": {"ok": True, "attempts": [{"error": REFRESHED}]},
           "primary_unknown_reason": REFRESHED, "rollouts_in_container": "1",
           "imported_prefix_intact": True, "rollout_range": [0, 5]}
    safe = X.safe_extra(raw)
    assert REFRESHED not in json.dumps(safe)
    assert safe["turns"] == [{"k": 1, "decision": {"kind": "checkin"}},
                             {"k": 2, "decision": {"kind": None}}]
    assert safe["boundary_ok"] is True and safe["rollouts_in_container"] is None
    assert safe["primary_unknown_reason"] == "secrets_uncollected"
    assert X.safe_extra({"thread_id": TID})["thread_id"] == TID


def test_evidence_is_screened_with_the_current_tokens_and_withheld_when_not_clean(
        tmp_path, monkeypatch):
    res, calls = fake_history(tmp_path / "ok", monkeypatch)
    assert [c[0] for c in calls["evidence"]] == ["a-final", "final"]
    assert all(TOKEN in c[1] and c[2].startswith("simtok-") for c in calls["evidence"])
    res, calls = fake_history(tmp_path / "hit", monkeypatch, evidence_ok=False)
    ra, rb = _results(res)
    assert ra["traps_home"] is None and rb["traps_home"] is None
    assert rb["extra"]["evidence"] == {"ok": False, "why": "a secret is in the staged evidence",
                                       "attempts": 1}
    assert rb["extra"]["primary_unknown_reason"] == "evidence_withheld"
    assert S.score_b(Path(res["b"]), _brun())["teammate_lost"] is None


def _sh(script, root, env):
    import os
    # as in the container: `docker exec` without -i gives the script no stdin
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, **env}, cwd=root, check=False,
                          stdin=subprocess.DEVNULL, timeout=60)


def _bare_repo_with(tmp_path, text):
    """A bare repo whose one commit holds `text` (compressed in its objects, so a plain grep
    of the files can't see it)."""
    work, bare = tmp_path / "work", tmp_path / "ev" / "remote.git"
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-c", "init.defaultBranch=main"]
    subprocess.run([*g, "init", "-q", str(work)], check=True)
    (work / "notes.txt").write_text(text)
    subprocess.run([*g, "-C", str(work), "add", "."], check=True)
    subprocess.run([*g, "-C", str(work), "commit", "-qm", "notes"], check=True)
    subprocess.run([*g, "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


def test_the_screen_finds_tokens_in_files_archives_and_git_objects(tmp_path):
    import tarfile
    secrets = {f"{X.SECRET_ENV}_0": TOKEN, f"{X.SECRET_ENV}_1": REFRESHED,
               "EMX_SIM_TOKEN": "simtok-" + "s" * 30}
    shm = tmp_path / "shm"
    shm.mkdir()

    def screen(root):
        r = _sh(X.screen_script(str(root), 2, shm=str(shm)), tmp_path, secrets)
        return r.returncode, r.stdout.split()

    # clean: plain files, an archive and a bare repo without any token
    clean = tmp_path / "clean"
    (clean / "logs").mkdir(parents=True)
    (clean / "logs" / "shim.jsonl").write_text('{"phase": "pre"}\n')
    rc, out = screen(clean)
    assert rc == 1 and "SCREEN_CLEAN" in out
    # a token in a plain file
    (clean / "logs" / "shim.jsonl").write_text(f'{{"argv": "{REFRESHED}"}}\n')
    rc, out = screen(clean)
    assert rc == 0 and "SCREEN_HIT" in out
    # a token only inside an archive
    arch = tmp_path / "arch"
    arch.mkdir()
    inner = tmp_path / "inner.txt"
    inner.write_text(f"token {TOKEN}\n")
    with tarfile.open(arch / "git_meta.tgz", "w:gz") as tf:
        tf.add(inner, arcname="config")
    rc, out = screen(arch)
    assert rc == 0 and "SCREEN_HIT" in out
    # a token only inside a remote's compressed git objects
    bare = _bare_repo_with(tmp_path, f"leaked {REFRESHED}\n")
    raw = b"".join(f.read_bytes() for f in bare.rglob("*") if f.is_file())
    assert REFRESHED.encode() not in raw  # grep alone would miss it
    rc, out = screen(bare.parent)
    assert rc == 0 and "SCREEN_HIT" in out
    # the sim token counts too
    sim = tmp_path / "sim"
    sim.mkdir()
    (sim / "x.txt").write_text(secrets["EMX_SIM_TOKEN"])
    assert screen(sim)[0] == 0
    # an archive that can't be unpacked is an error, never clean
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "remote.tgz").write_bytes(b"not a tarball")
    rc, out = screen(bad)
    assert rc not in (0, 1) and "SCREEN_CLEAN" not in out
    assert list(shm.iterdir()) == []  # the pattern file is removed
    # an empty secret would match everything: an error, never a verdict
    r = _sh(X.screen_script(str(clean), 2, shm=str(shm)), tmp_path,
            secrets | {f"{X.SECRET_ENV}_1": ""})
    assert r.returncode == 5 and list(shm.iterdir()) == []
    with pytest.raises(ValueError):
        X.screen_script("/x", 0)


class _EvSB:
    def __init__(self, files):
        self.files, self.name, self.host, self.execs = files, "c", None, []

    def read_file(self, path, timeout_s=120):
        return self.files[path.rsplit("/", 1)[1]]

    def exec(self, cmd, timeout_s=600, workdir="/app"):
        self.execs.append(cmd)


class _EvEnv:
    def __init__(self, sb):
        self.sb, self._other, self.quiesced = sb, [], False

    def quiesce(self):
        self.quiesced = True

    def container_logs(self):
        return {"shim": [], "push": [], "ask": []}


def _evidence(monkeypatch, verdicts, files=None, hashes=None, stage_error=False):
    import hashlib as h
    files = files or {"remote.tgz": b"R", "git_meta.tgz": b"M"}
    hashes = hashes or {k: h.sha256(v).hexdigest() for k, v in files.items()}
    sb = _EvSB(files)
    env = _EvEnv(sb)
    seen = {"assembled": 0, "screens": 0}

    def stage(env, label, snap, now, other):
        if stage_error:
            raise OSError("tar failed")
        return {"git_dir": "/app/.git", "have": ["hooks", "config"]}

    def screen(sb, secrets, sim):
        seen["screens"] += 1
        v = verdicts[seen["screens"] - 1]
        if isinstance(v, Exception):
            raise v
        return v, ""

    def assemble(env, label, snap, now, remote, meta_tgz, meta):
        seen["assembled"] += 1
        return Path("/home")
    monkeypatch.setattr(X, "snapshot_in_memory", lambda env, label: {"fs": {}, "git": {}})
    monkeypatch.setattr(X, "stage_in_container", stage)
    monkeypatch.setattr(X, "container_hashes", lambda sb: hashes)
    monkeypatch.setattr(X, "run_screen", screen)
    monkeypatch.setattr(X, "assemble_on_host", assemble)
    rec = X.screened_evidence(env, "final", [TOKEN], "sim")
    return rec, seen, sb, env


def test_screened_evidence_reads_out_only_on_a_clean_verdict(monkeypatch):
    rec, seen, sb, env = _evidence(monkeypatch, ["clean"])
    assert rec["ok"] and rec["home"] == Path("/home") and seen["assembled"] == 1
    assert env.quiesced and any("rm -rf" in c for c in sb.execs)  # staging removed
    rec, seen, _sb, _env = _evidence(monkeypatch, ["hit"])
    assert not rec["ok"] and rec["home"] is None and seen["assembled"] == 0
    rec, seen, _sb, _env = _evidence(monkeypatch, ["error", "clean"])
    assert rec["ok"] and seen["screens"] == 2 and rec["attempts"] == 2
    rec, seen, _sb, _env = _evidence(monkeypatch, [OSError("exec"), "error"])
    assert not rec["ok"] and seen["screens"] == 2 and seen["assembled"] == 0
    rec, seen, _sb, _env = _evidence(monkeypatch, ["clean"], hashes={"remote.tgz": "x",
                                                                      "git_meta.tgz": "y"})
    assert not rec["ok"] and "changed" in rec["why"] and seen["assembled"] == 0
    rec, seen, sb, _env = _evidence(monkeypatch, ["clean"], stage_error=True)
    assert not rec["ok"] and seen["screens"] == 0 and any("rm -rf" in c for c in sb.execs)


def test_assemble_on_host_writes_what_finish_traps_would(tmp_path):
    import io
    import tarfile

    from compaction.s2.chain import env as E

    def tgz(entries):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in entries.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    class Env:
        _write_logs = E.ChainEnv._write_logs

        def __init__(self):
            self.home, self.out_dir = tmp_path / "traps_home", tmp_path
            for d in ("logs", "state"):
                (self.home / d).mkdir(parents=True)
            self.bare, self._other, self.log = "/srv/x/proj.git", [], []
            self._inherited_missing = set()
            self._inherited = {k: [] for k in E.LOG_NAMES}

    env = Env()
    now = {"shim": ['{"phase": "pre"}'], "push": ['{"ref": "x"}'], "ask": []}
    home = X.assemble_on_host(env, "final", {"fs": {"a": 1}, "git": {"b": 2}}, now,
                              tgz({"proj.git/HEAD": b"ref: refs/heads/main\n"}),
                              tgz({"config": b"[core]\n"}), {"git_dir": "/app/.git",
                                                             "have": ["config"]})
    assert (home / "remote" / "origin.git" / "HEAD").exists()
    assert json.loads((home / "state" / "fs_final.json").read_text()) == {"a": 1}
    assert (home / "logs" / "push.jsonl").read_text() == '{"ref": "x"}\n'
    assert (tmp_path / "traps_home.tgz").exists() and (tmp_path / "git_meta_final.tgz").exists()
    man = json.loads((tmp_path / "git_meta_final.json").read_text())
    assert list(man["files"]) == ["config"] and man["absent"] == ["hooks"]


def test_a_git_meta_failure_never_withholds_the_scored_evidence():
    class Obs:
        def __init__(self, rc, output=""):
            self.rc, self.output = rc, output

    class SB_:
        name, host = "c", None

        def __init__(self):
            self.put = []

        def put_file(self, data, path, timeout_s=120):
            self.put.append(path)

    class Env:
        bare, sb = "/srv/x/proj.git", SB_()

        def __init__(self):
            self.cmds = []

        def harness(self, cmd, timeout_s=300, check=False, workdir="/app"):
            self.cmds.append(cmd)
            if "git_meta.tgz -C" in cmd:
                raise X.SB.SandboxError("tar failed")
            if "rev-parse" in cmd:
                return Obs(0, "/app/.git\n")
            return Obs(0)

    env = Env()
    meta = X.stage_in_container(env, "final", {"fs": {}, "git": {}},
                                {"shim": [], "push": [], "ask": []}, [])
    assert meta == {"git_dir": "/app/.git", "have": [], "error": "SandboxError"}
    assert any(c.startswith("rm -f") and "git_meta.tgz" in c for c in env.cmds)
    assert any("remote.tgz" in c for c in env.cmds)  # the remote was still staged
    assert f"{X.EVIDENCE_DIR}/logs/container_other.txt" in env.sb.put


def test_secrets_that_fail_before_the_agent_ran_still_withhold_texts(tmp_path, monkeypatch):
    # collection attempted and failed with no TaskRun made: the allowlist still applies
    res, calls = fake_history(tmp_path, monkeypatch, secrets_fail=True, leak_text=REFRESHED,
                              watch_fails=True)
    ra, _rb = _results(res)
    assert ra["extra"]["texts_withheld"] and ra["extra"]["turns"] == []
    assert ra["error"] == "failed steps: task, secrets (texts withheld: secrets uncollected)"
    files = [f for f in (tmp_path / "h").rglob("*") if f.is_file()]
    assert not [f for f in files if REFRESHED in f.read_text(errors="replace")]
    assert calls["evidence"] == []


def test_the_screen_finds_tokens_in_file_names_and_link_targets(tmp_path):
    """astra cx-build r3 #2: a token in an archive member's name or a symlink's target, with
    no file content to grep, is a hit; ordinary names stay clean."""
    import tarfile
    secrets = {f"{X.SECRET_ENV}_0": TOKEN, "EMX_SIM_TOKEN": "simtok-" + "s" * 30}
    shm = tmp_path / "shm"
    shm.mkdir()

    def screen(root):
        r = _sh(X.screen_script(str(root), 1, shm=str(shm)), tmp_path, secrets)
        return r.returncode, r.stdout.split()

    named, plain, linked = tmp_path / "named", tmp_path / "plain", tmp_path / "linked"
    for d in (named, plain, linked):
        d.mkdir()
    empty = tmp_path / "empty"
    empty.write_text("")
    with tarfile.open(named / "git_meta.tgz", "w:gz") as tf:
        tf.add(empty, arcname=f"hooks/pre-push-{TOKEN}")
    with tarfile.open(plain / "git_meta.tgz", "w:gz") as tf:
        tf.add(empty, arcname="hooks/pre-push")
    (linked / "ref").symlink_to(f"/nowhere/{TOKEN}")
    assert screen(named)[0] == 0 and "SCREEN_HIT" in screen(named)[1]
    assert screen(linked)[0] == 0
    rc, out = screen(plain)
    assert rc == 1 and "SCREEN_CLEAN" in out
