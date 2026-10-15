"""A small, generic async LLM client for probes: a list of ``Call`` in, ``{key: Result}`` out.

Built for P03 (probes/P03-text-proof-interp-items/log.md, design item 7), reusable by any probe.
Ideas reused from I9's client (``src/emx/i9/textllm.py``), which is bound to I9's contracts and
sends no temperature or seed: OpenRouter's billed ``usage.cost`` (``usage: {include: true}``),
provider pinning (``emx.i9.contracts.PROVIDER``, plus ``require_parameters``), and its split of
transient from fatal errors.

- **Routes.** ``gpt-6-luna`` goes to OpenAI (``max_completion_tokens``, ``reasoning_effort``);
  any other route is an OpenRouter model id (``max_tokens``, ``extra_body.reasoning``). A route
  must be priced in ``PRICE_PER_MTOK``. ``reasoning`` is ``"none" | "low" | "high" | "default"``, as in
  textllm (``"default"`` sends no reasoning field). ``temperature``/``seed`` are sent only when
  set, except to routes in ``NO_SAMPLING_PARAMS`` (see the note there).
- **Cache.** ``cache`` is a JSONL file, one ``Result`` row (plus ``user_sha``/``system_sha`` and
  ``req_sha``, the hash of every request setting) per landed call. Rows with ``error is None``
  are returned without a call, so a batch resumes; a row whose route or prompt hashes differ from
  the new ``Call``, or a reused row whose ``req_sha`` differs, raises ``ValueError``.
- **Spend** (an API reservation in the ledger, ``emx.spend.reserve_api``; P03 review r1 item 1.3).
  Before any call, ``reserve_api(project_usd(pending) * RETRY_MARGIN, note="probe_llm:<purpose>")``
  admits the batch against the cap and holds that amount as liability, so concurrent processes
  can't spend the same headroom (``BudgetExceeded`` propagates). It is marked submitted before
  the first HTTP call. Every attempt, retries included, must fit the reservation (known spend +
  the worst case of attempts in flight, at ``project_usd`` prices); one that doesn't lands as an
  error without a call. After the batch, also when it raises: one ``emx.spend.record`` per route
  under the reservation (OpenRouter's billed cost when reported, else ``PRICE_PER_MTOK``), then
  ``settle_api``, which also charges attempts of uncertain outcome (timeouts, connection errors)
  at their worst case and releases the rest. If the process dies, the open submitted reservation
  keeps counting in full; the next ``run_calls`` with that purpose refuses until
  ``emx.spend.recover_uncertain(exp, "probe_llm:<purpose>")`` settles it at the full amount.
- **Errors.** 408/409/429/5xx, timeouts, connection errors and a reply without choices are
  retried (up to ``max_retries``) with jittered backoff. 400/404 and other errors land as
  ``Result(error=...)`` without retry. 401/402/403 abort the batch: queued calls are not sent,
  and ``FatalAPIError`` is raised after the spend is recorded.

Keys (``OPENAI_API_KEY``, ``OPENROUTER_API_KEY``) come from the repo's ``.env``; never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
import random
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from emx import spend as emx_spend
from emx.i9 import contracts as i9c

OPENROUTER = "https://openrouter.ai/api/v1"
LUNA = i9c.LUNA
GEMMA = "google/gemma-4-31b-it"
OPENAI_ROUTES = frozenset({LUNA})
REASONING = ("none", "low", "high", "default")  # "high": forced thinking (P03 F2)
HIGH_REASONING_BUDGET = 3000

# Gemma 4 31B (OpenRouter /api/v1/models and /endpoints, 2026-10-11): the listed $0.09 / $0.34 is
# DeepInfra's fp4 "turbo" endpoint. We exclude fp4 (as I9 does for DeepSeek) and cap at
# $0.15 / $0.40, which admits the bf16/fp8 endpoints (Parasail fp8, Crusoe, Novita bf16).
# The table price is that ceiling, so projections and unbilled spend never undercount.
# Claude Haiku 4.5 (P16's Brauer judge): Anthropic's list price, $1 / $5 per MTok (P13 plan).
HAIKU = "anthropic/claude-haiku-4.5"
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {**i9c.PRICE_PER_MTOK, GEMMA: (0.15, 0.40),
                                                  HAIKU: (1.0, 5.0)}
PROVIDER: dict[str, dict] = {
    **copy.deepcopy(i9c.PROVIDER),
    GEMMA: {"quantizations": ["fp8", "bf16"], "max_price": {"prompt": 0.15, "completion": 0.40}},
}
# OpenRouter 400s on reasoning={"enabled": False} here ("Reasoning is mandatory for this endpoint",
# smoke call 2026-10-11): such routes need reasoning="default" (I1a's monitor) or "low".
REASONING_MANDATORY: frozenset[str] = frozenset({i9c.GEMINI, i9c.GLM})  # GLM: P03 selection run
# Routes that reject temperature/seed. Smoke calls 2026-10-11: luna (reasoning_effort="none"),
# Gemini (reasoning default) and Gemma all accepted temperature=0 and seed=0; luna with
# reasoning "low" is untested (OpenAI reasoning models may reject temperature there).
# Sonnet 5: no OpenRouter endpoint lists temperature or seed (endpoints API, 2026-10-11), so with
# require_parameters every call 404s; it runs at the provider default.
NO_SAMPLING_PARAMS: frozenset[str] = frozenset({i9c.SONNET})

# Input tokens are bounded by UTF-8 bytes: a byte-level BPE token covers at least one byte, so
# bytes >= tokens for every route (review r2: chars / 3 under-reserved dense text). Plus a
# generous chat-template overhead.
OVERHEAD_TOKENS = 64
RETRY_MARGIN = 1.25  # reservation = projection x this, to cover billed retries (no-choice replies)
_TRANSIENT = (408, 409, 429, 500, 502, 503, 504, 529)
_ABORT = (401, 402, 403)


class FatalAPIError(RuntimeError):
    """An auth or billing failure (401/402/403) that would fail every call."""


class _NoChoices(ValueError):
    """A 200 response without choices: transient."""


@dataclass(frozen=True)
class Call:
    key: str
    route: str
    user: str
    system: str | None = None
    temperature: float | None = None
    seed: int | None = None
    max_tokens: int = 256
    reasoning: str = "none"
    json_mode: bool = False


@dataclass
class Result:
    key: str
    route: str
    text: str | None
    error: str | None
    input_tokens: int
    output_tokens: int
    usd: float
    cost_source: str
    provider: str | None
    finish: str | None


_RESULT_FIELDS = tuple(f.name for f in fields(Result))


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def price_usd(route: str, input_tokens: float, output_tokens: float) -> float:
    if route not in PRICE_PER_MTOK:
        raise ValueError(f"unknown route {route!r}; priced: {sorted(PRICE_PER_MTOK)}")
    p_in, p_out = PRICE_PER_MTOK[route]
    return (input_tokens * p_in + output_tokens * p_out) / 1e6


def _req_sha(c: Call) -> str:
    """Hash of everything that shapes the request (the key excepted), for cache reuse."""
    req = {f.name: getattr(c, f.name) for f in fields(Call) if f.name != "key"}
    return hashlib.sha256(json.dumps(req, sort_keys=True).encode("utf-8", "replace")).hexdigest()


def _worst_usd(c: Call) -> float:
    n_in = len(((c.system or "") + c.user).encode("utf-8")) + OVERHEAD_TOKENS
    return price_usd(c.route, n_in, c.max_tokens)


def project_usd(calls: list[Call]) -> float:
    """Worst-case cost: input = UTF-8 bytes + overhead, output = ``max_tokens`` per call."""
    return sum(_worst_usd(c) for c in calls)


class _Budget:
    """In-process bound on one reservation: known spend + the worst case of attempts in flight
    (+ attempts of uncertain outcome, kept at their worst case) never passes ``cap``. One event
    loop, no await between check and update, so no lock is needed."""

    def __init__(self, cap: float):
        self.cap = cap
        self.committed = 0.0
        self.uncertain = 0.0

    def acquire(self, worst: float) -> bool:
        if self.committed + worst > self.cap + 1e-12:
            return False
        self.committed += worst
        return True


# ---------------------------------------------------------------- seams (tests patch these)


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(emx_spend.REPO_ROOT / ".env", override=False)


def _make_client(transport: str, key: str) -> Any:
    from openai import AsyncOpenAI

    base = OPENROUTER if transport == "openrouter" else None
    return AsyncOpenAI(api_key=key, base_url=base, max_retries=0)


def _backoff(attempt: int, status: int | None) -> float:
    cap = 60.0 if status == 429 else 20.0
    return min(cap, 2.0 * 2**attempt) * (0.5 + random.random())


# ---------------------------------------------------------------- one call


def _transport(route: str) -> str:
    return "openai" if route in OPENAI_ROUTES else "openrouter"


def _status(e: BaseException) -> int | None:
    s = getattr(e, "status_code", None)
    return s if isinstance(s, int) else None


def _transient(e: BaseException) -> bool:
    name = type(e).__name__
    return (
        _status(e) in _TRANSIENT or isinstance(e, (_NoChoices, asyncio.TimeoutError))
        or "Timeout" in name or "Connection" in name
    )


def _field(obj: Any, name: str) -> Any:
    """A field the SDK may keep only in pydantic's ``model_extra`` (``usage.cost``, ``provider``)."""
    v = getattr(obj, name, None)
    return v if v is not None else (getattr(obj, "model_extra", None) or {}).get(name)


