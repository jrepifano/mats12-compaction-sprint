"""Nudge sensitivity (log.md, validity check from the launching session): per continuation, the
first user turn after the edit (X3's nudge) and whether cheating happened *before* it.

`cheat_pre_nudge`: X3's tamper scan over the snapshots taken before the first nudge, or the
solution.py text as it stood at the last of those snapshots passing the pristine tests (X3's
fresh-container pass check). Undetermined (empty) when that solution text was not captured.
Continuations without a nudge copy the full-horizon `cheat`. Writes results/nudges.csv and
results/nudge_analysis.json (V - S on all continuations, on cheating before the first nudge,
and on continuations without a nudge: the last conditions on a post-treatment variable).

  PYTHONPATH=probes/P37-grader-belief-causal:src:scripts uv run --group swarm \
      python -m p37.nudges
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from p37 import contracts as C

SOLUTION = "/agent/solution.py"
COLS = ("episode_id", "cond", "n_nudges", "first_nudge_turn", "cheat_pre_nudge")


def first_nudge(art: dict) -> tuple[int | None, int]:
    """(turn after which the first nudge came, number of nudges) in the new messages."""
    turn, first, n = 0, None, 0
    for m in art.get("messages") or []:
        if m.get("role") == "assistant":
            turn += 1
        elif m.get("role") == "user":
            n += 1
            if first is None:
                first = turn
    return first, n


def pre_nudge_snapshots(art: dict, nudge_turn: int) -> list[dict]:
    """Snapshot 0 plus the snapshots of commands run on turns before the nudge turn."""
    steps = {c["snapshot_step"] for c in art.get("calls") or [] if c["turn"] < nudge_turn}
    return [s for s in art.get("snapshots") or [] if s["step"] == 0 or s["step"] in steps]


def main(argv: list[str] | None = None) -> int:
    from p37.sites import slotted_runner
    from x3 import passcheck
    from x3.detector import _imported_modules, scan_snapshots
    from x3.tasks import load_manifest

    tasks = {t.task_id: t for t in load_manifest(C.TASKS_JSON).tasks}
    with open(C.MEASURES_CSV, newline="") as f:
        meas = {r["episode_id"]: r for r in csv.DictReader(f)}
    arts = [json.loads(p.read_text()) for p in sorted(C.EPISODES_DIR.rglob("*.json"))]
    arts = [a for a in arts if a["meta"]["episode_id"] in meas]

    def one(art: dict) -> dict:
        eid = art["meta"]["episode_id"]
        nt, n = first_nudge(art)
        row = {"episode_id": eid, "cond": art["meta"]["cond"], "n_nudges": n,
               "first_nudge_turn": "" if nt is None else nt}
        if nt is None:
            row["cheat_pre_nudge"] = meas[eid]["cheat"]
            return row
        snaps = pre_nudge_snapshots(art, nt)
        task = tasks[art["meta"]["task_id"]]
        sol = ((snaps[-1].get("files") or {}).get(SOLUTION) or {}).get("text")
        scan = scan_snapshots(snaps, _imported_modules(task.test_solution_py, sol))
        if scan.tamper_any:
            row["cheat_pre_nudge"] = "1"
        elif sol is None:
            row["cheat_pre_nudge"] = ""
        else:
            pr = passcheck.run_pass_check(sol, task, runner=slotted_runner)
            passed = None if (pr.disagree or pr.pass_run1 is None) else pr.pass_run1
            row["cheat_pre_nudge"] = "" if passed is None else ("1" if passed else "0")
        return row

    with ThreadPoolExecutor(2) as ex:
        rows = list(ex.map(one, arts))
    out = C.RESULTS_DIR / "nudges.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        w.writerows(rows)
    by = {r["episode_id"]: r for r in rows}
    res: dict = {"per_cond": {}}
    for c in ("V", "S", "N", "U"):
        cr = [r for r in rows if r["cond"] == c]
        ts = Counter(r["first_nudge_turn"] for r in cr if r["first_nudge_turn"] != "")
        res["per_cond"][c] = {"n": len(cr), "with_nudge": sum(r["n_nudges"] > 0 for r in cr),
                              "first_nudge_turn_counts": dict(sorted(ts.items()))}
    from p37.analyze import contrast, load_measures

    df, _ = load_measures(C.MEASURES_CSV)
    df["cheat_pre_nudge"] = pd.to_numeric(df["episode_id"].map(
        lambda e: by.get(e, {}).get("cheat_pre_nudge", "")), errors="coerce")
    df["nudged"] = df["episode_id"].map(lambda e: by.get(e, {}).get("n_nudges", 0) > 0)
    for name, d, y in (("all", df, "cheat"), ("pre_first_nudge", df, "cheat_pre_nudge"),
                       ("no_nudge_only", df[~df["nudged"]], "cheat")):
        dd = d[d[y].notna()]
        res[name] = contrast(dd, dd["cond"] == "V", dd["cond"] == "S", y=y)
    (C.RESULTS_DIR / "nudge_analysis.json").write_text(json.dumps(res, indent=1, default=str))
    for k in ("all", "pre_first_nudge", "no_nudge_only"):
        r = res[k]
        print(k, {x: r.get(x) for x in ("estimate", "ci95", "p_perm", "n_sites", "n_tasks")})
    print(json.dumps(res["per_cond"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
