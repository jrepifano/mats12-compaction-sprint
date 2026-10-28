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
- antigravity: system + user(agy's CHECKPOINT: the last 10 user requests + the summary);
- opencode (OpenCode 1.19.3, `session/message-v2.ts` `filterCompacted` and the compaction user
  part's text, `session/compaction.ts` auto-continue): system + user("What did we do so far?") +
  assistant(the summary) + the kept tail (S1's `opencode_select`) + user(the auto-continue
  line). AGENTS.md lives in OpenCode's system prompt; here it is re-attached as in every policy.

Repeated compaction (pi only): `compact(..., previous_summary=, previous_summary_index=)` sends
pi's update prompt (prompts/pi_update/, pi-mono b06c1f2; SOURCES.md says which messages the
update call serialises): the message at `previous_summary_index` is left out of the summarizer's
input and the previous summary goes in `<previous-summary>` tags.

In every policy the root AGENTS.md is re-attached right after the system prompt, as Codex,
Claude Code and pi do (the instruction message is left out of the summarizer's input, since it
is re-attached anyway). `post_messages[summary_index]` is the message carrying the summary;
arms S+R and S+I append their block to it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from compaction import s1_replay as RP
from compaction.s2.model import ChatModel, ModelError, Usage

POLICIES = ("pi", "codex_local", "cc_prompt", "antigravity", "opencode")
INSTRUCTION_PREFIX = "# AGENTS.md instructions for /app"
CC_CONTINUATION = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n{summary}\n\n"
    "Please continue the conversation from where we left it off without asking the user any "
    "further questions. Continue with the last task that you were asked to work on.")
OPENCODE_COMPACTION_USER = "What did we do so far?"  # message-v2.ts:241 (compaction part)
OPENCODE_CONTINUE = ("Continue if you have next steps, or stop and ask for clarification if you "
                     "are unsure how to proceed.")  # compaction.ts:531 (auto-continue)
P49_PROMPTS = Path(RP.P49_PROMPTS)
PI_UPDATE_PROMPT_PATH = Path(__file__).parent / "prompts" / "pi_update" / \
    "pi_update_summarization_prompt.txt"


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


# A summary call that returns no text (its output cap spent on reasoning) is sent again, up to
# this many attempts in all, before the boundary fails. GLM's pi summary with mandatory thinking
# came back empty about 1 time in 4-5 at a 122k prefix and succeeded on resending (23 Oct).
EMPTY_SUMMARY_TRIES = 3


def pi_update_prompt() -> str:
    return PI_UPDATE_PROMPT_PATH.read_text(encoding="utf-8")


def pi_update_prompt_sha256() -> str:
    return RP._sha(pi_update_prompt())


def pi_update_request(rec: dict, previous_summary: str) -> RP.ReplayRequest:
    """pi's request for a repeated compaction (prompts/pi_update/SOURCES.md): the first
    compaction's request (`s1_replay.build_pi`) with its history call's prompt replaced by
    `<previous-summary>` + the update prompt. The turn-prefix call is unchanged."""
    req = RP.build_pi(rec)
    first = RP.prompts_for("pi").texts["pi_summarization_prompt.txt"]
    calls = []
    for c in req.calls:
        if c.label == "history":
            if not c.user.endswith(first):
                raise ValueError("pi history call does not end with the summarization prompt")
            user = (c.user[: -len(first)] + f"<previous-summary>\n{previous_summary}\n"
                    f"</previous-summary>\n\n" + pi_update_prompt())
            c = RP.Call(c.label, c.system, user, c.max_output_tokens)
        calls.append(c)
    return dataclasses.replace(
        req, system=calls[0].system, user=calls[0].user, label=calls[0].label,
        max_output_tokens=calls[0].max_output_tokens, extra_calls=tuple(calls[1:]),
        info={**req.info, "update": True, "update_prompt_sha256": pi_update_prompt_sha256()})


def _finish_summary(policy: str, req: RP.ReplayRequest, texts: dict[str, str],
                    previous_summary: str | None) -> str:
    if previous_summary is not None and req.info["split"] and not req.info["has_history"]:
        # pi with nothing before the split turn: the history text is the previous summary
        return (f"{previous_summary}\n\n---\n\n**Turn Context (split turn):**\n\n"
                f"{texts['turn_prefix']}")
    return RP.finish_summary(policy, req, texts)


