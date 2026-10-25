"""Offline fake-model contracts for exact planting and fork state."""

import json

import pytest

from compaction.s2.interact import UserSim
from compaction.s2.model import Reply, RunCapReached, Usage
from compaction.s2.planting import NO_RESTATEMENT, PlantingEntry, PlantingSchedule


class FixedModel:
    def __init__(self, text="A maintainer reply."):
        self.text = text
        self.requests = []

    def complete(self, messages, tools, max_tokens, *, purpose):
        self.requests.append(messages)
        if isinstance(self.text, Exception):
            raise self.text
        return Reply({"role": "assistant", "content": self.text},
                     Usage(100, 10, 0, 0, 0.0, "stub"), "stop")


def sim(entries, text="A maintainer reply."):
    return UserSim("Persona", FixedModel(text), lambda *_: (0, "", ""),
                   planting=PlantingSchedule(entries))


def test_exact_bytes_in_every_store_and_restored_fork_never_redelivers():
    exact = "  Use [TASK-é] — exact spacing.\nSecond line.  "
    s = sim([{"obligation_id": "arbitrary", "text": exact, "trigger": {"reply_index": 1}}])
    reply = s.ask("Question?")
    assert reply == "A maintainer reply.\n\n" + exact
    assert s.state.conversation[-1]["content"].encode() == reply.encode()
    assert s.ask_log[-1]["reply"].encode() == reply.encode()
    assert s.io[-1]["reply"].encode() == reply.encode()
    assert s.ask_log[-1]["delivered"][0]["text"].encode() == exact.encode()
    export = json.loads(json.dumps(s.export(), ensure_ascii=False))
    a = UserSim.restore(export, "Persona", FixedModel(), lambda *_: (0, "", ""))
    b = UserSim.restore(export, "Persona", FixedModel(), lambda *_: (0, "", ""))
    assert a.export() == export
    assert a.ask("Next?") == "A maintainer reply."
    assert a.ask_log[-1]["delivered"] == []
    assert b.export() == export == s.export()
    assert NO_RESTATEMENT in a.model.requests[-1][0]["content"]


def test_stage_reply_index_and_first_reply_after_compaction():
    s = sim([
        {"obligation_id": "early", "text": "Early rule.",
         "trigger": {"reply_index": 1, "stage": "02_implement"}},
        {"obligation_id": "late", "text": "Late rule.",
         "trigger": {"after_compaction": 1, "stage": "02_implement"}},
    ])
    s.set_delivery_context(stage="01_plan")
    assert s.ask("Question?") == "A maintainer reply."
    s.set_delivery_context(stage="02_implement", cumulative_tokens=200)
    assert s.ask("Question?").endswith("Early rule.")
    first = s.record_compaction(1, pre_tokens=78000, cumulative_tokens=500)
    assert first["pre_tokens"] == 78000
    assert first["obligations"][0]["token_distance"] == 300
    assert first["obligations"][1]["delivered"] is False
    restored = UserSim.restore(s.export(), "Persona", FixedModel(), lambda *_: (0, "", ""))
    restored.set_delivery_context(stage="02_implement", cumulative_tokens=600)
    assert restored.ask("Question?").endswith("Late rule.")
    assert restored.ask("Question?") == "A maintainer reply."
    second = restored.record_compaction(2, pre_tokens=79000, cumulative_tokens=900)
    assert [r["token_distance"] for r in second["obligations"]] == [700, 300]


def test_simulator_restatements_count_generated_text_only():
    rule = "Run the special command."
    s = sim([{"obligation_id": "rule", "text": rule, "trigger": {"reply_index": 1}}])
    s.ask("Question?")
    assert s.planting.restatements == []
    s.model.text = f"As discussed: {rule} {rule}"
    reply = s.ask("Question?")
    assert reply == s.model.text
    assert s.ask_log[-1]["restatements"][0]["occurrences"] == 2
    assert s.io[-1]["restatements"] == s.ask_log[-1]["restatements"]
    boundary = s.record_compaction(1, pre_tokens=80000)
    assert boundary["obligations"][0]["restatement_count"] == 1
    assert boundary["obligations"][0]["token_distance"] is None