def _request(c: Call, timeout: float) -> dict[str, Any]:
    messages = ([{"role": "system", "content": c.system}] if c.system is not None else []) + [
        {"role": "user", "content": c.user}
    ]
    kw: dict[str, Any] = {"model": c.route, "messages": messages, "timeout": timeout}
    if c.route not in NO_SAMPLING_PARAMS:
        if c.temperature is not None:
            kw["temperature"] = c.temperature
        if c.seed is not None:
            kw["seed"] = c.seed
    if c.json_mode:
        kw["response_format"] = {"type": "json_object"}
    if _transport(c.route) == "openai":
        kw["max_completion_tokens"] = c.max_tokens
        if c.reasoning != "default":
            kw["reasoning_effort"] = c.reasoning
        return kw
    kw["max_tokens"] = c.max_tokens
    extra: dict[str, Any] = {
        "usage": {"include": True},
        "provider": {**copy.deepcopy(PROVIDER.get(c.route, {})), "require_parameters": True},
    }
    if c.reasoning == "none":
        extra["reasoning"] = {"enabled": False}
    elif c.reasoning == "low":
        extra["reasoning"] = {"effort": "low"}
    elif c.reasoning == "high":
        # {"effort": "high"} let Sonnet 5's adaptive thinking skip thinking (P03 F2: 43 output
        # tokens per call); an explicit budget forces it (test call: 170-220 reasoning tokens).
        extra["reasoning"] = {"max_tokens": HIGH_REASONING_BUDGET}
    kw["extra_body"] = extra
    return kw