def post_context(policy: str, messages: list[dict], summary: str, kept_indices: list[int],
                 instruction_text: str | None, *,
                 kept_texts: list[str] | None = None) -> tuple[list[dict], int]:
    """The post-compaction context of `policy` (module docstring) and its summary index:
    messages[0] (the system prompt), the re-attached instruction (once), the policy's summary
    message and its carry-over. `kept_indices` index `messages`; `kept_texts` are the kept
    messages' texts as the policy keeps them (Codex middle-truncates the message that crosses
    its budget), by default those messages' own text. Used by `compact` and by the offline
    replays (`compaction.s2.replays`), so both build the same structure."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    if kept_texts is None:
        kept_texts = [_text(messages[i].get("content")) for i in kept_indices]
    if len(kept_texts) != len(kept_indices):
        raise ValueError("kept_texts and kept_indices differ in length")
    if any(is_instruction(messages[i]) for i in kept_indices):
        raise ValueError("a kept message is the instruction message (it is re-attached once)")
    head = [messages[0]]
    if instruction_text:
        head.append(instruction_message(instruction_text))
    if policy == "pi":
        body = [{"role": "user", "content": _summary_message(policy, summary, [])}]
        tail = [messages[i] for i in kept_indices]
        return head + body + tail, len(head)
    if policy == "codex_local":
        users = [{"role": "user", "content": t} for t in kept_texts]
        body = [{"role": "user", "content": _summary_message(policy, summary, [])}]
        post = head + users + body
        return post, len(post) - 1
    if policy == "antigravity":
        body = [{"role": "user", "content": _summary_message(policy, summary, kept_texts)}]
        return head + body, len(head)
    if policy == "opencode":
        body = [{"role": "user", "content": OPENCODE_COMPACTION_USER},
                {"role": "assistant", "content": summary}]
        tail = [messages[i] for i in kept_indices]
        return (head + body + tail + [{"role": "user", "content": OPENCODE_CONTINUE}],
                len(head) + 1)
    body = [{"role": "user", "content": _summary_message(policy, summary, [])}]  # cc_prompt
    return head + body, len(head)


def compact(policy: str, messages: list[dict], model: ChatModel, *, run_id: str,
            instruction_text: str | None, max_output_cap: int | None = None,
            previous_summary: str | None = None,
            previous_summary_index: int | None = None,
            custom_instructions: str | None = None) -> Compaction:
    """Summarize `messages` (the full pre-boundary history, messages[0] = system) with the
    policy's replayed request(s) on `model`, and build the post-compaction context.

    `custom_instructions` (pi only) is pi's `/compact <instructions>`: appended to the history
    summary's prompt as "\n\nAdditional focus: <instructions>", as pi-mono b06c1f2's
    generateSummaryWithUsage does; the turn-prefix call doesn't get it (week 2, experiment 5).

    `previous_summary` (pi only) makes it a repeated compaction with pi's update prompt; the
    message at `previous_summary_index` (the one carrying that summary) is left out of the
    summarizer's input, as pi serialises only what follows its previous compaction entry."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    if (previous_summary is None) != (previous_summary_index is None):
        raise ValueError("previous_summary and previous_summary_index go together")
    if previous_summary is not None and policy != "pi":
        raise ValueError(f"no update prompt for policy {policy!r} (pi only)")
    if custom_instructions and policy != "pi":
        raise ValueError(f"no custom instructions for policy {policy!r} (pi only)")
    rmsgs = to_record_messages(messages)
    if previous_summary_index is not None:
        if not 0 < previous_summary_index < len(messages) or \
                messages[previous_summary_index].get("role") not in ("user", "assistant"):
            raise ValueError(f"previous_summary_index {previous_summary_index} is not a "
                             "user or assistant message")
        rmsgs = [m for m in rmsgs if m["_i"] != previous_summary_index]
    rec = {"id": f"deepswe:{run_id}:0", "source": "deepswe", "session_id": run_id,
           "source_messages": rmsgs, "user_turns": [], "summary": "", "post_messages": []}
    req = (RP.ARM_BUILDERS[policy](rec) if previous_summary is None
           else pi_update_request(rec, previous_summary))
    texts: dict[str, str] = {}
    usage: list[dict] = []
    requests = []
    for c in req.calls:
        if custom_instructions and c.label == "history":  # pi-mono: a truthiness check
            c = replace(c, user=f"{c.user}\n\nAdditional focus: {custom_instructions}")
        mt = min(c.max_output_tokens, max_output_cap or c.max_output_tokens)
        requests.append({"label": c.label, "system": c.system, "user": c.user,
                         "max_output_tokens": mt})
        for attempt in range(1, EMPTY_SUMMARY_TRIES + 1):
            try:
                rep = model.complete([{"role": "system", "content": c.system},
                                      {"role": "user", "content": c.user}], None, mt,
                                     purpose=f"summary:{policy}:{c.label}")
            except ModelError as e:
                e.cost_usd += sum(u["cost_usd"] or 0.0 for u in usage)  # the earlier calls too
                e.usage, e.requests = usage, requests  # each attempt so far, for the record
                raise
            texts[c.label] = _text(rep.message.get("content"))
            u: Usage = rep.usage
            usage.append({"label": c.label, **asdict(u), "cost_usd": rep.cost_total_usd,
                          "finish_reason": rep.finish_reason, "attempt": attempt})
            if texts[c.label].strip():
                break
    summary = _finish_summary(policy, req, texts, previous_summary)
    empty = [k for k, t in texts.items() if not t.strip()]
    if empty or not summary.strip():
        # A compaction with no summary isn't a boundary (pilot order 3, 23 Oct: the summarizer
        # spent its output cap on reasoning): fail it, charged, so the run restarts.
        # Each call is checked: pi's split-turn merge adds headings, so its merged text is
        # never empty even when a part is (review r1).
        err = ModelError(f"empty summary from {policy} (empty: {empty}; finish: "
                         f"{[u['finish_reason'] for u in usage]})",
                         sum(u["cost_usd"] or 0.0 for u in usage))
        err.usage, err.requests = usage, requests  # each attempt's tokens and finish reason
        raise err

    kept = list(req.kept_messages)
    kept_idx = [m["_i"] for m in kept if "_i" in m]
    if len(kept_idx) != len(kept):
        raise ValueError(f"{policy}: a kept message has no index into the history")
    post, sidx = post_context(policy, messages, summary, kept_idx, instruction_text,
                              kept_texts=[m["text"] for m in kept])
    info = {k: v for k, v in req.info.items() if k not in ("cut",)} | {
        # per label, the attempt that was used (empty attempts that were resent don't count)
        "truncated": [lb for lb in texts if [u for u in usage if u["label"] == lb][-1]
                      ["finish_reason"] == "length"],
        "empty_resends": sum(u.get("attempt", 1) > 1 for u in usage),
        "prompt_sha256": req.prompt_sha256,
        # the requests as sent: with custom instructions they differ from the policy's own
        "request_sha256": (hashlib.sha256(json.dumps(requests, sort_keys=True).encode())
                           .hexdigest() if custom_instructions else req.request_sha256),
        "policy_version": RP.policy_version(policy) if policy in RP.REPLAY_ARMS else None,
        "update": previous_summary is not None,
        "update_prompt_sha256": pi_update_prompt_sha256() if previous_summary is not None
        else None,
        "previous_summary_sha256": RP._sha(previous_summary) if previous_summary is not None
        else None,
        "previous_summary_index": previous_summary_index,
        "instruction_text": instruction_text, "custom_instructions": custom_instructions}
    return Compaction(policy, requests, texts, summary, kept_idx, post, sidx, usage, info)


def with_block(post: list[dict], summary_index: int, block: str) -> list[dict]:
    """A copy of the post-compaction context with `block` appended to the summary message."""
    out = [dict(m) for m in post]
    m = out[summary_index]
    m["content"] = _text(m.get("content")).rstrip("\n") + "\n\n" + block
    return out
