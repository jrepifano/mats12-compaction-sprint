"""Summarizer requests for the three harness prompts, and the text views the judge reads.

Prompts are the verbatim files in `prompts/` (sources and commits in `prompts/SOURCES.md`).
Serialization follows pi-mono's `serializeConversation` (`[User]:`, `[Assistant]:`,
`[Assistant tool calls]: name(k=<json>, ...)`, `[Tool result]:`) for every harness; log.md
"Design decisions" 1-3 say why and what each harness keeps verbatim.

- codex:     user = serialized prefix, then the Codex compaction prompt (no system message).
             Kept verbatim beside the summary: the prefix's user messages.
- openhands: system = summarizing_system.j2; user = summarizing_events.j2 rendered over
             events[KEEP_FIRST:cut] (one event per message). Kept verbatim: events[:KEEP_FIRST].
- pimono:    system = SUMMARIZATION_SYSTEM_PROMPT; user = <conversation>...</conversation> with
             tool results truncated to 2,000 characters, then SUMMARIZATION_PROMPT. Kept: none
             of the prefix.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import cache
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
HARNESSES = ("codex", "openhands", "pimono")
OPENHANDS_KEEP_FIRST = 2
PIMONO_TOOL_RESULT_MAX_CHARS = 2000


@cache
def prompt_text(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _js_json(v) -> str:
    """JSON.stringify of a tool argument (compact separators, unicode kept)."""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def truncate_for_summary(text: str, max_chars: int) -> str:
    """pi-mono `truncateForSummary`."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


def message_parts(m: dict, tool_result_max: int | None = None) -> list[str]:
    """pi-mono `serializeConversation` for one message (system messages give nothing)."""
    role = m["role"]
    if role == "user":
        return [f"[User]: {m['text']}"] if m["text"] else []
    if role == "assistant":
        parts = []
        if m["text"]:
            parts.append(f"[Assistant]: {m['text']}")
        calls = []
        for c in m.get("tool_calls") or []:
            args = c.get("args") or {}
            if not isinstance(args, dict):
                args = {"_raw": args}
            calls.append(f"{c['name']}(" + ", ".join(f"{k}={_js_json(v)}" for k, v in
                                                     args.items()) + ")")
        if calls:
            parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        return parts
    if role == "tool":
        t = m["text"]
        if not t:
            return []
        if tool_result_max is not None:
            t = truncate_for_summary(t, tool_result_max)
        return [f"[Tool result]: {t}"]
    return []


def serialize(messages: Sequence[dict], tool_result_max: int | None = None) -> str:
    parts: list[str] = []
    for m in messages:
        parts.extend(message_parts(m, tool_result_max))
    return "\n\n".join(parts)


def _openhands_events(events: Sequence[dict]) -> str:
    import jinja2

    tpl = jinja2.Template(prompt_text("openhands_summarizing_events.j2"))
    strs = ["\n\n".join(message_parts(m)) for m in events]
    return tpl.render(events=[s for s in strs if s])


def summarizer_request(t: dict, harness: str) -> tuple[str | None, str]:
    """(system, user) for one transcript's prefix under one harness prompt."""
    prefix = t["messages"][: t["cut"]]
    if harness == "codex":
        return None, serialize(prefix) + "\n\n" + prompt_text("codex_prompt.md")
    if harness == "openhands":
        return (prompt_text("openhands_summarizing_system.j2"),
                _openhands_events(prefix[OPENHANDS_KEEP_FIRST:]))
    if harness == "pimono":
        conv = serialize(prefix, PIMONO_TOOL_RESULT_MAX_CHARS)
        return (prompt_text("pimono_system_prompt.txt"),
                f"<conversation>\n{conv}\n</conversation>\n\n"
                + prompt_text("pimono_summarization_prompt.txt"))
    raise ValueError(f"unknown harness {harness!r}")


def retained_message_indices(t: dict, harness: str) -> list[int]:
    """Prefix messages the harness keeps verbatim beside the summary."""
    prefix = t["messages"][: t["cut"]]
    if harness == "codex":
        return [i for i, m in enumerate(prefix) if m["role"] == "user"]
    if harness == "openhands":
        return list(range(min(OPENHANDS_KEEP_FIRST, len(prefix))))
    if harness == "pimono":
        return []
    raise ValueError(f"unknown harness {harness!r}")


def summarizer_saw_task(t: dict, harness: str) -> bool:
    """Whether the task message (the first user message) is inside the summarized span."""
    first_user = next(i for i, m in enumerate(t["messages"]) if m["role"] == "user")
    return not (harness == "openhands" and first_user < OPENHANDS_KEEP_FIRST)


def judge_view(t: dict) -> str:
    """The whole prefix as the judge reads it: same serialization, nothing truncated."""
    return serialize(t["messages"][: t["cut"]])


_FILE_ITEM_RE = __import__("re").compile(r"^The file (.+) was created or modified$")


def item_anchor(t: dict, item: dict) -> str:
    """The text a summarizer must have been shown to know a checklist fact ("" = always
    shown: actions and absences live in tool calls, which no harness truncates)."""
    k = item["kind"]
    if k in ("task", "constraint"):
        return next(m["text"] for m in t["messages"] if m["role"] == "user")[:200]
    if k == "file":
        m = _FILE_ITEM_RE.match(item["text"])  # basename: agents often `cd` then use it
        return m.group(1).rsplit("/", 1)[-1] if m else ""
    if k == "error":
        return (t["truth"].get("last_error") or {}).get("text", "")
    if k == "test_status":
        tr = t["truth"]
        src = tr["last_test"] if tr["last_test"]["status"] in ("pass", "fail") else \
            tr.get("last_script") or {}
        return src.get("evidence", "") if src.get("status") in ("pass", "fail") else ""
    return ""


def visible_items(t: dict, harness: str) -> set[str]:
    """Checklist ids whose anchor text is inside what this harness shows its summarizer
    (OpenHands keeps the task message out; pi-mono cuts tool results at 2,000 characters)."""
    system, user = summarizer_request(t, harness)
    shown = (system or "") + user
    return {c["id"] for c in t["checklist"] if item_anchor(t, c) in shown}
