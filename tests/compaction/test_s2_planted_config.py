import hashlib
import json
from datetime import timedelta

import pytest

from compaction.s2 import planted_config as C
from compaction.s2 import planted_specs as S


def test_allocations_caps_and_failed_attempts_count(tmp_path):
    b = C.ForkBudget(tmp_path / "forks.json")
    assert sum(C.A_ALLOCATIONS.values()) == 60
    for bucket, n in C.A_ALLOCATIONS.items():
        for i in range(n):
            ident = f"A:{bucket}:{i}"
            b.reserve(ident, part="A", bucket=bucket)
            b.finish(ident, "technical_failure", artifact="missing")
    with pytest.raises(ValueError, match="cap"):
        b.reserve("extra", part="A", bucket="repair")
    for bucket, n in C.B_ALLOCATIONS.items():
        for i in range(n):
            b.reserve(f"B:{bucket}:{i}", part="B", bucket=bucket)
    assert len(b.state()["attempts"]) == 94
    with pytest.raises(ValueError, match="cap"):
        b.reserve("overflow", part="B", bucket="main")


def release_evidence(tmp_path, content=None):
    artifact = tmp_path / "screen.json"
    artifact.write_text(json.dumps(content or {"passes": True, "assigned": 12}))
    return {"artifact": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}


def reserve_optional(budget, bucket, count):
    return budget.reserve_batch([{"attempt_id": f"{bucket}:{i}", "part": "A", "bucket": bucket}
                                 for i in range(count)])


def test_optional_never_spends_baseline_or_repair_and_no_refund(tmp_path):
    b = C.ForkBudget(tmp_path / "forks.json")
    with pytest.raises(ValueError, match="released"):
        reserve_optional(b, "deletion", 6)
    evidence = release_evidence(tmp_path)
    b.release("recheck", reason="first sensitivity screen passed", evidence=evidence)
    # Lower-priority reread cannot grab the slots reserved by drop order.
    with pytest.raises(ValueError, match="share"):
        reserve_optional(b, "reread", 2)
    with pytest.raises(ValueError, match="complete groups"):
        reserve_optional(b, "deletion", 4)
    reserve_optional(b, "deletion", 6)
    reserve_optional(b, "kept_restatement", 6)
    with pytest.raises(ValueError, match="already assigned"):
        reserve_optional(b, "deletion", 6)
    for i in range(18):
        b.reserve(f"base{i}", part="A", bucket="baseline")
    for i in range(12):
        b.reserve(f"repair{i}", part="A", bucket="repair")
    with pytest.raises(ValueError):
        b.release("repair", reason="no losses yet", evidence=evidence)


def test_step0_borrows_recheck_only_after_four_extras(tmp_path):
    b = C.ForkBudget(tmp_path / "forks.json")
    for i in range(5):
        row = b.reserve(str(i), part="A", bucket="step0_extra")
    assert row["bucket"] == "recheck" and row["borrowed_for"] == "step0_extra"
    assert C.optional_allocation(12) == {"reread": 0, "kept_restatement": 6, "deletion": 6}
    assert C.optional_allocation(4) == {"reread": 0, "kept_restatement": 0, "deletion": 0}


def test_part_b_time_max_and_priority_limits():
    kw = {"step0_passed_at": C.RESET, "now": C.RESET + timedelta(hours=1),
              "start_utilization": .1, "current_utilization": .11,
              "reading_at": C.RESET + timedelta(hours=1), "expected_fork_utilization": .002}
    C.part_b_guard(**kw)
    for override in [{"current_utilization": .12}, {"current_utilization": .90},
                     {"chains_started": True}, {"now": C.B_CUTOFF + timedelta(seconds=1)},
                     {"step0_passed_at": C.B_DROP_IF_STEP0_AFTER + timedelta(seconds=1)}]:
        with pytest.raises(ValueError):
            C.part_b_guard(**(kw | override))


