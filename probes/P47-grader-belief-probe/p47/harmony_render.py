"""gpt-oss renders with the official `openai-harmony` renderer (the one vLLM-based servers use), in
the configuration that matched the pinned provider's per-turn prompt token counts best
(log.md 2026-10-14): system content with reasoning effort medium and today's conversation start
date, the X3 system prompt as developer instructions with X3's function tools, an empty "final"
message for the visible text of every tool-calling assistant turn, the analysis (reasoning), the
tool call as a commentary message to functions.<name> with `<|constrain|>json` and the
arguments as `json.dumps`, and raw tool outputs. Needs `openai-harmony` (uv run --with).
"""
from __future__ import annotations

import datetime
import json

from p47 import render as RD
from p47 import sources as S


def conversation(msgs: list[dict], date: str | None = None):
    from openai_harmony import (
        Author,
        Conversation,
        DeveloperContent,
        Message,
        ReasoningEffort,
        Role,
        SystemContent,
        ToolDescription,
    )

    date = date or datetime.datetime.now(datetime.UTC).date().isoformat()
    sysc = SystemContent.new().with_reasoning_effort(ReasoningEffort.MEDIUM) \
        .with_conversation_start_date(date)
    tools = [ToolDescription.new(t["function"]["name"], t["function"]["description"],
                                 parameters=t["function"]["parameters"]) for t in RD.TOOLS_JSON]
    dev = DeveloperContent.new().with_function_tools(tools)
    out = [Message.from_role_and_content(Role.SYSTEM, sysc)]
    for m in msgs:
        r = m["role"]
        if r == "system":
            dev = dev.with_instructions(S.visible_text_of(m))
            out.append(Message.from_role_and_content(Role.DEVELOPER, dev))
        elif r == "user":
            out.append(Message.from_role_and_content(Role.USER, S.visible_text_of(m)))
        elif r == "assistant":
            rs, tx, tcs = S.reasoning_of(m), S.visible_text_of(m), m.get("tool_calls") or []
            if tcs:
                out.append(Message.from_role_and_content(Role.ASSISTANT, tx).with_channel("final"))
            if rs:
                out.append(Message.from_role_and_content(Role.ASSISTANT, rs)
                           .with_channel("analysis"))
            for t in tcs:
                out.append(Message.from_role_and_content(
                    Role.ASSISTANT, json.dumps(t.get("arguments") or {}))
                    .with_channel("commentary").with_recipient("functions." + t["function"])
                    .with_content_type("<|constrain|>json"))
            if tx and not tcs:
                out.append(Message.from_role_and_content(Role.ASSISTANT, tx)
                           .with_channel("final"))
        elif r == "tool":
            out.append(Message.from_author_and_content(
                Author.new(Role.TOOL, "functions." + m["function"]), S.visible_text_of(m))
                .with_channel("commentary").with_recipient("assistant"))
    return Conversation.from_messages(out)


def render_ids(msgs: list[dict], date: str | None = None) -> list[int]:
    from openai_harmony import (
        HarmonyEncodingName,
        RenderConversationConfig,
        Role,
        load_harmony_encoding,
    )

    enc = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    return list(enc.render_conversation_for_completion(
        conversation(msgs, date), Role.ASSISTANT, RenderConversationConfig(
            auto_drop_analysis=False)))
