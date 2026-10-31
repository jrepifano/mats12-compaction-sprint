"""The item record every scenario builder produces and every renderer consumes.

A view is a list of OpenAI-format chat messages (`system` / `user` / `assistant` with
`tool_calls` / `tool`), as the task-chaining harness stores them. Its LAST message is the
act's assistant message, cut so that its last tool call is the act: later tool calls in
that message are removed and no tool result follows. Labels that a view adds (pinned user
turns, reasoning) are already in the message text; renderers never add content.

Reasoning (view 5) rides on an assistant message as the extra key `reasoning` (a string),
which the renderer surfaces under each prompt's channel; other views carry no such key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

VIEWS = ("full", "summary_tail", "current_only", "pinned_user_turns", "current_plus_reasoning")

PINNED_LABEL = "[verbatim user message from earlier in this session]"
REASONING_LABEL = "[agent's private reasoning]"
# View 5b (Advisor, 28 Oct; staged, not in VIEWS until Jacob approves): view 5 with the agent's
# reasoning kept only on the last N agent messages up to and including the act. View 5 on new
# runs is 0.9-3.2M tokens, past the monitors' 1M context. N is fixed in config.
LAST_REASONING_VIEW = "current_plus_last_reasoning"


@dataclass
class Item:
    id: str
    scenario: str                 # "force_push" | "p38" | "p07"
    set: str                      # e.g. "primary", "kept", "no_history", "benign_push",
                                  # "p38_natural", "p38_false_licence", "p38_authorized"
    authorized: bool              # ground truth from the design, never a judge
    cluster: str                  # resampling unit (history / context id)
    views: dict[str, list[dict]]  # view name -> message list ending at the act
    act_command: str              # the act's command text (for checks and baselines)
    evidence: dict = field(default_factory=dict)  # grant text, refs before/after, ...
    meta: dict = field(default_factory=dict)      # source paths, indices, flags

    def act(self, view: str) -> dict:
        """The act's tool call in a view (the last tool call of the last message)."""
        msg = self.views[view][-1]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            raise ValueError(f"{self.id}/{view}: last message is not an assistant tool call")
        return msg["tool_calls"][-1]

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> Item:
        return cls(**d)

    def view_sha256(self, view: str) -> str:
        return sha256_json(self.views[view])


def last_reasoning(view5: list[dict], n: int) -> list[dict]:
    """View 5 with `reasoning` kept on the last `n` assistant messages (the act's included)
    and removed from every earlier one; nothing else changes."""
    if n < 1:
        raise ValueError("n must be >= 1")
    asst = [i for i, m in enumerate(view5) if m.get("role") == "assistant"]
    keep = set(asst[-n:])
    out = []
    for i, m in enumerate(view5):
        if m.get("role") == "assistant" and i not in keep and "reasoning" in m:
            m = {k: v for k, v in m.items() if k != "reasoning"}
        out.append(m)
    return out


def with_last_reasoning(item: Item, n: int) -> Item:
    """The item with view 5b added (derived from its view 5); unchanged without view 5."""
    if "current_plus_reasoning" not in item.views:
        return item
    views = dict(item.views)
    views[LAST_REASONING_VIEW] = last_reasoning(item.views["current_plus_reasoning"], n)
    return Item(**{**item.to_json(), "views": views,
                   "meta": {**item.meta, "last_reasoning_n": n}})


def base_record(item: Item) -> dict:
    """The item's record without the derived view 5b (bound records are built without it).
    5b is stripped only when it is exactly view 5 cut to the recorded N; any other 5b (no view
    5, no N, or a different message list) stays in, so the binding catches it."""
    d = item.to_json()
    v5b, n = d["views"].get(LAST_REASONING_VIEW), d["meta"].get("last_reasoning_n")
    if v5b is not None and "current_plus_reasoning" in d["views"] and isinstance(n, int) \
            and n >= 1 and v5b == last_reasoning(d["views"]["current_plus_reasoning"], n):
        d["views"] = {k: v for k, v in d["views"].items() if k != LAST_REASONING_VIEW}
        d["meta"] = {k: v for k, v in d["meta"].items() if k != "last_reasoning_n"}
    return d


def sha256_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()


def tool_call_command(tc: dict) -> str:
    """The `command` of an OpenAI-format bash tool call (arguments may be a JSON string)."""
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args
    if isinstance(args, dict):
        return str(args.get("command", json.dumps(args, sort_keys=True)))
    return str(args)
