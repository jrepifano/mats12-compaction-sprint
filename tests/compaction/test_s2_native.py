"""compaction.s2.native: the shared native-harness plumbing and the Claude Code arm ($0, no
Docker, no model calls). Stubs stand in for the harness (a fake CLI that writes a Claude Code
transcript and calls the compaction hook) and for the simulator. The MCP round trip needs
FastMCP (`uv run --with fastmcp==4.1.3 pytest ...`) and is skipped without it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path
from typing import ClassVar

import pytest

from compaction.s2 import interact as I
from compaction.s2 import interact_run as IR
from compaction.s2 import lossaudit as LA
from compaction.s2 import session as SS
from compaction.s2.model import RunCapReached
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N
from compaction.s2.native import egress_proxy as EP

# ------------------------------------------------------------------------------------------
# A synthetic Claude Code transcript (the row shapes of logs/s2/native_harness_feasibility.md)
# ------------------------------------------------------------------------------------------


class T:
    """Builds session JSONL rows: a parentUuid chain, compact boundaries with preserved
    segments, and the api-request-shape row that carries the system prompt."""

    def __init__(self, sid: str = "s1"):
        self.rows: list[dict] = []
        self.sid = sid
        self.n = 0
        self.leaf: str | None = None

    def _uid(self) -> str:
        self.n += 1
        return f"u{self.n:03d}"

    def add(self, typ: str, message: dict, **kw) -> str:
        u = self._uid()
        self.rows.append({"type": typ, "uuid": u, "parentUuid": self.leaf, "sessionId": self.sid,
                          "isSidechain": False, "message": message, **kw})
        self.leaf = u
        return u

    def attachment(self) -> None:
        u = self._uid()
        self.rows.append({"type": "attachment", "uuid": u, "parentUuid": self.leaf,
                          "attachment": {"type": "environment"}})
        self.leaf = u

    def shape(self, system: str) -> None:
        self.rows.append({"type": "api-request-shape", "shape": {"system": [
            {"type": "text", "text": system}]}})

    def user(self, text: str) -> str:
        return self.add("user", {"role": "user", "content": text})

    def assistant(self, mid: str, blocks: list[dict]) -> str:
        return self.add("assistant", {"role": "assistant", "id": mid, "content": blocks})

    def tool_result(self, tid: str, content) -> str:
        return self.add("user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": content}]})

    def compact(self, preserved: list[str], summary: str, pre: int, post: int,
                auto: bool = True) -> tuple[str, str]:
        b = self._uid()
        self.rows.append({"type": "system", "subtype": "compact_boundary", "uuid": b,
                          "parentUuid": None, "logicalParentUuid": self.leaf,
                          "sessionId": self.sid, "compactMetadata": {
                              "trigger": "auto", "preTokens": pre, "postTokens": post,
                              "preservedMessages": {"uuids": preserved}}})
        parent = b
        if auto:  # auto-compaction: attachment rows between the boundary and the summary
            for _ in range(2):
                a = self._uid()
                self.rows.append({"type": "attachment", "uuid": a, "parentUuid": parent,
                                  "attachment": {"type": "hook_success"}})
                parent = a
        s = self._uid()
        self.rows.append({"type": "user", "uuid": s, "parentUuid": parent, "sessionId": self.sid,
                          "isSidechain": False, "isCompactSummary": True,
                          "message": {"role": "user", "content": summary}})
        self.leaf = preserved[-1] if preserved else s  # the chain goes on from the kept tail
        return b, s


def ask(t: T, mid: str, tid: str, q: str, reply: str) -> None:
    t.assistant(mid, [{"type": "tool_use", "id": tid, "name": CC.ASK_TOOL,
                       "input": {"question": q}}])
    t.tool_result(tid, json.dumps({"result": reply}))


def bash(t: T, mid: str, tid: str, cmd: str, out: str, think: bool = False) -> None:
    if think:
        t.assistant(mid, [{"type": "thinking", "thinking": "hmm"}])
    t.assistant(mid, [{"type": "text", "text": f"running {cmd}"}])
    t.assistant(mid, [{"type": "tool_use", "id": tid, "name": "Bash",
                       "input": {"command": cmd, "description": "d"}}])
    t.tool_result(tid, out)


def two_boundary_transcript() -> tuple[T, dict]:
    t = T()
    t.shape("SYSTEM PROMPT v1")
    t.user("Implement the plan in /tmp/plan.md.")
    t.attachment()
    bash(t, "m1", "t1", "cat /tmp/plan.md", "the plan", think=True)
    ask(t, "m2", "t2", "Done with pass 1, review?", "Use snake_case for the flag.")
    bash(t, "m3", "t3", "git commit -am x", "ok")
    kept1 = [t.rows[-2]["uuid"], t.rows[-1]["uuid"]]  # m3's tool_use row and its result
    b1, s1 = t.compact(kept1, "SUMMARY ONE", pre=90_000, post=8_000)
    t.shape("SYSTEM PROMPT v2")
    ask(t, "m4", "t4", "Done, review again?", "Also add a changelog line.")
    bash(t, "m5", "t5", "ls", "a b")
    kept2 = [t.rows[-1]["uuid"]]
    b2, s2 = t.compact(kept2, "SUMMARY TWO", pre=91_000, post=7_000, auto=False)
    return t, {"b1": b1, "s1": s1, "b2": b2, "s2": s2, "kept1": kept1, "kept2": kept2}


# ------------------------------------------------------------------------------------------
# Transcript -> the pi layout
# ------------------------------------------------------------------------------------------


def test_first_boundary_context_is_the_whole_chain():
    t, ids = two_boundary_transcript()
    c = CC.transcript_boundary(t.rows, 1, model="m", step_instruction="")
    pre = c.nb.pre_messages
    assert pre[0] == {"role": "system", "content": "SYSTEM PROMPT v1"}
    assert pre[1] == {"role": "user", "content": "Implement the plan in /tmp/plan.md."}
    # m1's three rows merge into one assistant message; thinking is dropped and counted
    assert pre[2]["role"] == "assistant" and pre[2]["content"] == "running cat /tmp/plan.md"
    assert pre[2]["tool_calls"][0]["function"]["name"] == "Bash"
    assert c.nb.info["thinking_blocks"] == 1
    # ask_user is renamed and its FastMCP wrapper unwrapped
    assert pre[4]["tool_calls"][0]["function"]["name"] == "ask_user"
    assert pre[5] == {"role": "tool", "tool_call_id": "t2",
                      "content": "Use snake_case for the flag."}
    assert c.nb.pre_tokens == 90_000 and c.nb.post_tokens == 8_000
    assert c.summary_uuid == ids["s1"] and c.nb.summary == "SUMMARY ONE"
    # the preserved segment: m3 (merged message) and its result
    assert [pre[i]["role"] for i in c.nb.kept_indices] == ["assistant", "tool"]
    assert c.nb.kept_indices == [len(pre) - 2, len(pre) - 1]
    post = c.nb.post_messages
    assert post[1] == {"role": "user", "content": "SUMMARY ONE"} and c.nb.summary_index == 1
    assert post[2]["tool_calls"][0]["id"] == "t3" and post[3]["tool_call_id"] == "t3"


def test_second_boundary_context_starts_from_the_first_summary():
    t, _ = two_boundary_transcript()
    c = CC.transcript_boundary(t.rows, 2, model="m", step_instruction="")
    pre = c.nb.pre_messages
    assert pre[0]["content"] == "SYSTEM PROMPT v2"  # the latest request shape before it
    assert pre[1] == {"role": "user", "content": "SUMMARY ONE"}
    assert pre[2]["tool_calls"][0]["id"] == "t3" and pre[3]["tool_call_id"] == "t3"
    assert pre[4]["tool_calls"][0]["function"]["name"] == "ask_user"
    assert "the plan" not in json.dumps(pre)  # nothing from before the first boundary
    assert [pre[i].get("tool_call_id") for i in c.nb.kept_indices] == ["t5"]
    assert c.nb.summary == "SUMMARY TWO"


def test_unwrap_only_touches_the_fastmcp_wrapper():
    assert CC.unwrap_ask('{"result": "hi"}') == "hi"
    assert CC.unwrap_ask('{"result": "hi", "x": 1}') == '{"result": "hi", "x": 1}'
    assert CC.unwrap_ask("plain text") == "plain text"


def test_transcript_without_its_summary_row_is_refused():
    t, _ = two_boundary_transcript()
    rows = [r for r in t.rows if not r.get("isCompactSummary")]
    with pytest.raises(ValueError):
        CC.transcript_boundary(rows, 1, model="m", step_instruction="")


# ------------------------------------------------------------------------------------------
# Boundary layout round trip
# ------------------------------------------------------------------------------------------


def _task_dict() -> dict:
    return {"task_id": "toy-task", "image": "img:1", "base_image": "img:1",
            "spec_path": "x", "spec_sha256": None, "language": "python",
            "persona_sha256": "p"}


def _write(tmp_path: Path, j: int = 1, k: int = 1) -> tuple[Path, CC.Converted]:
    t, _ = two_boundary_transcript()
    rd = tmp_path / "run"
    rj = N.native_run_json(run_key="run", task_dict=_task_dict(), persona_sha256="p",
                           arm=CC.ARM, model="claude-haiku-5-5", policy=CC.POLICY,
                           threshold={"window": 80_000}, cli_version=CC.CLI_VERSION)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps(rj))
    conv = CC.transcript_boundary(t.rows, j, model="claude-haiku-5-5", step_instruction="go")
    conv.nb.native_files["transcript.jsonl"] = "\n".join(json.dumps(r) for r in t.rows)
    rows = [{"reply_id": "r1", "call_index": 1, "step": "02_implement",
             "question": "Done with pass 1, review?", "reply": "Use snake_case for the flag."}]
    sim_export = {"state": {"ask_user_call_count": 1, "conversation": []}}
    bdir = N.write_boundary(rd, k, conv.nb, run_json=rj, snap={"tag": "kept:1", "snapshot_s": 1},
                            sim_export=sim_export, sim_rows=rows, step="02_implement",
                            step_instruction="go", fingerprint="v3 abc", sim_probe_sha256="h",
                            calls_in_step=5, extra={"native_summary_uuid": conv.summary_uuid},
                            eligible=lambda b, f: f["pre_tokens"] > 0)
    return bdir, conv


def test_boundary_layout_round_trips_through_lossaudit(tmp_path):
    bdir, conv = _write(tmp_path)
    b = bdir / "boundary"
    for name in ("pre_messages", "compaction", "post_messages", "record", "sim_state",
                 "sim_replies", "boundary"):
        assert (b / f"{name}.json").exists(), name
    assert (bdir / "run.json").exists() and (bdir / "native" / "transcript.jsonl").exists()
    rec = LA.load(bdir)
    assert rec["summary"] == "SUMMARY ONE" and rec["history_complete"]
    assert rec["s2"]["summary_index"] == 1 and rec["s2"]["image"] == "kept:1"
    assert rec["s2"]["policy"] == CC.POLICY
    texts = [m.get("text") for m in rec["source_messages"]]
    assert "Use snake_case for the flag." in texts
    assert any(c.get("name") == "ask_user" for m in rec["source_messages"]
               for c in m.get("tool_calls") or [])
    f = json.loads((b / "boundary.json").read_text())
    assert f["eligible"] is True and f["tail_start_index"] == min(conv.nb.kept_indices)
    assert f["fingerprint_version"] == I.FINGERPRINT_VERSION and f["native"] is True
    # the ask_user reply is found in pre_messages by the pi arm's reply_index
    rep = json.loads((b / "sim_replies.json").read_text())
    assert rep[0]["pre_index"] == 5
    assert SS.boundary_dirs(tmp_path / "run") == [bdir]


def test_kept_tail_counts_as_visible_after_the_compaction(tmp_path):
    """Claude Code keeps its last few messages verbatim; a requirement in them is not lost
    even if the summary drops it. They are in post_messages after the summary, so lossaudit's
    post_context (S1's `availability` calls an item found there still_present) carries them,
    and boundary.json's tail_start_index marks where the verbatim tail starts."""
    bdir, _ = _write(tmp_path)
    rec = LA.load(bdir)
    post = LA.post_context(rec)
    assert any(m.get("tool_use_id") == "t3" and m.get("text") == "ok" for m in post)
    assert any(c.get("id") == "t3" for m in post for c in m.get("tool_calls") or [])
    assert "SUMMARY ONE" not in json.dumps(post)  # the summary itself is graded separately
    f = json.loads((bdir / "boundary" / "boundary.json").read_text())
    pre = json.loads((bdir / "boundary" / "pre_messages.json").read_text())
    assert pre[f["tail_start_index"]]["tool_calls"][0]["id"] == "t3"


def test_write_boundary_refuses_to_overwrite(tmp_path):
    _write(tmp_path)
    with pytest.raises(FileExistsError):
        _write(tmp_path)


# ------------------------------------------------------------------------------------------
# Forks
# ------------------------------------------------------------------------------------------


def test_edited_transcript_carries_the_new_summary_and_session(tmp_path):
    t, ids = two_boundary_transcript()
    rows = t.rows[: t.rows.index(next(r for r in t.rows if r.get("uuid") == ids["s1"])) + 1]
    out = CC.edited_transcript(rows, ids["s1"], "SUMMARY ONE\n\nFORK-PROBE: heron", "new-sid")
    assert all(r["sessionId"] == "new-sid" for r in out if "sessionId" in r)
    assert rows[-1]["message"]["content"] == "SUMMARY ONE"  # the input is untouched
    c = CC.transcript_boundary(out, 1, model="m", step_instruction="")
    assert c.nb.summary.endswith("FORK-PROBE: heron")  # a resumed session reads the edit
    with pytest.raises(ValueError):
        CC.edited_transcript(rows, "nope", "x", "s")


def test_fork_command_resumes_as_a_fork_with_the_prompt():
    cmd = CC.cli_command(CC.CCConfig(), resume="abc", fork=True, prompt="Continue.")
    assert "--resume abc --fork-session Continue." in cmd
    assert cmd.startswith("unset ANTHROPIC_API_KEY")


# ------------------------------------------------------------------------------------------
# Secrets stay out of argvs and files
# ------------------------------------------------------------------------------------------


class FakeEgress:
    def proxy_env(self):
        return {"HTTPS_PROXY": N.PROXY_URL}

    def events(self):
        return [{"ev": "deny", "host": "github.com"}]


class FakeServer:
    token = "simtok"


def test_token_only_in_env_never_in_the_command_or_argv(monkeypatch, tmp_path):
    env = CC.cli_env(CC.CCConfig(auto_compact_window=80_000), FakeEgress(), FakeServer(),
                     "sk-ant-oat-SECRET")
    assert env[CC.TOKEN_ENV] == "sk-ant-oat-SECRET"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "80000"
    cmd = CC.cli_command(CC.CCConfig(), session_id="s")
    assert "SECRET" not in cmd
    seen = {}

    class P:
        returncode = 0

        def __init__(self, argv, **kw):
            seen["argv"], seen["env"] = argv, kw["env"]
            import io
            self.stdout = io.BytesIO(b'{"type":"x"}\n')
            self.stdin = None

        def poll(self):
            return 0

    monkeypatch.setattr(N.subprocess, "Popen", P)
    sb = type("SB", (), {"name": "c1", "host": "ssh://rig"})()
    r = N.stream_exec(sb, cmd, timeout_s=10, log_path=tmp_path / "s.jsonl", env=env)
    assert r.rc == 0
    assert not any("SECRET" in a for a in seen["argv"])
    assert seen["argv"][seen["argv"].index("-e") + 1] == CC.TOKEN_ENV
    assert seen["env"][CC.TOKEN_ENV] == "sk-ant-oat-SECRET"
    assert "SECRET" not in (tmp_path / "s.jsonl").read_text()


def test_stream_watch_refuses_api_key_billing():
    st, stop = {}, threading.Event()
    on = CC._stream_watch(st, stop)
    on(json.dumps({"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY"}))
    assert stop.is_set() and "not Max" in st["auth_error"]
    st2, stop2 = {}, threading.Event()
    CC._stream_watch(st2, stop2)(json.dumps({"type": "system", "subtype": "init",
                                             "apiKeySource": "none"}))
    assert not stop2.is_set() and st2["init"]["apiKeySource"] == "none"


# ------------------------------------------------------------------------------------------
# Egress
# ------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _echo_server() -> int:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            c, _ = srv.accept()
            data = c.recv(100)
            c.sendall(b"echo:" + data)
            c.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


def _connect(port: int, line: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(line)
        time.sleep(0.2)
        return s.recv(200)


def test_proxy_tunnels_only_to_the_allowlist_and_forwards():
    echo = _echo_server()
    pport, fport = _free_port(), _free_port()
    EP.serve(pport, EP.handle_proxy, EP.parse_allow(f"127.0.0.1:{echo}"))
    EP.serve(fport, EP.handle_forward, "127.0.0.1", echo, fport)
    time.sleep(0.2)
    assert b"403" in _connect(pport, b"CONNECT github.com:443 HTTP/1.1\r\n\r\n")
    assert b"405" in _connect(pport, b"GET http://github.com/ HTTP/1.1\r\n\r\n")
    with socket.create_connection(("127.0.0.1", pport), timeout=5) as s:
        s.sendall(f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in s.recv(100)
        s.sendall(b"ping")
        assert s.recv(100) == b"echo:ping"
    assert _connect(fport, b"hello") == b"echo:hello"


def test_parse_specs():
    assert EP.parse_allow("API.x.com:443, b:80") == {("api.x.com", 443), ("b", 80)}
    assert EP.parse_forwards("8000=100.1.2.3:18765") == [(8000, "100.1.2.3", 18765)]


class FakeSB:
    """exec() answers curl like a container on an internal network behind the sidecar."""

    def __init__(self, leak: bool = False):
        self.leak = leak

    def exec(self, cmd, timeout_s=600, workdir="/app"):
        via = "-x " in cmd
        api = "api.anthropic.com" in cmd
        if not via:
            return SB_R(0 if self.leak else 6, "200" if self.leak else "000",
                        "" if self.leak else "curl: (6) Could not resolve host")
        if api:
            return SB_R(0, "405", "")
        return SB_R(56, "000", "curl: (56) CONNECT tunnel failed, response 403")


def SB_R(rc, out, err):
    from compaction.deepswe.sandbox import ExecResult
    return ExecResult(rc, out, err, 0.1)


def test_egress_check_passes_only_when_nothing_else_is_reachable():
    ok = N.egress_check(FakeSB(), FakeEgress(), "https://api.anthropic.com/v1/messages")
    assert ok["ok"] and len(ok["checks"]) == 2 * len(N.EGRESS_CHECK_DENIED) + 2
    bad = N.egress_check(FakeSB(leak=True), FakeEgress(), "https://api.anthropic.com/v1/x")
    assert not bad["ok"]


def test_proxy_run_args_have_labels_lifetime_and_no_secret():
    e = N.Egress("claude_code", "100.1.2.3", 18765, run_id="r1")
    a = e.proxy_run_args("img")
    assert a[:3] == ["run", "-d", "--rm"] and "--name" in a
    assert f"emx.sandbox={N.purpose('claude_code')}" in a and "emx.run=r1" in a
    assert "ALLOW=api.anthropic.com:443" in a
    assert "FORWARD=8000=100.1.2.3:18765" in a
    assert a[a.index("img") + 1: a.index("img") + 4] == ["timeout", "-k", "5"]


# ------------------------------------------------------------------------------------------
# The simulator server
# ------------------------------------------------------------------------------------------


class StubSim:
    def __init__(self, cap_after: int | None = None):
        self.state = I.SimState()
        self.cap_after = cap_after
        self.ask_log: list = []
        self.rounds: list = []

    def ask(self, q: str) -> str:
        if self.cap_after is not None and self.state.ask_user_call_count >= self.cap_after:
            raise RunCapReached("cap")
        self.state.ask_user_call_count += 1
        self.state.conversation += [{"role": "user", "content": q},
                                    {"role": "assistant", "content": f"re: {q}"}]
        return f"re: {q}"

    def export(self) -> dict:
        return {"state": self.state.to_dict()}

    def first_request(self, q):
        return [{"role": "user", "content": q}]


def test_sim_server_rows_and_cap():
    caps = []
    s = N.SimServer(StubSim(cap_after=1), bind="127.0.0.1", port=1, on_cap=caps.append)
    s.step = "02_implement"
    assert s.ask("q1") == "re: q1"
    assert s.rows[0]["reply_id"] == "r1" and s.rows[0]["step"] == "02_implement"
    with pytest.raises(RunCapReached):
        s.ask("q2")
    assert caps and s.cap_error
    with pytest.raises(RuntimeError):
        s.ask("q3")  # stopped for good


def test_sim_server_boundary_errors_are_reported_not_raised():
    def bad(p):
        raise ValueError("boom")

    s = N.SimServer(StubSim(), bind="127.0.0.1", port=1, on_boundary=bad)
    out = s.boundary({"hook_event_name": "PostCompact"})
    assert out["ok"] is False and "boom" in out["error"]
    assert s.boundary_calls[0]["ok"] is False


def test_sim_server_over_mcp():
    pytest.importorskip("fastmcp")
    got = []
    s = N.SimServer(StubSim(), bind="127.0.0.1", on_boundary=lambda p: got.append(p) or
                    {"ok": True}).start()
    try:
        base = f"http://127.0.0.1:{s.port}"
        hdr = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}

        def post(path, body, extra=None):
            req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                         {**hdr, **(extra or {})})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers), r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), ""

        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}}
        assert post("/mcp", init)[0] == 403  # no token
        tok = {N.TOKEN_HEADER: s.token, "Host": "user-server:8000"}
        code, h, body = post("/mcp", init, tok)
        assert code == 200 and '"name":"simulated-user"' in body
        sid = {k.lower(): v for k, v in h.items()}["mcp-session-id"]
        tok2 = {**tok, "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"}
        post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, tok2)
        _, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, tok2)
        tools = json.loads(body.split("data: ", 1)[1])["result"]["tools"]
        assert [t["name"] for t in tools] == ["ask_user"]
        assert tools[0]["description"] == N.ASK_USER_DOC
        assert tools[0]["inputSchema"]["required"] == ["question"]
        _, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "ask_user", "arguments": {"question": "ok?"}}}, tok2)
        assert json.loads(body.split("data: ", 1)[1])["result"]["structuredContent"] == {
            "result": "re: ok?"}
        code, _, body = post(N.BOUNDARY_PATH, {"hook_event_name": "PostCompact"}, tok)
        assert code == 200 and got == [{"hook_event_name": "PostCompact"}]
    finally:
        s.stop()


