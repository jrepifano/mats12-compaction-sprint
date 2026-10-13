"""P41 continuations: X3's agent loop from a restored site (plan "Setup").

The loop is x3.task.episode's (as P23's `p23_episode`): a byte-bounded cost check before every
call at GLM's ceiling prices against the remaining budget, one command per turn, X3's nudge
for a turn without a tool call, `done` ends, at most `remaining_turns` turns. Model calls go
through Inspect's `Model.generate` with X3's settings (so the request, reasoning replay included,
is what X3 sent), outside an eval; commands run in short-lived containers on the episode's own
volumes (`sandbox.py`). Billed cost is OpenRouter's `usage.cost` from the call record Inspect
keeps in the transcript (ceiling price when missing).

Also here: `fork`, one next turn from a prefix with no command executed (decision-point forks,
the canary).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
    get_model,
)
from inspect_ai.tool import ToolCall

from p41 import contracts as C
from p41 import sandbox as SB
from x3 import contracts as X3C
from x3 import texts as X3T
from x3.task import (
    ATTEMPT_TIMEOUT_S,
    ATTEMPTS,
    RETRY_BACKOFF_S,
    TOOLS,
    affordable_max_tokens,
    call_bound,
    call_cost,
    deliver,
    input_bound_tokens,
    tighter_bound,
    usage_tokens,
)

SOLUTION_PATH = f"{X3C.WORKDIR}/solution.py"
TEST_PATH = f"{X3C.WORKDIR}/test_solution.py"
END_REPLAY_MISMATCH = "replay_mismatch"
END_SANDBOX_ERROR = "sandbox_error"
END_SITE = "site"                 # P41 prefixes: stopped after the site turn (prefixes.py)
MAX_CONNECTIONS = 64

_MSG_CLASS = {"system": ChatMessageSystem, "user": ChatMessageUser,
              "assistant": ChatMessageAssistant, "tool": ChatMessageTool}


def to_messages(dumps: list[dict]) -> list[ChatMessage]:
    return [_MSG_CLASS[d["role"]].model_validate(d) for d in dumps]


_models: dict[str, object] = {}


def model_for(m: C.Model):
    """One Inspect Model per OpenRouter model, with X3b's routing (provider pinned, no retries
    inside Inspect or the SDK: retries are the loop's, as X3)."""
    if m.slug not in _models:
        from p41.api import load_key

        load_key()                       # Inspect reads OPENROUTER_API_KEY from the environment
        _models[m.slug] = get_model(f"openrouter/{m.model_id}", provider=m.provider,
                                    max_retries=0)
    return _models[m.slug]


def generate_config(m: C.Model, max_tokens: int) -> GenerateConfig:
    """x3.task._generate's config plus the eval-level settings X3b passed (X3's
    COMMON_GENERATE + the model's own), and a connection limit for 32+ episodes in flight."""
    extra = {k: v for k, v in {**X3C.COMMON_GENERATE, **m.generate}.items()
             if k in GenerateConfig.model_fields
             and k not in ("max_tokens", "parallel_tool_calls")}
    return GenerateConfig(**extra, max_tokens=max_tokens, parallel_tool_calls=False,
                          max_retries=0, attempt_timeout=ATTEMPT_TIMEOUT_S,
                          max_connections=MAX_CONNECTIONS)


def _billed_from_transcript() -> tuple[float | None, dict]:
    from inspect_ai.event import ModelEvent
    from inspect_ai.log._transcript import transcript

    evs = [e for e in transcript().events if isinstance(e, ModelEvent)]
    if not evs or evs[-1].call is None:
        return None, {}
    resp = evs[-1].call.response or {}
    u = resp.get("usage") if isinstance(resp, dict) else None
    cost = u.get("cost") if isinstance(u, dict) else None
    return (float(cost) if isinstance(cost, int | float) else None,
            {"provider": resp.get("provider"), "response_id": resp.get("id")})


