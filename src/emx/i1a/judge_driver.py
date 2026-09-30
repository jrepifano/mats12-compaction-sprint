"""Judge one I1a+ readouts arm on one WorkspaceBench family, in capped chunks (implementation plan
§2 U5; prereg v0.4 §7.1 and §7.3).

How the benchmark is driven: in-process, through ``wsbench.cli.main(["judge", ...])``, the
benchmark's own CLI entry with its own argument parsing, so the judged output is exactly what
``wsbench judge`` would write, and the tests' ``fake_llm`` (patched ``wsbench.llm._make_client``)
reaches every call. ``main`` returns 0 / 2 (usage) / 3 (judge config); a family's fatal data
error (missing cells, a malformed ``opts=``) escapes as ``SystemExit(2)``, which we catch and turn
into the same code.

Chunks: the in-scope items are split into chunks of at most ``chunk`` (25) items, each judged with
``items=<chunk>`` into the one shared ``out`` directory ``judged_dir(arm, family)``. The benchmark
caches every verdict in ``<out>/cells.jsonl`` keyed by (cell, prompt fingerprint), so the chunks
accumulate; cells left unjudged are retried in guarded chunks; and a last full pass, which may not
send a single request (the journaled client refuses it), writes the ``results.json`` that can be
``complete``. An unrestricted run (no ``items``, ``limit`` or ``layers``) must end ``complete``.

Spend (review r1 BLOCKING 1, 2 and MINOR 5):
- Every OpenRouter response goes through a wrapper around the client ``wsbench.llm._make_client``
  returns, which appends ``{call_id, utc, model, cost}`` (the response's ``usage.cost``) under an
  exclusive flock to ``<out>/spend_journal.jsonl`` before the benchmark sees the response.
- ``reconcile`` records to ``emx.spend`` whatever the journal holds beyond the ledger entries
  tagged ``[I1a <arm>/<family>]`` (dollars and calls, per model). It runs before the first call
  (so a crashed run's orphaned spend is recorded before any new call), after every chunk, and in a
  ``finally``. Ledger units and dollars are therefore the journal's HTTP responses and costs.
- Before each chunk: projected = calls x ``usd_per_call`` x 1.25 (retry headroom), printed and
  checked with ``emx.spend.check_total(..., "judge", exp="I1a")``; a refusal raises
  ``BudgetExceeded`` (exit 2) before any call. ``usd_per_call`` may not be below
  ``MIN_USD_PER_CALL``. If a chunk's journaled spend exceeds its projection, the run stops after
  that chunk (exit 2).
- Hallucination runs only with ``opts=verify=0`` unless ``allow_verify=True`` (checkpoint-5
  add-on).

Calls per cell (``verify=0``, the plan's §0 default; a cell is one (item, layer) group for the
bank families and one (item, layer, site) for hallucination):

| family           | prose arm | token arm            |
|------------------|-----------|----------------------|
| multihop         | 1 judge   | 1 judge (bag in the prompt, no summarizer) |
| basic_readout_mt | 0 (regex) | 1 summarizer         |
| hallucination    | 1 span    | 1 summarizer + 1 span |

``verify=1`` adds one claim-verification call per hallucination cell. Empty cells make no call.
The benchmark's one-call key/model preflight is run here, inside the journal, so its cost is
recorded too; it then marks the model as preflighted so the benchmark skips its own.

Exit codes: 2 guard refusal / chunk over projection / benchmark usage error; 3 key or judge
config; 4 the run did not end clean (cells still unjudged after retries, a final pass that tried
to call, or an unrestricted run that is not ``complete``).
"""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import re
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from emx import spend
from emx.i1a import contracts as C

# Conservative $/call until the judge pilot (P2) measures it: above the prereg §7.1 high-case
# per-call rate of every judged item (hallucination prose $17.69 / 5,615 = $0.00315; token
# hallucination $0.0021; multi-hop $0.0015), at Gemini 3.8 Flash $0.75 in / $3.75 out per MTok.
DEFAULT_USD_PER_CALL = 0.0035
MIN_USD_PER_CALL = 0.0015  # floor (the prereg's multi-hop high case); nothing projects lower
RETRY_HEADROOM = 1.25  # projections cover retries after a paid-but-garbled response
# What one HTTP attempt reserves before it is sent (review r3): a defensible maximum for one
# response. A max_tokens=8000 Gemini 3.8 Flash reply costs 8000 x $3.75/MTok = $0.03 of output,
# ~10x the high per-call estimate. Every pass projection carries one such attempt of slack
# (``project``) so a small pass can always send its first request.
MAX_USD_PER_ATTEMPT = 0.03
# 4xx replies where the provider rejected the request before generating anything (bad request,
# auth, credits, not found, too large, unprocessable, rate limited): journaled at $0, flagged.
REJECTED_UNBILLED = frozenset({400, 401, 402, 403, 404, 413, 422, 429})
MAX_RETRY_ROUNDS = 2  # guarded re-runs of chunks that left cells unjudged
EXIT_NOT_CLEAN = 4
KEY_ENV = "OPENROUTER_API_KEY_I1A"  # the dedicated, credit-limited key (prereg §7.3)
TOKEN_ARMS = frozenset(
    {"logit_lens", "jlens", "ridge_probe", "logit_lens_oldsite", "jlens_oldsite"}
)
MONITOR_LAYER = 44  # text_monitor multihop / hallucination are judged at one layer (plan §0)


