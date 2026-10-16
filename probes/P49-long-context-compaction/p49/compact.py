"""Codex / pi / Inspect compaction of a context (plan "What each method carries";
lit-notes/products.md; prompts/SOURCES.md for the pinned source of every prompt file).

`await build(method, messages, tools, summarize, history_id=, k=, source=)` reproduces each
product's summariser request(s) and carry-over. `messages` is the context being compacted
(messages[0] = the agent's system prompt, messages[1] = S1's task). `summarize(messages,
max_tokens, tools_or_None, our_tokens)` sends one request (the caller binds client.chat) and
returns a contracts.Response.

Codex CLI (openai/codex ac9db7d, local path for non-OpenAI providers, `core/src/compact.rs`):
  request  = system + the history with each tool output middle-truncated at Codex's history
             policy (10,000 tokens x the 1.2 serialisation allowance = 12,000 tokens = 48,000
             bytes, `utils/output-truncation`, marker "…N tokens truncated…") + user(prompt.md).
             No tools are declared (`Prompt { ..Default::default() }`). Approximation: Codex
             truncates outputs when they are recorded, so the agent also saw them truncated;
             here only the summariser input is truncated (the carry-over keeps no tool outputs).
  overflow = drop the oldest item and its call/output counterpart, retry (compact.rs L359).
  carry    = system + real user messages (not earlier summaries) kept newest-first within
             20,000 tokens (bytes/4, rounded up; the message crossing the budget is
             middle-truncated to what remains), restored to original order, + user(SUMMARY_PREFIX
             + "\n" + summary). Codex also rebuilds environment context / AGENTS.md: not modelled.
pi (badlogic/pi-mono b06c1f2, `compaction/compaction.ts`, `utils.ts`, `messages.ts`):
  cut      = findCutPoint: walk back from the newest message summing chars/4 estimates until
             >= 20,000; cut at the nearest user/assistant message at or after it (never a tool
             result). If the cut is not at a user message the turn is split: messages before the
             turn's user message are summarised with SUMMARIZATION_PROMPT (max 13,107 tokens) and
             the turn prefix (turn start .. cut) with TURN_PREFIX_SUMMARIZATION_PROMPT (max 8,192
             = 0.5 x 16,384), as two requests, merged as pi merges them. With one stitched
             history the cut lands inside the last task, so pi makes two calls.
  request  = system SUMMARIZATION_SYSTEM_PROMPT + one user message "<conversation>\n" +
             serializeConversation(...) + "\n</conversation>\n\n" + prompt; tool results cut to
             their first 2,000 chars + "\n\n[... N more characters truncated]"; no tools.
             A "length" stop or a tool call is a failure (getSummarizationFailure).
  carry    = system + user(COMPACTION_SUMMARY_PREFIX + summary + SUFFIX) + the kept tail.
  file ops = pi's <read-files>/<modified-files> come from its own read/write/edit tool calls
             (`addFileOp` switches on those names); OpenHands' str_replace_editor/execute_bash
             map to none of them, so pi computes empty lists and appends nothing (as here).
  overflow = pi has no shrink step for the summary request: a request that cannot fit fails.
Inspect AI (0.3.280 3f9c2e3, `model/_compaction/summary.py`, CompactionSummary):
  request  = system + input (messages[1]) + the conversation from the last summary message on
             + user(DEFAULT_SUMMARY_PROMPT.format(addendums="")); no tools (`model.generate(
             input=...)`). No memory tool, no instructions -> empty addendums.
  overflow = `_fit_summarization_input`: middle-truncate the largest tool output to half its
             size (marker "\n\n...[tool output truncated for summarization]...\n\n"), up to 12
             passes, until it fits. Our fit test is the context contract (C.CALIB.fits with
             C.RESERVE["summary"]) instead of Inspect's window - min(max_tokens, window/2).
  summary  = `_summary_text`: drop the <analysis> scratchpad when "</analysis>" is directly
             followed by "<summary>" (the summary's own tags are kept, as Inspect keeps them).
  carry    = system + input + user("[CONTEXT COMPACTION SUMMARY]…{completion}…").
  Summary messages are recognised by that wrapper's first line (Inspect uses metadata).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

from p49 import contracts as C
from p49 import tokens

METHODS = ("codex", "pi", "inspect")
PROMPT_FILES = {
    "codex": ("codex_prompt.md", "codex_summary_prefix.md"),
    "pi": ("pi_system_prompt.txt", "pi_summarization_prompt.txt", "pi_turn_prefix_prompt.txt",
           "pi_wrapper.md"),
    "inspect": ("inspect_summary_prompt.txt", "inspect_resume_wrapper.txt"),
}
_PROMPTS: dict[str, str] = {}

CODEX_TOOL_OUTPUT_TOKENS = 12_000          # 10,000 x 1.2 serialisation allowance
PI_TURN_PREFIX_MAX_TOKENS = 8_192           # floor(0.5 x reserveTokens 16,384)
INSPECT_MAX_TRUNCATION_ITERATIONS = 12
INSPECT_TRUNCATION_MARKER = "\n\n...[tool output truncated for summarization]...\n\n"
INSPECT_SUMMARY_HEAD = "[CONTEXT COMPACTION SUMMARY]"


def prompt(name: str) -> str:
    if name not in _PROMPTS:
        _PROMPTS[name] = (C.PROMPTS_DIR / name).read_bytes().decode("utf-8")
    return _PROMPTS[name]


def prompt_sha256(method: str) -> str:
    """sha256 over "name=sha256(file)" lines of the method's prompt files, in PROMPT_FILES order."""
    lines = [f"{n}={hashlib.sha256(prompt(n).encode()).hexdigest()}"
             for n in PROMPT_FILES[method]]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def request_sha256(bodies: list[dict]) -> str:
    obj = bodies[0] if len(bodies) == 1 else bodies
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()


