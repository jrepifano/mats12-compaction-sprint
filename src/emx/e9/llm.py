"""E9's LLM layer: one resumable, budgeted batch of JSON or free-text calls (prereg §4.3, §4.4,
§4.9, §7.3, Appendix A; implementation plan v4, unit B1 and the "E9 admission protocol").

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
- **Resumable.** Rows are appended to `out_path` (JSONL, one `contracts.LLM_RESULT_KEYS` row per
  request outcome, plus `note` and `error`) and flushed as each request finishes. A request whose
  (model_key, prompt_id, item_id, seed) already has a terminal row (`TERMINAL`: ok, refused,
  empty, malformed) is not re-sent; its row is returned as it is. "error" rows (API failures after
  retries, or a request cut off by the reservation's headroom) are re-sent by the next run. A row
  written under a different prompt sha256 is refused (the prompts are frozen).
- **Budget** (§7.3). Startup settles a crashed earlier run of the same `note` at its full
  reservation (`spend.recover_uncertain`) and releases an unsubmitted one. The pending requests'
  conservative projection (`estimate_usd`: every call at its maximum, input at
  `CHARS_PER_TOKEN_EST` characters per token, output at the max-token limit) is reserved through
  `reserve(stage, usd, note)` (default `emx.e9.budget.reserve_api`, the E9 admission protocol);
  `spend.mark_submitted` precedes the first call. Every call attempt, retries included, first
  claims its projected maximum; the sum of finished calls' actual cost and in-flight maxima never
  exceeds `spend.reservation_headroom`. When the next call would, no further call is sent (those
  requests get unwritten "error" rows; rerun with a fresh reservation). At the end the reservation
  is settled at the actual total (`spend.settle_api`); on a crash or interrupt, at the actual
  total plus every in-flight call at its maximum.
- **Actual cost** of a call: OpenRouter's `usage.cost` when present, else usage tokens x the
  ModelSpec prices (completion tokens include reasoning). A call with no usage, or a failed
  attempt of uncertain outcome (timeout, dropped connection), is charged its projected
  maximum; an attempt answered by an error status (4xx, 5xx) is charged 0.
- **JSON mode** (A.1, A.4, A.5): `parse_json`, then `prompts.validate`; a malformed or invalid
  answer (blank included) is retried with the seed unchanged up to `contracts.JSON_RETRIES`
  times, then recorded "malformed" (App. A).
- **Text mode** (A.2, A.3): "refused" if the API reports a refusal (message `refusal`,
  `finish_reason == "content_filter"`, or a 400/403 moderation error), or if the output is
  shorter than `REFUSAL_MAX_RATIO * ref_chars` and matches a `REFUSAL_PATTERNS` regex (skipped
  when `ref_chars` is None); "empty" if blank; else "ok".

A usage log line per run (`<out stem>.usage.jsonl` beside `out_path`) records the reservation,
the settlement, token totals and the status counts.
"""

from __future__ import annotations

import asyncio
import json
import math
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
CHARS_PER_TOKEN_EST = 2.5  # conservative: English runs ~4 characters per token
MESSAGE_OVERHEAD_TOKENS = 16
API_ATTEMPTS = 5  # per JSON attempt, for transient API errors
RETRY_BASE_S = 1.0  # exponential backoff with jitter, capped at RETRY_MAX_S (tests set 0)
RETRY_MAX_S = 30.0
RETRYABLE_STATUS = (408, 409, 429)
FILTER_WORDS = ("moderation", "content_filter", "content filter", "content_policy",
                "content policy", "safety")
FATAL_STATUS = (401, 402, 403, 404)  # stop sending: every later call would fail the same way
_REFUSAL_RES = tuple(re.compile(p) for p in C.REFUSAL_PATTERNS)


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


def call_max_usd(spec: C.ModelSpec, prompt_chars: int, mode: str) -> float:
    """The projected maximum cost of one call: input at CHARS_PER_TOKEN_EST, output at the cap."""
    tin = math.ceil(prompt_chars / CHARS_PER_TOKEN_EST) + MESSAGE_OVERHEAD_TOKENS
    return usd_of(spec, tin, max_tokens(spec, mode))


