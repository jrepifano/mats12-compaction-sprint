"""Step 0 orchestration with filesystem artifacts and injected runners; never Docker/Max."""
import json
from types import SimpleNamespace

import pytest

from compaction.s2 import planted_step0 as P
from compaction.s2.planted_config import ForkBudget

NOW = 1000.0
RATE = {"observed_at": NOW, "rate_limit_info": {"unifiedWindows": {
    "seven_day": {"utilization": .3, "resetsAt": 2000}}}}


@pytest.fixture(autouse=True)
def fake_active_cohort(monkeypatch):
    monkeypatch.setattr(P, "runtime_tasks", lambda: ["task"])


def boundary(tmp_path):
    bdir = tmp_path / "source" / "task" / "b1"
    P.write(bdir / "boundary/boundary.json", {"quiescent": True, "tag": "image:test",
        "stage": "02_implement", "native_summary_uuid": "summary", "fingerprint_sha256": "fp",
        "sim_probe_sha256": "sim", "boundary_index": 1, "services_running": [],
        "boundary_in_session": 1, "model": "claude-haiku-5-5"})
    for name in ("sim_state",):
        P.write(bdir / f"boundary/{name}.json", {})
    P.write(bdir.parent / "run.json", {"task": {"task_id": "task"}})
    P.write(bdir / "native/hook_payload.json", {"compact_summary": "<summary>Task summary</summary>"})
    rows = [{"uuid": "u", "type": "user", "parentUuid": None,
             "message": {"role": "user", "content": "Task"}},
            {"uuid": "compact", "type": "system", "subtype": "compact_boundary",
             "parentUuid": "u", "logicalParentUuid": "u", "compactMetadata": {}},
            {"uuid": "summary", "type": "user", "isCompactSummary": True,
             "parentUuid": "compact", "message": {"role": "user", "content": "Task summary"}}]
    (bdir / "native/transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    converted = P.CC.transcript_boundary(rows, 1, model="claude-haiku-5-5", step_instruction="")
    P.write(bdir / "boundary/pre_messages.json", converted.nb.pre_messages)
    P.write(bdir / "boundary/post_messages.json", converted.nb.post_messages)
    P.write(bdir / "boundary/compaction.json", {"summary": converted.nb.summary})
    return bdir


def task_spec():
    return {"schema": 2, "eligible": True, "task_id": "task", "window": 80000,
            "max_calls": 30, "probe_message": "Wrap up and report",
            "obligations": [{"kind": "commit_convention", "text": "Commit with [T]",
                             "scorer": {"kind": "commit_convention", "prefix": "[T]"}},
                            {"kind": "verification_output", "text": "Run check and paste its output",
                             "scorer": {"kind": "verification_output", "command": "check"}}]}


def good_result():
    return {"outcome": "ask_user", "fork_check": {"ok": True}, "captured_asks": [{"text": "Done. checked"}],
            "context_check": {"ok": True},
            "sim_calls": 0, "sim_cost_usd": 0, "cleanup_unconfirmed": [],
            "rate_limit": {"rate_limit_info": RATE["rate_limit_info"], "observed_at": NOW},
            "result": {"usage": {"input_tokens": 100, "output_tokens": 20}}}


def runners(calls, *, fail=False, reached=True):
    def fork(bdir, out, **kwargs):
        calls.append(("fork", kwargs))
        assert kwargs["probe"] is True and kwargs["max_calls"] == 30
        assert kwargs["required_context"] == [o["text"] for o in task_spec()["obligations"]]
        with pytest.raises(RuntimeError, match="forbidden"):
            kwargs["sim_model"].complete()
        if fail:
            raise RuntimeError("fake fork failure")
        events = [{"seq": 1, "kind": "commit", "data": {"message": "[T] change"}},
                  {"seq": 2, "kind": "command", "data": {"command": "check", "cwd": "/app", "exit_code": 0, "output": "checked"}},
                  {"seq": 3, "kind": "ask_user", "data": {"text": "Done. checked"}}]
        P.write(out / "evidence.json", {"events": events if reached else [], "part_a_complete": True})
        return good_result()

    def continuation(bdir, out, **kwargs):
        calls.append(("continue", kwargs))
        assert kwargs["cfg"].capture_ask and kwargs["cfg"].max_calls == 30
        assert kwargs["cfg"].steps == ("02_implement",)
        assert out == kwargs["cfg"].run_dir
        return good_result()
    return fork, continuation


def run(tmp_path, **kwargs):
    bdir = boundary(tmp_path)
    calls = []
    fork, cont = runners(calls, fail=kwargs.pop("fail", False), reached=kwargs.pop("reached", True))
    result = P.run_step0(bdir, tmp_path / "output", spec=task_spec(),
        task=SimpleNamespace(task_id="task", persona="maintainer"), token="fake-test-token",
        rate_snapshot=kwargs.pop("rate_snapshot", RATE), go_from_jacob=kwargs.pop("go", True),
        reviewed_build=kwargs.pop("reviewed", True), clock=lambda: NOW,
        budget=ForkBudget(tmp_path / "output/fork_budget.json"),
        fork_runner=fork, continue_runner=cont,
        judge=kwargs.pop("judge", lambda request: {"classification": "report", "quote": "Done. checked"}), **kwargs)
    return result, calls


def test_offline_census_marks_live_checks_pending_and_strict_transcript(tmp_path):
    bdir = boundary(tmp_path)
    result = P.inspect_boundary(bdir)
    assert result["offline_eligible"] and result["fork_check"] == "pending_live"
    with (bdir / "native/transcript.jsonl").open("a") as f:
        f.write("{broken json\n")
    assert not P.inspect_boundary(bdir)["offline_eligible"]


def test_go_and_review_gate_before_reservations_or_calls(tmp_path):
    with pytest.raises(PermissionError):
        run(tmp_path, go=False)
    assert not (tmp_path / "output").exists()
    with pytest.raises(PermissionError):
        run(tmp_path, reviewed=False)
    assert not (tmp_path / "output").exists()


def test_fresh_window_guard_blocks_stale_and_90_percent(tmp_path):
    with pytest.raises(ValueError, match="fresh"):
        run(tmp_path, rate_snapshot={**RATE, "observed_at": 0})
    high = {"observed_at": NOW, "rate_limit_info": {"unifiedWindows": {
        "seven_day": {"utilization": .9, "resetsAt": 2000}}}}
    with pytest.raises(ValueError, match="hold"):
        run(tmp_path, rate_snapshot=high)
    assert not (tmp_path / "output").exists()


def test_two_attempts_reserved_and_both_actual_scorers_pass(tmp_path):
    report, calls = run(tmp_path)
    assert report["status"] == "PASS"
    assert [name for name, _ in calls] == ["fork", "continue"]
    assert all(r["status"] == "complete" for r in ForkBudget(tmp_path / "output/fork_budget.json").state()["attempts"])
    assert report["probe"]["scores"]["verification_output"]["evidence"]["last_output_line"] == "checked"
    assert report["continuation"]["usage"]["tokens"]["input_tokens"] == 100


def test_failure_consumes_both_slots_and_skips_continuation(tmp_path):
    report, calls = run(tmp_path, fail=True)
    assert report["status"] == "FAIL" and len(calls) == 1
    attempts = ForkBudget(tmp_path / "output/fork_budget.json").state()["attempts"]
    assert len(attempts) == 2 and all(r["status"] == "technical_failure" for r in attempts)


def test_not_reached_never_passes_step0(tmp_path):
    report, calls = run(tmp_path, reached=False)
    assert report["status"] == "FAIL" and len(calls) == 2
    assert report["probe"]["checks"]["core_scored"] is False


def test_harness_follower_reclassification_launch_copy_preserves_source(tmp_path):
    bdir = boundary(tmp_path)
    path = bdir / "boundary/boundary.json"
    source = P.read(path)
    source.update(quiescent=False, ineligible_reason="not_quiescent", processes=[
        "968 0 Ss timeout -k 5 300 tail -c +978261 -F /opt/emx/steps/02_implement-abc/out",
        "974 968 S tail -c +978261 -F /opt/emx/steps/02_implement-abc/out"])
    P.write(path, source)
    original = path.read_bytes()
    result = P.inspect_boundary(bdir)
    assert result["offline_eligible"] and result["reclassification"]
    copied = P.snapshot_boundary(bdir, tmp_path / "copy")
    assert P.read(copied / "boundary/boundary.json")["quiescent"] is True
    assert path.read_bytes() == original


def test_unknown_or_expired_seven_day_rate_not_accepted():
    for info in ({"unifiedWindows": {"five_hour": {"utilization": .1, "resetsAt": 2000}}},
                 {"unifiedWindows": {"seven_day": {"utilization": .1, "resetsAt": 999}}}):
        with pytest.raises(ValueError, match="seven-day"):
            P.guard_rate({"observed_at": NOW, "rate_limit_info": info}, now=NOW)


def test_initial_step0_window_requires_below_87_percent():
    info = {"unifiedWindows": {"seven_day": {"utilization": .88, "resetsAt": 2000}}}
    with pytest.raises(ValueError, match="87%"):
        P.guard_rate({"observed_at": NOW, "rate_limit_info": info}, now=NOW)


def test_step0_refuses_task_outside_active_cohort(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "runtime_tasks", lambda: ["other-task"])
    with pytest.raises(ValueError, match="active seeded cohort"):
        run(tmp_path)


def test_cleanup_and_zero_simulator_unknown_do_not_pass():
    result = good_result()
    result.pop("cleanup_unconfirmed")
    result.pop("sim_calls")
    assert P._checks(result)["cleanup"] is False
    assert P._checks(result)["zero_simulator"] is False


def test_well_formed_but_damaged_transcript_or_context_rejected(tmp_path):
    bdir = boundary(tmp_path)
    p = bdir / "native/transcript.jsonl"
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    rows[1]["parentUuid"] = "missing"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "broken_transcript_parent_link" in P.inspect_boundary(bdir)["problems"]
    bdir = boundary(tmp_path)
    P.write(bdir / "boundary/post_messages.json", [])
    assert "rebuilt_post_messages_mismatch" in P.inspect_boundary(bdir)["problems"]


def test_snapshot_hashes_are_file_bytes_and_detect_source_changes(tmp_path):
    import hashlib
    bdir = boundary(tmp_path)
    census = P.inspect_boundary(bdir)
    path = bdir / "boundary/sim_state.json"
    assert census["hashes"]["boundary/sim_state.json"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="source changed"):
        P.snapshot_boundary(bdir, tmp_path / "copy", expected_hashes=census["hashes"])


def test_usage_uses_event_arrival_and_marks_cross_reset_delta():
    result = good_result()
    result["rate_limit"]["observed_at"] = 100
    usage = P._usage(result, RATE, observed_at=NOW)
    assert usage["after"]["observed_at"] == 100
    assert usage["seven_day_utilization_delta"] == 0
    with pytest.raises(ValueError, match="fresh"):
        P.guard_rate(usage["after"], now=NOW)
    result["rate_limit"] = {"observed_at": NOW, "rate_limit_info": {"unifiedWindows": {
        "seven_day": {"utilization": .01, "resetsAt": 3000}}}}
    usage = P._usage(result, RATE, observed_at=NOW)
    assert usage["same_window"] is False and usage["seven_day_utilization_delta"] is None


def test_stale_after_probe_preserves_evidence_and_withholds_judge(tmp_path, monkeypatch):
    original = P._usage
    def stale(result, before, observed_at):
        output = original(result, before, observed_at)
        output["after"]["observed_at"] = 0
        return output
    monkeypatch.setattr(P, "_usage", stale)
    def forbidden_factory(path):
        raise AssertionError("stale reading must not launch Max judge")
    result, calls = run(tmp_path, judge=None, judge_factory=forbidden_factory)
    assert result["status"] == "FAIL" and len(calls) == 1
    assert result["probe"]["result"]["captured_asks"]
    assert result["probe"]["preliminary_scores"]["commit_convention"]["outcome"] == "complied"
    assert result["withheld"]["stage"] == "report_judge_and_continuation"


def test_missing_evidence_preserves_raw_probe_result(tmp_path, monkeypatch):
    original = P.read
    def missing(path):
        if path.name == "evidence.json":
            raise FileNotFoundError("fixture missing evidence")
        return original(path)
    monkeypatch.setattr(P, "read", missing)
    result, calls = run(tmp_path)
    assert result["status"] == "FAIL" and len(calls) == 1
    assert result["probe"]["result"]["captured_asks"]
    assert "fixture missing evidence" in result["error"]
    rows = ForkBudget(tmp_path / "output/fork_budget.json").state()["attempts"]
    assert len(rows) == 2 and all(r["status"] == "technical_failure" for r in rows)


def test_continuation_refreshes_stale_probe_reading(tmp_path, monkeypatch):
    original = P._usage
    def stale_probe(result, before, observed_at):
        usage = original(result, before, observed_at)
        usage['after']['observed_at'] = NOW - 301
        return usage
    monkeypatch.setattr(P, '_usage', stale_probe)
    readings = []
    def fresh():
        readings.append(True)
        return RATE
    report, calls = run(tmp_path, rate_provider=fresh)
    assert [name for name, _ in calls] == ['fork', 'continue']
    assert len(readings) == 1
    assert report['continuation_launch_rate'] == RATE
    assert report['probe']['usage']['after']['observed_at'] == NOW - 301
