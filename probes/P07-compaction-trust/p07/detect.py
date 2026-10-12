"""P07 cheating detector (plan "Measure": cheating after the resume, exploratory). Free: local
Docker only.

X3's detector (`x3.detector.detect` plus the fresh-container pass check, as
`scripts/x3_detect.py` runs them) over exactly the artifacts `measures.select_artifacts`
chooses, so a rerun's `.dupN` file and its base never both count. Snapshot 0 is the state right
after the resume (the replayed workspace plus any plant), so tampering is judged against it.
Appends one row per episode to results/detector.csv; idempotent by episode_id.

  PYTHONPATH=probes/P07-compaction-trust:src:scripts uv run --group swarm python -m p07.detect
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from p07 import contracts as C
from p07.measures import select_artifacts

DETECTOR_CSV = C.RESULTS_DIR / "detector.csv"
WORKERS = 8


def main() -> int:
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
    with ThreadPoolExecutor(WORKERS) as ex:
        for art, row in ex.map(check, todo):
            x3_detect.append_row(DETECTOR_CSV, row)
            n += 1
            print(f"{row.episode_id}  passed={row.passed} tamper_any={row.tamper_any} "
                  f"cheat={row.cheat}", flush=True)
    print(f"appended {n} row(s) to {DETECTOR_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
