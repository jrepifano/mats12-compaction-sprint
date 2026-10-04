"""I9's free-text LLM client, and the adapter that lets WorkspaceBench's A-C judge use it.

Implementation plan §1.4 (frozen interfaces) and §2 U4; prereg v0.5 §4.3 ("we run the same
prompts, word for word, through luna, using a small free-text client in our own code").

``complete(prompts, *, route, ...)`` sends user-only (or system + user) free-text prompts to one
of the contracts' models and hands each reply to ``on_result(i, text)`` as it lands. Three
transports, picked by ``contracts.ROUTE``:

- ``openai``: gpt-6-luna through the OpenAI SDK (``max_completion_tokens``, ``reasoning_effort``;
  the request shape of ``emx.judge_panel.PRESETS["luna"]``);
- ``openrouter``: Sonnet 5 (``anthropic/claude-sonnet-5``; .env has no Anthropic key, contracts
  2026-10-04), DeepSeek, GLM and Gemini through the OpenAI SDK at OpenRouter
  (``extra_body.reasoning``, ``usage.include``, ``provider.require_parameters``, as wsbench sends,
  plus the provider routing of ``contracts.PROVIDER[route]``, sent unchanged: the hard ``only``
  allow-list, preferred ``order``, ``quantizations`` and the ``max_price`` ceiling; Jacob,
  2026-10-04; code review r2 finding 7);
- ``anthropic``: kept for a direct Anthropic key, unused unless ``contracts.ROUTE`` says so:
  streamed, exactly as ``wsbench.llm._anthropic_text_once`` sends it (``thinking`` disabled or
  adaptive).

Every transport re-asks an empty reply cut at the token budget with double the budget, up to
``TEXT_BUDGET_CEILING`` (wsbench's rule for its Sonnet route), but at most
``MAX_BUDGET_DOUBLINGS`` times per prompt (2: e.g. 400 -> 800 -> 1600, or 16k -> 32k -> 64k).
Each re-ask is a new call whose worst case (the prompt again, plus the doubled budget as output)
is not in the batch's upfront projection, so before it is sent the batch's running reservation
(upfront projection plus every re-ask reserved so far) is re-checked with
``emx.spend.check_total``; a re-ask that would cross a limit raises ``TextLLMConfigError`` before
the call (code review r2 finding 6). After the last allowed doubling an empty reply lands as
``""``, as at the ceiling.

``reasoning`` is one of ``REASONING`` (``contracts.StageEffort``): ``"none"`` (luna
``reasoning_effort="none"``; OpenRouter ``reasoning={"enabled": False}``; Anthropic
``thinking={"type": "disabled"}``), ``"low"`` (``reasoning_effort="low"``;
``reasoning={"effort": "low"}``; adaptive thinking with ``output_config.effort="low"``) or
``"default"`` (no reasoning field at all; Anthropic: adaptive thinking with no effort, which is
what wsbench sends for Stages B and C). See ``_openai_once`` for what "default" means for Sonnet
on OpenRouter (unverified offline). Each journal line records the response's
``reasoning_tokens`` so a run can check afterwards whether the model reasoned.

Pacing, retries and failures follow ``wsbench.llm``: a process-wide requests-per-minute pacer, a
semaphore of ``concurrency``, a per-call ``timeout``; transient failures (408/409/429/5xx,
timeouts, connection errors, a 200 with no choices) are retried with tenacity and jittered
backoff, and a call that still fails, or fails non-transiently, lands as ``on_result(i, None)``
with ``spend.errors`` (``spend.refusals`` for a refusal) incremented, never raised. Problems that
would fail every call raise ``TextLLMConfigError`` (a ``wsbench.llm.JudgeConfigError``): an
unknown route, a missing key or a cap refusal before any call; an HTTP 401/402/403 (or 404, an
unknown model: wsbench treats it the same way) on first sight, which also cancels the batch's
outstanding calls.

Spend: every HTTP response's token usage increments the caller's ``wsbench.llm.Spend`` in place
(``calls`` counts responses, as in wsbench). ``usd`` is the response's billed ``usage.cost`` when
OpenRouter reports it (we request ``usage.include``), else priced from
``contracts.PRICE_PER_MTOK``; the journal line records which (``cost_source``: "billed", "table"
or "mixed" across a prompt's responses) and the ``provider`` OpenRouter routed the call to.
Before each batch ``emx.spend.check_total(projection, "judge", exp="I9")`` runs on a projection
(input tokens = characters / ``CHARS_PER_TOKEN_EST`` + overhead; output = ``max_tokens`` per
prompt); after the batch (also when it raises) the batch's actual dollars are recorded with
``emx.spend.record``. Every prompt's outcome (reply text, usage, attempts, error) is journaled
as one JSONL line under ``contracts.CALLS_DIR`` (``<purpose>__<model>.jsonl``).

Keys are read from the environment (``OPENAI_API_KEY``, ``OPENROUTER_API_KEY``, and
``ANTHROPIC_API_KEY`` only for the unused direct route) and never printed; callers load ``.env`` first.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt
from wsbench.llm import JudgeConfigError, Spend

from emx import spend as emx_spend
from emx.i9 import contracts as C

OPENROUTER = "https://openrouter.ai/api/v1"
REASONING: tuple[str, ...] = ("none", "low", "default")
KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
CHARS_PER_TOKEN_EST = 3.0  # conservative, as emx.judge_panel
OVERHEAD_TOKENS = 16  # chat-template tokens per request, for the projection only
ATTEMPTS = 8  # tenacity attempts per prompt (transient failures only)
TEXT_BUDGET_CEILING = 64_000  # wsbench.llm._TEXT_BUDGET_CEILING
MAX_BUDGET_DOUBLINGS = 2  # per prompt; each re-ask is re-checked against the spend guard first
_TRANSIENT = (408, 409, 429, 500, 502, 503, 504, 529)
_FATAL_STATUS = (401, 402, 403, 404)
_FATAL_NAMES = ("AuthenticationError", "PermissionDeniedError", "NotFoundError")


class TextLLMConfigError(JudgeConfigError):
    """A problem that would fail every call: raised, never degraded to ``None``."""


class _NoChoices(ValueError):
    """A 200 response without choices or text (an error delivered in the body): transient."""


# ---------------------------------------------------------------- seams (tests patch these)


def _make_client(transport: str, key: str) -> Any:
    """The ONLY place SDK clients are built; ``max_retries=0`` (tenacity retries here)."""
    if transport == "anthropic":
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=key, max_retries=0)
    from openai import AsyncOpenAI

    if transport == "openrouter":
        return AsyncOpenAI(api_key=key, base_url=OPENROUTER, max_retries=0)
    return AsyncOpenAI(api_key=key, max_retries=0)


def _backoff(attempt: int, status: int | None) -> float:
    """wsbench.llm._backoff: jittered exponential, capped at 90 s for 429 and 30 s otherwise."""
    cap = 90.0 if status == 429 else 30.0
    return min(cap, 2.0 * 2**attempt) * (0.5 + random.random())


class _Pacer:
    """Process-wide next-slot time behind a lock (wsbench.llm._Pacer)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def reset(self) -> None:
        with self._lock:
            self._next = 0.0

    def reserve(self, rpm: float) -> float:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + 60.0 / rpm
            return slot - now


