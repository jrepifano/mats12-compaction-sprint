"""Deterministic, serializable maintainer-reply planting (no model or tool calls).

Reply indices are one-based successful replies, globally or within the optional stage.
Compaction indices are one-based and must be announced by the native runner before it
exports a boundary. Token positions are supplied by the runner on one cumulative token
clock; unknown positions stay null rather than using prompt sizes across compactions.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field

NO_RESTATEMENT = (
    "\n\nSome earlier maintainer replies may contain fixed task obligations appended by "
    "the harness. Do not repeat or paraphrase those obligations in later replies. "
    "They remain binding; discuss the task normally without restating them."
)


@dataclass(frozen=True)
class PlantingEntry:
    obligation_id: str
    text: str
    trigger: dict

    def __post_init__(self):
        if (not isinstance(self.obligation_id, str) or not self.obligation_id.strip()
                or not isinstance(self.text, str) or not self.text.strip()):
            raise ValueError("planting entries require an id and nonblank exact text")
        kinds = set(self.trigger) & {"reply_index", "after_compaction"}
        if len(kinds) != 1 or set(self.trigger) - kinds - {"stage"}:
            raise ValueError("trigger needs exactly reply_index or after_compaction, and optional stage")
        value = self.trigger[next(iter(kinds))]
        if type(value) is not int or value < 1:
            raise ValueError("trigger indices must be positive integers")
        if "stage" in self.trigger and not self.trigger["stage"]:
            raise ValueError("trigger stage must be nonempty")


@dataclass
class PlantingSchedule:
    entries: list[PlantingEntry | dict] = field(default_factory=list)
    stage: str | None = None
    cumulative_tokens: int | None = None
    reply_count: int = 0
    stage_reply_counts: dict[str, int] = field(default_factory=dict)
    compaction_index: int = 0
    deliveries: dict[str, dict] = field(default_factory=dict)
    restatements: list[dict] = field(default_factory=list)
    boundaries: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.entries = [PlantingEntry(**copy.deepcopy(e)) if isinstance(e, dict)
                        else copy.deepcopy(e) for e in self.entries]
        ids = [e.obligation_id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("obligation ids must be unique")
        if set(self.deliveries) - set(ids):
            raise ValueError("delivery record names an unknown obligation")

    def set_context(self, *, stage: str | None = None,
                    cumulative_tokens: int | None = None) -> None:
        self.stage = stage
        if cumulative_tokens is not None:
            if type(cumulative_tokens) is not int or cumulative_tokens < 0:
                raise ValueError("cumulative_tokens must be a nonnegative integer")
            if self.cumulative_tokens is not None and cumulative_tokens < self.cumulative_tokens:
                raise ValueError("cumulative token clock cannot move backwards")
        # A missing measurement must not reuse a stale token position.
        self.cumulative_tokens = cumulative_tokens

    def _due(self, entry: PlantingEntry) -> bool:
        if entry.obligation_id in self.deliveries:
            return False
        trigger = entry.trigger
        stage = trigger.get("stage")
        if stage is not None and stage != self.stage:
            return False
        if "reply_index" in trigger:
            index = self.stage_reply_counts.get(stage, 0) if stage else self.reply_count
            return index + 1 == trigger["reply_index"]
        return self.compaction_index >= trigger["after_compaction"]

    def apply(self, generated: str, *, call_index: int) -> tuple[str, dict]:
        """Append exact text once, then return the shared audit record for all stores."""
        due = [e for e in self.entries if self._due(e)]
        reply_index = self.reply_count + 1
        echoes = []
        for entry in self.entries:
            if entry.text in generated:
                echo = {"obligation_id": entry.obligation_id, "call_index": call_index,
                        "reply_index": reply_index, "stage": self.stage,
                        "occurrences": generated.count(entry.text),
                        "previously_delivered": entry.obligation_id in self.deliveries,
                        "match": "exact_string"}
                self.restatements.append(echo)
                echoes.append(copy.deepcopy(echo))
        text = generated
        delivered = []
        for entry in due:
            text += ("\n\n" if text else "") + entry.text
            row = {"obligation_id": entry.obligation_id, "text": entry.text,
                   "call_index": call_index, "reply_index": reply_index, "stage": self.stage,
                   "compaction_index": self.compaction_index,
                   "cumulative_tokens": self.cumulative_tokens}
            self.deliveries[entry.obligation_id] = row
            delivered.append(copy.deepcopy(row))
        self.reply_count = reply_index
        if self.stage is not None:
            self.stage_reply_counts[self.stage] = self.stage_reply_counts.get(self.stage, 0) + 1
        return text, {"delivered": delivered, "restatements": echoes}

    def record_compaction(self, k: int, *, pre_tokens: int | None,
                          cumulative_tokens: int | None = None) -> dict:
        """Mark the boundary before export; its next reply can deliver a late rule."""
        if type(k) is not int or k != self.compaction_index + 1:
            raise ValueError("compactions must be recorded once in increasing consecutive order")
        rules = []
        for entry in self.entries:
            delivered = self.deliveries.get(entry.obligation_id)
            start = delivered.get("cumulative_tokens") if delivered else None
            distance = (cumulative_tokens - start
                        if cumulative_tokens is not None and start is not None else None)
            if distance is not None and distance < 0:
                raise ValueError("boundary precedes obligation delivery on token clock")
            rules.append({"obligation_id": entry.obligation_id,
                          "delivered": delivered is not None,
                          "delivery_call_index": delivered.get("call_index") if delivered else None,
                          "token_distance": distance,
                          "token_distance_source": "runner_cumulative_tokens" if distance is not None else "unavailable",
                          "restatement_count": sum(r["obligation_id"] == entry.obligation_id
                                                   and r["previously_delivered"]
                                                   for r in self.restatements)})
        row = {"compaction_index": k, "pre_tokens": pre_tokens,
               "cumulative_tokens": cumulative_tokens, "obligations": rules}
        self.set_context(stage=self.stage, cumulative_tokens=cumulative_tokens)
        self.compaction_index = k
        self.boundaries.append(copy.deepcopy(row))
        return row

    def export(self) -> dict:
        return copy.deepcopy(asdict(self))

    @classmethod
    def restore(cls, exported: dict) -> PlantingSchedule:
        return cls(**copy.deepcopy(exported))