def inspect_wrap(completion: str) -> str:
    return prompt("inspect_resume_wrapper.txt").replace("{completion}", completion)


def pi_wrap(summary: str) -> str:
    return prompt("pi_wrapper.md").replace("{summary}", summary)


def codex_summary_message(summary: str) -> str:
    return prompt("codex_summary_prefix.md") + "\n" + summary


async def _count(messages, tools) -> int:
    return await asyncio.to_thread(tokens.count, messages, tools)


# --- Codex ------------------------------------------------------------------------------------
def _floor_boundary(b: bytes, i: int) -> int:
    while 0 < i < len(b) and (b[i] & 0xC0) == 0x80:
        i -= 1
    return i


def _ceil_boundary(b: bytes, i: int) -> int:
    while 0 < i < len(b) and (b[i] & 0xC0) == 0x80:
        i += 1
    return i


def codex_approx_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def codex_truncate_tokens(text: str, max_tokens: int) -> str:
    """codex_utils_string::truncate_middle_with_token_budget (plan_token_truncation)."""
    b = text.encode("utf-8")
    max_bytes = max_tokens * 4
    if len(b) <= max_bytes:
        return text
    left = max_bytes // 2
    right = max_bytes - left
    start, end = _floor_boundary(b, left), _ceil_boundary(b, len(b) - right)
    removed_tokens = (len(b) - max_bytes + 3) // 4
    marker = f"…{removed_tokens} tokens truncated…"
    return b[:start].decode("utf-8") + marker + b[end:].decode("utf-8")


def _is_codex_summary(m: dict) -> bool:
    return m["role"] == "user" and m["content"].startswith(prompt("codex_summary_prefix.md") + "\n")


def _drop_first_item(hist: list[dict]) -> list[dict]:
    """Codex remove_first_item: drop the oldest item and its call/output counterparts."""
    first, rest = hist[0], hist[1:]
    ids = {tc["id"] for tc in first.get("tool_calls") or []}
    return [m for m in rest if not (m["role"] == "tool" and m["tool_call_id"] in ids)]


def codex_kept_user_messages(messages: list[dict]) -> tuple[list[dict], dict]:
    users = [(i, m) for i, m in enumerate(messages) if m["role"] == "user"
             and not _is_codex_summary(m)]
    remaining = C.CODEX_KEEP_USER_TOKENS
    picked = []
    for i, m in reversed(users):
        if remaining == 0:
            break
        n = codex_approx_tokens(m["content"])
        if n <= remaining:
            picked.append((i, m, "verbatim"))
        else:
            picked.append((i, {"role": "user",
                               "content": codex_truncate_tokens(m["content"], remaining)},
                           "truncated"))
            break
        remaining -= n
    picked.reverse()
    info = {"user_messages": len(users), "kept": [i for i, _, _ in picked],
            "verbatim": [i for i, _, s in picked if s == "verbatim"],
            "truncated": [i for i, _, s in picked if s == "truncated"],
            "budget_left": max(remaining, 0)}
    return [m for _, m, _ in picked], info


