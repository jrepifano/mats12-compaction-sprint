"""E12 decisions, validation and analysis (prereg §4.5-§4.8, §6; plan v0.3.1 unit U7).

Usage (contracts.CLI_USAGE["e12_analyze"]):
  uv run python scripts/e12_analyze.py decide --stage poison|repair107|rescue214|final [--recipe R]
  uv run python scripts/e12_analyze.py validate [--recipe R]
  uv run python scripts/e12_analyze.py analyze [--recipe R]

Each prints one JSON object on its last stdout line. Exit codes (contracts): 0 ok, 3 refused (a
missing or inconsistent input, a stage already decided on other inputs, a stale validation),
1 failed (validate: a global check failed; the record is still written).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx.e12 import analysis as A
from emx.e12 import contracts as C
from emx.e12 import validation as V


def main(argv: list[str] | None = None, root: Path = C.REPO_ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--stage", required=True, choices=C.DECISION_STAGES)
    for p in (d, sub.add_parser("validate"), sub.add_parser("analyze")):
        p.add_argument("--recipe", default=C.DEFAULT_RECIPE, choices=sorted(C.RECIPES))
    args = ap.parse_args(argv)
    try:
        if args.cmd == "decide":
            out = A.decision_summary(A.decide(args.recipe, args.stage, root))
            code = C.EXIT_OK
        elif args.cmd == "validate":
            rec = V.validate(args.recipe, root)
            out = {"validation": C.VALIDATION_PATH, "validation_sha256": rec["validation_sha256"],
                   "global_pass": rec["global"]["pass"],
                   "global_pass_durability": rec["global"]["pass_durability"],
                   "failures": [f"{f['check']}: {f['reason']}" for f in rec["global"]["failures"]],
                   "readouts": {k: v["status"] for k, v in rec["readouts"].items()}}
            code = C.EXIT_OK if rec["global"]["pass_durability"] else C.EXIT_FAILED
        else:
            rec = A.analyze(args.recipe, root)
            out = {"analysis": C.ANALYSIS_PATH, "scenario": rec["scenario"],
                   "labels": {k: v.get("label") for k, v in rec["primaries"].items()},
                   "validity": {k: v["validity"] for k, v in rec["primaries"].items()},
                   "decisions_sha256": rec["decisions_sha256"]}
            code = C.EXIT_OK
    except A.AnalysisRefused as e:
        out, code = {"refused": str(e)}, C.EXIT_REFUSED
    print(json.dumps(out, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
