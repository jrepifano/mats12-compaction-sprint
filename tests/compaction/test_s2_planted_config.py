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


def test_optional_never_spends_baseline_or_repair_and_no_refund(tmp_path):
    b = C.ForkBudget(tmp_path / "forks.json")
    with pytest.raises(ValueError, match="released"):
        b.reserve("optional", part="A", bucket="deletion")
    b.release("recheck", reason="first sensitivity screen passed")
    for i in range(12):
        b.reserve(f"opt{i}", part="A", bucket="deletion" if i < 6 else "kept_restatement")
    with pytest.raises(ValueError):
        b.reserve("opt12", part="A", bucket="reread")
    with pytest.raises(ValueError, match="already assigned"):
        b.reserve("opt0", part="A", bucket="deletion")
    for i in range(18):
        b.reserve(f"base{i}", part="A", bucket="baseline")
    for i in range(12):
        b.reserve(f"repair{i}", part="A", bucket="repair")
    with pytest.raises(ValueError):
        b.release("repair", reason="no losses yet")


def test_step0_borrows_recheck_only_after_four_extras(tmp_path):
    b = C.ForkBudget(tmp_path / "forks.json")
    for i in range(5):
        row = b.reserve(str(i), part="A", bucket="step0_extra")
    assert row["bucket"] == "recheck" and row["borrowed_for"] == "step0_extra"
    assert C.optional_allocation(12) == {"reread": 0, "kept_restatement": 6, "deletion": 6}
    assert C.optional_allocation(4) == {"reread": 0, "kept_restatement": 0, "deletion": 0}


def test_part_b_time_max_and_priority_limits():
    kw = {"step0_passed_at": C.RESET, "now": C.RESET + timedelta(hours=1),
              "start_utilization": .1, "current_utilization": .11}
    C.part_b_guard(**kw)
    for override in [{"current_utilization": .12}, {"current_utilization": .90},
                     {"chains_started": True}, {"now": C.B_CUTOFF + timedelta(seconds=1)},
                     {"step0_passed_at": C.B_DROP_IF_STEP0_AFTER + timedelta(seconds=1)}]:
        with pytest.raises(ValueError):
            C.part_b_guard(**(kw | override))


def test_task_order_and_default_check_source_pin(tmp_path):
    tasks = C.task_order()
    assert tasks[:3] == ["boa-hierarchical-evaluation-cancellation",
                         "testem-bail-on-test-failure", "kombu-single-active-consumer-priority"]
    d = tmp_path / (tasks[0] + "-cc-a1") / "steps/02_implement"
    d.mkdir(parents=True)
    transcript = d / "transcript.jsonl"
    transcript.write_text(json.dumps({"message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m done"}}
    ]}}) + "\n")
    spec = S.build_task_spec(tasks[0], corpus=tmp_path, instruction="task")
    assert spec["obligations"][0]["trigger"]["reply_index"] == 1
    assert spec["obligations"][1]["trigger"]["after_compaction"] == 1
    assert spec["default_check"]["sources"][0]["sha256"]
    assert S.schedule(spec).entries[0].text == spec["obligations"][0]["text"]
    transcript.write_text(json.dumps({"message": {"content": "[BOA-417]"}}) + "\n")
    with pytest.raises(ValueError, match="inconclusive"):
        S.build_task_spec(tasks[0], corpus=tmp_path, instruction="task")


def test_frozen_config_commands_are_accepted_by_verification_scorer():
    from compaction.s2.planted_score import Event, score
    for cmd in S.COMMANDS.values():
        assert score([Event(0, "submission", {"text": "done"})],
                     {"kind": "verification_output", "command": cmd})["outcome"] == "violated"


def test_spend_tag_enforces_25_dollars_without_other_pilots_tag():
    spend = C.pilot_spend()
    assert spend.tags == (C.SPEND_TAG,)
    assert spend.caps == {C.SPEND_TAG: 25.0}