# ------------------------------------------------------------------------------------------
# run_base with a stub harness: a fake CLI that compacts once, calling the hook first and
# writing the boundary rows only after it returns (as Claude Code does)
# ------------------------------------------------------------------------------------------


class FakeContainer:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.name = "fake"
        self.host = None
        self.killed = 0

    def exec(self, cmd, timeout_s=600, workdir="/app"):
        if cmd.startswith("cat "):
            path = cmd.split()[1].strip("'")
            return SB_R(0 if path in self.files else 1, self.files.get(path, ""), "")
        if "pkill" in cmd:
            self.killed += 1
        return SB_R(0, "", "")

    def put_file(self, data, path, timeout_s=120):
        self.files[path] = data.decode()


class FakeSession:
    def __init__(self, arm, image, sim_factory, **kw):
        self.sb = FakeContainer()
        # the real UserSim factory is never called: a stub simulator answers
        self.server = N.SimServer(StubSim(), bind="127.0.0.1", port=1,
                                  on_boundary=kw.get("on_boundary"))
        self.egress = FakeEgress()
        self.egress.network = "net"
        self.cleanup_unconfirmed: list = []

    def __enter__(self):
        return self

    def __exit__(self, *e):
        pass


class Task:
    task_id = "toy-task"
    persona = "persona"
    steps: ClassVar[dict] = {"01_plan": "Plan it.", "02_implement": "Implement it."}

    class info:
        image = "img:1"

    def to_dict(self):
        return _task_dict()