def _check_mode(prompt_id: str, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if prompts.MODES[prompt_id] != mode:
        raise ValueError(f"{prompt_id} answers in {prompts.MODES[prompt_id]} mode, not {mode!r}")


def _prompt_chars(prompt_id: str, fields: dict) -> int:
    return prompts.fixed_chars(prompt_id) + sum(len(v) for v in fields.values())


def estimate_usd(model_key: str, prompt_id: str, requests: Sequence[dict], mode: str) -> float:
    """Conservative projected cost of sending every request once, each at its maximum."""
    spec = _spec(model_key)
    _check_mode(prompt_id, mode)
    total = sum(call_max_usd(spec, _prompt_chars(prompt_id, r["fields"]), mode) for r in requests)
    return math.ceil(total * 1e6) / 1e6


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


def load_rows(out_path: str | Path) -> dict[tuple, dict]:
    """The latest row per key in `out_path`. A malformed final line (a crash mid-write) is
    ignored; a malformed line elsewhere raises."""
    p = Path(out_path)
    if not p.exists():
        return {}
    lines = p.read_text(encoding="utf-8").splitlines()
    out: dict[tuple, dict] = {}
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            if i == len(lines) - 1:
                print(f"[e9.llm] {p}:{i + 1}: ignoring a partial last line", file=sys.stderr)
                continue
            raise RuntimeError(f"{p}:{i + 1}: malformed row: {e}") from e
        out[row_key(row)] = row
    return out


def pending(model_key: str, prompt_id: str, requests: Sequence[dict], out_path: str | Path
            ) -> list[dict]:
    """The requests `run_batch` would send: those without a terminal row in `out_path`."""
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


def _default_client(route: str):
    from emx.judge_panel import load_env, make_client

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
        self.inflight.pop(token)
        self.charged += usd

    def exposure(self) -> float:
        return self.charged + sum(self.inflight.values())


async def _run(spec, prompt_id, sha, pending, *, mode, note, concurrency, client, budget, writer,
               results):
    sem = asyncio.Semaphore(concurrency)
    keys = prompts.JSON_KEYS.get(prompt_id, ())

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
            acc["usd"] += usd
            _sum_usage(acc["usage"], u)
            return resp, "ok"
        return None, "error"

    async def one(idx: int, req: dict, prompt: str, cmax: float) -> None:
        async with sem:
            kw = request_kwargs(spec, prompt, req["seed"], mode)
            acc = {"attempts": 0, "usd": 0.0, "usage": {}, "error": None}
            status, parsed, text, finish = "error", None, None, None
            tries = 1 + C.JSON_RETRIES if mode == "json" else 1
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
            }
            results[idx] = row
            if status != "error" or acc["attempts"] > 0:
                writer.write(json.dumps(row, ensure_ascii=False) + "\n")
                writer.flush()

    tasks = [asyncio.ensure_future(one(i, r, p, c)) for i, r, p, c in pending]
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
    existing = load_rows(out_path)

    results: list[dict | None] = [None] * len(requests)
    pending = []
    for i, r in enumerate(requests):
        row = existing.get((model_key, prompt_id, r["item_id"], r["seed"]))
        if row is not None and row["status"] in TERMINAL:
            if row.get("prompt_sha256") != sha:
                raise RuntimeError(f"{out_path}: row {row_key(row)} was written under prompt sha "
                                   f"{row.get('prompt_sha256')}, not {sha}")
            results[i] = row
            continue
        prompt = prompts.fill(prompt_id, **r["fields"])
        pending.append((i, r, prompt, call_max_usd(spec, len(prompt), mode)))

    recovered = spend.recover_uncertain(EXP, note)
    if recovered:
        print(f"[e9.llm] {note}: settled crashed reservation(s) {recovered} at full amount",
              file=sys.stderr)
    stale = spend.find_open(EXP, note)
    if stale:
        spend.release_api(stale, exp=EXP)
    if not pending:
        return results  # type: ignore[return-value]

    projected = math.ceil(sum(c for *_, c in pending) * 1e6) / 1e6
    reserve = reserve or _default_reserve()
    client = client if client is not None else _default_client(spec.route)
    rid = reserve(stage, projected, note)
    budget = _Budget(spend.reservation_headroom(rid, EXP))
    spend.mark_submitted(rid, exp=EXP)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    settled = None
    try:
        with open(out_path, "a+", encoding="utf-8") as writer:
            writer.seek(0, 2)
            if writer.tell() > 0:
                writer.seek(writer.tell() - 1)
                if writer.read(1) != "\n":
                    writer.write("\n")
            asyncio.run(_run(spec, prompt_id, sha, pending, mode=mode, note=note,
                             concurrency=concurrency, client=client, budget=budget,
                             writer=writer, results=results))
    finally:
        # in-flight calls (a crash) at their maximum; rounded up to the ledger's 1e-6 grain
        settled = math.ceil(round(budget.exposure() * 1e6, 3)) / 1e6
        spend.settle_api(rid, settled, exp=EXP, note=f"{note} (settled at actual)")
        _log_usage(out_path, note=note, stage=stage, spec=spec, prompt_id=prompt_id, sha=sha,
                   rid=rid, reserved=projected, settled=settled, n=len(requests),
                   n_pending=len(pending), results=results, halted=budget.halted)
    for i, (idx, r, _, _) in enumerate(pending):
        if results[idx] is None:  # cut off before any call
            results[idx] = {
                "item_id": r["item_id"], "model_key": model_key, "model": spec.model,
                "prompt_id": prompt_id, "prompt_sha256": sha, "seed": r["seed"],
                "status": "error", "parsed": None, "text": None, "finish_reason": None,
                "usage": None, "usd": 0.0, "utc": utc_now(), "attempts": 0, "note": note,
                "error": budget.halted or "not sent",
            }
    if budget.halted:
        print(f"[e9.llm] {note}: stopped sending: {budget.halted}", file=sys.stderr)
    return results  # type: ignore[return-value]


def _log_usage(out_path: Path, *, note, stage, spec, prompt_id, sha, rid, reserved, settled, n,
               n_pending, results, halted) -> None:
    rows = [r for r in results if r is not None]
    tok: dict = {}
    for r in rows:
        if r.get("note") == note:
            _sum_usage(tok, r.get("usage"))
    line = {
        "utc": utc_now(), "note": note, "stage": stage, "model_key": spec.key,
        "model": spec.model, "prompt_id": prompt_id, "prompt_sha256": sha,
        "reservation_id": rid, "reserved_usd": reserved, "settled_usd": settled,
        "n_requests": n, "n_pending": n_pending,
        "statuses": dict(Counter(r["status"] for r in rows)), "usage": tok, "halted": halted,
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
