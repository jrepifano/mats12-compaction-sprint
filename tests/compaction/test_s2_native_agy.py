"""compaction.s2.native.agy: the Antigravity arm's turn hook, transcript conversion and
boundary layout ($0, no Docker, no model calls). Step shapes follow agy 1.4.7's
transcript_full.jsonl as seen live on 24 Oct (MCP calls through call_mcp_tool, tool output
with Created/Completed stamps, a /compact input, SYSTEM_MESSAGE notices, CHECKPOINT)."""

from __future__ import annotations

import json
import subprocess

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