async def generate_once(m: C.Model, messages: list[ChatMessage], max_tokens: int,
                        tools=TOOLS) -> tuple[ModelOutput, float | None, dict]:
    """One request; (output, billed usd or None, {provider, response_id})."""
    from inspect_ai.log._transcript import Transcript, init_transcript

    init_transcript(Transcript())          # per call: the last ModelEvent is this call's
    out = await model_for(m).generate(messages, tools=tools,
                                      config=generate_config(m, max_tokens))
    billed, info = _billed_from_transcript()
    return out, billed, info


async def generate_with_retries(m: C.Model, messages, max_tokens: int, attempt_bound: float,
                                tools=TOOLS, sleep=asyncio.sleep):
    """x3.task.generate_with_retries with an explicit model: up to ATTEMPTS attempts, each
    failed one charged `attempt_bound`. Returns (output | None, billed, info, extra, error)."""
    from inspect_ai._util.error import PrerequisiteError

    extra, err = 0.0, ""
    for attempt in range(ATTEMPTS):
        try:
            out, billed, info = await generate_once(m, messages, max_tokens, tools)
            return out, billed, info, extra, err
        except (PrerequisiteError, TypeError, ValueError):
            # raised before any request is sent (no key, bad config): nothing billed; the
            # caller sees a crash, not a charged API error (canary 2026-10-13, log.md)
            raise
        except Exception as e:  # noqa: BLE001 - any API failure
            extra += attempt_bound
            err = repr(e)[:2000]
            if attempt < ATTEMPTS - 1:
                await sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
    return None, None, {}, extra, err


# --- the per-continuation spec --------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    episode_id: str
    site_id: str
    task_id: str
    cond: str
    para: int
    repeat: int
    sentence: str | None
    mode: str                         # inject mode ("reasoning" unless a follow-up says)
    remaining_turns: int
    remaining_usd: float
    tar_path: str                     # the site state
    site_sha256: dict = field(default_factory=dict)    # {path: sha} the restore must match
    initial_messages: list = field(default_factory=list)
    # X3's loop state at the cut: (input tokens, output tokens, len(messages) after the reply) of
    # the source's site-turn call, so the cost bound is X3's (smoke 2026-10-13, log.md)
    prev_usage: tuple | None = None