class JudgeRunError(SystemExit):
    """A run that must stop non-zero; the message goes to stderr."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(code)
        self.message = message
        if message:
            print(f"[i1a judge] {message}", file=sys.stderr)


def project(calls: int, usd: float) -> float:
    """A pass's projection: calls x rate x retry headroom, plus one maximum attempt of slack."""
    return round(calls * usd * RETRY_HEADROOM + (MAX_USD_PER_ATTEMPT if calls else 0.0), 6)


def usd_rate(usd_per_call: float | None) -> float:
    """The projection rate: the default, or an override that is at least the floor."""
    if usd_per_call is None:
        return DEFAULT_USD_PER_CALL
    usd = float(usd_per_call)
    if not usd >= MIN_USD_PER_CALL:
        raise ValueError(f"usd_per_call ${usd} is below the floor ${MIN_USD_PER_CALL}")
    return usd


# --------------------------------------------------------------------------------------- key


def load_key(env_path: str | Path | None = None) -> None:
    """Set ``OPENROUTER_API_KEY`` for this process from ``OPENROUTER_API_KEY_I1A`` in ``.env``.
    Refuses (exit 3) when the key is missing or is not an OpenRouter key. Never prints it."""
    from dotenv import dotenv_values

    path = Path(env_path) if env_path is not None else C.REPO_ROOT / ".env"
    vals = dotenv_values(path) if path.exists() else {}
    key = (vals.get(KEY_ENV) or "").strip()
    if not key:
        print(f"[i1a judge] {KEY_ENV} missing from {path}; refusing", file=sys.stderr)
        raise SystemExit(3)
    if not key.startswith("sk-or-"):
        print(
            f"[i1a judge] {KEY_ENV} is not an OpenRouter key (sk-or-...); refusing", file=sys.stderr
        )
        raise SystemExit(3)
    os.environ["OPENROUTER_API_KEY"] = key
    print(f"[i1a judge] loaded {KEY_ENV} from {path} into OPENROUTER_API_KEY")


# ------------------------------------------------------------------- prerequisites (review r2)

APPROVALS_DIR = C.REPO_ROOT / "logs" / "approvals"  # overridable in tests
# the same rule as scripts/pod_run_i1a.sh approved(): non-empty, a line starting with a UTC stamp
MARKER_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.MULTILINE)
STAGES = ("pilot", "gate", "samples")
STAGE_MARKERS = {"pilot": ("P2",), "gate": ("P3",), "samples": ("P4",)}
_SAMPLE_ARM = re.compile(r"^(sel_.+|olens_s[1-9])$")


def pilot_record_path() -> Path:
    """Written after a pilot (``limit=``) run with its measured $/call; P3 judging needs it."""
    return C.JUDGED_DIR / "PILOT.json"


def expected_stage(arm: str, limit: int = 0) -> str:
    """The checkpoint a paid run belongs to: a ``limit=`` run is the P2 pilot; selection-rule and
    per-sample arms are P4 sample scoring; everything else (gate readers, baselines, reserve) is
    P3."""
    if limit > 0:
        return "pilot"
    return "samples" if _SAMPLE_ARM.match(arm) else "gate"


