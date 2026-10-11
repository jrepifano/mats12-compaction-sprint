"""A resumable, budgeted batch of chat calls for any experiment or probe (generic port of
`emx.e13.llm.run_batch`, which is itself the `emx.e9.llm` pattern; first used by probe P01).

Same contract as `emx.e13.llm` (rows, statuses, fingerprints, resume, reservation, retries,
usage log), with three changes:
- **`exp` is an argument** (`run_batch(..., exp="P01")`); the note must start with `"<exp>:"`.
- **Models** are keys of `MODELS` (or a `ModelSpec` passed directly). `extra_body` holds
  OpenRouter-only request fields (e.g. `reasoning`), sent through the SDK's `extra_body`.
- **Requests** may carry an optional `"system"` string, sent as a system message before the user
  `prompt` (the fingerprint covers it).

Input-token bound: UTF-8 bytes of system + prompt + `MESSAGE_OVERHEAD_TOKENS` (a strict upper
bound; real code-heavy text runs ~3-4 bytes per token), so reservations run ~3x the real cost.
Callers with large prompts should split work into chunks so one reservation fits the cap.
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
    extra_body: str = ""  # JSON object of OpenRouter-only fields, sent as the SDK's extra_body


def _luna_spec() -> ModelSpec:
    from emx.judge_panel import PRESETS

    p = PRESETS["luna"]
    return ModelSpec("luna", p.model, p.route, p.usd_per_mtok_in, p.usd_per_mtok_out,
                     p.request["max_tokens_param"],
                     (("reasoning_effort", p.request["reasoning_effort"]),))


# Prices per MTok from OpenRouter /models on 2026-10-11 (bounds only; OpenRouter's usage.cost is
# what gets charged). DeepSeek V4 Flash moved to 0.03 / 1.28 (CLAUDE.md lists 0.14 / 0.28).
MODELS: dict[str, ModelSpec] = {
    "glm": ModelSpec("glm", "z-ai/glm-5.3-flash", "openrouter", 0.15, 0.50, "max_tokens"),
    "dsk": ModelSpec("dsk", "deepseek/deepseek-v4-flash", "openrouter", 0.14, 1.28, "max_tokens"),
    "gemini": ModelSpec("gemini", "google/gemini-3.8-flash", "openrouter", 0.75, 3.75,
                        "max_tokens"),
    "luna": _luna_spec(),
}
assert MODELS["luna"].extra == (("reasoning_effort", "none"),)
assert MODELS["luna"].route == "openai"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec(model) -> ModelSpec:
    if isinstance(model, ModelSpec):
        return model
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; known {sorted(MODELS)}")
    return MODELS[model]


def request_kwargs(spec: ModelSpec, prompt: str, seed: int, *, max_tokens: int,
                   temperature: float | None, json_mode: bool, system: str | None = None) -> dict:
    """The chat-completions request (an optional system message, then one user message)."""
    msgs = [{"role": "system", "content": system}] if system else []
    msgs.append({"role": "user", "content": prompt})
    kw: dict = {"model": spec.model, "messages": msgs,
                "seed": int(seed), spec.max_tokens_param: int(max_tokens)}
    if temperature is not None:
        kw["temperature"] = float(temperature)
    if json_mode:
        kw["response_format"] = {"type": "json_object"}
    for k, v in spec.extra:
        kw[k] = v
    if spec.extra_body:
        kw["extra_body"] = json.loads(spec.extra_body)
    return kw


def fingerprint(spec: ModelSpec, kw: dict) -> str:
    obj = {"model_key": spec.key, "route": spec.route, "request": kw}
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
                          ).hexdigest()


def usd_of(spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * spec.usd_per_mtok_in + completion_tokens * spec.usd_per_mtok_out) / 1e6


def call_bound_usd(spec: ModelSpec, prompt: str, max_tokens: int, system: str | None = None
                   ) -> float:
    """Upper bound on one call: input <= prompt (+ system) bytes + overhead, output <=
    max_tokens."""
    n_in = len(prompt.encode("utf-8")) + len((system or "").encode("utf-8"))
    return usd_of(spec, n_in + MESSAGE_OVERHEAD_TOKENS, int(max_tokens))


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
        if r.get("system") is not None and not isinstance(r["system"], str):
            raise TypeError(f"{r['item_id']}: system must be a str or None")
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
                            temperature=temperature, json_mode=json_mode,
                            system=r.get("system"))
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
        todo.append((i, r, kw, call_bound_usd(spec, r["prompt"], max_tokens, r.get("system")),
                     fp))
    return spec, cached, todo


def pending(model, requests: Sequence[dict], out_path: str | Path, *, max_tokens: int,
            temperature: float | None, json_mode: bool) -> list[dict]:
    """The requests `run_batch` would send (a dry run; fingerprints checked)."""
    _, _, todo = _plan(model, requests, out_path, max_tokens=max_tokens,
                       temperature=temperature, json_mode=json_mode)
    return [t[1] for t in todo]


def estimate_usd(model, requests: Sequence[dict], out_path: str | Path, *, max_tokens: int,
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


def run_batch(model, requests: list[dict], out_path, *, exp: str, note: str, max_tokens: int,
              temperature: float | None, json_mode: bool, client=None,
              concurrency: int = 16) -> list[dict]:
    """Send every request not already answered, under one API reservation of `exp`; one row per
    request, in request order (module docstring)."""
    if not note or not note.startswith(f"{exp}:"):
        raise ValueError(f"note {note!r} must start with '{exp}:' (it keys the reservation)")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _BatchLock(out_path):
        repaired = repair_tail(out_path)
        spec, cached, todo = _plan(model, requests, out_path, max_tokens=max_tokens,
                                   temperature=temperature, json_mode=json_mode)
        results: list[dict | None] = [None] * len(requests)
        for i, row in cached.items():
            results[i] = row

        recovered = spend.recover_uncertain(exp, note)
        if recovered:
            print(f"[llm_batch] {note}: settled crashed reservation(s) {recovered} at full amount",
                  file=sys.stderr)
        stale = spend.find_open(exp, note)
        if stale:
            spend.release_api(stale, exp=exp)
        if not todo:
            return results  # type: ignore[return-value]

        projected = reservation_usd([t[3] for t in todo])
        print(f"[llm_batch] {note}: {len(todo)} {spec.key} call(s) pending ({len(cached)} cached); "
              f"projected <= ${projected:.4f}", file=sys.stderr)
        spend.check_total(projected, kind="judge", exp=exp)
        client = client if client is not None else _default_client(spec.route)
        rid = spend.reserve_api(exp, projected, note)
        budget = _Budget(spend.reservation_headroom(rid, exp))
        spend.mark_submitted(rid, exp=exp)
        settled = None
        try:
            with open(out_path, "a", encoding="utf-8") as writer:
                asyncio.run(_run(spec, todo, note=note, concurrency=concurrency, client=client,
                                 budget=budget, writer=writer, results=results))
        finally:
            settled = _ceil_usd(budget.exposure())
            spend.settle_api(rid, settled, exp=exp, note=f"{note} (settled at actual)")
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
            print(f"[llm_batch] {note}: stopped sending: {budget.halted}", file=sys.stderr)
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
