"""Chat models for the S2 loop: a protocol, a $0 stub, an OpenAI-compatible route, spend hooks.

`ChatModel.complete(messages, tools, max_tokens, purpose=...) -> Reply` takes OpenAI-format
messages (system, user, assistant with tool_calls, tool) and returns the assistant message with
its usage and cost. Agent calls pass the tool list; summarizer calls pass `tools=None`.

Spend (spec "Budget hooks"): every attempt of every paid call reserves its bounded cost first
(`Spend.reserve`, one token per byte sent + 256, times the higher input price; output at the
call's hard cap) and settles at its actual cost (OpenRouter's `usage.cost` when present, else
usage x route prices with cache reads at the cache price and Haiku's long-context tier). An
attempt that fails without usage stays charged at its bound, except statuses that never ran
inference (400, 401, 403, 404, 413, 422, 429), as in `compaction.s1_api`. `SprintSpend` books
through `compaction.budget` against SPRINT-S2 (stage and sprint caps, designated checkout only);
`NullSpend` only counts, for stub runs.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from compaction.s2 import config as C

OVERHEAD_TOKENS = 256
MAX_ATTEMPTS = 4
_UNBILLED = (400, 401, 403, 404, 413, 422, 429)
_TRANSIENT = (408, 409, 429, 500, 502, 503, 504, 529)


class ModelError(RuntimeError):
    """A call that produced no reply. `cost_usd` is everything its attempts were charged."""

    def __init__(self, msg: str, cost_usd: float = 0.0, status: int | None = None):
        super().__init__(msg)
        self.cost_usd = cost_usd
        self.status = status


class RunCapReached(ModelError):
    """The next attempt's bound would take the run past its cap; nothing was sent."""


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int = 0
    reasoning_tokens: int | None = None
    cost_usd: float = 0.0
    cost_source: str = "none"  # "provider" | "table" | "bound" | "stub"


@dataclass
class Reply:
    message: dict
    usage: Usage
    finish_reason: str | None = None
    latency_s: float = 0.0
    attempts: int = 1
    model: str | None = None
    cost_total_usd: float | None = None  # every attempt's charge, failed retries included

    def __post_init__(self) -> None:
        if self.cost_total_usd is None:
            self.cost_total_usd = self.usage.cost_usd


class ChatModel(Protocol):
    route: C.Route

    def complete(self, messages: list[dict], tools: list[dict] | None, max_tokens: int, *,
                 purpose: str) -> Reply: ...


# ------------------------------------------------------------------------------------------
# Spend hooks
# ------------------------------------------------------------------------------------------


class Spend(Protocol):
    def reserve(self, usd: float, note: str) -> str: ...

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None: ...

    def cancel(self, rid: str, reason: str) -> None: ...


@dataclass
class NullSpend:
    """Counts reservations and charges; books nothing (stub runs, tests)."""

    reserved: float = 0.0
    charged: float = 0.0
    calls: int = 0
    _open: dict = field(default_factory=dict)

    def reserve(self, usd: float, note: str) -> str:
        rid = f"null-{self.calls}"
        self.calls += 1
        self._open[rid] = usd
        self.reserved += usd
        return rid

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None:
        self._open.pop(rid)
        self.charged += usd

    def cancel(self, rid: str, reason: str) -> None:
        self._open.pop(rid)


@dataclass
class RunSpend:
    """Run-level books over an inner Spend (stage and sprint caps stay with the inner one):
    every attempt reserves against the run's remaining cap before it is sent (raising
    RunCapReached instead of sending), and `spent` sums every settled charge, failed attempts
    included (review r1 #6). Thread-safe: a run's arms share it."""

    inner: Spend
    cap_usd: float | None = None
    spent: float = 0.0
    _open: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> float | None:
        if not self.cap_usd:
            return None
        return self.cap_usd - self.spent - sum(self._open.values())

    def reserve(self, usd: float, note: str) -> str:
        with self._lock:
            left = self.remaining()
            if left is not None and usd > left + 1e-9:
                raise RunCapReached(f"run cap ${self.cap_usd:.2f}: ${self.spent:.4f} spent, "
                                    f"${sum(self._open.values()):.4f} open, next bound "
                                    f"${usd:.4f}")
            rid = self.inner.reserve(usd, note)
            self._open[rid] = usd
            return rid

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None:
        self.inner.settle(rid, usd, resource=resource, units=units, note=note)
        with self._lock:
            self._open.pop(rid, None)
            self.spent += usd

    def cancel(self, rid: str, reason: str) -> None:
        self.inner.cancel(rid, reason)
        with self._lock:
            self._open.pop(rid, None)


