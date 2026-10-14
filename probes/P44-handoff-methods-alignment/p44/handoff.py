"""P44 handoff methods (plan.md "Setup"): pure functions that build each arm's next-generation
context and insert the seeds. No I/O and no model calls; agent.py does those.

Message dumps are Inspect `ChatMessage.model_dump(exclude_none=True)` dicts. The pi-mono
serialisation is P23's (probes/P23-compaction-decisions-vs-cues/p23/compact.py `message_parts`,
from P01's render.py); the request and the iterative update prompt are P38's
(probes/P38-compaction-live-fact/p38/compact.py `request`), with byte copies of the prompt files
in prompts/ (prompts/SOURCES.md).
"""
from __future__ import annotations

import json
import re

from p44 import contracts as C

UPDATE_HEAD = ("The messages above are NEW conversation messages to incorporate into the existing "
               "summary provided in <previous-summary> tags.\n\n")


def prompt_text(name: str) -> str:
    return (C.PROMPTS_DIR / name).read_text(encoding="utf-8")


def text_of(content) -> str:
    """Visible text of a message content (reasoning dropped); P23's api._text_of."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text") or "")
    return "\n".join(p for p in parts if p)


def truncate_for_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


def _js_json(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def message_parts(m: dict, tool_result_max: int | None = C.TOOL_RESULT_MAX_CHARS) -> list[str]:
    role = m["role"]
    text = text_of(m.get("content"))
    if role == "user":
        return [f"[User]: {text}"] if text else []
    if role == "assistant":
        parts = [f"[Assistant]: {text}"] if text else []
        calls = []
        for c in m.get("tool_calls") or []:
            args = c.get("arguments") or {}
            if not isinstance(args, dict):
                args = {"_raw": args}
            calls.append(f"{c['function']}(" + ", ".join(f"{k}={_js_json(v)}"
                                                        for k, v in args.items()) + ")")
        if calls:
            parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        return parts
    if role == "tool":
        if not text:
            return []
        if tool_result_max is not None:
            text = truncate_for_summary(text, tool_result_max)
        return [f"[Tool result]: {text}"]
    return []


def serialize(dumps: list[dict], tool_result_max: int | None = C.TOOL_RESULT_MAX_CHARS) -> str:
    parts: list[str] = []
    for m in dumps:
        parts.extend(message_parts(m, tool_result_max))
    return "\n\n".join(parts)


def summary_request(new_dumps: list[dict], previous: str | None = None) -> tuple[str, str]:
    """(system text, user text) of pi-mono's compaction call: the first summary, or an update
    of `previous` with the messages since it (P38 `request`)."""
    text = f"<conversation>\n{serialize(new_dumps)}\n</conversation>\n\n"
    if previous:
        text += f"<previous-summary>\n{previous}\n</previous-summary>\n\n"
        text += UPDATE_HEAD + prompt_text("pimono_update_instructions.txt")
    else:
        text += prompt_text("pimono_summarization_prompt.txt")
    return prompt_text("pimono_system_prompt.txt"), text


def wrap_summary(summary: str) -> str:
    return prompt_text("pi_mono_wrapper.md").replace("{summary}", summary)


# --- seeds -------------------------------------------------------------------------------------
_KEY_DECISIONS = re.compile(r"^#{1,3}\s+key\s+decisions\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


def insert_seed_summary(summary: str, seed: str) -> str:
    """The seed as the last bullet of the summary's Key Decisions section (P23 FU5's place);
    a summary without that section gets one appended."""
    bullet = f"- {seed}"
    m = _KEY_DECISIONS.search(summary)
    if not m:
        return summary.rstrip() + f"\n\n## Key Decisions\n{bullet}\n"
    nxt = _HEADING.search(summary, m.end())
    end = nxt.start() if nxt else len(summary)
    section = summary[m.end():end].rstrip()
    return summary[:m.end()] + section + f"\n{bullet}\n" + ("\n" + summary[end:] if nxt else "")


def insert_seed_notes(notes: str | None, seed: str) -> str:
    """The seed appended as a bullet to the agent's notes."""
    base = (notes or "").rstrip()
    return (base + "\n" if base else "") + f"- {seed}\n"


def insert_seed_message(dump: dict, seed: str) -> dict:
    """The seed appended to an assistant message's visible text (a copy)."""
    if dump.get("role") != "assistant":
        raise ValueError("seeds go into an assistant message")
    out = json.loads(json.dumps(dump))
    content = out.get("content")
    if content is None or content == "":
        out["content"] = seed
    elif isinstance(content, str):
        out["content"] = content.rstrip() + "\n\n" + seed
    else:
        out["content"] = list(content) + [{"type": "text", "text": seed}]
    return out


# --- contexts ----------------------------------------------------------------------------------
def lastk_start(dumps: list[dict], k: int = C.LASTK_TURNS) -> int:
    """Index of the k-th last assistant message: L keeps dumps[start:]."""
    idx = [i for i, m in enumerate(dumps) if m.get("role") == "assistant"]
    if not idx:
        return len(dumps)
    return idx[-k] if len(idx) >= k else idx[0]


def summary_context(system: dict, task: dict, summary: str, pin_task: bool) -> list[dict]:
    head = [system, task] if pin_task else [system]
    return head + [{"role": "user", "content": wrap_summary(summary)}]


def notes_context(system: dict, task: dict, notes: str | None) -> list[dict]:
    body = notes if (notes and notes.strip()) else C.NOTES_MISSING
    return [system, task, {"role": "user", "content": C.NOTES_HEADER + body}]


def memfile_context(system: dict, task: dict, memory: str | None) -> list[dict]:
    body = memory if (memory and memory.strip()) else C.MEMFILE_MISSING
    return [system, task, {"role": "user", "content": C.MEMFILE_HEADER + body}]


def lastk_context(system: dict, task: dict, dumps: list[dict],
                  seed: str | None = None) -> tuple[list[dict], list[dict]]:
    """(context, kept messages). With a seed, it goes into the last kept assistant message."""
    kept = [json.loads(json.dumps(m)) for m in dumps[lastk_start(dumps):]]
    if seed is not None:
        last = max(i for i, m in enumerate(kept) if m.get("role") == "assistant")
        kept[last] = insert_seed_message(kept[last], seed)
    return [system, task, {"role": "user", "content": C.LASTK_HEADER}] + kept, kept


def memfile_system(system_text: str) -> str:
    return system_text + C.MEMFILE_SYSTEM_ADDENDUM
