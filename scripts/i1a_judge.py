"""Judge I1a+ readouts arms with WorkspaceBench, in capped chunks (implementation plan §2 U5).

Paid unless --dry-run: every chunk is projected (x1.25 retry headroom, at no less than
$0.0015/call) and checked against the combined spend guard (emx.spend.check_total, exp I1a)
before it runs, and every HTTP attempt (retries included) reserves budget first: an attempt that
would pass the chunk's projection or the caps is refused and the run stops. Every response's cost
is journaled as it lands and reconciled into logs/costs.jsonl (emx.i1a.judge_driver). A paid run
needs --stage, all required checks passed, the stage's approval marker in logs/approvals/
(pilot: I1a-P2, gate: I1a-P3 plus results/I1a/judged/PILOT.json, samples: I1a-P4). The key is
OPENROUTER_API_KEY from .env (the shared project key); it is never printed.
Hallucination runs with opts=verify=0 (plan §0) unless --allow-verify (the checkpoint-5 add-on).

Usage:
  uv run python scripts/i1a_judge.py --stage pilot --arm olens_rl600 --family multihop --limit 10
  uv run python scripts/i1a_judge.py --stage gate --arm jlens --family all
      [--items-file ids.txt --gate-cut NAME] [--layers 44] [--usd-per-call 0.002] [--chunk 25]
      [--allow-missing] [--allow-verify --opts verify=1] [--dry-run]

Exit codes: 0 ok; 2 spend-guard or prerequisite refusal, a chunk stopped at its projection, or a
benchmark usage/data error; 3 key or judge config; 4 not clean (cells unjudged after retries, a
final pass that needed a call, or an unrestricted run that is not complete).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wsbench.llm import JudgeConfigError

from emx import spend
from emx.i1a import contracts as C
from emx.i1a import judge_driver as J


def read_items(path: str) -> list[str]:
    """A JSON list of ids, or one id per line (blank lines and # comments ignored)."""
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return [str(x) for x in json.loads(text)]
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def rate(v: str) -> float:
    try:
        return J.usd_rate(float(v))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--arm", required=True, choices=sorted(C.ARM_FAMILIES))
    ap.add_argument(
        "--family",
        nargs="+",
        default=["all"],
        help="families to judge, or 'all' (every family the arm has)",
    )
    ap.add_argument("--items-file", default=None)
    ap.add_argument("--limit", type=int, default=0, help="pilot: the first N bank items")
    ap.add_argument("--layers", default=None, help="comma list, e.g. 44")
    ap.add_argument("--opts", default="verify=0", help="wsbench opts (comma list)")
    ap.add_argument("--chunk", type=int, default=25)
    ap.add_argument(
        "--usd-per-call",
        type=rate,
        default=None,
        help=f"projection rate (default ${J.DEFAULT_USD_PER_CALL}, floor ${J.MIN_USD_PER_CALL})",
    )
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument(
        "--allow-verify",
        action="store_true",
        help="let hallucination run stage-2 verification (checkpoint-5 add-on only)",
    )
    ap.add_argument(
        "--gate-cut",
        default=None,
        help="name of the prereg §7.3 pre-set cut when --items-file drops gate-arm items; "
        "writes results/I1a/judged/SCOPE_CURTAILED.json",
    )
    ap.add_argument("--gate-cut-reason", default="", help="why the cut was applied")
    ap.add_argument(
        "--stage",
        choices=J.STAGES,
        default=None,
        help="checkpoint of a paid run (required unless --dry-run): pilot = a --limit run (P2), "
        "gate = gate readers and baselines (P3), samples = sel_*/olens_s* arms (P4)",
    )
    ap.add_argument("--dry-run", action="store_true", help="project, check the guard, no calls")
    ap.add_argument("--env", default=None, help="path to .env (default: repo root)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    families = list(C.ARM_FAMILIES[args.arm]) if args.family == ["all"] else args.family
    items = read_items(args.items_file) if args.items_file else None
    layers = [int(x) for x in args.layers.split(",") if x.strip()] if args.layers else None
    opts = tuple(o for o in args.opts.split(",") if o.strip())
    if not args.dry_run and args.stage is None:
        print("refused: a paid run needs --stage pilot|gate|samples", file=sys.stderr)
        return 2
    try:
        if not args.dry_run:
            J.load_key(args.env)
        out = []
        for fam in families:
            out.append(
                J.judge_arm(
                    args.arm,
                    fam,
                    items=items,
                    limit=args.limit,
                    layers=layers,
                    opts=opts,
                    chunk=args.chunk,
                    usd_per_call=args.usd_per_call,
                    dry_run=args.dry_run,
                    allow_missing=args.allow_missing,
                    allow_verify=args.allow_verify,
                    gate_cut=args.gate_cut,
                    gate_cut_reason=args.gate_cut_reason,
                    stage=args.stage,
                )
            )
    except spend.BudgetExceeded as e:
        print(json.dumps(e.breakdown, indent=2), file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except JudgeConfigError as e:
        print(f"judge config error: {e}", file=sys.stderr)
        return 3
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
