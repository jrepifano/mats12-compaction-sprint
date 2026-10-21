"""Judge B candidates for Experiment 1 (stage S1): API backends for luna, GLM and DeepSeek.

Unit U2b of `docs/sprint/specs/S1.md` ("U2b src/compaction/s1_api.py" and "Backend interface").
`ApiBackend(route, cache_dir)` implements `s1_types.Backend`: a list of `Req` in, one `Resp` per
`Req` out.

Ported, not imported, from `src/emx/probe_llm.py` (P03's generic client): the request building
(OpenAI's `max_completion_tokens` / `reasoning_effort`; OpenRouter's `max_tokens`,
`extra_body.reasoning`, `usage: {include: true}` and `provider` routing with
`require_parameters`), the billed `usage.cost` read from pydantic's `model_extra`, the split of
transient from fatal errors, the jittered backoff, and the in-process reservation guard
(`_Budget`). Not ported: probe_llm's `emx.spend.reserve_api` path; S1 spend goes through
`compaction.budget` (stage `SPRINT-S1`), which enforces the stage and sprint caps from the
designated checkout. From `scripts/provider_bench.py`: DeepSeek's own API (OpenAI-compatible,
`https://api.deepseek.com`, model `deepseek-flash`, usage `prompt_cache_hit_tokens`), OpenRouter
pinning with `allow_fallbacks: False` and the non-4-bit quantization filter, and BYOK upstream
cost (`cost_details.upstream_inference_cost`). Review r1 (`logs/reviews/20261020-s1-u2b-r1.md`)
shaped the spend bound, the worker shutdown, the cache recovery and the shared instruction text.

**Order of checks in `run`:** privacy first (any `Req.private`, or a `source` outside
`PUBLIC_SOURCES`, refuses the whole batch with `PrivacyRefused` before anything else, the cache
included); then config (duplicate keys, purpose name, an unpinned route without
`allow_unpinned`, a missing API key); then `too_long` (spec D3: `fits` with the route's
`bytes_per_token` and `context_tokens`; never sent); then the cache; then one reservation for the
pending requests.

**Spend.** Each request's bound is `budget.bounded_cost(model, max_input, max_output_tokens)` with
`max_input` = UTF-8 bytes of everything sent (system, user, the appended schema instruction, and
the native schema on `json_schema` routes) + `OVERHEAD_TOKENS`, one token per byte (byte-level BPE
gives tokens <= bytes), so it is a hard upper bound; D3's estimate is used only for the context
check. The batch reserves the sum x `RETRY_MARGIN` once; a reservation the caps can't hold raises
`budget.BudgetRefused` (split the batch). Each attempt, retries included, must fit the reservation
at its bound before it is sent, or it lands as `error="budget"`. An attempt's bound is released
to its actual cost only when the cost is known: OpenRouter's `usage.cost` (plus BYOK upstream),
or complete usage (input and output token counts) x the route's prices, cache reads at the
cache-read price. It is released to zero only for statuses that never ran inference
(`_UNBILLED`: 400, 401, 403, 404, 413, 422, 429). Everything else stays charged at its bound:
timeouts, connection errors, 402/408/409/5xx, a response with missing usage
(`meta["usage_known"]=False`; the answer is still returned and cached), and requests in flight
when the batch stops. After the batch, also when it raises, every worker is cancelled and awaited
and the client closed, and only then `budget.settle` books the total (`budget.cancel` only if no
attempt was sent), so no request can start after settlement.

**Cache.** `cache_dir/<purpose>.jsonl`, one row per landed request, keyed by the spec's
`(key, sha256(system + "\\0" + user), sha256(canonical schema), model)` plus a settings
fingerprint (`settings_sha`: transport, provider pin, reasoning, JSON mode, temperature, seed,
max_output_tokens, the instruction template). Only rows with `error is None` and a matching
fingerprint are reused; error rows are kept for the record and re-sent. Appends and reads hold an
exclusive `flock` on the file; a torn final line (an interrupted append) is truncated away, while
a malformed earlier line raises `ValueError`.

**Errors.** One failed call never fails the batch: it lands as a `Resp` with `error` set
(`"http_<status>"`, `"timeout"`, `"connection"`, `"no_choices"`, `"budget"`, `"schema"`,
`"empty"`, `"provider_mismatch"`, `"api_error"`; detail in `meta["error_detail"]`). Retried:
408/409/429/5xx, timeouts, connection errors, replies without choices, with jittered backoff
(the concurrency slot is held per attempt, not across the backoff sleep). 401/402/403 abort the
batch: dispatch stops, workers are cancelled, spend is settled, and `FatalAPIError` is raised.

**Prompt and structured output.** The system message is `Req.system` unchanged, so passes that
share a system prompt and history keep a byte-identical prefix for provider prefix caching. For
schema requests the same instruction (`SCHEMA_INSTRUCTION`: the JSON schema, then "Answer with
one JSON object that follows this schema.") is appended to the end of the user message on every
route. `json_object` routes add `{"type": "json_object"}`; `json_schema` routes (luna) also send
the schema as an OpenAI strict `json_schema` response format. Either way the answer is checked
with `s1_types.validate` in code. Temperature is 0 by default on every route that accepts
sampling parameters (all four do), recorded in `meta["sampling"]` and the fingerprint.

Keys (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`) come from the repo's `.env`;
never printed. No call is made at import or in tests: the transport is injectable
(`client_factory`).
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
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compaction import budget
from compaction.s1_types import PUBLIC_SOURCES, STAGE, Req, Resp, fits, validate

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEEPSEEK_URL = "https://api.deepseek.com"  # the SDK appends /chat/completions (provider_bench)
TRANSPORTS = ("openai", "openrouter", "deepseek")
REASONING = ("none", "low", "high", "default")
JSON_MODES = ("json_schema", "json_object", "text")  # text: no response_format; parse the reply
# OpenRouter 400s on reasoning={"enabled": False} for these (probe_llm.REASONING_MANDATORY).
REASONING_MANDATORY = frozenset({"z-ai/glm-5.3-flash"})
# Non-4-bit quantizations (scripts/provider_bench.py ALLOWED_QUANT).
ALLOWED_QUANT = ["int8", "fp6", "fp8", "fp16", "bf16", "fp32", "unknown"]
HIGH_REASONING_BUDGET = 3000

OVERHEAD_TOKENS = 256  # chat-template overhead per call, on top of one token per byte sent
RETRY_MARGIN = 1.25
MAX_RETRIES = 5
DEFAULT_TIMEOUT_S = 900.0  # long-history judge calls; a timeout is retried and charged worst case
_TRANSIENT = (408, 409, 429, 500, 502, 503, 504, 529)
_ABORT = (401, 402, 403)
# Statuses that demonstrably never ran inference: an attempt answered with one is not billed.
# Every other failure (402, 408, 409, 5xx, timeouts, connection errors) stays charged at its bound.
_UNBILLED = (400, 401, 403, 404, 413, 422, 429)
# Appended to the end of the user message of every schema request, identically on every route.
SCHEMA_INSTRUCTION = (
    "\n\nJSON schema:\n{schema}\n\nAnswer with one JSON object that follows this schema."
)
_PURPOSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PrivacyRefused(PermissionError):
    """A request about a private record, or a non-public source, was given to an API backend."""


class FatalAPIError(RuntimeError):
    """An auth or billing failure (401/402/403) that would fail every call."""


class ConfigError(RuntimeError):
    """A route, key or batch problem that stops the batch before anything is sent."""


class _NoChoices(ValueError):
    """A 200 response without choices: transient."""


# ------------------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One Judge B candidate. `prices` are USD per million tokens: `input`, `output` and, when
    known, `cache_read`. `provider_pin` is the OpenRouter provider to pin with
    `allow_fallbacks: False`; for a direct transport (openai, deepseek) it only records that the
    route was chosen by the Phase 3 benchmark. `None` until then, and `run` refuses it."""

    name: str
    model: str
    transport: str  # "openai" | "openrouter" | "deepseek"
    provider_pin: str | None
    prices: Mapping[str, float]
    context_tokens: int
    bytes_per_token: float
    reasoning: str  # "none" | "low" | "high" | "default" (default: send no reasoning field)
    family: str = ""
    json_mode: str = "json_object"  # "json_schema" where the transport supports strict schemas
    key_env: str = ""
    prices_placeholder: bool = True  # list prices from the repo docs, not yet benchmark-checked
    accepts_sampling: bool = True  # temperature/seed sent (all four routes take them)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise ValueError(f"{self.name}: transport must be one of {TRANSPORTS}")
        if self.reasoning not in REASONING:
            raise ValueError(f"{self.name}: reasoning must be one of {REASONING}")
        if self.reasoning == "none" and self.model in REASONING_MANDATORY:
            raise ValueError(f"{self.name}: {self.model} cannot disable reasoning")
        if self.json_mode not in JSON_MODES:
            raise ValueError(f"{self.name}: json_mode must be one of {JSON_MODES}")
        for k in ("input", "output"):
            if k not in self.prices:
                raise ValueError(f"{self.name}: prices need {k!r}")