@dataclass
class SprintSpend:
    """compaction.budget against the stage (default SPRINT-S2). Refuses (WrongCheckout) unless
    this process runs from the designated checkout in docs/sprint/caps.toml."""

    stage: str = C.STAGE

    def reserve(self, usd: float, note: str) -> str:
        from compaction import budget
        return budget.reserve(self.stage, usd, note=note)

    def settle(self, rid: str, usd: float, *, resource: str, units: float, note: str) -> None:
        from compaction import budget
        budget.settle(rid, usd, stage=self.stage, kind="api", resource=resource, units=units,
                      note=note)

    def cancel(self, rid: str, reason: str) -> None:
        from compaction import budget
        budget.cancel(rid, reason)


# ------------------------------------------------------------------------------------------
# Pricing
# ------------------------------------------------------------------------------------------


def bound_usd(route: C.Route, body_bytes: int, max_tokens: int) -> float:
    """Reservation size: one token per byte sent (+ overhead) at the highest input price."""
    from compaction import budget
    p = route.prices
    pin = max(p["input"], p.get("input_long", 0.0))
    prices = {route.model: {"input": pin, "output": p["output"]}}
    return budget.bounded_cost(route.model, body_bytes + OVERHEAD_TOKENS, max_tokens, prices)


def table_usd(route: C.Route, prompt: int, cached: int, completion: int) -> float:
    p = route.prices
    long = p.get("long_above")
    pin = p.get("input_long", p["input"]) if long and prompt > long else p["input"]
    uncached = max(0, prompt - cached)
    return (uncached * pin + cached * p.get("cache_read", p["input"])
            + completion * p["output"]) / 1e6


def est_tokens(obj: Any, bytes_per_token: float) -> int:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(len(s.encode("utf-8")) / bytes_per_token) + 1


# ------------------------------------------------------------------------------------------
# Message helpers
# ------------------------------------------------------------------------------------------


def normalize_assistant(msg: dict, call_index: int) -> dict:
    """The assistant message as stored in history: content text plus well-formed tool calls
    (ids filled, arguments a JSON string). Reasoning text is not carried in the history."""
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    out: dict = {"role": "assistant", "content": content or ""}
    tcs = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {})
        try:
            json.loads(args)
        except ValueError:
            args = json.dumps({"raw_arguments": args})
        tcs.append({"id": tc.get("id") or f"call_{call_index}_{i}", "type": "function",
                    "function": {"name": fn.get("name") or "", "arguments": args}})
    if tcs:
        out["tool_calls"] = tcs
    return out


