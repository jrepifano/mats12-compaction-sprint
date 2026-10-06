"""X3b judges for one stage (implementation plan v0.4 §3 U2, §4 step 5; prereg §4.8).

Runs after the stage is TERMINAL and after `x3b_detect` and `collect` (the detector table and
episodes.csv exist). Without `--yes` this is a dry run: it prints each judgment's projected worst
case and spends nothing. With `--yes` it judges, reserving each batch under the stage's ledger
exp id (`x3b.judges.paid_judge`); a reservation a crashed batch left open is settled first.

Usage:
  uv run --group swarm python scripts/x3b_judge.py --stage glm                 # dry run
  uv run --group swarm python scripts/x3b_judge.py --stage glm --yes           # paid
  uv run --group swarm python scripts/x3b_judge.py --stage glm --reconcile     # settle only
"""
from __future__ import annotations

import argparse
import json

from x3b import contracts as C
from x3b import judges as J


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", required=True, choices=[s.value for s in C.StageKey])
    ap.add_argument("--judgment", default="all", choices=["all", *(j.value for j in C.Judgment)])
    ap.add_argument("--yes", action="store_true", help="make paid calls (default: dry run)")
    ap.add_argument("--reconcile", action="store_true",
                    help="only settle open judge reservations of this stage")
    a = ap.parse_args(argv)
    stage = C.StageKey(a.stage)
    if a.reconcile:
        print(json.dumps(J.reconcile_open(stage), indent=1, default=str))
        return 0
    judgments = list(C.Judgment) if a.judgment == "all" else [C.Judgment(a.judgment)]
    ecsv = C.episodes_csv(stage)
    for jd in judgments:
        res = J.paid_judge(stage, jd, episodes_dir=C.episodes_dir(stage),
                           out_dir=C.judged_dir(stage), detector_csv=C.detector_csv(stage),
                           dry_run=not a.yes, episodes_csv=ecsv if ecsv.exists() else None)
        print(json.dumps(res, default=str))
        if res["status"] == "refused":
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