async def _codex(messages, tools, summarize, res: C.CompactionResult):
    system, hist = messages[0], list(messages[1:])
    n_cut = 0
    for idx, m in enumerate(hist):
        if m["role"] == "tool":
            t = codex_truncate_tokens(m["content"], CODEX_TOOL_OUTPUT_TOKENS)
            if t != m["content"]:
                hist[idx] = {**m, "content": t}
                n_cut += 1
    res.truncation["tool_outputs_truncated"] = n_cut
    user_prompt = {"role": "user", "content": prompt("codex_prompt.md")}
    dropped = 0
    while True:
        req = [system, *hist, user_prompt]
        ours = await _count(req, None)
        if C.CALIB.fits(ours, C.RESERVE["summary"]) or len(hist) <= 1:
            break
        hist = _drop_first_item(hist)
        dropped += 1
    res.truncation["overflow_dropped_oldest"] = dropped
    max_tokens = C.SUMMARY_MAX_TOKENS["codex"]
    body = C.make_body(req, None, max_tokens)
    res.input_tokens, res.request_sha256 = ours, request_sha256([body])
    if not C.CALIB.fits(ours, C.RESERVE["summary"]):
        res.error = "context_overflow"
        return
    r = await summarize(req, max_tokens, None, ours)
    _record(res, [r])
    summary = r.message.get("content") or ""
    res.summary_text = summary
    if not summary.strip():
        res.error = "empty_summary"
    kept, info = codex_kept_user_messages(messages)
    task_idx = _task_indices(messages)
    info["task_messages_verbatim"] = [i for i in info["verbatim"] if i in task_idx]
    info["s1_task_verbatim"] = 1 in info["verbatim"]
    info["join_tasks_verbatim"] = [i for i in info["verbatim"] if i in task_idx and i != 1]
    res.kept = info
    res.carry_over = [system, *kept, {"role": "user", "content": codex_summary_message(summary)}]


