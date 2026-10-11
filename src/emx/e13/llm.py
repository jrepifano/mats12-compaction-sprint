"""E13's LLM layer: one resumable, budgeted batch of chat calls to the notice writer or to luna
(prereg §4.1 "Writing the instances", "Checking the instances", Amendment A; implementation plan
§1 unit B). Unit B owns it; unit B2 (style audit) uses it.

Port of the `emx.e9.llm.run_batch` pattern, simplified for E13 (no prompt registry, no safety
sidecar, no JSON validation: callers parse the text). Imported from `emx.e9.llm` rather than
copied: the AsyncOpenAI client with the SDK's hidden retries off (`make_client`; keys
`OPENAI_API_KEY` / `OPENROUTER_API_KEY` from `.env` via `emx.judge_panel.load_env`, never
logged), the exclusive batch lock, torn-tail repair, the in-process headroom accounting
(`_Budget`), the usage parsing and the error classifiers.

`run_batch(model, requests, out_path, *, note, max_tokens, temperature, json_mode, client=None,
concurrency=16) -> list[dict]` (signature frozen in `contracts`):
- **Models.** `"writer"` = `contracts.WRITER_MODEL` on OpenRouter ($0.75 in / $3.75 out per
  MTok; `max_tokens`); `"luna"` = `emx.judge_panel.PRESETS["luna"]`'s model on the OpenAI route
  with `reasoning_effort="none"` ($0.10 / $0.50; `max_completion_tokens`). One user message, the
  request seed, `temperature` when not None, and `response_format={"type": "json_object"}` when
  `json_mode` (the prompt must then ask for a JSON object).
- **Requests** `{"item_id": str, "prompt": str, "seed": int}`; item ids and (item_id, seed) keys
  unique within a batch.
- **Rows** (one per request, in request order): `item_id, seed, status, text, finish_reason,
  usage, usd, fingerprint, model, note, utc, attempts, error`. `status`: "refused" (API refusal,
  `finish_reason == "content_filter"`, or a 400/403 moderation error), "empty" (blank text),
  "ok", or "error" (API failure after retries, or not sent because the reservation's headroom
  was reached). Callers parse `text`; a malformed answer is the caller's to retry (with a new
  seed, i.e. a new key).
- **Resumable, bound to its inputs.** Rows are appended to `out_path` (JSONL) and flushed as each
  request finishes. `fingerprint` = sha256 over the model key, route and the complete request
  (model id, messages, seed, max-token limit, temperature, reasoning, response format). A
  request whose (item_id, seed) already has a terminal row (ok, empty, refused) is not re-sent;
  ANY row of that key with a different fingerprint raises `FingerprintMismatch` before anything
  is reserved. "error" rows are re-sent by the next run. One `out_path` holds one model.
- **Budget** (exp `E13`, ledger kind "judge"). Under the batch lock: an earlier crashed batch of
  the same `note` is settled at its full reservation (`spend.recover_uncertain`) and an unsent
  open one released. The pending calls' upper bound (input tokens <= UTF-8 bytes of the prompt +
  `MESSAGE_OVERHEAD_TOKENS`, output tokens <= `max_tokens`, at the model's prices) plus a retry
  allowance is printed and checked with `spend.check_total(projected, kind="judge", exp="E13")`,
  then reserved (`spend.reserve_api`), marked submitted before the first call, every attempt
  bounded by the reservation's headroom, and settled at the actual total (`spend.settle_api`;
  on a crash, actual plus in-flight calls at their bound). Actual cost: OpenRouter's
  `usage.cost` when present, else tokens x prices; an attempt of uncertain outcome (timeout,
  dropped connection) is charged its bound, an error status 0. A call charged above its bound
  stops the batch and raises `AccountingError` after settlement.
- **Retries** on transient errors (timeouts, 408/409/429, 5xx): up to `API_ATTEMPTS` accounted
  attempts with exponential backoff and jitter (the e9 loop: every HTTP submission goes through
  the budget, which a tenacity wrapper around the SDK could not guarantee). 401/402/403/404 stop
  the batch.

Usage log: one line per invocation in `<out stem>.usage.jsonl`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from emx import spend
from emx.e9.llm import (
    AccountingError,
    BatchLocked,
    FingerprintMismatch,
    _BatchLock,
    _Budget,
    _ceil_usd,
    _is_api_error,
    _is_filter,
    _message,
    _read_jsonl,
    _retryable,
    _status_code,
    repair_tail,
    usage_of,
)
from emx.e13 import contracts as C

__all__ = [
    "MODELS",
    "TERMINAL",
    "AccountingError",
    "BatchLocked",
    "FingerprintMismatch",
    "ModelSpec",
    "estimate_usd",
    "fingerprint",
    "load_rows",
    "pending",
    "request_kwargs",
    "run_batch",
]

EXP = C.EXP
STATUSES: tuple[str, ...] = ("ok", "empty", "refused", "error")
TERMINAL: tuple[str, ...] = ("ok", "empty", "refused")
MESSAGE_OVERHEAD_TOKENS = 64
API_ATTEMPTS = 5
RETRY_BASE_S = 1.0  # tests set 0
RETRY_MAX_S = 30.0
RETRY_ALLOWANCE_FRACTION = 0.02  # extra calls reserved: max(1, ceil(2% of pending))
FATAL_STATUS = (401, 402, 403, 404)


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model: str
    route: str  # "openai" | "openrouter"
    usd_per_mtok_in: float
    usd_per_mtok_out: float
    max_tokens_param: str
    extra: tuple[tuple[str, str], ...] = ()  # top-level request fields (OpenAI route)


def _luna_spec() -> ModelSpec:
    from emx.judge_panel import PRESETS

    p = PRESETS["luna"]
    return ModelSpec("luna", p.model, p.route, p.usd_per_mtok_in, p.usd_per_mtok_out,
                     p.request["max_tokens_param"],
                     (("reasoning_effort", p.request["reasoning_effort"]),))


MODELS: dict[str, ModelSpec] = {
    "writer": ModelSpec("writer", C.WRITER_MODEL, "openrouter", 0.75, 3.75, "max_tokens"),
    "luna": _luna_spec(),
}
assert MODELS["luna"].extra == (("reasoning_effort", "none"),)
assert MODELS["luna"].route == "openai"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec(model: str) -> ModelSpec:
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; known {sorted(MODELS)}")
    return MODELS[model]


def request_kwargs(spec: ModelSpec, prompt: str, seed: int, *, max_tokens: int,
                   temperature: float | None, json_mode: bool) -> dict:
    """The chat-completions request (one user message)."""
    kw: dict = {"model": spec.model, "messages": [{"role": "user", "content": prompt}],
                "seed": int(seed), spec.max_tokens_param: int(max_tokens)}
    if temperature is not None:
        kw["temperature"] = float(temperature)
    if json_mode:
        kw["response_format"] = {"type": "json_object"}
    for k, v in spec.extra:
        kw[k] = v
    return kw


def fingerprint(spec: ModelSpec, kw: dict) -> str:
    obj = {"model_key": spec.key, "route": spec.route, "request": kw}
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
                          ).hexdigest()


def usd_of(spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * spec.usd_per_mtok_in + completion_tokens * spec.usd_per_mtok_out) / 1e6


def call_bound_usd(spec: ModelSpec, prompt: str, max_tokens: int) -> float:
    """Upper bound on one call: input <= prompt bytes + overhead, output <= max_tokens."""
    return usd_of(spec, len(prompt.encode("utf-8")) + MESSAGE_OVERHEAD_TOKENS, int(max_tokens))


def reservation_usd(bounds: Sequence[float]) -> float:
    """Every pending call at its bound plus max(1, ceil(2%)) extra calls at the largest bound."""
    if not bounds:
        return 0.0
    n_extra = max(1, math.ceil(RETRY_ALLOWANCE_FRACTION * len(bounds)))
    return _ceil_usd(sum(bounds) + n_extra * max(bounds))


def actual_usd(spec: ModelSpec, usage: dict | None, cmax: float) -> float:
    if usage is None:
        return cmax
    if usage.get("cost") is not None:
        return float(usage["cost"])
    return usd_of(spec, usage["prompt_tokens"], usage["completion_tokens"])


def _check_requests(requests: Sequence[dict]) -> None:
    seen = set()
    for r in requests:
        if not isinstance(r.get("item_id"), str) or not r["item_id"]:
            raise ValueError(f"request item_id must be a non-empty str: {r.get('item_id')!r}")
        if not isinstance(r.get("seed"), int) or isinstance(r.get("seed"), bool):
            raise TypeError(f"{r['item_id']}: seed must be an int")
        if not isinstance(r.get("prompt"), str) or not r["prompt"]:
            raise ValueError(f"{r['item_id']}: prompt must be a non-empty str")
        k = (r["item_id"], r["seed"])
        if k in seen:
            raise ValueError(f"duplicate request {k}")
        seen.add(k)


def _load(out_path: Path) -> tuple[dict[tuple, dict], dict[tuple, set]]:
    latest: dict[tuple, dict] = {}
    fps: dict[tuple, set] = {}
    for row in _read_jsonl(out_path):
        k = (str(row["item_id"]), int(row["seed"]))
        latest[k] = row
        fps.setdefault(k, set()).add(row.get("fingerprint"))
    return latest, fps


def load_rows(out_path: str | Path) -> dict[tuple[str, int], dict]:
    """{(item_id, seed): latest row} (not fingerprint-checked)."""
    return _load(Path(out_path))[0]


def _plan(model, requests, out_path, *, max_tokens, temperature, json_mode):
    """(spec, cached results by index, pending [(idx, req, kw, bound, fp)])."""
    spec = _spec(model)
    _check_requests(requests)
    latest, fps = _load(Path(out_path))
    cached: dict[int, dict] = {}
    todo = []
    for i, r in enumerate(requests):
        kw = request_kwargs(spec, r["prompt"], r["seed"], max_tokens=max_tokens,
                            temperature=temperature, json_mode=json_mode)
        fp = fingerprint(spec, kw)
        key = (r["item_id"], r["seed"])
        seen = fps.get(key, set())
        if seen and seen != {fp}:
            raise FingerprintMismatch(
                f"{out_path}: cached rows for {key} were made from a different prompt or request "
                f"({sorted(map(str, seen))[:2]} != {fp})")
        row = latest.get(key)
        if row is not None and row["status"] in TERMINAL:
            cached[i] = row
            continue
        todo.append((i, r, kw, call_bound_usd(spec, r["prompt"], max_tokens), fp))
    return spec, cached, todo


def pending(model: str, requests: Sequence[dict], out_path: str | Path, *, max_tokens: int,
            temperature: float | None, json_mode: bool) -> list[dict]:
    """The requests `run_batch` would send (a dry run; fingerprints checked)."""
    _, _, todo = _plan(model, requests, out_path, max_tokens=max_tokens,
                       temperature=temperature, json_mode=json_mode)
    return [t[1] for t in todo]


def estimate_usd(model: str, requests: Sequence[dict], out_path: str | Path, *, max_tokens: int,
                 temperature: float | None = None, json_mode: bool = False) -> float:
    """What `run_batch` would reserve for the pending requests (0 if none)."""
    _, _, todo = _plan(model, requests, out_path, max_tokens=max_tokens,
                       temperature=temperature, json_mode=json_mode)
    return reservation_usd([t[3] for t in todo])


def _sum_usage(total: dict, u: dict | None) -> None:
    if not u:
        return
    for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        total[k] = total.get(k, 0) + int(u.get(k) or 0)
    if u.get("cost") is not None:
        total["cost"] = round(total.get("cost", 0.0) + float(u["cost"]), 9)


def _status(text: str | None, finish: str | None, refusal: str | None) -> str:
    if refusal or finish == "content_filter":
        return "refused"
    if text is None or not text.strip():
        return "empty"
    return "ok"


async def _run(spec, todo, *, note, concurrency, client, budget, writer, results):
    sem = asyncio.Semaphore(concurrency)

    async def api_call(kw: dict, cmax: float, acc: dict):
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
                    raise  # a bug: the token stays in flight (charged its bound)
                acc["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                if _is_filter(e):
                    budget.finish(token, 0.0)
                    return None, "filter"
                if not _retryable(e):
                    budget.finish(token, 0.0)
                    if _status_code(e) in FATAL_STATUS:
                        budget.halted = f"fatal API error, batch stopped: {acc['error']}"
                    return None, "error"
                lost = 0.0 if _status_code(e) is not None else cmax
                budget.finish(token, lost)
                acc["usd"] += lost
                if n + 1 < API_ATTEMPTS and RETRY_BASE_S > 0:
                    await asyncio.sleep(min(RETRY_MAX_S, RETRY_BASE_S * 2**n)
                                        * random.uniform(0.5, 1.0))
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

    async def one(idx: int, req: dict, kw: dict, cmax: float, fp: str) -> None:
        async with sem:
            acc = {"attempts": 0, "usd": 0.0, "usage": {}, "error": None}
            text = finish = None
            resp, outcome = await api_call(kw, cmax, acc)
            if outcome == "filter":
                status = "refused"
            elif outcome != "ok":
                status = "error"
            else:
                text, finish, refusal = _message(resp)
                acc["error"] = None
                status = _status(text, finish, refusal)
            row = {
                "item_id": req["item_id"], "seed": req["seed"], "status": status, "text": text,
                "finish_reason": finish, "usage": acc["usage"] or None,
                "usd": round(acc["usd"], 9), "fingerprint": fp, "model": spec.model,
                "note": note, "utc": utc_now(), "attempts": acc["attempts"],
                "error": acc["error"],
            }
            results[idx] = row
            if acc["attempts"] > 0:
                writer.write(json.dumps(row, ensure_ascii=False) + "\n")
                writer.flush()

    tasks = [asyncio.ensure_future(one(*t)) for t in todo]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _default_client(route: str):
    from emx.e9.llm import make_client
    from emx.judge_panel import load_env

    load_env()
    return make_client(route)


def run_batch(model: str, requests: list[dict], out_path, *, note: str, max_tokens: int,
              temperature: float | None, json_mode: bool, client=None,
              concurrency: int = 16) -> list[dict]:
    """Send every request not already answered, under one E13 API reservation; one row per
    request, in request order (module docstring)."""
    if not note or not note.startswith(f"{EXP}:"):
        raise ValueError(f"note {note!r} must start with '{EXP}:' (it keys the reservation)")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _BatchLock(out_path):
        repaired = repair_tail(out_path)
        spec, cached, todo = _plan(model, requests, out_path, max_tokens=max_tokens,
                                   temperature=temperature, json_mode=json_mode)
        results: list[dict | None] = [None] * len(requests)
        for i, row in cached.items():
            results[i] = row

        recovered = spend.recover_uncertain(EXP, note)
        if recovered:
            print(f"[e13.llm] {note}: settled crashed reservation(s) {recovered} at full amount",
                  file=sys.stderr)
        stale = spend.find_open(EXP, note)
        if stale:
            spend.release_api(stale, exp=EXP)
        if not todo:
            return results  # type: ignore[return-value]

        projected = reservation_usd([t[3] for t in todo])
        print(f"[e13.llm] {note}: {len(todo)} {model} call(s) pending ({len(cached)} cached); "
              f"projected <= ${projected:.4f}", file=sys.stderr)
        spend.check_total(projected, kind="judge", exp=EXP)
        client = client if client is not None else _default_client(spec.route)
        rid = spend.reserve_api(EXP, projected, note)
        budget = _Budget(spend.reservation_headroom(rid, EXP))
        spend.mark_submitted(rid, exp=EXP)
        settled = None
        try:
            with open(out_path, "a", encoding="utf-8") as writer:
                asyncio.run(_run(spec, todo, note=note, concurrency=concurrency, client=client,
                                 budget=budget, writer=writer, results=results))
        finally:
            settled = _ceil_usd(budget.exposure())
            spend.settle_api(rid, settled, exp=EXP, note=f"{note} (settled at actual)")
            _log_usage(out_path, note=note, spec=spec, rid=rid, reserved=projected,
                       settled=settled, n=len(requests), todo=todo, results=results,
                       budget=budget, repaired=repaired)
        for idx, r, _kw, _b, fp in todo:
            if results[idx] is None:  # cut off before any call
                results[idx] = {
                    "item_id": r["item_id"], "seed": r["seed"], "status": "error", "text": None,
                    "finish_reason": None, "usage": None, "usd": 0.0, "fingerprint": fp,
                    "model": spec.model, "note": note, "utc": utc_now(), "attempts": 0,
                    "error": budget.halted or "not sent",
                }
        if budget.halted:
            print(f"[e13.llm] {note}: stopped sending: {budget.halted}", file=sys.stderr)
        if budget.accounting_errors:
            raise AccountingError(f"{note}: {len(budget.accounting_errors)} call(s) exceeded "
                                  f"their bound (settled at actual ${settled:.6f}): "
                                  f"{budget.accounting_errors[:3]}")
    return results  # type: ignore[return-value]


def _log_usage(out_path: Path, *, note, spec, rid, reserved, settled, n, todo, results, budget,
               repaired) -> None:
    new_rows = [results[t[0]] for t in todo if results[t[0]] is not None]
    all_rows = [r for r in results if r is not None]
    line = {
        "utc": utc_now(), "note": note, "model_key": spec.key, "model": spec.model,
        "invocation": {
            "reservation_id": rid, "reserved_usd": reserved, "settled_usd": settled,
            "n_pending": len(todo), "n_sent": sum(1 for r in new_rows if r["attempts"]),
            "calls_ok": budget.calls, "usage": budget.usage,
            "charged_usd": round(budget.charged, 9),
            "statuses": dict(Counter(r["status"] for r in new_rows)),
            "halted": budget.halted, "accounting_errors": budget.accounting_errors,
            "tail_bytes_truncated": repaired,
        },
        "batch": {"n_requests": n, "n_cached": n - len(todo),
                  "statuses": dict(Counter(r["status"] for r in all_rows))},
    }
    with open(out_path.parent / f"{out_path.stem}.usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def usd_total(rows: Sequence[dict]) -> float:
    return round(sum(float(r.get("usd") or 0.0) for r in rows), 9)
