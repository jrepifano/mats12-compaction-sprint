"""Each model's paired vendor summarizer, replayed inside our loop (spec "Compaction").

Pairing (sprint plan, "Harness x model pairing"; the coding model writes its own summary in
every harness): DeepSeek V4.1 Flash and GLM 5.3 Flash -> pi; GPT-6 luna -> Codex local
summarizer; Haiku 5.5 -> Claude Code's prompt. Antigravity is supported for a Gemini subset.

Requests and keep rules come from `compaction.s1_replay` (the S1 Part B ports: pi-mono b06c1f2,
Codex ac9db7d local compact, Claude Code 2.1.304's captured compaction request, agy 1.4.3), so S1
and S2 replay the same policies. The carry-over (what the model sees after compaction) follows
each harness:

- pi: system + user(pi's wrapper around the summary) + the kept tail (the last ~20k tokens of
  messages, verbatim, cut at a user or assistant message);
- codex_local: system + the kept user requests (newest first within 20,000 tokens, the crossing
  one middle-truncated) + user(Codex's summary prefix + summary);
- cc_prompt: system + user(Claude Code's continuation text around the summary); Claude Code's
  own verbatim tail and its transcript pointer are not reproduced in a replay;
- antigravity: system + user(agy's CHECKPOINT: the last 10 user requests + the summary).

In every policy the root AGENTS.md is re-attached right after the system prompt, as Codex,
Claude Code and pi do (the instruction message is left out of the summarizer's input, since it
is re-attached anyway). `post_messages[summary_index]` is the message carrying the summary;
arms S+R and S+I append their block to it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from compaction import s1_replay as RP
from compaction.s2.model import ChatModel, Usage

POLICIES = ("pi", "codex_local", "cc_prompt", "antigravity")
INSTRUCTION_PREFIX = "# AGENTS.md instructions for /app"
CC_CONTINUATION = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n{summary}\n\n"
    "Please continue the conversation from where we left it off without asking the user any "
    "further questions. Continue with the last task that you were asked to work on.")
P49_PROMPTS = Path(RP.P49_PROMPTS)


def instruction_message(text: str) -> dict:
    """The re-attached root AGENTS.md, in Codex's wrapper."""
    return {"role": "user",
            "content": f"{INSTRUCTION_PREFIX}\n\n<INSTRUCTIONS>\n{text.rstrip()}\n</INSTRUCTIONS>"}


def is_instruction(m: dict) -> bool:
    return m.get("role") == "user" and str(m.get("content") or "").startswith(INSTRUCTION_PREFIX)


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def to_record_messages(messages: list[dict]) -> list[dict]:
    """OpenAI-format history -> compaction.schema Messages, each tagged with `_i`, its index in
    `messages`. The system prompt and the instruction message are left out."""
    out = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system" or is_instruction(m):
            continue
        rec: dict = {"role": role, "text": _text(m.get("content")), "_i": i}
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = fn.get("arguments")
                calls.append({"name": fn.get("name"), "args": args, "id": tc.get("id")})
            rec["tool_calls"] = calls
        if role == "tool":
            rec["tool_use_id"] = m.get("tool_call_id")
        out.append(rec)
    return out


def boundary_record(run_id: str, messages: list[dict], *, model: str, pre_tokens: int,
                    summary: str = "", rules: list[dict] | None = None) -> dict:
    """A compaction.schema CompactionRecord (source "deepswe") for the boundary, the shape S1's
    judges and the retention judge read."""
    src = [{k: v for k, v in m.items() if k != "_i"} for m in to_record_messages(messages)]
    return {"id": f"deepswe:{run_id}:0", "source": "deepswe", "session_id": run_id,
            "harness": "s2-loop", "model": model, "trigger": "auto", "pre_tokens": pre_tokens,
            "timestamp": None, "source_messages": src, "summary": summary, "post_messages": [],
            "user_turns": [m["text"] for m in src if m["role"] == "user"],
            "rules": rules or [], "license": "apache-2.0 (DeepSWE); our runs", "revision": None,
            "scrubbed": True}