# --- pi ---------------------------------------------------------------------------------------
def pi_estimate(m: dict) -> int:
    """pi estimateTokens (chars/4, rounded up)."""
    if m["role"] == "assistant":
        chars = len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(tc["function"]["name"]) + len(_js_args(tc["function"]["arguments"]))
        return -(-chars // 4)
    return -(-len(m.get("content") or "") // 4)


def _parse_args(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        v = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def _js(v) -> str:
    """JSON.stringify (no spaces, non-ASCII kept)."""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _js_args(arguments) -> str:
    return _js(_parse_args(arguments))


def pi_find_cut(entries: list[dict], keep_tokens: int) -> dict:
    """pi findCutPoint over `entries` (the session without its system prompt)."""
    cut_points = [i for i, m in enumerate(entries) if m["role"] in ("user", "assistant")]
    if not cut_points:
        return {"first_kept": 0, "turn_start": -1, "split": False}
    acc, cut = 0, cut_points[0]
    for i in range(len(entries) - 1, -1, -1):
        n = pi_estimate(entries[i])
        if n == 0:
            continue
        acc += n
        if acc >= keep_tokens:
            cut = next((c for c in cut_points if c >= i), cut_points[-1])
            break
    starts_turn = entries[cut]["role"] == "user"
    turn_start = -1
    if not starts_turn:
        turn_start = next((i for i in range(cut, -1, -1) if entries[i]["role"] == "user"), -1)
    return {"first_kept": cut, "turn_start": turn_start,
            "split": (not starts_turn) and turn_start != -1, "kept_estimate": acc}


def pi_truncate_result(text: str) -> str:
    n = C.PI_TOOL_RESULT_CHARS
    if len(text) <= n:
        return text
    return f"{text[:n]}\n\n[... {len(text) - n} more characters truncated]"


def pi_serialize(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        if m["role"] == "user":
            if m["content"]:
                parts.append(f"[User]: {m['content']}")
        elif m["role"] == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                args = _parse_args(tc["function"]["arguments"])
                calls.append(f"{tc['function']['name']}("
                             + ", ".join(f"{k}={_js(v)}" for k, v in args.items()) + ")")
            if m.get("content"):
                parts.append(f"[Assistant]: {m['content']}")
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif m["role"] == "tool" and m["content"]:
            parts.append(f"[Tool result]: {pi_truncate_result(m['content'])}")
    return "\n\n".join(parts)


def _pi_request(text: str) -> list[dict]:
    return [{"role": "system", "content": prompt("pi_system_prompt.txt")},
            {"role": "user", "content": text}]


async def _pi(messages, tools, summarize, res: C.CompactionResult):
    system, entries = messages[0], messages[1:]
    cut = pi_find_cut(entries, C.PI_KEEP_RECENT_TOKENS)
    history_end = cut["turn_start"] if cut["split"] else cut["first_kept"]
    to_summarize = entries[:history_end]
    prefix = entries[cut["turn_start"]:cut["first_kept"]] if cut["split"] else []
    calls = []                                   # (request, max_tokens, label)
    if to_summarize or not prefix:
        text = ("<conversation>\n" + pi_serialize(to_summarize) + "\n</conversation>\n\n"
                + prompt("pi_summarization_prompt.txt"))
        calls.append((_pi_request(text), C.SUMMARY_MAX_TOKENS["pi"], "history"))
    if prefix:
        text = ("# Conversation\n" + pi_serialize(prefix) + "\n\n# Instructions\n"
                + prompt("pi_turn_prefix_prompt.txt"))
        calls.append((_pi_request(text), PI_TURN_PREFIX_MAX_TOKENS, "turn_prefix"))
    n_cut = sum(1 for m in entries[:cut["first_kept"]] if m["role"] == "tool"
                and len(m["content"]) > C.PI_TOOL_RESULT_CHARS)
    res.truncation["tool_results_cut_2000"] = n_cut
    counts = [await _count(req, None) for req, _, _ in calls]
    bodies = [C.make_body(req, None, mt) for req, mt, _ in calls]
    res.input_tokens = sum(counts)
    res.request_sha256 = request_sha256(bodies)
    kept = entries[cut["first_kept"]:]
    res.kept = {"cut": cut, "kept_messages": len(kept), "first_kept_index": cut["first_kept"] + 1,
                "calls": [{"label": lab, "our_tokens": n, "max_tokens": mt}
                          for (_, mt, lab), n in zip(calls, counts)],
                "file_ops": "none (pi tracks only its read/write/edit tools)"}
    if not all(C.CALIB.fits(n, C.RESERVE["summary_pi"]) for n in counts):
        res.error = "context_overflow"
        return
    responses = []
    for (req, mt, _), n in zip(calls, counts):
        responses.append(await summarize(req, mt, None, n))
    _record(res, responses)
    texts = [r.message.get("content") or "" for r in responses]
    if prefix:
        history_text = texts[0] if to_summarize else "No prior history."
        summary = f"{history_text}\n\n---\n\n**Turn Context (split turn):**\n\n{texts[-1]}"
    else:
        summary = texts[0]
    res.summary_text = summary
    if any(r.finish_reason == "length" for r in responses):
        res.error = "summary_length_stop"            # pi: incomplete summary is a failure
    elif any(r.message.get("tool_calls") for r in responses):
        res.error = "summary_tool_call"
    elif not all(t.strip() for t in texts):
        res.error = "empty_summary"
    res.carry_over = [system, {"role": "user", "content": pi_wrap(summary)}, *kept]


# --- Inspect ----------------------------------------------------------------------------------
_ANALYSIS_BOUNDARY = re.compile(r"</analysis>\s*(?=<summary>)")


def inspect_summary_text(completion: str) -> str:
    """inspect_ai _summary_text (summary.py @3f9c2e3)."""
    open_at = completion.find("<analysis>")
    if open_at == -1:
        return completion
    summary_at = completion.find("<summary>")
    if summary_at != -1 and summary_at < open_at:
        return completion
    boundary = _ANALYSIS_BOUNDARY.search(completion, open_at)
    if boundary is None:
        return completion
    if "<summary>" in completion[open_at:boundary.start()]:
        return completion
    remainder = completion[:open_at] + completion[boundary.end():]
    return remainder.strip() or completion


def inspect_truncate_middle(text: str, max_bytes: int) -> str:
    budget = max_bytes - len(INSPECT_TRUNCATION_MARKER)
    if budget <= 0:
        return text
    enc = text.encode("utf-8", errors="replace")
    if len(enc) <= budget:
        return text
    front = enc[: budget // 2].decode("utf-8", errors="replace")
    back = enc[len(enc) - (budget - budget // 2):].decode("utf-8", errors="replace")
    return front + INSPECT_TRUNCATION_MARKER + back


def _is_inspect_summary(m: dict) -> bool:
    return m["role"] == "user" and m["content"].startswith(INSPECT_SUMMARY_HEAD)


async def _inspect(messages, tools, summarize, res: C.CompactionResult):
    system = [m for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    inp, conversation = rest[:1], rest[1:]
    start = 0
    for i, m in enumerate(conversation):
        if _is_inspect_summary(m):
            start = i
    user_prompt = {"role": "user",
                   "content": prompt("inspect_summary_prompt.txt").format(addendums="")}
    req = [*system, *inp, *conversation[start:], user_prompt]
    ours = await _count(req, None)
    passes = []
    for _ in range(INSPECT_MAX_TRUNCATION_ITERATIONS):
        if C.CALIB.fits(ours, C.RESERVE["summary"]):
            break
        cands = [(len(m["content"]), i) for i, m in enumerate(req) if m["role"] == "tool"
                 and len(m["content"]) // 2 > len(INSPECT_TRUNCATION_MARKER)]
        if not cands:
            break
        size, i = max(cands)
        req = list(req)
        req[i] = {**req[i], "content": inspect_truncate_middle(req[i]["content"], size // 2)}
        shrunk = await _count(req, None)
        passes.append({"message": i, "chars": size, "tokens_after": shrunk})
        if shrunk >= ours:
            break
        ours = shrunk
    res.truncation["fit_passes"] = passes
    max_tokens = C.SUMMARY_MAX_TOKENS["inspect"]
    body = C.make_body(req, None, max_tokens)
    res.input_tokens, res.request_sha256 = ours, request_sha256([body])
    res.kept = {"input": "messages[1] (S1 task)", "conversation_start": start + 2,
                "previous_summary": start > 0 or bool(conversation and
                                                      _is_inspect_summary(conversation[0]))}
    if not C.CALIB.fits(ours, C.RESERVE["summary"]):
        res.error = "context_overflow"
        return
    r = await summarize(req, max_tokens, None, ours)
    _record(res, [r])
    completion = inspect_summary_text(r.message.get("content") or "")
    res.summary_text = completion
    if not completion.strip():
        res.error = "empty_summary"
    res.carry_over = [*system, *inp, {"role": "user", "content": inspect_wrap(completion)}]


# --- common -----------------------------------------------------------------------------------
def _task_indices(messages: list[dict]) -> set[int]:
    return {1} | {i for i, m in enumerate(messages) if m["role"] == "user"
                  and m["content"].startswith(C.JOIN_PREFIX)}


def _record(res: C.CompactionResult, responses: list) -> None:
    res.cost_usd = sum(r.cost_usd or 0.0 for r in responses)
    toks = [r.completion_tokens for r in responses]
    res.summary_tokens = None if any(t is None for t in toks) else sum(toks)
    res.finish_reason = ",".join(str(r.finish_reason) for r in responses)


async def build(method: str, messages: list[dict], tools: list[dict], summarize, *,
                history_id: str, k: int = 1, source: str = "H") -> C.CompactionResult:
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    if method != "inspect" and k != 1:
        raise ValueError(f"{method}: only k = 1 is planned (repeated arms are Inspect)")
    if messages[0]["role"] != "system":
        raise ValueError("messages[0] must be the system prompt")
    res = C.CompactionResult(method=method, k=k, history_id=history_id, source=source,
                             input_tokens=0, prompt_sha256=prompt_sha256(method),
                             request_sha256="", summary_text="", summary_tokens=None,
                             carry_over=[], carry_tokens=0)
    fn = {"codex": _codex, "pi": _pi, "inspect": _inspect}[method]
    try:
        await fn(messages, tools, summarize, res)
    except C.ApiError as e:
        res.error = f"api_error: {e}"
    if res.carry_over:
        res.carry_tokens = await _count(res.carry_over, tools)
    return res
