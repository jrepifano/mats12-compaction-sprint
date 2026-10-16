"""P49 chain loop: one chain = start context + transition -> beliefs -> agent turns on the shim
(plan.md v5.2, "Setup" > Context contract / Tools, "Measure" (b); contracts.py).

`run_chain(spec, start_messages, tools, task, ...)`; `start_messages` already ends with the
transition user message (the caller builds it).

1. One sandbox per chain (`shim.Sandbox`, started before the first call).
2. Beliefs (if spec.beliefs): B1 and B3, each a forked call on start_messages + the question,
   tools declared with tool_choice "none", max_tokens RESERVE["belief"]; both are sent at once
   (they are independent forks). The last `ANSWER: <n>` (0-100) is the value. They never enter
   the transcript. A belief that does not fit the window is "skipped:window", one that does not
   fit the chain cap "skipped:chain_cap"; a belief ApiError is recorded and the chain goes on
   (beliefs are secondary; the brief's END_API rule is applied to agent turns).
3. Agent turns, up to TURN_CAP. Before every call:
   - deadline_epoch passed -> END_DEADLINE;
   - context contract: ours = count(start_messages, tools) (once, in a thread) + count_messages
     of everything appended since; sent only if CALIB.fits(ours, reserve), else max_tokens
     shrinks to CALIB.room(ours); below MIN_AGENT_MAX_TOKENS -> END_WINDOW without sending;
   - chain cap: worst case = (c * ours * PRICE_IN + max_tokens * PRICE_OUT) / 1e6; if
     spent + worst case > CHAIN_CAP_USD, max_tokens shrinks to what fits; below
     MIN_AGENT_MAX_TOKENS -> END_CHAIN_CAP. The guard uses cap - spent - in-flight bounds: the
     two concurrent beliefs reserve their worst cases jointly before either is sent.
     `spent` is the sum of Response.cost_usd (billed, every attempt per unit C's client; plus
     an ApiError's `cost_usd` if it carries one) over beliefs and turns.
   Every tool call of a turn is executed in order, each answered by a tool message. A turn with
   no tool calls gets NUDGE. `finish` ends the chain (END_DONE) after the calls before it; calls
   after it in the same turn are not run (recorded with "skipped": "after_finish").
4. Ends: ApiError on a turn -> END_API; any unexpected exception -> END_ERROR (traceback in
   `error`); the container is always removed.

Cancellation (choice, per the brief): a CancelledError while the chain runs is caught, the
container's final solution.py is read and the container removed, and a record with
END_DEADLINE (error "cancelled") is RETURNED rather than re-raised, so a hard stop keeps every
snapshot taken so far (a known positive stays positive, plan "Measure"). `asyncio.wait_for` /
`asyncio.timeout` around run_chain therefore return this record instead of raising TimeoutError.
A container that cannot be confirmed removed raises `shim.SandboxLeak` (its `.record` holds the
chain record): the runner must stop on it (plan: "Stop on ... an orphaned container").
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import re
import time
import traceback
from datetime import UTC, datetime

from p49 import contracts as C
from p49 import shim

NUDGE = "Continue with your tools, or call `finish` if you are finished."
ANSWER_RE = re.compile(r"ANSWER:\s*\**\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
BELIEF_KEYS = ("B1", "B3")
FINAL_READ_TIMEOUT_S = 60


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_answer(text: str | None) -> int | None:
    """The last `ANSWER: <n>` with 0 <= n <= 100, rounded; else None."""
    if not text:
        return None
    hits = ANSWER_RE.findall(text)
    if not hits:
        return None
    v = float(hits[-1])
    if not 0 <= v <= 100:
        return None
    return round(v)


def request_sha256(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def worst_case_usd(ours: int, max_tokens: int) -> float:
    return (C.CALIB.c * ours * C.PRICE_IN_PER_M + max_tokens * C.PRICE_OUT_PER_M) / 1e6


def cap_max_tokens(spent: float, ours: int) -> int:
    """Largest max_tokens with spent + worst case <= CHAIN_CAP_USD (may be <= 0)."""
    room = C.CHAIN_CAP_USD - spent - C.CALIB.c * ours * C.PRICE_IN_PER_M / 1e6
    return math.floor(room * 1e6 / C.PRICE_OUT_PER_M)


def normalize_assistant(msg: dict, turn: int) -> dict:
    """The assistant message in the contract's shape: role, content, tool_calls (if any)."""
    content = msg.get("content")
    if isinstance(content, list):          # content parts -> text
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    out: dict = {"role": "assistant", "content": content}
    tcs = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {})
        try:
            json.loads(args)
        except ValueError:
            # Vertex rejects a history holding unparsable arguments ("Expected a valid JSON
            # object", log 2026-10-16 07:53Z): keep them, wrapped, so the history stays sendable
            args = json.dumps({"raw_arguments": args})
        tcs.append({"id": tc.get("id") or f"call_p49_{turn}_{i}", "type": "function",
                    "function": {"name": fn.get("name") or "", "arguments": args}})
    if tcs:
        out["tool_calls"] = tcs
    return out