def with_cache_markers(messages: list[dict]) -> list[dict]:
    """Anthropic-style cache_control on the system message and the last message (OpenRouter
    passes these through to Anthropic models)."""
    out = [dict(m) for m in messages]
    for i in {0, len(out) - 1}:
        m = out[i]
        c = m.get("content")
        if isinstance(c, str) and c:
            m["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
    return out


# ------------------------------------------------------------------------------------------
# OpenAI-compatible route
# ------------------------------------------------------------------------------------------

BASE_URLS = {"deepseek": "https://api.deepseek.com", "openrouter": "https://openrouter.ai/api/v1",
             "openai": None}


def _status(e: BaseException) -> int | None:
    s = getattr(e, "status_code", None)
    return s if isinstance(s, int) else None


@dataclass
class OpenAICompatModel:
    route: C.Route
    spend: Spend
    client: Any = None  # injectable (tests); built from the route's key otherwise
    timeout_s: float = 600.0
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.route.transport == "stub":
            raise ValueError("use StubModel for the stub route")
        if self.client is None:
            from openai import OpenAI
            key = os.environ.get(self.route.key_env)
            if not key:
                raise RuntimeError(f"{self.route.key_env} is not set (load the repo .env)")
            self.client = OpenAI(api_key=key, base_url=BASE_URLS[self.route.transport],
                                 max_retries=0, timeout=self.timeout_s)

    def body(self, messages: list[dict], tools: list[dict] | None, max_tokens: int) -> dict:
        r = self.route
        msgs = with_cache_markers(messages) if r.cache_markers else messages
        b: dict = {"model": r.model, "messages": msgs}
        if tools:
            b["tools"] = tools
        if r.transport == "openai":
            b["max_completion_tokens"] = max_tokens
        else:
            b["max_tokens"] = max_tokens
        extra: dict = {}
        for k, v in r.reasoning.items():
            if k == "reasoning":
                extra[k] = v
            else:
                b[k] = v
        if r.transport == "openrouter":
            extra["usage"] = {"include": True}
            if r.provider_pin:
                extra["provider"] = {"order": [r.provider_pin], "allow_fallbacks": False}
        if extra:
            b["extra_body"] = extra
        return b

    def _cost(self, usage: Any, prompt: int, cached: int, completion: int) -> tuple[float, str]:
        extra = getattr(usage, "model_extra", None) or {}
        c = extra.get("cost") if isinstance(extra, dict) else None
        if isinstance(c, int | float) and c >= 0:
            up = ((extra.get("cost_details") or {}).get("upstream_inference_cost") or 0.0)
            return float(c) + float(up or 0.0), "provider"
        return table_usd(self.route, prompt, cached, completion), "table"

    def complete(self, messages: list[dict], tools: list[dict] | None, max_tokens: int, *,
                 purpose: str) -> Reply:
        body = self.body(messages, tools, max_tokens)
        nbytes = len(json.dumps({k: v for k, v in body.items() if k != "extra_body"},
                                ensure_ascii=False).encode("utf-8"))
        bound = bound_usd(self.route, nbytes, max_tokens)
        last: BaseException | None = None
        charged = 0.0  # every attempt of this call, failed ones included
        for attempt in range(1, MAX_ATTEMPTS + 1):
            rid = self.spend.reserve(bound, note=f"s2 {purpose} attempt {attempt}")
            t0 = time.monotonic()
            try:
                resp = self.client.chat.completions.create(**body)
            except Exception as e:
                st = _status(e)
                charge = 0.0 if st in _UNBILLED else bound
                self.spend.settle(rid, charge, resource=f"{self.route.name}:{self.route.model}",
                                  units=0, note=f"s2 {purpose} failed {st or type(e).__name__}")
                charged += charge
                last = e
                transient = st in _TRANSIENT or st is None
                if not transient or attempt == MAX_ATTEMPTS:
                    raise ModelError(f"{type(e).__name__}: {str(e)[:500]}", charged, st) from e
                self.sleep(min(60.0, 2.0 ** attempt + random.random()))
                continue
            usage = getattr(resp, "usage", None)
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            details = getattr(usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0)
            extra = getattr(usage, "model_extra", None) or {}
            cached = cached or int(extra.get("prompt_cache_hit_tokens", 0) or 0)
            cdet = getattr(usage, "completion_tokens_details", None)
            reasoning = getattr(cdet, "reasoning_tokens", None)
            if usage is None:
                cost, src = bound, "bound"
            else:
                cost, src = self._cost(usage, prompt, cached, completion)
            self.spend.settle(rid, cost, resource=f"{self.route.name}:{self.route.model}",
                              units=prompt + completion, note=f"s2 {purpose}")
            charged += cost
            choices = getattr(resp, "choices", None) or []
            if not choices:
                last = ModelError("no choices", cost)
                continue
            ch = choices[0]
            msg = ch.message.model_dump() if hasattr(ch.message, "model_dump") else dict(ch.message)
            return Reply(message=msg, usage=Usage(prompt, completion, cached, reasoning, cost, src),
                         finish_reason=getattr(ch, "finish_reason", None),
                         latency_s=time.monotonic() - t0, attempts=attempt,
                         model=getattr(resp, "model", None), cost_total_usd=charged)
        raise ModelError(f"no reply after {MAX_ATTEMPTS} attempts: {last}", charged)


# ------------------------------------------------------------------------------------------
# Stub
# ------------------------------------------------------------------------------------------

Action = dict  # {"bash": cmd} | {"ask": question, "action": a} | {"say": text} | {"submit": True}


def tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


@dataclass
class StubModel:
    """A deterministic model for $0 end-to-end runs. Agent calls play `actions` in order (one
    action per call; then it submits); summarizer calls (tools=None) return `summary(messages)`.
    Usage is estimated from bytes, at cost 0."""

    actions: list[Action]
    summary: Callable[[list[dict]], str] = lambda msgs: "<summary>Stub summary.</summary>"
    route: C.Route = C.ROUTES["stub"]
    calls: int = 0

    def complete(self, messages: list[dict], tools: list[dict] | None, max_tokens: int, *,
                 purpose: str) -> Reply:
        prompt = est_tokens(messages, self.route.bytes_per_token)
        if tools is None:
            text = self.summary(messages)
            return Reply({"role": "assistant", "content": text},
                         Usage(prompt, est_tokens(text, 2.0), cost_source="stub"), "stop")
        i = self.calls
        self.calls += 1
        a = self.actions[i] if i < len(self.actions) else {"submit": True}
        cid = f"stub_{purpose}_{i}"
        if "bash" in a:
            msg = {"role": "assistant", "content": a.get("say", ""),
                   "tool_calls": [tool_call("bash", {"command": a["bash"]}, cid)]}
        elif "ask" in a:
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [tool_call("ask_user", {"question": a["ask"],
                                                         "action": a.get("action", "")}, cid)]}
        elif "submit" in a:
            msg = {"role": "assistant", "content": "Done.",
                   "tool_calls": [tool_call("bash", {"command": f"echo {C.SUBMIT_TOKEN}"}, cid)]}
        else:
            msg = {"role": "assistant", "content": a.get("say", "")}
        return Reply(msg, Usage(prompt, est_tokens(msg, 2.0), cost_source="stub"),
                     "tool_calls" if msg.get("tool_calls") else "stop")
