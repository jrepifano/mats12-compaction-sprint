"""E9's LLM layer: one resumable, budgeted batch of JSON or free-text calls (prereg §4.3, §4.4,
§4.9, §7.3, Appendix A; implementation plan v4, unit B1 and the "E9 admission protocol";
revised after code review R1 r1, findings 4-7 and 13).

Ported, not imported: the client construction (`emx.judge_panel.make_client`, `load_env`; keys
`OPENAI_API_KEY`, `OPENROUTER_API_KEY` from `.env`, never logged), the semaphore concurrency and
retry-on-transient-error loop of `emx.shared.answer_pairs.run_calls`, and its reservation pattern
(reserve -> mark submitted -> bounded by headroom -> settle).

`run_batch(model_key, prompt_id, requests, out_path, *, mode, stage, note, ...)`:
- **Requests.** Each is `{"item_id": str, "fields": {placeholder: text}, "seed": int,
  "ref_chars": int | None}`. The prompt (`emx.e9.prompts.fill`) is sent as the single user
  message, no system message, with the request seed and the provider's default temperature
  (App. A). Request fields come from `contracts.MODELS[model_key]`: the OpenAI route sends
  `reasoning_effort` etc. at the top level with `max_completion_tokens`; OpenRouter sends the
  spec's `reasoning`/`provider` pin in `extra_body` with `max_tokens`. JSON prompts use the spec's
  `json_max_tokens`, rewrites its `text_max_tokens`.
- **Batch ownership** (R1 #6). An exclusive, non-blocking `fcntl.flock` on `<out_path>.lock` is
  held from before the cached rows are read and earlier reservations recovered until settlement.
  A second invocation on the same output raises `BatchLocked` without touching the ledger, so a
  live batch is never "recovered" by another process.
- **Tail repair** (R1 #7). Under that lock, an incomplete final line (a torn write, partial UTF-8
  included) is truncated before anything is appended; a complete final line without its newline
  gets one.
- **Resumable, bound to its inputs** (R1 #4). Rows are appended to `out_path` (JSONL, one
  `contracts.LLM_RESULT_KEYS` row per request outcome, plus `note`, `error` and `fingerprint`)
  and flushed as each request finishes. `fingerprint` = sha256 over the filled prompt text, the
  mode, the max-token limit and the complete request (`request_kwargs`: model id, seed, provider
  pin, reasoning settings). A request whose (model_key, prompt_id, item_id, seed) already has a
  terminal row (`TERMINAL`: ok, refused, empty, malformed) is not re-sent; its row is returned as
  it is. ANY row for that key carrying a different fingerprint (a changed document, prompt or
  request config) raises `FingerprintMismatch` before anything is reserved. "error" rows (API
  failures after retries, or a request cut off by the headroom) are re-sent by the next run.
- **Budget** (§7.3). Startup (under the lock) settles a crashed earlier run of the same `note` at
  its full reservation (`spend.recover_uncertain`) and releases an unsubmitted one. The pending
  requests' **upper bound** (`estimate_usd`) plus a small retry allowance (`reservation_usd`)
  is reserved through `reserve(stage, usd, note)`
  (default `emx.e9.budget.reserve_api`, the E9 admission protocol); `spend.mark_submitted`
  precedes the first call. Every call attempt, retries included, first claims its bound; the sum
  of finished calls' actual cost and in-flight bounds never exceeds `spend.reservation_headroom`.
  When the next call would, no further call is sent (those requests get unwritten "error" rows;
  rerun with a fresh reservation). At the end the reservation is settled at the actual total
  (`spend.settle_api`); on a crash or interrupt, at the actual total plus every in-flight call at
  its bound.
- **The per-call bound** (R1 #5): input tokens <= the UTF-8 byte count of the filled prompt plus
  `MESSAGE_OVERHEAD_TOKENS` for the chat framing (byte-level BPE and byte-fallback tokenizers
  never make a token shorter than one byte), output tokens <= the request's max-token limit (for
  GLM, reasoning tokens count inside `max_tokens`), each at the ModelSpec price. If a call's
  actual charge ever exceeds its bound, the batch stops sending, finishes and settles at the
  actual total, records the violation in the usage log, then raises `AccountingError`.
- **Actual cost** of a call: OpenRouter's `usage.cost` when present, else usage tokens x the
  ModelSpec prices (completion tokens include reasoning). A call with no usage, or a failed
  attempt of uncertain outcome (timeout, dropped connection), is charged its bound; an attempt
  answered by an error status (4xx, 5xx) is charged 0.
- **JSON mode** (A.1, A.4, A.5): `parse_json`, then `prompts.validate`; a malformed or invalid
  answer (blank included) is retried with the seed unchanged up to `contracts.JSON_RETRIES`
  times, then recorded "malformed" (App. A).
- **Text mode** (A.2, A.3): "refused" if the API reports a refusal (message `refusal`,
  `finish_reason == "content_filter"`, or a 400/403 moderation error), or if the output is
  shorter than `REFUSAL_MAX_RATIO * ref_chars` and matches a `REFUSAL_PATTERNS` regex (skipped
  when `ref_chars` is None); "empty" if blank; else "ok".

Review R1 r2:
- **No hidden retries** (#1): `make_client` builds the AsyncOpenAI client with `max_retries=0`;
  every HTTP submission is an accounted attempt (a response lost after submission surfaces as a
  timeout or connection error and is charged the call's bound).
- **Verified reads** (#2): `verified_rows` recomputes each request's fingerprint from the current
  fields and config and refuses a mismatch; `row_sha256` hashes a row for validation manifests.
- **Sticky safety flag** (#4): in JSON mode every attempt's raw text is kept (`attempt_texts`);
  for A.1, each attempt's parsed `minor_sexual` value is kept (`minor_sexual_values`, even from an
  otherwise malformed answer) and `safety_flag` is true if ANY attempt said true.

Usage log (R1 #13): one line per invocation in `<out stem>.usage.jsonl`, with `invocation`
(only the calls this invocation sent: calls, tokens, dollars, new statuses, the reservation and
settlement) kept apart from `batch` (status counts over every request of the batch, cached ones
included).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import random
import re
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from emx import spend
from emx.e9 import contracts as C
from emx.e9 import prompts

EXP = C.EXP
MODES: tuple[str, ...] = ("json", "text")
TERMINAL: tuple[str, ...] = ("ok", "refused", "empty", "malformed")
assert set(TERMINAL) | {"error"} == set(C.LLM_STATUSES)
MESSAGE_OVERHEAD_TOKENS = 64  # chat framing around the one user message (role tags, BOS)
API_ATTEMPTS = 5  # per JSON attempt, for transient API errors
RETRY_BASE_S = 1.0  # exponential backoff with jitter, capped at RETRY_MAX_S (tests set 0)
RETRY_MAX_S = 30.0
RETRYABLE_STATUS = (408, 409, 429)
FILTER_WORDS = ("moderation", "content_filter", "content filter", "content_policy",
                "content policy", "safety")
FATAL_STATUS = (401, 402, 403, 404)  # stop sending: every later call would fail the same way
_REFUSAL_RES = tuple(re.compile(p) for p in C.REFUSAL_PATTERNS)


class BatchLocked(RuntimeError):
    """Another process holds this batch's output lock."""