_PACER = _Pacer()


async def _pace(rpm: float) -> None:
    delay = _PACER.reserve(rpm)
    if delay > 0:
        await asyncio.sleep(delay)


# ---------------------------------------------------------------- config


def transport_of(route: str) -> str:
    if route not in C.ROUTE:
        raise TextLLMConfigError(f"unknown route {route!r}; known {sorted(C.ROUTE)}")
    return C.ROUTE[route]


def api_key(route: str) -> str:
    transport = transport_of(route)
    key = os.environ.get(KEY_ENV[transport], "")
    if not key or (transport == "openrouter" and not key.startswith("sk-or-")):
        raise TextLLMConfigError(
            f"{KEY_ENV[transport]} is missing or malformed (needed for {route})"
        )
    return key


def price_usd(route: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = C.PRICE_PER_MTOK[route]
    return (input_tokens * p_in + output_tokens * p_out) / 1e6


def project_usd(prompts: list[str], *, route: str, system: str | None, max_tokens: int) -> float:
    """Projected cost of one batch: input from characters, output = ``max_tokens`` per prompt."""
    n_sys = len(system or "")
    tin = sum((len(p) + n_sys) / CHARS_PER_TOKEN_EST + OVERHEAD_TOKENS for p in prompts)
    return price_usd(route, int(tin) + 1, max_tokens * len(prompts))


class _Reservation:
    """A batch's in-flight worst-case spend, not yet in the ledger: the upfront projection plus
    every budget re-ask reserved so far. ``reask`` re-runs ``emx.spend.check_total`` on the new
    total before the re-ask is sent. The batch runs on one event loop thread and ``reask`` never
    awaits, so check and increment cannot interleave."""

    def __init__(self, route: str, projected: float, system: str | None) -> None:
        self.route, self.total, self.n_sys = route, projected, len(system or "")

    def reask(self, user: str, budget: int) -> float:
        tin = int((len(user) + self.n_sys) / CHARS_PER_TOKEN_EST + OVERHEAD_TOKENS) + 1
        extra = price_usd(self.route, tin, budget)
        try:
            emx_spend.check_total(self.total + extra, "judge", exp=C.EXP)
        except emx_spend.BudgetExceeded as e:
            raise TextLLMConfigError(
                f"budget re-ask to max_tokens={budget} refused (batch reservation "
                f"${self.total:.4f} + ${extra:.4f}): {e}"
            ) from e
        self.total += extra
        return extra


def journal_path(purpose: str, route: str) -> Path:
    return C.CALLS_DIR / f"{purpose}__{C.LABEL_SHORT[route]}.jsonl"


# ---------------------------------------------------------------- one call


@dataclass
class _Landed:
    text: str | None
    stop: str | None
    refusal: bool = False


@dataclass
class _Tally:
    """One prompt's HTTP responses and tokens, for the journal."""

    responses: int = 0
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    provider_cost: float | None = None
    reasoning_tokens: int | None = None  # OpenAI shape: completion_tokens_details, if reported
    cost_sources: tuple[str, ...] = ()  # per priced response: "billed" (usage.cost) or "table"
    providers: tuple[str, ...] = ()  # OpenRouter's response `provider`, per response that has one

    def cost_source(self) -> str | None:
        kinds = set(self.cost_sources)
        return None if not kinds else kinds.pop() if len(kinds) == 1 else "mixed"

    def provider(self) -> str | None:
        seen = list(dict.fromkeys(self.providers))
        return None if not seen else seen[0] if len(seen) == 1 else ",".join(seen)


def _status(e: BaseException) -> int | None:
    s = getattr(e, "status_code", None)
    return s if isinstance(s, int) else None


def _field(obj: Any, name: str) -> Any:
    """A response field the SDK may hold as an attribute or only in pydantic's ``model_extra``
    (OpenRouter's ``usage.cost`` and top-level ``provider``)."""
    v = getattr(obj, name, None)
    if v is None:
        v = (getattr(obj, "model_extra", None) or {}).get(name)
    return v


def openrouter_provider(route: str) -> dict[str, Any]:
    """The request's ``provider`` object: ``contracts.PROVIDER[route]`` (copied) plus
    ``require_parameters: true``."""
    return {**copy.deepcopy(C.PROVIDER.get(route, {})), "require_parameters": True}


def _fatal(e: BaseException) -> bool:
    return type(e).__name__ in _FATAL_NAMES or _status(e) in _FATAL_STATUS


def _transient(e: BaseException) -> bool:
    if isinstance(e, TextLLMConfigError):
        return False
    name = type(e).__name__
    return (
        _status(e) in _TRANSIENT
        or "Timeout" in name
        or "Connection" in name
        or "Deadline" in name
        or isinstance(e, _NoChoices)
    )


def _count(route: str, spend: Spend, tally: _Tally, usage: Any, *, openai_shape: bool) -> None:
    spend.calls += 1
    tally.responses += 1
    if usage is None:
        return
    if openai_shape:
        tin = int(getattr(usage, "prompt_tokens", 0) or 0)
        tout = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "completion_tokens_details", None)
        rt = getattr(details, "reasoning_tokens", None) if details is not None else None
        if isinstance(rt, int):
            tally.reasoning_tokens = (tally.reasoning_tokens or 0) + rt
        cost = _field(usage, "cost")
        if cost is not None:
            tally.provider_cost = (tally.provider_cost or 0.0) + float(cost)
    else:
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        cost = None
    if cost is not None:  # OpenRouter's billed cost wins over the price table
        usd = float(cost)
        tally.cost_sources += ("billed",)
    else:
        usd = price_usd(route, tin, tout)
        tally.cost_sources += ("table",)
    spend.input_tokens += tin
    spend.output_tokens += tout
    spend.usd += usd
    tally.input_tokens += tin
    tally.output_tokens += tout
    tally.usd += usd