def _harness(tmp_path, monkeypatch, scenario: str = "ok", eligible=None):
    """run_base with a fake CLI. Scenarios: "ok" (01_plan compacts once, 02_implement compacts
    and the smoke stops after it), "phase1_fail" (the kept commit fails), "missing" (the CLI
    compacts without calling the hook), "cli_fail" (01_plan ends with an error result)."""
    monkeypatch.setattr(CC, "TRANSCRIPT_POLL_S", 0.05)
    monkeypatch.setattr(CC, "POST_REQUEST_WAIT_S", 0.2)
    monkeypatch.setattr(CC, "ensure_cli_volume", lambda h, i: CC.CLI_VOLUME)
    monkeypatch.setattr(N, "egress_check", lambda sb, e, u: {"ok": True, "checks": []})
    ps = {1: (0, "Ss", "timeout -k 5 21600 sleep infinity"), 7: (1, "S", "sleep infinity"),
          120: (1, "Ss", "timeout -k 5 5400 bash -lc cd /app && " + CC.CLI + " -p"),
          124: (120, "Sl", CC.CLI + " -p --verbose"), 130: (124, "S", "/bin/sh " + CC.HOOK),
          131: (130, "S", "curl -sS -H @/run/emx-x/h"),
          150: (124, "S", "bash -c pytest -x > /tmp/t.log")}
    monkeypatch.setattr(N, "ps_table", lambda sb: (dict(ps), "tok-xyz"))
    signals = []

    def signal(sb, pids, sig):  # a stopped process shows "T" to the next ps
        signals.append((sig, list(pids)))
        for p in pids:
            ppid, _, args = ps[p]
            ps[p] = (ppid, "T" if sig == "STOP" else "S", args)

    monkeypatch.setattr(N, "signal_pids", signal)
    scans = []

    def scan(sb, secrets):
        scans.append(sorted(secrets))
        return {"hits": [], "scanned": 3, "seconds": 0.1}

    monkeypatch.setattr(N, "secret_scan", scan)
    monkeypatch.setattr(I, "fingerprint", lambda runner, version=3: "v3 fp")
    snaps = []

    def snapshot(sb, keep, k):
        if scenario == "phase1_fail" and "Implement" in sb.files.get("step", ""):
            raise RuntimeError("commit failed")
        if scenario == "docker_fail" and "Implement" in sb.files.get("step", ""):
            raise N.SB.SandboxError("docker commit failed: kex_exchange_identification")
        tag = keep.new_tag(k)
        keep.record(k, tag, snapshot_s=0.1)
        snaps.append((k, sb.files.get("marker"), str(keep.run_dir.name)))
        return {"tag": tag, "snapshot_s": 0.1}

    monkeypatch.setattr(N, "snapshot", snapshot)
    monkeypatch.setattr(IR, "sim_probe", lambda sim: "probe")
    sessions = {}

    def fake_stream(sb, cmd, *, timeout_s, log_path, env, stdin=None, stop=None, on_line=None,
                    name=None):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        sid = cmd.split("--session-id ")[1].split()[0]
        path = f"{CC.PROJECT}/{sid}.jsonl"
        prompt = stdin.decode()
        sb.files["step"] = prompt
        on_line(json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none",
                            "session_id": sid}))
        t = T(sid)
        t.shape("SYS")
        t.user(prompt)
        reply = sessions["server"].ask("Done, review?")
        t.assistant("m1", [{"type": "tool_use", "id": "t1", "name": CC.ASK_TOOL,
                            "input": {"question": "Done, review?"}}])
        t.tool_result("t1", json.dumps({"result": reply}))
        sb.files[path] = "\n".join(json.dumps(r) for r in t.rows)
        sb.files["marker"] = "at-boundary"
        if scenario != "missing" or "Plan" in prompt:
            out = sessions["server"].boundary({"hook_event_name": "PostCompact",
                                               "session_id": sid, "transcript_path": path,
                                               "compact_summary": "<summary>S</summary>"})
            if not out.get("ok"):
                if scenario == "docker_fail":  # the CLI had compacted: observed > captured
                    t.compact([t.rows[-1]["uuid"]], "S", 50_000, 5_000)
                    sb.files[path] = "\n".join(json.dumps(r) for r in t.rows)
                return N.StreamResult(-9, 1.0, False, "stop")
        sb.files["marker"] = "after"
        t.compact([t.rows[-1]["uuid"]], "This session is being continued... S", 50_000, 5_000)
        sb.files[path] = "\n".join(json.dumps(r) for r in t.rows)
        if scenario == "unreadable" and "Implement" in prompt:
            del sb.files[path]  # the final read of this step's transcript fails
            on_line(json.dumps({"type": "result", "subtype": "success", "is_error": False}))
            return N.StreamResult(0, 1.0, False)
        if "Plan" in prompt or scenario == "missing":
            res = {"type": "result", "subtype": "success", "is_error": False}
            if scenario == "cli_fail":
                res = {"type": "result", "subtype": "error_during_execution", "is_error": True}
            on_line(json.dumps(res))
            return N.StreamResult(0, 1.0, False)
        deadline = time.monotonic() + 20
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        return N.StreamResult(-9, 2.0, False, "stop" if stop.is_set() else None)

    monkeypatch.setattr(N, "stream_exec", fake_stream)
    monkeypatch.setattr(N, "detached_exec", fake_stream)

    class Sess(FakeSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            sessions["server"] = self.server
            sessions["sb"] = self.sb

    cfg = CC.BaseConfig(task=Task(), root=tmp_path, prefix="smoke-x-", stop_after=1,
                        host=None, cc=CC.CCConfig(auto_compact_window=60_000))
    res = CC.run_base(cfg, sim_model=None, token="tok", session_cls=Sess, eligible=eligible)
    return cfg, res, {"snaps": snaps, "signals": signals, "scans": scans, "sb": sessions["sb"]}


def test_run_base_with_a_stub_harness(tmp_path, monkeypatch):
    cfg, res, seen = _harness(tmp_path, monkeypatch)
    assert res["end"] == "stop_after", res.get("error") or res.get("boundary_errors")
    rd = cfg.run_dir
    # planning compacted too: its boundary is kept in its own chain, outside the graded one
    assert res["plan_boundaries"] == [1] and res["boundaries"] == [1]
    assert (rd / CC.PLAN_DIR / "b1" / "boundary" / "boundary.json").exists()
    assert [s[2] for s in seen["snaps"]] == [CC.PLAN_DIR, rd.name]
    assert all(s[1] == "at-boundary" for s in seen["snaps"])  # inside the hook
    f = json.loads((rd / "b1" / "boundary" / "boundary.json").read_text())
    assert f["previous_boundary"] is None and f["update"] is False and f["graded_chain"]
    assert f["stage"] == "02_implement" and f["sim_calls"] == 2
    assert f["auto_compact_window"] == 60_000 and f["pre_tokens"] == 50_000
    # the agent's background shell was stopped for the capture and resumed after
    assert ("STOP", [150]) in seen["signals"] and ("CONT", [150]) in seen["signals"]
    assert f["quiescent"] is False and f["processes"][0].startswith("150 ")
    assert seen["scans"][0] == sorted([CC.TOKEN_ENV, "EMX_SIM_TOKEN"])
    rec = LA.load(rd / "b1")
    assert rec["summary"].endswith("S") and rec["s2"]["image"].startswith(SS.KEEP_REPO)
    rep = json.loads((rd / "b1" / "boundary" / "sim_replies.json").read_text())
    assert [r.get("pre_index") for r in rep if r["step"] == "02_implement"] == [3]
    assert (rd / "steps" / "02_implement" / "transcript.jsonl").exists()
    assert json.loads((rd / "result.json").read_text())["end"] == "stop_after"
    assert "tok" not in (rd / "b1" / "native" / "transcript.jsonl").read_text()


def test_a_docker_failure_in_capture_is_infra_and_not_overwritten(tmp_path, monkeypatch):
    _, res, _ = _harness(tmp_path, monkeypatch, "docker_fail")
    # 2 compactions observed, 1 captured: the reconcile keeps infra_error (the rerun cause)
    assert res["end"] == "infra_error", res.get("error")


def test_phase1_failure_stops_the_cli(tmp_path, monkeypatch):
    _, res, seen = _harness(tmp_path, monkeypatch, "phase1_fail")
    assert res["end"] == "boundary_error"
    assert res["boundary_errors"][0]["phase"] == 1 and res["boundaries"] == []
    assert seen["sb"].killed >= 1
    assert ("CONT", [150]) in seen["signals"]  # resumed even though the commit failed


def test_a_compaction_without_a_boundary_is_flagged(tmp_path, monkeypatch):
    _, res, _ = _harness(tmp_path, monkeypatch, "missing")
    assert res["end"] == "boundary_missing" and res["compactions_observed"] == 2


def test_a_failed_step_is_not_submitted(tmp_path, monkeypatch):
    _, res, _ = _harness(tmp_path, monkeypatch, "cli_fail")
    assert res["end"] == "cli_error" and "01_plan" in res["error"]
    assert "02_implement" not in res["steps"]


def test_step_ok_rules():
    good = {"init": {"apiKeySource": "none"}, "result": {"subtype": "success",
                                                         "is_error": False}}
    assert CC._step_ok(N.StreamResult(0, 1, False), good) is None
    assert "init" in CC._step_ok(N.StreamResult(0, 1, False), {})
    assert "not Max" in CC._step_ok(N.StreamResult(0, 1, False),
                                    {**good, "init": {"apiKeySource": None}})
    assert "exit" in CC._step_ok(N.StreamResult(1, 1, False), good)
    assert "result" in CC._step_ok(N.StreamResult(0, 1, False), {"init": good["init"]})
    assert CC._step_ok(N.StreamResult(-9, 1, False, "stop"), {"init": good["init"]}) is None


def test_classify_keeps_the_cli_and_hook_and_flags_background_work():
    ps = N.parse_ps("""  1  0 Ss timeout -k 5 21600 sleep infinity
  7  1 S  sleep infinity
120  1 Ss timeout -k 5 5400 bash -lc cd /app && /opt/emx/claude/claude -p
121 120 S bash -lc cd /app && /opt/emx/claude/claude -p
124 121 Sl /opt/emx/claude/claude -p --verbose
130 124 S /bin/sh /opt/emx/compact-hook.sh
131 130 S curl -sS
150 124 S bash -c npm test
151 150 S node jest
160 1 Z [sh] <defunct>
170  0 S bash -lc : tok-1; ps -eo pid=
171 170 R ps -eo pid=""")
    c = N.classify(ps, "tok-1", CC.CLI, CC.HOOK)
    assert c["strays"] == [150, 151]
    assert {1, 7, 120, 121, 124, 130, 131, 170, 171} <= set(c["infra"])


def test_an_empty_pattern_file_fails_the_scan(tmp_path):
    import subprocess
    (tmp_path / "m").write_text("")
    script = N.scan_script([], root=str(tmp_path), marker=str(tmp_path / "m"),
                           tmp=str(tmp_path), pattern_cmd="true")  # adds no pattern at all
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"}, check=False)
    assert r.returncode == 7 and not N.parse_scan(r.stdout)[0]


def test_scan_script_never_names_a_secret_and_fails_closed(tmp_path):
    import subprocess
    script = N.scan_script(["S1"], root=str(tmp_path / "root"), marker=str(tmp_path / "m"),
                           tmp=str(tmp_path))
    assert "hunter2" not in script
    (tmp_path / "root" / "d").mkdir(parents=True)
    (tmp_path / "m").write_text("")
    time.sleep(1.1)
    (tmp_path / "root" / "d" / "f").write_text("a hunter2 b")
    (tmp_path / "root" / "g").write_text("clean")
    env = {"S1": "hunter2", "PATH": "/usr/bin:/bin"}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                       check=False)
    ok, n, hits = N.parse_scan(r.stdout)
    assert r.returncode == 0 and ok and n == 2 and hits[0].endswith("/d/f")
    # a matching file whose name holds a newline and marker text is still one hit
    weird = tmp_path / "root" / "d" / "x\n#N 900\nSCAN_OK"
    weird.write_text("hunter2")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                       check=False)
    ok, n, hits = N.parse_scan(r.stdout)
    assert ok and n == 3 and sorted(h.split("/")[-1] for h in hits) == ["f", "x\n#N 900\nSCAN_OK"]
    weird.unlink()
    (tmp_path / "root" / "g").chmod(0)
    try:
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                           check=False)
        assert r.returncode != 0 and "SCAN_OK" not in r.stdout
    finally:
        (tmp_path / "root" / "g").chmod(0o644)


