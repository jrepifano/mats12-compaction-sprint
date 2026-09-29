"""Judge one E1 generation CSV into its judged CSV (plan §1; prereg §4.7, §4.9).

Port of the working repo's judge drivers (../mats12_neel_working/scripts/judge_em.py,
judge_task.py) on top of emx.judge_run. Paid: every column is projected and checked against the
combined spend guard (emx.spend.check_total) before it runs; the actual cost goes to
logs/costs.jsonl. Refuses to run until scripts/e1_judge_probe.py has confirmed each judge's
scoring mode (results/E1/checks/judge_probe.json).

Default metrics by question set:
  general, medical -> aligned, coherent
  narrow           -> medcorrect, coherent
  medtopic (leakage) is run separately on EM rows only:  --metrics medtopic --em-rows-from luna

Usage:
  uv run python scripts/e1_judge.py results/E1/gen/<condition>__<qset>.csv [--judges luna dsk]
      [--metrics aligned coherent] [--out PATH] [--concurrency 8] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx import judge_panel as JP
from emx import judge_run as JR
from emx.contracts import JUDGES, METRICS

DEFAULT_METRICS = {
    "general": ["aligned", "coherent"],
    "medical": ["aligned", "coherent"],
    "narrow": ["medcorrect", "coherent"],
}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("gen_csv")
    ap.add_argument("--out", default=None, help="judged CSV (default: sibling judged/ dir)")
    ap.add_argument("--judges", nargs="+", choices=JUDGES, default=list(JUDGES))
    ap.add_argument("--metrics", nargs="+", choices=METRICS, default=None)
    ap.add_argument("--em-rows-from", choices=JUDGES, default=None,
                    help="score only rows this judge labels EM (for medtopic)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true", help="project cost and check the guard only")
    ap.add_argument("--probe", default=str(JP.PROBE_PATH))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    gen = Path(args.gen_csv)
    out = Path(args.out) if args.out else JR.default_judged_path(gen)
    if args.metrics is None:
        qsets = set(JR.read_gen(gen)["qset"].astype(str)) if gen.exists() else set()
        if len(qsets) != 1 or next(iter(qsets)) not in DEFAULT_METRICS:
            print(f"cannot infer metrics from qset {sorted(qsets)}; pass --metrics",
                  file=sys.stderr)
            return 2
        metrics = DEFAULT_METRICS[next(iter(qsets))]
    else:
        metrics = args.metrics

    row_masks = None
    if args.em_rows_from:
        if not out.exists():
            print(f"{out} does not exist; score aligned and coherent first", file=sys.stderr)
            return 2
        _, judged, _ = JR.load_or_init_judged(gen, out)
        mask = JR.em_mask(judged, args.em_rows_from)
        row_masks = {m: mask for m in metrics}
        print(f"EM rows under {args.em_rows_from}: {int(mask.sum())}/{len(mask)}", file=sys.stderr)

    clients = {}
    if not args.dry_run:
        JP.load_env()
        for j in args.judges:
            JP.check_probe(JP.PRESETS[j], Path(args.probe))
            clients[j] = JP.make_client(JP.PRESETS[j].route)

    res = JR.run_judging(
        gen, out, metrics, args.judges, clients, row_masks=row_masks,
        max_concurrency=args.concurrency, max_retries=args.max_retries, dry_run=args.dry_run,
    )
    summary = {
        col: {k: u.get(k) for k in ("rows_todo", "projected_usd", "calls", "usd", "n_scored",
                                     "n_none", "n_failed") if k in u}
        for col, u in res.items()
    }
    print(json.dumps({"judged_csv": str(out), "columns": summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
