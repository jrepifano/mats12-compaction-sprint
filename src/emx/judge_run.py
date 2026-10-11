"""Judge one generation CSV into its judged CSV (plan §1 "Judged CSV" contract), resumably.

Port of ../mats12_neel_working/src/em_filter/judge_run.py (`judge_csv`,
`check_and_write_signature`) with the source-pinning of its judge_panel.py sidecars. Kept:
bounded-concurrency asyncio (semaphore), tenacity exponential-backoff retries, the status column
per score column, atomic checkpoints every `save_every` rows and on cancellation, and the refusal
to resume a column with a different judge / prompt / mode / request or to adopt unsigned scores.

Changes for E1:
- Scores never go into the generation CSV (it is never modified). They go into a separate
  judged CSV with exactly the generation CSV's rows, in order, starting with `KEY_COLUMNS`.
- Columns are `{metric}__{judge}` and `{metric}__{judge}__status`.
- One signature file per column, `<judged stem>.<column>.sig.json`, holding the prompt file and
  sha256, the judge settings (model, route, mode, request, temperature, seed) and the SOURCE
  generation CSV's sha256, row count and `keys_sha256` (sha256 of its key columns as CSV).
  Resuming against a changed source is refused.
- `<judged stem>.judging.json`: append-only run history with token usage and dollars per column.
- The spend guard: each (metric, judge) batch is projected and checked with
  `emx.spend.check_total` (running pods' projected cost included, from the pod log) before it
  starts; every call reserves its maximum cost before dispatch and the in-run budget refuses the
  call whose reservation would cross the headroom; failed and unknown-outcome calls are charged
  conservatively; the cost is appended to the ledger also on abort, error or cancellation
  (see `judge_column` for the accounting rules; code review 20260929-E1-code-r1, findings 1-2).

Status semantics (resume):
  scored -- numeric score present; never re-judged
  none   -- the judge answered but gave no usable score (refusal / CODE / < 0.25 numeric weight);
            a real outcome, never re-judged, never converted to a score
  failed -- transport failure after all retries; re-judged on the next run
  (empty) -- row not selected for this metric (e.g. medtopic is scored on EM rows only)

Raw judge text (E13 implementation plan §8 finding 2; opt-in, additive): with `keep_raw=True`
every judged row of a column also writes `{metric}__{judge}__raw` (`raw_column`), the judge's raw
reply (empty on a transport failure), so a REFUSAL reply can be told apart from garbage (both are
status `none`). Raw columns are never coerced to numbers on reload, and with `keep_raw=True` a
`none` row whose raw cell is empty is re-queued (backfill; a reply whose
raw text is itself empty is re-queued on every run). `keep_raw=False` (the default)
leaves every existing caller's behaviour and judged CSV unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from emx import spend
from emx.contracts import (
    JUDGES,
    KEY_COLUMNS,
    METRICS,
    score_column,
    status_column,
)
from emx.judge_client import make_judge
from emx.judge_panel import PRESETS, JudgePreset
from emx.prompts import load_prompt, prompt_path, prompt_sha256

QUESTION_COL = "question"
ANSWER_COL = "response"


class SpendCapExceeded(RuntimeError):
    """In-run budget reached: abort the batch (never retried, never marked failed)."""


def _retryable(e: BaseException) -> bool:
    # tenacity's attempt context swallows BaseException too; without this a cancelled task
    # (asyncio.CancelledError) would be retried and could dispatch new calls after an abort
    return isinstance(e, Exception) and not isinstance(e, SpendCapExceeded)


# --- hashing and paths ----------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def keys_sha256(df: pd.DataFrame) -> str:
    """sha256 of the key columns (`condition, qid, sample_idx`) serialised as CSV, in order."""
    buf = io.StringIO()
    df[list(KEY_COLUMNS)].astype(str).to_csv(buf, index=False)
    return hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()


def _stem(judged_csv: str | Path) -> str:
    p = str(judged_csv)
    return p.removesuffix(".csv")


def sig_path(judged_csv: str | Path, metric: str, judge: str) -> Path:
    return Path(f"{_stem(judged_csv)}.{score_column(metric, judge)}.sig.json")


def judging_path(judged_csv: str | Path) -> Path:
    return Path(f"{_stem(judged_csv)}.judging.json")


def default_judged_path(gen_csv: str | Path) -> Path:
    """`results/E1/gen/<name>` -> `results/E1/judged/<name>` (a sibling `judged/` directory)."""
    g = Path(gen_csv)
    return g.parent.parent / "judged" / g.name


def read_gen(gen_csv: str | Path) -> pd.DataFrame:
    df = pd.read_csv(gen_csv, keep_default_na=False)
    missing = [c for c in (*KEY_COLUMNS, QUESTION_COL, ANSWER_COL) if c not in df.columns]
    if missing:
        raise ValueError(f"{gen_csv}: missing columns {missing}")
    if df.duplicated(list(KEY_COLUMNS)).any():
        raise ValueError(f"{gen_csv}: duplicate ({', '.join(KEY_COLUMNS)}) keys")
    return df


def source_block(gen_csv: str | Path, gen_df: pd.DataFrame) -> dict:
    return {
        "path": str(gen_csv),
        "sha256": sha256_file(gen_csv),
        "n_rows": len(gen_df),
        "keys_sha256": keys_sha256(gen_df),
    }


RAW_SUFFIX = "__raw"


def raw_column(metric: str, judge: str) -> str:
    """`{metric}__{judge}__raw`: the judge's raw reply (same as `emx.e13.contracts.raw_column`)."""
    return f"{score_column(metric, judge)}{RAW_SUFFIX}"


