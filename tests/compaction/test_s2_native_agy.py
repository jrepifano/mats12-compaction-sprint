"""compaction.s2.native.agy: the Antigravity arm's turn hook, transcript conversion and
boundary layout ($0, no Docker, no model calls). Step shapes follow agy 1.4.7's
transcript_full.jsonl as seen live on 24 Oct (MCP calls through call_mcp_tool, tool output
with Created/Completed stamps, a /compact input, SYSTEM_MESSAGE notices, CHECKPOINT)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from compaction import s1_judge as J
from compaction.s2 import interact_run as IR
from compaction.s2 import lossaudit as LA
from compaction.s2.native import agy as A
from compaction.s2.native import common as N


def ui(n: int, text: str) -> dict:
    return {"step_index": n, "source": "USER_EXPLICIT", "type": "USER_INPUT",
            "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nt\n"
                       "</ADDITIONAL_METADATA>"}


def plan(n: int, calls: list[dict], tokens: int, content: str = "") -> dict:
    return {"step_index": n, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": content,
            "input_tokens": tokens - 1000, "cache_read_tokens": 1000, "output_tokens": 50,
            "tool_calls": calls}


def out(n: int, text: str) -> dict:
    return {"step_index": n, "source": "MODEL", "type": "GENERIC",
            "content": f"Created At: 2026-10-24T21:01:42Z\nCompleted At: 2026-10-24T21:01:43Z\n"
                       f"{text}"}


def ask(q: str) -> dict:
    return {"name": "call_mcp_tool", "args": {"Arguments": {"question": q},
                                              "ServerName": "user-server",
                                              "ToolName": "ask_user"}}


def run(cmd: str) -> dict:
    return {"name": "run_command", "args": {"CommandLine": cmd, "WaitMsBeforeAsync": 500}}


def sysmsg(n: int) -> dict:
    return {"step_index": n, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
            "content": "<SYSTEM_MESSAGE>[Notice] background tasks done</SYSTEM_MESSAGE>"}


def cp(n: int, text: str) -> dict:
    return {"step_index": n, "source": "SYSTEM", "type": "CHECKPOINT",
            "content": f"# Resuming from a compaction\n\n<summary>{text}</summary>"}


def two_checkpoints() -> list[dict]:
    return [ui(0, "Implement the plan."), plan(1, [ask("Which file first?")], 7000),
            out(2, "Start with entity.py. Use tabs."), plan(3, [run("sed -n 1,9p a.py")], 9000),
            out(4, "line1"), ui(5, "/compact"), sysmsg(6), cp(7, "S1: use tabs"),
            ui(8, "Continue."), sysmsg(9), plan(10, [run("git commit -am x")], 8000),
            out(11, "ok"), plan(12, [ask("Done, review?")], 9000), out(13, "Ship it."),
            ui(14, "/compact"), cp(15, "S2: shipped"), ui(16, "Continue.")]


def test_first_checkpoint_context_and_names():
    nb = A.checkpoint_boundary(two_checkpoints(), 1, model="m", ctx_tokens=9000)
    pre = nb.pre_messages
    assert pre[1] == {"role": "user", "content": "Implement the plan."}
    assert pre[2]["tool_calls"][0]["function"]["name"] == "ask_user"
    assert json.loads(pre[2]["tool_calls"][0]["function"]["arguments"]) == {
        "question": "Which file first?"}
    assert pre[3] == {"role": "tool", "tool_call_id": "agy1_0",
                      "content": "Start with entity.py. Use tabs."}
    assert pre[4]["tool_calls"][0]["function"]["name"] == "run_command"
    assert pre[5]["content"].startswith("Created At:")  # other tools keep what agy showed
    assert "/compact" not in json.dumps(pre)  # our own input is not context
    assert nb.post_messages[1]["content"].startswith("# Resuming from a compaction")
    assert nb.summary_index == 1 and nb.kept_indices == [] and nb.pre_tokens == 9000
    assert nb.trigger == "forced" and nb.policy == A.POLICY


def test_second_checkpoint_starts_from_the_first():
    nb = A.checkpoint_boundary(two_checkpoints(), 2, model="m", ctx_tokens=9000)
    pre = nb.pre_messages
    assert pre[1]["content"].endswith("<summary>S1: use tabs</summary>")
    assert pre[2] == {"role": "user", "content": "Continue."}
    assert "Implement the plan." not in json.dumps(pre)
    assert nb.summary.endswith("S2: shipped</summary>")


def test_commands_are_visible_to_the_loss_audit():
    nb = A.checkpoint_boundary(two_checkpoints(), 1, model="m", ctx_tokens=1)
    cmds = [J.command_of({"name": t["function"]["name"],
                          "args": json.loads(t["function"]["arguments"])})
            for m in nb.pre_messages for t in m.get("tool_calls") or []]
    assert "sed -n 1,9p a.py" in cmds


def test_other_mcp_tools_keep_their_server():
    name, args = A.normalize_call({"name": "call_mcp_tool", "args": {
        "ServerName": "x", "ToolName": "y", "Arguments": {"a": 1}}})
    assert (name, args) == ("mcp__x__y", {"a": 1})
    assert A.unwrap_reply('Created At: a\nCompleted At: b\n{"result": "hi"}') == "hi"


def test_boundary_layout_round_trip(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    rj = N.native_run_json(run_key="run", task_dict={"task_id": "t", "image": "i:1"},
                           persona_sha256="p", arm=A.ARM, model="m", policy=A.POLICY,
                           threshold={"compact_at": 150_000}, cli_version=A.CLI_VERSION)
    (rd / "run.json").write_text(json.dumps(rj))
    nb = A.checkpoint_boundary(two_checkpoints(), 1, model="m", ctx_tokens=9000)
    rows = [{"reply_id": "r1", "call_index": 1, "step": "02_implement",
             "question": "Which file first?", "reply": "Start with entity.py. Use tabs."}]
    b = N.write_boundary(rd, 1, nb, run_json=rj, snap={"tag": "kept:1", "snapshot_s": 1},
                         sim_export={"state": {"ask_user_call_count": 1, "conversation": []}},
                         sim_rows=rows, step="02_implement", step_instruction="go",
                         fingerprint="v3 x", sim_probe_sha256="h", calls_in_step=2)
    rec = LA.load(b)
    assert rec["history_complete"] and rec["summary"].endswith("S1: use tabs</summary>")
    assert json.loads((b / "boundary" / "sim_replies.json").read_text())[0]["pre_index"] == 3
    assert IR.reply_index(rows, nb.pre_messages)[0]["pre_index"] == 3


def _hook(tmp_path, transcript: list[dict], compact_at: int) -> tuple[str, str]:
    x = tmp_path / "x"
    x.mkdir(parents=True, exist_ok=True)
    logs = tmp_path / "brain" / "c1" / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "transcript_full.jsonl").write_text(
        "".join(json.dumps(s, separators=(",", ":")) + "\n" for s in transcript))
    (x / "compact_at").write_text(f"{compact_at}\n")
    script = A.hook_script().replace(A.XDIR, str(x)).replace(A.COMPACT_AT_FILE,
                                                               str(x / "compact_at"))
    payload = json.dumps({"conversationId": "c1",
                          "transcriptPath": str(logs / "transcript.jsonl")})
    r = subprocess.run(["sh", "-c", script], input=payload, capture_output=True, text=True,
                       check=False)
    marker = x / "compact-c1"
    return r.stdout.strip(), marker.read_text().strip() if marker.is_file() else ""


def test_hook_terminates_at_the_threshold(tmp_path):
    steps = two_checkpoints()[:5]  # the last call: 9000 = input 8000 + cache_read 1000
    out_, marker = _hook(tmp_path, steps, 9000)
    assert json.loads(out_) == {"terminationBehavior": "terminate"} and marker == "9000"
    out_, marker = _hook(tmp_path / "b", steps, 9001)
    assert json.loads(out_) == {} and marker == ""
    out_, _ = _hook(tmp_path / "c", steps, 0)  # 0 = never
    assert json.loads(out_) == {}
    assert (tmp_path / "x" / "ctx-c1.log").read_text().split()[-1] == "9000"


def test_command_line_has_the_conversation_and_no_token():
    cmd = A.agy_command(A.AgyConfig(), "Continue.", "c1", 600)
    assert "--conversation c1" in cmd and "--dangerously-skip-permissions" in cmd
    assert f"</dev/null > {A.TURN_OUT} 2> {A.TURN_ERR}" in cmd and "emx_token" not in cmd


def test_a_turn_that_ran_to_the_print_timeout_is_not_success():
    ok = N.StreamResult(0, 100.0, False)
    assert A._turn_ok(ok, {"status": "SUCCESS"}, "", 600) is None
    assert A._turn_ok(N.StreamResult(0, 597.0, False), {"status": "SUCCESS"}, "", 600) \
        .startswith("cli_timeout")
    assert A._turn_ok(ok, {"status": "SUCCESS"}, "warning: print timeout reached", 600) \
        .startswith("cli_timeout")
    assert A._turn_ok(N.StreamResult(137, 600.0, True), {}, "", 3600).startswith("infra_killed")


def test_exactly_one_new_checkpoint_after_our_compact():
    st = two_checkpoints()
    first = st[:8]  # ... /compact, notice, CHECKPOINT
    assert A.new_checkpoint_ok(first, 0)
    assert not A.new_checkpoint_ok(first, 1)  # nothing new
    auto = [s for s in first if not A._is_compact_input(s)]  # a checkpoint agy made itself
    assert not A.new_checkpoint_ok(auto, 0)
    assert A.new_checkpoint_ok(st, 1)


class LogSB:
    def __init__(self, marker: str | None, log: str, rc: int = 0):
        self.marker, self.log, self.rc = marker, log, rc

    def exec(self, cmd, timeout_s=600, workdir="/"):
        from compaction.deepswe.sandbox import ExecResult
        if "compact-" in cmd:
            return ExecResult(self.rc, self.marker if self.marker is not None else "NONE", "",
                              0.1)
        return ExecResult(0, self.log, "", 0.1)


def test_hook_evidence_needs_a_line_per_call_and_a_readable_marker():
    st = two_checkpoints()[:5]  # 2 model calls
    good = "t 7000\nt 9000\n"
    assert A.hook_evidence(LogSB(None, good), "c1", st) == (None, None)
    assert A.hook_evidence(LogSB("9000", good), "c1", st) == (None, 9000)
    assert "1 of 2" in A.hook_evidence(LogSB(None, "t 7000\n"), "c1", st)[0]
    assert "no token counts" in A.hook_evidence(LogSB(None, "t 7000\nt NA\n"), "c1", st)[0]
    assert "marker read failed" in A.hook_evidence(LogSB(None, good, rc=2), "c1", st)[0]


def test_hook_logs_na_without_token_counts(tmp_path):
    st = [ui(0, "go"), {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                        "content": "x"}]
    out_, marker = _hook(tmp_path, st, 100)
    assert json.loads(out_) == {} and marker == ""
    assert (tmp_path / "x" / "ctx-c1.log").read_text().split()[-1] == "NA"


def test_screen_command_never_holds_a_secret():
    cmd = A.screen_script(["/run/emx-x/turn.json"])
    assert "$EMX_SIM_TOKEN" in cmd and A.TOKEN_PATTERNS in cmd and "/run/emx-x/turn.json" in cmd


def test_a_call_past_the_threshold_without_a_marker_is_a_hook_error():
    st = two_checkpoints()[:5]
    prob, marker = A.hook_evidence(LogSB(None, "t 7000\nt 9000\n"), "c1", st, compact_at=9000)
    assert "no marker" in prob and marker is None
    assert A.hook_evidence(LogSB(None, "t 7000\nt 9000\n"), "c1", st, compact_at=9001)[0] is None
    assert "could not write" in A.hook_evidence(
        LogSB(None, "t 7000\nt MARKERFAIL\n"), "c1", st, compact_at=1)[0]


def test_hook_reports_a_failed_marker_write(tmp_path):
    steps = two_checkpoints()[:5]
    x = tmp_path / "x"
    x.mkdir()
    (x / "compact-c1").mkdir()  # the marker path can't be written as a file
    out_, _ = _hook(tmp_path, steps, 9000)
    assert json.loads(out_) == {}
    assert (x / "ctx-c1.log").read_text().split()[-1] == "MARKERFAIL"


def test_token_patterns_read_both_stores():
    assert A.AUTH_DIR in A.TOKEN_PATTERNS and A.STATE_DIR in A.TOKEN_PATTERNS


def test_infra_and_quota_classification():
    gone = N.StreamResult(-1, 5.0, False, "container_gone")
    assert A._turn_ok(gone, {}, "", 600).startswith("infra_killed")
    q = A._turn_ok(N.StreamResult(3, 5.0, False), {"status": "ERROR", "error": "x"},
                   "AGY_ERROR: {\"code\": \"RESOURCE_EXHAUSTED\", \"quota\": 1}", 600)
    import importlib.util
    spec = importlib.util.spec_from_file_location("s2n", "scripts/s2_native.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    res = {"steps": {"02_implement": {"turns": [{"n": "3-compact", "failed": q}]}}}
    assert S._rate_limited(res)  # a quota stop during /compact re-queues the task


def test_screen_needs_its_own_verdict_line():
    cmd = A.screen_script(["/x"])
    assert "SCREEN_CLEAN" in cmd and "SCREEN_HIT" in cmd

    class SB1:
        name, host, log = "c", None, []

    import compaction.s2.native.common as C2
    from compaction.deepswe.sandbox import ExecResult
    orig = C2.exec_env
    try:
        C2.exec_env = lambda sb, c, e, timeout_s=300, workdir="/": ExecResult(
            1, "", "Error response from daemon: No such container: c", 0.1)
        import pytest
        with pytest.raises(A.SB.SandboxError, match="No such container"):
            A.screen(SB1(), ["/x"], "tok")  # rc 1 from docker is not "clean"
        C2.exec_env = lambda sb, c, e, timeout_s=300, workdir="/": ExecResult(
            1, "SCREEN_CLEAN\n", "", 0.1)
        A.screen(SB1(), ["/x"], "tok")
    finally:
        C2.exec_env = orig


def test_a_killed_agy_run_after_spend_reruns_once_heavy(tmp_path, monkeypatch):
    import importlib.util
    import sys as _sys
    spec = importlib.util.spec_from_file_location("s2n", "scripts/s2_native.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    from compaction.s2.native import audit as AU
    calls = []

    def fake_run(cfg, sim_model):
        calls.append((cfg.run_key, cfg.memory_mb))
        cfg.run_dir.mkdir(parents=True)
        killed = len(calls) == 1
        return {"run_key": cfg.run_key, "end": "infra_killed" if killed else "submitted",
                "boundaries": [] if killed else [1], "sim_cost_usd": 1.2, "steps": {}}

    monkeypatch.setattr(A, "run_base", fake_run)
    monkeypatch.setattr(S, "_sim_model", lambda r, n, c: None)
    monkeypatch.setattr(S, "_live_sessions", lambda h, arm="x": 0)
    monkeypatch.setattr(S, "_max_ping", lambda r: None)  # never a real claude -p in tests
    monkeypatch.setattr(S, "_latest_rate", lambda r: {})
    monkeypatch.setattr(S, "RATE_BACKOFF_S", 0)
    monkeypatch.setattr(N, "code_version", lambda: {"dirty": False})
    monkeypatch.setattr(AU, "requirements", lambda rd, bf: [])
    monkeypatch.setattr(AU, "stage_lossaudit", lambda rds, st: [])
    monkeypatch.setattr(AU, "run_lossaudit", lambda st, out, docker_host=None: 0)
    monkeypatch.setattr(AU, "lossaudit_counts", lambda out, rk: {})
    import compaction.s1_second_look as SL
    monkeypatch.setattr(SL, "check_frozen", lambda: None)
    _sys.argv = ["x"]
    S.main(["agy-batch", "--go-from-jacob", "--root", str(tmp_path),
            "--tasks", "kombu-single-active-consumer-priority"])
    assert [c[1] for c in calls] == [N.AGENT_MEMORY_MB, N.HEAVY_MEMORY_MB]
    assert calls[1][0].endswith("-a2")


def test_output_lost_is_infra_and_skips_the_screen(monkeypatch):
    called = []
    monkeypatch.setattr(A, "screen", lambda *a, **k: called.append(1))
    monkeypatch.setattr(N, "detached_exec", lambda *a, **k: N.StreamResult(
        0, 5.0, False, "output_lost"))
    r, out, err = A.run_turn(None, A.AgyConfig(), "go", None, 600,
                             Path("/tmp/x.json"), {}, None, "tok")
    assert r.killed == "output_lost" and out == {} and not called
    assert A._turn_ok(r, out, err, 600).startswith("infra_killed")


class BgSB:
    """A container where agy (pid 10) idles on a background test (20, child 21) while its
    hook (30, child 31 `cat`) runs; `immortal` processes ignore signals."""

    def __init__(self, immortal=()):
        self.ps = {1: (0, "Ss", "timeout -k 5 1 sleep infinity"),
                   10: (1, "Sl", A.CLI + " -p go"), 20: (10, "S", "bash -c pytest"),
                   21: (20, "S", "python -m pytest"), 30: (10, "S", "/bin/sh " + A.HOOK),
                   31: (30, "S", "cat")}
        self.immortal, self.sigs, self.name = set(immortal), [], "c"

    def exec(self, cmd, timeout_s=600, workdir="/"):
        from compaction.deepswe.sandbox import ExecResult
        if cmd.startswith("ls "):
            return ExecResult(0, "root agent idle; waiting up to 1h59m for 1 background task(s)",
                              "", 0.1)
        if "ps -eo" in cmd:
            tok = cmd.split(";")[0].split(": ")[1]
            lines = [f"{p} {pp} {st} {a}" for p, (pp, st, a) in self.ps.items()]
            return ExecResult(0, "\n".join(lines + [f"99 0 S bash -lc : {tok}; ps"]), "", 0.1)
        if cmd.startswith("bad=0"):
            sig = cmd.split("kill -")[1].split()[0]
            pids = [int(x) for x in cmd.split("for p in ")[1].split(";")[0].split()]
            self.sigs.append((sig, pids))
            for p in pids:
                if p not in self.immortal:
                    self.ps.pop(p, None)
            return ExecResult(0, "", "", 0.1)
        return ExecResult(0, "", "", 0.1)


def test_background_kill_spares_the_hook_tree_and_is_verified(monkeypatch):
    monkeypatch.setattr(A, "BG_GRACE_S", 0)
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    sb, rec = BgSB(), []
    assert A.background_rule(sb, None, rec)() is False
    assert sb.sigs[0] == ("TERM", [20, 21])  # never the hook (30) or its child (31)
    assert rec[0]["outcome"] == "confirmed" and rec[0]["survivors"] == []


def test_a_background_kill_with_survivors_is_unconfirmed(monkeypatch):
    monkeypatch.setattr(A, "BG_GRACE_S", 0)
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    sb, rec = BgSB(immortal={21}), []
    assert A.background_rule(sb, None, rec)() is True  # tells detached_exec to stop waiting
    assert rec[0]["outcome"] == "unconfirmed" and rec[0]["survivors"] == [21]


def _s2n():
    import importlib.util
    spec = importlib.util.spec_from_file_location("s2n", "scripts/s2_native.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    return S


def _audit_stubs(monkeypatch, S, rc=0):
    from compaction.s2.native import audit as AU
    calls = {"requirements": 0, "lossaudit": 0}

    def reqs(rd, bf):
        calls["requirements"] += 1
        for b in Path(rd).glob("b*/boundary"):  # its outputs, as the real ledger writes them
            (b / "requirements.json").write_text("{}")
        return [{"pre_tokens": 150_000, "items": 4, "lost": 1, "lost_new": 1}]

    def la(st, out, docker_host=None):
        calls["lossaudit"] += 1
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "losses.jsonl").write_text("")  # an empty out dir is never reused
        return rc

    monkeypatch.setattr(AU, "requirements", reqs)
    monkeypatch.setattr(AU, "stage_lossaudit", lambda rds, st: [])
    monkeypatch.setattr(AU, "run_lossaudit", la)
    monkeypatch.setattr(AU, "lossaudit_counts", lambda out, rk: {"items": 3, "lost": 0})
    monkeypatch.setattr(S, "_max_ping", lambda r: None)  # never a real claude -p in tests
    return calls


def _queued_run(tmp_path, S, rk="kombu-agy-a1", comps=("requirements", "lossaudit")):
    rd = tmp_path / rk
    (rd / "b1" / "boundary").mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"run_key": rk}))
    for n in ("boundary.json", "record.json"):
        (rd / "b1" / "boundary" / n).write_text(json.dumps({"pre_tokens": 1}))
    from compaction.s2 import session as SS
    assert [b.name for b in SS.boundary_dirs(rd)] == ["b1"]
    return rd, S._queue_audit(tmp_path, rd, rk, "seven_day at 0.91", comps,
                              S.audit_inputs(rd))


def test_past_the_max_hold_audits_queue_and_run_after_the_reset(tmp_path, monkeypatch):
    import sys as _sys
    S = _s2n()
    week = {"now": {}}
    held = {"unifiedWindows": {"seven_day": {"utilization": 0.91, "resetsAt": 4e9,
                                             "status": "allowed"}}}

    def fake_run(cfg, sim_model):
        cfg.run_dir.mkdir(parents=True)
        (cfg.run_dir / "run.json").write_text(json.dumps({"run_key": cfg.run_key}))
        week["now"] = held  # the window crosses 90% while the run is live
        return {"run_key": cfg.run_key, "end": "submitted", "boundaries": [1],
                "sim_cost_usd": 0.5, "steps": {}}

    calls = _audit_stubs(monkeypatch, S)
    monkeypatch.setattr(A, "run_base", fake_run)
    monkeypatch.setattr(S, "_sim_model", lambda r, n, c: None)
    monkeypatch.setattr(S, "_live_sessions", lambda h, arm="x": 0)
    monkeypatch.setattr(S, "_latest_rate", lambda r: week["now"])
    monkeypatch.setattr(N, "code_version", lambda: {"dirty": False})
    import compaction.s1_second_look as SL
    monkeypatch.setattr(SL, "check_frozen", lambda: None)
    _sys.argv = ["x"]
    S.main(["agy-batch", "--go-from-jacob", "--root", str(tmp_path),
            "--tasks", "kombu-single-active-consumer-priority"])
    assert calls == {"requirements": 0, "lossaudit": 0}
    (q,) = (tmp_path / S.AUDIT_QUEUE).glob("*.json")
    entry = json.loads(q.read_text())
    assert entry["components"] == ["requirements", "lossaudit"]
    assert Path(entry["run_dir"]).is_absolute()
    rows = [json.loads(x) for x in next((tmp_path / "_batch").glob("*.jsonl")).read_text()
            .splitlines()]
    done = next(r for r in rows if r["event"] == "done")
    assert done["audit_queued"] == str(q) and done["items"] is None and done["lost"] is None
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 3  # still held
    assert q.exists() and calls["requirements"] == 0
    week["now"] = {}
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 0
    assert calls == {"requirements": 1, "lossaudit": 1}
    assert not q.exists() and (q.parent / "done" / q.name).exists()
    ad = tmp_path / "_audit" / entry["run_key"]
    assert json.loads((ad / "lossaudit.done.json").read_text())["result"]["lossaudit"] == {
        "items": 3, "lost": 0}


def test_the_second_audit_holds_if_the_window_crosses_during_the_first(tmp_path, monkeypatch):
    S = _s2n()
    calls = _audit_stubs(monkeypatch, S)
    rd, _ = _queued_run(tmp_path, S)
    import threading

    def hold():
        return "seven_day at 0.9" if calls["requirements"] else None

    au = S._audits(tmp_path, rd, "kombu-agy-a1", None, threading.Lock(), hold=hold)
    assert au["pending"] == ["lossaudit"] and calls == {"requirements": 1, "lossaudit": 0}
    assert S._audit_counts(au)["items"] == 4 and S._audit_counts(au)["la_items"] is None


def test_a_failed_audit_stays_queued_and_a_done_component_is_not_rerun(tmp_path, monkeypatch):
    S = _s2n()
    calls = _audit_stubs(monkeypatch, S, rc=1)
    monkeypatch.setattr(S, "_latest_rate", lambda r: {})
    _, q = _queued_run(tmp_path, S)
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 5
    e = json.loads(q.read_text())
    assert e["components"] == ["lossaudit"] and "lossaudit rc 1" in e["last_error"]
    calls2 = _audit_stubs(monkeypatch, S, rc=0)
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 0
    assert calls["requirements"] == 1 and calls2 == {"requirements": 0, "lossaudit": 1}
    # a crash after the work but before the entry moved: the records are reused
    _, q2 = _queued_run(tmp_path, S, rk="kombu-agy-a1")
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 0
    assert calls2 == {"requirements": 0, "lossaudit": 1} and not q2.exists()


def test_a_changed_run_or_malformed_entry_stays_queued(tmp_path, monkeypatch):
    S = _s2n()
    calls = _audit_stubs(monkeypatch, S)
    monkeypatch.setattr(S, "_latest_rate", lambda r: {})
    rd, q = _queued_run(tmp_path, S)
    (rd / "b1" / "boundary" / "boundary.json").write_text(json.dumps({"pre_tokens": 2}))
    (q.parent / "zz-broken.json").write_text("{trunc")
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 5
    assert calls == {"requirements": 0, "lossaudit": 0}
    assert "inputs changed" in json.loads(q.read_text())["last_error"]
    assert (q.parent / "zz-broken.json").exists()


def test_one_audit_queued_at_a_time(tmp_path, monkeypatch):
    import fcntl
    S = _s2n()
    _audit_stubs(monkeypatch, S)
    monkeypatch.setattr(S, "_latest_rate", lambda r: {})
    _queued_run(tmp_path, S)
    with open(tmp_path / S.AUDIT_QUEUE / ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 4


def test_block_slots_hold_only_older_launchers(tmp_path):
    import os
    S = _s2n()
    S._slot(tmp_path, "run-a1")
    S._slot(tmp_path, S.BLOCK_PREFIX + "1")
    blk = tmp_path / "_batch" / "slots" / (S.BLOCK_PREFIX + "1")
    os.utime(blk, (1, 1))  # aged placeholders stay ignored too
    assert S._slot(tmp_path) == (1, 1)


def _ping_events(source="none"):
    info = {"unifiedWindows": {"seven_day": {"utilization": 0.91, "resetsAt": 4e9}},
            "isUsingOverage": False, "overageStatus": "rejected"}
    return [{"type": "system", "subtype": "init", "apiKeySource": source, "cwd": "/x"},
            {"type": "rate_limit_event", "rate_limit_info": info},
            {"type": "result", "result": "OK"}, {"type": "_process", "returncode": 0}]


def test_max_ping_only_when_streams_are_stale_and_keeps_rate_events_only(tmp_path):
    import os
    S = _s2n()
    live = tmp_path / "run-a1" / "steps" / "02_implement" / "stream.jsonl"
    live.parent.mkdir(parents=True)
    live.write_text("")
    calls, checks = [], []

    def fake(cwd, args, stdin, timeout_s, env, on_event):
        calls.append((args, env))
        evs = _ping_events()
        assert not any(on_event(e) for e in evs)
        return evs

    def ping():
        return S._max_ping(tmp_path, run=fake, checks=lambda: checks.append(1))

    assert ping() is None and not calls  # a stream is fresh
    old = live.stat().st_mtime - 2 * S.MAX_PING_S
    os.utime(live, (old, old))
    assert ping() == "ok" and checks == [1]
    args, env = calls[0]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert args[args.index("--model") + 1] == S.CC.MODEL and args[args.index("--tools") + 1] == ""
    (f,) = tmp_path.glob("_ping/steps/*/stream.jsonl")
    assert [json.loads(x)["type"] for x in f.read_text().splitlines()] == ["rate_limit_event"]
    assert S.window_hold(S._latest_rate(tmp_path), 1000)[0].startswith("seven_day")
    assert ping() is None and len(calls) == 1  # spaced


def test_a_ping_off_the_subscription_is_killed_unpublished_and_stops_pings(tmp_path):
    S = _s2n()
    killed = []

    def fake(cwd, args, stdin, timeout_s, env, on_event):
        for e in _ping_events("ANTHROPIC_API_KEY"):
            if on_event(e):
                killed.append(e["type"])
                return [e]
        return []

    assert S._max_ping(tmp_path, run=fake, checks=lambda: None) == "bad"
    assert killed == ["system"] and not list(tmp_path.glob("_ping/steps/*/stream.jsonl"))
    for p in (tmp_path / "_ping" / "attempts").iterdir():
        p.unlink()
    assert S._max_ping(tmp_path, run=fake, checks=lambda: None) is None  # off for the process


def test_a_done_record_is_not_reused_once_its_outputs_are_gone(tmp_path, monkeypatch):
    import threading
    S = _s2n()
    calls = _audit_stubs(monkeypatch, S)
    rd, _ = _queued_run(tmp_path, S)
    from compaction.s2.native import audit as AU

    def reqs(rd, bf):  # the real one writes its outputs into each boundary
        calls["requirements"] += 1
        (rd / "b1" / "boundary" / "grades.json").write_text("{}")
        return [{"pre_tokens": 1, "items": 4, "lost": 1}]

    def la(st, out, docker_host=None):
        calls["lossaudit"] += 1
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "losses.jsonl").write_text("")
        return 0

    monkeypatch.setattr(AU, "requirements", reqs)
    monkeypatch.setattr(AU, "run_lossaudit", la)
    for _ in range(2):
        S._audits(tmp_path, rd, "kombu-agy-a1", None, threading.Lock())
    assert calls == {"requirements": 1, "lossaudit": 1}  # the second pass reused both
    (rd / "b1" / "boundary" / "grades.json").unlink()
    (tmp_path / "_audit" / "kombu-agy-a1" / "lossaudit" / "losses.jsonl").unlink()
    S._audits(tmp_path, rd, "kombu-agy-a1", None, threading.Lock())
    assert calls == {"requirements": 2, "lossaudit": 2}


def test_boundary_metadata_is_part_of_the_audit_inputs(tmp_path):
    S = _s2n()
    rd, _ = _queued_run(tmp_path, S)
    (rd / "b1" / "run.json").write_text(json.dumps({"image": "a"}))
    before = S.audit_inputs(rd)
    (rd / "b1" / "run.json").write_text(json.dumps({"image": "b"}))
    assert S.audit_inputs(rd) != before


def test_entries_with_a_bad_schema_are_reported_and_skipped(tmp_path, monkeypatch):
    S = _s2n()
    calls = _audit_stubs(monkeypatch, S)
    monkeypatch.setattr(S, "_latest_rate", lambda r: {})
    _, q = _queued_run(tmp_path, S)
    good = json.loads(q.read_text())
    qd = q.parent
    (qd / "a-list.json").write_text("[]")
    (qd / "b-typo.json").write_text(json.dumps({**good, "components": ["lossaudit", "typo"]}))
    (qd / "c-rel.json").write_text(json.dumps({**good, "run_dir": "kombu-agy-a1"}))
    (qd / "c-nested.json").write_text(json.dumps({**good, "components": [{}, []]}))
    rd2 = tmp_path / "other"
    (rd2 / "b1" / "boundary").mkdir(parents=True)
    (rd2 / "run.json").write_text("[1]")
    (qd / "d-runjson.json").write_text(json.dumps({**good, "run_dir": str(rd2)}))
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 5
    assert calls == {"requirements": 1, "lossaudit": 1} and not q.exists()  # the good one ran
    assert all((qd / n).exists() for n in ("a-list.json", "b-typo.json", "c-rel.json",
                                           "c-nested.json", "d-runjson.json"))


def test_a_partly_done_entry_revalidates_its_finished_component(tmp_path, monkeypatch):
    S = _s2n()
    calls = _audit_stubs(monkeypatch, S, rc=1)
    monkeypatch.setattr(S, "_latest_rate", lambda r: {})
    rd, q = _queued_run(tmp_path, S)
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 5
    assert json.loads(q.read_text())["components"] == ["lossaudit"]
    (rd / "b1" / "boundary" / "requirements.json").unlink()  # its outputs vanish
    calls2 = _audit_stubs(monkeypatch, S, rc=0)
    assert S.main(["audit-queued", "--arm", "agy", "--root", str(tmp_path)]) == 0
    assert calls["requirements"] == 1 and calls2 == {"requirements": 1, "lossaudit": 1}
    assert (rd / "b1" / "boundary" / "requirements.json").exists() and not q.exists()


def test_audit_counts_sum_the_by_reply_provenance():
    S = _s2n()
    au = {"requirements": [{"items": 3, "lost": 1, "items_by_reply": 2, "lost_by_reply": 1},
                           {"items": 4, "lost": 0}]}  # a row from before the fallback
    c = S._audit_counts(au)
    assert (c["items"], c["items_by_reply"], c["lost_by_reply"]) == (7, 2, 1)
    assert S._audit_counts({})["items_by_reply"] is None