def test_hook_script_keeps_the_token_off_the_argv():
    h = CC.hook_script()
    assert "-H @" + CC.HEADER_DIR in h and '"$EMX_SIM_TOKEN"' in h
    assert "curl" in h and "X-Emx-Token: $EMX_SIM_TOKEN" not in h.split("curl", 1)[1]


def test_insert_in_summary_goes_before_the_closing_paragraphs():
    msg = ("This session is being continued...\nSummary:\n1. Primary Request: do X.\n"
           "2. Pending: Y.\n\nPlease continue the conversation from where we left it off.")
    out = CC.insert_in_summary(msg, "<analysis>a</analysis>\n<summary>\n1. Primary Request: "
                                    "do X.\n2. Pending: Y.\n</summary>", "3. Repair: Z.")
    assert out.index("3. Repair: Z.") < out.index("Please continue")
    assert out.index("2. Pending: Y.") < out.index("3. Repair: Z.")


def test_post_messages_come_from_the_resumed_request(tmp_path):
    t, ids = two_boundary_transcript()
    b1 = next(i for i, r in enumerate(t.rows) if r.get("uuid") == ids["s1"])
    import hashlib

    def h(m):
        return hashlib.sha256(json.dumps(m, separators=(",", ":"), sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()

    req = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>git</sys"
                                                                 "tem-reminder>"},
                                        {"type": "text", "text": "SUMMARY ONE"}]},
           {"role": "system", "content": "Note: f.py was read before; contents re-attached"}]
    extra = [{"type": "api-request-blob", "hash": h(m), "message": m} for m in req]
    extra.append({"type": "api-request", "id": "rq1", "base": None, "keep": 0,
                  "tail": [h(m) for m in req]})
    rows = t.rows[: b1 + 1] + extra + t.rows[b1 + 1:]
    c = CC.transcript_boundary(rows, 1, model="m", step_instruction="")
    assert c.nb.info["post_source"] == "request"
    post = c.nb.post_messages
    assert post[c.nb.summary_index]["content"] == "SUMMARY ONE"
    assert post[-1] == {"role": "user", "content":
                        "Note: f.py was read before; contents re-attached"}
    rec_post = LA.record_messages(post, skip=[c.nb.summary_index])
    assert any("re-attached" in m["text"] for m in rec_post)  # visible to the graders