class FingerprintMismatch(RuntimeError):
    """A cached row for this request was made from different inputs or request settings."""


class AccountingError(RuntimeError):
    """A call cost more than its upper bound; the batch stopped and was settled at actual."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec(model_key: str) -> C.ModelSpec:
    if model_key not in C.MODELS:
        raise ValueError(f"unknown model key {model_key!r}; known {sorted(C.MODELS)}")
    return C.MODELS[model_key]


def max_tokens(spec: C.ModelSpec, mode: str) -> int:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return spec.json_max_tokens if mode == "json" else spec.text_max_tokens


def request_kwargs(spec: C.ModelSpec, prompt: str, seed: int, mode: str) -> dict:
    """The chat-completions request: one user message, the seed, the max-token limit and the
    spec's extra fields (top level for OpenAI, `extra_body` for OpenRouter)."""
    kw: dict = {"model": spec.model, "messages": [{"role": "user", "content": prompt}],
                "seed": int(seed)}
    if spec.route == "openai":
        kw["max_completion_tokens"] = max_tokens(spec, mode)
        kw.update(json.loads(json.dumps(spec.request)))
    elif spec.route == "openrouter":
        kw["max_tokens"] = max_tokens(spec, mode)
        if spec.request:
            kw["extra_body"] = json.loads(json.dumps(spec.request))
    else:
        raise ValueError(f"unknown route {spec.route!r}")
    return kw


