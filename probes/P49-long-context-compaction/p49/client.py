"""P49 OpenRouter chat-completions client (unit C; plan v5.2 "Setup", "Automatic rules").

`chat(body, *, kind, chain_id, our_tokens)` sends one `contracts.make_body` request to
C.BASE_URL + C.CHAT_PATH and returns a `contracts.Response`:
  * validates the body first (model, provider pin, `transforms: []`); a bad body never leaves;
  * one process-wide limiter of C.API_CONCURRENCY requests in flight (`set_concurrency(n)`
    changes it at runtime; a slot is held only while a request is on the wire, not in backoff);
  * retries with C.BACKOFF_S on 429, 5xx, timeouts, connection errors, and 200 responses that
    carry an `error` object or no choices, up to C.MAX_ATTEMPTS, all within C.REQUEST_DEADLINE_S
    (counted from the first attempt's start, not from the wait for a slot) -> C.ApiError;
    any other 4xx -> C.ApiError at once, with the body text;
  * checkpoints one JSON line per attempt sent and per outcome to C.CHECKPOINTS (flock-locked;
    no message contents), and calls C.CALIB.observe(our_tokens, usage.prompt_tokens).

Cost is `usage.cost` (billed). If absent, the list price from C.PRICE_* is used and
`cost_from_usage` is False. A response served by a provider other than C.PROVIDER_NAME is
counted in PROVIDER_MISMATCH and logged loudly (with allow_fallbacks False it should not occur).

The key comes from the environment, else the worktree's .env, else the main checkout's .env
(as probes/P47-grader-belief-probe/p47/api.py load_key); only key names are logged. A
127.0.0.1 / localhost base URL (the mock server) uses a dummy key.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

from p49 import contracts as C

ENDPOINTS_PATH = "/api/v1/models/qwen/qwen3-coder/endpoints"
VERTEX_TAG = "google-vertex/us-south1"
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) p49-client/1"

# --- runtime configuration (tests and the mock override these) ------------------------------
_cfg = {"base_url": None, "backoff": None, "deadline_s": None, "max_attempts": None,
        "checkpoints": None}
PROVIDER_MISMATCH: Counter = Counter()
_stats = {"requests": 0, "errors": 0, "attempts": 0, "retries": 0}
_status_counts: Counter = Counter()


def configure(*, base_url: str | None = None, backoff: tuple[float, ...] | None = None,
              deadline_s: float | None = None, max_attempts: int | None = None) -> None:
    """Override C.BASE_URL / C.BACKOFF_S / C.REQUEST_DEADLINE_S / C.MAX_ATTEMPTS (None keeps
    the current value). Used by tests and by the mock end-to-end run."""
    for k, v in (("base_url", base_url), ("backoff", backoff), ("deadline_s", deadline_s),
                 ("max_attempts", max_attempts)):
        if v is not None:
            _cfg[k] = v


def reset_config() -> None:
    for k in _cfg:
        _cfg[k] = None


def base_url() -> str:
    return (_cfg["base_url"] or C.BASE_URL).rstrip("/")


def _backoff() -> tuple[float, ...]:
    return tuple(_cfg["backoff"] or C.BACKOFF_S)


def _deadline_s() -> float:
    return float(_cfg["deadline_s"] or C.REQUEST_DEADLINE_S)


def _max_attempts() -> int:
    return int(_cfg["max_attempts"] or C.MAX_ATTEMPTS)


def set_checkpoint_path(path: str | Path | None) -> None:
    _cfg["checkpoints"] = Path(path) if path else None


def checkpoint_path() -> Path:
    return _cfg["checkpoints"] or C.CHECKPOINTS


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host
    return host in ("127.0.0.1", "localhost", "::1")


# --- key --------------------------------------------------------------------------------------
def load_key() -> str:
    """OPENROUTER_API_KEY from env, the worktree .env, or the main checkout .env."""
    if _is_local(base_url()):
        return "mock-key"
    v = os.environ.get("OPENROUTER_API_KEY")
    if v:
        return v
    from dotenv import dotenv_values

    for env in (C.ROOT / ".env", C.MAIN_CHECKOUT / ".env"):
        if env.exists():
            v = dotenv_values(env).get("OPENROUTER_API_KEY")
            if v:
                os.environ["OPENROUTER_API_KEY"] = v
                print(f"[p49.client] loaded keys: OPENROUTER_API_KEY (from {env})", flush=True)
                return v
    raise C.ApiError("OPENROUTER_API_KEY not found")


# --- adjustable limiter and shared client (both rebuilt per event loop) -----------------------
class _Limiter:
    def __init__(self, n: int):
        self.limit = n
        self.inflight = 0
        self._cond: asyncio.Condition | None = None
        self._loop = None

    def _c(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond, self._loop, self.inflight = asyncio.Condition(), loop, 0
        return self._cond

    async def acquire(self) -> None:
        c = self._c()
        async with c:
            await c.wait_for(lambda: self.inflight < self.limit)
            self.inflight += 1

    async def release(self) -> None:
        c = self._c()
        async with c:
            self.inflight -= 1
            c.notify_all()

    async def set_limit(self, n: int) -> None:
        self.limit = n
        if self._cond is not None and self._loop is asyncio.get_running_loop():
            async with self._cond:
                self._cond.notify_all()


_limiter = _Limiter(C.API_CONCURRENCY)
_client: httpx.AsyncClient | None = None
_client_loop = None


def set_concurrency(n: int) -> None:
    """Change the in-flight request limit (takes effect for waiting requests at once when
    called from the running loop, else on the next acquire)."""
    if n < 1:
        raise ValueError("concurrency must be >= 1")
    _limiter.limit = n
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_limiter.set_limit(n))


def concurrency() -> int:
    return _limiter.limit


def _http() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop or _client.is_closed:
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=64)
        _client = httpx.AsyncClient(limits=limits, timeout=None,
                                    headers={"User-Agent": _USER_AGENT})
        _client_loop = loop
    return _client


async def aclose() -> None:
    global _client, _client_loop
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except RuntimeError:                       # its loop is gone
            pass
    _client, _client_loop = None, None


# --- checkpoints ------------------------------------------------------------------------------
def body_sha256(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _checkpoint(line: dict) -> None:
    p = checkpoint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(line, ensure_ascii=False) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(data)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_checkpoints(path: str | Path | None = None) -> list[dict]:
    """All checkpoint lines; a torn last line (crash mid-write) is skipped."""
    p = Path(path) if path else checkpoint_path()
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _cost_of(r: dict) -> float:
    """Cost an outcome line carries: a response's cost, or a failed attempt's (200) usage cost
    (billed, or at list price when usage had no cost)."""
    if r.get("event") in ("response", "retry"):
        return float(r.get("cost") or 0.0)
    return 0.0


def billed_total(path: str | Path | None = None, chain_ids: set[str] | None = None) -> float:
    """Sum of costs in the checkpoints: every successful response, plus any failed attempt
    whose response still reported usage. Optionally only `chain_ids`. Excludes requests with no
    known outcome; use `liability_total` for settlement."""
    return sum(_cost_of(r) for r in read_checkpoints(path)
               if chain_ids is None or r.get("chain_id") in chain_ids)


def _resolves(r: dict) -> bool:
    """Does this outcome line settle its attempt (cost known, possibly 0)?"""
    ev = r.get("event")
    if ev == "response":
        return True
    if ev == "retry":
        return not r.get("maybe_billed")
    if ev == "failure":
        return not r.get("final")         # the final summary line resolves no attempt itself
    if ev == "cancelled":
        return bool(r.get("not_sent"))
    return False


def _bound(r: dict) -> float:
    ours, mx = r.get("our_tokens") or 0, r.get("max_tokens") or 0
    return (ours * C.CALIB.c * C.PRICE_IN_PER_M + mx * C.PRICE_OUT_PER_M) / 1e6


def liability_total(path: str | Path | None = None, chain_ids: set[str] | None = None) -> dict:
    """Settlement view of the checkpoints (optionally only `chain_ids`):
    billed: the costs of every outcome line that carries one (any attempt);
    unresolved_bound: for each attempt sent with no settling outcome line (a crash, a cancel
      mid-request, a timeout or dropped connection that may have been billed), the worst case
      our_tokens * CALIB.c * input price + max_tokens * output price;
    total = billed + unresolved_bound. Attempts are matched by `rid`; lines written without
    one are matched on (chain_id, kind, body_sha256, attempt)."""
    rows = [r for r in read_checkpoints(path)
            if chain_ids is None or r.get("chain_id") in chain_ids]

    def key(r):
        return (r.get("chain_id"), r.get("kind"), r.get("body_sha256"), r.get("attempt"))

    resolved_rid = {r["rid"] for r in rows if r.get("rid") and _resolves(r)}
    resolved_key = Counter(key(r) for r in rows if not r.get("rid") and _resolves(r))
    billed = sum(_cost_of(r) for r in rows)
    bound, n = 0.0, 0
    for r in rows:
        if r.get("event") != "request":
            continue
        if r.get("rid"):
            if r["rid"] in resolved_rid:
                continue
        elif resolved_key[key(r)] > 0:
            resolved_key[key(r)] -= 1
            continue
        bound += _bound(r)
        n += 1
    return {"billed": billed, "unresolved_bound": bound, "n_unresolved": n,
            "total": billed + bound}


def failure_stats() -> dict:
    """This process's logical requests and the ones that ended in ApiError (the > 5% stop rule
    uses `frac`), plus attempts, retries and HTTP statuses seen."""
    n = _stats["requests"]
    return {**_stats, "frac": (_stats["errors"] / n) if n else 0.0,
            "statuses": dict(_status_counts), "provider_mismatch": dict(PROVIDER_MISMATCH)}


def reset_stats() -> None:
    for k in _stats:
        _stats[k] = 0
    _status_counts.clear()
    PROVIDER_MISMATCH.clear()


# --- validation and normalisation -------------------------------------------------------------
def validate_body(body: dict) -> None:
    if body.get("model") != C.MODEL:
        raise ValueError(f"model {body.get('model')!r} != {C.MODEL!r}")
    if body.get("provider") != C.PROVIDER:
        raise ValueError(f"provider {body.get('provider')!r} != {C.PROVIDER!r}")
    if "transforms" not in body or body["transforms"] != []:
        raise ValueError(f"transforms must be [] (got {body.get('transforms', '<missing>')!r})")
    if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
        raise ValueError(f"max_tokens missing or invalid: {body.get('max_tokens')!r}")


def _text(content) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") in ("text", None))
    return str(content)


def normalize_message(msg: dict) -> dict:
    """OpenAI assistant message: content str|None, tool_calls (omitted when none) with
    `arguments` a JSON string (kept verbatim when the provider already sent a string)."""
    out: dict = {"role": "assistant", "content": _text(msg.get("content"))}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if args is None:
            args = "{}"
        elif not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        calls.append({"id": tc.get("id") or f"call_{i}", "type": "function",
                      "function": {"name": fn.get("name"), "arguments": args}})
    if calls:
        out["tool_calls"] = calls
    return out


def list_cost(prompt_tokens: int | None, completion_tokens: int | None) -> float:
    return ((prompt_tokens or 0) * C.PRICE_IN_PER_M
            + (completion_tokens or 0) * C.PRICE_OUT_PER_M) / 1e6


class _Retry(Exception):
    """A retryable attempt failure. `maybe_billed`: the request may have reached the provider
    and been billed (a read timeout, a dropped connection), so its cost stays unresolved."""

    def __init__(self, why: str, status: int | None = None, data: dict | None = None,
                 maybe_billed: bool = False):
        super().__init__(why)
        self.status, self.data, self.maybe_billed = status, data, maybe_billed


def _usage_fields(data: dict | None) -> dict:
    u = (data or {}).get("usage") or {}
    pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
    billed = u.get("cost") is not None
    cost = float(u["cost"]) if billed else (list_cost(pt, ct) if (pt or ct) else None)
    return {"prompt_tokens": pt, "completion_tokens": ct, "cost": cost, "cost_billed": billed}


class _NotSent(Exception):
    """Cancelled while waiting for a slot: the request never left this process."""


# --- the call ---------------------------------------------------------------------------------
async def _attempt(body: dict, key: str, timeout: float) -> tuple[dict, float]:
    url = base_url() + C.CHAT_PATH
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    t0 = time.monotonic()
    try:
        await _limiter.acquire()
    except asyncio.CancelledError as e:
        raise _NotSent() from e
    try:
        r = await asyncio.wait_for(_http().post(url, json=body, headers=headers), timeout)
    except TimeoutError as e:
        raise _Retry(f"timeout after {timeout:.1f}s", maybe_billed=True) from e
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:      # never reached the server
        raise _Retry(f"{type(e).__name__}: {e}") from e
    except httpx.HTTPError as e:
        raise _Retry(f"{type(e).__name__}: {e}", maybe_billed=True) from e
    finally:
        await _limiter.release()
    lat = time.monotonic() - t0
    _status_counts[r.status_code] += 1
    if r.status_code == 429 or r.status_code >= 500:
        raise _Retry(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    if r.status_code != 200:
        raise C.ApiError(f"HTTP {r.status_code}: {r.text[:2000]}")
    try:
        data = r.json()
    except ValueError as e:
        raise _Retry(f"unparsable 200 body: {r.text[:300]}", 200, maybe_billed=True) from e
    if data.get("error"):
        raise _Retry(f"200 with error: {json.dumps(data['error'])[:300]}", 200, data)
    if not data.get("choices"):
        raise _Retry("200 with no choices", 200, data)
    return data, lat


def _api_error(msg: str, spent: float) -> C.ApiError:
    e = C.ApiError(msg)
    e.cost_usd = spent                    # billed cost of the failed attempts (loop.py reads it)
    return e


async def chat(body: dict, *, kind: str, chain_id: str, our_tokens: int) -> C.Response:
    """Response.cost_usd is the billed cost summed over every attempt of this call (a failed
    attempt's 200 response may carry a cost); cost_from_usage is False if any attempt was
    priced at list price. A raised ApiError carries the same sum as `cost_usd`."""
    validate_body(body)
    key = load_key()
    sha = body_sha256(body)
    base = {"kind": kind, "chain_id": chain_id, "body_sha256": sha, "our_tokens": our_tokens,
            "max_tokens": body.get("max_tokens")}
    _stats["requests"] += 1
    backoff = _backoff()
    n_max = _max_attempts()
    deadline = None
    last = "no attempt"
    attempt = 0
    spent, from_usage = 0.0, True
    while attempt < n_max:
        attempt += 1
        if deadline is None:
            deadline = time.monotonic() + _deadline_s()
        left = deadline - time.monotonic()
        if left <= 0:
            last = f"deadline {_deadline_s():.0f}s passed; last: {last}"
            break
        _stats["attempts"] += 1
        rid = uuid.uuid4().hex[:16]
        _checkpoint({"ts": _now(), "event": "request", **base, "attempt": attempt, "rid": rid})
        try:
            data, lat = await _attempt(body, key, left)
        except _NotSent:
            _checkpoint({"ts": _now(), "event": "cancelled", **base, "attempt": attempt,
                         "rid": rid, "not_sent": True})
            raise asyncio.CancelledError() from None
        except _Retry as e:
            last = str(e)
            _stats["retries"] += 1
            u = _usage_fields(e.data)
            if u["cost"]:
                spent += u["cost"]
                from_usage = from_usage and u["cost_billed"]
            _checkpoint({"ts": _now(), "event": "retry", **base, "attempt": attempt, "rid": rid,
                         "status": e.status, "error": last, "maybe_billed": e.maybe_billed, **u})
            wait = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0
            if attempt >= n_max or time.monotonic() + wait >= deadline:
                if time.monotonic() + wait >= deadline:
                    last = f"deadline {_deadline_s():.0f}s would pass; last: {last}"
                break
            await asyncio.sleep(wait)
            continue
        except C.ApiError as e:
            _stats["errors"] += 1
            _checkpoint({"ts": _now(), "event": "failure", **base, "attempt": attempt,
                         "rid": rid, "error": str(e)[:2000]})
            raise _api_error(str(e), spent) from None
        resp = _success(data, lat, attempt, base, our_tokens, rid)
        resp.cost_usd = round(resp.cost_usd + spent, 9)
        resp.cost_from_usage = resp.cost_from_usage and from_usage
        return resp
    _stats["errors"] += 1
    _checkpoint({"ts": _now(), "event": "failure", **base, "attempt": attempt, "final": True,
                 "error": last[:2000]})
    raise _api_error(f"{kind} {chain_id}: failed after {attempt} attempts: {last}", spent)


def _success(data: dict, lat: float, attempt: int, base: dict, our_tokens: int,
             rid: str) -> C.Response:
    ch = data["choices"][0]
    msg = normalize_message(ch.get("message") or {})
    u = _usage_fields(data)
    provider = data.get("provider")
    if provider != C.PROVIDER_NAME:
        PROVIDER_MISMATCH[str(provider)] += 1
        print(f"[p49.client] !!! PROVIDER MISMATCH: {provider!r} != {C.PROVIDER_NAME!r} "
              f"({base['kind']} {base['chain_id']}, gen {data.get('id')})",
              file=sys.stderr, flush=True)
    C.CALIB.observe(our_tokens, u["prompt_tokens"])
    _checkpoint({"ts": _now(), "event": "response", **base, "attempt": attempt, "rid": rid,
                 "prompt_tokens": u["prompt_tokens"], "completion_tokens": u["completion_tokens"],
                 "cost": u["cost"] or 0.0, "cost_billed": u["cost_billed"],
                 "provider": provider, "latency_s": round(lat, 3),
                 "finish_reason": ch.get("finish_reason"), "gen_id": data.get("id"),
                 "n_tool_calls": len(msg.get("tool_calls") or []), "error": None})
    return C.Response(message=msg, finish_reason=ch.get("finish_reason"),
                      prompt_tokens=u["prompt_tokens"],
                      completion_tokens=u["completion_tokens"], cost_usd=u["cost"] or 0.0,
                      cost_from_usage=u["cost_billed"], provider=provider,
                      latency_s=lat, attempts=attempt, gen_id=data.get("id"))


# --- endpoints (free) -------------------------------------------------------------------------
def check_endpoints_listing(data: dict) -> dict:
    """The Vertex entry's fields we rely on; raises C.ApiError if it no longer matches."""
    eps = ((data or {}).get("data") or {}).get("endpoints") or []
    ent = next((e for e in eps if e.get("tag") == VERTEX_TAG), None)
    if ent is None:
        raise C.ApiError(f"no {VERTEX_TAG} endpoint in listing: {[e.get('tag') for e in eps]}")
    pr = ent.get("pricing") or {}
    p_in = round(float(pr.get("prompt", "nan")) * 1e6, 6)
    p_out = round(float(pr.get("completion", "nan")) * 1e6, 6)
    bad = []
    if ent.get("context_length") != C.WINDOW:
        bad.append(f"context_length {ent.get('context_length')} != {C.WINDOW}")
    if abs(p_in - C.PRICE_IN_PER_M) > 1e-9 or abs(p_out - C.PRICE_OUT_PER_M) > 1e-9:
        bad.append(f"prices {p_in}/{p_out} != {C.PRICE_IN_PER_M}/{C.PRICE_OUT_PER_M}")
    if bad:
        raise C.ApiError(f"{VERTEX_TAG} endpoint changed: " + "; ".join(bad))
    keys = ("tag", "provider_name", "context_length", "max_completion_tokens", "quantization",
            "status", "throughput_last_30m", "latency_last_30m", "uptime_last_30m",
            "uptime_last_1d")
    return {**{k: ent.get(k) for k in keys}, "price_in_per_m": p_in, "price_out_per_m": p_out}


async def endpoints_check() -> dict:
    r = await _http().get(base_url() + ENDPOINTS_PATH, timeout=30)
    if r.status_code != 200:
        raise C.ApiError(f"endpoints listing HTTP {r.status_code}: {r.text[:300]}")
    return check_endpoints_listing(r.json())