def test_attachments_are_part_of_the_context():
    t = T()
    t.user("go")
    u = t._uid()
    t.rows.append({"type": "attachment", "uuid": u, "parentUuid": t.leaf,
                   "attachment": {"type": "session_context"},
                   "rendered": [{"content": "<system-reminder>RULE: use tabs</system-reminder>"}]})
    t.leaf = u
    bash(t, "m1", "t1", "ls", "x")
    t.compact([t.rows[-1]["uuid"]], "S", 10, 2)
    c = CC.transcript_boundary(t.rows, 1, model="m", step_instruction="")
    assert any("RULE: use tabs" in str(m["content"]) for m in c.nb.pre_messages)
    assert c.nb.info["attachments"] == 1


def test_a_boundary_with_background_work_is_never_a_fork_source(tmp_path, monkeypatch):
    cfg, res, _ = _harness(tmp_path, monkeypatch, eligible=lambda b, f: True)
    f = json.loads((cfg.run_dir / "b1" / "boundary" / "boundary.json").read_text())
    assert f["quiescent"] is False and f["eligible"] is False
    assert f["ineligible_reason"] == "not_quiescent"
    assert res["end"] == "stop_after"  # kept for Q1, not "eligible"


def test_an_unreadable_transcript_is_not_a_complete_run(tmp_path, monkeypatch):
    _, res, _ = _harness(tmp_path, monkeypatch, "unreadable")
    assert res["end"] in ("transcript_error", "boundary_error", "infra_error")
    assert res["end"] != "submitted"