async def _openai_once(
    client: Any,
    transport: str,
    route: str,
    system: str | None,
    user: str,
    *,
    reasoning: str,
    max_tokens: int,
    timeout: float,
    spend: Spend,
    tally: _Tally,
) -> _Landed:
    messages = ([{"role": "system", "content": system}] if system is not None else []) + [
        {"role": "user", "content": user}
    ]
    kw: dict[str, Any] = {"model": route, "messages": messages, "timeout": timeout}
    if transport == "openai":
        kw["max_completion_tokens"] = max_tokens
        if reasoning != "default":
            kw["reasoning_effort"] = reasoning
    else:
        kw["max_tokens"] = max_tokens
        extra: dict[str, Any] = {
            "usage": {"include": True},
            "provider": openrouter_provider(route),
        }
        # "default" sends no reasoning field. For anthropic/claude-sonnet-5 (contracts'
        # SONNET_EFFORT_BY_THINKING sends "default" for Stages B and C, to match wsbench's
        # adaptive thinking) it is NOT verified offline whether OpenRouter then omits Anthropic's
        # `thinking` (the native API's default for Sonnet 5 is adaptive) or sends thinking off.
        # Check the first paid Sonnet batch: the journal's reasoning_tokens should be > 0 on B/C.
        if reasoning == "none":
            extra["reasoning"] = {"enabled": False}
        elif reasoning == "low":
            extra["reasoning"] = {"effort": "low"}
        kw["extra_body"] = extra
    resp = await client.chat.completions.create(**kw)
    used = _field(resp, "provider")
    if isinstance(used, str) and used:
        tally.providers += (used,)
    _count(route, spend, tally, getattr(resp, "usage", None), openai_shape=True)
    choices = getattr(resp, "choices", None)
    if not choices:
        raise _NoChoices("response has no choices")
    choice = choices[0]
    msg = getattr(choice, "message", None)
    finish = getattr(choice, "finish_reason", None)
    if getattr(msg, "refusal", None) or finish == "content_filter":
        return _Landed(None, finish, refusal=True)
    return _Landed((getattr(msg, "content", None) or "").strip(), finish)