# Pins and prices from the Phase 3 provider benchmark, 2026-10-20 (100k-token prompts, $0.21;
# logs/provider_bench_SPRINT-S1_20261020.txt and .jsonl). Measured single-call output tok/s:
# luna on OpenAI 128, GLM on Together 159, DeepSeek V4.1 Flash on Baidu 197 (DeepSeek's own API
# 173, Together 434 at about 3x Baidu's price). Context windows are the pinned endpoints' listings
# on OpenRouter's endpoints API the same day (Baidu: max completion 393,216).
ROUTES: dict[str, Route] = {
    "luna": Route(
        name="luna",
        model="gpt-6-luna",
        transport="openai",  # direct; OpenRouter lists OpenAI as luna's only provider
        provider_pin="OpenAI",  # a label for the direct route: nothing is sent for it
        prices={"input": 0.10, "output": 0.50, "cache_read": 0.01},
        context_tokens=1_050_000,  # OpenRouter /models listing, 2026-10-19
        bytes_per_token=2.0,  # spec D3: 2.0 until measured
        reasoning="none",  # AGENTS.md default judge setting (reasoning effort none)
        family="openai",
        json_mode="json_schema",  # OpenAI structured outputs (strict); not yet smoke-tested
        key_env="OPENAI_API_KEY",
        prices_placeholder=False,
        notes="benchmark 2026-10-20: 128 tok/s, TTFT 3.5 s",
    ),
    "glm": Route(
        name="glm",
        model="z-ai/glm-5.3-flash",
        transport="openrouter",
        provider_pin="Together",  # allow_fallbacks: False, non-4-bit quantizations
        prices={"input": 0.15, "output": 0.50, "cache_read": 0.03},
        context_tokens=1_048_575,  # Together's endpoint listing
        bytes_per_token=2.0,
        # reasoning is mandatory on this endpoint (probe_llm); at the default it spent the whole
        # 32k output on reasoning and returned empty content on 21 of 30 pass-1a calls in the
        # 20 Oct dev run, and json_object mode added nothing, so: low reasoning, text mode
        reasoning="low",
        family="zhipu",
        json_mode="text",
        key_env="OPENROUTER_API_KEY",
        prices_placeholder=False,
        notes="benchmark 2026-10-20: 159 tok/s, TTFT 2.3 s",
    ),
    "deepseek": Route(
        name="deepseek",
        model="deepseek/deepseek-v4.1-flash",
        transport="openrouter",
        # Baidu (benchmarked 197 tok/s, $0.035/M) returned HTTP 429 on most calls at
        # concurrency 8 in the 20 Oct dev run; DeepInfra fp8 (benchmarked 109 tok/s, batch 335)
        # replaced it before any labels. Table price is DeepInfra's highest fp8 listing.
        provider_pin="DeepInfra",  # fp8; allow_fallbacks: False, non-4-bit quantizations
        prices={"input": 0.21, "output": 0.63, "cache_read": 0.006},
        context_tokens=1_048_576,  # DeepInfra's endpoint listing
        bytes_per_token=2.0,
        # thinking off: at the default, json_object replies degenerated (reasoning leaked into
        # keys, or `{""` loops to the cap) and pass 2 spent all 32k on thinking (20 Oct dev run)
        reasoning="none",
        family="deepseek",
        json_mode="text",
        key_env="OPENROUTER_API_KEY",
        prices_placeholder=False,
        notes="benchmark 2026-10-20: DeepInfra 109 tok/s, TTFT 4.6 s",
    ),
    "deepseek-api": Route(
        name="deepseek-api",
        model="deepseek-flash",  # DeepSeek-V4.1-Flash-1017 on DeepSeek's own API
        transport="deepseek",
        provider_pin=None,  # unpinned alternative: run refuses it without allow_unpinned
        # PLACEHOLDER: peak list rates (01-04 and 06-10 UTC weekdays; off-peak is half), used as
        # the table price so unbilled estimates never undercount.
        prices={"input": 0.30, "output": 1.20, "cache_read": 0.006},
        # 1,048,576 is OpenRouter's listing for V4.1 Flash; DeepSeek's API limit UNVERIFIED.
        context_tokens=1_000_000,
        bytes_per_token=2.0,
        reasoning="default",  # DeepSeek's API takes no reasoning field here; vendor default
        family="deepseek",
        json_mode="json_object",  # DeepSeek's API supports json_object only
        key_env="DEEPSEEK_API_KEY",
        notes="benchmark 2026-10-20: 173 tok/s, TTFT 2.6 s (not chosen: Baidu cheaper, faster)",
    ),
}


