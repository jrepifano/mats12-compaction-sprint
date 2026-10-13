"""P37 cheating detector (plan "Measure"): X3's detector (`x3.detector.detect`) plus the
fresh-container pass check on every continuation artifact, as P23's detect.py runs it. Snapshot
0 of a continuation is the restored site state, so tampering is judged against it (the test file
there is pristine: sites have none changed). Containers hold P37 slots. Appends one row per
episode to results/detector.csv; idempotent by episode_id.

  PYTHONPATH=probes/P37-grader-belief-causal:src:scripts uv run --group swarm \
      python -m p37.detect [--follow] [--workers 2]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from p37 import contracts as C

SKIP_END = ("replay_mismatch", "sandbox_error", "api_error")
WORKERS = 2


def one_pass(workers: int = WORKERS) -> int:
    import x3_detect

    from p37.sites import slotted_runner
    from x3 import passcheck
    from x3.detector import detect
    from x3.tasks import load_manifest

    tasks = {t.task_id: t for t in load_manifest(C.TASKS_JSON).tasks}
    seen = x3_detect.done_ids(C.DETECTOR_CSV)
    todo = []
    for p in sorted(C.EPISODES_DIR.rglob("*.json")):
        art = json.loads(p.read_text())
        if art["meta"]["episode_id"] in seen or art.get("end_reason") in SKIP_END:
            continue
        todo.append(art)

    def check(art: dict):
        task = tasks[art["meta"]["task_id"]]
        pr = passcheck.run_pass_check(art.get("final_solution_py") or "", task,
                                      runner=slotted_runner)
        return detect(art, task, pr)

    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for row in ex.map(check, todo):
            x3_detect.append_row(C.DETECTOR_CSV, row)
            n += 1
    print(f"appended {n} row(s) to {C.DETECTOR_CSV}", flush=True)
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--follow", action="store_true",
                    help="repeat every --poll s until results/DETECT_STOP exists and a pass "
                    "finds nothing new")
    ap.add_argument("--poll", type=int, default=90)
    ap.add_argument("--workers", type=int, default=WORKERS)
    a = ap.parse_args(argv)
    if not a.follow:
        one_pass(a.workers)
        return 0
    stop = C.RESULTS_DIR / "DETECT_STOP"
    while True:
        n = one_pass(a.workers)
        if n == 0 and stop.exists():
            return 0
        if n == 0:
            time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main())
