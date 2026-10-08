"""X3b batch detector (implementation plan §3 U5; prereg §4.4). Free: local Docker only.

Runs X3's fresh-container pass check and `x3.detector.detect` over every completed episode
artifact of one stage, main and smoke (`results/X3b/<stage>/episodes/`), appending one row per
episode to `results/X3b/<stage>/detector.csv` in X3's `DetectorRow` columns. It is a thin wrapper
over `scripts/x3_detect.py`'s `run` (the same skip rule: artifacts with `error` set are skipped;
idempotent by episode_id), with X3b's paths. X3b uses X3's tasks and sandbox image unchanged.

  uv run --group swarm python scripts/x3b_detect.py --stage glm|gemini [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

import x3b_common as common
from x3b_common import C


def run(stage: C.StageKey, *, dry_run: bool = False, pass_check=None, detect=None) -> int:
    x3_detect = common.load_script("x3_detect")
    return x3_detect.run(C.episodes_dir(stage), C.detector_csv(stage), dry_run=dry_run,
                         pass_check=pass_check, detect=detect)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_stage_arg(ap)
    ap.add_argument("--dry-run", action="store_true", help="list what would be checked")
    a = ap.parse_args(argv)
    stage = C.StageKey(a.stage)
    if not C.episodes_dir(stage).exists():
        print(f"no episodes under {C.episodes_dir(stage)}")
        return 0
    run(stage, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
