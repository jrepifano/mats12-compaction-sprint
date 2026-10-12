"""Arm H cheating (plan "Measure", secondary): X3's detector over P10's Arm H artifacts, as
`p07/detect.py` runs it for P07 (`x3.detector.detect` plus X3's fresh-container pass check,
`x3.passcheck`, which names, labels, bounds and removes its containers). Free: local Docker only.
Appends one row per episode to results/armh/detector.csv; idempotent by episode_id.

    uv run --group swarm python probes/P10-fabricated-history-trust/p10/detect.py [--workers 4]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p10 import armh  # noqa: E402
from p10 import common as K  # noqa: E402

DETECTOR_CSV = K.RESULTS / "armh" / "detector.csv"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    import x3_detect
    from p07.measures import failure_reason

    from x3 import passcheck
    from x3.detector import detect
    from x3.tasks import load_manifest

    tasks = {t.task_id: t for t in load_manifest().tasks}
    seen = x3_detect.done_ids(DETECTOR_CSV)
    todo = []
    for p in sorted(armh.EPISODES.rglob("*.json")):
        if ".failed" in p.name:
            continue
        art = json.loads(p.read_text())
        if art["meta"]["episode_id"] not in seen and not failure_reason(art):
            todo.append(art)

    def check(art: dict):
        task = tasks[art["meta"]["task_id"]]
        return detect(art, task, passcheck.run_pass_check(art.get("final_solution_py") or "",
                                                          task))

    n = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for row in ex.map(check, todo):
            x3_detect.append_row(DETECTOR_CSV, row)
            n += 1
    print(f"appended {n} row(s) to {DETECTOR_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
