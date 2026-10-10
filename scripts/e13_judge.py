"""Judge E13 generation CSVs with both judges (`emx.e13.judging.judge_gen`), resumably.

Each argument is a gen CSV path or a gen name (`results/E13/gen/<name>.csv`). Before any call
the spend projection for ALL remaining work over every given CSV (per column, exactly what
`run_judging_reserved` projects; `keep_raw` backfill included) is printed and checked once with
`emx.spend.check_total(total, "judge", running pods, exp="E13")`; above a limit the script exits
2 and sends nothing (pattern of `scripts/eq0_judge.py`). `--dry-run` stops after the projection:
no keys read, no clients created, nothing written. Each CSV is then judged by `judge_gen` (one
API reservation per column, raw judge text kept). A second run is the retry pass for failed
rows. Running pods' projected cost comes from logs/pods_E13.jsonl (`--running-pod-usd`
overrides). Log tags start with `E13_JUDGE`.

Usage:
  uv run python scripts/e13_judge.py dev__org_r1_s0 [more gen names or paths] [--dry-run]
      [--judges luna gfl] [--concurrency 32] [--max-retries 6] [--running-pod-usd X]

Concurrency (2026-10-10, throughput): up to `--concurrency` calls per column are in flight
(default E13_JUDGE_CONCURRENCY = 32, or $EMX_JUDGE_CONCURRENCY when set; the library default in
`emx.judge_run` stays 8). The calls, prompts, models, sampling and spend projection are the same at
any concurrency; 429 rate-limit replies back off and retry (`emx.judge_run`, "Concurrency").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx import judge_run as JR
from emx import spend
from emx.e13 import contracts as C
from emx.e13 import judging as J

E13_JUDGE_CONCURRENCY = 32  # was 8; 7,680 calls took 627 s at 8 in flight (2026-10-10)


def resolve(arg: str, root: Path) -> Path:
    p = Path(arg)
    if p.suffix == ".csv":
        return p if p.is_absolute() else root / p
    return root / C.gen_csv(arg)


def main(argv=None, *, presets: dict | None = None, clients: dict | None = None,
         **judge_kw) -> int:
    """`presets`, `clients` and `judge_kw` are test hooks; the CLI passes none."""
    from emx import judge_panel as JP

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("gens", nargs="+", help="gen CSV paths or gen names")
    ap.add_argument("--judges", nargs="+", choices=C.JUDGES, default=list(C.JUDGES))
    ap.add_argument("--dry-run", action="store_true", help="print the projection only")
    ap.add_argument("--concurrency", type=int, default=None,
                    help=f"calls in flight per column (default ${JR.CONCURRENCY_ENV} or "
                         f"{E13_JUDGE_CONCURRENCY})")
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--running-pod-usd", type=float, default=None)
    ap.add_argument("--probe", default=None)
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    ap.add_argument("--ledger", default=None, help=argparse.SUPPRESS)  # tests
    a = ap.parse_args(argv)
    try:
        if a.concurrency is None:
            a.concurrency = JR.judge_concurrency(E13_JUDGE_CONCURRENCY)
        if a.concurrency < 1:
            raise ValueError(f"--concurrency {a.concurrency} is not a positive integer")
    except ValueError as e:
        print(f"E13_JUDGE_REFUSED: {e}", file=sys.stderr)
        return 2
    root = Path(a.root)
    presets = presets or JP.PRESETS
    gens = [resolve(g, root) for g in a.gens]
    try:
        proj = {g.stem: J.project(g, JR.default_judged_path(g), a.judges, presets) for g in gens}
    except (OSError, ValueError, RuntimeError) as e:
        print(f"E13_JUDGE_REFUSED: {e}", file=sys.stderr)
        return 2
    total = sum(c["projected_usd"] for cols in proj.values() for c in cols.values())
    pods = (a.running_pod_usd if a.running_pod_usd is not None
            else JR.running_pod_usd_from_log(J.pod_events()))
    for name, cols in proj.items():
        for col, c in cols.items():
            print(f"projection {name} {col}: {c['rows_todo']} rows, ${c['projected_usd']:.4f}")
    print(f"E13_JUDGE_PROJECTION: ${total:.4f} for all remaining work in {len(gens)} CSV(s); "
          f"running pods ${pods:.2f}; concurrency {a.concurrency}")
    try:
        guard = spend.check_total(total, "judge", running_pod_usd=pods, exp=C.EXP,
                                  ledger=a.ledger)
    except spend.BudgetExceeded as e:
        print(f"E13_JUDGE_REFUSED: {e}", file=sys.stderr)
        print(json.dumps(e.breakdown, indent=2), file=sys.stderr)
        return 2
    print(f"guard: E13 judge spend ${guard['judge_spent_usd']:.4f} + reserved "
          f"${guard['judge_reserved_usd']:.4f} + projected ${total:.4f} <= limit "
          f"${guard['judge_limit_usd']:.2f}")
    if a.dry_run:
        print("E13_JUDGE_DRY_RUN " + json.dumps({"projected_usd": round(total, 6),
                                                 "columns": proj}))
        return 0
    usd = 0.0
    for g in gens:
        try:
            res = J.judge_gen(g, judges=a.judges, presets=presets, clients=clients,
                              running_pod_usd=a.running_pod_usd, ledger=a.ledger,
                              probe=a.probe, max_concurrency=a.concurrency,
                              max_retries=a.max_retries, **judge_kw)
        except spend.BudgetExceeded as e:
            print(f"E13_JUDGE_REFUSED: {g.stem}: {e}", file=sys.stderr)
            return 2
        usd += sum(float(u.get("usd") or 0.0) for u in res["usage"].values())
        n_retry = J.reset_for_retry(JR.default_judged_path(g))
        if n_retry:  # the one retry of malformed (non-REFUSAL "none") replies
            print(f"E13_JUDGE_RETRY {g.stem}: {n_retry} malformed repl(ies) re-queued once")
            res = J.judge_gen(g, judges=a.judges, presets=presets, clients=clients,
                              running_pod_usd=a.running_pod_usd, ledger=a.ledger,
                              probe=a.probe, max_concurrency=a.concurrency,
                              max_retries=a.max_retries, **judge_kw)
            usd += sum(float(u.get("usd") or 0.0) for u in res["usage"].values())
        comp = J.completeness(JR.default_judged_path(g), g)
        for c in comp["cells"]:
            worst = max(c["columns"].values(), default=0.0)
            print(f"E13_JUDGED {g.stem} {c['condition']} {c['task_type']}: {c['n_rows']} rows, "
                  f"worst column incomplete {worst:.3f}"
                  + (" INCOMPLETE" if c["incomplete"] else ""))
    print(f"E13_JUDGE_DONE: ${usd:.4f} this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