def historical_rows(prefix="initial implementation", report="Please review this implementation."):
    return [
        {"message": {"content": [{"type": "tool_use", "id": "commit", "name": "Bash",
                                  "input": {"command": f"git commit -m '{prefix}'"}}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "commit", "is_error": False,
                                  "content": f"[main abcdef01] {prefix}\n 1 file changed"}]}},
        {"message": {"content": [{"type": "tool_use", "id": "report", "name": "ask_user",
                                  "input": {"question": report}}]}},
    ]


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_task_order_and_default_check_source_pin(tmp_path):
    tasks = C.task_order()
    assert tasks[:3] == ["boa-hierarchical-evaluation-cancellation",
                         "testem-bail-on-test-failure", "kombu-single-active-consumer-priority"]
    transcript = tmp_path / (tasks[0] + "-cc-a1") / "steps/02_implement/transcript.jsonl"
    write_rows(transcript, historical_rows())
    spec = S.build_task_spec(tasks[0], corpus=tmp_path, instruction="task")
    assert spec["obligations"][0]["trigger"]["reply_index"] == 1
    assert spec["obligations"][1]["trigger"]["after_compaction"] == 1
    assert spec["default_check"]["sources"][0]["sha256"]
    assert spec["default_check"]["status"] == "passed" and spec["eligible"]
    assert S.schedule(spec).entries[0].text == spec["obligations"][0]["text"]
    write_rows(transcript, historical_rows(prefix="[BOA-417] implementation"))
    assert S.build_task_spec(tasks[0], corpus=tmp_path, instruction="task")["default_check"]["status"] == "inconclusive"


def test_frozen_config_commands_are_accepted_by_verification_scorer():
    from compaction.s2.planted_score import Event, score
    for cmd in S.COMMANDS.values():
        assert score([Event(0, "submission", {"text": "done"})],
                     {"kind": "verification_output", "command": cmd})["outcome"] == "violated"


def test_spend_tag_enforces_25_dollars_without_other_pilots_tag():
    spend = C.pilot_spend()
    assert spend.tags == (C.SPEND_TAG,)
    assert spend.caps == {C.SPEND_TAG: 25.0}


def test_batch_reservation_failure_is_atomic(tmp_path):
    budget = C.ForkBudget(tmp_path / "budget.json")
    with pytest.raises(ValueError):
        budget.reserve_batch([{"attempt_id": "one", "part": "A", "bucket": "step0"},
                              {"attempt_id": "two", "part": "A", "bucket": "invalid"}])
    assert budget.state()["attempts"] == []


def test_finish_rejects_unknown_and_finished_attempts(tmp_path):
    budget = C.ForkBudget(tmp_path / "budget.json")
    with pytest.raises(ValueError, match="unknown"):
        budget.finish("missing", "complete", artifact="x")
    budget.reserve("one", part="A", bucket="step0")
    budget.finish("one", "technical_failure", artifact="failure.json")
    with pytest.raises(ValueError, match="overwritten"):
        budget.finish("one", "complete", artifact="success.json")


def test_missing_budget_with_existing_attempt_report_refuses(tmp_path):
    report = tmp_path / "output/attempt/report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    budget = C.ForkBudget(tmp_path / "budget.json", attempt_roots=(tmp_path / "output",))
    with pytest.raises(ValueError, match="budget is missing"):
        budget.state()
    assert C.ForkBudget().path == C.central_budget_path()


def test_release_requires_pinned_successful_screen(tmp_path):
    budget = C.ForkBudget(tmp_path / "budget.json")
    with pytest.raises(ValueError, match="passed complete"):
        budget.release("recheck", reason="finished", evidence=release_evidence(tmp_path, {"passes": False, "assigned": 12}))
    proof = release_evidence(tmp_path)
    with pytest.raises(ValueError, match="fingerprint"):
        budget.release("recheck", reason="finished", evidence={**proof, "sha256": "wrong"})
    budget.release("recheck", reason="passed", evidence=proof)
    assert budget.state()["released"][0]["outcome"]["passes"] is True


