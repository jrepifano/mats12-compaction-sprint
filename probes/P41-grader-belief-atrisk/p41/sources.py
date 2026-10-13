"""P41 message helpers and the fresh episodes' first two messages.

Ported from probes/P37-grader-belief-causal/p37/sources.py (reasoning_of, visible_text_of,
assistant_turns). P41 has no old source transcripts: `initial_messages` reads only messages[:2]
(the system prompt and X3b's neutral first user turn) of X3b's neutral GLM episodes for a task,
checks they are identical across that task's episodes, and returns them. No outcome or later
turn is read.
"""
from __future__ import annotations

import hashlib
import json

from p41 import contracts as C


def reasoning_of(msg: dict) -> str:
    """Readable reasoning text of an assistant message dump ('' if none)."""
    c = msg.get("content")
    if not isinstance(c, list):
        return ""
    return "\n".join(x.get("reasoning") or "" for x in c
                     if isinstance(x, dict) and x.get("type") == "reasoning"
                     and not x.get("redacted"))


def visible_text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text") or "" for x in c or []
                     if isinstance(x, dict) and x.get("type") == "text")


def assistant_turns(messages: list[dict]) -> list[dict]:
    """[{turn (1-based), index, reasoning, text, tool_calls}] for each assistant message."""
    out, n = [], 0
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        n += 1
        out.append({"turn": n, "index": i, "reasoning": reasoning_of(m),
                    "text": visible_text_of(m), "tool_calls": m.get("tool_calls") or []})
    return out


def _canon(msgs: list[dict]) -> str:
    keep = [{"role": m["role"], "text": visible_text_of(m)} for m in msgs]
    return json.dumps(keep, sort_keys=True)


def initial_messages(task_id: str) -> tuple[list[dict], str]:
    """(X3b's neutral [system, user] dumps for the task, sha256 of their role+text). Raises if
    the task's X3b neutral episodes disagree on them."""
    paths = sorted((C.X3B_NEUTRAL_DIR / task_id).glob("r*_a*.json"))
    if not paths:
        raise FileNotFoundError(f"no X3b neutral episodes for {task_id}")
    first, seen = None, set()
    for p in paths:
        msgs = json.loads(p.read_text())["messages"][:2]
        if [m["role"] for m in msgs] != ["system", "user"]:
            raise ValueError(f"{p}: first messages are not system, user")
        seen.add(_canon(msgs))
        first = first or msgs
    if len(seen) != 1:
        raise ValueError(f"{task_id}: X3b neutral first messages differ across episodes")
    return first, hashlib.sha256(next(iter(seen)).encode()).hexdigest()