def _clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def run_continuation(spec: Spec, m: C.Model = C.GLM, stop_rule=None,
                           tar_out: Path | None = None) -> dict:
    """Restore the site, run the loop, collect; returns the episode artifact (volumes removed
    in every case).

    P41 prefixes (`prefixes.py`) pass `stop_rule(new_message_dumps) -> bool`: it is checked
    after each turn whose first call is execute_command has its tool result, and True ends the
    loop with END_SITE; the volumes are then exported to `tar_out` before removal."""
    t0 = time.time()
    calls: list[dict] = []
    snapshots: list[dict] = []
    model_calls: list[dict] = []
    bounds: list[dict] = []
    messages: list[ChatMessage] = to_messages(spec.initial_messages)
    n_init = len(messages)
    spent, turns, nudges, multi, bg_killed = 0.0, 0, 0, 0, 0
    prev: tuple[int, int, int] | None = tuple(spec.prev_usage) if spec.prev_usage else None
    end_reason: str | None = None
    err: str | None = None
    setup: dict = {}
    final = {SOLUTION_PATH: None, TEST_PATH: None}
    mounts: dict[str, str] = {}
    try:
        mounts = await asyncio.to_thread(SB.create_volumes, spec.episode_id)
        tar = Path(spec.tar_path).read_bytes()
        snap0 = await asyncio.to_thread(SB.restore, mounts, tar, 0)
        snapshots.append(snap0)
        mism = SB.compare(SB.snapshot_shas(snap0), spec.site_sha256) \
            if snap0.get("files") is not None else ["snapshot 0 failed"]
        setup = {"replay_match": not mism, "mismatched": mism[:20],
                 "tar_sha256": hashlib.sha256(tar).hexdigest()}
        if mism:
            end_reason = END_REPLAY_MISMATCH
        while end_reason is None:
            if turns >= spec.remaining_turns:
                end_reason = X3C.EndReason.TURN_CAP.value
                break
            ib = tighter_bound(input_bound_tokens(messages, TOOLS), prev, messages)
            mt = affordable_max_tokens(spent, spec.remaining_usd, ib, m.in_ceiling,
                                       m.out_ceiling, X3C.MAX_TOKENS_PER_TURN)
            bounds.append({"turn": turns + 1, "input_bound": ib, "max_tokens": mt,
                           "spent": spent})
            if mt < X3C.MIN_CALL_MAX_TOKENS:
                end_reason = X3C.EndReason.COST_CAP.value
                break
            one = call_bound(ib, mt, m.in_ceiling, m.out_ceiling) / ATTEMPTS
            out, billed, info, extra, gerr = await generate_with_retries(m, messages, mt, one)
            spent += extra
            bounds[-1]["failed_charge"] = extra
            if out is None:
                end_reason = X3C.EndReason.API_ERROR.value
                err = gerr
                break
            turns += 1
            ceil = call_cost(out, m.in_ceiling, m.out_ceiling, one)
            spent += ceil
            ut = usage_tokens(out)
            model_calls.append({"turn": turns, "billed_usd": billed, "ceiling_usd": ceil,
                                "failed_charge": extra, "input_tokens": ut[0] if ut else None,
                                "output_tokens": ut[1] if ut else None,
                                "stop_reason": out.stop_reason, **info})
            msg = out.message
            messages.append(msg)
            prev = (ut[0], ut[1], len(messages)) if ut else None
            if out.stop_reason in ("model_length", "content_filter"):
                end_reason = out.stop_reason
                break
            tcs: list[ToolCall] = list(msg.tool_calls or [])
            if not tcs:
                nudges += 1
                messages.append(ChatMessageUser(content=X3T.NUDGE))
                continue
            first, rest = tcs[0], tcs[1:]
            if first.function == "done":
                result = ""
                end_reason = X3C.EndReason.DONE.value
            elif first.function == "execute_command":
                cmd = first.arguments.get("command")
                if not isinstance(cmd, str):
                    result = "error: execute_command needs a string argument `command`"
                else:
                    ts = _clock()
                    r = await asyncio.to_thread(SB.exec_command, mounts, cmd, len(snapshots))
                    snapshots.append(r["snapshot"])
                    bg_killed += r["bg_killed"]
                    result, truncated = deliver(r["stdout"], r["stderr"])
                    calls.append({"turn": turns, "t_start": ts, "t_end": _clock(), "cmd": cmd,
                                  "stdout": result, "stderr_tail": r["stderr"][-2000:],
                                  "exit": r["code"], "truncated": truncated,
                                  "bg_killed": r["bg_killed"],
                                  "snapshot_step": r["snapshot"]["step"]})
            else:
                result = f"error: unknown tool {first.function!r}"
            messages.append(ChatMessageTool(content=result, tool_call_id=first.id,
                                            function=first.function))
            for tc in rest:
                multi += 1
                messages.append(ChatMessageTool(content=X3T.MULTI_CALL_RESULT,
                                                tool_call_id=tc.id, function=tc.function))
            if stop_rule is not None and end_reason is None \
                    and first.function == "execute_command" \
                    and stop_rule([x.model_dump(exclude_none=True) for x in messages[n_init:]]):
                end_reason = END_SITE
        if mounts and end_reason != END_REPLAY_MISMATCH:
            final = await asyncio.to_thread(SB.read_files, mounts, [SOLUTION_PATH, TEST_PATH])
        if mounts and end_reason == END_SITE and tar_out is not None:
            tar_bytes = await asyncio.to_thread(SB.export_tar, mounts)
            tar_out.parent.mkdir(parents=True, exist_ok=True)
            tar_out.write_bytes(tar_bytes)
            setup["site_tar_sha256"] = hashlib.sha256(tar_bytes).hexdigest()
            setup["site_sha256"] = SB.snapshot_shas(snapshots[-1]) \
                if snapshots and snapshots[-1].get("files") is not None else {}
    except Exception as e:  # noqa: BLE001 - recorded; the runner retries sandbox failures
        end_reason = END_SANDBOX_ERROR if end_reason in (None, X3C.EndReason.DONE.value,
                                                         X3C.EndReason.TURN_CAP.value) \
            else end_reason
        err = f"{type(e).__name__}: {e}"[:2000]
    finally:
        if mounts:
            why = await asyncio.to_thread(SB.remove_volumes, mounts)
            if why:
                err = (err or "") + f"; {why}"
    final_asst = next((x for x in reversed(messages[n_init:])
                       if isinstance(x, ChatMessageAssistant)), None)
    billed_vals = [c["billed_usd"] for c in model_calls]
    return {
        "meta": {"episode_id": spec.episode_id, "site_id": spec.site_id,
                 "task_id": spec.task_id, "cond": spec.cond, "para": spec.para,
                 "repeat": spec.repeat, "mode": spec.mode, "sentence": spec.sentence,
                 "model": m.model_id, "model_slug": m.slug,
                 "remaining_turns": spec.remaining_turns, "remaining_usd": spec.remaining_usd},
        "n_initial": n_init, "setup": setup,
        "messages": [x.model_dump(exclude_none=True) for x in messages[n_init:]],
        "calls": calls, "snapshots": snapshots, "model_calls": model_calls, "bounds": bounds,
        "end_reason": end_reason, "n_turns": turns, "n_nudges": nudges, "n_multi_call": multi,
        "bg_killed": bg_killed, "spent_at_ceiling": spent,
        "billed_usd": (None if any(v is None for v in billed_vals) else sum(billed_vals)),
        "final_message": final_asst.text if final_asst else "",
        "final_solution_py": final.get(SOLUTION_PATH),
        "final_test_solution_py": final.get(TEST_PATH),
        "error": err, "wall_s": round(time.time() - t0, 1),
    }