async def _one(
    client: Any, c: Call, timeout: float, max_retries: int, tally: list, budget: _Budget
) -> Result:
    """Send one call with retries; ``tally`` (the route's [responses, usd]) is updated per
    response. Each attempt must first fit ``budget``; one that doesn't lands as an error.
    Raises ``FatalAPIError`` on 401/402/403; any other failure lands as an error."""
    res = Result(c.key, c.route, None, None, 0, 0, 0.0, "table", None, None)
    kw = _request(c, timeout)
    worst = _worst_usd(c)
    sources: set[str] = set()
    for attempt in range(max_retries + 1):
        if not budget.acquire(worst):
            res.error = (f"ReservationExhausted: attempt {attempt + 1} not sent (reservation "
                         f"${budget.cap:.4f}, committed ${budget.committed:.4f})")
            return res
        landed = False
        try:
            resp = await client.chat.completions.create(**kw)
            usage = getattr(resp, "usage", None)
            tin = int(getattr(usage, "prompt_tokens", 0) or 0)
            tout = int(getattr(usage, "completion_tokens", 0) or 0)
            cost = _field(usage, "cost") if usage is not None else None
            usd = float(cost) if cost is not None else price_usd(c.route, tin, tout)
            sources.add("billed" if cost is not None else "table")
            res.cost_source = "billed" if sources == {"billed"} else "table"
            res.input_tokens += tin
            res.output_tokens += tout
            res.usd += usd
            tally[0] += 1
            tally[1] += usd
            budget.committed += usd - worst  # the attempt's worst case becomes its actual cost
            landed = True
            used = _field(resp, "provider")
            res.provider = used if isinstance(used, str) and used else res.provider
            choices = getattr(resp, "choices", None)
            if not choices:
                raise _NoChoices("response has no choices")
            msg = getattr(choices[0], "message", None)
            res.finish = getattr(choices[0], "finish_reason", None)
            res.text, res.error = (getattr(msg, "content", None) or "").strip(), None
            return res
        except Exception as e:  # classified below; one call never fails the batch
            if not landed:
                if _status(e) is None:  # timeout/connection: may have been billed
                    budget.uncertain += worst
                else:  # the provider answered with an error status: not billed
                    budget.committed -= worst
            res.error = f"{type(e).__name__}: {str(e)[:300]}"
            if _status(e) in _ABORT:
                raise FatalAPIError(f"{c.route}: {res.error}") from e
            if not _transient(e) or attempt == max_retries:
                return res
            await asyncio.sleep(_backoff(attempt, _status(e)))
    return res


