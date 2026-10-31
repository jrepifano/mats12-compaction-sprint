"""The model's reasoning text, kept in a side log per run (week 2, 27 Oct).

The loop stores each reply through `normalize_assistant`, which keeps only the content and the
tool calls, so the reasoning text never reaches the history sent back to the model or the
container. On the pinned route (OpenRouter -> Together, DeepSeek V4.1 Flash, effort max) a
reply carries it in `reasoning` (a string) and `reasoning_details` (a list of
{"type": "reasoning.text", "text": ...}); `reasoning_content` is DeepSeek's own API's name
(results/sprint/week2/reasoning_feasibility.json).

`ReasoningLog` wraps a ChatModel: it passes every call through unchanged and appends one JSON
line per reply to `<run dir>/reasoning.jsonl` on the host: the call's purpose, its kind
("agent", or "summary" for the summarizer, whose purposes start with "summary:"), the agent
call's index, the text and the raw details. Calls that end in ModelError have no reply and
write nothing.

Readers: `read_log` (tolerates a torn last line), `agent_reasoning` (call index -> entry for
one loop's purpose prefix) and `task_view` (the experiment-2 "second task + CoT" view: the
second task's messages from its opening on, each assistant turn with its reasoning).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from compaction.s2.model import ChatModel, Reply

LOG_NAME = "reasoning.jsonl"
FIELDS = ("reasoning_content", "reasoning", "reasoning_details")


def reasoning_of(msg: dict) -> dict:
    """The reasoning text of one reply message: {"text", "field", "details"}. The text is the
    first non-empty string field, else the joined text items of `reasoning_details`."""
    details = msg.get("reasoning_details")
    details = details if isinstance(details, list) and details else None
    for k in ("reasoning_content", "reasoning"):
        v = msg.get(k)
        if isinstance(v, str) and v:
            return {"text": v, "field": k, "details": details}
    if details:
        text = "".join(d.get("text") or "" for d in details if isinstance(d, dict))
        return {"text": text or None, "field": "reasoning_details", "details": details}
    return {"text": None, "field": None, "details": None}


def call_index(purpose: str) -> int | None:
    """The loop's call number from an agent purpose (`<kind>:<run id>:<n>`)."""
    tail = purpose.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


@dataclass
class ReasoningLog:
    """A ChatModel that logs each reply's reasoning text and returns the reply unchanged."""

    inner: ChatModel
    path: Path
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def route(self):
        return self.inner.route

    def complete(self, messages: list[dict], tools: list[dict] | None, max_tokens: int, *,
                 purpose: str) -> Reply:
        reply = self.inner.complete(messages, tools, max_tokens, purpose=purpose)
        self._write(reply, purpose)
        return reply

    def _write(self, reply: Reply, purpose: str) -> None:
        r = reasoning_of(reply.message)
        summary = purpose.startswith("summary:")
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "purpose": purpose, "kind": "summary" if summary else "agent",
                  "call": None if summary else call_index(purpose), "field": r["field"],
                  "text": r["text"], "details": r["details"],
                  "reasoning_tokens": reply.usage.reasoning_tokens,
                  "finish_reason": reply.finish_reason}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())


def read_log(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn last line from a crash
    return out


def agent_reasoning(run_dir: Path, prefix: str) -> dict[int, dict]:
    """Call index -> the log entry, for the agent calls whose purpose starts with `prefix`
    (e.g. "B:" for a second task). A later entry for the same index wins."""
    return {e["call"]: e for e in read_log(Path(run_dir) / LOG_NAME)
            if e.get("kind") == "agent" and e.get("call") is not None
            and str(e.get("purpose", "")).startswith(prefix)}


def task_view(run_dir: Path, prefix: str = "B:") -> list[dict]:
    """The second task as experiment 2's monitor reads it, with the agent's reasoning: the
    messages from the second task's opening (result.json `extra.b_message_index`) on, each
    assistant message carrying `reasoning` (None when the log has no text for that call).
    The k-th assistant message after the opening is the second task's call k."""
    run_dir = Path(run_dir)
    msgs = json.loads((run_dir / "messages.json").read_text())
    res = json.loads((run_dir / "result.json").read_text())
    start = (res.get("extra") or {}).get("b_message_index")
    if start is None:
        raise ValueError(f"{run_dir}: no b_message_index (not a second-task run)")
    by_call = agent_reasoning(run_dir, prefix)
    out, k = [], 0
    for m in msgs[start:]:
        row = dict(m)
        if m.get("role") == "assistant":
            k += 1
            e = by_call.get(k)
            row["call"] = k
            row["reasoning"] = e.get("text") if e else None
        out.append(row)
    return out
