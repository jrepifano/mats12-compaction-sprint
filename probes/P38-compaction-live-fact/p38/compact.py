"""P38 compaction (plan.md, Setup: Compaction): pi-mono's prompts, verbatim (prompts/SOURCES.md),
with the agent model as its own summariser.

  S1 = generateSummary(user ticket + A + B + C)                       (one compaction)
  S3 = update(update(generateSummary(ticket + A), B), C)               (three nested compactions)

The request is pi-mono's `generateSummaryWithUsage`: system = SUMMARIZATION_SYSTEM_PROMPT; user =
"<conversation>\n" + serializeConversation(messages) + "\n</conversation>\n\n" [+
"<previous-summary>\n" + previous + "\n</previous-summary>\n\n"] + prompt, where prompt is
SUMMARIZATION_PROMPT, or for an update "The messages above are NEW conversation messages to
incorporate into the existing summary provided in <previous-summary> tags.\n\n" +
UPDATE_SUMMARIZATION_INSTRUCTIONS. Serialization as P08 (`[User]:`, `[Assistant]:`,
`[Assistant tool calls]: name(k=json, ...)`, `[Tool result]:` cut at 2,000 characters); the
system prompt and reasoning are not serialized. A reply that is empty or stopped at the length
cap is retried once (pi-mono refuses to persist a length-capped summary).
"""
from __future__ import annotations

import json

from p38 import api
from p38 import contracts as C

TOOL_RESULT_MAX_CHARS = 2000
CALLS_PER_SUMMARY = 2
UPDATE_HEAD = ("The messages above are NEW conversation messages to incorporate into the existing "
               "summary provided in <previous-summary> tags.\n\n")


def prompt_text(name: str) -> str:
    return (C.PROMPTS_DIR / name).read_text(encoding="utf-8")


def truncate_for_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


def _js_json(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def message_parts(m: dict) -> list[str]:
    role = m["role"]
    text = m.get("content") or ""
    if role == "user":
        return [f"[User]: {text}"] if text else []
    if role == "assistant":
        parts = [f"[Assistant]: {text}"] if text else []
        calls = []
        for c in m.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": c["function"]["arguments"]}
            if not isinstance(args, dict):
                args = {"_raw": args}
            calls.append(f"{c['function']['name']}("
                         + ", ".join(f"{k}={_js_json(v)}" for k, v in args.items()) + ")")
        if calls:
            parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        return parts
    if role == "tool":
        return [f"[Tool result]: {truncate_for_summary(text, TOOL_RESULT_MAX_CHARS)}"] if text else []
    return []


def serialize(messages: list[dict]) -> str:
    parts: list[str] = []
    for m in messages:
        parts.extend(message_parts(m))
    return "\n\n".join(parts)


def request(messages: list[dict], previous: str | None = None) -> list[dict]:
    text = f"<conversation>\n{serialize(messages)}\n</conversation>\n\n"
    if previous:
        text += f"<previous-summary>\n{previous}\n</previous-summary>\n\n"
        text += UPDATE_HEAD + prompt_text("pimono_update_instructions.txt")
    else:
        text += prompt_text("pimono_summarization_prompt.txt")
    return [{"role": "system", "content": prompt_text("pimono_system_prompt.txt")},
            {"role": "user", "content": text}]


def segments(ctx: dict) -> dict[str, list[dict]]:
    """The prefix split at the phase ends: A includes the user ticket (index 1)."""
    m, b = ctx["messages"], ctx["bounds"]
    return {"A": m[1:b["A"]], "B": m[b["A"]:b["B"]], "C": m[b["B"]:b["C"]],
            "ALL": m[1:b["C"]]}


async def _one(model: C.Model, msgs: list[dict], chat, on_reply) -> dict:
    attempts = []
    for _ in range(CALLS_PER_SUMMARY):
        r = await chat(model, msgs, max_tokens=C.SUMMARY_MAX_TOKENS)
        if on_reply:
            on_reply(r)
        attempts.append({"text": r.text, "finish": r.finish_reason, "usd": api.charge(r),
                         "usage": r.usage, "provider": r.provider, "error": r.error})
        if r.text.strip() and r.finish_reason != "length" and not r.error:
            return {"text": r.text.strip(), "ok": True, "attempts": attempts}
    return {"text": "", "ok": False, "attempts": attempts}


async def summarize_context(model: C.Model, ctx: dict, chat=api.chat, on_reply=None) -> dict:
    """{"S1": {...}, "S3": {...}, "S3_steps": [s_after_A, s_after_B, s_after_C]}"""
    seg = segments(ctx)
    s1 = await _one(model, request(seg["ALL"]), chat, on_reply)
    steps = []
    prev = None
    for name in ("A", "B", "C"):
        r = await _one(model, request(seg[name], prev), chat, on_reply)
        steps.append(r)
        if not r["ok"]:
            break
        prev = r["text"]
    ok3 = len(steps) == 3 and all(s["ok"] for s in steps)
    return {"ctx_id": ctx["ctx_id"], "model": model.slug, "S1": s1,
            "S3": {"text": steps[-1]["text"] if ok3 else "", "ok": ok3},
            "S3_steps": steps, "status": "ok" if (s1["ok"] and ok3) else "failed",
            "usd": round(sum(a["usd"] for r in [s1, *steps] for a in r["attempts"]), 6)}


def worst_case_usd(model: C.Model, ctx: dict) -> float:
    seg = segments(ctx)
    tot = 0.0
    for msgs in (seg["ALL"], seg["A"], seg["B"], seg["C"]):
        req = request(msgs, "x" * 6000 if msgs is not seg["A"] and msgs is not seg["ALL"] else None)
        tot += CALLS_PER_SUMMARY * api.list_cost(model, api.request_bound_tokens(req),
                                                 C.SUMMARY_MAX_TOKENS)
    return tot