async def _anthropic_once(
    client: Any,
    route: str,
    system: str | None,
    user: str,
    *,
    reasoning: str,
    max_tokens: int,
    timeout: float,
    spend: Spend,
    tally: _Tally,
) -> _Landed:
    kw: dict[str, Any] = {
        "timeout": timeout,
        "model": route,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled" if reasoning == "none" else "adaptive"},
        "messages": [{"role": "user", "content": user}],
    }
    if reasoning == "low":
        kw["output_config"] = {"effort": "low"}
    if system is not None:
        kw["system"] = system
    async with client.messages.stream(**kw) as stream:
        resp = await stream.get_final_message()
    _count(route, spend, tally, getattr(resp, "usage", None), openai_shape=False)
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        return _Landed(None, stop, refusal=True)
    text = "".join(
        b.text for b in getattr(resp, "content", []) if getattr(b, "type", None) == "text"
    ).strip()
    return _Landed(text, stop)


async def _one(
    client: Any,
    transport: str,
    route: str,
    system: str | None,
    user: str,
    *,
    reasoning: str,
    max_tokens: int,
    timeout: float,
    rpm: float,
    spend: Spend,
    tally: _Tally,
    reservation: _Reservation | None = None,
) -> _Landed:
    """One prompt: tenacity retries transient failures; an empty reply cut at ``max_tokens``
    is re-asked with double the budget, up to ``TEXT_BUDGET_CEILING`` (wsbench's rule) and at
    most ``MAX_BUDGET_DOUBLINGS`` times, each re-ask first reserved on ``reservation`` (which
    raises ``TextLLMConfigError`` if it would cross a spend limit). Raises
    ``TextLLMConfigError`` on a fatal status; any other exception propagates after the
    retries."""
    # detokenised lens readouts can contain lone surrogates, which every API rejects
    user = user.encode("utf-8", "replace").decode("utf-8")
    if system is not None:
        system = system.encode("utf-8", "replace").decode("utf-8")

    def wait(rs: Any) -> float:
        exc = rs.outcome.exception() if rs.outcome is not None else None
        return _backoff(rs.attempt_number - 1, _status(exc) if exc else None)

    def before_sleep(_rs: Any) -> None:
        spend.retries += 1

    budget = max_tokens
    doublings = 0
    while True:
        landed: _Landed | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(ATTEMPTS),
            wait=wait,
            retry=retry_if_exception(_transient),
            before_sleep=before_sleep,
            reraise=True,
        ):
            with attempt:
                tally.attempts += 1
                await _pace(rpm)
                try:
                    if transport == "anthropic":
                        landed = await _anthropic_once(
                            client,
                            route,
                            system,
                            user,
                            reasoning=reasoning,
                            max_tokens=budget,
                            timeout=timeout,
                            spend=spend,
                            tally=tally,
                        )
                    else:
                        landed = await _openai_once(
                            client,
                            transport,
                            route,
                            system,
                            user,
                            reasoning=reasoning,
                            max_tokens=budget,
                            timeout=timeout,
                            spend=spend,
                            tally=tally,
                        )
                except Exception as e:
                    if _fatal(e):
                        raise TextLLMConfigError(f"{type(e).__name__}: {str(e)[:300]}") from e
                    raise
        assert landed is not None
        if (
            landed.text == ""
            and landed.stop in ("max_tokens", "length")
            and budget < TEXT_BUDGET_CEILING
            and doublings < MAX_BUDGET_DOUBLINGS
        ):
            budget = min(budget * 2, TEXT_BUDGET_CEILING)
            doublings += 1
            if reservation is not None:
                reservation.reask(user, budget)  # raises before the call if over a limit
            continue
        return landed