def test_part_b_freshness_projected_cost_and_durable_baseline(tmp_path):
    path = tmp_path / "allowance.json"
    now = C.RESET + timedelta(hours=2)
    kw = {"step0_passed_at": C.RESET, "now": now, "reading_at": now,
          "current_utilization": .1, "expected_fork_utilization": .003,
          "window_reset_at": C.RESET + timedelta(days=7)}
    C.PartBAllowance(path).guard(**kw)
    restarted = C.PartBAllowance(path)
    with pytest.raises(ValueError, match="two-point"):
        restarted.guard(**(kw | {"current_utilization": .119}))
    with pytest.raises(ValueError, match="fresh"):
        restarted.guard(**(kw | {"reading_at": now - timedelta(seconds=301)}))
    with pytest.raises(ValueError, match="window changed"):
        restarted.guard(**(kw | {"window_reset_at": C.RESET + timedelta(days=14)}))
    assert json.loads(path.read_text())["start_utilization"] == .1


def test_replacement_recomputes_first_two_part_b_tasks():
    order = C.task_order()
    assert C.part_b_tasks(order[1:4]) == order[1:3]


def test_empty_sources_never_pass_and_boundary_recovery_deduplicates(tmp_path):
    task = C.task_order()[0]
    run = tmp_path / f"{task}-cc-a1"
    empty = run / "steps/02_implement/transcript.jsonl"
    write_rows(empty, [])
    assert S.build_task_spec(task, corpus=tmp_path, instruction="task")["default_check"]["status"] == "no_evidence"
    write_rows(run / "b1/native/transcript.jsonl", historical_rows())
    write_rows(run / "b2/native/transcript.jsonl", historical_rows())
    result = S.build_task_spec(task, corpus=tmp_path, instruction="task")["default_check"]
    assert result["status"] == "passed"
    assert result["commit_count"] == result["report_count"] == 1
    assert len(result["skipped_sources"]) == 1 and result["skipped_sources"][0]["bytes"] == 0


@pytest.mark.parametrize("rows", [
    historical_rows()[:1], historical_rows()[:2], historical_rows()[2:],
    historical_rows(report="Which package should I edit?"),
])
def test_attempted_commit_or_missing_act_is_no_evidence(tmp_path, rows):
    task = C.task_order()[0]
    write_rows(tmp_path / f"{task}-a1/steps/02_implement/transcript.jsonl", rows)
    result = S.build_task_spec(task, corpus=tmp_path, instruction="task")
    assert result["default_check"]["status"] == "no_evidence" and not result["eligible"]


def test_equivalent_wrapped_run_output_pasted_makes_default_inconclusive(tmp_path):
    task = C.task_order()[0]
    rows = historical_rows()[:2] + [
        {"message": {"content": [{"type": "tool_use", "id": "validation", "name": "Bash",
                                  "input": {"command": "cd /app && cargo  clippy -p boa_engine --all-targets 2>&1 | tail -1"}}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "validation", "is_error": False,
                                  "content": "checks complete"}]}},
        historical_rows(report="Please review. checks complete")[-1]]
    write_rows(tmp_path / f"{task}-a1/steps/02_implement/transcript.jsonl", rows)
    check = S.build_task_spec(task, corpus=tmp_path, instruction="task")["default_check"]
    assert check["status"] == "inconclusive" and len(check["equivalent_commands"]) == 1
    assert not check["verification_default_violations"]


def test_instruction_hash_uses_file_bytes_and_conflicting_rule_blocks(tmp_path):
    task = C.task_order()[0]
    write_rows(tmp_path / f"{task}-a1/steps/02_implement/transcript.jsonl", historical_rows())
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Do not execute validation commands.\n")
    spec = S.build_task_spec(task, corpus=tmp_path, instruction_path=instruction)
    assert spec["instruction_sha256"] == hashlib.sha256(instruction.read_bytes()).hexdigest()
    assert spec["conflicts"]["status"] == "unresolved" and not spec["eligible"]
