"""P38 tool-calling loop (plan.md, Setup): prefix generation (phases A-C with the full history)
and the resumed decision phase D.

Messages are OpenAI chat messages. Assistant messages carry `reasoning_details` (passed back to
OpenRouter on later turns) and our private `_reasoning` (visible reasoning text, kept for the
sim/test-talk measure, stripped before sending by `api.clean_messages`).
"""
from __future__ import annotations

from p38 import api
from p38 import contracts as C
from p38 import worlds as W

NUDGE = "Continue with the runbook."


def assistant_message(r: api.Reply) -> dict:
    m: dict = {"role": "assistant", "content": r.text or None}
    if r.tool_calls:
        m["tool_calls"] = [{"id": t["id"], "type": "function",
                            "function": {"name": t["name"], "arguments": t["arguments"]}}
                           for t in r.tool_calls]
    elif m["content"] is None:
        m["content"] = ""
    if r.reasoning_details:
        m["reasoning_details"] = r.reasoning_details
    m["_reasoning"] = r.reasoning or ""
    return m


def _usage(r: api.Reply) -> dict:
    u = r.usage or {}
    return {"in": u.get("prompt_tokens") or 0, "out": u.get("completion_tokens") or 0,
            "usd": api.charge(r), "provider": r.provider, "finish": r.finish_reason,
            "error": r.error}


async def run_prefix(model: C.Model, world: W.World, chat=api.chat, on_reply=None) -> dict:
    """Generate phases A-C. Returns the context record: status "ok" with `messages` and the
    phase ends (`bounds`: index one past the last message of each phase), or "dropped" with the
    reason. `on_reply(reply)` sees every API reply (spend accounting)."""
    world.reset()
    msgs = [{"role": "system", "content": world.system_prompt()},
            {"role": "user", "content": world.task_message()}]
    bounds: dict[str, int] = {}
    calls: list[dict] = []
    nudges, turns_in_phase = 0, 0
    status, reason = "dropped", ""
    while True:
        ph = world.state["phase"]
        if turns_in_phase >= C.PHASE_TURN_CAP:
            reason = f"phase {ph} not finished in {C.PHASE_TURN_CAP} turns"
            break
        r = await chat(model, msgs, max_tokens=C.AGENT_MAX_TOKENS, tools=world.tools())
        if on_reply:
            on_reply(r)
        calls.append(_usage(r))
        if r.error:
            status, reason = "error", r.error
            break
        turns_in_phase += 1
        msgs.append(assistant_message(r))
        if not r.tool_calls:
            if nudges >= C.MAX_NUDGES:
                reason = "no tool call after the nudges"
                break
            nudges += 1
            msgs.append({"role": "user", "content": NUDGE})
            continue
        for t in r.tool_calls:
            out = world.call(t["name"], t["arguments"])
            msgs.append({"role": "tool", "tool_call_id": t["id"], "content": out})
        if ph == "C" and world.phase_done("C"):
            bounds["C"] = len(msgs)
            status = "ok"
            break
        before = world.state["phase"]
        world.advance()
        if world.state["phase"] != before:
            bounds[before] = len(msgs)
            turns_in_phase = 0
    return {"ctx_id": world.ctx_id, "model": model.slug, "workflow": world.workflow,
            "cue": world.cue, "variant": world.variant, "status": status, "reason": reason,
            "messages": msgs, "bounds": bounds, "nudges": nudges,
            "world_calls": world.state["calls"], "api_calls": calls,
            "usd": round(sum(c["usd"] for c in calls), 6)}


async def run_decision(model: C.Model, world: W.World, messages: list[dict], chat=api.chat,
                       on_reply=None) -> dict:
    """Phase D from a resumed context. Ends on the irreversible call, `submit_report`, a reply
    without a tool call, an API error, or RESUME_TURN_CAP turns."""
    world.reset()
    world.enter_decision()
    msgs = list(messages)
    start = len(msgs)
    calls: list[dict] = []
    end = "turn_cap"
    for _ in range(C.RESUME_TURN_CAP):
        r = await chat(model, msgs, max_tokens=C.AGENT_MAX_TOKENS, tools=world.tools())
        if on_reply:
            on_reply(r)
        calls.append(_usage(r))
        if r.error:
            end = "error"
            break
        msgs.append(assistant_message(r))
        if not r.tool_calls:
            end = "text"
            break
        for t in r.tool_calls:
            out = world.call(t["name"], t["arguments"])
            msgs.append({"role": "tool", "tool_call_id": t["id"], "content": out})
            if world.state["irreversible"] is not None or world.state["report"] is not None:
                break
        if world.state["irreversible"] is not None:
            end = "irreversible"
            break
        if world.state["report"] is not None:
            end = "report"
            break
    return {"end": end, "turns": len(calls), "messages": msgs[start:],
            "world_calls": world.state["calls"], "approvals": world.state["approvals"],
            "check_failed": world.state["check_failed"],
            "irreversible": world.state["irreversible"], "report": world.state["report"],
            "api_calls": calls, "usd": round(sum(c["usd"] for c in calls), 6)}


async def run_continue(model: C.Model, world: W.World, messages: list[dict], chat=api.chat,
                       on_reply=None) -> dict:
    """Follow-up 3: resume at phase B and let the agent run B, C and D in one conversation
    (phase-gated tools as in the prefix; D starts once C is complete). Ends as run_decision,
    or at CONTINUE_TURN_CAP turns."""
    world.reset()
    world.state["phase"] = "B"
    msgs = list(messages)
    start = len(msgs)
    calls: list[dict] = []
    end = "turn_cap"
    for _ in range(C.CONTINUE_TURN_CAP):
        r = await chat(model, msgs, max_tokens=C.AGENT_MAX_TOKENS, tools=world.tools())
        if on_reply:
            on_reply(r)
        calls.append(_usage(r))
        if r.error:
            end = "error"
            break
        msgs.append(assistant_message(r))
        if not r.tool_calls:
            end = "text"
            break
        for t in r.tool_calls:
            out = world.call(t["name"], t["arguments"])
            msgs.append({"role": "tool", "tool_call_id": t["id"], "content": out})
            if world.state["irreversible"] is not None or world.state["report"] is not None:
                break
        if world.state["irreversible"] is not None:
            end = "irreversible"
            break
        if world.state["report"] is not None:
            end = "report"
            break
        ph = world.state["phase"]
        if ph == "C" and world.phase_done("C"):
            world.enter_decision()
        elif ph == "B":
            world.advance()
    return {"end": end, "turns": len(calls), "messages": msgs[start:],
            "phase_end": world.state["phase"], "world_calls": world.state["calls"],
            "approvals": world.state["approvals"], "check_failed": world.state["check_failed"],
            "irreversible": world.state["irreversible"], "report": world.state["report"],
            "api_calls": calls, "usd": round(sum(c["usd"] for c in calls), 6)}
