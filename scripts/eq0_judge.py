"""Judge EQ0's generation CSVs: both judges (luna, gfl) score aligned and coherent on every answer.

For each model in --models (default all four), results/EQ0/gen/<model>.csv is judged into
results/EQ0/judged/<model>.csv through `emx.judge_run.run_judging_reserved` (exp "EQ0", note
prefix "EQ0 judge <model>"): one API reservation per (metric, judge) column, recovered / settled /
released as in E3, resumable (rows already `scored` or `none` are never re-sent), each column
signed with its prompt, judge settings and source CSV.

Prompts: one rubric per metric for every question set (`emx.prompts.load_prompt`: the Betley et
al. aligned and coherent prompts in prompts/E1/, as E1b/E3/E9 used on both general and ext48);
the qset only decides which judge's call counts, in analysis (contracts.DECIDING_JUDGE).

Before any call:
1. the spend projection for ALL remaining work (sum over models x metrics x judges of
   `PRESETS[j].project_usd(filled prompts of the rows still to judge)`, exactly what
   `run_judging_reserved` projects per column) is printed, and checked with
   `emx.spend.check_total(projected, "judge", running pods, exp="EQ0")` against EQ0's caps (API
   limit $5, hard cap $20) with EQ0's recorded spend and open reservations; above a limit the
   script exits 2 and sends nothing;
2. `emx.judge_panel.check_probe` must confirm each judge's scoring mode and request from the
   committed probe record results/E1/checks/judge_probe.json (tracked in git, so present in a
   worktree; luna logprob, gfl integer), as scripts/e1_judge.py requires.
`--dry-run` stops after the projection: no keys read, no clients created, nothing written.
Running pods' projected cost comes from logs/pods_EQ0.jsonl (`--running-pod-usd` overrides).

No re-arm hook: EQ0's limits are fixed (GPU $15 + API $5 = hard cap $20), so while judge spend
stays within the API limit the pods' kill line (min(GPU limit, hard cap - judge spend)) never
moves, unlike E3's shared-cap reservations.

Usage:
  uv run python scripts/eq0_judge.py [--models base bad_medical ...] [--dry-run]
      [--concurrency 8] [--max-retries 6] [--running-pod-usd X]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx import judge_panel as JP
from emx import judge_run as JR
from emx import spend
from emx.contracts import GEN_COLUMNS
from emx.eq0 import contracts as C
from emx.qwen import registry as R


def pod_events() -> Path:
    from emx import runpod  # lazy: pulls in requests

    return runpod.LOGS / f"pods_{C.EXP}.jsonl"


def check_gen(gen: Path, model: str) -> None:
    df = JR.read_gen(gen)
    if tuple(df.columns) != GEN_COLUMNS:
        raise ValueError(f"{gen}: columns {list(df.columns)} != GEN_COLUMNS")
    if set(df["condition"].astype(str)) != {model}:
        raise ValueError(f"{gen}: condition is not {model!r} on every row")
    if len(df) != C.ROWS_PER_MODEL:
        raise ValueError(f"{gen}: {len(df)} rows, expected {C.ROWS_PER_MODEL}")


def project(root: Path, models: list[str], metrics: list[str], judges: list[str],
            presets: dict) -> dict:
    """{model: {column: {rows_todo, projected_usd}}} for the rows still to judge."""
    out: dict = {}
    for k in models:
        gen, judged = root / C.gen_csv(k), root / C.judged_csv(k)
        gen_df, jdf, _ = JR.load_or_init_judged(gen, judged)
        cols = {}
        for m in metrics:
            for j in judges:
                rows = JR.todo_rows(jdf, m, j, None)
                usd = presets[j].project_usd(JR.filled_prompts(gen_df, rows, m)) if rows else 0.0
                cols[f"{m}__{j}"] = {"rows_todo": len(rows), "projected_usd": usd}
        out[k] = cols
    return out


def summarize(root: Path, models: list[str], metrics: list[str], judges: list[str]) -> dict:
    """Status counts per model and column from the judged CSVs (judging summary)."""
    out = {}
    for k in models:
        p = root / C.judged_csv(k)
        if not p.exists():
            out[k] = None
            continue
        _, jdf, _ = JR.load_or_init_judged(root / C.gen_csv(k), p)
        out[k] = {}
        for m in metrics:
            for j in judges:
                st = jdf.get(f"{m}__{j}__status")
                out[k][f"{m}__{j}"] = {
                    s: int((st == s).sum()) if st is not None else 0
                    for s in ("scored", "none", "failed")
                } | {"unjudged": int(st.isna().sum()) if st is not None else len(jdf)}
    return out


def main(argv=None, *, presets: dict | None = None, clients: dict | None = None,
         **judge_kw) -> int:
    """`presets`, `clients` and `judge_kw` (e.g. `wait`) are test hooks; the CLI passes none."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", nargs="+", choices=R.MODEL_KEYS, default=list(R.MODEL_KEYS))
    ap.add_argument("--judges", nargs="+", choices=C.JUDGES, default=list(C.JUDGES))
    ap.add_argument("--metrics", nargs="+", choices=C.METRICS, default=list(C.METRICS))
    ap.add_argument("--dry-run", action="store_true", help="print the projection only")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--running-pod-usd", type=float, default=None,
                    help="running pods' projected $ (default: from logs/pods_EQ0.jsonl)")
    ap.add_argument("--probe", default=str(JP.PROBE_PATH))
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)
    root = Path(args.root)
    presets = presets or JP.PRESETS
    try:
        for k in args.models:
            check_gen(root / C.gen_csv(k), k)
        proj = project(root, args.models, args.metrics, args.judges, presets)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"EQ0 JUDGE REFUSED: {e}", file=sys.stderr)
        return 2
    total = sum(c["projected_usd"] for cols in proj.values() for c in cols.values())
    largest = max((c["projected_usd"] for cols in proj.values() for c in cols.values()),
                  default=0.0)
    pods = (args.running_pod_usd if args.running_pod_usd is not None
            else JR.running_pod_usd_from_log(pod_events()))
    for k, cols in proj.items():
        for col, c in cols.items():
            print(f"projection {k} {col}: {c['rows_todo']} rows, ${c['projected_usd']:.4f}")
    print(f"EQ0 JUDGE PROJECTION: ${total:.4f} for all remaining work; largest column "
          f"${largest:.4f} (reserved at {JR.RESERVATION_MARGIN}x while it runs); running pods "
          f"${pods:.2f}")
    try:
        guard = spend.check_total(total, "judge", running_pod_usd=pods, exp=C.EXP)
    except spend.BudgetExceeded as e:
        print(f"EQ0 JUDGE REFUSED: {e}", file=sys.stderr)
        print(json.dumps(e.breakdown, indent=2), file=sys.stderr)
        return 2
    print(f"guard: EQ0 judge spend ${guard['judge_spent_usd']:.4f} + reserved "
          f"${guard['judge_reserved_usd']:.4f} + projected ${total:.4f} <= limit "
          f"${guard['judge_limit_usd']:.2f}")
    if args.dry_run:
        print(json.dumps({"dry_run": True, "projected_usd": round(total, 6), "guard": guard,
                          "columns": proj}, indent=2))
        return 0
    if clients is None:
        JP.load_env()
        try:
            for j in args.judges:
                JP.check_probe(presets[j], Path(args.probe))
        except RuntimeError as e:
            print(f"EQ0 JUDGE REFUSED: {e}", file=sys.stderr)
            return 2
        clients = {j: JP.make_client(presets[j].route) for j in args.judges}
    usage: dict = {}
    for k in args.models:
        if not any(c["rows_todo"] for c in proj[k].values()):
            print(f"{k}: nothing left to judge")
            continue
        res = JR.run_judging_reserved(
            root / C.gen_csv(k), root / C.judged_csv(k), args.metrics, args.judges, clients,
            exp=C.EXP, note_prefix=f"{C.NOTE_PREFIX} {k}", presets=presets,
            max_concurrency=args.concurrency, max_retries=args.max_retries,
            running_pod_usd=args.running_pod_usd, pod_events=pod_events(), **judge_kw,
        )
        usage[k] = {col: {f: u.get(f) for f in ("rows_todo", "calls", "usd", "n_scored",
                                                "n_none", "n_failed") if f in u}
                    for col, u in res.items()}
    summary = summarize(root, args.models, args.metrics, args.judges)
    usd = sum(float(u.get("usd") or 0.0) for cols in usage.values() for u in cols.values())
    print(json.dumps({"usd": round(usd, 6), "usage": usage, "status": summary}, indent=2))
    for k, cols in summary.items():
        for col, n in (cols or {}).items():
            print(f"EQ0 JUDGED {k} {col}: scored {n['scored']}, none {n['none']}, failed "
                  f"{n['failed']}, unjudged {n['unjudged']}")
    print(f"EQ0 JUDGE DONE: ${usd:.4f} this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
