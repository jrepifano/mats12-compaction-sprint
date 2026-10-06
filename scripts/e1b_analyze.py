"""E1b Part 1 analysis driver (implementation plan §2 U5; prereg §4.6-4.8, §4.11, §6).

Free, laptop only. Library: `emx.e1b.analysis` (rules in `emx.e1b.rules`). Every input is
re-hashed and checked against its signature, metadata, sample profile and training record; any
mismatch refuses (exit 2). Re-running over an existing analysis JSON with different inputs
refuses unless --force-recompute.

Writes results/E1b/analysis/{primary,robustness,breadth,reeval,timing,logprob_gap,just_enough,
scenarios}.json. `robustness.json`'s `seed2_plain_clear` is what the Part 3 gate reads.

  --cut2   prereg §7 cut 2: the timing subset's ext48 is x 5 at every step, step 0 and the
           final included

Example:
  uv run python scripts/e1b_analyze.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e1b import analysis as A


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--cut2", action="store_true", help="prereg §7 cut 2 (ext48 x 5 in timing)")
    ap.add_argument("--cut3", action="store_true", help="prereg §7 cut 3 (dense every 20 to 200)")
    ap.add_argument("--force-recompute", action="store_true")
    args = ap.parse_args(argv)
    try:
        res = A.analyze(Path(args.root), args.force_recompute, args.cut2, args.cut3)
    except A.AnalysisRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    t = res["timing"]["stages"]
    print(
        json.dumps(
            {
                "primary": res["primary"].get("result"),
                "scenarios": res["scenarios"],
                "seed2_plain_clear": res["robustness"]["seed2_plain_clear"],
                "onset": {st: s["onset"] for st, s in t.items()},
                "overfitting_point": {st: s["overfitting_point"] for st, s in t.items()},
            },
            indent=1,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
