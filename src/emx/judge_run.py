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
  `emx.spend.check_total` before it starts, an in-run budget aborts before the call that would
  cross it, and the actual cost is appended to the ledger (also on abort or cancellation).

Status semantics (resume):
  scored -- numeric score present; never re-judged
  none   -- the judge answered but gave no usable score (refusal / CODE / < 0.25 numeric weight);
            a real outcome, never re-judged, never converted to a score
  failed -- transport failure after all retries; re-judged on the next run
  (empty) -- row not selected for this metric (e.g. medtopic is scored on EM rows only)
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
    retry_if_not_exception_type,
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


def expected_signature(metric: str, preset: JudgePreset, src: dict) -> dict:
    prompt = load_prompt(metric)
    return {
        "column": score_column(metric, preset.name),
        "metric": metric,
        "judge": preset.name,
        "prompt_file": str(prompt_path(metric).relative_to(prompt_path(metric).parents[2])),
        "prompt_sha256": prompt_sha256(prompt),
        "judge_model": preset.model,
        "scoring_mode": preset.mode,
        "settings": preset.settings(),
        "source": src,
    }


def check_and_write_signature(
    judged_csv: str | Path, metric: str, preset: JudgePreset, src: dict, has_prior: bool
) -> dict:
    """Refuse to resume a column with a different prompt / judge / settings / source than started
    it, and refuse to adopt prior scores without a signature. Writes the signature if new."""
    sp = sig_path(judged_csv, metric, preset.name)
    expected = expected_signature(metric, preset, src)
    if sp.exists():
        existing = json.loads(sp.read_text())
        existing.pop("created_utc", None)
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


def filled_prompts(gen_df: pd.DataFrame, rows: list[int], metric: str) -> list[str]:
    t = load_prompt(metric)
    return [
        t.format(question=gen_df.at[i, QUESTION_COL], answer=gen_df.at[i, ANSWER_COL])
        for i in rows
    ]


def todo_rows(judged: pd.DataFrame, metric: str, judge: str, mask: pd.Series | None) -> list[int]:
    st = status_column(metric, judge)
    status = judged[st] if st in judged.columns else pd.Series(pd.NA, index=judged.index)
    need = ~status.isin(["scored", "none"])
    if mask is not None:
        need &= mask.fillna(False).astype(bool).values
    return judged.index[need].tolist()


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
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> dict:
    """Score every unfinished selected row of one metric x judge column. Returns the usage
    record (calls, tokens, usd, status counts, first 20 raw outputs of `none` rows)."""
    col, st = score_column(metric, preset.name), status_column(metric, preset.name)
    has_prior = col in judged.columns and (
        pd.to_numeric(judged[col], errors="coerce").notna().any()
        or judged.get(st, pd.Series(dtype=object)).notna().any()
    )
    check_and_write_signature(judged_csv, metric, preset, src, has_prior)
    if col not in judged.columns:
        judged[col] = pd.Series(pd.NA, index=judged.index, dtype="Float64")
    if st not in judged.columns:
        judged[st] = pd.Series(pd.NA, index=judged.index, dtype=object)
    judged[col] = pd.to_numeric(judged[col], errors="coerce").astype("Float64")
    judged[st] = judged[st].astype(object)

    usage = {"column": col, "model": preset.model, "calls": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "reasoning_tokens": 0, "usd": 0.0,
             "none_raw_samples": [], "aborted": None}
    todo = todo_rows(judged, metric, preset.name, row_mask)
    if not todo:
        log(f"[{col}] nothing to do ({len(judged)} rows)")
        _count_statuses(judged, st, usage)
        return usage
    log(f"[{col}] judging {len(todo)}/{len(judged)} rows with {preset.model} ({preset.mode})")

    judge = make_judge(preset.model, load_prompt(metric), preset.mode, client, preset.request)
    sem = asyncio.Semaphore(max_concurrency)
    lock = asyncio.Lock()
    done = 0

    async def on_attempt() -> None:
        if budget_usd is not None and usage["usd"] >= budget_usd:
            raise SpendCapExceeded(
                f"[{col}] in-run budget ${budget_usd:.4f} reached (${usage['usd']:.4f})"
            )

    async def judge_one(idx: int) -> None:
        nonlocal done
        async with sem:
            score, status = None, "failed"
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_not_exception_type(SpendCapExceeded),
                    stop=stop_after_attempt(max_retries),
                    wait=wait if wait is not None else wait_exponential_jitter(initial=2, max=60),
                    reraise=True,
                ):
                    with attempt:
                        await on_attempt()
                        res = await judge.judge_detail(
                            question=gen_df.at[idx, QUESTION_COL],
                            answer=gen_df.at[idx, ANSWER_COL],
                        )
                score = res.score
                status = "scored" if score is not None else "none"
                async with lock:
                    u = res.usage or {}
                    usage["calls"] += 1
                    for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                        usage[k] += int(u.get(k) or 0)
                    usage["usd"] = preset.usd(usage["prompt_tokens"], usage["completion_tokens"])
                    if status == "none" and len(usage["none_raw_samples"]) < 20:
                        usage["none_raw_samples"].append(
                            {"row": int(idx), "raw": (res.raw or "")[:120]}
                        )
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
                done += 1
                if done % save_every == 0:
                    save_judged(judged, judged_csv)

    tasks = [asyncio.ensure_future(judge_one(i)) for i in todo]
    try:
        await asyncio.gather(*tasks)
    except SpendCapExceeded as e:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        usage["aborted"] = str(e)
        log(str(e))
    finally:
        save_judged(judged, judged_csv)  # checkpoint even on cancellation
    _count_statuses(judged, st, usage)
    log(f"[{col}] done: {usage['n_scored']} scored, {usage['n_none']} none, "
        f"{usage['n_failed']} failed; {usage['calls']} calls ${usage['usd']:.4f}")
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


def run_judging(
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
) -> dict:
    """Judge `metrics` x `judges` of one generation CSV into `judged_csv`.

    For each column: project its cost, run `spend.check_total` (exits non-zero above any limit),
    judge with an in-run budget equal to the remaining headroom, append the actual cost to the
    ledger and the `.judging.json` run history. `clients` maps judge name -> AsyncOpenAI-like
    client. `row_masks` maps metric -> boolean Series selecting rows (default: all rows).
    Returns {column: usage}."""
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
    for metric in metrics:
        mask = (row_masks or {}).get(metric)
        for jname in judges:
            preset = presets[jname]
            col = score_column(metric, jname)
            rows = todo_rows(judged, metric, jname, mask)
            projected = preset.project_usd(filled_prompts(gen_df, rows, metric))
            guard = spend.check_total(projected, "judge", exp=exp, ledger=ledger)
            if dry_run:
                out[col] = {"column": col, "rows_todo": len(rows), "projected_usd": projected,
                            "guard": guard}
                continue
            headroom = min(
                guard["judge_limit_usd"] - guard["judge_spent_usd"],
                guard["hard_cap_usd"] - guard["gpu_spent_usd"] - guard["judge_spent_usd"],
            )
            usage: dict = {}
            try:
                usage = asyncio.run(judge_column(
                    gen_df, judged, judged_csv, metric, preset, clients[jname], src,
                    row_mask=mask, max_concurrency=max_concurrency, max_retries=max_retries,
                    save_every=save_every, budget_usd=headroom, wait=wait,
                ))
            finally:
                if usage.get("calls"):
                    spend.record("judge", preset.model, usage["calls"], usage["usd"],
                                 note=f"{Path(judged_csv).name} {col}", exp=exp, ledger=ledger)
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