# ---------------------------------------------------------------- the batch


_ABORTED = object()  # an unsent prompt of an aborted batch: no on_result, no journal line


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


async def _complete_async(
    prompts: list[str],
    *,
    route: str,
    transport: str,
    key: str,
    system: str | None,
    max_tokens: int,
    reasoning: str,
    on_result: Callable[[int, str | None], None],
    concurrency: int,
    rpm: float,
    timeout: float,
    spend: Spend,
    journal: Path,
    purpose: str,
    reservation: _Reservation | None = None,
) -> None:
    client = _make_client(transport, key)
    sem = asyncio.Semaphore(concurrency)
    journal.parent.mkdir(parents=True, exist_ok=True)
    fh = journal.open("a", encoding="utf-8")

    def write(
        i: int, tally: _Tally, status: str, text: str | None, stop: str | None, error: str | None
    ) -> None:
        row = {
            "utc": emx_spend.utc_now(),
            "purpose": purpose,
            "model": route,
            "transport": transport,
            "reasoning": reasoning,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "index": i,
            "prompt_sha": _sha((system or "") + "\0" + prompts[i]),
            "status": status,
            "stop": stop,
            "attempts": tally.attempts,
            "responses": tally.responses,
            "input_tokens": tally.input_tokens,
            "output_tokens": tally.output_tokens,
            "usd": round(tally.usd, 8),
            "provider_cost": tally.provider_cost,
            "cost_source": tally.cost_source(),
            "provider": tally.provider(),
            "reasoning_tokens": tally.reasoning_tokens,
            "error": error,
            "text": text,
        }
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()

    aborted = [False]  # set on a fatal error: queued prompts are never sent

    async def one(i: int) -> tuple[int, str | None | object]:
        tally = _Tally()
        async with sem:
            if aborted[0]:
                return i, _ABORTED
            try:
                landed = await _one(
                    client,
                    transport,
                    route,
                    system,
                    prompts[i],
                    reasoning=reasoning,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    rpm=rpm,
                    spend=spend,
                    tally=tally,
                    reservation=reservation,
                )
            except TextLLMConfigError as e:
                aborted[0] = True
                write(i, tally, "fatal", None, None, str(e)[:300])
                raise
            except Exception as e:  # noqa: BLE001 - a per-call failure lands as None, by contract
                spend.errors += 1
                print(f"  textllm error: {route} {type(e).__name__}: {str(e)[:200]}")
                write(i, tally, "error", None, None, f"{type(e).__name__}: {str(e)[:300]}")
                return i, None
        if landed.refusal:
            spend.refusals += 1
            print(f"  textllm refusal: {route}")
            write(i, tally, "refusal", None, landed.stop, None)
            return i, None
        write(i, tally, "ok", landed.text, landed.stop, None)
        return i, landed.text

    tasks = [asyncio.ensure_future(one(i)) for i in range(len(prompts))]
    try:
        for fut in asyncio.as_completed(tasks):
            i, text = await fut
            if text is not _ABORTED:
                on_result(i, text)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        fh.close()
        await client.close()


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def complete(
    prompts: list[str],
    *,
    route: str,
    system: str | None = None,
    max_tokens: int,
    reasoning: str,
    on_result: Callable[[int, str | None], None],
    concurrency: int = 64,
    rpm: float = 240.0,
    timeout: float = 600.0,
    spend: Spend | None = None,
    purpose: str = "textllm",
) -> Spend:
    """Run free-text prompts on ``route``; ``on_result(i, text | None)`` once per prompt as it
    lands. Returns the (same, incremented) ``spend``. ``purpose`` names the journal file
    (``CALLS_DIR/<purpose>__<model>.jsonl``) and tags the ledger entry. See the module docstring
    for the error classes."""
    spend = spend if spend is not None else Spend()
    transport = transport_of(route)
    if reasoning not in REASONING:
        raise TextLLMConfigError(f"reasoning must be one of {REASONING}, got {reasoning!r}")
    if concurrency < 1 or rpm <= 0 or timeout <= 0 or max_tokens < 1:
        raise TextLLMConfigError(
            f"need concurrency >= 1, rpm > 0, timeout > 0, max_tokens >= 1 "
            f"(got {concurrency}, {rpm}, {timeout}, {max_tokens})"
        )
    if not prompts:
        return spend
    if _in_running_loop():
        raise TextLLMConfigError("textllm.complete called inside a running event loop")
    key = api_key(route)
    projected = project_usd(prompts, route=route, system=system, max_tokens=max_tokens)
    print(f"textllm: {len(prompts)} prompts on {route} ({reasoning}), projected ${projected:.4f}")
    try:
        emx_spend.check_total(projected, "judge", exp=C.EXP)
    except emx_spend.BudgetExceeded as e:
        raise TextLLMConfigError(str(e)) from e
    usd0, calls0 = spend.usd, spend.calls
    try:
        asyncio.run(
            _complete_async(
                prompts,
                route=route,
                transport=transport,
                key=key,
                system=system,
                max_tokens=max_tokens,
                reasoning=reasoning,
                on_result=on_result,
                concurrency=concurrency,
                rpm=rpm,
                timeout=timeout,
                spend=spend,
                journal=journal_path(purpose, route),
                purpose=purpose,
                reservation=_Reservation(route, projected, system),
            )
        )
    finally:
        calls, usd = spend.calls - calls0, spend.usd - usd0
        if calls:
            emx_spend.record(
                "judge",
                route,
                calls,
                max(usd, 0.0),
                note=f"[I9 {purpose}] {len(prompts)} prompts, reasoning={reasoning}",
                exp=C.EXP,
            )
    return spend