class PsSB:
    """ps output that changes as signals arrive: a child appears after the first STOP."""

    def __init__(self, stubborn: bool = False):
        self.ps = {1: (0, "Ss", "timeout -k 5 1 sleep infinity"),
                   124: (1, "Sl", CC.CLI + " -p"), 130: (124, "S", "/bin/sh " + CC.HOOK),
                   150: (124, "S", "bash -c make")}
        self.stubborn, self.n = stubborn, 0
        self.name = "c"

    def exec(self, cmd, timeout_s=600, workdir="/app"):
        if "ps -eo" in cmd:
            tok = cmd.split(";")[0].split(": ")[1]
            lines = [f"{p} {pp} {st} {a}" for p, (pp, st, a) in self.ps.items()]
            lines.append(f"999 0 S bash -lc : {tok}; ps")
            return SB_R(0, "\n".join(lines), "")
        if cmd.startswith("bad=0"):
            self.sigs = getattr(self, "sigs", []) + [cmd.split("kill -")[1].split()[0]]
            pids = [int(x) for x in cmd.split("for p in ")[1].split(";")[0].split()]
            for p in pids:
                if not self.stubborn:
                    pp, _, a = self.ps[p]
                    self.ps[p] = (pp, "T", a)
            self.n += 1
            if self.n == 1:  # a child spawned between the ps and the STOP
                self.ps[151] = (150, "S", "cc -c x.c")
            return SB_R(0, "", "")
        return SB_R(0, "", "")


