"""E1 analysis driver (implementation-plan §1 "Analysis", §5 branches; prereg §4.4, §4.7, §4.10, §7).

Free, laptop only. Library: `emx.e1.analysis`. Every input is re-hashed and checked against its
signature, metadata and training records; any mismatch refuses (exit 2). Re-running over an
existing analysis JSON with different inputs refuses unless --force-recompute.

Modes:
  --recipe-check CELL            prereg §4.4 pass rule, e.g. inst-chat-bad-R0-s1 (exit 1 on fail)
  --engine-check VLLM HF         judged CSVs; vLLM − HF EM rate, 90% question-bootstrap interval,
                                 pass within ±3 points (exit 1 on fail)
  --part PART --recipe R         full labelling; writes results/E1/analysis/{condition}.json and
                                 summary_<part>.json; exit 3 if > 20% of seed-1 conditions are
                                 unusable (prereg §7 "Measurement failed")

Examples:
  uv run python scripts/e1_analyze.py --recipe-check inst-chat-bad-R0-s1
  uv run python scripts/e1_analyze.py --engine-check results/E1/judged/A__general.csv \\
      results/E1/judged/B__general.csv
  uv run python scripts/e1_analyze.py --part part2 --recipe R0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e1 import analysis as A


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--recipe-check", metavar="CELL")
    mode.add_argument("--engine-check", nargs=2, metavar=("VLLM_JUDGED", "HF_JUDGED"))
    mode.add_argument("--part", choices=sorted(A.PART_GROUPS))
    ap.add_argument("--recipe", choices=("R0", "R1", "R2"))
    ap.add_argument("--judges", nargs="+", default=None)
    ap.add_argument(
        "--max-samples-per-q",
        type=int,
        default=50,
        help="engine check: keep sample_idx < N in both files (default 50)",
    )
    ap.add_argument("--root", default=".")
    ap.add_argument("--force-recompute", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        if args.recipe_check:
            rec = A.recipe_check_cell(args.recipe_check, root, args.judges, args.force_recompute)
            print(f"recipe check {rec['condition']}: {'PASS' if rec['passed'] else 'FAIL'}")
            for r in rec["reasons"]:
                print(f"  - {r}")
            return 0 if rec["passed"] else 1
        if args.engine_check:
            v, h = map(Path, args.engine_check)
            rec = A.engine_check(v, h, args.judges, args.max_samples_per_q)
            out = root / A.ANALYSIS_DIR / "engine_check.json"
            A.write_json(out, rec)
            for j, r in rec["judges"].items():
                print(
                    f"{j}: vLLM {r['em_vllm']:.3f} HF {r['em_hf']:.3f} diff {r['diff']:+.3f} "
                    f"90% [{r['ci90'][0]:+.3f}, {r['ci90'][1]:+.3f}] "
                    f"{'PASS' if r['passed'] else 'FAIL'}"
                )
            print(f"wrote {out}")
            return 0 if rec["passed"] else 1
        if args.recipe is None:
            ap.error("--part needs --recipe")
        s = A.analyze_part(args.part, args.recipe, root, args.force_recompute)
    except A.AnalysisRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "labels": {k: v["result"] for k, v in s["labels"].items()},
                "stage_results": s["stage_results"],
                "missing": s["conditions_missing"],
                "seed1_unusable_fraction": s["seed1_unusable_fraction"],
                "outcome_scenario": s["outcome_scenario"],
            },
            indent=1,
            default=str,
        )
    )
    if s["stop_measurement_failed"]:
        print(
            "STOP: more than 20% of seed-1 conditions unusable (prereg §7, Measurement failed)",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