# ---------------------------------------------------------------- the wsbench adapter


def as_wsbench_stream_text(
    route: str, *, effort_by_thinking: dict[bool, str], purpose: str = "wsjudge"
) -> Callable[..., Spend]:
    """A drop-in for ``wsbench.llm.stream_text`` that sends every call to ``route``.

    ``model`` (the vendored code passes ``args.judge.model``, I9's cache label) is ignored;
    ``thinking`` picks the reasoning setting from ``effort_by_thinking`` (Stage A is called with
    ``thinking=False``, Stages B and C with ``thinking=True``); ``concurrency``, ``rpm``,
    ``timeout`` and ``spend`` are forwarded to :func:`complete` unchanged."""
    transport_of(route)
    if set(effort_by_thinking) != {False, True}:
        raise TextLLMConfigError(
            f"effort_by_thinking needs keys False and True: {effort_by_thinking}"
        )
    bad = [v for v in effort_by_thinking.values() if v not in REASONING]
    if bad:
        raise TextLLMConfigError(f"effort_by_thinking values must be in {REASONING}: {bad}")
    efforts = dict(effort_by_thinking)

    def stream_text(
        prompts: list[str],
        *,
        model: str,
        on_result: Callable[[int, str | None], None],
        thinking: bool,
        max_tokens: int,
        concurrency: int = 64,
        rpm: float = 240.0,
        timeout: float = 600.0,
        spend: Spend | None = None,
    ) -> Spend:
        del model  # the cache label; the call goes to `route`
        return complete(
            prompts,
            route=route,
            max_tokens=max_tokens,
            reasoning=efforts[bool(thinking)],
            on_result=on_result,
            concurrency=concurrency,
            rpm=rpm,
            timeout=timeout,
            spend=spend,
            purpose=purpose,
        )

    return stream_text