def charge(art: dict) -> float:
    """What an episode costs the ledger: billed, with a call missing it charged at its
    ceiling, plus failed-attempt charges."""
    return sum(c["billed_usd"] if c["billed_usd"] is not None else c["ceiling_usd"]
               for c in art["model_calls"]) \
        + sum(b.get("failed_charge") or 0.0 for b in art["bounds"])


# --- forks ---------------------------------------------------------------------------------------
async def fork(messages: list[dict], m: C.Model = C.GLM, max_tokens: int = 16000) -> dict:
    """One next turn from `messages` (Inspect dumps), nothing executed: {reasoning, text,
    tool_calls, billed_usd, ceiling_usd, error}."""
    from p41.sources import reasoning_of

    msgs = to_messages(messages)
    ib = input_bound_tokens(msgs, TOOLS)
    one = call_bound(ib, max_tokens, m.in_ceiling, m.out_ceiling) / ATTEMPTS
    out, billed, info, extra, gerr = await generate_with_retries(m, msgs, max_tokens, one)
    if out is None:
        return {"error": gerr, "billed_usd": None, "ceiling_usd": extra, "reasoning": None,
                "text": None, "tool_calls": None}
    d = out.message.model_dump(exclude_none=True)
    return {"reasoning": reasoning_of(d), "text": out.message.text,
            "tool_calls": [{"function": t.function, "arguments": t.arguments}
                           for t in (out.message.tool_calls or [])],
            "stop_reason": out.stop_reason, "billed_usd": billed,
            "ceiling_usd": call_cost(out, m.in_ceiling, m.out_ceiling, one) + extra,
            "error": None, **info}


def spec_dict(spec: Spec) -> dict:
    d = asdict(spec)
    d["initial_messages"] = len(spec.initial_messages)
    return d


def dumps(art: dict) -> str:
    return json.dumps(art, indent=1, default=str)