def _call_record(turn: int, kind: str, body: dict, ours: int, resp: C.Response | None = None,
                 error: str | None = None, attempts: int = 0) -> C.CallRecord:
    rec = C.CallRecord(turn=turn, kind=kind, attempt=resp.attempts if resp else attempts,
                       request_sha256=request_sha256(body), our_tokens=ours,
                       max_tokens=body["max_tokens"], error=error)
    if resp is not None:
        rec.prompt_tokens = resp.prompt_tokens
        rec.completion_tokens = resp.completion_tokens
        rec.cost_usd = resp.cost_usd
        rec.provider = resp.provider
        rec.finish_reason = resp.finish_reason
        rec.latency_s = resp.latency_s
    return rec


def _api_error_cost(e: BaseException) -> float:
    """Billed cost a failed request carries, if the client attaches one (`cost_usd`)."""
    try:
        return float(getattr(e, "cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


class _Chain:
    """Mutable state of one chain run."""

    def __init__(self, spec, start_messages, tools, task, chat, count, count_messages,
                 deadline_epoch):
        self.spec = spec
        self.start = list(start_messages)
        self.tools = tools
        self.task = task
        self.chat = chat
        self.count = count
        self.count_messages = count_messages
        self.deadline = deadline_epoch
        self.messages = list(start_messages)
        self.transcript: list[dict] = []
        self.calls: list[C.CallRecord] = []
        self.skipped: list[dict] = []
        self.beliefs: dict = {}
        self.spent = 0.0
        self.inflight = 0.0          # worst-case bounds of calls sent and not yet settled
        self.peak = 0
        self.or_ratio = 0.0          # OpenRouter pre-flight estimate / ours, once it rejected
        self.turns = 0
        self.start_tokens = 0
        self.ours = 0
        self.end: str | None = None
        self.error: str | None = None

    def committed(self) -> float:
        """Billed so far (every attempt) plus the bounds of calls still in flight."""
        return self.spent + self.inflight

    def past_deadline(self) -> bool:
        return self.deadline is not None and time.time() >= self.deadline

    async def append(self, msgs: list[dict]) -> None:
        self.messages.extend(msgs)
        self.transcript.extend(msgs)
        self.ours += await asyncio.to_thread(self.count_messages, msgs)

    # -- beliefs --
    async def beliefs_run(self) -> None:
        planned = []
        for key in BELIEF_KEYS:
            q = {"role": "user", "content": C.BELIEFS[key]}
            ours = self.start_tokens + await asyncio.to_thread(self.count_messages, [q])
            mt = C.RESERVE["belief"]
            entry = {"value": None, "raw": None, "status": None, "our_tokens": ours,
                     "max_tokens": mt, "error": None}
            self.beliefs[key] = entry
            if self.past_deadline():
                entry["status"] = "skipped:deadline"
            elif not C.CALIB.fits(ours, mt):
                entry["status"] = "skipped:window"
            elif self.committed() + worst_case_usd(ours, mt) > C.CHAIN_CAP_USD:
                entry["status"] = "skipped:chain_cap"
            else:
                bound = worst_case_usd(ours, mt)
                self.inflight += bound          # reserved before launch: joint guard
                body = C.make_body(self.start + [q], self.tools, mt, tool_choice="none")
                planned.append((key, body, ours, bound))
        await asyncio.gather(*(self._belief(k, b, o, w) for k, b, o, w in planned))

    async def _belief(self, key: str, body: dict, ours: int, bound: float) -> None:
        entry = self.beliefs[key]
        kind = f"belief:{key}"
        try:
            resp = await self.chat(body, kind=kind, chain_id=self.spec.chain_id,
                                   our_tokens=ours)
        except C.WindowFull as e:
            entry.update(status="skipped:window", error=str(e)[:600])
            self.calls.append(_call_record(0, kind, body, ours, error=str(e)[:600]))
            return
        except C.ApiError as e:
            self.spent += _api_error_cost(e)
            entry.update(status="api_error", error=str(e)[:2000])
            self.calls.append(_call_record(0, kind, body, ours, error=str(e)[:2000],
                                           attempts=getattr(e, "attempts", 0)))
            return
        finally:
            self.inflight -= bound
        self.spent += resp.cost_usd or 0.0
        self.calls.append(_call_record(0, kind, body, ours, resp))
        raw = resp.message.get("content")
        entry.update(raw=raw, value=parse_answer(raw), status="ok")

    # -- agent turns --
    async def agent_run(self, sb) -> None:
        while self.end is None:
            if self.turns >= C.TURN_CAP:
                self.end = C.END_TURN_CAP
                break
            if self.past_deadline():
                self.end = C.END_DEADLINE
                break
            ours = self.ours
            mt = C.RESERVE["agent"]
            if not C.CALIB.fits(ours, mt):
                mt = C.CALIB.room(ours)
                if mt < C.MIN_AGENT_MAX_TOKENS:
                    self.end = C.END_WINDOW
                    break
            if self.or_ratio:
                mt = min(mt, int(C.WINDOW - C.OR_MARGIN - self.or_ratio * ours))
                if mt < C.MIN_AGENT_MAX_TOKENS:
                    self.end = C.END_WINDOW
                    break
            if self.committed() + worst_case_usd(ours, mt) > C.CHAIN_CAP_USD:
                mt = min(mt, cap_max_tokens(self.committed(), ours))
                if mt < C.MIN_AGENT_MAX_TOKENS:
                    self.end = C.END_CHAIN_CAP
                    break
            body = C.make_body(list(self.messages), self.tools, mt)   # a copy: we append
            turn = self.turns + 1
            try:
                resp = await self.chat(body, kind="agent", chain_id=self.spec.chain_id,
                                       our_tokens=ours)
            except C.WindowFull as e:
                self.calls.append(_call_record(turn, "agent", body, ours, error=str(e)[:600]))
                if e.or_input and ours:
                    ratio = (e.or_input + 1) / ours
                    if ratio > self.or_ratio:
                        self.or_ratio = ratio
                        continue                 # retry the turn with a smaller max_tokens
                self.end = C.END_WINDOW
                break
            except C.ApiError as e:
                self.spent += _api_error_cost(e)
                self.calls.append(_call_record(turn, "agent", body, ours, error=str(e)[:2000],
                                               attempts=getattr(e, "attempts", 0)))
                self.end = C.END_API
                self.error = f"ApiError: {str(e)[:2000]}"
                break
            self.turns = turn
            self.spent += resp.cost_usd or 0.0
            self.peak = max(self.peak, resp.prompt_tokens or 0)
            self.calls.append(_call_record(turn, "agent", body, ours, resp))
            msg = normalize_assistant(resp.message or {}, turn)
            await self.append([msg])
            tcs = msg.get("tool_calls") or []
            if not tcs:
                await self.append([{"role": "user", "content": NUDGE}])
                continue
            out: list[dict] = []
            for i, tc in enumerate(tcs):
                fn = tc["function"]
                if self.end == C.END_DONE:
                    self.skipped.append({"turn": turn, "index": i, "tool_call_id": tc["id"],
                                         "name": fn["name"], "arguments": fn["arguments"],
                                         "skipped": "after_finish"})
                    continue
                res = await sb.call(fn["name"], fn["arguments"], turn=turn, index=i,
                                    tool_call_id=tc["id"])
                out.append({"role": "tool", "tool_call_id": tc["id"],
                            "content": res.observation})
                if res.done:
                    self.end = C.END_DONE
            await self.append(out)


async def run_chain(spec: C.ChainSpec, start_messages: list[dict], tools: list[dict],
                    task, *, chat=None, count=None, count_messages=None,
                    deadline_epoch: float | None = None,
                    sandbox_factory=shim.Sandbox) -> C.ChainRecord:
    """One chain (module docstring). `chat(body, *, kind, chain_id, our_tokens)` returns a
    contracts.Response or raises contracts.ApiError."""
    if chat is None:
        from p49 import client
        chat = client.chat
    if count is None or count_messages is None:
        from p49 import tokens
        count = count or tokens.count
        count_messages = count_messages or tokens.count_messages
    st = _Chain(spec, start_messages, tools, task, chat, count, count_messages, deadline_epoch)
    started = _now()
    sb = None
    final: str | None = None
    leak: shim.SandboxLeak | None = None
    try:
        st.start_tokens = await asyncio.to_thread(count, st.start, tools)
        st.ours = st.start_tokens
        sb = sandbox_factory(spec.chain_id, task)
        async with sb:
            try:
                if spec.beliefs:
                    await st.beliefs_run()
                await st.agent_run(sb)
            except asyncio.CancelledError:
                st.end, st.error = C.END_DEADLINE, "cancelled"
            except Exception:  # noqa: BLE001 - recorded, the container is still removed
                st.end, st.error = C.END_ERROR, traceback.format_exc()[-8000:]
            try:
                final = await asyncio.wait_for(sb.final_solution(), FINAL_READ_TIMEOUT_S)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                final = None
    except shim.SandboxLeak as e:
        leak = e
        st.error = ((st.error + "\n") if st.error else "") + f"sandbox leak: {e}"
        st.end = st.end or C.END_ERROR
    except asyncio.CancelledError:
        st.end, st.error = C.END_DEADLINE, "cancelled"
    except Exception:  # noqa: BLE001 - e.g. the sandbox failed to start
        st.end = C.END_ERROR
        st.error = ((st.error + "\n") if st.error else "") + traceback.format_exc()[-8000:]
    rec = C.ChainRecord(
        spec=spec, end_reason=st.end or C.END_ERROR, n_turns=st.turns, calls=st.calls,
        tool_calls=(list(sb.calls) if sb is not None else []) + st.skipped,
        snapshots=list(sb.snapshots) if sb is not None else [],
        final_solution_py=final, beliefs=st.beliefs, transcript=st.transcript,
        start_tokens=st.start_tokens, peak_prompt_tokens=st.peak, cost_usd=round(st.spent, 6),
        started_at=started, ended_at=_now(), error=st.error)
    if leak is not None:
        leak.record = rec
        raise leak
    return rec


# --- (de)serialisation -----------------------------------------------------------------------
def record_to_dict(rec: C.ChainRecord) -> dict:
    """JSON-safe dict (dataclasses.asdict; tuples become lists)."""
    return json.loads(json.dumps(dataclasses.asdict(rec), ensure_ascii=False))


def record_from_dict(d: dict) -> C.ChainRecord:
    d = dict(d)
    spec = C.ChainSpec(**d.pop("spec"))
    calls = [C.CallRecord(**c) for c in d.pop("calls")]
    return C.ChainRecord(spec=spec, calls=calls, **d)