# ------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def canonical_schema(schema: dict | None) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_key(req: Req, model: str) -> list[str]:
    """The spec's cache key: (key, sha256(system + "\\0" + user), sha256(schema), model)."""
    return [req.key, _sha(req.system + "\0" + req.user), _sha(canonical_schema(req.schema)), model]


def schema_instruction(schema: dict) -> str:
    """The text appended to the user message of every schema request, on every route."""
    return SCHEMA_INSTRUCTION.format(schema=json.dumps(schema, ensure_ascii=False))


def settings_sha(settings: Mapping[str, Any]) -> str:
    return _sha(json.dumps(dict(settings), sort_keys=True, ensure_ascii=False))


def _status(e: BaseException) -> int | None:
    s = getattr(e, "status_code", None)
    return s if isinstance(s, int) else None


def _transient(e: BaseException) -> bool:
    name = type(e).__name__
    return (
        _status(e) in _TRANSIENT
        or isinstance(e, (_NoChoices, asyncio.TimeoutError, TimeoutError, ConnectionError))
        or "Timeout" in name
        or "Connection" in name
    )


def _error_label(e: BaseException) -> str:
    s = _status(e)
    if s is not None:
        return f"http_{s}"
    if isinstance(e, _NoChoices):
        return "no_choices"
    name = type(e).__name__
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in name:
        return "timeout"
    if isinstance(e, ConnectionError) or "Connection" in name:
        return "connection"
    return "api_error"


