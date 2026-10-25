"""Planted native runner tests: no containers, agent calls or simulator calls."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from compaction.s2 import interact as I
from compaction.s2 import interact_run as IR
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N
from test_s2_native import FakeSession, StubSim, T, Task, _fork_dir


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live Docker forbidden in build tests")
    monkeypatch.setattr(N.SB, "_docker", forbidden)


def test_capture_ask_is_terminal_and_does_not_touch_simulator():
    class Sim(StubSim):
        def ask(self, question):
            raise AssertionError("simulator must not run")
    sim = Sim()
    before = sim.export()
    captured = []
    server = N.SimServer(sim, bind="127.0.0.1", port=1, capture_ask=captured.append)
    with pytest.raises(N.ProbeCaptured):
        server.ask("Done, please review.")
    assert sim.export() == before
    assert server.rows == []
    assert captured[0]["text"] == "Done, please review."
    assert server.captured_asks == captured
    with pytest.raises(RuntimeError, match="forbidden"):
        CC.NoSimulatorModel().complete([], None, 10)


def test_general_insertion_exact_lines_and_frozen_position():
    message = "wrapper\n<summary>body α\nsecond</summary>\nclosing guidance"
    body = "<summary>body α\nsecond</summary>"
    lines = ["  exact whitespace  ", "arbitrary λ statement"]
    inserted = CC.insert_summary_lines(message, body, lines)
    assert inserted == "wrapper\n<summary>body α\nsecond\n\n  exact whitespace  \narbitrary λ statement</summary>\nclosing guidance"
    assert CC.insert_summary_lines(message, body, []) == message
    with pytest.raises(ValueError):
        CC.insert_summary_lines(message, body, ["line\nbreak"])
    with pytest.raises(ValueError, match="not found"):
        CC.insert_summary_lines(message, "different body", ["claim"])


def test_context_verifier_checks_tail_attachments_and_probe():
    checked = CC.verify_effective_context("repair rule", [{"text": "tail secret"}],
        ["attached secret"], "probe secret", required=["repair rule"],
        forbidden=["tail secret", "attached secret", "probe secret"])
    assert not checked["ok"]
    assert checked["hits"]["tail secret"] == ["preserved_tail"]
    assert len(checked["leaked"]) == 3
    with pytest.raises(ValueError):
        CC.verify_effective_context("", None, [], "")


def test_cli_enforces_fixed_maximum_calls():
    cmd = CC.cli_command(CC.CCConfig(), prompt="Finish the remaining step.", max_calls=30)
    assert "--max-turns 30" in cmd
    assert "Finish the remaining step." in cmd
    with pytest.raises(ValueError):
        CC.cli_command(CC.CCConfig(), max_calls=0)


@pytest.mark.parametrize("end", ["ask_user", "submission", "call_cap"])
def test_probe_fork_zero_simulator_and_terminal_capture(tmp_path, monkeypatch, end):
    bdir = _fork_dir(tmp_path)
    monkeypatch.setattr(I, "fingerprint", lambda runner, version=3: "v3 fp")
    monkeypatch.setattr(IR, "sim_probe", lambda sim: "probe")
    holder = {}

    class Sess(FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            holder["ns"] = self
            def no_ask(question):
                raise AssertionError("simulator invoked")
            self.server.sim.ask = no_ask

    def execute(sb, cmd, **kwargs):
        assert "--max-turns 30" in cmd and "Finish now." in cmd
        callback, path = kwargs["on_line"], Path(kwargs["log_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        input_path = next(p for p in sb.files if p.endswith(".jsonl"))
        rows = CC.read_jsonl(sb.files[input_path])
        callback(json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none",
                             "session_id": "fork-1"}))
        sb.files[f"{CC.PROJECT}/fork-1.jsonl"] = "".join(json.dumps(r) + "\n" for r in rows)
        if end == "ask_user":
            path.write_text("")
            with pytest.raises(N.ProbeCaptured):
                holder["ns"].server.ask("The work is ready for review.")
            assert kwargs["stop"].is_set()
            return N.StreamResult(-9, 0.1, False, "stop")
        event = {"type": "result", "subtype": "success" if end == "submission" else "error_max_turns",
                 "is_error": end != "submission", "result": "Finished."}
        path.write_text(json.dumps(event) + "\n")
        callback(json.dumps(event))
        return N.StreamResult(0 if end == "submission" else 1, 0.1, False)

    monkeypatch.setattr(N, "detached_exec", execute)
    result = CC.fork(bdir, tmp_path / "probe", new_summary="new summary", prompt="Finish now.",
                     sim_model=CC.NoSimulatorModel(), persona="persona", token="test-token",
                     host=None, session_cls=Sess, probe=True)
    assert result["outcome"] == end
    assert result["sim_calls"] == 0 and result["sim_cost_usd"] == 0
    assert result["fork_check"]["ok"]
    evidence = json.loads((tmp_path / "probe/evidence.json").read_text())
    if end == "ask_user":
        assert evidence["events"][-1]["kind"] == "ask_user"
    assert not evidence["capture_complete"]  # no tool hooks in this fake CLI


def test_trace_captures_edit_and_revert_in_same_command():
    trace = '\n'.join([
        '101 123.100 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 3</app/call.py>',
        '101 123.101 write(3</app/call.py>, "missing_helper()\\n", 17) = 17',
        '101 123.102 close(3</app/call.py>) = 0',
        '101 123.103 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 3</app/call.py>',
        '101 123.104 write(3</app/call.py>, "pass\\n", 5) = 5',
        '101 123.105 close(3</app/call.py>) = 0',
    ])
    result = CC.parse_mutation_trace(trace, {"/app/call.py": "pass\n"})
    assert result["capture_complete"], result["issues"]
    assert result["after"] == {"/app/call.py": "pass\n"}
    assert any(e["data"]["after"] == "missing_helper()\n" for e in result["events"])
    assert result["events"][-1]["data"]["after"] == "pass\n"


def test_trace_replays_atomic_rename_and_rejects_unsupported_target_mutation():
    trace = '\n'.join([
        '101 123.100 openat(AT_FDCWD, "/app/.tmp", O_WRONLY|O_TRUNC|O_CREAT|O_EXCL) = 3</app/.tmp>',
        '101 123.101 write(3</app/.tmp>, "missing_helper()\\n", 17) = 17',
        '101 123.102 rename("/app/.tmp", "/app/call.py") = 0',
    ])
    result = CC.parse_mutation_trace(trace, {"/app/call.py": "pass\n"})
    assert result["capture_complete"]
    assert result["events"][-1]["data"]["after"] == "missing_helper()\n"
    result = CC.parse_mutation_trace('101 123.100 writev(3</app/call.py>, [], 0) = 0',
                                     {"/app/call.py": "pass\n"})
    assert not result["capture_complete"]


def test_trace_unfinished_write_is_joined_at_completion():
    trace = '\n'.join([
        '101 123.100 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 3</app/call.py>',
        '101 123.101 write(3</app/call.py>, "x()", 3 <unfinished ...>',
        '101 123.102 <... write resumed>) = 3',
    ])
    result = CC.parse_mutation_trace(trace, {"/app/call.py": ""})
    assert result["capture_complete"]
    assert result["after"]["/app/call.py"] == "x()"


def test_transcript_cannot_claim_shell_mutation_coverage():
    t = T()
    t.assistant("m", [{"type": "tool_use", "id": "t", "name": "Bash",
                       "input": {"command": "python edit_then_revert.py"}}])
    t.tool_result("t", "")
    evidence = CC.ordered_evidence(t.rows)
    assert not evidence["capture_complete"]
    assert "intra-call audit" in evidence["issues"][0]


def test_continue_from_boundary_preserves_state_and_captures_later_boundary(tmp_path, monkeypatch):
    bdir = _fork_dir(tmp_path / "source")
    b = json.loads((bdir / "boundary/boundary.json").read_text())
    b["stage"] = CC.GRADED_STEP
    (bdir / "boundary/boundary.json").write_text(json.dumps(b))
    holder = {}
    monkeypatch.setattr(CC, "TRANSCRIPT_POLL_S", 0.01)
    monkeypatch.setattr(CC, "POST_REQUEST_WAIT_S", 0.03)
    monkeypatch.setattr(CC, "ensure_cli_volume", lambda *args: None)
    monkeypatch.setattr(N, "egress_check", lambda *args: {"ok": True})
    monkeypatch.setattr(I, "fingerprint", lambda runner, version=3: "v3 fp")
    monkeypatch.setattr(IR, "sim_probe", lambda sim: "probe")
    monkeypatch.setattr(N, "freeze", lambda *args: {})
    monkeypatch.setattr(N, "signal_pids", lambda *args: None)
    monkeypatch.setattr(N, "secret_scan", lambda *args: {"hits": [], "scanned": 1, "seconds": 0})
    monkeypatch.setattr(N, "snapshot", lambda sb, keep, k: {"tag": f"kept-{k}", "snapshot_s": 0})

    class Sess(FakeSession):
        def __init__(self, arm, image, factory, **kwargs):
            super().__init__(arm, image, factory, **kwargs)
            assert image == b["tag"] and kwargs["setup_git"] is False
            holder["ns"] = self
            self.before_close = kwargs["before_close"]

        def __exit__(self, *args):
            assert not self.before_close()

    def execute(sb, cmd, **kwargs):
        assert "--resume " in cmd and "--fork-session" not in cmd
        assert kwargs["stdin"] == b"Continue."
        sid = cmd.split("--resume ")[1].split()[0]
        path = f"{CC.PROJECT}/{sid}.jsonl"
        t = T(sid)
        t.rows = CC.read_jsonl(sb.files[path])
        t.n = 1000
        t.leaf = t.rows[-1]["uuid"]
        kwargs["on_line"](json.dumps({"type": "system", "subtype": "init",
                                      "apiKeySource": "none", "session_id": sid}))
        reply = holder["ns"].server.boundary({"hook_event_name": "PostCompact", "session_id": sid,
            "transcript_path": path, "compact_summary": "<summary>continued summary</summary>"})
        assert reply["ok"]
        t.compact([], "continued summary", 78000, 5000)
        sb.files[path] = "".join(json.dumps(row) + "\n" for row in t.rows)
        kwargs["on_line"](json.dumps({"type": "result", "subtype": "success", "is_error": False}))
        return N.StreamResult(0, 0.1, False)

    monkeypatch.setattr(N, "detached_exec", execute)
    cfg = CC.BaseConfig(task=Task(), root=tmp_path / "continued", host=None,
                        steps=(CC.GRADED_STEP,), stop_after=1)
    result = CC.continue_from_boundary(bdir, cfg.run_dir, cfg=cfg,
        sim_model=None, token="token", session_cls=Sess)
    assert result["fork_check"]["ok"]
    assert result["continued_from"] == str(bdir)
    assert result["boundaries"] == [b["boundary_index"] + 1], result
    new = json.loads((cfg.run_dir / f'b{b["boundary_index"] + 1}/boundary/boundary.json').read_text())
    assert new["pre_tokens"] == 78000


@pytest.mark.parametrize("argv", [
    "timeout -k 5 300 tail -c +123 -F /opt/emx/steps/02_implement-abc123/out",
    "tail -c +1 -F /opt/emx/steps/02_implement-abc123/out",
])
def test_exact_harness_followers_are_exempt(argv):
    assert N.is_harness_log_follower(argv)
    classified = N.classify({1: (0, "S", "init"), 10: (1, "S", argv)}, "token", "cli", "hook")
    assert classified["strays"] == [] and classified["harness_followers"] == [10]


@pytest.mark.parametrize("argv", [
    "timeout -k 5 600 tail -c +1 -F /opt/emx/steps/task/out",
    "tail -c +1 -F /app/out",
    "tail -c +1 -F /opt/emx/steps/task/err",
    "tail -c +1 -F /opt/emx/steps/../out",
    "tail -c +1 -F /opt/emx/steps/task/out ; python change.py",
    "bash -c tail -c +1 -F /opt/emx/steps/task/out",
    "tail -c +0 -F /opt/emx/steps/task/out",
    "node server.js", "python test.py", "sleep 5",
])
def test_nonexact_followers_and_other_processes_are_never_exempt(argv):
    assert not N.is_harness_log_follower(argv)
    assert N.classify({10: (1, "S", argv)}, "token", "cli", "hook")["strays"] == [10]


def test_existing_boundary_reclassification_is_copy_and_only_fixes_quiescence():
    fields = {"quiescent": False, "ineligible_reason": "not_quiescent", "eligible": False,
        "processes": [
            "20 1 S timeout -k 5 300 tail -c +300 -F /opt/emx/steps/task/out",
            "21 20 S tail -c +300 -F /opt/emx/steps/task/out"], "services_running": []}
    changed = N.reclassify_harness_quiescence(fields)
    assert changed["quiescent"] and "ineligible_reason" not in changed
    assert changed["eligible"] is None
    assert fields["quiescent"] is False and fields["ineligible_reason"] == "not_quiescent"
    assert changed["harness_exempted_processes"] == fields["processes"]
    fields["processes"].append("22 1 S python agent-background.py")
    assert N.reclassify_harness_quiescence(fields) == fields
    fields["processes"].pop()
    fields["services_running"] = [55]
    assert N.reclassify_harness_quiescence(fields)["ineligible_reason"] == "services_unvalidated"


def test_freeze_records_exemptions_and_still_stops_real_work(monkeypatch):
    procs = {1: (0, "S", "init"),
             10: (1, "S", "tail -c +7 -F /opt/emx/steps/run/out"),
             11: (1, "S", "python work.py")}
    monkeypatch.setattr(N, "ps_table", lambda sb: (dict(procs), "token"))
    calls = []
    def signal(sb, pids, sig):
        calls.append((pids, sig))
        for pid in pids:
            parent, _, argv = procs[pid]
            procs[pid] = (parent, "T", argv)
    monkeypatch.setattr(N, "signal_pids", signal)
    sb = SimpleNamespace()
    stopped = N.freeze(sb, "cli", "hook")
    assert list(stopped) == [11]
    assert calls == [([11], "STOP")]
    assert list(sb.harness_exempted_processes) == [10]


def test_delivery_distance_is_labelled_estimate_over_messages_not_context_sizes():
    t = T()
    t.assistant("first", [{"type": "tool_use", "id": "ask", "name": CC.ASK_TOOL,
                           "input": {"question": "What next?"}}])
    t.tool_result("ask", json.dumps({"result": "Reply plus the exact rule."}))
    t.assistant("next", [{"type": "text", "text": "I will apply the rule now."}])
    t.compact([], "short summary", 80000, 5000)
    exported = {"ask_log": [{"call_index": 3, "reply": "Reply plus the exact rule."}],
                "planting": {"boundaries": [{"compaction_index": 1, "pre_tokens": None,
                  "obligations": [{"obligation_id": "r", "delivery_call_index": 3,
                                    "token_distance": None}]}]}}
    out = CC.annotate_delivery_distances(t.rows, 1, exported, 1)
    item = out["planting"]["boundaries"][0]["obligations"][0]
    assert item["token_distance"] > 0
    assert item["token_distance_source"] == "transcript_message_utf8_bytes_div4_estimate"
    assert exported["planting"]["boundaries"][0]["obligations"][0]["token_distance"] is None


def test_usage_sums_distinct_new_responses_only():
    rows = [{"type": "assistant", "message": {"id": "a", "usage": {"input_tokens": 3, "output_tokens": n}}}
            for n in (1, 4)]
    rows += [{"type": "assistant", "message": {"id": "b", "usage": {"input_tokens": 6, "output_tokens": 2}}}]
    usage = CC.transcript_usage(rows)
    assert usage["responses"] == 2 and usage["input_tokens"] == 9 and usage["output_tokens"] == 6
    assert CC.transcript_usage([]) is None


def test_native_read_default_does_not_prove_complete_file():
    t = T()
    t.assistant("m", [{"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/app/file.py"}}])
    t.tool_result("r", "1  first line")
    event = CC.ordered_evidence(t.rows)["events"][0]
    assert event["kind"] == "read" and not event["data"]["complete"]


def test_native_grep_becomes_search_event():
    t = T()
    t.assistant("m", [{"type": "tool_use", "id": "g", "name": "Grep", "input": {
        "pattern": "missing_helper", "path": "/app/helpers.py", "output_mode": "content"}}])
    t.tool_result("g", "No matches found")
    event = CC.ordered_evidence(t.rows)["events"][0]
    assert event["kind"] == "search"
    assert event["data"]["pattern"] == "missing_helper"