@dataclass
class Compaction:
    policy: str
    requests: list[dict]
    responses: dict[str, str]
    summary: str
    kept_indices: list[int]
    post_messages: list[dict]
    summary_index: int
    usage: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return sum(u.get("cost_usd", 0.0) for u in self.usage)

    def to_dict(self) -> dict:
        return asdict(self)


def _p49(name: str) -> str:
    return (P49_PROMPTS / name).read_text(encoding="utf-8")


def _summary_message(policy: str, summary: str, kept_user_texts: list[str]) -> str:
    if policy == "pi":
        return _p49("pi_wrapper.md").rstrip("\n").replace("{summary}", summary)
    if policy == "codex_local":
        return _p49("codex_summary_prefix.md").rstrip("\n") + "\n" + summary
    if policy == "cc_prompt":
        return CC_CONTINUATION.format(summary=summary)
    if policy == "antigravity":
        capped = len(kept_user_texts) >= RP.AGY_MAX_REQUESTS
        return RP.agy_checkpoint(kept_user_texts, summary, capped=capped)
    raise ValueError(f"unknown policy {policy!r}")


def compact(policy: str, messages: list[dict], model: ChatModel, *, run_id: str,
            instruction_text: str | None, max_output_cap: int | None = None) -> Compaction:
    """Summarize `messages` (the full pre-boundary history, messages[0] = system) with the
    policy's replayed request(s) on `model`, and build the post-compaction context."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    rmsgs = to_record_messages(messages)
    rec = {"id": f"deepswe:{run_id}:0", "source": "deepswe", "session_id": run_id,
           "source_messages": rmsgs, "user_turns": [], "summary": "", "post_messages": []}
    req = RP.ARM_BUILDERS[policy](rec)
    texts: dict[str, str] = {}
    usage: list[dict] = []
    requests = []
    for c in req.calls:
        mt = min(c.max_output_tokens, max_output_cap or c.max_output_tokens)
        requests.append({"label": c.label, "system": c.system, "user": c.user,
                         "max_output_tokens": mt})
        rep = model.complete([{"role": "system", "content": c.system},
                              {"role": "user", "content": c.user}], None, mt,
                             purpose=f"summary:{policy}:{c.label}")
        texts[c.label] = _text(rep.message.get("content"))
        u: Usage = rep.usage
        usage.append({"label": c.label, **asdict(u), "finish_reason": rep.finish_reason})
    summary = RP.finish_summary(policy, req, texts)

    kept = list(req.kept_messages)
    kept_idx = [m["_i"] for m in kept if "_i" in m]
    head = [messages[0]]
    if instruction_text:
        head.append(instruction_message(instruction_text))
    if policy == "pi":
        body = [{"role": "user", "content": _summary_message(policy, summary, [])}]
        tail = [messages[i] for i in kept_idx]
        post, sidx = head + body + tail, len(head)
    elif policy == "codex_local":
        users = [{"role": "user", "content": m["text"]} for m in kept]
        body = [{"role": "user", "content": _summary_message(policy, summary, [])}]
        post = head + users + body
        sidx = len(post) - 1
    elif policy == "antigravity":
        body = [{"role": "user",
                 "content": _summary_message(policy, summary, [m["text"] for m in kept])}]
        post, sidx = head + body, len(head)
    else:  # cc_prompt
        body = [{"role": "user", "content": _summary_message(policy, summary, [])}]
        post, sidx = head + body, len(head)
    info = {k: v for k, v in req.info.items() if k not in ("cut",)} | {
        "prompt_sha256": req.prompt_sha256, "request_sha256": req.request_sha256,
        "policy_version": RP.policy_version(policy) if policy in RP.REPLAY_ARMS else None}
    return Compaction(policy, requests, texts, summary, kept_idx, post, sidx, usage, info)


def with_block(post: list[dict], summary_index: int, block: str) -> list[dict]:
    """A copy of the post-compaction context with `block` appended to the summary message."""
    out = [dict(m) for m in post]
    m = out[summary_index]
    m["content"] = _text(m.get("content")).rstrip("\n") + "\n\n" + block
    return out
