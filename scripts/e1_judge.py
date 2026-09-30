"""Judge one E1 generation CSV into its judged CSV (plan §1; prereg §4.7, §4.9).

Port of the working repo's judge drivers (../mats12_neel_working/scripts/judge_em.py,
judge_task.py) on top of emx.judge_run. Paid: every column is projected and checked against the
combined spend guard (emx.spend.check_total), including running pods' projected cost from the
pod log, before it runs; the actual cost goes to logs/costs.jsonl. Refuses to run until
scripts/e1_judge_probe.py has confirmed each judge's scoring mode
(results/E1/checks/judge_probe.json).

Default metrics by question set:
  general, medical -> aligned, coherent
  narrow           -> medcorrect, coherent
  medtopic (leakage) is run separately on EM rows only:  --metrics medtopic --em-rows-from luna

Batch judges (prereg §11 amendment C: gfl through OpenRouter's batch API at 50% price):
  --batch gfl routes that judge through emx.judge_batch (the other judges stay live). Its state
  (<judged stem>.batches.json) survives restarts: a rerun collects finished batches and never
  resubmits rows pending in an open batch. By default the run submits, then polls every
  --poll-s seconds until every batch is collected. --submit-only submits and exits;
  --collect submits nothing and waits for the open batches. Exit code 3: a batch ended
  failed / expired / cancelled (recorded; its rows stay unscored; rerun to resubmit).

Usage:
  uv run python scripts/e1_judge.py results/E1/gen/<condition>__<qset>.csv [--judges luna gfl]
      [--batch gfl] [--metrics aligned coherent] [--out PATH] [--concurrency 8] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx import judge_batch as JB
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
    ap.add_argument("--judges", nargs="+", choices=list(JP.PRESETS), default=list(JUDGES))
    ap.add_argument("--metrics", nargs="+", choices=METRICS, default=None)
    ap.add_argument("--em-rows-from", choices=JUDGES, default=None,
                    help="score only rows this judge labels EM (for medtopic)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true", help="project cost and check the guard only")
    ap.add_argument("--probe", default=str(JP.PROBE_PATH))
    ap.add_argument("--batch", nargs="+", default=[], metavar="JUDGE",
                    choices=[n for n, p in JP.PRESETS.items() if p.supports_batch],
                    help="judges (among --judges) to score through the OpenRouter batch API")
    ap.add_argument("--poll-s", type=float, default=60.0, help="batch poll interval (s)")
    ap.add_argument("--max-wait-s", type=float, default=None,
                    help="stop waiting after this long; open batches are collected by a rerun")
    ap.add_argument("--max-batch-requests", type=int, default=JB.DEFAULT_MAX_REQUESTS,
                    help="split larger jobs into batches of at most this many requests")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--submit-only", action="store_true",
                      help="batch: collect finished batches, submit, exit without waiting")
    mode.add_argument("--collect", action="store_true",
                      help="batch: submit nothing, wait for open batches to finish")
    ap.add_argument("--running-pod-usd", type=float, default=None,
                    help="running pods' projected $ for the spend guard (default: read the "
                         "pod log logs/pods_E1.jsonl before every column)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    missing = [j for j in args.batch if j not in args.judges]
    if missing:
        print(f"--batch {missing} not in --judges {args.judges}", file=sys.stderr)
        return 2
    live = [j for j in args.judges if j not in args.batch]
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
    batch_client = None
    if not args.dry_run:
        JP.load_env()
        for j in args.judges:
            JP.check_probe(JP.PRESETS[j], Path(args.probe))
        for j in live:
            clients[j] = JP.make_client(JP.PRESETS[j].route)
        if args.batch:
            batch_client = JB.make_batch_client()

    keys = ("rows_todo", "projected_usd", "calls", "usd", "n_scored", "n_none", "n_failed")
    summary: dict = {"judged_csv": str(out), "columns": {}}
    if live:
        res = JR.run_judging(
            gen, out, metrics, live, clients, row_masks=row_masks,
            max_concurrency=args.concurrency, max_retries=args.max_retries,
            dry_run=args.dry_run, running_pod_usd=args.running_pod_usd,
        )
        summary["columns"] = {col: {k: u.get(k) for k in keys if k in u}
                              for col, u in res.items()}
    rc = 0
    for j in args.batch:
        try:
            r = JB.run_batch_judging(
                gen, out, metrics, j, batch_client, row_masks=row_masks,
                running_pod_usd=args.running_pod_usd, do_submit=not args.collect,
                wait=not args.submit_only, poll_s=args.poll_s, max_wait_s=args.max_wait_s,
                max_requests=args.max_batch_requests, dry_run=args.dry_run,
            )
        except JB.BatchFailed as e:
            print(str(e), file=sys.stderr)
            r, rc = e.result, 3
        except JB.BatchStateError as e:
            print(str(e), file=sys.stderr)
            return 4
        summary.setdefault("batch", {})[j] = {
            "submit": {k: v for k, v in (r.get("submit") or {}).items() if k != "guard"},
            "collected": [{"batch_id": a["batch_id"], "status": a["status"],
                           "columns": {c: {k: u.get(k) for k in keys if k in u}
                                       for c, u in a["columns"].items()}}
                          for a in r.get("applied", [])],
            "failed": r.get("failed", []),
            "open": r.get("open", []),
        }
    print(json.dumps(summary, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