def test_multiple_due_rules_preserve_schedule_order_and_no_future_text_leak():
    s = sim([
        {"obligation_id": "b", "text": "Second alphabetically.", "trigger": {"reply_index": 1}},
        {"obligation_id": "a", "text": "First alphabetically.", "trigger": {"reply_index": 1}},
    ], text="")
    assert s.ask("Question?") == "Second alphabetically.\n\nFirst alphabetically."
    assert all("alphabetically" not in m["content"] for m in s.model.requests[0])


def test_failed_model_call_does_not_consume_planting_reply_index():
    s = sim([{"obligation_id": "x", "text": "Rule.", "trigger": {"reply_index": 1}}],
            text=RunCapReached("cap"))
    with pytest.raises(RunCapReached):
        s.ask("Question?")
    assert not s.planting.deliveries and s.planting.reply_count == 0
    s.model.text = "Reply"
    assert s.ask("Question?") == "Reply\n\nRule."
    assert s.planting.deliveries["x"]["call_index"] == 2


def test_old_exports_restore_without_planting_or_new_log_keys():
    s = UserSim("Persona", FixedModel(), lambda *_: (0, "", ""))
    s.ask("Question?")
    old = s.export()
    for key in ("planting", "io", "ask_log"):
        old.pop(key, None)
    restored = UserSim.restore(old, "Persona", FixedModel(), lambda *_: (0, "", ""))
    assert restored.planting is None
    assert restored.ask("Question?") == "A maintainer reply."
    assert NO_RESTATEMENT not in restored.system_prompt


@pytest.mark.parametrize("trigger", [
    {}, {"reply_index": 0}, {"reply_index": True}, {"after_compaction": -1},
    {"reply_index": 1, "after_compaction": 1}, {"reply_index": 1, "typo": 1},
])
def test_invalid_trigger_refused(trigger):
    with pytest.raises(ValueError):
        PlantingEntry("id", "text", trigger)


def test_duplicate_ids_and_nonmonotonic_boundary_refused():
    entry = PlantingEntry("id", "text", {"reply_index": 1})
    with pytest.raises(ValueError, match="unique"):
        PlantingSchedule([entry, entry])
    p = PlantingSchedule([entry])
    with pytest.raises(ValueError, match="consecutive"):
        p.record_compaction(2, pre_tokens=100)
    p.record_compaction(1, pre_tokens=100, cumulative_tokens=100)
    with pytest.raises(ValueError, match="consecutive"):
        p.record_compaction(1, pre_tokens=100)
    with pytest.raises(ValueError, match="backwards"):
        p.set_context(cumulative_tokens=99)


def test_omitted_context_fields_preserve_clock_and_stage_high_water_survives_missing():
    s = sim([{"obligation_id": "x", "text": "Rule.",
              "trigger": {"reply_index": 1, "stage": "02_implement"}}])
    s.set_delivery_context(stage="02_implement", cumulative_tokens=100)
    s.set_delivery_context(stage="02_implement")
    s.set_delivery_context(cumulative_tokens=120)
    assert s.ask("Question?").endswith("Rule.")
    assert s.planting.deliveries["x"]["cumulative_tokens"] == 120
    s.set_delivery_context(cumulative_tokens=None)
    with pytest.raises(ValueError, match="backwards"):
        s.set_delivery_context(cumulative_tokens=119)
    restored = UserSim.restore(s.export(), "Persona", FixedModel(), lambda *_: (0, "", ""))
    with pytest.raises(ValueError, match="backwards"):
        restored.set_delivery_context(cumulative_tokens=119)


def test_legacy_export_and_io_shape_unchanged_but_empty_schedule_changes_prompt():
    legacy = UserSim("Persona", FixedModel(), lambda *_: (0, "", ""))
    legacy.ask("Question?")
    assert set(legacy.export()) == {"upstream", "config", "persona_sha256", "state"}
    assert all(row["kind"] != "reply" for row in legacy.io)
    scheduled = sim([])
    assert scheduled.system_prompt != legacy.system_prompt
    assert NO_RESTATEMENT in scheduled.system_prompt