# ---------------------------------------------------------------- the batch


def _read_cache(cache: Path) -> dict[str, dict]:
    if not cache.exists():
        return {}
    rows: dict[str, dict] = {}
    for line in cache.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["key"]] = row  # the latest row of a key wins
    return rows


async def _run(
    pending: list[Call], cache: Path, concurrency: int, timeout: float, max_retries: int,
    tallies: dict[str, list], budget: _Budget,
) -> dict[str, Result]:
    keys = {
        t: os.environ.get(env, "")
        for t, env in (("openai", "OPENAI_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"))
    }
    needed = {_transport(c.route) for c in pending}
    missing = [t for t in needed if not keys[t]]
    if missing:
        raise FatalAPIError(f"missing API key for transport(s) {missing}")
    clients = {t: _make_client(t, keys[t]) for t in needed}
    sem = asyncio.Semaphore(concurrency)
    aborted: list[BaseException] = []
    out: dict[str, Result] = {}
    cache.parent.mkdir(parents=True, exist_ok=True)

    async def one(c: Call) -> None:
        async with sem:
            if aborted:
                return
            tally = tallies.setdefault(c.route, [0, 0.0])
            try:
                res = await _one(
                    clients[_transport(c.route)], c, timeout, max_retries, tally, budget
                )
            except FatalAPIError as e:
                aborted.append(e)
                return
        out[c.key] = res
        row = {**asdict(res), "user_sha": _sha(c.user), "system_sha": _sha(c.system),
               "req_sha": _req_sha(c)}
        with cache.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    try:
        await asyncio.gather(*(one(c) for c in pending))
    finally:
        for client in clients.values():
            await client.close()
    if aborted:
        raise aborted[0]
    return out