def test_freeze_catches_late_children_and_fails_when_it_cannot_stop():
    sb = PsSB()
    stopped = N.freeze(sb, CC.CLI, CC.HOOK)
    assert sorted(stopped) == [150, 151] and all(sb.ps[p][1] == "T" for p in (150, 151))
    stubborn = PsSB(stubborn=True)
    with pytest.raises(N.SB.SandboxError):
        N.freeze(stubborn, CC.CLI, CC.HOOK)
    assert stubborn.sigs[-1] == "CONT"  # what it stopped is resumed before it raises


def test_fallback_keeps_the_attachments_after_the_summary():
    t = T()
    t.user("go")
    bash(t, "m1", "t1", "ls", "x")
    t.compact([t.rows[-1]["uuid"]], "SUMMARY", 10, 2)
    u = t._uid()
    t.rows.append({"type": "attachment", "uuid": u, "parentUuid": t.leaf, "rendered": [
        {"content": "<system-reminder>Note: plan.md re-attached</system-reminder>"}]})
    t.assistant("m9", [{"type": "text", "text": "after the boundary"}])
    s = CC.summary_row(t.rows, CC.compact_rows(t.rows)[0])
    cut = CC._resume_cut(t.rows, s, len(t.rows))
    rows = t.rows[: cut + 1]
    assert rows[-1]["uuid"] == u
    c = CC.transcript_boundary(rows, 1, model="m", step_instruction="")
    assert c.nb.info["post_source"] == "chain"
    texts = json.dumps(c.nb.post_messages)
    assert "plan.md re-attached" in texts and "after the boundary" not in texts


def test_insert_in_summary_with_a_long_body():
    body = "\n".join(f"{i}. item {i} " + "x" * 50 for i in range(80))
    assert len(body) > 2000
    msg = f"This session is being continued...\nSummary:\n{body}\n\nPlease continue."
    out = CC.insert_in_summary(msg, f"<summary>\n{body}\n</summary>", "REPAIR")
    assert out.index("79. item 79") < out.index("REPAIR") < out.index("Please continue")


def test_entrypoint_services_are_never_strays():
    ps = N.parse_ps("""  1  0 Ss /bin/bash -c "$0" true; ... timeout -k 5 1 sleep infinity
 20  1 S  postgres -D /var/lib/pg
 21 20 S  postgres: checkpointer
124  1 Sl /opt/emx/claude/claude -p
150 124 S bash -c npm test""")
    c = N.classify(ps, "tok", CC.CLI, CC.HOOK, services=frozenset({20}))
    assert c["strays"] == [150] and {20, 21} <= set(c["infra"])


class StagedSB:
    """A transcript that grows between reads: the summary first, an attachment later, no
    request ever."""

    def __init__(self, stages: list[list[dict]]):
        self.stages, self.n, self.name = stages, 0, "c"

    def exec(self, cmd, timeout_s=600, workdir="/app"):
        rows = self.stages[min(self.n, len(self.stages) - 1)]
        self.n += 1
        return SB_R(0, "\n".join(json.dumps(r) for r in rows), "")