def _field(obj: Any, name: str) -> Any:
    """A field the SDK may keep only in pydantic's `model_extra` (`usage.cost`, `provider`)."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    v = getattr(obj, name, None)
    return v if v is not None else (getattr(obj, "model_extra", None) or {}).get(name)


def _count(v: Any) -> int | None:
    """A token count, or None when absent or not a non-negative integer."""
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v


def _usd_ok(v: Any) -> bool:
    """A reported cost usable as billed: a finite, non-negative number (0 is fine)."""
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0


def _backoff(attempt: int, status: int | None) -> float:
    cap = 60.0 if status == 429 else 20.0
    return min(cap, 2.0 * 2**attempt) * (0.5 + random.random())


def _parse_json(text: str) -> tuple[Any, str | None, bool]:
    """(object, error, fenced). Tolerates one surrounding ``` fence."""
    t = text.strip()
    fenced = False
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, flags=re.DOTALL)
    if m:
        t, fenced = m.group(1), True
    try:
        return json.loads(t), None, fenced
    except json.JSONDecodeError as e:
        # Text mode (and chatty replies): accept the reply only if exactly one top-level JSON
        # object parses out of it, and nothing after it opens another one (review 21 Oct: an
        # earlier object followed by a cut-off "correction" must not pass as the answer).
        found, k, dec = [], 0, json.JSONDecoder()
        while (k := t.find("{", k)) >= 0:
            try:
                obj, end = dec.raw_decode(t, k)
            except json.JSONDecodeError:
                if found:
                    return None, "json: ambiguous (a later object does not parse)", fenced
                k += 1
                continue
            found.append(obj)
            k = end
        if len(found) == 1:
            return found[0], None, True
        why = "ambiguous (several objects)" if found else str(e)
        return None, f"json: {why}", fenced


def table_usd(route: Route, input_tokens: int, cached: int, output_tokens: int) -> float:
    """Cost from usage at the route's prices: uncached input, cache reads, output."""
    p = route.prices
    cached = min(max(cached, 0), input_tokens)
    p_cache = p.get("cache_read", p["input"])
    return (
        (input_tokens - cached) * p["input"] + cached * p_cache + output_tokens * p["output"]
    ) / 1e6


class _Budget:
    """In-process bound on one reservation (ported from probe_llm): known spend + the bound of
    every attempt in flight or of uncertain outcome never passes `cap`. One event loop and no
    await between check and update, so no lock."""

    def __init__(self, cap: float):
        self.cap = cap
        self.committed = 0.0  # known + uncertain + in flight
        self.known = 0.0
        self.uncertain = 0.0
        self.sent = 0  # attempts handed to the transport

    def acquire(self, worst: float) -> bool:
        if self.committed + worst > self.cap + 1e-12:
            return False
        self.committed += worst
        return True


class _Stopped(Exception):
    """Dispatch stopped (another worker failed) before this attempt was sent."""


# ------------------------------------------------------------------------------------------
# Transport seams (tests inject a fake client)
# ------------------------------------------------------------------------------------------


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(budget.REPO_ROOT / ".env", override=False)


def default_client(route: Route, key: str) -> Any:
    from openai import AsyncOpenAI

    base = {"openai": None, "openrouter": OPENROUTER_URL, "deepseek": DEEPSEEK_URL}[route.transport]
    return AsyncOpenAI(api_key=key, base_url=base, max_retries=0)


# ------------------------------------------------------------------------------------------
# Cache file
# ------------------------------------------------------------------------------------------


@contextmanager
def _locked_file(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_cache(path: Path) -> list[dict]:
    """All rows of a cache file, oldest first. A torn final line (an interrupted append) is
    truncated away under the lock, and a missing final newline restored; a malformed earlier line
    raises `ValueError`."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with _locked_file(path) as fh:
        fh.seek(0)
        raw = fh.read()
        lines = raw.split(b"\n")
        pos = 0
        for i, line in enumerate(lines):
            last = i == len(lines) - 1 or (i == len(lines) - 2 and lines[-1] == b"")
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    if not last:
                        raise ValueError(f"{path}: malformed cache line {i + 1}: {e}") from e
                    fh.truncate(pos)  # torn tail: drop it, keep every earlier row
                    fh.flush()
                    os.fsync(fh.fileno())
                    return rows
            pos += len(line) + 1
        if raw and not raw.endswith(b"\n"):
            fh.write(b"\n")  # a complete last row without its newline
            fh.flush()
            os.fsync(fh.fileno())
    return rows


def append_row(path: Path, row: dict) -> None:
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    with _locked_file(path) as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# ------------------------------------------------------------------------------------------
# The backend
# ------------------------------------------------------------------------------------------


@dataclass
class _Prepared:
    req: Req
    kwargs: dict
    max_input_tokens: int  # hard bound: bytes sent + overhead
    worst_usd: float
    ckey: list[str]
    settings_sha: str


@dataclass
class ApiBackend:
    """`s1_types.Backend` over one `Route`. See the module docstring for the contract."""

    route: Route
    cache_dir: str | Path
    concurrency: int = 32
    allow_unpinned: bool = False
    ledger: str | Path | None = None
    caps_file: str | Path | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = MAX_RETRIES
    client_factory: Callable[[Route, str], Any] | None = None
    temperature: float | None = 0.0  # sent only to routes that accept sampling parameters
    seed: int | None = None
    name: str = field(init=False)
    model: str = field(init=False)
    family: str = field(init=False)
    context_tokens: int = field(init=False)
    bytes_per_token: float = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.route.name
        self.model = self.route.model
        self.family = self.route.family
        self.context_tokens = self.route.context_tokens
        self.bytes_per_token = self.route.bytes_per_token
        self.cache_dir = Path(self.cache_dir)
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")

    # ---------------------------------------------------------------- requests

    def sampling(self) -> dict[str, Any]:
        """The sampling parameters actually sent."""
        if not self.route.accepts_sampling:
            return {"temperature": None, "seed": None}
        return {"temperature": self.temperature, "seed": self.seed}

    def settings(self, req: Req) -> dict[str, Any]:
        r = self.route
        return {
            "transport": r.transport,
            "provider_pin": r.provider_pin,
            "reasoning": r.reasoning,
            "json_mode": r.json_mode if req.schema is not None else None,
            "max_output_tokens": req.max_output_tokens,
            "instruction_sha": _sha(SCHEMA_INSTRUCTION) if req.schema is not None else None,
            **self.sampling(),
        }

    def _prices(self) -> dict[str, dict[str, float]]:
        p = self.route.prices
        return {self.model: {"input": p["input"], "output": p["output"]}}

    def _prepare(self, req: Req) -> _Prepared:
        r = self.route
        user = req.user if req.schema is None else req.user + schema_instruction(req.schema)
        messages = [
            {"role": "system", "content": req.system},
            {"role": "user", "content": user},
        ]
        kw: dict[str, Any] = {"model": r.model, "messages": messages, "timeout": self.timeout_s}
        for k, v in self.sampling().items():
            if v is not None:
                kw[k] = v
        sent_bytes = len(req.system.encode("utf-8")) + len(user.encode("utf-8"))
        if req.schema is not None:
            if r.json_mode == "json_schema":
                kw["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "s1_judge", "strict": True, "schema": req.schema},
                }
                sent_bytes += len(json.dumps(kw["response_format"]).encode("utf-8"))
            elif r.json_mode == "json_object":
                kw["response_format"] = {"type": "json_object"}
        if r.transport == "openai":
            kw["max_completion_tokens"] = req.max_output_tokens
            if r.reasoning != "default":
                kw["reasoning_effort"] = r.reasoning
        else:
            kw["max_tokens"] = req.max_output_tokens
        if r.transport == "openrouter":
            provider: dict[str, Any] = {"require_parameters": True, "quantizations": ALLOWED_QUANT}
            if r.provider_pin is not None:
                provider.update({"order": [r.provider_pin], "allow_fallbacks": False})
            extra: dict[str, Any] = {"usage": {"include": True}, "provider": provider}
            if r.reasoning == "none":
                extra["reasoning"] = {"enabled": False}
            elif r.reasoning == "low":
                extra["reasoning"] = {"effort": "low"}
            elif r.reasoning == "high":
                extra["reasoning"] = {"max_tokens": HIGH_REASONING_BUDGET}
            kw["extra_body"] = extra
        max_in = sent_bytes + OVERHEAD_TOKENS
        worst = budget.bounded_cost(r.model, max_in, req.max_output_tokens, self._prices())
        st = settings_sha(self.settings(req))
        return _Prepared(req, kw, max_in, worst, cache_key(req, r.model), st)

    # ---------------------------------------------------------------- one response

    def _usage(self, usage: Any) -> dict[str, Any]:
        """Token counts and cost of one response. `usd` is None when the cost is unknown:
        no OpenRouter cost and incomplete token counts."""
        tin = _count(_field(usage, "prompt_tokens"))
        tout = _count(_field(usage, "completion_tokens"))
        details = _field(usage, "prompt_tokens_details")
        cached = _field(usage, "prompt_cache_hit_tokens")  # DeepSeek's API
        if cached is None:
            cached = _field(details, "cached_tokens")
        cached = _count(cached) or 0
        cwrite = _count(_field(details, "cache_write_tokens")) or 0
        known = tin is not None and tout is not None
        cost = _field(usage, "cost") if self.route.transport == "openrouter" else None
        if cost is not None:  # reported: it must be a valid amount, or the cost is unknown
            usd, source = (float(cost), "billed") if _usd_ok(cost) else (None, "bound")
            if usd is not None and _field(usage, "is_byok"):
                # BYOK: OpenRouter's cost is its fee; the provider bills our key separately.
                upstream = _field(_field(usage, "cost_details"), "upstream_inference_cost")
                if _usd_ok(upstream):
                    usd, source = usd + float(upstream), "billed+byok_upstream"
                else:  # missing, null or invalid: the real cost is unknown
                    usd, source = None, "bound"
            if usd is None:
                known = False
        elif known:
            usd, source = table_usd(self.route, tin, cached, tout), "table"
        else:
            usd, source = None, "bound"
        return {
            "usd": usd,
            "source": source,
            "usage_known": known,
            "input": tin or 0,
            "output": tout or 0,
            "cache_read": cached,
            "cache_write": cwrite,
        }

    def _finish(self, resp: Resp, req: Req, text: str, finish: str | None) -> None:
        """Set text, data and error from a landed answer. A pinned OpenRouter route answered by
        another provider is `provider_mismatch` (answer kept, never cached as a success)."""
        resp.text = text
        resp.error = None
        resp.meta["finish_reason"] = finish
        if finish == "length":
            resp.meta["truncated"] = True
        if req.schema is None:
            if not text:
                resp.error = "empty"
        else:
            obj, jerr, fenced = _parse_json(text) if text else (None, "json: empty", False)
            if fenced:
                resp.meta["fenced"] = True
            errs = [jerr] if jerr else validate(obj, req.schema)
            if errs:
                resp.error = "schema"
                resp.meta["schema_errors"] = errs[:20]
            else:
                resp.data = obj
        pin = self.route.provider_pin
        used = resp.meta.get("provider")
        if (
            resp.error is None
            and self.route.transport == "openrouter"
            and pin is not None
            and isinstance(used, str)
            and used.lower() != pin.lower()
        ):
            resp.error = "provider_mismatch"

    def _land(self, resp: Resp, p: _Prepared, b: _Budget, out: Any) -> None:
        """Book one response's cost: the attempt's bound becomes its actual cost when known,
        else stays charged at the bound."""
        u = self._usage(_field(out, "usage"))
        resp.input_tokens += u["input"]
        resp.output_tokens += u["output"]
        resp.cache_read_tokens += u["cache_read"]
        resp.cache_write_tokens += u["cache_write"]
        if u["usd"] is None:
            b.uncertain += p.worst_usd
            resp.usd += p.worst_usd
            resp.meta["uncertain_usd"] = resp.meta.get("uncertain_usd", 0.0) + p.worst_usd
        else:
            b.committed += u["usd"] - p.worst_usd
            b.known += u["usd"]
            resp.usd += u["usd"]
        resp.meta["usage_known"] = resp.meta.get("usage_known", True) and u["usage_known"]
        srcs = set(filter(None, resp.meta.get("cost_source", "").split("+"))) | set(
            u["source"].split("+")
        )
        resp.meta["cost_source"] = "+".join(sorted(srcs))

    async def _one(
        self, client: Any, p: _Prepared, b: _Budget, sem: asyncio.Semaphore, stop: asyncio.Event
    ) -> Resp:
        req = p.req
        resp = Resp(
            req.key,
            None,
            None,
            None,
            model=self.model,
            meta={"retries": 0, "sampling": self.sampling()},
        )
        t0 = time.monotonic()
        last_status: int | None = None
        try:
            for attempt in range(self.max_retries + 1):
                resp.meta["retries"] = attempt
                async with sem:  # one slot per attempt, released before any backoff
                    if stop.is_set():
                        raise _Stopped
                    if not b.acquire(p.worst_usd):
                        if attempt > 0:  # keep the last error; say why it wasn't retried
                            resp.meta["retry_stopped_by_budget"] = True
                            return resp
                        resp.error = "budget"
                        resp.meta["error_detail"] = (
                            f"attempt {attempt + 1} not sent: reservation ${b.cap:.6f}, "
                            f"committed ${b.committed:.6f}"
                        )
                        return resp
                    landed = False
                    try:
                        b.sent += 1
                        out = await client.chat.completions.create(**p.kwargs)
                        self._land(resp, p, b, out)
                        landed = True
                        model = _field(out, "model")
                        if isinstance(model, str) and model:
                            resp.model = model
                        used = _field(out, "provider")
                        if isinstance(used, str) and used:
                            resp.meta["provider"] = used
                        choices = _field(out, "choices")
                        if not choices:
                            raise _NoChoices("response has no choices")
                        msg = _field(choices[0], "message")
                        text = (_field(msg, "content") or "").strip()
                        self._finish(resp, req, text, _field(choices[0], "finish_reason"))
                        return resp
                    except Exception as e:  # classified below; one call never fails the batch
                        status = _status(e)
                        if not landed:
                            if status in _UNBILLED:  # rejected before inference: not billed
                                b.committed -= p.worst_usd
                            else:  # may have run: stays charged at its bound
                                b.uncertain += p.worst_usd
                                resp.usd += p.worst_usd
                                resp.meta["uncertain_usd"] = (
                                    resp.meta.get("uncertain_usd", 0.0) + p.worst_usd
                                )
                        last_status = status
                        resp.error = _error_label(e)
                        resp.meta["error_detail"] = f"{type(e).__name__}: {str(e)[:300]}"
                        if status in _ABORT:
                            raise FatalAPIError(f"{self.name}: {resp.meta['error_detail']}") from e
                        if not _transient(e) or attempt == self.max_retries:
                            return resp
                await asyncio.sleep(_backoff(attempt, last_status))
            return resp
        finally:
            resp.duration_s = round(time.monotonic() - t0, 3)

    # ---------------------------------------------------------------- the batch

    def _cache_path(self, purpose: str) -> Path:
        return Path(self.cache_dir) / f"{purpose}.jsonl"

    def _check(self, reqs: list[Req], purpose: str) -> None:
        bad = [r for r in reqs if r.private or r.source not in PUBLIC_SOURCES]
        if bad:
            r = bad[0]
            raise PrivacyRefused(
                f"{self.name}: {len(bad)} request(s) may not leave Anthropic (first: key "
                f"{r.key!r}, rid {r.rid!r}, source {r.source!r}, private={r.private}); API "
                f"backends take only {sorted(PUBLIC_SOURCES)} records that are not private"
            )
        keys = [r.key for r in reqs]
        if len(set(keys)) != len(keys):
            dup = sorted({k for k in keys if keys.count(k) > 1})[:5]
            raise ConfigError(f"duplicate request keys: {dup}")
        if not _PURPOSE.match(purpose):
            raise ConfigError(f"purpose {purpose!r} must match {_PURPOSE.pattern}")
        if self.route.provider_pin is None and not self.allow_unpinned:
            raise ConfigError(
                f"route {self.name!r} has no provider_pin (set from the Phase 3 provider "
                f"benchmark); allow_unpinned=True only for the benchmark and tests"
            )

    def _row(self, p: _Prepared, resp: Resp, purpose: str) -> dict:
        return {
            "cache_key": p.ckey,
            "settings_sha": p.settings_sha,
            "settings": self.settings(p.req),
            "key": resp.key,
            "rid": p.req.rid,
            "purpose": purpose,
            "route": self.route.name,
            "data": resp.data,
            "text": resp.text,
            "error": resp.error,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cache_read_tokens": resp.cache_read_tokens,
            "cache_write_tokens": resp.cache_write_tokens,
            "usd": resp.usd,
            "model": resp.model,
            "duration_s": resp.duration_s,
            "meta": resp.meta,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @staticmethod
    def _from_row(row: dict) -> Resp:
        return Resp(
            key=row["key"],
            data=row["data"],
            text=row["text"],
            error=None,
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            usd=row["usd"],
            model=row["model"],
            duration_s=row["duration_s"],
            meta={**row.get("meta", {}), "cached": True},
        )

    def run(self, reqs: list[Req], *, purpose: str) -> dict[str, Resp]:
        """Sync wrapper over `arun` (not callable from inside a running event loop)."""
        return asyncio.run(self.arun(reqs, purpose=purpose))

    async def arun(self, reqs: list[Req], *, purpose: str) -> dict[str, Resp]:
        self._check(reqs, purpose)  # privacy first, then config
        out: dict[str, Resp] = {}
        live: list[Req] = []
        for r in reqs:
            if not fits(r, self):
                out[r.key] = Resp(r.key, None, None, "too_long", model=self.model)
            else:
                live.append(r)
        path = self._cache_path(purpose)
        cached: dict[tuple, dict] = {}
        for row in read_cache(path):
            if row.get("error") is None and "settings_sha" in row:
                cached[(*row["cache_key"], row["settings_sha"])] = row  # latest success wins
        pending: list[_Prepared] = []
        for r in live:
            p = self._prepare(r)
            row = cached.get((*p.ckey, p.settings_sha))
            if row is not None:
                out[r.key] = self._from_row(row)
            else:
                pending.append(p)
        if not pending:
            return out
        key = self._api_key()
        bounded = sum(p.worst_usd for p in pending)
        reserve_usd = math.ceil(bounded * RETRY_MARGIN * 1e6) / 1e6  # the ledger's grain
        note = f"S1 {self.name} {purpose}"
        try:
            res_id = budget.reserve(
                STAGE, reserve_usd, note=note, ledger=self.ledger, caps_file=self.caps_file
            )
        except budget.BudgetRefused as e:
            if type(e) is not budget.BudgetRefused:  # wrong checkout, top-up gate: as is
                raise
            raise type(e)(
                f"{self.name} {purpose}: {len(pending)} requests need ${reserve_usd:.4f} "
                f"(hard bound ${bounded:.4f} x {RETRY_MARGIN} retry margin), which the caps "
                f"can't hold; split the batch. {e}",
                e.breakdown,
            ) from e
        b = _Budget(reserve_usd)
        try:
            out.update(await self._send(pending, key, b, path, purpose))
        finally:
            self._close(res_id, b, len(pending), note)
        return out

    def _api_key(self) -> str:
        env = self.route.key_env
        if not env:
            raise ConfigError(f"route {self.name!r} names no key_env")
        if self.client_factory is None:
            _load_env()
        key = os.environ.get(env, "")
        if not key and self.client_factory is None:
            raise ConfigError(f"{env} is not set (repo .env)")
        return key

    async def _send(
        self, pending: list[_Prepared], key: str, b: _Budget, path: Path, purpose: str
    ) -> dict[str, Resp]:
        """Run every request; on the first failure stop dispatch, cancel and await every worker,
        close the client, then raise. Returns only once no worker is left running."""
        factory = self.client_factory or default_client
        client = factory(self.route, key)
        sem = asyncio.Semaphore(self.concurrency)
        stop = asyncio.Event()
        out: dict[str, Resp] = {}

        async def worker(p: _Prepared) -> None:
            try:
                resp = await self._one(client, p, b, sem, stop)
                out[p.req.key] = resp
                append_row(path, self._row(p, resp, purpose))
            except _Stopped:
                return
            except BaseException:
                stop.set()  # before yielding, so no queued worker sends another request
                raise

        tasks = [asyncio.create_task(worker(p)) for p in pending]
        failure: BaseException | None = None
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in tasks:  # the first failure in submission order, for a stable report
                if t in done and not t.cancelled() and t.exception() is not None:
                    failure = t.exception()
                    break
        finally:
            stop.set()
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            close = getattr(client, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
        if failure is not None:
            raise failure
        return out

    def _close(self, res_id: str, b: _Budget, n_pending: int, note: str) -> None:
        """Settle at known spend plus the bound of every attempt whose cost is unknown (uncertain
        outcomes, missing usage, requests cancelled in flight); cancel only when no attempt was
        sent. Runs after every worker has stopped."""
        if b.sent == 0:
            budget.cancel(
                res_id, f"{note}: no call sent", ledger=self.ledger, caps_file=self.caps_file
            )
            return
        # Rounded to 1e-9 first: float drift from adding and removing bounds must not tip the
        # budget's round-up to the next millionth.
        actual = round(max(b.known, b.committed, 0.0), 9)
        budget.settle(
            res_id,
            actual,
            stage=STAGE,
            kind="judge",
            resource=f"{self.name}:{self.model}",
            units=b.sent,
            note=(
                f"{note}: {b.sent} calls sent for {n_pending} requests; ${b.known:.6f} known + "
                f"${max(b.committed - b.known, 0.0):.6f} at bound (uncertain or cancelled)"
            ),
            ledger=self.ledger,
            caps_file=self.caps_file,
        )