def run_calls(
    calls: list[Call], *, exp: str, cache: Path, purpose: str, concurrency: int = 64,
    timeout: float = 120.0, max_retries: int = 5, dry_run: bool = False, legacy_ok: bool = False,
) -> dict[str, Result]:
    """Run ``calls`` (cached ones are returned without a call); returns ``{key: Result}``.
    ``legacy_ok``: reuse successful cached rows that predate ``req_sha`` (their settings can't be
    checked); without it such a row raises (review r2)."""
    keys = [c.key for c in calls]
    if len(set(keys)) != len(keys):
        dup = sorted({k for k in keys if keys.count(k) > 1})[:5]
        raise ValueError(f"duplicate call keys: {dup}")
    bad = [c.reasoning for c in calls if c.reasoning not in REASONING]
    if bad:
        raise ValueError(f"reasoning must be one of {REASONING}, got {bad[0]!r}")
    forced = sorted({c.route for c in calls if c.reasoning == "none" and c.route in
                     REASONING_MANDATORY})
    if forced:
        raise ValueError(f"{forced} cannot disable reasoning: use reasoning='default' or 'low'")
    cache = Path(cache)
    cached = _read_cache(cache)
    done: dict[str, Result] = {}
    pending: list[Call] = []
    for c in calls:
        row = cached.get(c.key)
        if row is not None:
            if (row.get("route"), row.get("user_sha"), row.get("system_sha")) != (
                c.route, _sha(c.user), _sha(c.system)
            ):
                raise ValueError(f"cache key collision for {c.key!r} in {cache}")
            if row.get("error") is None:
                # Rows written before req_sha existed (P03's committed caches) carry none: they
                # are reused on the route/prompt check alone only with legacy_ok. An errored row
                # is re-sent with the new settings whatever its req_sha.
                if row.get("req_sha") is None and not legacy_ok:
                    raise ValueError(
                        f"cached row for {c.key!r} in {cache} has no req_sha (its settings can't "
                        "be checked); pass legacy_ok=True to reuse it, or use a new cache file"
                    )
                if row.get("req_sha") not in (None, _req_sha(c)):
                    raise ValueError(
                        f"cached row for {c.key!r} in {cache} was made with other request "
                        "settings (temperature/seed/max_tokens/reasoning/json_mode); use a new "
                        "key or cache file"
                    )
                done[c.key] = Result(**{f: row[f] for f in _RESULT_FIELDS})
                continue
        pending.append(c)
    projected = project_usd(pending)
    print(f"probe_llm [{exp} {purpose}]: {len(done)} cached, {len(pending)} pending, "
          f"projected ${projected:.4f}")
    if dry_run:
        return {}
    if not pending:
        return done
    note = f"probe_llm:{purpose}"
    stale = [rid for rid, s in emx_spend.open_reservations(exp).items() if s["note"] == note]
    if stale:
        raise emx_spend.ReservationError(
            f"{exp}: reservation {stale[0]} for {note!r} is still open (a crashed or concurrent "
            f"batch). If no batch with this purpose is running, settle it first with "
            f"emx.spend.recover_uncertain({exp!r}, {note!r}) (submitted; charged at its full "
            f"reserved amount) or emx.spend.release_api({stale[0]!r}) (never submitted)"
        )
    reserve = math.ceil(projected * RETRY_MARGIN * 1e6) / 1e6  # up to the ledger's 1e-6 grain
    rid = emx_spend.reserve_api(exp, reserve, note)  # BudgetExceeded propagates
    budget = _Budget(reserve)
    tallies: dict[str, list] = {}
    try:
        emx_spend.mark_submitted(rid, exp=exp)
        _load_env()
        done.update(asyncio.run(
            _run(pending, cache, concurrency, timeout, max_retries, tallies, budget)
        ))
    finally:
        known = 0.0
        for route, (n, usd) in sorted(tallies.items()):
            if n:
                known += max(usd, 0.0)
                emx_spend.record(
                    "judge", route, n, max(usd, 0.0),
                    note=f"[{exp} {purpose}] {n} responses, {len(pending)} calls sent", exp=exp,
                    reservation_id=rid,
                )
        # committed = known spend + attempts of uncertain outcome (and any left in flight by an
        # interrupt) at their worst case; the settle row charges that excess, if any.
        emx_spend.settle_api(
            rid, total_usd=max(known, budget.committed), exp=exp,
            note=f"[{exp} {purpose}] settled: ${known:.4f} known + ${budget.uncertain:.4f} "
                 f"uncertain of ${reserve:.4f} reserved",
        )
    errors = sum(1 for c in pending if c.key in done and done[c.key].error is not None)
    if errors:
        print(f"probe_llm [{exp} {purpose}]: {errors} of {len(pending)} calls landed as errors")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one tiny paid call ('Say OK')")
    ap.add_argument("--route", default="google/gemini-3.8-flash")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reasoning", default="none", choices=REASONING)
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--exp", default="P03")
    args = ap.parse_args(argv)
    if not args.smoke:
        ap.print_help()
        return 0
    cache = emx_spend.REPO_ROOT / "probes/P03-text-proof-interp-items/results/api/smoke.jsonl"
    tag = f"smoke:{args.route}:t={args.temperature}:s={args.seed}:r={args.reasoning}"
    call = Call(tag, args.route, "Say OK", temperature=args.temperature, seed=args.seed,
                max_tokens=args.max_tokens, reasoning=args.reasoning)
    res = run_calls([call], exp=args.exp, cache=cache, purpose="smoke", concurrency=1,
                    max_retries=1)
    print(json.dumps(asdict(res[call.key]), indent=1))
    return 0 if res[call.key].error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
