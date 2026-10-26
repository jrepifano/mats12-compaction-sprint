"""Planted native runner tests: no containers, agent calls or simulator calls."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_s2_native import FakeSession, StubSim, T, Task, _fork_dir

from compaction.s2 import interact as I
from compaction.s2 import interact_run as IR
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N


def test_audit_http_route_returns_exact_hook_schema_without_simulator():
    import urllib.error
    import urllib.request
    pytest.importorskip("fastmcp")
    expected = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "allow",
                                      "updatedInput": {"command": "true"}}}
    sim = StubSim()
    server = N.SimServer(sim, bind="127.0.0.1")
    server.on_audit = lambda payload: expected
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}" + N.AUDIT_PATH
        data = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"}).encode()
        request = urllib.request.Request(url, data, {"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=5)
        assert denied.value.code == 403
        request.add_header(N.TOKEN_HEADER, server.token)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.load(response) == expected
        assert server.rows == [] and len(server.audit_calls) == 1
    finally:
        assert server.stop() == []


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
    with pytest.raises(N.ProbeCaptured):
        server.ask("A racing second question.")
    assert len(server.captured_asks) == 1 and len(captured) == 1
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
    trace = '\n'.join([  # noqa: FLY002 - one literal line per syscall fixture
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
    trace = '\n'.join([  # noqa: FLY002 - one literal line per syscall fixture
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
    trace = '\n'.join([  # noqa: FLY002 - one literal line per syscall fixture
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


@pytest.mark.parametrize("damaged", [False, True])
def test_continue_from_boundary_preserves_state_and_captures_later_boundary(tmp_path, monkeypatch, damaged):
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
        Path(kwargs["log_path"]).parent.mkdir(parents=True, exist_ok=True)
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
        t.compact([], "wrong summary" if damaged else "continued summary", 78000, 5000)
        sb.files[path] = "".join(json.dumps(row) + "\n" for row in t.rows)
        kwargs["on_line"](json.dumps({"type": "result", "subtype": "success", "is_error": False}))
        return N.StreamResult(0, 0.1, False)

    monkeypatch.setattr(N, "detached_exec", execute)
    cfg = CC.BaseConfig(task=Task(), root=tmp_path / "continued", host=None,
                        steps=(CC.GRADED_STEP,), stop_first_eligible=True, capture_ask=True)
    result = CC.continue_from_boundary(bdir, cfg.run_dir, cfg=cfg,
        sim_model=None, token="token", session_cls=Sess)
    assert result["fork_check"]["ok"]
    assert result["continued_from"] == str(bdir)
    assert result["token_usage_anchored_to_prompt"] is False  # fake transcript omits the Continue prompt
    assert result["token_usage_window"]["anchored_to_prompt"] is False
    assert result["boundaries"] == [b["boundary_index"] + 1], result
    new = json.loads((cfg.run_dir / f'b{b["boundary_index"] + 1}/boundary/boundary.json').read_text())
    assert new["pre_tokens"] == 78000
    assert new["eligible"] is (not damaged)
    assert result["end"] == ("submitted" if damaged else "eligible")
    if damaged:
        assert new["ineligible_reason"] == "invalid_transcript"


@pytest.mark.parametrize("argv", [
    "timeout -k 5 300 tail -c +123 -F /opt/emx/steps/02_implement-abc123/out",
    "tail -c +1 -F /opt/emx/steps/02_implement-abc123/out",
])
def test_exact_harness_followers_are_exempt(argv):
    assert N.is_harness_log_follower(argv)
    classified = N.classify({1: (0, "S", "init"), 10: (1, "S", argv)}, "token", "cli", "hook")
    assert classified["strays"] == [10] and classified["harness_followers"] == []


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
            "20 0 S timeout -k 5 300 tail -c +300 -F /opt/emx/steps/task/out",
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
             9: (0, "S", "timeout -k 5 300 tail -c +7 -F /opt/emx/steps/run/out"),
             10: (9, "S", "tail -c +7 -F /opt/emx/steps/run/out"),
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
    assert list(sb.harness_exempted_processes) == [9, 10]


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


def test_strict_probe_transcripts_reject_missing_or_nonobject_evidence():
    with pytest.raises(ValueError):
        CC.read_jsonl('{"type":"assistant"}\n{"truncated":', strict=True)
    with pytest.raises(ValueError):
        CC.read_jsonl('[]\n', strict=True)


def test_audit_detects_both_hooks_silently_missing_from_a_tool_call():
    audit = object.__new__(CC.ProbeAudit)
    audit.events, audit.issues, audit.pending = [], [], {}
    audit.pre_ids, audit.post_ids = set(), set()
    audit.hooks_seen, audit.trace_available, audit.paths = 1, False, []
    audit.lock = threading.RLock()
    t = T()
    t.assistant("m", [{"type": "tool_use", "id": "unobserved", "name": "Bash",
                       "input": {"command": "git commit -m bad"}}])
    t.tool_result("unobserved", "committed")
    evidence = audit.finish(t.rows, submission="Done.")
    assert not evidence["part_a_complete"] and not evidence["capture_complete"]
    assert any("hook coverage missing" in issue for issue in evidence["issues"])


def test_search_adapter_preserves_filters_and_glob_for_unknown_checks():
    for name in ("Grep", "Glob"):
        t = T()
        args = {"pattern": "helper", "path": "/app", "type": "py", "head_limit": 10,
                "offset": 2, "-i": True, "multiline": True}
        t.assistant("m", [{"type": "tool_use", "id": "g", "name": name, "input": args}])
        t.tool_result("g", "No matches found")
        event = CC.ordered_evidence(t.rows)["events"][0]
        assert event["kind"] == "search" and event["data"]["tool"] == name
        assert event["data"]["arguments"] == args and event["data"]["truncated"]
        assert event["data"]["type"] == "py" and event["data"]["ignore_case"]


def _docker_smoke_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("planted_docker_smoke", "scripts/s2_planted_docker_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_smoke_is_import_safe_and_runs_only_bounded_fake_container(tmp_path, monkeypatch):
    module = _docker_smoke_module()  # autouse no_docker makes any import-time invocation fail
    seen = {}
    class Invocation:
        run_id = "fake-run"
        def __init__(self, **kwargs):
            seen["invocation"] = kwargs
        def cleanup(self):
            seen["cleaned"] = True
            return []
    monkeypatch.setattr(module.SB, "Invocation", Invocation)
    monkeypatch.setattr(module, "preflight", lambda image, host: "sha256:already-local")
    def start(inv, image, **kwargs):
        seen["container"] = {"image": image, **kwargs}
        return SimpleNamespace(name="fake")
    monkeypatch.setattr(module.N, "start_task_container", start)
    monkeypatch.setattr(module, "exercise", lambda sb, directory: {"ok": True})
    out = tmp_path / "smoke.json"
    report = module.run("existing-image", out)
    assert report["ok"] and seen["cleaned"]
    assert seen["container"]["network"] == "none"
    assert seen["container"]["memory_mb"] == 512 and seen["container"]["cpus"] == 1
    assert seen["container"]["lifetime_s"] == 180
    assert seen["container"]["extra_args"] == (*CC.run_args(), "--pull", "never", "--entrypoint", "")
    assert report["agent_calls"] == 0 and report["paid_calls"] == 0
    with pytest.raises(FileExistsError):
        module.run("existing-image", out)


def test_docker_smoke_reports_cleanup_failure_as_failure(tmp_path, monkeypatch):
    module = _docker_smoke_module()
    class Invocation:
        def __init__(self, **kwargs):
            pass
        def cleanup(self):
            return ["container unresolved"]
    monkeypatch.setattr(module.SB, "Invocation", Invocation)
    def missing(*args):
        raise RuntimeError("image unavailable; never pull")
    monkeypatch.setattr(module, "preflight", missing)
    result = module.run("missing", tmp_path / "failed.json")
    assert not result["ok"] and result["cleanup_unconfirmed"] == ["container unresolved"]
    assert "never pull" in result["error"]


@pytest.mark.parametrize("text", ["No files found", "No matches found\n"])
def test_native_grep_successful_no_match_has_explicit_empty_result_and_raw_evidence(text):
    t = T()
    t.assistant("m", [{"type": "tool_use", "id": "g", "name": "Grep", "input": {
        "pattern": "missing_helper", "path": "/app/helpers.py"}}])
    t.tool_result("g", text)
    data = CC.ordered_evidence(t.rows)["events"][0]["data"]
    assert data["exit_code"] == 1 and data["output"] == "" and data["raw_output"] == text
    assert data["output_mode"] == "files_with_matches" and data["completed"]
    t.rows[-1]["message"]["content"][0]["is_error"] = True
    failed = CC.ordered_evidence(t.rows)["events"][0]["data"]
    assert not failed["completed"] and failed["output"] == text
    assert "raw_output" not in failed


@pytest.mark.parametrize('parent,child,offset', [(1, 20, 300), (0, 1, 300), (0, 20, 301)])
def test_follower_pair_requires_external_parent_and_matching_child(parent, child, offset):
    fields = {'quiescent': False, 'ineligible_reason': 'not_quiescent', 'processes': [
        f'20 {parent} S timeout -k 5 300 tail -c +300 -F /opt/emx/steps/task/out',
        f'21 {child} S tail -c +{offset} -F /opt/emx/steps/task/out']}
    assert N.reclassify_harness_quiescence(fields) == fields


def test_trace_real_alignment_dup_fork_relative_rename_and_cache_scope():
    trace = '\n'.join([  # noqa: FLY002 - one literal line per syscall fixture
        '100 123.001 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 3</app/call.py>',
        '100 123.002 dup2(3</app/call.py>, 1</dev/null>) = 1</app/call.py>',
        '100 123.003 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>',
        '101 123.004 write(1</app/call.py>, "missing_", 8) = 8',
        '100 123.005 <... clone resumed>)     = 101',
        '100 123.006 write(3</app/call.py>, "helper()", 8) = 8',
        '100 123.007 openat(AT_FDCWD, "/app/cache.bin", O_WRONLY|O_TRUNC) = 4</app/cache.bin>',
        '100 123.008 write(4</app/cache.bin>, "\\xff\\x00", 2) = 2',
        '100 123.009 mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, 3</app/call.py>, 0) = 0x1234',
        '100 123.010 chdir("/app/sub")         = 0',
        '100 123.011 openat(AT_FDCWD, "../tmp", O_WRONLY|O_TRUNC|O_CREAT) = 4</app/tmp>',
        '100 123.012 fcntl(4</app/tmp>, F_DUPFD_CLOEXEC, 10) = 10</app/tmp>',
        '100 123.013 write(10</app/tmp>, "pass\\n", 5) = 5',
        '100 123.014 rename("../tmp", "../call.py") = 0',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']
    assert any(e['data']['after'] == 'missing_helper()' for e in result['events'])
    assert result['after'] == {'/app/call.py': 'pass\n'}


@pytest.mark.parametrize('write', [
    'write(3</app/call.py>, ""..., 17) = 17',
    'write(3</app/call.py>, "missing_"..., 17) = 17',
    'mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, 3</app/call.py>, 0) = 0x1234',
])
def test_trace_truncation_and_shared_mapping_fail_closed(write):
    result = CC.parse_mutation_trace(
        '100 123.001 openat(AT_FDCWD, "/app/call.py", O_RDWR) = 3</app/call.py>\n100 123.002 ' + write,
        {'/app/call.py': 'pass\n'})
    assert not result['capture_complete']


class AuditSandbox:
    def __init__(self, preflight_rc=0):
        self.commands = []
        self.preflight_rc = preflight_rc
        self.reflog = 'new\x00HEAD@{0}\n'

    def exec(self, command, **kwargs):
        self.commands.append(command)
        stdout, rc = '', 0
        if command == 'git rev-list --all --reflog':
            stdout = 'old\nnew\n'
        elif command.startswith('git reflog --all'):
            stdout = self.reflog
        elif command.startswith('git reflog show HEAD'):
            stdout = 'old\nnew\n'
        elif command.startswith('if [ -x'):
            stdout = '/usr/bin/strace'
        elif command.startswith('/usr/bin/strace '):
            rc = self.preflight_rc
        elif command.startswith('python3 -c'):
            stdout = json.dumps(['/app/call.py'])
        return SimpleNamespace(stdout=stdout, stderr='ptrace denied' if rc else '', rc=rc)


def test_part_a_command_capture_survives_missing_trace_and_timeout(monkeypatch):
    sb = AuditSandbox(preflight_rc=1)
    audit = CC.ProbeAudit(sb, mutation_paths=['/app/call.py'])
    assert not audit.trace_available
    assert '-s 4194304' in sb.commands[-1] and 'raw=read,readv' in sb.commands[-1]
    def read_files(sb, paths):
        return {path: 'pass\n' if path.endswith('.py') else None for path in paths}
    monkeypatch.setattr(CC, '_read_files', read_files)
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'b',
               'cwd': '/app', 'tool_input': {'command': 'sleep 10'}}
    response = audit.hook(payload)
    command = response['hookSpecificOutput']['updatedInput']['command']
    assert command.startswith('(\n') and 'bash -c ' not in command
    assert 'ok' not in response
    assert audit.hook({**payload, 'hook_event_name': 'PostToolUseFailure',
                       'tool_response': {'timed_out': True}}) == {}
    t = T()
    t.assistant('m', [{'type': 'tool_use', 'id': 'b', 'name': 'Bash', 'input': payload['tool_input']}])
    t.tool_result('b', 'Command timed out')
    result = audit.finish(t.rows)
    assert result['part_a_complete'] and not result['capture_complete'], result
    event = next(e['data'] for e in result['events'] if e['kind'] == 'command')
    assert event['exit_code'] is None and not event['completed']


def test_part_a_skips_strace_preflight_and_old_checkout_is_not_new_commit():
    sb = AuditSandbox()
    audit = CC.ProbeAudit(sb)
    assert not any('strace' in cmd for cmd in sb.commands)
    audit.hook({'hook_event_name': 'SessionStart'})
    sb.reflog += 'old\x00HEAD@{1}\n'
    audit._record_commits()
    assert not audit.events
    assert audit.finish([], submission='Done')['part_a_complete']
    assert 'Task' in CC.DISALLOWED and 'Agent' in CC.DISALLOWED


def test_hook_exception_is_durable_and_never_claims_post_hook_observed(monkeypatch):
    stop = threading.Event()
    audit = CC.ProbeAudit(AuditSandbox(), stop=stop)
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'b',
               'cwd': '/app', 'tool_input': {'command': 'true'}}
    audit.hook(payload)
    def fail(*args):
        raise OSError('fixture cannot read output')
    monkeypatch.setattr(CC, '_read_files', fail)
    response = audit.hook({**payload, 'hook_event_name': 'PostToolUse'})
    assert response['continue'] is False and stop.is_set() and 'b' not in audit.post_ids
    result = audit.finish([])
    assert not result['part_a_complete'] and not result['capture_complete']
    assert any('audit hook error' in issue for issue in result['issues'])


def test_initial_context_uses_actual_request_and_excludes_later_assistant():
    summary, prompt = 'Summary with exact repair', 'Finish now.'
    rows = [{'type': 'user', 'uuid': 's', 'message': {'content': summary}},
            {'type': 'user', 'uuid': 'probe', 'message': {'content': prompt}}]
    msgs = [{'role': 'user', 'content': summary}, {'role': 'user', 'content': prompt},
            {'role': 'user', 'content': 'attached file contents'}]
    rows += [{'type': 'api-request-blob', 'hash': str(i), 'message': m} for i, m in enumerate(msgs)]
    rows += [{'type': 'api-request', 'id': 'r', 'base': None, 'keep': 0, 'tail': ['0', '1', '2']},
             {'type': 'assistant', 'message': {'content': 'later secret'}}]
    result = CC.resumed_context_check(rows, 's', summary, prompt,
                                     required=['exact repair'], forbidden=['later secret'])
    assert result['ok'], result
    assert result['surfaces']['attachments']
    assert not CC.resumed_context_check(rows, 's', summary, prompt,
                                       forbidden=['attached file contents'])['ok']
    assert not CC.resumed_context_check(rows, 's', 'different summary', prompt)['ok']
    assert not CC.resumed_context_check(rows[:1] + rows[-1:], 's', summary, prompt)['ok']


def test_capture_integrity_rejects_known_damaged_boundary():
    rows = [{'type': 'user', 'uuid': 's', 'parentUuid': None,
             'message': {'content': 'wrapper <summary>body</summary>'}}]
    assert CC.boundary_replay_integrity(rows, 's', '<summary>body</summary>')['ok']
    assert not CC.boundary_replay_integrity(rows, 's', '<summary>wrong</summary>')['ok']
    rows[0]['parentUuid'] = 'absent'
    assert not CC.boundary_replay_integrity(rows, 's', '<summary>body</summary>')['ok']
    rows[0]['parentUuid'] = None
    rows.append({'type': 'api-request', 'id': 'r', 'tail': ['missing-blob']})
    assert not CC.boundary_replay_integrity(rows, 's', '<summary>body</summary>')['ok']


def test_live_delivery_clock_update_holds_server_lock_and_copies_frozen_metadata():
    held = []
    class Lock:
        def __enter__(self):
            held.append(True)
        def __exit__(self, *args):
            held.pop()
    class Boundary(dict):
        def update(self, row):
            assert held
            super().update(row)
    row = Boundary(compaction_index=2)
    server = SimpleNamespace(_lock=Lock(), sim=SimpleNamespace(
        planting=SimpleNamespace(boundaries=[row])))
    frozen = {'planting': {'boundaries': [{'compaction_index': 2, 'obligations': [{'token_distance': 123}]}]}}
    CC.update_delivery_boundary(server, frozen, 2, 80000)
    assert row['pre_tokens'] == 80000
    frozen['planting']['boundaries'][0]['obligations'][0]['token_distance'] = 999
    assert row['obligations'][0]['token_distance'] == 123


@pytest.mark.parametrize('incomplete', [
    '101 123.101 read(0<pipe:[123]>, 0x1234, 0x1000 <unfinished ...>',
    '101 123.101 read(0<pipe:[123]>, 0x1234, 0x1000 <unfinished ...>\n101 123.102 <... read resumed>) = ?',
    '101 123.101 <... read resumed>) = ?',
    'diagnostic from unrelated process',
])
def test_incomplete_unrelated_reads_do_not_poison_mutation_scope(incomplete):
    result = CC.parse_mutation_trace(incomplete, {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']


def test_incomplete_target_write_remains_unknown():
    result = CC.parse_mutation_trace(
        '101 123.101 write(3</app/call.py>, "partial", 7 <unfinished ...>',
        {'/app/call.py': 'pass\n'})
    assert not result['capture_complete']


@pytest.mark.parametrize('operation', [
    'copy_file_range(3</app/call.py>, NULL, 4</app/backup.py>, NULL, 4096, 0)',
    'sendfile(4</app/backup.py>, 3</app/call.py>, NULL, 4096)',
    'splice(3</app/call.py>, NULL, 4</app/backup.py>, NULL, 4096, 0)',
])
def test_transfer_syscall_only_poisons_written_file(operation):
    result = CC.parse_mutation_trace('101 123.101 ' + operation + ' = 5', {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']
    result = CC.parse_mutation_trace('101 123.101 ' + operation + ' = 5', {'/app/backup.py': 'pass\n'})
    assert not result['capture_complete']


def test_replayed_edit_events_include_precommand_baseline():
    trace = '\n'.join([  # noqa: FLY002 - syscall fixtures
        '101 123.100 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 3</app/call.py>',
        '101 123.101 write(3</app/call.py>, "helper()\\n", 9) = 9',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/call.py': 'helper()\n'})
    assert result['capture_complete']
    assert len(result['events']) == 2
    assert all(event['data']['command_baseline'] == 'helper()\n' for event in result['events'])


def test_missing_wrapper_artifacts_is_diagnostic_unless_timeout(monkeypatch):
    audit = CC.ProbeAudit(AuditSandbox())
    monkeypatch.setattr(CC, '_read_files', lambda sb, paths: dict.fromkeys(paths))
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'b',
               'cwd': '/app', 'tool_input': {'command': 'true'}}
    audit.hook(payload)
    audit.hook({**payload, 'hook_event_name': 'PostToolUse', 'tool_response': {'stdout': ''}})
    assert 'bash wrapper artifacts absent' in audit.issues
    assert not audit.finish([])['part_a_complete']
    assert '-m 165 ' in CC.audit_hook_script()


def test_terminal_stop_distinguishes_unexecuted_from_executing_without_evidence():
    audit = CC.ProbeAudit(AuditSandbox())
    audit.hook({'hook_event_name': 'SessionStart'})
    t = T()
    t.assistant('m', [{'type': 'tool_use', 'id': 'never-started', 'name': 'Bash', 'input': {'command': 'true'}}])
    result = audit.finish(t.rows, terminal_stop=True)
    assert result['part_a_complete']
    assert result['unexecuted_after_stop'] == ['never-started']
    audit = CC.ProbeAudit(AuditSandbox())
    audit.hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'never-started',
                'cwd': '/app', 'tool_input': {'command': 'true'}})
    result = audit.finish(t.rows, terminal_stop=True)
    assert not result['part_a_complete'] and not result['unexecuted_after_stop']


def test_terminal_denied_call_without_result_is_unexecuted():
    stop = threading.Event()
    stop.set()
    audit = CC.ProbeAudit(AuditSandbox(), stop=stop)
    audit.hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'denied',
                'cwd': '/app', 'tool_input': {'command': 'true'}})
    t = T()
    t.assistant('m', [{'type': 'tool_use', 'id': 'denied', 'name': 'Bash', 'input': {'command': 'true'}}])
    result = audit.finish(t.rows, terminal_stop=True)
    assert result['part_a_complete'] and result['unexecuted_after_stop'] == ['denied']


def test_request_hash_recovery_merges_native_assistant_parts_without_guessing():
    import hashlib
    content = [{'type': 'thinking', 'thinking': '', 'signature': 'signed'},
               {'type': 'tool_use', 'id': 'old-tool', 'name': 'Read', 'input': {'file_path': '/app/a'}}]
    merged = {'role': 'assistant', 'content': content}
    digest = hashlib.sha256(json.dumps(merged, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    rows = [{'type': 'assistant', 'message': {'role': 'assistant', 'id': 'old-message', 'content': [part]}}
            for part in content]
    rows.append({'type': 'api-request', 'id': 'rq', 'tail': [digest]})
    assert CC.request_messages(rows, 2) == [merged]
    rows[1]['message']['content'][0]['input']['file_path'] = '/app/different'
    assert CC.request_messages(rows, 2) is None


def test_resumed_window_excludes_preserved_old_calls_after_summary_and_synthetic_turn():
    old = {'type': 'assistant', 'uuid': 'old-row', 'message': {'id': 'old-mid', 'content': [
        {'type': 'tool_use', 'id': 'old-tool', 'name': 'Edit', 'input': {}}]}}
    source = [old]
    rows = [{'type': 'user', 'uuid': 'summary', 'message': {'content': 'summary'}}, old,
            {'type': 'assistant', 'uuid': 'synthetic', 'message': {'content': 'No response requested.'}},
            {'type': 'user', 'uuid': 'probe', 'message': {'content': 'Finish now.'}},
            {'type': 'assistant', 'uuid': 'new-row', 'message': {'id': 'new-mid', 'content': [
                {'type': 'tool_use', 'id': 'new-tool', 'name': 'Bash', 'input': {'command': 'true'}}]}}]
    window = CC.resumed_window_rows(rows, source, 'Finish now.', cut=0)
    assert [row['uuid'] for row in window] == ['probe', 'new-row']
    audit = CC.ProbeAudit(AuditSandbox())
    audit.hook({'hook_event_name': 'SessionStart'})
    evidence = audit.finish(window, terminal_stop=True)
    assert evidence['part_a_complete'] and evidence['unexecuted_after_stop'] == ['new-tool']


def test_historical_resumed_fork_context_replay_if_archive_available():
    path = Path('../sprint-prep/data/raw/sprint/s2/native/claude_code/'
                'smoke-20261024T195802Z-kombu-single-active-consumer-priority-cc-haiku-a1/'
                'forks/b1-20261024T200556Z')
    if not path.exists():
        pytest.skip('historical read-only fork archive absent')
    source = CC.read_jsonl((path / 'input_transcript.jsonl').read_text(), strict=True)
    rows = CC.read_jsonl((path / 'transcript.jsonl').read_text(), strict=True)
    info = json.loads((path / 'fork.json').read_text())
    summary = next(row for row in rows if row.get('isCompactSummary'))
    result = CC.resumed_context_check(rows, summary['uuid'], summary['message']['content'],
                                      info['prompt'], source_rows=source)
    assert result['ok'] and result['request_index'] == 28, result
    window = CC.resumed_window_rows(rows, source, info['prompt'], cut=rows.index(summary))
    assert all(row.get('uuid') not in {r.get('uuid') for r in source if r.get('uuid')} for row in window)
    assert all(block.get('id') != 'toolu_01WCY1wFDHGqgQuHgYgF558z' for row in window
               for block in (row.get('message') or {}).get('content', []) if isinstance(block, dict))


def test_no_trace_wrapper_preserves_shell_snapshot_function_and_alias(tmp_path):
    import subprocess
    audit = CC.ProbeAudit(AuditSandbox())
    response = audit.hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'shell',
                           'cwd': str(tmp_path), 'tool_input': {'command': 'fixture_fn; fixture_alias'}})
    wrapped = response['hookSpecificOutput']['updatedInput']['command'].replace(CC.AUDIT_DIR, str(tmp_path))
    shell = "shopt -s expand_aliases\nfixture_fn() { printf 'function\\n'; }\nalias fixture_alias=\"printf 'alias\\\\n'\"\n" + wrapped
    result = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0 and result.stdout == 'function\nalias\n', result


def test_incomplete_transfer_does_not_poison_read_source():
    result = CC.parse_mutation_trace(
        '101 123.101 copy_file_range(3</app/call.py>, NULL, 4</app/backup.py>, NULL, 4096, 0 <unfinished ...>',
        {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']


def test_denied_call_with_error_tool_result_still_did_not_execute():
    stop = threading.Event()
    stop.set()
    audit = CC.ProbeAudit(AuditSandbox(), stop=stop)
    response = audit.hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'denied',
                           'cwd': '/app', 'tool_input': {'command': 'touch should-not-run'}})
    assert response['hookSpecificOutput']['permissionDecision'] == 'deny'
    t = T()
    t.assistant('m', [{'type': 'tool_use', 'id': 'denied', 'name': 'Bash',
                      'input': {'command': 'touch should-not-run'}}])
    t.tool_result('denied', 'Tool use denied by hook')
    t.rows[-1]['message']['content'][0]['is_error'] = True
    result = audit.finish(t.rows, terminal_stop=True)
    assert result['part_a_complete'] and result['unexecuted_after_stop'] == ['denied']
    assert not result['events']


@pytest.mark.parametrize('prompt_present', [True, False])
def test_usage_window_explicitly_reports_prompt_anchor(prompt_present):
    rows = [{'type': 'user', 'uuid': 's', 'message': {'content': 'summary'}}]
    if prompt_present:
        rows.append({'type': 'user', 'uuid': 'p', 'message': {'content': CC.CONTINUE_PROMPT}})
    rows.append({'type': 'assistant', 'uuid': 'a', 'message': {'id': 'm', 'content': 'answer',
                  'usage': {'input_tokens': 17, 'output_tokens': 2}}})
    metadata = {}
    window = CC.resumed_window_rows(rows, [], CC.CONTINUE_PROMPT, cut=0, metadata=metadata)
    assert metadata['anchored_to_prompt'] is prompt_present
    assert metadata['start_index'] == 1
    assert CC.transcript_usage(window)['input_tokens'] == 17


@pytest.mark.parametrize('annotation', ['pipe:[123]', 'socket:[456]', 'anon_inode:[eventfd]', '/dev/pts/0'])
def test_unfinished_write_to_known_nonfile_fd_does_not_poison_scope(annotation):
    result = CC.parse_mutation_trace(
        f'101 123.100 write(1<{annotation}>, "output", 6 <unfinished ...>',
        {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']


def test_unfinished_write_to_unresolved_fd_remains_unknown():
    result = CC.parse_mutation_trace('101 123.100 write(1, "output", 6 <unfinished ...>',
                                     {'/app/call.py': 'pass\n'})
    assert not result['capture_complete']


@pytest.mark.parametrize('operation', [
    'open("/usr/lib/lib.so", O_RDONLY|O_CLOEXEC',
    'openat(AT_FDCWD, "/app/unrelated.py", O_RDONLY',
])
def test_unfinished_unrelated_readonly_open_does_not_poison_scope(operation):
    result = CC.parse_mutation_trace(f'101 123.100 {operation} <unfinished ...>',
                                     {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']


@pytest.mark.parametrize('operation', [
    'openat(AT_FDCWD, "/app/temporary", O_WRONLY|O_CREAT',
    'openat(AT_FDCWD, "/app/call.py", O_RDONLY',
    'creat("/app/temporary", 0644',
])
def test_unfinished_mutating_or_target_open_remains_unknown(operation):
    result = CC.parse_mutation_trace(f'101 123.100 {operation} <unfinished ...>',
                                     {'/app/call.py': 'pass\n'})
    assert not result['capture_complete']


def test_reflink_copy_then_revert_preserves_transient_helper_call():
    trace = '\n'.join([  # noqa: FLY002 -- syscall order is the test contract
        '101 123.100 openat(AT_FDCWD, "/tmp/replacement.py", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3</tmp/replacement.py>',
        '101 123.101 write(3</tmp/replacement.py>, "helper()\\n", 9) = 9',
        '101 123.102 close(3</tmp/replacement.py>) = 0',
        '102 123.103 openat(AT_FDCWD, "/tmp/replacement.py", O_RDONLY) = 3</tmp/replacement.py>',
        '102 123.104 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 4</app/call.py>',
        '102 123.105 ioctl(4</app/call.py>, BTRFS_IOC_CLONE or FICLONE, 3</tmp/replacement.py>) = 0',
        '102 123.106 close(4</app/call.py>) = 0',
        '101 123.107 openat(AT_FDCWD, "/app/call.py", O_WRONLY|O_TRUNC) = 3</app/call.py>',
        '101 123.108 write(3</app/call.py>, "pass\\n", 5) = 5',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/call.py': 'pass\n'})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/call.py'] == 'pass\n'
    edits = [event['data'] for event in result['events']]
    assert any(row['after'] == 'helper()\n' and 'FICLONE' in row['syscall'] for row in edits)
    assert all(row['command_baseline'] == 'pass\n' for row in edits)
    assert 'ioctl' in CC.TRACE_SYSCALLS.split(',')


@pytest.mark.parametrize('clone_request', ['FICLONE', 'BTRFS_IOC_CLONE', '0x40049409'])
def test_full_clone_preserves_destination_tail_and_descriptor_offset(clone_request):
    trace = '\n'.join([
        '101 123.100 openat(AT_FDCWD, "/app/source", O_RDONLY) = 3</app/source>',
        '101 123.101 openat(AT_FDCWD, "/app/dest", O_RDWR) = 4</app/dest>',
        '101 123.102 lseek(4</app/dest>, 2, SEEK_SET) = 2',
        f'101 123.103 ioctl(4</app/dest>, {clone_request}, 3</app/source>) = 0',
        '101 123.104 write(4</app/dest>, "!", 1) = 1',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/source': 'x' * 4096, '/app/dest': 'y' * 8192})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/dest'] == 'xx!' + 'x' * 4093 + 'y' * 4096


@pytest.mark.parametrize('clone_request', ['FICLONERANGE', 'BTRFS_IOC_CLONE_RANGE', '0x4020940d'])
def test_range_clone_reads_source_slice_and_preserves_destination(clone_request):
    trace = ('101 123.100 ioctl(4</app/dest>, ' + clone_request
             + ', {src_fd=3</app/source>, src_offset=4096, src_length=4096, dest_offset=4096}) = 0')
    result = CC.parse_mutation_trace(trace, {'/app/source': 'a' * 4096 + 'b' * 4096,
                                           '/app/dest': 'z' * 12288})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/dest'] == 'z' * 4096 + 'b' * 4096 + 'z' * 4096


def test_range_clone_zero_length_means_source_eof_and_can_extend_with_hole():
    result = CC.parse_mutation_trace(
        '101 123.100 ioctl(4</app/dest>, FICLONERANGE, '
        '{src_fd=3</app/source>, src_offset=4096, src_length=0, dest_offset=4096}) = 0',
        {'/app/source': 'a' * 4096 + 'helper()\n', '/app/dest': ''})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/dest'] == '\0' * 4096 + 'helper()\n'


def test_clone_uses_inherited_duplicated_descriptors_without_annotations():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 openat(AT_FDCWD, "/app/source", O_RDONLY) = 3</app/source>',
        '101 123.101 openat(AT_FDCWD, "/app/dest", O_WRONLY|O_TRUNC) = 4</app/dest>',
        '101 123.102 dup(3</app/source>) = 5</app/source>',
        '101 123.103 clone(child_stack=NULL, flags=SIGCHLD) = 102',
        '102 123.104 ioctl(4, FICLONE, 5) = 0',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/source': 'helper()\n', '/app/dest': 'pass\n'})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/dest'] == 'helper()\n'


@pytest.mark.parametrize('clone', [
    'ioctl(4</app/dest>, FICLONE, 3</app/unknown>) = 0',
    'ioctl(4</app/dest>, FICLONERANGE, 0x7ffe0000) = 0',
    'ioctl(4</app/dest>, 0x123456, 0) = 0',
    'ioctl(4</app/dest>, FICLONE, 3</app/source> <unfinished ...>',
])
def test_unknown_clone_contents_remain_unknown_after_revert(clone):
    trace = '\n'.join([
        '101 123.100 ' + clone,
        '102 123.101 openat(AT_FDCWD, "/app/dest", O_WRONLY|O_TRUNC) = 4</app/dest>',
        '102 123.102 write(4</app/dest>, "pass\\n", 5) = 5',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/dest': 'pass\n'})
    assert not result['capture_complete']
    assert result['after']['/app/dest'] == 'pass\n'


def test_failed_clone_does_not_mutate_or_poison_capture():
    result = CC.parse_mutation_trace(
        '101 123.100 ioctl(4</app/dest>, FICLONE, 3</app/unknown>) = -1 EOPNOTSUPP (Operation not supported)',
        {'/app/dest': 'pass\n'})
    assert result['capture_complete'], result['issues']
    assert not result['events']


def test_clone_only_invalidates_destination_not_known_read_source():
    result = CC.parse_mutation_trace(
        '101 123.100 ioctl(4</app/untracked>, FICLONE, 3</app/source>) = 0',
        {'/app/source': 'helper()\n'})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/source'] == 'helper()\n'


def test_clone_into_temporary_then_rename_is_captured():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 openat(AT_FDCWD, "/tmp/replacement", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 4</tmp/replacement>',
        '101 123.101 ioctl(4</tmp/replacement>, FICLONE, 3</app/source>) = 0',
        '101 123.102 rename("/tmp/replacement", "/app/dest") = 0',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/source': 'helper()\n', '/app/dest': 'pass\n'})
    assert result['capture_complete'], result['issues']
    assert result['after']['/app/dest'] == 'helper()\n'


def test_clone_overlapping_another_write_has_unknown_ordering():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 ioctl(4</app/dest>, FICLONE, 3</app/source> <unfinished ...>',
        '102 123.101 openat(AT_FDCWD, "/app/source", O_WRONLY|O_TRUNC) = 3</app/source>',
        '102 123.102 write(3</app/source>, "helper()\\n", 9) = 9',
        '101 123.103 <... ioctl resumed>) = 0',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/source': 'pass\n', '/app/dest': 'pass\n'})
    assert not result['capture_complete']
    assert any('ordering' in issue for issue in result['issues'])


@pytest.mark.parametrize('reported_size', [str(CC.TRACE_BYTE_LIMIT + 1), '', 'not a size', '0'])
def test_trace_size_gate_stops_before_host_transfer(monkeypatch, reported_size):
    class SizedAuditSandbox(AuditSandbox):
        def exec(self, command, **kwargs):
            if command.startswith('wc -c < '):
                self.commands.append(command)
                return SimpleNamespace(stdout=reported_size, stderr='', rc=0)
            if command.startswith(('cat ', 'head -c ')):
                pytest.fail('oversized or unmeasured trace must never be transferred')
            return super().exec(command, **kwargs)

    stop = threading.Event()
    sb = SizedAuditSandbox()
    audit = CC.ProbeAudit(sb, mutation_paths=['/app/call.py'], stop=stop)
    monkeypatch.setattr(CC, '_read_files', lambda sb, paths: dict.fromkeys(paths, 'pass\n'))
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'large',
               'cwd': '/app', 'tool_input': {'command': 'cargo test'}}
    audit.hook(payload)
    response = audit.hook({**payload, 'hook_event_name': 'PostToolUse', 'tool_response': {}})
    assert response['continue'] is False and stop.is_set()
    assert any('512 MiB' in issue for issue in audit.issues)
    assert audit.trace_preflight['max_bytes'] == CC.TRACE_BYTE_LIMIT


def test_trace_growth_during_bounded_read_stops_without_parsing(monkeypatch):
    class GrowingAuditSandbox(AuditSandbox):
        def exec(self, command, **kwargs):
            if command.startswith('wc -c < '):
                return SimpleNamespace(stdout='3\n', stderr='', rc=0)
            if command.startswith('head -c '):
                assert str(CC.TRACE_BYTE_LIMIT + 1) in command
                return SimpleNamespace(stdout='grew', stderr='', rc=0)
            return super().exec(command, **kwargs)

    stop = threading.Event()
    audit = CC.ProbeAudit(GrowingAuditSandbox(), mutation_paths=['/app/call.py'], stop=stop)
    monkeypatch.setattr(CC, '_read_files', lambda sb, paths: dict.fromkeys(paths, 'pass\n'))
    monkeypatch.setattr(CC, 'parse_mutation_trace', lambda *a, **kw: pytest.fail('changed trace cannot be scored'))
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'growth',
               'cwd': '/app', 'tool_input': {'command': 'background tool'}}
    audit.hook(payload)
    response = audit.hook({**payload, 'hook_event_name': 'PostToolUse', 'tool_response': {}})
    assert response['continue'] is False and stop.is_set()
    assert any('changed or exceeded' in issue for issue in audit.issues)


def test_malformed_ioctl_on_tracked_fd_without_annotation_fails_closed():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 openat(AT_FDCWD, "/app/call.py", O_RDWR) = 4</app/call.py>',
        '101 123.101 ioctl(4) = 0',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/call.py': 'pass\n'})
    assert not result['capture_complete']


@pytest.mark.parametrize('operation', [
    'write(4</app/lib/other.js>, "helper()", 8) = 8',
    'pwrite64(4</app/lib/other.js>, "helper()", 8, 0) = 8',
    'copy_file_range(3</tmp/input>, NULL, 4</app/lib/other.js>, NULL, 8, 0) = 8',
    'ioctl(4</app/lib/other.js>, FICLONE, 3</tmp/input>) = 0',
    'mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, 4</app/lib/other.js>, 0) = 0x1000',
    'rename("/tmp/input", "/app/lib/other.js") = 0',
    'rename("/app/lib/other.js", "/tmp/moved") = 0',
])
def test_tracked_source_mutations_outside_frozen_scope_are_incomplete(operation):
    result = CC.parse_mutation_trace('101 123.100 ' + operation,
        {'/app/lib/call.js': 'pass\n'}, tracked_sources=['/app/lib/call.js', '/app/lib/other.js'])
    assert not result['capture_complete']
    assert any('tracked source mutation outside observed scope' in issue for issue in result['issues'])


def test_untracked_binary_build_output_does_not_expand_scope():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 openat(AT_FDCWD, "/app/target/obj", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 4</app/target/obj>',
        '101 123.101 write(4</app/target/obj>, "\\xff\\0binary", 8) = 8',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/lib/call.js': 'pass\n'},
        tracked_sources=['/app/lib/call.js', '/app/lib/other.js'], mutation_markers=['helper'])
    assert result['capture_complete'], result['issues']
    assert not result['events']


def test_new_source_helper_write_is_emitted_before_later_declared_edit():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 openat(AT_FDCWD, "/app/new.js", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 4</app/new.js>',
        '101 123.101 write(4</app/new.js>, "helper();", 9) = 9',
        '101 123.102 openat(AT_FDCWD, "/app/call.js", O_WRONLY|O_TRUNC) = 5</app/call.js>',
        '101 123.103 write(5</app/call.js>, "helper();", 9) = 9',
        '101 123.104 openat(AT_FDCWD, "/app/new.js", O_WRONLY|O_TRUNC) = 4</app/new.js>',
        '101 123.105 write(4</app/new.js>, "pass;", 5) = 5',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/call.js': 'pass;'}, mutation_markers=['helper'])
    assert result['capture_complete'], result['issues']
    first = result['events'][0]['data']
    assert first['path'] == '/app/new.js' and first['after'] == 'helper();'
    assert first['source'] == 'strace_outside_scope'
    assert first['command_baseline'] is None  # unknown baseline cannot become a negative
    assert result['events'][-1]['data']['path'] == '/app/call.js'


@pytest.mark.parametrize('path', ['/app/lib/new.js', '/app/target/generated.rs'])
def test_unknown_new_source_write_fails_closed_even_for_generated_source(path):
    result = CC.parse_mutation_trace(f'101 123.100 write(4<{path}>, "helper();", 9) = 9',
                                     {'/app/call.js': 'pass;'}, mutation_markers=['helper'])
    assert not result['capture_complete']
    assert any('unknown outside-scope source mutation' in issue for issue in result['issues'])


def test_generated_source_without_frozen_helper_does_not_emit_irrelevant_edits():
    trace = '\n'.join([  # noqa: FLY002
        '101 123.100 openat(AT_FDCWD, "/app/target/generated.rs", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 4</app/target/generated.rs>',
        '101 123.101 write(4</app/target/generated.rs>, "fn other() {}", 13) = 13',
    ])
    result = CC.parse_mutation_trace(trace, {'/app/call.js': 'pass;'}, mutation_markers=['helper'])
    assert result['capture_complete'], result['issues']
    assert not result['events']


def test_tracked_inventory_and_markers_are_pinned_in_audit_evidence():
    audit = CC.ProbeAudit(AuditSandbox(), mutation_paths=['/app/call.py'], mutation_markers=['helper'])
    guard = audit.source_scope_guard
    assert guard['paths'] == ['/app/call.py'] and guard['markers'] == ['helper']
    assert guard['version'] == CC.SOURCE_SCOPE_GUARD_VERSION
    assert len(guard['sha256']) == 64
    assert audit.trace_preflight['source_scope_guard'] == guard
    assert audit.finish([])['source_scope_guard'] == guard


def test_stream_rate_limited_status_stops_nonweekly_window():
    stop = threading.Event()
    state = {}
    watch = CC._stream_watch(state, stop, cfg=CC.CCConfig(max_window_utilization=.12))
    watch(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': {
        'five_hour': {'status': 'rate_limited', 'utilization': .3}}}}))
    assert stop.is_set() and state['policy_stop'] == 'max_window_hold'


@pytest.mark.parametrize('operation', [
    'fallocate(4</app/lib/other.js>, FALLOC_FL_COLLAPSE_RANGE, 0, 4096)',
    'link("/app/lib/other.js", "/tmp/alias")',
    'linkat(AT_FDCWD, "/app/lib/other.js", AT_FDCWD, "/tmp/alias", 0)',
    'linkat(4</app/lib/other.js>, "", AT_FDCWD, "/tmp/alias", AT_EMPTY_PATH)',
    'symlink("/app/lib/other.js", "/tmp/alias")',
    'symlinkat("/app/lib/other.js", AT_FDCWD, "/tmp/alias")',
    'symlink("../../app/lib/other.js", "/tmp/sub/alias")',
    'symlink("/tmp/source", "/app/lib/other.js")',
])
def test_common_metadata_changes_cannot_create_complete_negative(operation):
    result = CC.parse_mutation_trace('101 123.100 ' + operation + ' = 0',
        {'/app/call.js': 'pass;'}, tracked_sources=['/app/lib/other.js'], mutation_markers=['helper'])
    assert not result['capture_complete']
    assert any('outside observed scope' in issue for issue in result['issues'])


@pytest.mark.parametrize('operation', [
    'link("/app/target/a.o", "/app/target/b.o")',
    'symlink("a.so", "/app/target/b.so")',
    'fallocate(4</app/target/a.o>, 0, 0, 4096)',
])
def test_unrelated_binary_build_metadata_does_not_poison_source_scope(operation):
    result = CC.parse_mutation_trace('101 123.100 ' + operation + ' = 0',
        {'/app/call.js': 'pass;'}, tracked_sources=['/app/lib/other.js'], mutation_markers=['helper'])
    assert result['capture_complete'], result['issues']


def test_failed_source_alias_has_no_mutation():
    result = CC.parse_mutation_trace(
        '101 123.100 link("/app/lib/other.js", "/tmp/alias") = -1 EACCES (Permission denied)',
        {'/app/call.js': 'pass;'}, tracked_sources=['/app/lib/other.js'], mutation_markers=['helper'])
    assert result['capture_complete'], result['issues']
    assert {'fallocate', 'link', 'linkat', 'symlink', 'symlinkat'} <= set(CC.TRACE_SYSCALLS.split(','))


def _parallel_result_resume_fixture(count):
    """Sanitized native2.1.310 shape from fail1 Boa(6) and Testem(4) kept tools."""
    import hashlib
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    summary, prompt = 'Frozen summary. Required planted rule.', 'Please wrap up and report.'
    assistant = [{'type': 'thinking', 'thinking': '', 'signature': 'signed'},
                 {'type': 'text', 'text': 'Apply the reviewed edits.'}]
    assistant += [{'type': 'tool_use', 'id': f'edit-{i}', 'name': 'Edit',
                   'input': {'file_path': f'/app/file-{i}', 'old_string': 'before', 'new_string': 'after'}}
                  for i in range(count)]
    results = [{'type': 'tool_result', 'tool_use_id': f'edit-{i}',
                'content': f'The file /app/file-{i} has been updated successfully.'} for i in range(count)]
    old = [{'type': 'assistant', 'uuid': f'old-a-{i}', 'message': {
        'role': 'assistant', 'id': 'old-message', 'content': [block]}} for i, block in enumerate(assistant)]
    old += [{'type': 'user', 'uuid': f'old-u-{i}', 'message': {'role': 'user', 'content': [block]}}
            for i, block in enumerate(results)]
    synthetic = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'No response requested.'}]}
    request = [
        {'role': 'user', 'content': [{'type': 'text', 'text': '<system-reminder>status</system-reminder>'},
                                   {'type': 'text', 'text': summary}]},
        {'role': 'user', 'content': 'Today is the recorded date.'},
        {'role': 'assistant', 'content': assistant}, {'role': 'user', 'content': results},
        {'role': 'user', 'content': '<total_tokens>100 left</total_tokens>'}, synthetic,
        {'role': 'user', 'content': [{'type': 'text', 'text': '<system-reminder>attribution</system-reminder>'},
                                   {'type': 'text', 'text': prompt}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Environment: /app'}]},
    ]
    rows = [{'type': 'user', 'uuid': 'summary', 'message': {'role': 'user', 'content': summary}}, *old,
            {'type': 'attachment', 'attachment': {'type': 'token_usage'}},
            {'type': 'assistant', 'uuid': 'synthetic', 'message': {**synthetic, 'id': 'synthetic-message'}},
            {'type': 'user', 'uuid': 'probe', 'message': {'role': 'user', 'content': prompt}}]
    rows += [{'type': 'api-request-blob', 'hash': digest(msg), 'message': msg}
             for i, msg in enumerate(request) if i not in (2, 3, 5)]
    rows.append({'type': 'api-request', 'id': 'initial', 'base': None, 'keep': 0,
                 'tail': [digest(msg) for msg in request]})
    return rows, old, request, summary, prompt


@pytest.mark.parametrize('count', [4, 6])
def test_initial_request_recovers_exact_merged_parallel_user_results(count):
    rows, source, request, summary, prompt = _parallel_result_resume_fixture(count)
    assert CC.request_messages(rows, len(rows) - 1) == request
    rows.append({'type': 'assistant', 'uuid': 'new-assistant', 'message': {
        'role': 'assistant', 'content': [{'type': 'text', 'text': 'later forbidden output'}]}})
    checked = CC.resumed_context_check(rows, 'summary', summary, prompt, source_rows=source,
        required=['Required planted rule.'], forbidden=['later forbidden output'])
    assert checked['ok'] and checked['request_index'] == len(rows) - 2, checked
    assert checked['hits']['Required planted rule.'] == ['summary']


@pytest.mark.parametrize('change', ['drop', 'alter', 'reorder'])
def test_parallel_result_hash_recovery_never_fills_missing_or_changed_evidence(change):
    rows, _, _, _, _ = _parallel_result_resume_fixture(4)
    at = next(i for i, row in enumerate(rows) if row.get('uuid') == 'old-u-1')
    if change == 'drop':
        rows.pop(at)
    elif change == 'alter':
        rows[at]['message']['content'][0]['content'] = 'Different tool output.'
    else:
        rows[at], rows[at + 1] = rows[at + 1], rows[at]
    assert CC.request_messages(rows, len(rows) - 1) is None


@pytest.mark.parametrize('terminal', ['ask_user', 'call_cap', 'cli_error', 'callback_error', 'execution_error',
    'cap_missing_init', 'cap_non_max', 'cap_infra_exit'])
def test_continuation_capture_terminal_and_failure_evidence(tmp_path, monkeypatch, terminal):
    bdir = _fork_dir(tmp_path / 'source')
    b = json.loads((bdir / 'boundary/boundary.json').read_text())
    b['stage'] = CC.GRADED_STEP
    (bdir / 'boundary/boundary.json').write_text(json.dumps(b))
    monkeypatch.setattr(CC, 'ensure_cli_volume', lambda *args: None)
    monkeypatch.setattr(N, 'egress_check', lambda *args: {'ok': True})
    monkeypatch.setattr(I, 'fingerprint', lambda runner, version=3: 'v3 fp')
    monkeypatch.setattr(IR, 'sim_probe', lambda sim: 'probe')
    holder = {}
    class Sess(FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            holder['session'] = self
    def execute(sb, cmd, **kwargs):
        Path(kwargs['log_path']).parent.mkdir(parents=True, exist_ok=True)
        callback = kwargs['on_line']
        if terminal != 'cap_missing_init':
            callback(json.dumps({'type': 'system', 'subtype': 'init',
                                 'apiKeySource': 'environment' if terminal == 'cap_non_max' else 'none'}))
        server = holder['session'].server
        if terminal == 'callback_error':
            def broken_capture(row):
                raise RuntimeError('stop callback failed')
            server.capture_ask = broken_capture
            with pytest.raises(RuntimeError, match='stop callback failed'):
                server.ask('Ready for review.')
        elif terminal in ('ask_user', 'execution_error'):
            with pytest.raises(N.ProbeCaptured):
                server.ask('Ready for review.')
            if terminal == 'execution_error':
                raise RuntimeError('transport failed after capture')
            return N.StreamResult(-1, .1, False, 'stop')
        subtype = ('error_max_turns' if terminal == 'call_cap' or terminal.startswith('cap_') else
                   'error_during_execution' if terminal == 'cli_error' else 'success')
        callback(json.dumps({'type': 'result', 'subtype': subtype, 'is_error': subtype != 'success'}))
        return N.StreamResult(137 if terminal == 'cap_infra_exit' else 0 if subtype == 'success' else 1, .1, False)
    monkeypatch.setattr(N, 'detached_exec', execute)
    cfg = CC.BaseConfig(task=Task(), root=tmp_path / 'continued', host=None,
                        steps=(CC.GRADED_STEP,), capture_ask=True)
    result = CC.continue_from_boundary(bdir, cfg.run_dir, cfg=cfg, sim_model=None, token='fake', session_cls=Sess)
    assert result['sim_calls'] == result['sim_cost_usd'] == 0
    assert result['cleanup_unconfirmed'] == []
    assert bool(result['captured_asks']) is (terminal in ('ask_user', 'callback_error', 'execution_error'))
    if terminal in ('ask_user', 'call_cap'):
        assert result['end'] == terminal and not result.get('error'), result
    else:
        assert result.get('error') and result['end'] in ('error', 'cli_error', 'auth_error', 'infra_killed'), result
    if terminal == 'callback_error':
        assert result['capture_errors']
