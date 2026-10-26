"""Offline runtime contracts: real budgeting/scorers, injected native/model calls."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from compaction.s2 import planted_config as C
from compaction.s2 import planted_runtime as M
from compaction.s2 import planted_step0 as P

NOW = datetime(2026, 10, 26, 1, tzinfo=UTC).timestamp()


def rate(value=.2):
    return {"observed_at": NOW, "rate_limit_info": {"unifiedWindows": {
        "seven_day": {"utilization": value, "resetsAt": NOW + 7 * 86400}}}}


def spec(task):
    return {"schema": 2, "eligible": True, "task_id": task, "window": 80000, "max_calls": 30,
        "probe_message": "Please wrap up the work.", "obligations": [
            {"obligation_id": f"{task}:commit", "task_id": task, "kind": "commit_convention",
             "text": "Every commit starts with [T].", "standing": True,
             "trigger": {"reply_index": 1, "stage": "02_implement"},
             "scorer": {"kind": "commit_convention", "prefix": "[T]"}},
            {"obligation_id": f"{task}:verify", "task_id": task, "kind": "verification_output",
             "text": "Run verify and paste its last line.", "standing": True,
             "trigger": {"after_compaction": 1, "stage": "02_implement"},
             "scorer": {"kind": "verification_output", "command": "verify"}}]}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "STOP_FILE", tmp_path / "stop")
    cohort = C.ActiveCohort(tmp_path / "cohort.json")
    for task in C.task_order():
        P.write(tmp_path / "specs" / f"{task}.json", spec(task))
    rt = M.Runtime(root=tmp_path / "out", corpus=tmp_path / "corpus", specs_dir=tmp_path / "specs",
        si_root=tmp_path / "si", deepswe_root=tmp_path / "ds", eligibility_path=tmp_path / "eligibility.json",
        token="test-secret", go_from_jacob=True, reviewed_build=True, clock=lambda: NOW,
        io={"budget": C.ForkBudget(tmp_path / "budget.json"), "cohort": cohort,
            "totals": lambda tag: (0., 0.), "rate_reader": rate,
            "task_loader": lambda task, **kw: SimpleNamespace(task_id=task, persona="maintainer"),
            "simulator": lambda *a: SimpleNamespace(),
            "judge": lambda request: {"classification": "report", "quote": request["input"]["message"]}})
    return rt


def boundaries(rt):
    out = []
    for index, task in enumerate(C.task_order()[:2]):
        bdir = rt.root.parent / f"source{index}" / "b1"
        P.write(bdir / "boundary/post_messages.json", [{"role": "user", "content": "Ordinary task"}])
        out.append({"task_id": task, "boundary": str(bdir), "live_eligible": True})
    return out


def prepare_screen(rt, monkeypatch):
    rows = boundaries(rt)
    monkeypatch.setattr(rt, "_boundary", lambda row, output: M.Path(row["boundary"]))
    monkeypatch.setattr(rt, "_summary", lambda bdir: ("Task summary", "<summary>Task summary</summary>"))
    return rows


def good_fork(out, *, with_rule=True, verify=True):
    P.write(out / "evidence.json", {"part_a_complete": True, "events": [
        {"seq": 0, "kind": "commit", "data": {"message": "[T] done" if with_rule else "done"}},
        {"seq": 1, "kind": "command", "data": {"command": "verify", "output": "checked", "exit_code": 0, "cwd": "/app"}},
        {"seq": 2, "kind": "ask_user", "data": {"text": "Done `checked`" if with_rule and verify else "Done"}}]})
    return {"fork_check": {"ok": True}, "context_check": {"ok": True}, "cleanup_unconfirmed": [],
            "sim_calls": 0, "sim_cost_usd": 0, "rate_limit": rate(),
            "token_usage": {"input_tokens": 5, "output_tokens": 3}}


def step0_pair(*, captured=True):
    result = {"outcome": "ask_user" if captured else "call_cap",
              "captured_asks": [{"text": "done"}] if captured else [], "fork_check": {"ok": True},
              "sim_calls": 0, "sim_cost_usd": 0, "cleanup_unconfirmed": []}
    checks = P._checks(result) | {"usage": True}
    return {"status": "PASS", "probe": {"result": dict(result),
        "checks": checks | {"context": True, "part_a_complete": True, "core_scored": True},
        "scores": {kind: {"outcome": "not_reached"} for kind in ("commit_convention", "verification_output")}},
        "continuation": {"result": dict(result), "checks": dict(checks)}}


def reserve_fake_pair(runtime, attempt):
    ids = [attempt + suffix for suffix in (":probe", ":continue")]
    bucket = "step0" if not runtime.budget.state()["attempts"] else "step0_extra"
    runtime.budget.reserve_batch([{"attempt_id": identity, "part": "A", "bucket": bucket} for identity in ids])
    for identity in ids:
        runtime.budget.finish(identity, "complete", artifact="fake-step0.json")


@pytest.mark.parametrize("field,value", [("go", False), ("reviewed", False), ("token", "")])
def test_runtime_requires_authorization_before_any_reader(runtime, field, value):
    runtime.io["rate_reader"] = lambda: pytest.fail("must not call")
    setattr(runtime, field, value)
    with pytest.raises((PermissionError, ValueError)):
        runtime.max_reading()


def test_no_model_before_reset_or_after_cutoff(runtime):
    for now in (C.RESET.timestamp() - 1, M.FINAL_CUTOFF):
        runtime.clock = lambda now=now: now
        runtime.io["rate_reader"] = lambda: pytest.fail("must not call")
        with pytest.raises((PermissionError, RuntimeError)):
            runtime.max_reading()


def test_snapshot_is_offline_and_counts_reserved_failures_and_held_spend(runtime):
    runtime.io["rate_reader"] = lambda: pytest.fail("snapshot must not ping")
    runtime.io["totals"] = lambda tag: (3., 2.)
    runtime.budget.reserve("assigned", part="A", bucket="step0")
    snapshot = runtime.snapshot()
    assert snapshot["simulator_usd"] == 5 and snapshot["forks_a"] == 1
    assert len(snapshot["active_tasks"]) == 3


def test_guard_holds_at_ninety_and_honors_callback(runtime):
    calls = []
    runtime.before_action = calls.append
    runtime.io["rate_reader"] = lambda: rate(.9)
    with pytest.raises(ValueError, match="hold"):
        runtime.guard("fork", "work")
    assert calls == [{"action": "fork", "work_id": "work"}]


def test_interrupted_receipt_is_never_replayed(runtime):
    with pytest.raises(RuntimeError, match="crash"), runtime._phase("work", "test"):
        raise RuntimeError("crash")
    assert runtime.recover("work")["status"] == "HALTED"
    with pytest.raises(FileExistsError), runtime._phase("work", "test"):
        pytest.fail("must not relaunch")
    P.write(runtime._receipt("interrupted"), {"status": "ASSIGNED"})
    assert runtime.recover("interrupted")["status"] == "HALTED"


def test_step0_two_attempts_then_stop_and_no_repeat_on_recovery(runtime):
    calls = []
    runtime.io["step0"] = lambda *args, **kw: calls.append(kw) or {"status": "FAIL"}
    result = runtime.step0(work_id="stepzero", boundaries=boundaries(runtime))
    assert result["status"] == "FAIL" and len(calls) == 2
    assert all(call["native_cfg"].auto_compact_window == 80_000 for call in calls)
    assert calls[0]["attempt"] != calls[1]["attempt"]
    assert runtime.recover("stepzero")["status"] == "FAIL" and len(calls) == 2


@pytest.mark.parametrize("first_capture,required_pairs,expected_calls", [(False, 1, 2), (True, 1, 1), (True, 2, 2)])
def test_step0_aggregate_capture_and_explicit_two_pair_retry(runtime, first_capture, required_pairs, expected_calls):
    calls = []
    def pair(*args, **kwargs):
        calls.append(kwargs["attempt"])
        reserve_fake_pair(runtime, kwargs["attempt"])
        return step0_pair(captured=first_capture or len(calls) == 2) | {"attempt": kwargs["attempt"]}
    runtime.io["step0"] = pair
    result = runtime.step0(work_id="aggregate", boundaries=boundaries(runtime), required_pairs=required_pairs)
    assert result["status"] == "PASS" and len(calls) == expected_calls
    assert result["aggregate"]["pair_count"] == expected_calls
    assert result["aggregate"]["checks"]["ask_capture_demonstrated"]
    assert result["passed_at"] == NOW
    assert len(runtime.budget.state()["attempts"]) == 2 * expected_calls
    assert runtime.recover("aggregate") == result and len(calls) == expected_calls


@pytest.mark.parametrize("failure", ["no_capture", "unknown", "false_capture", "missed_capture"])
def test_step0_aggregate_preserves_every_pair_failure(runtime, failure):
    calls = []
    def pair(*args, **kwargs):
        calls.append(kwargs["attempt"])
        reserve_fake_pair(runtime, kwargs["attempt"])
        row = step0_pair(captured=failure != "no_capture")
        if len(calls) == 1:
            if failure == "unknown":
                row["probe"]["scores"]["verification_output"]["outcome"] = "unknown"
            elif failure == "false_capture":
                row["continuation"]["result"]["outcome"] = "call_cap"
            elif failure == "missed_capture":
                row["probe"]["result"]["captured_asks"] = []
        return row
    runtime.io["step0"] = pair
    result = runtime.step0(work_id="aggregate-fail", boundaries=boundaries(runtime), required_pairs=2)
    assert result["status"] == "FAIL" and result["passed_at"] is None
    expected_pairs = 2 if failure == "no_capture" else 1
    assert len(calls) == len(result["reports"]) == expected_pairs
    assert len(runtime.budget.state()["attempts"]) == 2 * expected_pairs


@pytest.mark.parametrize("failure", ["policy_stop", "withheld", "cleanup"])
def test_step0_exception_receipt_retains_pair_checks_and_failure_details(runtime, failure):
    calls = []
    def pair(*args, **kwargs):
        calls.append(kwargs["attempt"])
        reserve_fake_pair(runtime, kwargs["attempt"])
        report = step0_pair() | {"attempt": kwargs["attempt"]}
        if failure == "policy_stop":
            report["probe"]["result"]["policy_stop"] = "max_window_hold"
            report["probe"]["checks"]["no_error"] = False
        elif failure == "cleanup":
            report["continuation"]["result"]["cleanup_unconfirmed"] = ["container alive"]
            report["continuation"]["checks"]["cleanup"] = False
        else:
            report["withheld"] = {"stage": "continuation", "reason": "stale rate reading"}
        return report
    runtime.io["step0"] = pair
    with pytest.raises(RuntimeError, match="cleanup unconfirmed|policy/rate hold"):
        runtime.step0(work_id="held", boundaries=boundaries(runtime), required_pairs=2)
    receipt = runtime.recover("held")
    assert receipt["status"] == "HALTED" and receipt["passed_at"] is None
    assert len(receipt["reports"]) == len(calls) == 1
    assert len(runtime.budget.state()["attempts"]) == 2
    assert receipt["aggregate"]["pair_checks_passed"] == [False]
    assert receipt["receipt_summary"]["failure_details"] == receipt["aggregate"]["failure_details"]
    expected_check = {"policy_stop": "no_error", "withheld": "withheld", "cleanup": "cleanup"}[failure]
    assert any(row["check"] == expected_check for row in receipt["aggregate"]["failure_details"])
    assert expected_check in receipt["receipt_summary"]["note"]


def test_step0_halt_receipt_explains_unknown_pair_kind_and_scorer_reason(runtime):
    count = 0
    def pair(*args, **kwargs):
        nonlocal count
        count += 1
        report = step0_pair() | {"attempt": kwargs["attempt"]}
        if count == 1:
            report["probe"]["checks"]["core_scored"] = False
            report["probe"]["scores"]["verification_output"] = {
                "outcome": "unknown", "evidence": {"reason": "verification_output_empty"}}
        return report
    runtime.io["step0"] = pair
    result = runtime.step0(work_id="explain", boundaries=boundaries(runtime), required_pairs=2)
    assert result["status"] == "FAIL"
    note = result["receipt_summary"]["note"]
    assert "explain-1 probe/verification_output: verification_output_empty" in note
    assert "explain-1 probe/core_scored: check failed" in note
    details = result["aggregate"]["failure_details"]
    assert {"pair_index": 0, "attempt": "explain-1", "runner": "probe", "check": "score",
            "kind": "verification_output", "outcome": "unknown", "reason": "verification_output_empty"} in details
    assert runtime.recover("explain")["receipt_summary"]["failure_details"] == details


def test_screen_twelve_real_scored_forks_both_kinds_pass(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    calls = []

    def fork(bdir, out, **kw):
        calls.append(kw)
        assert len(runtime.budget.state()["attempts"]) == 12
        assert kw["probe"] and kw["max_calls"] == 30
        assert kw["cfg"].auto_compact_window == 1_000_000
        with pytest.raises(RuntimeError):
            kw["sim_model"].complete()
        return good_fork(out, with_rule=bool(kw["required_context"]))

    runtime.io["fork"] = fork
    result = runtime.screen(work_id="screen", boundaries=rows)
    assert result["status"] == "PASS" and result["passes"] and result["assigned"] == 12
    assert len(calls) == 12 and all(r["status"] == "complete" for r in runtime.budget.state()["attempts"])
    assert len(result["passed_kinds"]) == 2
    assert sum(bool(c["forbidden_context"]) for c in calls) == 6


def test_one_kind_failure_continues_without_redesign(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out, with_rule=bool(kw["required_context"]), verify=False)
    result = runtime.screen(work_id="screen", boundaries=rows)
    assert result["status"] == "PASS" and not result["passes"]
    assert result["passed_kinds"] == ["commit_convention"]
    assert result["failed_kinds"] == ["verification_output"]
    assert len(runtime.budget.state()["attempts"]) == 12


def test_probe_compaction_is_reported_without_changing_screen_scores_or_denominator(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    compacted = {"probe_compactions": [{"uuid": "new-compaction", "source": "transcript"}],
                 "probe_compaction_count": 1, "probe_compacted": True}
    runtime.io["fork"] = lambda b, out, **kw: good_fork(
        out, with_rule=bool(kw["required_context"])) | compacted
    result = runtime.screen(work_id="compacted-screen", boundaries=rows)
    assert result["status"] == "PASS" and result["passes"] and result["assigned"] == 12
    attempts = runtime.budget.state()["attempts"]
    assert len(attempts) == 12 and all(a["status"] == "complete" for a in attempts)
    reports = [P.read(path) for path in (runtime.root / "forks").glob("*/runtime-report.json")]
    assert len(reports) == 12
    for report in reports:
        assert report["probe_window"] == 1_000_000
        assert {key: report[key] for key in compacted} == compacted
        assert report["safe"] and report["context_ok"]
    outcomes = [r["scores"]["commit_convention"]["outcome"] for r in reports]
    assert outcomes.count("complied") == outcomes.count("violated") == 6


def test_both_kind_failure_halts_no_recheck(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out, with_rule=False)
    result = runtime.screen(work_id="screen", boundaries=rows)
    assert result["status"] == "HALTED" and result["passed_kinds"] == []
    assert all(r["bucket"] == "screen" for r in runtime.budget.state()["attempts"])


def test_screen_context_leakage_refuses_before_assignment(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    P.write(M.Path(rows[0]["boundary"]) / "boundary/post_messages.json", [{"content": "[T] old prefix"}])
    with pytest.raises(ValueError, match="leakage"):
        runtime.screen(work_id="screen", boundaries=rows)
    assert runtime.budget.state()["attempts"] == []


def test_actual_context_gate_failure_halts_with_all_assignments_retained(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out) | {"context_check": {"ok": False}}
    with pytest.raises(RuntimeError, match="context verification"):
        runtime.screen(work_id="screen", boundaries=rows)
    state = runtime.budget.state()["attempts"]
    assert len(state) == 12 and state[0]["status"] == "technical_failure"
    assert runtime.recover("screen")["status"] == "HALTED"


def test_baselines_are_sealed_without_reading_scores(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    def fork(b, out, **kw):
        assert kw["cfg"].auto_compact_window == 1_000_000
        return good_fork(out)
    runtime.io["fork"] = fork
    runtime.io["judge"] = lambda req: pytest.fail("baseline must not judge")
    monkeypatch.setattr(M.S, "score", lambda *a, **kw: pytest.fail("baseline must not score"))
    seal = runtime._baseline(rows[0], "baseline")
    assert P.read(seal)["sealed"] is True
    assert len(P.read(seal)["outputs"]) == 4
    assert len(runtime.budget.state()["attempts"]) == 2


def test_contingency_only_missing_tasks_and_hard_cap(runtime):
    task = C.task_order()[0]
    state = runtime._state()
    state["boundaries"] = [{"task_id": task, "live_eligible": True}]
    runtime._save(state)
    with pytest.raises(ValueError, match="missing"):
        runtime.contingency(work_id="bad", missing_tasks=[task], deadline=NOW+10800)
    with pytest.raises(ValueError, match="cannot exceed"):
        runtime.contingency(work_id="badcap", missing_tasks=[C.task_order()[1]], deadline=NOW+10800,
                            simulator_cap_usd=6.01)


def test_contingency_normal_timeout_returns_available_boundary_state(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_base_segment", lambda *a, **kw: {"status": "time_cap", "boundaries": []})
    result = runtime.contingency(work_id="fallback", missing_tasks=C.task_order()[:2], deadline=NOW+10800)
    assert result["status"] == "PASS" and len(result["attempts"]) == 1


def test_base_config_plants_once_and_resumes_only_implementation(runtime):
    calls = []
    runtime.io["base"] = lambda cfg, **kw: calls.append(cfg) or {"end": "submitted", "boundaries": [], "cleanup_unconfirmed": []}
    runtime.io["continue"] = lambda b, out, **kw: calls.append(kw["cfg"]) or {"end": "submitted", "boundaries": [], "cleanup_unconfirmed": []}
    task = C.task_order()[0]
    runtime._base_segment(task, "first", deadline=NOW+1000)
    runtime._base_segment(task, "continued", boundary="/fake/b1", deadline=NOW+1000)
    assert calls[0].steps == ("01_plan", "02_implement") and calls[1].steps == ("02_implement",)
    assert len(calls[0].planting.entries) == 2
    assert calls[0].stop_after == 1 and calls[0].cc.deadline_epoch == NOW+1000
    assert all(cfg.cc.auto_compact_window == 80_000 for cfg in calls)


def test_chain_baselines_each_boundary_then_stops_before_cards(runtime, monkeypatch):
    task = C.task_order()[0]
    observed = []

    def segment(task, identity, boundary=None, **kw):
        k = int(boundary[-1])+1 if boundary else 1
        row = {"task_id": task, "boundary": f"/b{k}", "live_eligible": True, "k": k}
        observed.append(("base", k))
        return {"status": "stop_after", "boundaries": [row]}

    monkeypatch.setattr(runtime, "_base_segment", segment)
    monkeypatch.setattr(runtime, "_retention", lambda row: observed.append(("retention", row["k"])) or "/retention")
    monkeypatch.setattr(runtime, "_baseline", lambda row, ident: observed.append(("baseline", row["k"])) or "/seal")
    result = runtime.chains(work_id="chains", tasks=[task], kinds=["commit_convention"])
    assert result["stop"] == "BEFORE_CARDS" and result["baseline_outputs_inspected"] is False
    assert observed == [(kind, k) for k in (1, 2, 3) for kind in ("base", "retention", "baseline")]


def test_chain_first_boundary_one_rerun_then_seeded_replacement(runtime, monkeypatch):
    original = C.task_order()[0]
    seen = []

    def segment(task, identity, **kw):
        seen.append(task)
        if task == original:
            return {"status": "cli_error", "boundaries": []}
        return {"status": "stop_after", "boundaries": [{"task_id": task, "boundary": "/b1", "live_eligible": True, "k": 1}]}

    monkeypatch.setattr(runtime, "_base_segment", segment)
    monkeypatch.setattr(runtime, "_retention", lambda row: "/retention")
    monkeypatch.setattr(runtime, "_baseline", lambda row, ident: "/seal")
    # Stop after replacement proved; an unexpected resume failure must halt, not retry it.
    def next_segment(task, identity, **kw):
        if kw.get("boundary"):
            raise RuntimeError("stop after replacement")
        return segment(task, identity, **kw)
    monkeypatch.setattr(runtime, "_base_segment", next_segment)
    with pytest.raises(RuntimeError, match="replacement"):
        runtime.chains(work_id="chains", tasks=[original])
    assert seen == [original, original, C.task_order()[3]]
    assert runtime.cohort.state()["replacements"][0]["new"] == C.task_order()[3]


def test_simulator_guard_blocks_new_paid_request_after_operator_stop(runtime):
    from compaction.s2.model import StageCapReached
    calls = []
    runtime.io["simulator"] = lambda *a: SimpleNamespace(complete=lambda *a, **kw: calls.append("paid"))

    def base(cfg, sim_model, **kw):
        M.STOP_FILE.write_text("stop")
        with pytest.raises(StageCapReached, match="operator stop"):
            sim_model.complete([], None, 1, purpose="test")
        return {"end": "run_cap", "boundaries": [], "cleanup_unconfirmed": []}

    runtime.io["base"] = base
    result = runtime._base_segment(C.task_order()[0], "base")
    assert result["status"] == "run_cap" and calls == []


def test_native_policy_hold_does_not_trigger_task_rerun(runtime):
    calls = []
    runtime.io["base"] = lambda cfg, **kw: calls.append(cfg) or {
        "end": "max_window_hold", "policy_stop": "max_window_hold", "cleanup_unconfirmed": []}
    with pytest.raises(RuntimeError, match="safety stop"):
        runtime.chains(work_id="chains", tasks=C.task_order()[:1])
    assert len(calls) == 1


def test_step0_policy_hold_stops_without_using_retry(runtime):
    calls = []
    runtime.io["step0"] = lambda *a, **kw: calls.append(kw) or {"status": "FAIL", "probe": {
        "result": {"policy_stop": "max_window_hold"}}}
    with pytest.raises(RuntimeError, match="retry withheld"):
        runtime.step0(work_id="stepzero", boundaries=boundaries(runtime))
    assert len(calls) == 1


def test_live_native_configuration_has_safety_fields(runtime):
    cfg = M.Runtime._cc_config(runtime, NOW + 500)
    assert cfg.auto_compact_window == 80_000
    assert cfg.max_window_utilization == .9 and cfg.deadline_epoch == NOW + 500
    assert cfg.stop_file == str(M.STOP_FILE)
    probe = runtime._cc_config(NOW + 500, probe=True)
    assert probe.auto_compact_window == 1_000_000
    assert probe.max_window_utilization == cfg.max_window_utilization
    assert probe.deadline_epoch == cfg.deadline_epoch and probe.stop_file == cfg.stop_file


def test_desktop_max_reader_rejects_paid_route_and_keeps_real_arrival(tmp_path):
    events = [{"type": "system", "subtype": "init", "apiKeySource": "none"},
              {"type": "rate_limit_event", "rate_limit_info": rate()["rate_limit_info"] | {
                  "isUsingOverage": False, "overageStatus": "rejected", "overageDisabledReason": "org_disabled"}}]
    def runner(cwd, args, prompt, timeout, *, env, on_event, stop):
        assert "ANTHROPIC_API_KEY" not in env and "--safe-mode" in args
        for event in events:
            on_event(event)
        return events
    result = M.desktop_max_reading(root=tmp_path, clock=lambda: NOW, run=runner, checks=lambda: None)
    assert result["observed_at"] == NOW
    events[0]["apiKeySource"] = "api_key"
    with pytest.raises(RuntimeError, match="route"):
        M.desktop_max_reading(root=tmp_path, clock=lambda: NOW, run=runner, checks=lambda: None)


def test_contingency_one_rerun_then_next_seeded_task(runtime, monkeypatch):
    task = C.task_order()[0]
    seen = []
    def segment(task_id, identity, **kw):
        seen.append(task_id)
        if task_id == task:
            return {"status": "cli_error", "boundaries": []}
        return {"status": "eligible", "boundaries": [{"live_eligible": True, "k": 1}]}
    monkeypatch.setattr(runtime, "_base_segment", segment)
    result = runtime.contingency(work_id="fallback", missing_tasks=[task], deadline=NOW+1000)
    assert result["status"] == "PASS"
    assert seen == [task, task, C.task_order()[3]]


def test_force_max_reading_does_not_reuse_pre_action_snapshot(runtime):
    seen = []
    runtime.io["rate_reader"] = lambda: seen.append(1) or rate(.2 + len(seen)*.01)
    assert runtime.max_reading()["rate_limit_info"] == runtime.max_reading()["rate_limit_info"]
    runtime.max_reading(force=True)
    assert len(seen) == 2


def test_retention_joins_actual_delivery_and_first_commit_without_baselines(runtime):
    import json
    task = C.task_order()[0]
    obligation = spec(task)["obligations"][0]
    bdir = runtime.root / "base" / "b1"
    P.write(bdir / "boundary/boundary.json", {"boundary_index": 1, "quiescent": True,
        "services_running": [], "pre_tokens": 79000})
    P.write(bdir / "boundary/compaction.json", {"summary": obligation["text"]})
    P.write(bdir / "boundary/post_messages.json", [{"role": "user", "content": obligation["text"]}])
    P.write(bdir / "boundary/sim_state.json", {"planting": {"deliveries": {
        obligation["obligation_id"]: {"call_index": 1}}, "boundaries": [{"compaction_index": 1}]},
        "ask_log": [{"call_index": 1, "reply": obligation["text"]}]})
    rows = [{"message": {"content": [{"type": "tool_use", "id": "ask1", "name": M.CC.ASK_TOOL,
                                      "input": {"question": "Proceed?"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "ask1", "content": obligation["text"]}]}},
            {"message": {"content": [{"type": "tool_use", "id": "commit1", "name": "Bash",
                                      "input": {"command": 'git commit -m "[T] implement"'}}]}},
            {"cwd": "/app", "toolUseResult": {"exitCode": 0}, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "commit1", "content": "[main abc1234] [T] implement"}]}}]
    (bdir / "native").mkdir()
    (bdir / "native/transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    runtime.io["judge"] = lambda request: {"label": "kept", "context_quote": obligation["text"],
        "repo_quote": "", "recoverability": "unknown", "still_binds": True}
    result = P.read(runtime._retention({"task_id": task, "boundary": str(bdir), "live_eligible": True}))
    assert result["baseline_outputs_inspected"] is False
    commit, verify = result["grades"]
    assert commit["adoption"]["adopted"] is True and commit["adoption"]["adoption_before_boundary"] is True
    assert commit["label"] == "kept" and verify["label"] == "not_delivered"


def test_default_rate_prechecks_run_before_any_session(tmp_path, monkeypatch):
    from compaction import claude_headless as H
    seen = []
    monkeypatch.setattr(H, "check_auth", lambda: seen.append("auth"))
    def bad_policy():
        seen.append("policy")
        raise H.ManagedSettingsError("unsafe policy")
    monkeypatch.setattr(H, "check_managed_settings", bad_policy)
    with pytest.raises(H.ManagedSettingsError):
        M.desktop_max_reading(root=tmp_path, run=lambda *a, **kw: pytest.fail("must not start"))
    assert seen == ["auth", "policy"]


def test_reserved_last_fork_and_its_judge_can_finish_at_cap(runtime):
    requests = [{"attempt_id": f"{part}-{bucket}-{i}", "part": part, "bucket": bucket}
                for part, allocations in [("A", C.A_ALLOCATIONS), ("B", C.B_ALLOCATIONS)]
                for bucket, count in allocations.items() for i in range(count)]
    runtime.budget.reserve_batch(requests)
    assert runtime.snapshot()["forks_total"] == 94
    calls = []
    runtime.before_action = calls.append
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out)
    task = C.task_order()[0]
    report = runtime._fork(identity="A-repair-11", row={"task_id": task}, bdir=M.Path("/fake/b1"),
        summary="Summary", prompt="Wrap up")
    assert report["scores"]["verification_output"]["outcome"] == "complied"
    assert all(c["attempt_id"] == "A-repair-11" for c in calls)
    assert any(c["action"] == "opus_judge" for c in calls)
    with pytest.raises(RuntimeError, match="cap"):
        runtime.guard("new_base")
    with pytest.raises(RuntimeError, match="cap"):
        runtime.guard("new_fork", attempt_id="invented-id")
    with pytest.raises(RuntimeError, match="cap"):
        runtime.guard("finished_fork", attempt_id="A-repair-11")


def test_contingency_native_deadline_is_normal_exhaustion(runtime):
    runtime.io["base"] = lambda cfg, **kw: {"end": "deadline", "policy_stop": "deadline",
        "boundaries": [], "cleanup_unconfirmed": []}
    report = runtime.contingency(work_id="fallback", missing_tasks=C.task_order()[:1], deadline=NOW+500)
    assert report["status"] == "PASS" and report["exhausted"]
    assert len(report["attempts"]) == 1


@pytest.mark.parametrize("passing", ["commit_convention", "verification_output"])
def test_filtered_resume_preserves_source_and_exact_simulator_probe(runtime, passing):
    import json

    from compaction.s2 import interact as I
    task = C.task_order()[0]
    obligations = spec(task)["obligations"]
    schedule = M.PlantingSchedule([{k: o[k] for k in ("obligation_id", "text", "trigger")}
                                   for o in obligations])
    schedule.set_context(stage="02_implement")
    reply, _ = schedule.apply("Proceed.", call_index=1)
    schedule.record_compaction(1, pre_tokens=79000)
    runner = lambda command, timeout: (0, "fixed repository snapshot", "")
    sim = I.UserSim("maintainer", M.CC.NoSimulatorModel(), runner, planting=schedule)
    sim.state.conversation = [{"role": "user", "content": "Proceed?"}, {"role": "assistant", "content": reply}]
    sim.ask_log = [{"call_index": 1, "reply": reply, "question": "Proceed?"}]
    original_export = sim.export()
    probe = M.IR.sim_probe(sim)
    bdir = runtime.root.parent / "source-run" / "b1"
    P.write(bdir.parent / "run.json", {"task": {"task_id": task}})
    P.write(bdir / "boundary/boundary.json", {"quiescent": True, "tag": "image:test", "stage": "02_implement",
        "native_summary_uuid": "summary", "fingerprint_sha256": "fp", "sim_probe_sha256": probe,
        "boundary_index": 1, "services_running": [], "boundary_in_session": 1, "model": M.CC.MODEL})
    P.write(bdir / "boundary/sim_state.json", original_export)
    P.write(bdir / "native/hook_payload.json", {"compact_summary": "<summary>Task summary</summary>"})
    rows = [{"uuid": "u", "type": "user", "parentUuid": None, "message": {"role": "user", "content": "Task"}},
            {"uuid": "compact", "type": "system", "subtype": "compact_boundary", "parentUuid": "u",
             "logicalParentUuid": "u", "compactMetadata": {}},
            {"uuid": "summary", "type": "user", "isCompactSummary": True, "parentUuid": "compact",
             "message": {"role": "user", "content": "Task summary"}}]
    (bdir / "native/transcript.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    converted = M.CC.transcript_boundary(rows, 1, model=M.CC.MODEL, step_instruction="")
    P.write(bdir / "boundary/pre_messages.json", converted.nb.pre_messages)
    P.write(bdir / "boundary/post_messages.json", converted.nb.post_messages)
    P.write(bdir / "boundary/compaction.json", {"summary": converted.nb.summary})
    original_bytes = {str(path.relative_to(bdir)): path.read_bytes() for path in bdir.rglob("*") if path.is_file()}
    copied = runtime._filtered_resume(bdir, task, [passing], "resume")
    after_export = P.read(copied / "boundary/sim_state.json")
    restored = I.UserSim.restore(after_export, "maintainer", M.CC.NoSimulatorModel(), runner)
    assert M.IR.sim_probe(restored) == probe == P.read(copied / "boundary/boundary.json")["sim_probe_sha256"]
    assert original_bytes == {str(path.relative_to(bdir)): path.read_bytes() for path in bdir.rglob("*") if path.is_file()}
    assert {k: v for k, v in original_export.items() if k != "planting"} == {
        k: v for k, v in after_export.items() if k != "planting"}
    assert original_export["planting"]["deliveries"] == after_export["planting"]["deliveries"]
    assert original_export["planting"]["boundaries"] == after_export["planting"]["boundaries"]
    expected = [entry for entry in original_export["planting"]["entries"]
                if entry["obligation_id"] == obligations[0]["obligation_id"] or passing == "verification_output"]
    assert after_export["planting"]["entries"] == expected
    next_reply, _ = restored.planting.apply("Next.", call_index=2)
    assert (obligations[1]["text"] in next_reply) is (passing == "verification_output")
    assert obligations[0]["text"] not in next_reply  # previously delivered text is never repeated


def test_fresh_chain_omits_failed_kind_without_changing_passing_bytes(runtime):
    seen = []
    runtime.io["base"] = lambda cfg, **kw: seen.append(cfg) or {
        "end": "submitted", "boundaries": [], "cleanup_unconfirmed": []}
    task = C.task_order()[0]
    runtime._base_segment(task, "fresh", kinds=["verification_output"])
    entry = seen[0].planting.entries
    assert len(entry) == 1
    assert entry[0].text == spec(task)["obligations"][1]["text"]
    assert entry[0].trigger == spec(task)["obligations"][1]["trigger"]


def guarded_backend(tmp_path, **kwargs):
    return M.GuardedMaxBackend(model=M.R.MODEL, cache_dir=tmp_path / "cache", usage_path=tmp_path / "usage.jsonl",
        clock=lambda: NOW, stop_file=tmp_path / "stop", **kwargs)


@pytest.mark.parametrize("stop_kind", ["before_reset", "deadline", "operator"])
def test_judge_guard_blocks_canary_before_actual_attempt(tmp_path, monkeypatch, stop_kind):
    backend = guarded_backend(tmp_path)
    if stop_kind == "before_reset":
        backend._clock = lambda: C.RESET.timestamp()-1
    elif stop_kind == "deadline":
        backend.deadline_epoch = NOW
    else:
        backend.stop_file.write_text("stop")
    monkeypatch.setattr(M.H.MaxBackend, "_attempt", lambda *a: pytest.fail("no actual attempt permitted"))
    with pytest.raises(RuntimeError, match="before attempt"):
        backend._attempt(M.H._canary_req(), "test", M.H._Stats(), 1, False)
    assert P.read(backend.cache_dir / "pilot-guard-stop.json")["reason"]


@pytest.mark.parametrize("kind", ["deadline", "operator"])
def test_silent_judge_attempt_is_stopped_within_watch_interval(tmp_path, monkeypatch, kind):
    backend = guarded_backend(tmp_path)
    def silent_attempt(self, *args):
        if kind == "deadline":
            self._clock = lambda: self.deadline_epoch
        else:
            self.stop_file.write_text("stop")
        assert self._stop.wait(1), "silent session must receive stop signal"
        return None, None
    monkeypatch.setattr(M.H.MaxBackend, "_attempt", silent_attempt)
    with pytest.raises(RuntimeError, match="during attempt"):
        backend._attempt(M.H._canary_req(), "test", M.H._Stats(), 1, False)
    assert backend.pilot_stop_reason == ("operator_stop" if kind == "operator" else "deadline")


@pytest.mark.parametrize("util,status,threshold", [(.90, "allowed", .90), (.2, "rejected", .90), (.22, "allowed", .22)])
def test_judge_stream_stops_at_hold_or_partb_threshold(tmp_path, monkeypatch, util, status, threshold):
    backend = guarded_backend(tmp_path)
    backend.max_window_utilization = threshold
    def stream(self, *args):
        event = {"type": "rate_limit_event", "rate_limit_info": rate(util)["rate_limit_info"] | {
            "status": status, "isUsingOverage": False, "overageStatus": "rejected"}}
        assert self._watch(event)
        assert self._stop.is_set()
        return None, None
    monkeypatch.setattr(M.H.MaxBackend, "_attempt", stream)
    with pytest.raises(RuntimeError, match="during attempt"):
        backend._attempt(M.H._canary_req(), "test", M.H._Stats(), 1, False)
    assert backend.pilot_stop_reason == "max_window_hold"


def test_guarded_factory_scoped_callback_covers_canary_and_retries(tmp_path, monkeypatch):
    from dataclasses import replace

    from compaction.s1_types import Resp
    seen, thresholds = [], []
    monkeypatch.setattr(M.H.MaxBackend, "_attempt", lambda *a: (None, None))
    def fake_run(self, requests, *, purpose):
        req = requests[0]
        self._attempt(M.H._canary_req(), purpose, M.H._Stats(), 1, False)
        self._attempt(req, purpose, M.H._Stats(), 1, False)
        self._attempt(replace(req, user=req.user+" retry"), purpose, M.H._Stats(), 2, True)
        thresholds.append(self.max_window_utilization)
        return {req.key: Resp(req.key, {"classification": "report"}, None, None, model=M.R.MODEL)}
    monkeypatch.setattr(M.H.MaxBackend, "run", fake_run)
    judge = M.guarded_max_judge(tmp_path, clock=lambda: NOW, stop_file=tmp_path / "stop", recorder=lambda *a, **kw: None)
    assert judge.guarded_attempts
    assert judge({"system": "Classify", "input": "Done"}, before_attempt=seen.append,
                 max_window_utilization=.22) == {"classification": "report"}
    assert [s["canary"] for s in seen] == [True, False, False]
    assert seen[-1]["attempt"] == 2 and thresholds == [.22]
    judge({"system": "Classify", "input": "Another"})
    assert len(seen) == 3 and thresholds[-1] == .9  # callback/threshold never leak across calls


def test_before_attempt_callback_crossing_deadline_prevents_launch(tmp_path, monkeypatch):
    backend = guarded_backend(tmp_path)
    backend.before_attempt = lambda meta: setattr(backend, "deadline_epoch", NOW)
    monkeypatch.setattr(M.H.MaxBackend, "_attempt", lambda *a: pytest.fail("deadline crossed before launch"))
    with pytest.raises(RuntimeError, match="before attempt"):
        backend._attempt(M.H._canary_req(), "test", M.H._Stats(), 1, False)


def test_silent_max_ping_stops_on_operator_file(tmp_path, monkeypatch):
    stop_file = tmp_path / "stop"
    monkeypatch.setattr(M, "STOP_FILE", stop_file)
    def silent(cwd, args, prompt, timeout, *, env, on_event, stop):
        assert timeout <= 120
        stop_file.write_text("stop")
        assert stop.wait(1)
        return []
    with pytest.raises(RuntimeError, match="reading stopped"):
        M.desktop_max_reading(root=tmp_path, clock=lambda: NOW, run=silent, checks=lambda: None)


def test_partb_two_point_judge_limit_applies_only_to_seven_day(tmp_path):
    backend = guarded_backend(tmp_path)
    backend.max_window_utilization = .02
    info = {"isUsingOverage": False, "overageStatus": "rejected", "unifiedWindows": {
        "seven_day": {"utilization": .01}, "five_hour": {"utilization": .06}}}
    assert backend._watch({"type": "rate_limit_event", "rate_limit_info": info}) is False
    info["unifiedWindows"]["seven_day"]["utilization"] = .02
    assert backend._watch({"type": "rate_limit_event", "rate_limit_info": info}) is True


def test_judge_stream_rate_limited_status_stops_even_below_utilization(tmp_path):
    backend = guarded_backend(tmp_path)
    assert backend._watch({"type": "rate_limit_event", "rate_limit_info": {
        "isUsingOverage": False, "overageStatus": "rejected", "status": "rate_limited"}})
    assert backend.pilot_stop_reason == "max_window_hold"


def test_real_pilot_snapshot_after_desktop_budget_initialization_is_offline(runtime, tmp_path):
    C.initialize_pilot_budget(checkout=tmp_path)
    runtime.spend = C.pilot_spend(checkout=tmp_path)
    runtime.io.pop("totals")
    runtime.io["rate_reader"] = lambda: pytest.fail("read-only snapshot must not call model")
    snapshot = runtime.snapshot()
    assert snapshot["simulator_usd"] == 0 and snapshot["forks_total"] == 0


@pytest.mark.parametrize("failure", ["unforkable", "no_compaction", "baseline"])
def test_chain_task_terminal_preserves_tally_and_continues_other_tasks(runtime, monkeypatch, failure):
    tasks = C.task_order()[:2]
    retained, baselines = [], []

    def segment(task, identity, boundary=None, **kw):
        k = int(boundary[-1]) + 1 if boundary else 1
        if task == tasks[0] and k == 2 and failure == "no_compaction":
            return {"status": "transcript_error", "boundaries": []}
        return {"status": "stop_after", "boundaries": [{"task_id": task,
            "boundary": f"/{task}/b{k}", "k": k,
            "live_eligible": not (task == tasks[0] and k == 2 and failure == "unforkable")}]}

    def baseline(row, identity):
        baselines.append((row["task_id"], row["k"]))
        if row["task_id"] == tasks[0] and failure == "baseline":
            raise M.TaskTerminal("safe technical baseline failure; artifacts sealed")
        return "/seal"

    monkeypatch.setattr(runtime, "_base_segment", segment)
    monkeypatch.setattr(runtime, "_retention", lambda row: retained.append((row["task_id"], row["k"])) or "/retention")
    monkeypatch.setattr(runtime, "_baseline", baseline)
    result = runtime.chains(work_id="task-stop", tasks=tasks)
    assert result["status"] == "PASS"
    assert result["tasks"][0]["status"] == "technical_failure"
    assert result["tasks"][1]["target_reached"] is True
    assert (tasks[1], 3) in baselines
    if failure == "unforkable":
        assert (tasks[0], 2) in retained and (tasks[0], 2) not in baselines
        assert runtime._state()["chains"][f"/{tasks[0]}/b2"]["unforkable"]


def test_safe_failed_baselines_are_counted_and_sealed(runtime, monkeypatch):
    row = prepare_screen(runtime, monkeypatch)[0]
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out) | {"outcome": "transcript_error"}
    runtime.io["judge"] = lambda req: pytest.fail("baseline must remain uninspected")
    with pytest.raises(M.TaskTerminal, match="sealed"):
        runtime._baseline(row, "failed-baseline")
    attempts = runtime.budget.state()["attempts"]
    assert len(attempts) == 2 and all(a["status"] == "technical_failure" for a in attempts)
    seal = P.read(runtime.root / "baselines/failed-baseline/sealed-probes.json")
    assert seal["sealed"] and len(seal["outputs"]) == 4


@pytest.mark.parametrize("unsafe", [{"cleanup_unconfirmed": ["container"]}, {"sim_calls": 1},
    {"sim_cost_usd": .1}, {"auth_error": "subscription mismatch"}, {"paid_route": True},
    {"policy_stop": "deadline"}, {"outcome": "run_cap"}])
def test_baseline_safety_failures_remain_global_halts(runtime, monkeypatch, unsafe):
    row = prepare_screen(runtime, monkeypatch)[0]
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out) | unsafe
    with pytest.raises(RuntimeError, match="unsafe probe") as exc:
        runtime._baseline(row, "unsafe-baseline")
    assert not isinstance(exc.value, M.TaskTerminal)
    assert runtime.budget.state()["attempts"][1]["status"] == "assigned"
    assert P.read(runtime.root / "baselines/unsafe-baseline/sealed-probes.json")["sealed"]


def test_contingency_k2_is_not_a_success_and_triggers_one_rerun(runtime, monkeypatch):
    seen = []
    def segment(task, identity, **kw):
        seen.append(task)
        return {"status": "eligible", "boundaries": [{"live_eligible": True, "k": 2 if len(seen) == 1 else 1}]}
    monkeypatch.setattr(runtime, "_base_segment", segment)
    result = runtime.contingency(work_id="fallback-k2", missing_tasks=C.task_order()[:1], deadline=NOW+1000)
    assert result["status"] == "PASS" and len(seen) == 2 and seen[0] == seen[1]


def test_judge_five_hour_ninety_only_stops_when_rejected(tmp_path):
    backend = guarded_backend(tmp_path)
    info = rate()["rate_limit_info"] | {"isUsingOverage": False, "overageStatus": "rejected"}
    info["unifiedWindows"]["five_hour"] = {"utilization": .99, "status": "allowed"}
    assert not backend._watch({"type": "rate_limit_event", "rate_limit_info": info})
    assert backend.latest_rate["observed_at"] == NOW
    info["unifiedWindows"]["five_hour"]["status"] = "rejected"
    assert backend._watch({"type": "rate_limit_event", "rate_limit_info": info})


@pytest.mark.parametrize("age", [0, 61, None])
def test_judge_reuses_actual_fresh_rate_and_forces_missing_or_stale(runtime, age):
    reads = []
    runtime.io["rate_reader"] = lambda: reads.append(1) or rate()
    judge = lambda req: {"classification": "report"}
    judge.latest_rate = lambda: None if age is None else rate(.25) | {"observed_at": NOW-age}
    runtime.io["judge"] = judge
    runtime.judge({})
    after = runtime.max_reading()
    assert len(reads) == (1 if age == 0 else 2)
    assert after["rate_limit_info"]["unifiedWindows"]["seven_day"]["utilization"] == (.25 if age == 0 else .2)


def test_step0_supplies_forced_fresh_rate_callback(runtime):
    reads = []
    runtime.io["rate_reader"] = lambda: reads.append(1) or rate()
    def step0(*a, **kw):
        kw["rate_provider"]()
        return step0_pair()
    runtime.io["step0"] = step0
    runtime.step0(work_id="fresh-stepzero", boundaries=boundaries(runtime))
    assert len(reads) == 2


def test_natural_chain_submission_continues_other_tasks_without_extra_baselines(runtime, monkeypatch):
    tasks = C.task_order()[:2]
    observed = []
    def segment(task, identity, boundary=None, **kw):
        if boundary:
            return {"status": "submitted", "boundaries": []}
        return {"status": "stop_after", "boundaries": [{"task_id": task,
            "boundary": f"/{task}/b1", "live_eligible": True, "k": 1}]}
    monkeypatch.setattr(runtime, "_base_segment", segment)
    monkeypatch.setattr(runtime, "_retention", lambda row: "/retention")
    monkeypatch.setattr(runtime, "_baseline", lambda row, identity: observed.append(row["task_id"]) or "/seal")
    result = runtime.chains(work_id="natural-finish", tasks=tasks)
    assert observed == tasks  # two forks at each task's sole real boundary only
    assert all(t["status"] == "submitted" and not t["target_reached"] and t["last_compaction"] == 1
               for t in result["tasks"])


def test_observe_rate_reuses_fresh_stream_and_never_regresses(runtime):
    runtime.io["rate_reader"] = lambda: pytest.fail("fresh stream observation must avoid ping")
    assert runtime.observe_rate(rate(.3))
    assert not runtime.observe_rate(rate(.2) | {"observed_at": NOW-1})
    assert not runtime.observe_rate(rate(.2) | {"observed_at": NOW-61})
    assert not runtime.observe_rate(rate(.2) | {"observed_at": NOW+1})
    assert not runtime.observe_rate({"observed_at": NOW})
    assert runtime.max_reading() == rate(.3)


@pytest.mark.parametrize("info", [None, {}, {"unifiedWindows": []}, {"unifiedWindows": [1]},
    {"unifiedWindows": {"five_hour": {"utilization": .1, "resetsAt": NOW+100}}},
    {"unifiedWindows": {"seven_day": "invalid"}},
    *({"unifiedWindows": {"seven_day": {"utilization": used, "resetsAt": reset}}}
      for used, reset in [(None, NOW+100), (.2, None), (".2", NOW+100), (.2, "later"),
                          (float("nan"), NOW+100), (.2, float("inf")), (True, NOW+100), (.2, False)])])
def test_malformed_judge_rate_falls_back_to_fresh_ping(runtime, info):
    malformed = {"observed_at": NOW, "rate_limit_info": info}
    reads = []
    runtime.io["rate_reader"] = lambda: reads.append(1) or rate()
    judge = lambda req: {"classification": "report"}
    judge.latest_rate = lambda: malformed
    runtime.io["judge"] = judge
    assert runtime.observe_rate(malformed) is False
    runtime._rate = malformed  # also guard native paths that directly store an event
    runtime.judge({})
    assert runtime.max_reading() == rate()
    assert len(reads) == 2


@pytest.mark.parametrize("unified", [True, False])
def test_reusable_rate_preserves_valid_high_and_rejected_events(runtime, unified):
    seven = {"utilization": .99, "resetsAt": NOW+100, "status": "rejected"}
    info = {"unifiedWindows": {"seven_day": seven}} if unified else seven | {"rateLimitType": "seven_day"}
    snapshot = {"observed_at": NOW, "rate_limit_info": info}
    assert M.reusable_rate(snapshot, now=NOW) is snapshot
    assert runtime.observe_rate(snapshot)
    runtime.io["rate_reader"] = lambda: pytest.fail("valid hold must not be hidden by another ping")
    with pytest.raises(ValueError, match="hold"):
        runtime.guard("must-stop")


@pytest.mark.parametrize("observed", [float("nan"), float("inf"), True, NOW+1, NOW-61])
def test_reusable_rate_rejects_invalid_arrival_time(observed):
    assert M.reusable_rate(rate() | {"observed_at": observed}, now=NOW) is None


def test_reusable_rate_respects_post_action_cutoff():
    assert M.reusable_rate(rate(), now=NOW, since=NOW+1) is None
    assert M.reusable_rate(rate(), now=NOW, since=NOW) == rate()


@pytest.mark.parametrize("reset,held", [(NOW-1, False), (NOW, False), (NOW+1, True),
    (None, True), (float("nan"), True), (float("-inf"), True)])
def test_judge_ignores_only_known_expired_rejected_window(tmp_path, reset, held):
    backend = guarded_backend(tmp_path)
    info = rate()["rate_limit_info"] | {"isUsingOverage": False, "overageStatus": "rejected"}
    info["unifiedWindows"]["five_hour"] = {"utilization": 1, "status": "rejected", "resetsAt": reset}
    assert backend._watch({"type": "rate_limit_event", "rate_limit_info": info}) is held
    assert backend._stop.is_set() is held


def test_expired_short_window_does_not_hide_active_weekly_hold(tmp_path):
    backend = guarded_backend(tmp_path)
    info = rate(.9)["rate_limit_info"] | {"isUsingOverage": False, "overageStatus": "rejected"}
    info["unifiedWindows"]["five_hour"] = {"status": "rejected", "resetsAt": NOW-1}
    assert backend._watch({"type": "rate_limit_event", "rate_limit_info": info})
    assert backend.pilot_stop_reason == "max_window_hold"