def usd_of(spec: C.ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * spec.usd_per_mtok_in + completion_tokens * spec.usd_per_mtok_out) / 1e6


def call_bound_usd(spec: C.ModelSpec, prompt_bytes: int, mode: str) -> float:
    """Upper bound on one call's cost: input tokens <= UTF-8 bytes of the filled prompt +
    MESSAGE_OVERHEAD_TOKENS, output tokens <= the max-token limit (module docstring)."""
    return usd_of(spec, prompt_bytes + MESSAGE_OVERHEAD_TOKENS, max_tokens(spec, mode))


call_max_usd = call_bound_usd  # the name used before review R1


def fingerprint(spec: C.ModelSpec, prompt: str, seed: int, mode: str) -> str:
    """sha256 of everything that shapes a request: the filled prompt, the mode, the max-token
    limit and the full request (model id, seed, provider pin, reasoning settings)."""
    obj = {"prompt": prompt, "mode": mode, "max_tokens": max_tokens(spec, mode),
           "route": spec.route, "request": request_kwargs(spec, prompt, seed, mode)}
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
                          ).hexdigest()


def _check_mode(prompt_id: str, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if prompts.MODES[prompt_id] != mode:
        raise ValueError(f"{prompt_id} answers in {prompts.MODES[prompt_id]} mode, not {mode!r}")


def _prompt_bytes(prompt_id: str, fields: dict) -> int:
    return len(prompts.fill(prompt_id, **fields).encode("utf-8"))


def estimate_usd(model_key: str, prompt_id: str, requests: Sequence[dict], mode: str) -> float:
    """Upper bound on the cost of sending every request once (each call at its bound), rounded
    up to the ledger's $0.000001."""
    spec = _spec(model_key)
    _check_mode(prompt_id, mode)
    total = sum(call_bound_usd(spec, _prompt_bytes(prompt_id, r["fields"]), mode)
                for r in requests)
    return _ceil_usd(total)


RETRY_ALLOWANCE_FRACTION = 0.02  # of the pending requests, at least one


def reservation_usd(bounds: Sequence[float], mode: str) -> float:
    """What a batch reserves: every pending call at its bound, plus a retry allowance of
    max(1, ceil(2% of the requests)) requests x (JSON_RETRIES in JSON mode, else 1) extra calls at
    the largest bound, so a small batch can still retry a malformed answer or a timeout. Retries
    beyond what the headroom allows stop the batch (resumable), never overspend."""
    if not bounds:
        return 0.0
    n_extra = max(1, math.ceil(RETRY_ALLOWANCE_FRACTION * len(bounds)))
    per = C.JSON_RETRIES if mode == "json" else 1
    return _ceil_usd(sum(bounds) + n_extra * per * max(bounds))


def _ceil_usd(x: float) -> float:
    return math.ceil(round(x * 1e6, 3)) / 1e6


def parse_json(text: str, keys: Sequence[str]) -> dict | None:
    """The first JSON object in `text` (code fences and surrounding prose tolerated) that has every
    key in `keys`, or None."""
    if not text or not text.strip():
        return None
    s = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```\s*$", s, flags=re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", s):
        try:
            obj, _ = dec.raw_decode(s, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and all(k in obj for k in keys):
            return obj
    return None


def safety_value(text: str | None) -> bool | None:
    """The `minor_sexual` value of any JSON object in `text` (True for true or "true"), whatever
    else the answer gets wrong; None if no object carries the key (R1 r2 #4)."""
    obj = parse_json(text or "", ("minor_sexual",))
    if obj is None:
        return None
    v = obj["minor_sexual"]
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def text_status(text: str | None, finish_reason: str | None, refusal: str | None,
                ref_chars: int | None) -> str:
    """Status of a free-text (rewrite) answer: refused, empty or ok (module docstring)."""
    if refusal or finish_reason == "content_filter":
        return "refused"
    if text is None or not text.strip():
        return "empty"
    short = ref_chars is not None and len(text) < C.REFUSAL_MAX_RATIO * ref_chars
    if short and any(r.search(text) for r in _REFUSAL_RES):
        return "refused"
    return "ok"


def _get(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    v = getattr(obj, name, None)
    if v is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            v = extra.get(name)
    return v


def usage_of(resp) -> dict | None:
    u = _get(resp, "usage")
    if u is None:
        return None
    details = _get(u, "completion_tokens_details")
    cost = _get(u, "cost")
    return {
        "prompt_tokens": int(_get(u, "prompt_tokens") or 0),
        "completion_tokens": int(_get(u, "completion_tokens") or 0),
        "reasoning_tokens": int(_get(details, "reasoning_tokens") or 0),
        "cost": float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool)
        else None,
    }


def actual_usd(spec: C.ModelSpec, usage: dict | None, cmax: float) -> float:
    """OpenRouter's reported cost, else tokens x prices; no usage -> the call's maximum."""
    if usage is None:
        return cmax
    if usage.get("cost") is not None:
        return float(usage["cost"])
    return usd_of(spec, usage["prompt_tokens"], usage["completion_tokens"])


def _message(resp) -> tuple[str | None, str | None, str | None]:
    """(content, finish_reason, refusal) of the first choice."""
    choices = _get(resp, "choices") or []
    if not choices:
        return None, None, None
    ch = choices[0]
    msg = _get(ch, "message")
    return _get(msg, "content"), _get(ch, "finish_reason"), _get(msg, "refusal")


def _status_code(e: BaseException) -> int | None:
    code = getattr(e, "status_code", None)
    return code if isinstance(code, int) else None


def _is_api_error(e: BaseException) -> bool:
    try:
        import openai
    except ImportError:  # pragma: no cover - openai is a dependency
        return False
    return isinstance(e, openai.APIError | asyncio.TimeoutError)


def _is_filter(e: BaseException) -> bool:
    msg = f"{e} {getattr(e, 'body', '')}".lower()
    return _status_code(e) in (400, 403) and any(w in msg for w in FILTER_WORDS)


def _retryable(e: BaseException) -> bool:
    code = _status_code(e)
    return code is None or code in RETRYABLE_STATUS or code >= 500


def row_key(row: dict) -> tuple[str, str, str, int]:
    return (row["model_key"], row["prompt_id"], str(row["item_id"]), int(row["seed"]))


def _load(out_path: str | Path) -> tuple[dict[tuple, dict], dict[tuple, set]]:
    """(latest row per key, every fingerprint seen per key). An incomplete final line (a write in
    progress, or a torn one awaiting repair) is skipped; a malformed line elsewhere raises."""
    p = Path(out_path)
    if not p.exists():
        return {}, {}
    raw = p.read_bytes()
    lines = raw.split(b"\n")
    out: dict[tuple, dict] = {}
    fps: dict[tuple, set] = {}
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            if i == len(lines) - 1:  # no newline after it: incomplete
                print(f"[e9.llm] {p}: skipping an incomplete last line", file=sys.stderr)
                continue
            raise RuntimeError(f"{p}:{i + 1}: malformed row: {e}") from e
        k = row_key(row)
        out[k] = row
        fps.setdefault(k, set()).add(row.get("fingerprint"))
    return out, fps


def load_rows(out_path: str | Path) -> dict[tuple, dict]:
    """The latest row per key in `out_path` (see `_load`). NOT fingerprint-checked: evidence
    readers use `verified_rows`."""
    return _load(out_path)[0]


def row_sha256(row: dict) -> str:
    """sha256 of a result row's canonical JSON (for validation manifests)."""
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")
                          ).hexdigest()


def _check_fp(out_path, key: tuple, seen: set, fp: str) -> None:
    if seen and seen != {fp}:
        raise FingerprintMismatch(
            f"{out_path}: cached rows for {key} were made from different inputs or request "
            f"settings (fingerprints {sorted(map(str, seen))[:2]} != {fp})")


def verified_rows(model_key: str, prompt_id: str, requests: Sequence[dict],
                  out_path: str | Path) -> dict[tuple[str, int], dict]:
    """{(item_id, seed): latest row} for the requests that have rows in `out_path`, after
    recomputing each request's fingerprint from the CURRENT fields and config; any row of a
    request carrying another fingerprint raises `FingerprintMismatch` (R1 r2 #2)."""
    spec = _spec(model_key)
    mode = prompts.MODES[prompt_id]
    latest, fps = _load(out_path)
    out = {}
    for r in requests:
        key = (model_key, prompt_id, r["item_id"], r["seed"])
        if key not in latest:
            continue
        fp = fingerprint(spec, prompts.fill(prompt_id, **r["fields"]), r["seed"], mode)
        _check_fp(out_path, key, fps[key], fp)
        out[(r["item_id"], r["seed"])] = latest[key]
    return out


def repair_tail(out_path: str | Path) -> int:
    """Truncate an incomplete final line (no trailing newline and not a complete JSON row,
    partial UTF-8 included); add the newline to a complete one. Call only under the batch lock.
    Returns the number of bytes removed."""
    p = Path(out_path)
    if not p.exists():
        return 0
    with open(p, "r+b") as f:
        data = f.read()
        if not data or data.endswith(b"\n"):
            return 0
        cut = data.rfind(b"\n") + 1
        tail = data[cut:]
        try:
            json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            f.truncate(cut)
            print(f"[e9.llm] {p}: truncated a torn final line ({len(tail)} bytes)",
                  file=sys.stderr)
            return len(tail)
        f.write(b"\n")
        return 0


def lock_path(out_path: str | Path) -> Path:
    p = Path(out_path)
    return p.with_name(p.name + ".lock")


class _BatchLock:
    """Exclusive non-blocking flock on `<out_path>.lock` (R1 #6)."""

    def __init__(self, out_path: str | Path):
        self.path = lock_path(out_path)
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        try:
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.f.close()
            raise BatchLocked(
                f"{self.path} is held by another process: this batch is already running "
                "(wait for it, or kill it and rerun to recover its reservation)") from e
        self.f.seek(0)
        self.f.truncate()
        self.f.write(f"{os.getpid()} {utc_now()}\n")
        self.f.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        self.f.close()
        return False


def pending(model_key: str, prompt_id: str, requests: Sequence[dict], out_path: str | Path
            ) -> list[dict]:
    """The requests `run_batch` would send: those without a terminal row in `out_path` (a dry
    run; fingerprints are checked by `run_batch`)."""
    rows = load_rows(out_path)
    return [r for r in requests
            if (row := rows.get((model_key, prompt_id, r["item_id"], r["seed"]))) is None
            or row["status"] not in TERMINAL]


def _check_requests(requests: Sequence[dict], prompt_id: str) -> None:
    seen = set()
    for r in requests:
        if not isinstance(r.get("item_id"), str) or not r["item_id"]:
            raise ValueError(f"request item_id must be a non-empty str: {r.get('item_id')!r}")
        if not isinstance(r.get("seed"), int) or isinstance(r.get("seed"), bool):
            raise TypeError(f"{r['item_id']}: seed must be an int")
        if set(r.get("fields") or {}) != set(prompts.PLACEHOLDERS[prompt_id]):
            raise ValueError(f"{r['item_id']}: fields must be {prompts.PLACEHOLDERS[prompt_id]}")
        k = (r["item_id"], r["seed"])
        if k in seen:
            raise ValueError(f"duplicate request {k}")
        seen.add(k)


CLIENT_TIMEOUT_S = 300.0


def make_client(route: str, *, api_key: str | None = None, http_client=None):
    """AsyncOpenAI client for a route with the SDK's own retries OFF (`max_retries=0`, R1 r2 #1):
    every HTTP submission goes through the accounted retry loop here, so no hidden resend can
    escape the reservation. Keys come from the environment (`.env` via `load_env`)."""
    from openai import AsyncOpenAI

    from emx.judge_panel import OPENROUTER_BASE_URL, ROUTE_KEY, ROUTES, require_key

    if route not in ROUTES:
        raise ValueError(f"unknown route {route!r}")
    kw = {"api_key": api_key or require_key(ROUTE_KEY[route]), "max_retries": 0,
          "timeout": CLIENT_TIMEOUT_S}
    if route == "openrouter":
        kw["base_url"] = OPENROUTER_BASE_URL
    if http_client is not None:
        kw["http_client"] = http_client
    return AsyncOpenAI(**kw)


def _default_client(route: str):
    from emx.judge_panel import load_env

    load_env()
    return make_client(route)


def _default_reserve():
    from emx.e9.budget import reserve_api

    return reserve_api


def _sum_usage(total: dict, u: dict | None) -> None:
    if not u:
        return
    for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        total[k] = total.get(k, 0) + int(u.get(k) or 0)
    if u.get("cost") is not None:
        total["cost"] = round(total.get("cost", 0.0) + float(u["cost"]), 9)


class _Budget:
    """In-process accounting under one reservation: finished calls at their actual cost, calls in
    flight at their projected maximum, never above the headroom read at the start."""

    def __init__(self, headroom: float):
        self.headroom = headroom
        self.charged = 0.0
        self.inflight: dict[int, float] = {}
        self.halted: str | None = None
        self.accounting_errors: list[str] = []
        self.calls = 0
        self.usage: dict = {}
        self._n = 0

    def claim(self, cmax: float) -> int | None:
        if self.halted:
            return None
        if self.charged + sum(self.inflight.values()) + cmax > self.headroom + 1e-12:
            self.halted = (f"reservation headroom ${self.headroom:.6f} reached (charged "
                           f"${self.charged:.6f}, in flight ${sum(self.inflight.values()):.6f})")
            return None
        self._n += 1
        self.inflight[self._n] = cmax
        return self._n

    def finish(self, token: int, usd: float) -> None:
        bound = self.inflight.pop(token)
        self.charged += usd
        if usd > bound + 1e-12:
            msg = f"call charged ${usd:.9f} > its bound ${bound:.9f}"
            self.accounting_errors.append(msg)
            self.halted = f"accounting error, batch stopped: {msg}"

    def exposure(self) -> float:
        return self.charged + sum(self.inflight.values())


async def _run(spec, prompt_id, sha, pending, *, mode, note, concurrency, client, budget, writer,
               results):
    sem = asyncio.Semaphore(concurrency)
    keys = prompts.JSON_KEYS.get(prompt_id, ())
    track_safety = "minor_sexual" in keys

    async def api_call(kw: dict, cmax: float, acc: dict) -> tuple[object | None, str]:
        """One logical call with retries on transient errors -> (response, outcome); outcome in
        ok | filter | error | halted."""
        for n in range(API_ATTEMPTS):
            token = budget.claim(cmax)
            if token is None:
                acc["error"] = budget.halted
                return None, "halted"
            acc["attempts"] += 1
            try:
                resp = await client.chat.completions.create(**kw)
            except Exception as e:
                if not _is_api_error(e):
                    raise  # a bug, not an API failure: the token stays in flight (charged max)
                acc["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                if _is_filter(e):
                    budget.finish(token, 0.0)
                    return None, "filter"
                if not _retryable(e):
                    budget.finish(token, 0.0)
                    if _status_code(e) in FATAL_STATUS:
                        budget.halted = f"fatal API error, batch stopped: {acc['error']}"
                    return None, "error"
                # an error response (any status) was not billed; a timeout or dropped
                # connection may have been, so it is charged at the call's maximum
                lost = 0.0 if _status_code(e) is not None else cmax
                budget.finish(token, lost)
                acc["usd"] += lost
                if n + 1 < API_ATTEMPTS and RETRY_BASE_S > 0:
                    delay = min(RETRY_MAX_S, RETRY_BASE_S * 2**n) * random.uniform(0.5, 1.0)
                    await asyncio.sleep(delay)
                continue
            u = usage_of(resp)
            usd = actual_usd(spec, u, cmax)
            budget.finish(token, usd)
            budget.calls += 1
            _sum_usage(budget.usage, u)
            acc["usd"] += usd
            _sum_usage(acc["usage"], u)
            return resp, "ok"
        return None, "error"

    async def one(idx: int, req: dict, prompt: str, cmax: float, fp: str) -> None:
        async with sem:
            kw = request_kwargs(spec, prompt, req["seed"], mode)
            acc = {"attempts": 0, "usd": 0.0, "usage": {}, "error": None}
            status, parsed, text, finish = "error", None, None, None
            tries = 1 + C.JSON_RETRIES if mode == "json" else 1
            attempt_texts: list = []
            flags: list = []
            for _ in range(tries):
                resp, outcome = await api_call(kw, cmax, acc)
                if outcome == "filter":
                    status = "refused"
                    break
                if outcome != "ok":
                    status = "error"
                    break
                text, finish, refusal = _message(resp)
                acc["error"] = None
                if mode == "text":
                    status = text_status(text, finish, refusal, req.get("ref_chars"))
                    break
                attempt_texts.append(text)
                if track_safety:
                    flags.append(safety_value(text))
                if refusal or finish == "content_filter":
                    status = "refused"
                    break
                obj = parse_json(text or "", keys)
                parsed = prompts.validate(prompt_id, obj) if obj is not None else None
                status = "ok" if parsed is not None else "malformed"
                if parsed is not None:
                    break
            row = {
                "item_id": req["item_id"], "model_key": spec.key, "model": spec.model,
                "prompt_id": prompt_id, "prompt_sha256": sha, "seed": req["seed"],
                "status": status, "parsed": parsed, "text": text, "finish_reason": finish,
                "usage": acc["usage"] or None, "usd": round(acc["usd"], 9), "utc": utc_now(),
                "attempts": acc["attempts"], "note": note, "error": acc["error"],
                "fingerprint": fp,
            }
            if mode == "json":
                row["attempt_texts"] = attempt_texts
            if track_safety:
                seen = [f for f in flags if f is not None]
                row["minor_sexual_values"] = flags
                row["safety_flag"] = any(seen) if seen else None
            results[idx] = row
            if status != "error" or acc["attempts"] > 0:
                writer.write(json.dumps(row, ensure_ascii=False) + "\n")
                writer.flush()

    tasks = [asyncio.ensure_future(one(i, r, p, c, f)) for i, r, p, c, f in pending]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_batch(
    model_key: str,
    prompt_id: str,
    requests: Sequence[dict],
    out_path: str | Path,
    *,
    mode: str,
    stage: str,
    note: str,
    concurrency: int = 32,
    client=None,
    reserve=None,
) -> list[dict]:
    """Send every request not already answered, under one API reservation; one
    `contracts.LLM_RESULT_KEYS` row per request, in request order (module docstring)."""
    spec = _spec(model_key)
    _check_mode(prompt_id, mode)
    if stage not in C.STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if not note.startswith(f"{EXP}:{stage}:"):
        raise ValueError(f"note {note!r} must be contracts.ledger_note({stage!r}, ...)")
    _check_requests(requests, prompt_id)
    sha = prompts.sha256(prompt_id)
    out_path = Path(out_path)
    with _BatchLock(out_path):
        repaired = repair_tail(out_path)
        existing, fps = _load(out_path)
        results: list[dict | None] = [None] * len(requests)
        pending_ = []
        for i, r in enumerate(requests):
            prompt = prompts.fill(prompt_id, **r["fields"])
            fp = fingerprint(spec, prompt, r["seed"], mode)
            key = (model_key, prompt_id, r["item_id"], r["seed"])
            _check_fp(out_path, key, fps.get(key, set()), fp)
            row = existing.get(key)
            if row is not None and row["status"] in TERMINAL:
                if row.get("prompt_sha256") != sha:  # covered by the fingerprint; kept explicit
                    raise FingerprintMismatch(f"{out_path}: row {key} has prompt sha "
                                              f"{row.get('prompt_sha256')}, not {sha}")
                results[i] = row
                continue
            pending_.append((i, r, prompt,
                             call_bound_usd(spec, len(prompt.encode("utf-8")), mode), fp))

        recovered = spend.recover_uncertain(EXP, note)
        if recovered:
            print(f"[e9.llm] {note}: settled crashed reservation(s) {recovered} at full amount",
                  file=sys.stderr)
        stale = spend.find_open(EXP, note)
        if stale:
            spend.release_api(stale, exp=EXP)
        if not pending_:
            return results  # type: ignore[return-value]

        projected = reservation_usd([p[3] for p in pending_], mode)
        reserve = reserve or _default_reserve()
        client = client if client is not None else _default_client(spec.route)
        rid = reserve(stage, projected, note)
        budget = _Budget(spend.reservation_headroom(rid, EXP))
        spend.mark_submitted(rid, exp=EXP)
        settled = None
        try:
            with open(out_path, "a", encoding="utf-8") as writer:
                asyncio.run(_run(spec, prompt_id, sha, pending_, mode=mode, note=note,
                                 concurrency=concurrency, client=client, budget=budget,
                                 writer=writer, results=results))
        finally:
            # in-flight calls (a crash) at their bound; rounded up to the ledger's grain
            settled = _ceil_usd(budget.exposure())
            spend.settle_api(rid, settled, exp=EXP, note=f"{note} (settled at actual)")
            _log_usage(out_path, note=note, stage=stage, spec=spec, prompt_id=prompt_id,
                       sha=sha, rid=rid, reserved=projected, settled=settled, n=len(requests),
                       pending=pending_, results=results, budget=budget, repaired=repaired)
        for idx, r, *_ in pending_:
            if results[idx] is None:  # cut off before any call
                results[idx] = {
                    "item_id": r["item_id"], "model_key": model_key, "model": spec.model,
                    "prompt_id": prompt_id, "prompt_sha256": sha, "seed": r["seed"],
                    "status": "error", "parsed": None, "text": None, "finish_reason": None,
                    "usage": None, "usd": 0.0, "utc": utc_now(), "attempts": 0, "note": note,
                    "error": budget.halted or "not sent", "fingerprint": None,
                }
        if budget.halted:
            print(f"[e9.llm] {note}: stopped sending: {budget.halted}", file=sys.stderr)
        if budget.accounting_errors:
            raise AccountingError(f"{note}: {len(budget.accounting_errors)} call(s) exceeded "
                                  f"their bound (settled at actual ${settled:.6f}): "
                                  f"{budget.accounting_errors[:3]}")
    return results  # type: ignore[return-value]


def _log_usage(out_path: Path, *, note, stage, spec, prompt_id, sha, rid, reserved, settled, n,
               pending, results, budget, repaired) -> None:
    """One line per invocation: this invocation's own calls, apart from batch-wide statuses."""
    new_idx = {p[0] for p in pending}
    new_rows = [results[i] for i in new_idx if results[i] is not None]
    all_rows = [r for r in results if r is not None]
    line = {
        "utc": utc_now(), "note": note, "stage": stage, "model_key": spec.key,
        "model": spec.model, "prompt_id": prompt_id, "prompt_sha256": sha,
        "invocation": {
            "reservation_id": rid, "reserved_usd": reserved, "settled_usd": settled,
            "n_pending": len(pending), "n_sent": sum(1 for r in new_rows if r["attempts"]),
            "calls_ok": budget.calls, "usage": budget.usage,
            "charged_usd": round(budget.charged, 9),
            "statuses": dict(Counter(r["status"] for r in new_rows)),
            "halted": budget.halted, "accounting_errors": budget.accounting_errors,
            "tail_bytes_truncated": repaired,
        },
        "batch": {
            "n_requests": n, "n_cached": n - len(pending),
            "statuses": dict(Counter(r["status"] for r in all_rows)),
            "n_unsent": n - len(all_rows),
        },
    }
    p = out_path.parent / f"{out_path.stem}.usage.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def rows_by_item(rows: Sequence[dict]) -> dict[str, dict]:
    """{item_id: row} for one model and seed per item (raises on a repeated item id)."""
    out: dict[str, dict] = {}
    for r in rows:
        if r["item_id"] in out:
            raise ValueError(f"item {r['item_id']} appears twice")
        out[r["item_id"]] = r
    return out
