"""S2 pilot runner (docs/sprint/specs/S2.md).

    uv run python scripts/s2_run.py cohort
        Print the held-out draw, the 24-run pilot cohort (order, task, model, channel) and the
        replacement tasks. $0, no Docker.
    uv run python scripts/s2_run.py dryrun [--tasks koota-pair-relation-tracking,...]
            [--policy pi|cc_prompt|codex_local] [--threshold 50000] [--no-grade]
        $0 end to end with the scripted stub model on the local Docker (or DOCKER_HOST): base run
        to the boundary, fork, all four arms x 2, trap scoring, grading, sealing under
        data/raw/sprint/s2/dryrun/. Ends by checking that no emx.sandbox=sprint-s2 container is
        left; exit 3 if one is.
    uv run python scripts/s2_run.py pilot --go-from-jacob [--only ORDER,...] [--concurrency 4]
        The paid pilot (SPRINT-S2 via compaction.budget; refuses outside the designated
        checkout). Needs Jacob's go; the flag is the record of it.
    uv run python scripts/s2_run.py containers
        List leftover emx.sandbox=sprint-s2 containers.

Keys are read from the repo .env (never printed). Every container carries
emx.sandbox=sprint-s2 and its run's emx.run=<id>; each run's cleanup removes only its own.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from concurrent.futures import ThreadPoolExecutor

from compaction.s2 import config as C
from compaction.s2 import pilot as P
from compaction.s2 import stubs
from compaction.s2 import tasks as T
from compaction.s2.model import OpenAICompatModel, SprintSpend

DRY_TASKS = ("koota-pair-relation-tracking", "wasmi-trap-coredumps")
DRY_CHANNELS = ("chat", "readme")
DRY_POLICIES = ("pi", "cc_prompt")


def _sigterm(*_a) -> None:
    raise SystemExit(143)  # unwinds through finally blocks, so every run's cleanup runs


def load_env() -> None:
    p = C.REPO_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip().removeprefix("export "), v.strip().strip("'\""))


def cmd_cohort(_a) -> int:
    rows = T.ranking()
    print("held-out (S5 only):")
    for r in T.heldout(rows):
        print(f"  rank {r['rank']:2d}  {r['task']}")
    print("pilot cohort:")
    for c in T.pilot_cohort(rows):
        print(f"  {c.order:2d}  rank {c.rank:2d}  {c.task:52s} {c.model:9s} {c.channel}")
    print("replacements:", ", ".join(f"{r['rank']}:{r['task']}" for r in T.replacements(rows)))
    print("thresholds:", {m: C.threshold_for(m) for m in C.PILOT_MODELS})
    return 0


def assumed_labels(spec: str) -> dict:
    lost = {r.strip() for r in spec.split(",") if r.strip()}
    return {r: ("LOST" if r in lost else "KEPT") for r in ("R1", "R3", "R6", "C")}


def cmd_dryrun(a) -> int:
    """Task 1 runs all four arms in one phase (labels given up front); task 2 runs the two-phase
    flow (F and S, boundary image kept; then S+R and S+I resumed with labels)."""
    rows = T.ranking()
    root = C.raw_dir() / "dryrun"
    results_root = root / "results"
    tasks = a.tasks.split(",") if a.tasks else list(DRY_TASKS)
    route = C.ROUTES["stub"]
    labels = assumed_labels(a.assume_lost)
    plans = []
    for i, task_id in enumerate(tasks):
        info = T.task_info(task_id, rank_rows=rows)
        policy = a.policy or DRY_POLICIES[i % len(DRY_POLICIES)]
        cohort = T.CohortRun(i, task_id, info.rank or 0, "stub", DRY_CHANNELS[i % 2], "dryrun")
        plans.append((i, P.RunPlan(cohort, info, a.threshold, root / cohort.run_key(),
                                   policy=policy, base_max_calls=60)))

    def go(item):
        i, plan = item
        fac = stubs.factory(plan.task.task_id)
        two_phase = i % 2 == 1
        tele = P.run_one(plan, route, fac, results_root=results_root,
                         arm_concurrency=a.arm_concurrency, do_grade=not a.no_grade,
                         labels=None if two_phase else labels, keep_boundary=two_phase,
                         release_name="dryrun")
        out = [tele]
        if two_phase and tele.get("kept_boundary_image"):
            out.append(P.resume_arms(plan.out_dir, route, fac, results_root=results_root,
                                     labels=labels, arm_concurrency=a.arm_concurrency,
                                     do_grade=not a.no_grade, release_name="dryrun"))
        return out

    rc = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for f in [ex.submit(go, p) for p in plans]:
            try:
                for tele in f.result():
                    print(json.dumps({k: tele.get(k) for k in (
                        "phase", "run_key", "tier", "base", "boundary", "arms_planned",
                        "lost_used", "kept_boundary_image", "cleanup_unconfirmed", "seconds")},
                        default=str))
            except Exception as e:  # noqa: BLE001
                print(f"RUN FAILED: {type(e).__name__}: {e}")
                rc = 2
    left = P.our_containers()
    print(f"leftover emx.sandbox={C.PURPOSE} containers: {left}")
    if left is None or left:
        return 3
    return rc


def cmd_pilot(a) -> int:
    if not a.go_from_jacob:
        print("refusing: the paid pilot needs Jacob's go (pass --go-from-jacob)")
        return 2
    load_env()
    rows = T.ranking()
    cohort = T.pilot_cohort(rows)
    if a.only:
        keep = {int(x) for x in a.only.split(",")}
        cohort = [c for c in cohort if c.order in keep]
    spend = SprintSpend()
    root = C.raw_dir() / "pilot"
    results_root = C.REPO_ROOT / "results"

    def go(c: T.CohortRun):
        route = C.ROUTES[c.model]
        info = T.task_info(c.task, rank_rows=rows)
        plan = P.RunPlan(c, info, C.threshold_for(c.model), root / c.run_key(),
                         run_cap_usd=C.RUN_CAP_USD[c.model])
        shared = OpenAICompatModel(route, spend)

        def factory(_phase: str, _rep: int):
            return shared
        return P.run_one(plan, route, factory, results_root=results_root,
                         arm_concurrency=a.arm_concurrency)

    rc = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for c, f in [(c, ex.submit(go, c)) for c in cohort]:
            try:
                tele = f.result()
                print(json.dumps({k: tele.get(k) for k in ("run_key", "tier", "cost_usd",
                                                           "seconds")}, default=str))
            except Exception as e:  # noqa: BLE001
                print(f"RUN FAILED {c.run_key()}: {type(e).__name__}: {e}")
                rc = 2
    left = P.our_containers()
    print(f"leftover emx.sandbox={C.PURPOSE} containers: {left}")
    return 3 if (left is None or left) else rc


def cmd_containers(_a) -> int:
    left = P.our_containers()
    print(json.dumps({"label": f"emx.sandbox={C.PURPOSE}", "containers": left}))
    return 0 if left == [] else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cohort")
    d = sub.add_parser("dryrun")
    d.add_argument("--tasks")
    d.add_argument("--policy", choices=("pi", "cc_prompt", "codex_local"))
    d.add_argument("--threshold", type=int, default=50_000)
    d.add_argument("--concurrency", type=int, default=2)
    d.add_argument("--arm-concurrency", type=int, default=4)
    d.add_argument("--no-grade", action="store_true")
    d.add_argument("--assume-lost", default="R6,C",
                   help="stand-in retention labels for the dry run (rules marked LOST)")
    p = sub.add_parser("pilot")
    p.add_argument("--go-from-jacob", action="store_true")
    p.add_argument("--only")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--arm-concurrency", type=int, default=4)
    sub.add_parser("containers")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _sigterm)
    return {"cohort": cmd_cohort, "dryrun": cmd_dryrun, "pilot": cmd_pilot,
            "containers": cmd_containers}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
