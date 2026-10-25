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
    # Native guard fields are developed independently; the fake runners check the
    # requested settings while never constructing a container or invoking a model.
    monkeypatch.setattr(rt, "_cc_config", lambda deadline=M.FINAL_CUTOFF: SimpleNamespace(
        auto_compact_window=80000, max_window_utilization=.9, deadline_epoch=deadline,
        stop_file=str(rt.root / "STOP")))
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
        {"seq": 2, "kind": "ask_user", "data": {"text": "Done checked" if with_rule and verify else "Done"}}]})
    return {"fork_check": {"ok": True}, "context_check": {"ok": True}, "cleanup_unconfirmed": [],
            "sim_calls": 0, "sim_cost_usd": 0, "rate_limit": rate(),
            "token_usage": {"input_tokens": 5, "output_tokens": 3}}


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
    assert calls[0]["attempt"] != calls[1]["attempt"]
    assert runtime.recover("stepzero")["status"] == "FAIL" and len(calls) == 2


def test_screen_twelve_real_scored_forks_both_kinds_pass(runtime, monkeypatch):
    rows = prepare_screen(runtime, monkeypatch)
    calls = []

    def fork(bdir, out, **kw):
        calls.append(kw)
        assert len(runtime.budget.state()["attempts"]) == 12
        assert kw["probe"] and kw["max_calls"] == 30
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
    runtime.io["fork"] = lambda b, out, **kw: good_fork(out)
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
    assert cfg.max_window_utilization == .9 and cfg.deadline_epoch == NOW + 500
    assert cfg.stop_file == str(M.STOP_FILE)


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
        return {"status": "eligible", "boundaries": [{"live_eligible": True}]}
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
