"""Judge E11's generation CSVs (luna and gfl score aligned and coherent), then the rejudge pass.

Port of scripts/eq0_judge.py (EQ0, 2026-10-08) to E11's conditions (src/emx/e11/contracts.py;
implementation plan U7; prereg §4.4-4.5). Each selected condition's generation CSV
(`C.gen_csv(cond, pilot)`) is judged into `C.judged_csv(cond, pilot)` through
`emx.judge_run.run_judging_reserved` (exp "E11", note prefix "E11 judge <condition>"): one API
reservation per (metric, judge) column, resumable (rows already `scored` or `none` are never
re-sent by the first pass), each column signed with its prompt, judge settings and source CSV.
Part 2's CSVs (results/E11/part2/gen/) are never judged.

Selection, `--conditions` (one or more):
  all          every main, battery and elicitation condition whose CSV exists (not the pilots)
  pilot-r<k>   the rung-k pilots (`C.pilot_key(ds, k)`) whose CSV exists under gen/pilot/
  <glob>       fnmatch over every known condition name (main, battery, elicitation, pilot)
Expected rows per condition (a CSV with a different count is refused): main 1,360; pilot 400;
battery 20 x `battery_samples` from results/E11/plan.json (refused without it); elicitation 160.
Judges: `--judges` for every condition; by default `C.PILOT_JUDGES` (luna) for pilots and
`C.JUDGES` (luna, gfl) for the rest. Prompts: the E1 aligned and coherent rubrics
(`emx.prompts.load_prompt`), as eq0_judge.py.

Rejudge pass (prereg §4.4 "Missing scores"; plan review r1, finding 4). After a condition's first
pass, every cell (row, metric) whose DECIDING judge (`C.DECIDING_JUDGE[qset]`) has a status other
than `scored` (`none` = refusal or no usable score, `failed` = transport error) is reset and
judged once more, for that judge and metric only. judge_run treats `none` as final, so the reset
writes status `failed` with an empty score (judge_run's own "re-judge on the next run" state;
`todo_rows` then selects it) and the pass runs with a row mask of exactly the reset cells. It is
persisted in `<judged stem>.rejudge.json` and crash-safe by phase:
  planned  cells and statuses before are recorded, no reset saved yet -> a rerun re-applies the
           reset (no judging has happened, so the reset is idempotent);
  reset    the reset is saved -> a rerun only resumes judging (cells already re-judged keep
           their new status: never reset twice);
  done     statuses after and the unscored shares are recorded -> a rerun never rejudges again.
Recorded cells (any phase) are excluded from the first pass's work (`first_pass_masks`), so a
cell still `failed` after a `done` pass is terminal and an unfinished pass resumes only through
this state machine (review r1b, finding 1).
The pass runs only when every deciding judge of the CSV's question sets is among `--judges` and
both metrics are judged; otherwise it is deferred (printed) to a later run. Then each
condition's unscored share per question set (deciding judge, aligned or coherent not `scored`)
is reported, and a share > `C.MISSING_SCORE_MAX` (2%) is flagged INCOMPLETE.

Before any call: the projection for all remaining first-pass work plus the currently known
rejudge cells (`none` statuses of deciding judges), per call as `JudgePreset.project_usd`, is
printed and checked with `emx.spend.check_total(projected, "judge", running pods, exp="E11")`
(and `--max-usd`); above a limit it exits 2 and sends nothing. `emx.judge_panel.check_probe`
confirms each judge's mode against results/E1/checks/judge_probe.json, as eq0_judge.py.
`--dry-run` stops after the projection: no keys read, no clients created, nothing written.

Usage:
  uv run python scripts/e11_judge.py --conditions all [--judges luna gfl] [--dry-run]
      [--max-usd X] [--concurrency 8] [--max-retries 6] [--running-pod-usd X]
  uv run python scripts/e11_judge.py --conditions pilot-r1 --judges luna
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

from emx import judge_panel as JP
from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS, status_column
from emx.e11 import contracts as C

TAG = "E11 JUDGE"
DECIDING = set(C.DECIDING_JUDGE.values())


# --- conditions -----------------------------------------------------------------------------


def known_conditions() -> dict[str, dict]:
    """{condition: {"kind", "pilot", "qsets"}} for every judged E11 condition."""
    out: dict[str, dict] = {}
    for k in C.EVAL_KEYS:
        out[k] = {"kind": "main", "pilot": False, "qsets": set(C.QSETS)}
    for m in (C.BASE_KEY, *C.EVAL_KEYS):
        for ctx in C.BATTERY_GENERATED:
            out[C.battery_condition(m, ctx)] = {"kind": "battery", "pilot": False,
                                                "qsets": {C.TEST_QSET}}
    for k in C.ELICIT_CONDITIONS:
        out[k] = {"kind": "elicit", "pilot": False, "qsets": {C.DEV_QSET}}
    for ds in C.DS_KEYS:
        for r in C.RUNGS:
            out[C.pilot_key(ds, r)] = {"kind": "pilot", "pilot": True, "qsets": {C.DEV_QSET},
                                       "rung": r}
    return out


def battery_samples(root: Path) -> int:
    p = root / C.PLAN_JSON
    if not p.exists():
        raise ValueError(f"{C.PLAN_JSON} is missing: the battery's sample count is unknown")
    n = json.loads(p.read_text()).get("battery_samples")
    if n not in (C.BATTERY_SAMPLES, C.BATTERY_SAMPLES_CUT):
        raise ValueError(f"{C.PLAN_JSON}: battery_samples {n!r} is not "
                         f"{C.BATTERY_SAMPLES} or {C.BATTERY_SAMPLES_CUT}")
    return int(n)


def expected_rows(kind: str, root: Path) -> int:
    if kind == "main":
        return C.ROWS_PER_MODEL
    if kind == "pilot":
        return C.N_QUESTIONS[C.DEV_QSET] * C.PILOT_SAMPLES
    if kind == "elicit":
        return C.N_QUESTIONS[C.DEV_QSET] * C.ELICIT_SAMPLES
    if kind == "battery":
        return len(C.BATTERY_QIDS) * battery_samples(root)
    raise ValueError(f"unknown kind {kind!r}")


def select(root: Path, selectors: list[str]) -> list[str]:
    """Known conditions with an existing generation CSV, in contracts order."""
    known = known_conditions()
    exists = {k: (root / C.gen_csv(k, v["pilot"])).exists() for k, v in known.items()}
    chosen: list[str] = []
    for sel in selectors:
        if sel.startswith(("vec:", "p2base:")) or "part2" in sel:
            raise ValueError(f"{sel!r}: part 2's generations are never judged")
        if sel == "all":
            hit = [k for k, v in known.items() if not v["pilot"] and exists[k]]
            if not hit:
                raise ValueError("--conditions all: no main, battery or elicitation CSV exists")
        elif m := re.fullmatch(r"pilot-r(\d+)", sel):
            r = int(m.group(1))
            if r not in C.RUNGS:
                raise ValueError(f"{sel!r}: no rung {r}")
            hit = [C.pilot_key(ds, r) for ds in C.DS_KEYS if exists[C.pilot_key(ds, r)]]
            if not hit:
                raise ValueError(f"{sel!r}: no rung-{r} pilot CSV exists")
        else:
            hit = [k for k in known if fnmatch.fnmatchcase(k, sel) and exists[k]]
            if not hit:
                raise ValueError(f"{sel!r} matches no known condition with a generation CSV")
        chosen += [k for k in hit if k not in chosen]
    return [k for k in known if k in chosen]


def check_gen(gen: Path, cond: str, info: dict, root: Path) -> pd.DataFrame:
    df = JR.read_gen(gen)
    if tuple(df.columns) != GEN_COLUMNS:
        raise ValueError(f"{gen}: columns {list(df.columns)} != GEN_COLUMNS")
    if set(df["condition"].astype(str)) != {cond}:
        raise ValueError(f"{gen}: condition is not {cond!r} on every row")
    n = expected_rows(info["kind"], root)
    if len(df) != n:
        raise ValueError(f"{gen}: {len(df)} rows, expected {n} for a {info['kind']} condition")
    qs = set(df["qset"].astype(str))
    if not qs <= info["qsets"]:
        raise ValueError(f"{gen}: question sets {sorted(qs)} not within {sorted(info['qsets'])}")
    return df


def paths(root: Path, cond: str, info: dict) -> tuple[Path, Path]:
    return root / C.gen_csv(cond, info["pilot"]), root / C.judged_csv(cond, info["pilot"])


# --- rejudge pass ---------------------------------------------------------------------------


def rejudge_path(judged: Path) -> Path:
    return Path(f"{JR._stem(judged)}.rejudge.json")


def _write_json(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    os.replace(tmp, p)


def read_rejudge(judged: Path) -> dict | None:
    p = rejudge_path(judged)
    return json.loads(p.read_text()) if p.exists() else None


def deciding_statuses(gen_df: pd.DataFrame, jdf: pd.DataFrame) -> dict[str, dict]:
    """{qset: {"judge", "rows", metric: Series of statuses (NA = unjudged)}}."""
    out = {}
    for q in sorted(set(gen_df["qset"].astype(str))):
        j = C.DECIDING_JUDGE[q]
        rows = gen_df.index[gen_df["qset"].astype(str) == q]
        rec = {"judge": j, "rows": rows}
        for m in C.METRICS:
            st = status_column(m, j)
            col = jdf[st] if st in jdf.columns else pd.Series(pd.NA, index=jdf.index)
            rec[m] = col.loc[rows]
        out[q] = rec
    return out


def rejudge_cells(gen_df: pd.DataFrame, jdf: pd.DataFrame) -> list[dict] | None:
    """The deciding-judge cells to reset, or None while the first pass is unfinished (a
    deciding cell still unjudged)."""
    cells = []
    for q, rec in deciding_statuses(gen_df, jdf).items():
        for m in C.METRICS:
            s = rec[m]
            if s.isna().any():
                return None
            for idx in s.index[(s != "scored").astype(bool)]:
                cells.append({"row": int(idx), "qid": str(gen_df.at[idx, "qid"]),
                              "sample_idx": int(gen_df.at[idx, "sample_idx"]), "qset": q,
                              "judge": rec["judge"], "metric": m, "before": str(s.at[idx])})
    return cells


def status_counts(jdf: pd.DataFrame, cells: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cells:
        st = jdf.at[c["row"], status_column(c["metric"], c["judge"])]
        k = "unjudged" if pd.isna(st) else str(st)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def reset_cells(gen: Path, judged: Path, cells: list[dict]) -> None:
    """Score empty and status `failed` on exactly these cells (judge_run re-judges `failed`)."""
    _, jdf, _ = JR.load_or_init_judged(gen, judged)
    for c in cells:
        sc, st = f"{c['metric']}__{c['judge']}", status_column(c["metric"], c["judge"])
        jdf.at[c["row"], sc] = pd.NA
        jdf.at[c["row"], st] = "failed"
    JR.save_judged(jdf, judged)


def unscored(gen_df: pd.DataFrame, jdf: pd.DataFrame) -> dict[str, dict]:
    """{qset: {judge, n, n_unscored, share, incomplete}}: deciding aligned or coherent not
    `scored` (prereg §4.4)."""
    out = {}
    for q, rec in deciding_statuses(gen_df, jdf).items():
        bad = pd.Series(False, index=rec["rows"])
        for m in C.METRICS:
            bad |= (rec[m] != "scored").fillna(True).astype(bool)
        n, k = len(rec["rows"]), int(bad.sum())
        out[q] = {"judge": rec["judge"], "n": n, "n_unscored": k,
                  "share": k / n if n else 0.0, "incomplete": n == 0 or k / n > C.MISSING_SCORE_MAX}
    return out


def git_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(C.REPO_ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def rejudge(cond: str, gen: Path, judged: Path, run_cells, log) -> dict | None:
    """The persisted, crash-safe rejudge pass of one condition. `run_cells(cells)` judges the
    reset cells (paid). Returns the record, or None if the first pass is unfinished."""
    rec = read_rejudge(judged)
    if rec is not None and rec.get("phase") == "done":
        return rec
    gen_df, jdf, src = JR.load_or_init_judged(gen, judged)
    if rec is None:
        cells = rejudge_cells(gen_df, jdf)
        if cells is None:
            log(f"{TAG} REJUDGE DEFERRED {cond}: first pass unfinished (deciding cells unjudged)")
            return None
        rec = {"condition": cond, "phase": "planned", "utc": spend.utc_now(),
               "source": src, "cells": cells,
               "rows": sorted({c["row"] for c in cells}),
               "statuses_before": status_counts(jdf, cells)}
        _write_json(rejudge_path(judged), rec)
    cells = rec["cells"]
    if rec["phase"] == "planned":
        # nothing has been re-judged yet in this phase, so (re)applying the reset is safe
        if cells:
            reset_cells(gen, judged, cells)
        rec["phase"], rec["utc_reset"] = "reset", spend.utc_now()
        _write_json(rejudge_path(judged), rec)
    if cells:
        log(f"{TAG} REJUDGE {cond}: {len(cells)} cells in {len(rec['rows'])} rows")
        run_cells(cells)
    gen_df, jdf, _ = JR.load_or_init_judged(gen, judged)
    still = [c for c in cells if pd.isna(jdf.at[c["row"], status_column(c["metric"], c["judge"])])
             or jdf.at[c["row"], status_column(c["metric"], c["judge"])] == "failed"]
    rec["statuses_after"] = status_counts(jdf, cells)
    rec["unscored"] = unscored(gen_df, jdf)
    rec["phase"], rec["utc_done"] = "done", spend.utc_now()
    rec["provenance"] = C.provenance({}, {str(gen): src["sha256"]}, git_commit(), rec["utc_done"])
    rec["n_still_failed"] = len(still)
    _write_json(rejudge_path(judged), rec)
    return rec


def cell_masks(gen_df: pd.DataFrame, cells: list[dict], judge: str) -> dict[str, pd.Series]:
    masks = {}
    for m in C.METRICS:
        rows = {c["row"] for c in cells if c["judge"] == judge and c["metric"] == m}
        masks[m] = pd.Series(gen_df.index.isin(sorted(rows)), index=gen_df.index)
    return masks


def first_pass_masks(gen_df: pd.DataFrame, rec: dict | None,
                     judge: str) -> dict[str, pd.Series] | None:
    """`judge`'s first-pass row masks: every row except the cells recorded in a rejudge record
    of any phase (review r1b, finding 1). Those cells belong to the rejudge state machine only:
    an unfinished pass resumes there, and a `done` pass's cells are terminal (never sent
    again), so the once-only rule holds across CLI restarts. None = no exclusion."""
    if rec is None or not any(c["judge"] == judge for c in rec["cells"]):
        return None
    return {m: ~mk for m, mk in cell_masks(gen_df, rec["cells"], judge).items()}


def pending_rejudge(jdf: pd.DataFrame, rec: dict) -> list[dict]:
    """The recorded cells an unfinished rejudge will still send: every cell while `planned`
    (the reset is re-applied), the cells not yet `scored` or `none` while `reset`, none once
    `done`."""
    if rec["phase"] == "planned":
        return list(rec["cells"])
    if rec["phase"] == "reset":
        return [c for c in rec["cells"]
                if jdf.at[c["row"], status_column(c["metric"], c["judge"])]
                not in ("scored", "none")]
    return []


# --- projection -----------------------------------------------------------------------------


def project(root: Path, conds: list[str], known: dict, judges_of: dict, metrics: list[str],
            presets: dict) -> dict:
    """{cond: {column: {rows_todo, projected_usd}}}: first-pass work (recorded rejudge cells
    excluded, `first_pass_masks`), plus the rejudge cells still to send (`rejudge__<column>`):
    with no record, the deciding judges' current `none` cells; with an unfinished record, its
    `pending_rejudge` cells; with a `done` record, none."""
    out: dict = {}
    for k in conds:
        gen, judged = paths(root, k, known[k])
        gen_df, jdf, _ = JR.load_or_init_judged(gen, judged)
        rec = read_rejudge(judged)
        cols = {}
        for m in metrics:
            for j in judges_of[k]:
                rows = JR.todo_rows(jdf, m, j, (first_pass_masks(gen_df, rec, j) or {}).get(m))
                usd = presets[j].project_usd(JR.filled_prompts(gen_df, rows, m)) if rows else 0.0
                cols[f"{m}__{j}"] = {"rows_todo": len(rows), "projected_usd": usd}
        if rec is None:
            todo = [(d["judge"], m, d[m].index[(d[m] == "none").fillna(False).astype(bool)])
                    for d in deciding_statuses(gen_df, jdf).values() for m in metrics]
        else:
            pend = pending_rejudge(jdf, rec)
            todo = [(j, m, sorted({c["row"] for c in pend
                                   if c["judge"] == j and c["metric"] == m}))
                    for j in sorted({c["judge"] for c in pend}) for m in metrics]
        for j, m, rows in todo:
            rows = list(rows)
            if rows:
                usd = presets[j].project_usd(JR.filled_prompts(gen_df, rows, m))
                c = cols.setdefault(f"rejudge__{m}__{j}", {"rows_todo": 0, "projected_usd": 0.0})
                c["rows_todo"] += len(rows)
                c["projected_usd"] += usd
        out[k] = cols
    return out


# --- main -----------------------------------------------------------------------------------


def main(argv=None, *, presets: dict | None = None, clients: dict | None = None,
         **judge_kw) -> int:
    """`presets`, `clients` and `judge_kw` (e.g. `wait`, `save_every`) are test hooks."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--conditions", nargs="+", required=True,
                    help="all | pilot-r<k> | <glob> (one or more)")
    ap.add_argument("--judges", nargs="+", choices=C.JUDGES, default=None,
                    help="default: luna for pilots, luna and gfl otherwise")
    ap.add_argument("--metrics", nargs="+", choices=C.METRICS, default=list(C.METRICS))
    ap.add_argument("--dry-run", action="store_true", help="print the projection only")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="refuse when the projection exceeds this")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--running-pod-usd", type=float, default=None,
                    help="running pods' projected $ (default: from logs/pods_E11.jsonl)")
    ap.add_argument("--probe", default=str(JP.PROBE_PATH))
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    presets = presets or JP.PRESETS
    pod_events = root / "logs" / f"pods_{C.EXP}.jsonl"
    known = known_conditions()

    def refuse(msg: str) -> int:
        print(f"{TAG} REFUSED: {msg}", file=sys.stderr)
        return 2

    try:
        conds = select(root, args.conditions)
        judges_of = {k: list(args.judges) if args.judges else
                     list(C.PILOT_JUDGES if known[k]["pilot"] else C.JUDGES) for k in conds}
        for k in conds:
            check_gen(paths(root, k, known[k])[0], k, known[k], root)
        proj = project(root, conds, known, judges_of, args.metrics, presets)
    except (OSError, ValueError, RuntimeError) as e:
        return refuse(str(e))
    total = sum(c["projected_usd"] for cols in proj.values() for c in cols.values())
    pods = (args.running_pod_usd if args.running_pod_usd is not None
            else JR.running_pod_usd_from_log(pod_events))
    for k, cols in proj.items():
        for col, c in cols.items():
            print(f"projection {k} {col}: {c['rows_todo']} rows, ${c['projected_usd']:.4f}")
    print(f"{TAG} PROJECTION: ${total:.4f} for {len(conds)} condition(s) (first pass plus the "
          f"known rejudge cells); running pods ${pods:.2f}")
    if args.max_usd is not None and total > args.max_usd:
        return refuse(f"projection ${total:.4f} > --max-usd ${args.max_usd:.4f}")
    try:
        guard = spend.check_total(total, "judge", running_pod_usd=pods, exp=C.EXP)
    except spend.BudgetExceeded as e:
        print(json.dumps(e.breakdown, indent=2), file=sys.stderr)
        return refuse(str(e))
    print(f"guard: E11 judge spend ${guard['judge_spent_usd']:.4f} + reserved "
          f"${guard['judge_reserved_usd']:.4f} + projected ${total:.4f} <= limit "
          f"${guard['judge_limit_usd']:.2f}")
    if args.dry_run:
        print(json.dumps({"dry_run": True, "conditions": conds, "projected_usd": round(total, 6),
                          "guard": guard, "columns": proj}, indent=2))
        return 0
    used = sorted({j for k in conds for j in judges_of[k]})  # the rejudge needs no other
    if clients is None:
        JP.load_env()
        try:
            for j in used:
                JP.check_probe(presets[j], Path(args.probe))
        except RuntimeError as e:
            return refuse(str(e))
        clients = {j: JP.make_client(presets[j].route) for j in used}
    run_kw = dict(exp=C.EXP, presets=presets, max_concurrency=args.concurrency,
                  max_retries=args.max_retries, running_pod_usd=args.running_pod_usd,
                  pod_events=pod_events, **judge_kw)
    usage: dict = {}
    report: dict = {}

    def log(s: str) -> None:
        print(s, flush=True)

    for k in conds:
        gen, judged = paths(root, k, known[k])
        try:
            if any(c["rows_todo"] for col, c in proj[k].items() if not col.startswith("rejudge")):
                # recorded rejudge cells (any phase) never re-enter the first pass (finding 1)
                rec0, gen_df = read_rejudge(judged), JR.read_gen(gen)
                res = {}
                for j in judges_of[k]:
                    res.update(JR.run_judging_reserved(
                        gen, judged, args.metrics, [j], clients,
                        note_prefix=f"{C.NOTE_PREFIX} {k}",
                        row_masks=first_pass_masks(gen_df, rec0, j), **run_kw))
                usage[k] = {col: {f: u.get(f) for f in ("rows_todo", "calls", "usd", "n_scored",
                                                        "n_none", "n_failed") if f in u}
                            for col, u in res.items()}
            else:
                print(f"{k}: nothing left to judge in the first pass")
            gen_qsets = set(JR.read_gen(gen)["qset"].astype(str))
            needed = {C.DECIDING_JUDGE[q] for q in gen_qsets}
            if not needed <= set(judges_of[k]) or set(args.metrics) != set(C.METRICS):
                log(f"{TAG} REJUDGE DEFERRED {k}: deciding judge(s) {sorted(needed)} and both "
                    f"metrics must be judged in this run")
                rec = None
            else:
                def run_cells(cells, k=k, gen=gen, judged=judged):
                    gen_df = JR.read_gen(gen)
                    for j in sorted({c["judge"] for c in cells}):
                        res = JR.run_judging_reserved(
                            gen, judged, list(C.METRICS), [j], clients,
                            note_prefix=f"{C.NOTE_PREFIX} {k} rejudge",
                            row_masks=cell_masks(gen_df, cells, j), **run_kw)
                        usage.setdefault(f"{k} rejudge", {}).update(
                            {col: {f: u.get(f) for f in ("rows_todo", "calls", "usd")
                                   if f in u} for col, u in res.items()})
                rec = rejudge(k, gen, judged, run_cells, log)
        except spend.BudgetExceeded as e:
            return refuse(f"{k}: {e}")
        except (OSError, ValueError, RuntimeError) as e:
            return refuse(f"{k}: {e}")
        gen_df, jdf, _ = JR.load_or_init_judged(gen, judged)
        report[k] = {"rejudge": None if rec is None else {
            f: rec.get(f) for f in ("phase", "statuses_before", "statuses_after")},
            "unscored": unscored(gen_df, jdf)}
    usd = sum(float(u.get("usd") or 0.0) for cols in usage.values() for u in cols.values())
    print(json.dumps({"usd": round(usd, 6), "usage": usage, "report": report}, indent=2))
    for k, r in report.items():
        for q, u in r["unscored"].items():
            flag = " INCOMPLETE" if u["incomplete"] else ""
            print(f"{TAG} UNSCORED {k} {q} ({u['judge']}): {u['n_unscored']}/{u['n']} "
                  f"({100 * u['share']:.2f}%){flag}")
    print(f"{TAG} DONE: ${usd:.4f} this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