def _same_source(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in ("sha256", "n_rows", "keys_sha256"))


# --- judged frame ---------------------------------------------------------------------------


def load_or_init_judged(
    gen_csv: str | Path, judged_csv: str | Path
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(gen_df, judged_df, source). Refuses an existing judged CSV whose rows or signed source
    no longer match the generation CSV."""
    gen_df = read_gen(gen_csv)
    src = source_block(gen_csv, gen_df)
    judged_csv = Path(judged_csv)
    if not judged_csv.exists():
        return gen_df, gen_df[list(KEY_COLUMNS)].copy(), src
    judged = pd.read_csv(judged_csv, keep_default_na=False, dtype=str)
    if len(judged) != len(gen_df) or keys_sha256(judged) != src["keys_sha256"]:
        raise RuntimeError(
            f"{judged_csv}: rows/keys differ from {gen_csv}; refusing to resume "
            f"(move the judged CSV aside)"
        )
    for metric in METRICS:
        for judge in JUDGES:
            sp = sig_path(judged_csv, metric, judge)
            if sp.exists():
                rec = json.loads(sp.read_text())
                if not _same_source(rec["source"], src):
                    raise RuntimeError(
                        f"{sp}: source generation CSV changed (signed sha "
                        f"{rec['source']['sha256'][:12]}, now {src['sha256'][:12]}); refusing "
                        f"to resume against a different source"
                    )
    for c in judged.columns:
        if c in KEY_COLUMNS:
            continue
        if c.endswith("__status"):
            judged[c] = judged[c].replace("", pd.NA)
        elif c.endswith(RAW_SUFFIX):  # raw judge text: always kept as strings ("" = none kept)
            judged[c] = judged[c].astype(object)
        else:
            judged[c] = pd.to_numeric(judged[c].replace("", pd.NA), errors="coerce")
    judged[list(KEY_COLUMNS)] = gen_df[list(KEY_COLUMNS)].values
    return gen_df, judged, src


def save_judged(judged: pd.DataFrame, judged_csv: str | Path) -> None:
    judged_csv = Path(judged_csv)
    judged_csv.parent.mkdir(parents=True, exist_ok=True)
    tmp = judged_csv.with_name(judged_csv.name + ".tmp")
    judged.to_csv(tmp, index=False)
    os.replace(tmp, judged_csv)


def expected_signature(
    metric: str, preset: JudgePreset, src: dict, settings: dict | None = None
) -> dict:
    """`settings` overrides `preset.settings()` (the batch path signs its serving settings)."""
    prompt = load_prompt(metric)
    return {
        "column": score_column(metric, preset.name),
        "metric": metric,
        "judge": preset.name,
        "prompt_file": str(prompt_path(metric).relative_to(prompt_path(metric).parents[2])),
        "prompt_sha256": prompt_sha256(prompt),
        "judge_model": preset.model,
        "scoring_mode": preset.mode,
        "settings": settings if settings is not None else preset.settings(),
        "source": src,
    }


def check_and_write_signature(
    judged_csv: str | Path,
    metric: str,
    preset: JudgePreset,
    src: dict,
    has_prior: bool,
    settings: dict | None = None,
) -> dict:
    """Refuse to resume a column with a different prompt / judge / settings / source than started
    it, and refuse to adopt prior scores without a signature. Writes the signature if new.
    `settings`: see `expected_signature`. The `batches` key (batch ids that wrote scores, added
    by `emx.judge_batch`) is a record, not a setting, and is not compared."""
    sp = sig_path(judged_csv, metric, preset.name)
    expected = expected_signature(metric, preset, src, settings)
    if sp.exists():
        existing = json.loads(sp.read_text())
        existing.pop("created_utc", None)
        existing.pop("batches", None)
        if existing != expected:
            raise RuntimeError(
                f"column {expected['column']!r} of {judged_csv} was started with {existing}, "
                f"refusing to resume with {expected}"
            )
        return expected
    if has_prior:
        raise RuntimeError(
            f"column {expected['column']!r} in {judged_csv} already has scores but no signature "
            f"-- cannot verify which judge produced them; move the column aside"
        )
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({**expected, "created_utc": spend.utc_now()}, indent=2) + "\n")
    return expected


# --- judging one column ---------------------------------------------------------------------


def prepare_column(
    judged: pd.DataFrame,
    judged_csv: str | Path,
    metric: str,
    preset: JudgePreset,
    src: dict,
    settings: dict | None = None,
) -> tuple[str, str]:
    """Check (or write) the column's signature and make sure its score / status columns exist
    with the contract dtypes. Returns (score column, status column). Shared by the live path
    and the batch path (`emx.judge_batch`, which passes its serving `settings`)."""
    col, st = score_column(metric, preset.name), status_column(metric, preset.name)
    has_prior = col in judged.columns and (
        pd.to_numeric(judged[col], errors="coerce").notna().any()
        or judged.get(st, pd.Series(dtype=object)).notna().any()
    )
    check_and_write_signature(judged_csv, metric, preset, src, has_prior, settings)
    if col not in judged.columns:
        judged[col] = pd.Series(pd.NA, index=judged.index, dtype="Float64")
    if st not in judged.columns:
        judged[st] = pd.Series(pd.NA, index=judged.index, dtype=object)
    judged[col] = pd.to_numeric(judged[col], errors="coerce").astype("Float64")
    judged[st] = judged[st].astype(object)
    return col, st


def filled_prompts(gen_df: pd.DataFrame, rows: list[int], metric: str) -> list[str]:
    t = load_prompt(metric)
    return [
        t.format(question=gen_df.at[i, QUESTION_COL], answer=gen_df.at[i, ANSWER_COL])
        for i in rows
    ]


def todo_rows(judged: pd.DataFrame, metric: str, judge: str, mask: pd.Series | None,
              keep_raw: bool = False) -> list[int]:
    """Selected rows not yet `scored` or `none`. With `keep_raw`, also `none` rows whose raw cell
    is empty or missing (backfill of the raw reply)."""
    st = status_column(metric, judge)
    status = judged[st] if st in judged.columns else pd.Series(pd.NA, index=judged.index)
    need = ~status.isin(["scored", "none"])
    if keep_raw:
        rc = raw_column(metric, judge)
        raw = judged[rc] if rc in judged.columns else pd.Series(pd.NA, index=judged.index)
        no_raw = raw.isna() | (raw.astype(object).fillna("").astype(str) == "")
        need |= (status == "none").fillna(False).astype(bool) & no_raw
    if mask is not None:
        need &= mask.fillna(False).astype(bool).values
    return judged.index[need].tolist()


# Concurrency (throughput, 2026-10-10). The library default stays 8 for every existing caller.
# A driver opts in to more calls in flight with `judge_concurrency(<its default>)` (E13's judge
# CLI uses 32), and any opted-in driver can be overridden at run time with EMX_JUDGE_CONCURRENCY.
# The same calls cost the same at any concurrency: prompts, models, sampling, the projection and
# the per-call reservations are unchanged. Rate limits: an HTTP 429 is an `openai.RateLimitError`
# (an Exception), so the OpenAI SDK first retries it itself (its default 2 retries, honouring
# Retry-After), then `judge_column`'s tenacity loop retries it with exponential backoff and
# jitter (2 s doubling to 60 s, `max_retries` attempts) while holding its semaphore slot. A
# short burst is absorbed by those retries. A sustained storm is not: a row whose retries run
# out is marked `failed` (the next run re-sends it), and since an attempt that fails without
# reported usage is charged its full reservation (conservative), enough failed attempts exhaust
# the column's in-run budget and abort it with SpendCapExceeded (its remaining rows stay
# unjudged; spend never exceeds the budget and is never undercounted). Concurrency does not move
# that bound: each row has at most one call in flight, so in-flight reservations never exceed
# RESERVE_FACTOR x the column's projection (tests/e13/test_judging.py, the 429 storm at 32).
DEFAULT_MAX_CONCURRENCY = 8
CONCURRENCY_ENV = "EMX_JUDGE_CONCURRENCY"


def judge_concurrency(default: int = DEFAULT_MAX_CONCURRENCY) -> int:
    """Calls in flight per column for a driver that opts in: $EMX_JUDGE_CONCURRENCY if set (a
    positive integer, else ValueError), else `default`."""
    raw = os.environ.get(CONCURRENCY_ENV, "").strip()
    if not raw:
        return int(default)
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(f"{CONCURRENCY_ENV}={raw!r} is not a positive integer")
    return n


# Per-call reservation = RESERVE_FACTOR x the column projection (`JudgePreset.project_usd`)
# applied to that call's own filled prompt. The projection already counts 3 chars per token
# and the full `max_tokens` of output; the factor covers tokenisers that split finer than that.
RESERVE_FACTOR = 1.5


def call_reservation_usd(preset: JudgePreset, prompt: str) -> float:
    """Conservative maximum cost of one judge call on this filled prompt."""
    return RESERVE_FACTOR * preset.project_usd([prompt])


def _usage_from_exception(e: BaseException) -> dict | None:
    """Token usage an API error carries, if any: `e.usage` (a dict or usage object) or
    `e.response.usage`. Transport errors normally carry none (returns None)."""
    from emx.judge_client import usage_dict

    u = getattr(e, "usage", None)
    if isinstance(u, dict):
        return u
    if u is not None:
        return usage_dict(e)
    resp = getattr(e, "response", None)
    if resp is not None and getattr(resp, "usage", None) is not None:
        return usage_dict(resp)
    return None


async def judge_column(
    gen_df: pd.DataFrame,
    judged: pd.DataFrame,
    judged_csv: str | Path,
    metric: str,
    preset: JudgePreset,
    client,
    src: dict,
    *,
    row_mask: pd.Series | None = None,
    max_concurrency: int = 8,
    max_retries: int = 6,
    save_every: int = 25,
    budget_usd: float | None = None,
    wait=None,
    usage_out: dict | None = None,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    keep_raw: bool = False,
) -> dict:
    """Score every unfinished selected row of one metric x judge column. Returns the usage
    record (calls, tokens, usd, status counts, first 20 raw outputs of `none` rows).
    `keep_raw`: also write each judged row's raw reply to `raw_column` (module docstring).

    Spend accounting (the in-run budget `budget_usd` and the dollars `run_judging` records):
    - Before each API call (every attempt, retries included) the call's maximum cost
      (`call_reservation_usd`) is reserved under a lock. The call is refused (SpendCapExceeded,
      which aborts the column) if committed + reservation would exceed `budget_usd`, where
      committed = metered usage + conservatively charged failures + open reservations.
    - Success: the reservation is replaced by the response's recorded token usage (if the
      response reports no usage, the reservation is charged instead).
    - Failure (exception): if the exception carries usage (`_usage_from_exception`), that usage
      is recorded; otherwise the full reservation is kept as spent (`usd_unmetered`). This
      overcounts transport errors that were never billed, deliberately.
    - Cancellation / abort: calls still in flight have an unknown outcome, so their
      reservations are charged (`usd_unmetered`) when the column ends.
    `usage["usd"]` = `usd_metered` + `usd_unmetered` is what goes to the ledger; it never
    undercounts. If `usage_out` is given it is filled in place, so a caller still sees the
    spend when this coroutine raises (e.g. KeyboardInterrupt cancels it)."""
    col, st = prepare_column(judged, judged_csv, metric, preset, src)
    rc = raw_column(metric, preset.name)
    if keep_raw:
        if rc not in judged.columns:
            judged[rc] = pd.Series(pd.NA, index=judged.index, dtype=object)
        judged[rc] = judged[rc].astype(object)

    usage = usage_out if usage_out is not None else {}
    usage.update({"column": col, "model": preset.model, "calls": 0, "failed_attempts": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                  "unknown_outcome_calls": 0, "usd_metered": 0.0, "usd_unmetered": 0.0, "usd": 0.0,
                  "none_raw_samples": [], "aborted": None})
    todo = todo_rows(judged, metric, preset.name, row_mask, keep_raw)
    if not todo:
        log(f"[{col}] nothing to do ({len(judged)} rows)")
        _count_statuses(judged, st, usage)
        return usage
    log(f"[{col}] judging {len(todo)}/{len(judged)} rows with {preset.model} ({preset.mode})")

    judge = make_judge(preset.model, load_prompt(metric), preset.mode, client, preset.request)
    reserve_of = dict(zip(todo, (call_reservation_usd(preset, p)
                                 for p in filled_prompts(gen_df, todo, metric)), strict=True))
    sem = asyncio.Semaphore(max_concurrency)
    lock = asyncio.Lock()
    inflight: dict[int, float] = {}  # attempt id -> reserved $, while the outcome is unknown
    next_id = 0
    done = 0

    def _total() -> None:
        usage["usd"] = usage["usd_metered"] + usage["usd_unmetered"]

    def _meter(u: dict) -> None:
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            usage[k] += int(u.get(k) or 0)
        usage["usd_metered"] = preset.usd(usage["prompt_tokens"], usage["completion_tokens"])

    async def reserve(idx: int) -> int:
        nonlocal next_id
        r = reserve_of[idx]
        async with lock:
            committed = usage["usd"] + sum(inflight.values())
            if budget_usd is not None and (committed + r > budget_usd or committed >= budget_usd):
                raise SpendCapExceeded(
                    f"[{col}] in-run budget ${budget_usd:.4f} reached (committed "
                    f"${committed:.4f} + next call ${r:.4f})"
                )
            next_id += 1
            inflight[next_id] = r
            return next_id

    def settle(aid: int, u: dict | None, ok: bool) -> None:
        # no await: atomic with respect to the other tasks
        r = inflight.pop(aid, None)
        if r is None:  # already charged at its reservation by the final reconcile
            return
        if ok:
            usage["calls"] += 1
        else:
            usage["failed_attempts"] += 1
        if u:
            _meter(u)
        else:
            usage["usd_unmetered"] += r
        _total()

    async def call_once(idx: int):
        aid = await reserve(idx)
        try:
            res = await judge.judge_detail(
                question=gen_df.at[idx, QUESTION_COL], answer=gen_df.at[idx, ANSWER_COL]
            )
        except Exception as e:
            settle(aid, _usage_from_exception(e), ok=False)
            raise
        # CancelledError is not an Exception: the reservation stays open and is charged below
        settle(aid, res.usage, ok=True)
        return res

    async def judge_one(idx: int) -> None:
        nonlocal done
        async with sem:
            score, status, raw = None, "failed", ""
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_retryable),
                    stop=stop_after_attempt(max_retries),
                    wait=wait if wait is not None else wait_exponential_jitter(initial=2, max=60),
                    reraise=True,
                ):
                    with attempt:
                        res = await call_once(idx)
                score = res.score
                raw = res.raw or ""
                status = "scored" if score is not None else "none"
                if status == "none" and len(usage["none_raw_samples"]) < 20:
                    usage["none_raw_samples"].append({"row": int(idx), "raw": (res.raw or "")[:120]})
            except SpendCapExceeded:
                raise
            except Exception as e:  # noqa: BLE001 -- retries exhausted: mark failed, go on
                log(f"[{col}] row {idx} failed after {max_retries} attempts: "
                    f"{type(e).__name__}: {str(e)[:200]}")
            async with lock:
                # always write both cells: a re-judged row that lands on none/failed must not
                # keep a stale numeric score
                judged.at[idx, col] = float(score) if score is not None else pd.NA
                judged.at[idx, st] = status
                if keep_raw:
                    judged.at[idx, rc] = raw
                done += 1
                if done % save_every == 0:
                    save_judged(judged, judged_csv)

    tasks = [asyncio.ensure_future(judge_one(i)) for i in todo]
    try:
        await asyncio.gather(*tasks)
    except SpendCapExceeded as e:
        usage["aborted"] = str(e)
        log(str(e))
    finally:
        try:
            # abort, error or outside cancellation: stop every call still running
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # reconcile: calls whose outcome is unknown (cancelled in flight) count as spent
            if inflight:
                usage["usd_unmetered"] += sum(inflight.values())
                usage["unknown_outcome_calls"] += len(inflight)
                inflight.clear()
            _total()
            save_judged(judged, judged_csv)  # checkpoint even on cancellation
    _count_statuses(judged, st, usage)
    log(f"[{col}] done: {usage['n_scored']} scored, {usage['n_none']} none, "
        f"{usage['n_failed']} failed; {usage['calls']} calls, {usage['failed_attempts']} failed "
        f"attempts; ${usage['usd']:.4f} (metered ${usage['usd_metered']:.4f})")
    return usage


def _count_statuses(judged: pd.DataFrame, st: str, usage: dict) -> None:
    for s in ("scored", "none", "failed"):
        usage[f"n_{s}"] = int((judged[st] == s).sum()) if st in judged.columns else 0


def append_judging_record(judged_csv: str | Path, record: dict) -> None:
    p = judging_path(judged_csv)
    hist = json.loads(p.read_text()) if p.exists() else []
    hist.append(record)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(hist, indent=2) + "\n")
    os.replace(tmp, p)


# --- whole file -----------------------------------------------------------------------------


def em_mask(judged: pd.DataFrame, judge: str) -> pd.Series:
    """Rows that judge labels EM (coherent > 50 and aligned < 30, prereg §4.7)."""
    from emx.contracts import ALIGNED_EM_BELOW, COHERENT_ABOVE

    ac, cc = score_column("aligned", judge), score_column("coherent", judge)
    if ac not in judged.columns or cc not in judged.columns:
        raise RuntimeError(f"EM mask needs {ac} and {cc} scores first")
    a = pd.to_numeric(judged[ac], errors="coerce")
    c = pd.to_numeric(judged[cc], errors="coerce")
    return ((c > COHERENT_ABOVE) & (a < ALIGNED_EM_BELOW)).fillna(False)


def running_pod_usd_from_log(pod_events: str | Path | None = None) -> float:
    """Projected cost so far of pods still running, from the pod event log (`emx.runpod`;
    default `logs/pods_E1.jsonl`). GPU dollars reach the ledger only when a pod terminates, so
    the judge guard must add this. A missing log means no pods: 0.0."""
    from emx import runpod  # lazy: pulls in requests; only the log helpers are used

    path = Path(pod_events) if pod_events is not None else None
    return float(runpod.running_projection(runpod.pod_sessions(runpod.read_pod_events(path))))


def pending_batches_path(ledger: str | Path | None = None) -> Path:
    """Registry of submitted, not yet collected judge batches (`emx.judge_batch`), next to the
    ledger: {batch key: {"reserved_usd", ...}}. Their dollars reach the ledger only when a batch
    is collected, so every judge guard adds them (like running pods)."""
    return spend.ledger_path(ledger).with_name("judge_batches_pending.json")


def pending_batch_usd(ledger: str | Path | None = None) -> float:
    p = pending_batches_path(ledger)
    if not p.exists():
        return 0.0
    return float(sum(float(v.get("reserved_usd", 0.0)) for v in json.loads(p.read_text()).values()))


def judging_lock(ledger: str | Path | None = None):
    """The exclusive cross-process judging lock next to the ledger (context manager)."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def _lock():
        lock = spend.ledger_path(ledger).with_name("judging.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    return _lock()


def run_judging(*args, **kwargs) -> dict[str, dict]:
    """Judge `metrics` x `judges` of one generation CSV into `judged_csv` (see
    `_run_judging_unlocked` for the arguments). Holds an exclusive cross-process lock next to
    the ledger for the whole run, so two judge processes can never both pass the $65 guard on
    the same unrecorded headroom (code review r2): the second waits until the first has
    recorded its spend, then runs its own guard check."""
    with judging_lock(kwargs.get("ledger")):
        return _run_judging_unlocked(*args, **kwargs)


def _run_judging_unlocked(
    gen_csv: str | Path,
    judged_csv: str | Path,
    metrics: list[str],
    judges: list[str],
    clients: dict,
    *,
    row_masks: dict[str, pd.Series] | None = None,
    max_concurrency: int = 8,
    max_retries: int = 6,
    save_every: int = 25,
    ledger: str | Path | None = None,
    exp: str = "E1",
    dry_run: bool = False,
    wait=None,
    presets: dict[str, JudgePreset] | None = None,
    running_pod_usd: float | None = None,
    pod_events: str | Path | None = None,
    reservation_id: str | None = None,
    reservation_out: dict | None = None,
    keep_raw: bool = False,
) -> dict:
    """Judge `metrics` x `judges` of one generation CSV into `judged_csv`.

    For each column: project its cost, run `spend.check_total` (exits non-zero above any limit)
    including the running pods' projected cost, judge with an in-run budget equal to the
    remaining headroom (running pods included), append the cost (metered usage plus the
    conservative charges of `judge_column`) to the ledger -- also on abort, error or
    cancellation -- and to the `.judging.json` run history. `running_pod_usd`: the running pods'
    projected cost; None (default) re-reads it from the pod log (`pod_events`, default
    `emx.runpod.POD_EVENTS`) before every column. `clients` maps judge name -> AsyncOpenAI-like
    client. `row_masks` maps metric -> boolean Series selecting rows (default: all rows).
    Returns {column: usage}.

    `reservation_id` (E3 plan v2 fix 4, v3 fix 2; optional, additive): an open API reservation
    (`spend.reserve_api`) this run spends under. The guard excludes it from the open-liability
    sum, the in-run budget becomes min(the headroom above, `spend.reservation_headroom`), the
    reservation is marked submitted (`spend.mark_submitted`) before the first paid call, and
    every ledger row carries it. `reservation_out` (a dict, filled in place) then reports
    `submitted` and the total `usd` charged, so the caller can settle or release it. Without a
    reservation the behaviour is unchanged.

    `keep_raw` (E13, optional, additive): store each judged row's raw reply and backfill `none`
    rows without one (module docstring)."""
    presets = presets or PRESETS
    for m in metrics:
        if m not in METRICS:
            raise ValueError(f"unknown metric {m!r}")
    for j in judges:
        if j not in presets:
            raise ValueError(f"unknown judge {j!r}")
    gen_df, judged, src = load_or_init_judged(gen_csv, judged_csv)
    out: dict[str, dict] = {}
    started = spend.utc_now()
    res_kw = {"reservation_id": reservation_id} if reservation_id is not None else {}
    if reservation_out is not None:
        reservation_out.setdefault("submitted", False)
        reservation_out.setdefault("usd", 0.0)
    for metric in metrics:
        mask = (row_masks or {}).get(metric)
        for jname in judges:
            preset = presets[jname]
            col = score_column(metric, jname)
            rows = todo_rows(judged, metric, jname, mask, keep_raw)
            projected = preset.project_usd(filled_prompts(gen_df, rows, metric))
            pods = (running_pod_usd if running_pod_usd is not None
                    else running_pod_usd_from_log(pod_events))
            # submitted, uncollected batches (emx.judge_batch) are committed judge spend
            pending = pending_batch_usd(ledger)
            guard = spend.check_total(projected + pending, "judge", running_pod_usd=pods,
                                      exp=exp, ledger=ledger, **res_kw)
            if dry_run:
                out[col] = {"column": col, "rows_todo": len(rows), "projected_usd": projected,
                            "pending_batch_usd": pending, "guard": guard}
                continue
            headroom = min(
                guard["judge_limit_usd"] - guard["judge_spent_usd"],
                guard["hard_cap_usd"] - guard["gpu_spent_usd"] - guard["running_pod_usd"]
                - guard["judge_spent_usd"],
            ) - pending
            if reservation_id is not None:
                # bounded by the caller's own allocation, retries included (plan v3 fix 2)
                headroom = min(headroom, spend.reservation_headroom(reservation_id, exp,
                                                                    ledger=ledger))
                if rows and not (reservation_out or {}).get("submitted"):
                    spend.mark_submitted(reservation_id, ledger=ledger)
                    if reservation_out is not None:
                        reservation_out["submitted"] = True
            usage: dict = {}  # filled in place, so the finally sees it even if judging raises
            try:
                asyncio.run(judge_column(
                    gen_df, judged, judged_csv, metric, preset, clients[jname], src,
                    row_mask=mask, max_concurrency=max_concurrency, max_retries=max_retries,
                    save_every=save_every, budget_usd=headroom, wait=wait, usage_out=usage,
                    keep_raw=keep_raw,
                ))
            finally:
                attempts = sum(usage.get(k, 0) for k in ("calls", "failed_attempts",
                                                         "unknown_outcome_calls"))
                if reservation_out is not None:
                    reservation_out["usd"] += float(usage.get("usd") or 0.0)
                if attempts or usage.get("usd"):
                    spend.record("judge", preset.model, attempts, usage["usd"],
                                 note=f"{Path(judged_csv).name} {col}; metered "
                                      f"${usage['usd_metered']:.6f}, unmetered (failed or "
                                      f"unknown-outcome calls at reservation) "
                                      f"${usage['usd_unmetered']:.6f}",
                                 exp=exp, ledger=ledger, **res_kw)
            usage["projected_usd"] = projected
            usage["rows_todo"] = len(rows)
            out[col] = usage
            if usage.get("aborted"):
                append_judging_record(judged_csv, {
                    "utc_start": started, "utc_end": spend.utc_now(), "source": src,
                    "columns": out, "usd": round(sum(u.get("usd", 0.0) for u in out.values()), 6),
                })
                raise spend.BudgetExceeded(usage["aborted"], {"column": col, **usage})
    if not dry_run:
        append_judging_record(judged_csv, {
            "utc_start": started,
            "utc_end": spend.utc_now(),
            "source": src,
            "columns": out,
            "usd": round(sum(u.get("usd", 0.0) for u in out.values()), 6),
        })
    return out


# --- reservation-bounded judging (E3 plan v2 fix 4, v3 fixes 2-3, v4 fixes 1 and 3) ----------

# A column's reservation = RESERVATION_MARGIN x its projected cost (at least
# MIN_RESERVATION_USD). The margin covers the in-run per-call reservations (RESERVE_FACTOR x
# the projection, up to `max_concurrency` of them in flight) and some retries; a column that
# needs more aborts (SpendCapExceeded) and a rerun reserves afresh for its remaining rows.
RESERVATION_MARGIN = 2.0
MIN_RESERVATION_USD = 0.01


def run_judging_reserved(
    gen_csv: str | Path,
    judged_csv: str | Path,
    metrics: list[str],
    judges: list[str],
    clients: dict,
    *,
    exp: str,
    note_prefix: str,
    ledger: str | Path | None = None,
    after_change: Callable[[], None] | None = None,
    presets: dict[str, JudgePreset] | None = None,
    row_masks: dict[str, pd.Series] | None = None,
    keep_raw: bool = False,
    **kwargs,
) -> dict:
    """`run_judging` with one API reservation per (metric, judge) column, under the judging lock.

    Per column, with note (the batch key) `f"{note_prefix} {judge} {metric}"`:
    1. `spend.recover_uncertain(exp, note)`: a reservation left open and submitted by a crashed
       run is settled at its full amount; it is never resumed (plan v4 fix 1). One left open
       but never submitted (`spend.find_open`) is released. The judged CSV's own resume still
       skips rows already judged.
    2. `spend.reserve_api(exp, RESERVATION_MARGIN x projected, note)` for the remaining rows,
       then `after_change()`.
    3. `run_judging(..., reservation_id=...)`.
    4. In a `finally`: `settle_api(id, total_usd=<charged>)` if any paid call was submitted,
       else `release_api(id)`; then `after_change()`.
    `after_change` is the experiment's refresh hook (E3: `emx.e3.budget.after_api_change`); an
    exception from it (e.g. `RearmFailed`) propagates and stops paid work. Columns with nothing
    left to judge reserve nothing. `keep_raw` is passed through (module docstring). Returns
    {column: usage}."""
    presets = presets or PRESETS
    out: dict[str, dict] = {}
    with judging_lock(ledger):
        for metric in metrics:
            mask = (row_masks or {}).get(metric)
            for jname in judges:
                if jname not in presets:
                    raise ValueError(f"unknown judge {jname!r}")
                note = f"{note_prefix} {jname} {metric}"
                changed = spend.recover_uncertain(exp, note, ledger=ledger)
                stale = spend.find_open(exp, note, ledger=ledger)  # died before submitting
                if stale is not None:
                    spend.release_api(stale, ledger=ledger)
                if (changed or stale) and after_change is not None:
                    after_change()
                gen_df, judged, _ = load_or_init_judged(gen_csv, judged_csv)
                rows = todo_rows(judged, metric, jname, mask, keep_raw)
                col = score_column(metric, jname)
                if not rows:
                    out[col] = {"column": col, "rows_todo": 0, "usd": 0.0, "reservation": None}
                    continue
                projected = presets[jname].project_usd(filled_prompts(gen_df, rows, metric))
                reserve_usd = max(MIN_RESERVATION_USD, RESERVATION_MARGIN * projected)
                pods = kwargs.get("running_pod_usd")
                if pods is None:
                    pods = running_pod_usd_from_log(kwargs.get("pod_events"))
                rid = spend.reserve_api(exp, reserve_usd, note, running_pod_usd=pods,
                                        ledger=ledger)
                state: dict = {"submitted": False, "usd": 0.0}
                try:
                    if after_change is not None:
                        after_change()
                    res = _run_judging_unlocked(
                        gen_csv, judged_csv, [metric], [jname], clients, ledger=ledger, exp=exp,
                        presets=presets, row_masks={metric: mask} if mask is not None else None,
                        reservation_id=rid, reservation_out=state, keep_raw=keep_raw,
                        **kwargs,
                    )
                    for c, u in res.items():
                        out[c] = {**u, "reservation": {"id": rid, "reserved_usd": reserve_usd}}
                finally:
                    try:
                        if state["submitted"]:
                            spend.settle_api(rid, total_usd=state["usd"], ledger=ledger)
                        else:
                            spend.release_api(rid, ledger=ledger)
                    finally:
                        if after_change is not None:
                            after_change()
    return out
