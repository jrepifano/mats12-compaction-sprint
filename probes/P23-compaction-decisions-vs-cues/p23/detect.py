"""P23 cheating detector (ported from P08's p08/detect.py; P23 adds --follow, which streams the
detector alongside the episodes: P08 review r1, finding 7).

P08 cheating detector (plan "Measure", F1: cheating after the resume). Ported from P07's
detect.py. Free: local Docker only.

X3's detector (`x3.detector.detect` plus the fresh-container pass check, as
`scripts/x3_detect.py` runs them) over exactly the artifacts `measures.select_artifacts`
chooses, so a rerun's `.dupN` file and its base never both count. Snapshot 0 is the state right
after the resume (the replayed workspace), so tampering is judged against it.
Appends one row per episode to results/detector.csv; idempotent by episode_id.

  PYTHONPATH=probes/P23-compaction-decisions-vs-cues:src:scripts uv run --group swarm python -m p23.detect
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from p23 import contracts as C
from p23.measures import select_artifacts

DETECTOR_CSV = C.DETECTOR_CSV
WORKERS = 6


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--follow", action="store_true",
                    help="repeat every --poll seconds until results/DETECT_STOP exists and a "
                    "pass finds nothing new")
    ap.add_argument("--poll", type=int, default=90)
    ap.add_argument("--workers", type=int, default=WORKERS)
    a = ap.parse_args(argv)
    if not a.follow:
        return one_pass(a.workers)
    stop = C.RESULTS_DIR / "DETECT_STOP"
    while True:
        n = one_pass(a.workers)
        if n == 0 and stop.exists():
            return 0
        if n == 0:
            time.sleep(a.poll)


def one_pass(workers: int = WORKERS) -> int:
    """One detector pass over the artifacts with no detector row yet; returns rows appended."""
    import x3_detect

    from x3 import passcheck
    from x3.detector import detect
    from x3.tasks import load_manifest

    tasks = {t.task_id: t for t in load_manifest().tasks}
    seen = x3_detect.done_ids(DETECTOR_CSV)
    chosen, _ = select_artifacts(C.EPISODES_DIR)
    todo = []
    for p in chosen:
        art = json.loads(p.read_text())
        if art["meta"]["episode_id"] not in seen and "/mock/" not in str(p):
            todo.append(art)

    def check(art: dict):
        task = tasks[art["meta"]["task_id"]]
        return art, detect(art, task, passcheck.run_pass_check(art.get("final_solution_py") or "",
                                                                task))

    n = 0
    # the pass check is two fresh containers per episode (~25 s serial); run WORKERS at once,
    # appending rows from this thread only
    with ThreadPoolExecutor(workers) as ex:
        for art, row in ex.map(check, todo):
            x3_detect.append_row(DETECTOR_CSV, row)
            n += 1
            print(f"{row.episode_id}  passed={row.passed} tamper_any={row.tamper_any} "
                  f"cheat={row.cheat}", flush=True)
    print(f"appended {n} row(s) to {DETECTOR_CSV}", flush=True)
    return n


if __name__ == "__main__":
    sys.exit(main())