def test_rows_written_while_waiting_for_the_request_are_kept(monkeypatch):
    monkeypatch.setattr(CC, "TRANSCRIPT_POLL_S", 0.01)
    t = T()
    t.user("go")
    bash(t, "m1", "t1", "ls", "x")
    t.compact([t.rows[-1]["uuid"]], "SUMMARY", 10, 2)
    first = list(t.rows)
    u = t._uid()
    t.rows.append({"type": "attachment", "uuid": u, "parentUuid": t.leaf, "rendered": [
        {"content": "<system-reminder>late attachment</system-reminder>"}]})
    sb = StagedSB([first, list(t.rows)])
    quit_ = threading.Event()

    def has_summary(rows):
        return bool(CC.compact_rows(rows))

    rows, ok = CC._read_until(sb, "/x", has_summary, 5)
    assert ok and rows == first
    quit_.set()

    def has_request(rows):
        return CC.first_request_after(rows, CC.summary_row(rows, CC.compact_rows(rows)[0])) \
            is not None

    rows, ok = CC._read_until(sb, "/x", has_request, 5, quit_)
    assert not ok and rows[-1]["uuid"] == u  # the last read, not the earlier one


def _fork_dir(tmp_path, extra=None) -> Path:
    bdir, conv = _write(tmp_path)
    f = json.loads((bdir / "boundary" / "boundary.json").read_text())
    f.update(native_summary_uuid=conv.summary_uuid, fingerprint_sha256=I.sha256("v3 fp"),
             sim_probe_sha256="probe", quiescent=True, **(extra or {}))
    (bdir / "boundary" / "boundary.json").write_text(json.dumps(f))
    (bdir / "boundary" / "sim_state.json").write_text(json.dumps(
        {"state": {"ask_user_call_count": 0, "conversation": []}}))
    return bdir


def _fork(tmp_path, monkeypatch, bdir, cap: bool):
    monkeypatch.setattr(I, "fingerprint", lambda runner, version=3: "v3 fp")
    monkeypatch.setattr(IR, "sim_probe", lambda sim: "probe")
    caps = {}

    class Sess(FakeSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            caps["on_cap"] = kw["on_cap"]

    def fake_stream(sb, cmd, *, timeout_s, log_path, env, stdin=None, stop=None, on_line=None,
                    name=None):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        on_line(json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none",
                            "session_id": "f1"}))
        if cap:
            caps["on_cap"](RunCapReached("cap"))
            return N.StreamResult(-9, 1.0, False, "stop")
        on_line(json.dumps({"type": "result", "subtype": "success", "is_error": False}))
        return N.StreamResult(0, 1.0, False)

    monkeypatch.setattr(N, "stream_exec", fake_stream)
    monkeypatch.setattr(N, "detached_exec", fake_stream)
    return CC.fork(bdir, tmp_path / "fork", new_summary="S2", prompt="Continue.", sim_model=None,
                   token="tok", persona="persona", host=None, session_cls=Sess)


def test_a_cap_stopped_fork_is_not_ok(tmp_path, monkeypatch):
    bdir = _fork_dir(tmp_path)
    assert _fork(tmp_path, monkeypatch, bdir, cap=True)["outcome"] == "run_cap"
    assert _fork(tmp_path / "b", monkeypatch, _fork_dir(tmp_path / "b"),
                 cap=False)["outcome"] == "ok"


def test_forks_refuse_boundaries_that_are_not_fork_sources(tmp_path, monkeypatch):
    bdir = _fork_dir(tmp_path, {"ineligible_reason": "services_unvalidated"})
    with pytest.raises(CC.ForkMismatch):
        _fork(tmp_path, monkeypatch, bdir, cap=False)


def test_tolerant_exec_is_the_shared_one(monkeypatch):
    seen = {}

    def shared(sb, cmd, *, timeout_s, workdir="/app", stats=None, env=None, log=True):
        seen.update(cmd=cmd, timeout_s=timeout_s, stats=stats, env=env)
        return N.SB.ExecResult(124, "", "command timed out", 1.0)

    monkeypatch.setattr(N.I, "tolerant_exec", shared)
    sb = N.SB.Sandbox(name="c1", image="i", purpose="p")
    sb.exec_stats = {}
    r = N.tolerant_exec(sb)("git status", timeout_s=5, env={"A": "1"})
    assert r.rc == 124 and seen["stats"] is sb.exec_stats and seen["env"] == {"A": "1"}


def test_window_hold_expires_with_the_window():
    import importlib.util
    spec = importlib.util.spec_from_file_location("s2n", "scripts/s2_native.py")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    rate = {"unifiedWindows": {"seven_day": {"utilization": 0.93, "resetsAt": 2000,
                                             "status": "allowed"},
                               "five_hour": {"utilization": 0.2, "resetsAt": 1500,
                                             "status": "allowed"}}}
    why, wait_s = S.window_hold(rate, 1000)
    assert why.startswith("seven_day") and wait_s == 1000
    assert S.window_hold(rate, 2001) == (None, 0.0)  # reset since: stale, no hold
    rej = {"unifiedWindows": {"five_hour": {"status": "rejected", "resetsAt": 1100}}}
    assert S.window_hold(rej, 1000)[0].startswith("five_hour")
    assert S.window_hold({"unifiedWindows": {"seven_day": {"utilization": 0.5}}}, 1)[0] is None


def test_a_vanished_container_is_infra():
    r = N.StreamResult(-1, 9.0, False, "container_gone")
    assert CC._step_ok(r, {}).startswith("infra_killed")
    assert N.code_version() is N.code_version()  # frozen once per process


def test_output_lost_ends_a_claude_code_run_as_infra(tmp_path, monkeypatch):
    def lost(*a, **k):
        Path(k["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        k["on_line"](json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none"}))
        return N.StreamResult(0, 3.0, False, "output_lost")

    monkeypatch.setattr(CC, "ensure_cli_volume", lambda h, i: CC.CLI_VOLUME)
    monkeypatch.setattr(N, "egress_check", lambda sb, e, u: {"ok": True, "checks": []})
    monkeypatch.setattr(N, "detached_exec", lost)
    cfg = CC.BaseConfig(task=Task(), root=tmp_path, host=None)
    res = CC.run_base(cfg, sim_model=None, token="tok", session_cls=FakeSession)
    assert res["end"] == "infra_killed" and "output_lost" in res["error"]
