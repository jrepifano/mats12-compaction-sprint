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