def marker_ok(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(text.strip()) and MARKER_UTC.search(text) is not None


# Which pod step's manifest carries each required check (review r3). None: a laptop-only check
# with no step manifest (the vendored benchmark's offline test run); its file is the evidence.
CHECK_STEP: dict[str, str | None] = {
    "offline_tests": None,
    "backend": "smoke",
    "capture_repeat": "smoke",
    "logit_lens_last_layer": "smoke",
    "jlens_reference": "smoke",
    "olens_adapter_live": "smoke",
    "olens_batching": "smoke",
    "memory_speed": "smoke",
    "store_vs_producer": "tokens",
}


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def step_manifest(step: str) -> dict[Path, str] | str:
    """``{absolute path: sha256}`` from ``MANIFEST.<step>.sha256`` (paths repo-relative), or a
    reason string when the manifest pair is missing, malformed, for another step, or from a
    limited (cut-down) run."""
    man = C.manifest_path(step)
    side = man.with_suffix(".json")
    try:
        d = json.loads(side.read_text(encoding="utf-8"))
        text = man.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        return f"step {step} manifest {man} / {side.name} missing or unreadable ({e})"
    if not isinstance(d, dict) or d.get("step") != step:
        return f"{side} is not the sidecar of step {step}"
    if d.get("limited", False) is not False:
        return f"{side} records a limited (cut-down) run"
    out: dict[Path, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            return f"{man}: malformed line {line!r}"
        out[(C.REPO_ROOT / parts[1].lstrip("*")).resolve()] = parts[0].lower()
    return out


def pilot_record_problems() -> list[str]:
    """Why ``PILOT.json`` does not show a completed pilot: every gate family needs an entry from
    a non-dry run with calls, spend and $/call all above zero."""
    path = pilot_record_path()
    try:
        runs = json.loads(path.read_text(encoding="utf-8")).get("runs", {})
    except (OSError, ValueError, AttributeError):
        return [f"pilot record {path} missing or unreadable (run the P2 pilot first)"]
    out = []
    for fam in C.FAMILIES:
        good = [
            r
            for r in runs.values()
            if isinstance(r, dict)
            and r.get("family") == fam
            and r.get("dry_run") is False
            and all(
                isinstance(r.get(k), int | float) and r[k] > 0
                for k in ("calls", "spend_usd", "usd_per_call")
            )
        ]
        if not good:
            out.append(f"pilot record {path}: no measured non-dry pilot entry for {fam}")
    return out


def preflight_gate(stage: str) -> None:
    """Refuse (exit 2) a paid run unless every required check passed (``C.CHECK_KIND`` kind
    "required", a missing file counts as failed) AND its file is listed with a matching sha256
    in its step's ``MANIFEST.<step>.sha256`` whose sidecar is not ``limited`` (``CHECK_STEP``;
    the laptop-only ``offline_tests`` is exempt), and the stage's approval marker exists in
    ``APPROVALS_DIR``; P3 also needs a measured, non-dry pilot entry for every gate family in
    ``PILOT.json``. Dry runs and call-free (regex) passes never reach this."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}, got {stage!r}")
    missing: list[str] = []
    manifests: dict[str, dict[Path, str] | str] = {}
    for name, kind in C.CHECK_KIND.items():
        if kind != "required":
            continue
        p = C.check_path(name)
        try:
            ok = json.loads(p.read_text(encoding="utf-8")).get("passed") is True
        except (OSError, ValueError):
            ok = False
        if not ok:
            missing.append(f"check {name} ({p} missing or not passed)")
            continue
        if name not in CHECK_STEP:
            missing.append(f"check {name} has no step manifest mapped (judge_driver.CHECK_STEP)")
            continue
        step = CHECK_STEP[name]
        if step is None:  # laptop-only check (offline_tests): the file itself is the evidence
            continue
        if step not in manifests:
            manifests[step] = step_manifest(step)
        listed = manifests[step]
        if isinstance(listed, str):
            missing.append(f"check {name}: {listed}")
            continue
        want = listed.get(p.resolve())
        if want is None:
            missing.append(f"check {name}: {p} is not in {C.manifest_path(step)}")
        elif want != _sha256(p):
            missing.append(f"check {name}: {p} changed since {C.manifest_path(step)} was written")
    for m in STAGE_MARKERS[stage]:
        p = APPROVALS_DIR / f"{C.EXP}-{m}.approved"
        if not marker_ok(p):
            missing.append(f"approval {p} (missing, empty or no UTC-timestamp line)")
    if stage == "gate":
        missing.extend(pilot_record_problems())
    if missing:
        raise JudgeRunError(
            2,
            f"REFUSED paid {stage} judging; missing prerequisites:\n  - " + "\n  - ".join(missing),
        )


def write_pilot_record(name: str, calls: int, usd: float, limit: int) -> Path:
    """Merge one (non-dry) pilot run's measured cost into ``PILOT.json``; ``name`` is
    ``<arm>/<family>``."""
    path = pilot_record_path()
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"runs": {}}
    doc.setdefault("runs", {})[name] = {
        "utc": spend.utc_now(),
        "arm": name.split("/", 1)[0],
        "family": name.split("/", 1)[1],
        "dry_run": False,
        "limit": limit,
        "calls": calls,
        "spend_usd": usd,
        "usd_per_call": round(usd / calls, 6) if calls else None,
    }
    rates = [r["usd_per_call"] for r in doc["runs"].values() if r.get("usd_per_call") is not None]
    doc["utc"] = spend.utc_now()
    doc["usd_per_call_max"] = max(rates) if rates else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------------------- the benchmark


def family_spec(family: str) -> Any:
    """The registered ``EvalSpec`` (registering it again if a test restored the registry)."""
    from wsbench import registry

    registry.load_all()
    if family not in registry.FAMILIES:
        mod = importlib.import_module(f"wsbench.evals.{family}")
        registry.FAMILIES.setdefault(family, mod.SPEC)
    return registry.get(family)


def bank_items(family: str) -> list[dict]:
    """The family's bank items (bank order), each with its readout ``id``."""
    from wsbench.banks import label_of
    from wsbench.registry import REPO_ROOT

    spec = family_spec(family)
    d = json.loads((REPO_ROOT / spec.bank).read_text(encoding="utf-8"))
    items = d["items"] if isinstance(d, dict) else d
    for it in items:
        it.setdefault("id", label_of(it["name"]) if "name" in it else None)
    return items


def judge_model(family: str) -> Any:
    """The family's pinned judge (``ResolvedJudge``). Refuses an env override: I1a needs the
    pinned instrument."""
    from wsbench.judge_config import ENV_OVERRIDE, resolve

    if os.environ.get(ENV_OVERRIDE):
        raise SystemExit(f"{ENV_OVERRIDE} is set; I1a judges with the pinned instrument only")
    return resolve(family_spec(family).judge, env={})


def preflight(judge: Any) -> None:
    """``wsbench.llm.preflight`` (same prompt, same check), run inside the caller's journal so
    its cost is recorded; marks the model as preflighted for the benchmark. A no-op if done
    already in this process."""
    from wsbench import llm, mcjudge

    k = mcjudge.preflight_key(judge.model, judge.reasoning)
    with mcjudge._PREFLIGHT_LOCK:
        if k in mcjudge._PREFLIGHTED:
            return
        out: list = []
        llm.stream_json(
            [("Reply JSON.", 'Return {"a":1}')],
            schema=llm.schema_block("t", {"a": {"type": "integer"}}, ["a"]),
            model=judge.model,
            reasoning=judge.reasoning,
            concurrency=1,
            on_result=lambda _i, r: out.append(r),
        )
        if out != [{"a": 1}]:
            raise llm.JudgeConfigError(f"preflight failed for {judge.model}: {out}")
        mcjudge._PREFLIGHTED.add(k)


# ---------------------------------------------------------------------------- spend journal


class AttemptRefused(Exception):
    """Raised by the journaled client instead of sending a request that the pass's budget does
    not cover (or any request in a cache-only pass). ``status_code`` 403 makes the benchmark
    treat it as fatal (``JudgeConfigError``): no retry, nothing sent."""

    status_code = 403


CacheOnlyViolation = AttemptRefused  # the cache-only refusal is the zero-budget case


def journal_path(arm: str, family: str) -> Path:
    return C.judged_dir(arm, family) / "spend_journal.jsonl"


def ledger_tag(name: str) -> str:
    """The ledger note prefix that ties entries to one journal (e.g. ``[I1a jlens/multihop]``)."""
    return f"[{C.EXP} {name}]"


def append_journal(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_journal(path: Path) -> list[dict]:
    """Journal rows; a torn last line (a crash mid-write) is ignored, any other bad line raises."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            text = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out = []
    for i, ln in enumerate(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            if i == len(lines) - 1 and not text.endswith("\n"):
                print(f"[i1a spend] ignoring a torn last line in {path}", file=sys.stderr)
                continue
            raise RuntimeError(f"{path}:{i + 1}: malformed journal line") from None
    return out


def journal_totals(path: Path) -> tuple[float, int]:
    rows = read_journal(path)
    return round(sum(float(r["cost"]) for r in rows), 6), len(rows)


def ledger_tagged_usd(name: str) -> float:
    tag = ledger_tag(name) + " "
    return round(
        sum(
            float(e.get("usd", 0.0))
            for e in spend.read_entries()
            if e.get("exp") == C.EXP and str(e.get("note", "")).startswith(tag)
        ),
        6,
    )


def cap_room(path: Path, name: str) -> float:
    """Judge dollars still allowed under every cap: ``check_total(0)`` on the ledger, minus any
    spend the journal holds that the ledger does not yet (refuses, exit 2, if already over)."""
    b = spend.check_total(0.0, "judge", exp=C.EXP)
    unrecorded = max(0.0, journal_totals(path)[0] - ledger_tagged_usd(name))
    room = min(
        b["judge_limit_usd"] - b["judge_after_usd"], b["hard_cap_usd"] - b["total_after_usd"]
    )
    return round(room - unrecorded, 6)


class Budget:
    """Per-pass reservation ledger for HTTP attempts, retries included (review r2 and r3).

    Each attempt reserves ``per_attempt = max(rate, MAX_USD_PER_ATTEMPT)`` before it is sent,
    a defensible maximum for one response, and settles to what it is charged when it ends. An
    attempt that would take settled + in flight + ``per_attempt`` past ``min(limit, room)`` waits
    while other attempts are in flight (they may free room) and is refused once none are. A
    settled cost above its reservation is recorded, marks the budget refused, and every later
    attempt is refused, so the pass stops."""

    def __init__(self, limit: float, rate: float, room: float, *, cache_only: bool = False):
        import threading

        self.limit, self.rate, self.room = limit, rate, room
        self.per_attempt = max(rate, MAX_USD_PER_ATTEMPT)
        self.cache_only = cache_only
        self.spent = 0.0
        self.inflight = 0.0
        self.attempts = 0
        self.refused: str | None = None
        self._lock = threading.Lock()

    def _try(self) -> bool | None:
        """True: reserved. None: wait (others in flight). Raises AttemptRefused otherwise."""
        with self._lock:
            if self.cache_only:
                self.refused = "a cache-only pass tried to send a request"
            if self.refused:
                raise AttemptRefused(self.refused)
            cap = min(self.limit, self.room)
            if self.spent + self.inflight + self.per_attempt <= cap + 1e-9:
                self.inflight += self.per_attempt
                self.attempts += 1
                return True
            if self.inflight > 0:
                return None
            what = (
                f"the pass projection ${self.limit:.4f}"
                if cap == self.limit
                else f"the cap headroom ${self.room:.4f}"
            )
            self.refused = (
                f"attempt {self.attempts + 1} refused: ${self.spent:.4f} spent + a "
                f"${self.per_attempt:.4f} reservation would pass {what}"
            )
            raise AttemptRefused(self.refused)

    def reserve(self) -> None:
        """Synchronous form (never waits): for tests and single-threaded callers."""
        if self._try() is None:
            with self._lock:
                self.refused = "no room while other attempts are in flight"
            raise AttemptRefused(self.refused)

    async def areserve(self) -> None:
        import asyncio

        while self._try() is None:
            await asyncio.sleep(0.01)

    def settle(self, cost: float) -> None:
        """Release one reservation, charging ``cost`` (0 for a request that was never billed)."""
        with self._lock:
            self.inflight = max(0.0, self.inflight - self.per_attempt)
            self.spent += cost
            if cost > self.per_attempt + 1e-9 and not self.refused:
                self.refused = (
                    f"a response cost ${cost:.4f}, more than its ${self.per_attempt:.4f} "
                    "reservation; no further attempts"
                )


def _status(e: BaseException) -> int | None:
    s = getattr(e, "status_code", None)
    return s if isinstance(s, int) else None


class _JournalClient:
    """Wraps the benchmark's OpenRouter client: every attempt reserves budget first, and every
    attempt that may have been billed is journaled before the benchmark sees its outcome:
    - a response: its ``usage.cost`` (the reservation if the cost is missing);
    - an exception after the request may have reached the provider (timeout, connection
      error, 5xx, cancellation, anything unknown): the reservation, flagged ``uncertain``;
    - a 4xx rejection in ``REJECTED_UNBILLED`` (the provider refused the request, nothing was
      generated): a zero-cost row flagged ``rejected``;
    - our own refusal (``AttemptRefused``) happens before sending and is not journaled."""

    def __init__(self, inner: Any, path: Path, fallback_usd: float, budget: Budget):
        from types import SimpleNamespace

        self._inner, self._path = inner, path
        self._fallback, self._budget = fallback_usd, budget
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _journal(self, model: Any, cost: float, **flags: Any) -> None:
        append_journal(
            self._path,
            {
                "call_id": uuid.uuid4().hex,
                "utc": spend.utc_now(),
                "model": model,
                "cost": cost,
                **flags,
            },
        )

    async def _create(self, **kw: Any) -> Any:
        await self._budget.areserve()
        held = self._budget.per_attempt
        try:
            resp = await self._inner.chat.completions.create(**kw)
        except BaseException as e:
            status = _status(e)
            if status in REJECTED_UNBILLED:
                self._journal(kw.get("model"), 0.0, rejected=status)
                self._budget.settle(0.0)
            else:
                self._journal(
                    kw.get("model"), held, uncertain=True, error=type(e).__name__, status=status
                )
                self._budget.settle(held)
            raise
        usage = getattr(resp, "usage", None)
        cost = getattr(usage, "cost", None) if usage is not None else None
        if cost is None and usage is not None:
            cost = (getattr(usage, "model_extra", None) or {}).get("cost")
        usd = float(cost) if cost is not None else held
        self._journal(kw.get("model"), usd, cost_missing=cost is None)
        self._budget.settle(usd)
        return resp

    async def close(self) -> None:
        await self._inner.close()


@contextmanager
def journaled(
    path: Path,
    *,
    fallback_usd: float,
    budget: Budget | None = None,
    cache_only: bool = False,
) -> Iterator[Budget]:
    """Route every client the benchmark builds through ``_JournalClient`` for the block, every
    attempt reserved against ``budget``. A budget is required, except for a cache-only pass
    (which sends nothing). Yields the budget."""
    from wsbench import llm

    if budget is None and cache_only:
        budget = Budget(0.0, fallback_usd, 0.0, cache_only=True)
    if budget is None:
        raise ValueError("journaled() needs a budget (or cache_only=True)")
    inner = llm._make_client

    def make(route: str, key: str) -> Any:
        if route != "openrouter":
            raise llm.JudgeConfigError(f"I1a judges on the OpenRouter route only, not {route}")
        return _JournalClient(inner(route, key), path, fallback_usd, budget)

    llm._make_client = make
    try:
        yield budget
    finally:
        llm._make_client = inner


def reconcile(path: Path, name: str, note: str) -> float:
    """Record to the ledger whatever ``path`` holds beyond the ledger entries tagged
    ``ledger_tag(name)``, per model (dollars and calls). Returns the dollars recorded."""
    tag = ledger_tag(name)
    j: dict[str, list[float]] = {}
    for r in read_journal(path):
        acc = j.setdefault(str(r.get("model")), [0.0, 0])
        acc[0] += float(r["cost"])
        acc[1] += 1
    led: dict[str, list[float]] = {}
    for e in spend.read_entries():
        if e.get("exp") == C.EXP and str(e.get("note", "")).startswith(tag + " "):
            acc = led.setdefault(str(e.get("resource")), [0.0, 0])
            acc[0] += float(e.get("usd", 0.0))
            acc[1] += int(e.get("units", 0))
    recorded = 0.0
    for model, (usd, calls) in sorted(j.items()):
        l_usd, l_calls = led.get(model, [0.0, 0])
        d_usd, d_calls = round(usd - l_usd, 6), int(calls - l_calls)
        if d_calls > 0 or d_usd > 1e-5:
            spend.record(
                "judge", model, max(d_calls, 0), max(d_usd, 0.0), f"{tag} {note}", exp=C.EXP
            )
            recorded += max(d_usd, 0.0)
        elif d_usd < -1e-5 or d_calls < 0:
            print(
                f"[i1a spend] WARNING {tag}: the ledger holds more than the journal "
                f"({l_usd:.6f} > {usd:.6f} USD)",
                file=sys.stderr,
            )
    return round(recorded, 6)


def run_wsbench(argv: Sequence[str]) -> int:
    """``wsbench.cli.main`` in-process; a family's ``SystemExit`` becomes its exit code."""
    from wsbench import cli

    try:
        return int(cli.main(list(argv)) or 0)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2


# ------------------------------------------------------------------------------- projection


def verify_on(opts: Sequence[str]) -> bool:
    d = dict(o.split("=", 1) for o in opts if "=" in o)
    return d.get("verify", "1") != "0"


def calls_per_cell(family: str, kind: str, *, verify: bool = False) -> int:
    """Judge calls one non-empty cell costs (see the module table)."""
    tokens = kind == "tokens"
    if family == "multihop":
        return 1
    if family == "basic_readout_mt":
        return 1 if tokens else 0
    if family == "hallucination":
        return (1 if tokens else 0) + 1 + (1 if verify else 0)
    raise ValueError(f"not an I1a family: {family!r}")


def project_usd(
    arm: str,
    family: str,
    n_items: int,
    usd_per_call: float | None = None,
    *,
    n_layers: int | None = None,
    verify: bool = False,
) -> float:
    """Planning estimate for ``n_items`` items of a full readouts file (hallucination uses the
    bank's mean 1,123 / 149 sites per item). ``judge_arm`` projects each chunk exactly instead."""
    usd = DEFAULT_USD_PER_CALL if usd_per_call is None else usd_per_call
    kind = "tokens" if arm in TOKEN_ARMS else "prose"
    layers = len(C.FAMILY_LAYERS[family]) if n_layers is None else n_layers
    sites = C.N_SITES[family] / C.N_ITEMS[family]
    return round(n_items * sites * layers * calls_per_cell(family, kind, verify=verify) * usd, 6)


def count_calls(
    family: str,
    readouts: Path,
    ids: Sequence[str],
    layers: Sequence[int] | None,
    *,
    verify: bool = False,
) -> tuple[dict[str, int], str]:
    """Exact fresh-run call count per item id, and the file's kind, the way the family builds
    its calls (non-empty cells only; hallucination rows only on bank read sites)."""
    from wsbench.readouts import load_readouts

    per: dict[str, int] = dict.fromkeys(ids, 0)
    if family == "hallucination":
        sites = {it["id"]: [int(s["pos"]) for s in it["sites"]] for it in bank_items(family)}
        positions = {i: sites[i] for i in ids if i in sites}
        cells, rep = load_readouts(readouts, layers=layers, positions=positions)
        live = [c.id for c in cells if not c.empty]  # one call set per (item, layer, site)
    elif family == "basic_readout_mt":
        cells, rep = load_readouts(readouts, ids=ids, layers=layers)
        live = [c.id for c in cells if not c.empty]  # one summary per token cell
    else:  # multihop: one call per (item, layer) group with any text
        cells, rep = load_readouts(readouts, ids=ids, layers=layers)
        live = [i for (i, _layer) in {(c.id, c.layer) for c in cells if not c.empty}]
    kind = rep.kind or "prose"
    k = calls_per_cell(family, kind, verify=verify)
    for i in live:
        per[i] = per.get(i, 0) + k
    return per, kind


# ---------------------------------------------------------------------------------- the run


def mark_scope_curtailed(
    reason: str, cut: str, arms: Sequence[str], families: Sequence[str]
) -> Path:
    """Write ``contracts.scope_curtailed_path()`` for a pre-set judge cut that removes gate cells
    (prereg §7.3; e.g. token-reader hallucination on the seeded 75 conversations only). Its
    presence makes the gate "scope_curtailed" (U6). Only gate arms on gate families qualify: a
    sample-scoring cut (selection rules, per-sample arms) never touches the gate and is refused.
    A second cut is added to ``cuts``; the top level keeps ``{utc, reason, cut, arms, families}``
    with the arms and families of every cut so far."""
    arms, families = sorted(set(arms)), sorted(set(families))
    if not arms or not families:
        raise ValueError("a gate cut names at least one arm and one family")
    not_gate = [a for a in arms if a not in C.GATE_ARMS]
    if not_gate:
        raise ValueError(
            f"{not_gate} are not gate arms {C.GATE_ARMS}: sample-scoring cuts do not touch the "
            "gate and never mark it scope-curtailed"
        )
    bad = [f for f in families if f not in C.FAMILIES]
    if bad:
        raise ValueError(f"not gate families: {bad}")
    if not reason.strip() or not cut.strip():
        raise ValueError("a gate cut needs a reason and a cut name")
    entry = {
        "utc": spend.utc_now(),
        "reason": reason,
        "cut": cut,
        "arms": arms,
        "families": families,
    }
    path = C.scope_curtailed_path()
    cuts = []
    if path.exists():
        cuts = json.loads(path.read_text(encoding="utf-8")).get("cuts", [])
    same = [c for c in cuts if (c["cut"], c["arms"], c["families"]) == (cut, arms, families)]
    if same:  # a resumed run of the same cut: already marked
        return path
    cuts.append(entry)
    doc = {
        **cuts[0],
        "utc": entry["utc"],
        "arms": sorted({a for c in cuts for a in c["arms"]}),
        "families": sorted({f for c in cuts for f in c["families"]}),
        "cuts": cuts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(
        f"[i1a judge] gate scope curtailed ({cut}: {', '.join(arms)} on {', '.join(families)})"
        f" -> {path}"
    )
    return path


def _read_results(out: Path) -> dict | None:
    p = out / "results.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _argv(
    family: str,
    readouts: Path,
    out: Path,
    *,
    items: Sequence[str] | None,
    limit: int,
    layers: Sequence[int] | None,
    opts: Sequence[str],
    allow_missing: bool,
    dry_run: bool,
) -> list[str]:
    argv = ["judge", f"family={family}", f"readouts={readouts}", f"out={out}"]
    if items is not None:
        bad = [i for i in items if "," in i]
        if bad:
            raise ValueError(f"item ids with commas cannot pass through items=: {bad[:3]}")
        argv.append(f"items={','.join(items)}")
    if limit:
        argv.append(f"limit={limit}")
    if layers:
        argv.append(f"layers={','.join(str(x) for x in layers)}")
    if opts:
        argv.append(f"opts={','.join(opts)}")
    if allow_missing:
        argv.append("allow_missing=True")
    if dry_run:
        argv.append("dry_run=True")
    return argv


class _Run:
    """One (arm, family) run: its journal, its ledger tag and its guarded benchmark passes."""

    def __init__(self, arm: str, family: str, judge: Any, usd: float, common: dict):
        self.name = f"{arm}/{family}"
        self.family = family
        self.judge, self.usd, self.common = judge, usd, common
        self.readouts = C.readouts_path(arm, family)
        self.out = C.judged_dir(arm, family)
        self.journal = journal_path(arm, family)

    def guard(self, label: str, calls: int) -> float:
        """Print and check the projection for ``calls`` calls (with retry headroom)."""
        proj = project(calls, self.usd)
        print(
            f"[i1a judge] {self.name} {label}: {calls} calls, projected ${proj:.4f} "
            f"(${self.usd:.4f}/call x {RETRY_HEADROOM} + ${MAX_USD_PER_ATTEMPT} slack)"
        )
        spend.check_total(proj, "judge", exp=C.EXP)
        return proj

    def run(
        self,
        label: str,
        *,
        items,
        limit: int,
        proj: float,
        cache_only: bool = False,
        first: bool = False,
    ) -> dict:
        """One benchmark pass inside the journal; reconciles after it and stops the run if the
        pass spent more than its projection or exited non-zero."""
        res_path = self.out / "results.json"
        if res_path.exists():
            res_path.unlink()  # never read a stale results.json as this pass's
        usd0, n0 = journal_totals(self.journal)
        argv = _argv(
            self.family,
            self.readouts,
            self.out,
            items=items,
            limit=limit,
            dry_run=False,
            **self.common,
        )
        from wsbench.llm import JudgeConfigError

        budget = Budget(proj, self.usd, cap_room(self.journal, self.name), cache_only=cache_only)
        try:
            with journaled(self.journal, fallback_usd=self.usd, budget=budget):
                if first:
                    try:
                        preflight(self.judge)
                    except JudgeConfigError:
                        if budget.refused is None:
                            raise
                code = 3 if budget.refused else run_wsbench(argv)
        finally:
            reconcile(self.journal, self.name, label)
        usd1, n1 = journal_totals(self.journal)
        res = _read_results(self.out)
        row: dict[str, Any] = {
            "label": label,
            "items": list(items) if items is not None else None,
            "projected_usd": proj,
            "new_calls": n1 - n0,
            "spend_usd": round(usd1 - usd0, 6),
            "exit_code": code,
        }
        if res is not None:
            row.update(
                complete=res.get("complete"),
                value=res["numbers"].get("value"),
                n_unjudged_cells=int(res["counts"].get("n_unjudged_cells") or 0),
                spend_usd_benchmark=res["counts"].get("spend_usd"),
            )
        row["attempts"] = budget.attempts
        if cache_only and budget.refused:
            raise JudgeRunError(
                EXIT_NOT_CLEAN, f"{self.name} {label}: the cache-only pass needed a new call"
            )
        if budget.refused:
            raise JudgeRunError(
                2,
                f"{self.name} {label}: stopped mid-pass, {budget.refused}; spend so far is "
                "recorded (raise --usd-per-call if the rate is too low)",
            )
        if code != 0:
            raise JudgeRunError(code, f"{self.name} {label}: wsbench exited {code}")
        if row["spend_usd"] > proj + 1e-9:
            raise JudgeRunError(
                2,
                f"{self.name} {label}: spent ${row['spend_usd']:.4f}, over "
                f"its projection ${proj:.4f}; stopping (raise --usd-per-call)",
            )
        return row


def judge_arm(
    arm: str,
    family: str,
    *,
    items: Sequence[str] | None = None,
    limit: int = 0,
    layers: Sequence[int] | None = None,
    opts: Sequence[str] = ("verify=0",),
    chunk: int = 25,
    usd_per_call: float | None = None,
    dry_run: bool = False,
    allow_missing: bool = False,
    allow_verify: bool = False,
    gate_cut: str | None = None,
    gate_cut_reason: str = "",
    stage: str | None = None,
) -> dict:
    """Judge ``readouts/<arm>/<family>.jsonl`` into ``judged/<arm>/<family>/`` (module doc).

    ``stage`` (pilot / gate / samples) defaults to ``expected_stage(arm, limit)`` and must equal
    it; a paid run first passes ``preflight_gate(stage)``. A pilot writes ``PILOT.json``.

    ``items`` / ``limit`` (pilot) / ``layers`` restrict the scope; a restricted result is not
    ``complete``, as the benchmark rules, and says so. ``allow_missing`` passes through (subset
    arms, toys). A gate arm (``GATE_ARMS``) restricted with ``items=`` to fewer than the whole bank
    loses gate cells: that needs ``gate_cut`` (the pre-set cut's name, prereg §7.3) and writes
    ``SCOPE_CURTAILED.json`` via ``mark_scope_curtailed`` before any call. Raises
    ``BudgetExceeded`` (exit 2) on a cap refusal and ``JudgeRunError`` on a run that must stop
    (its code: 2, 3 or ``EXIT_NOT_CLEAN``)."""
    if family not in C.ARM_FAMILIES.get(arm, ()):
        raise ValueError(f"arm {arm!r} has no {family!r} readouts (contracts.ARM_FAMILIES)")
    if chunk < 1:
        raise ValueError("chunk must be >= 1")
    usd = usd_rate(usd_per_call)
    want_stage = expected_stage(arm, limit)
    stage = stage or want_stage
    if stage != want_stage:
        raise ValueError(f"{arm} with limit={limit} is a {want_stage!r} run, not {stage!r}")
    opts = tuple(opts)
    if any(o.split("=", 1)[0] == "judge" for o in opts):
        raise ValueError("opts=judge=... (the mc judge) is not part of I1a")
    verify = verify_on(opts)
    if family == "hallucination" and verify and not allow_verify:
        raise ValueError(
            "hallucination is judged with opts=verify=0 (plan §0); stage-2 verification needs "
            "allow_verify=True (the checkpoint-5 add-on)"
        )
    if family != "hallucination":
        verify = False
    if layers is None and arm == "text_monitor" and family in ("multihop", "hallucination"):
        layers = [MONITOR_LAYER]
        print(f"[i1a judge] text_monitor/{family}: judged at layer {MONITOR_LAYER} only (plan §0)")
    layers = sorted(int(x) for x in layers) if layers else None
    restricted = items is not None or limit > 0 or layers is not None
    readouts = C.readouts_path(arm, family)
    if not readouts.exists():
        raise FileNotFoundError(f"no readouts at {readouts}")
    out = C.judged_dir(arm, family)
    out.mkdir(parents=True, exist_ok=True)

    judge = judge_model(family)
    bank_ids = [it["id"] for it in bank_items(family)]
    if items is not None:
        want = set(items)
        unknown = sorted(want - set(bank_ids))
        if unknown:
            raise ValueError(f"{len(unknown)} ids not in the {family} bank: {unknown[:5]}")
        scope = [i for i in bank_ids if i in want]
        if arm in C.GATE_ARMS and len(scope) < len(bank_ids) and gate_cut is None:
            raise ValueError(
                f"items= drops {len(bank_ids) - len(scope)} {family} items from gate arm {arm}: "
                "a gate cut; pass gate_cut=<cut> (writes SCOPE_CURTAILED.json) or judge it all"
            )
    else:
        scope = list(bank_ids)
    if limit > 0:
        scope = scope[:limit]
    per_item, kind = count_calls(family, readouts, scope, layers, verify=verify)
    total_calls = sum(per_item.values())
    live = [i for i in scope if per_item.get(i, 0) > 0]
    chunks = [live[k : k + chunk] for k in range(0, len(live), chunk)]
    common = {"layers": layers, "opts": opts, "allow_missing": allow_missing}
    r = _Run(arm, family, judge, usd, common)
    print(
        f"[i1a judge] {r.name}: kind={kind} items={len(scope)} layers={layers or 'all'} "
        f"calls={total_calls} chunks={len(chunks)} usd/call=${usd:.4f} "
        f"projected=${project(total_calls, usd):.4f} opts={','.join(opts) or '-'}"
        + (" RESTRICTED (items/limit/layers): the result cannot be complete" if restricted else "")
    )
    summary: dict[str, Any] = {
        "arm": arm,
        "family": family,
        "kind": kind,
        "items": len(scope),
        "layers": layers,
        "restricted": restricted,
        "projected_calls": total_calls,
        "usd_per_call": usd,
        "chunks": [],
        "retries": [],
        "final": None,
    }
    final_items = list(items) if items is not None else None

    if dry_run:
        proj = project(total_calls, usd)
        print(f"[i1a judge] {r.name}: DRY RUN, projected ${proj:.4f}; no calls")
        if total_calls:
            summary["guard"] = spend.check_total(proj, "judge", exp=C.EXP)
        run_wsbench(
            _argv(family, readouts, out, items=final_items, limit=limit, dry_run=True, **common)
        )
        return summary

    if total_calls:  # paid: the checks, the stage's approval and (P3) the pilot record
        preflight_gate(stage)
    if gate_cut is not None:
        summary["scope_curtailed"] = str(
            mark_scope_curtailed(gate_cut_reason or gate_cut, gate_cut, [arm], [family])
        )
    orphan = reconcile(r.journal, r.name, "orphaned spend from an earlier run")
    if orphan:
        print(f"[i1a judge] {r.name}: recorded ${orphan:.4f} of orphaned journal spend")
    cpc = calls_per_cell(family, kind, verify=verify)
    todo: list[tuple[list[str], int]] = []  # (chunk ids, cells left unjudged)
    for k, ids in enumerate(chunks, 1):
        n = sum(per_item[i] for i in ids) + (1 if k == 1 else 0)  # + the preflight
        label = f"chunk {k}/{len(chunks)}"
        proj = r.guard(label, n)
        row = r.run(label, items=ids, limit=0, proj=proj, first=k == 1)
        summary["chunks"].append(row)
        todo.append((ids, row.get("n_unjudged_cells", 0)))

    for rnd in range(1, MAX_RETRY_ROUNDS + 1):
        todo = [(ids, n) for ids, n in todo if n > 0]
        if not todo:
            break
        nxt = []
        for k, (ids, n) in enumerate(todo, 1):
            label = f"retry {rnd}.{k}"
            proj = r.guard(label, n * cpc + 1)
            row = r.run(label, items=ids, limit=0, proj=proj, first=True)
            summary["retries"].append(row)
            nxt.append((ids, row.get("n_unjudged_cells", 0)))
        todo = nxt
    left = sum(n for _ids, n in todo)
    if left:
        raise JudgeRunError(
            EXIT_NOT_CLEAN,
            f"{r.name}: {left} cells still unjudged after {MAX_RETRY_ROUNDS} retry rounds; "
            "the final pass was not run",
        )

    row = r.run("final pass", items=final_items, limit=limit, proj=0.0, cache_only=True)
    summary["final"] = row
    if row["new_calls"] or row["spend_usd"]:
        raise JudgeRunError(EXIT_NOT_CLEAN, f"{r.name}: the final pass was not cache-only")
    if not restricted and row.get("complete") is not True:
        raise JudgeRunError(
            EXIT_NOT_CLEAN,
            f"{r.name}: the full-grid run is not complete ({out / 'results.json'}: "
            "missing, unjudged or too many empty cells)",
        )
    passes = [*summary["chunks"], *summary["retries"], row]
    summary["spend_usd"] = round(sum(p["spend_usd"] for p in passes), 6)
    summary["new_calls"] = sum(p["new_calls"] for p in passes)
    summary["results"] = str(out / "results.json")
    if stage == "pilot" and summary["new_calls"]:
        summary["pilot_record"] = str(
            write_pilot_record(r.name, summary["new_calls"], summary["spend_usd"], limit)
        )
    note = " (restricted run: not complete, as expected)" if restricted else ""
    print(
        f"[i1a judge] {r.name}: done, {summary['new_calls']} calls, "
        f"${summary['spend_usd']:.4f}, complete={row.get('complete')}{note} "
        f"value={row.get('value')}"
    )
    return summary
