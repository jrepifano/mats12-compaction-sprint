"""S2 pilot runner (docs/sprint/specs/S2.md).

    uv run python scripts/s2_run.py cohort
        The held-out draw, the 24-run pilot cohort (order, task, model, channel), the
        replacement tasks and the thresholds. $0, no Docker.
    uv run python scripts/s2_run.py freeze-rules [--tasks a,b] [--pilot]
        Start each task image once, list its tracked files, render the frozen rule text and the
        R1/R3 inventories, and write results/sprint/rules/<task>.{json,txt,tests.txt}. $0.
    uv run python scripts/s2_run.py dryrun [--tasks t1,t2] [--policy pi|cc_prompt|codex_local]
            [--threshold 50000] [--assume-lost R6,C] [--no-grade]
        $0 end to end with the scripted stub model on the local Docker (or DOCKER_HOST): base run
        to the boundary, fork, all four arms x 2 (task 1 in one phase with stand-in labels, task
        2 in two phases with a kept boundary image), trap scoring, grading, sealing under
        data/raw/sprint/s2/dryrun/. Ends by checking that no emx.sandbox=sprint-s2 container is
        left (exit 3 if one is).
    uv run python scripts/s2_run.py pilot --go-from-jacob [--only ORDER,...] [--concurrency 4]
        The paid pilot, phase 1 (base runs, F and S arms; boundary images kept for S+R/S+I).
        Books SPRINT-S2 through compaction.budget, which refuses outside the designated
        checkout. Refuses tasks whose rules are not frozen. The flag is the record of Jacob's go.
    uv run python scripts/s2_run.py pilot-resume --go-from-jacob --labels labels.json
        Phase 2: S+R and S+I from each kept boundary with its retention labels
        ({run_key: {"R1": "LOST"|"KEPT"|"UNKNOWN", ...}}); removes each kept image after.
    uv run python scripts/s2_run.py haiku-peak --go-from-jacob [--cap-usd 8]
        One uncompacted Haiku run on the first pilot task, to measure its peak (sprint plan S2).
    uv run python scripts/s2_run.py containers
        List leftover emx.sandbox=sprint-s2 containers.

Keys come from the repo .env (never printed). Every container carries emx.sandbox=sprint-s2 and
its run's emx.run=<id>; each run's cleanup removes only its own. SIGTERM or Ctrl-C sets a stop
flag: every loop ends at its next model call and its containers are removed before exit.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import config as C
from compaction.s2 import loop as LOOP
from compaction.s2 import pilot as P
from compaction.s2 import rules as R
from compaction.s2 import stubs
from compaction.s2 import tasks as T
from compaction.s2.env import NOSHIM, line_counts
from compaction.s2.model import OpenAICompatModel, SprintSpend

DRY_TASKS = ("koota-pair-relation-tracking", "wasmi-trap-coredumps")
DRY_CHANNELS = ("chat", "readme")
DRY_POLICIES = ("pi", "cc_prompt")
TELE_SHOW = ("phase", "run_key", "tier", "base", "boundary", "arms_planned", "lost_used",
             "kept_boundary_image", "cleanup_unconfirmed", "cost_usd", "seconds")


def _sigterm(*_a) -> None:
    LOOP.STOP.set()
    raise SystemExit(143)


def run_pool(fn, items, workers: int) -> int:
    """Run fn over items in threads, printing each result's telemetry; on Ctrl-C or SIGTERM set
    the stop flag so workers wind down and clean up before the pool exits. Returns 2 if any run
    failed, else 0."""
    rc = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [(it, ex.submit(fn, it)) for it in items]
        try:
            for it, f in futs:
                try:
                    res = f.result()
                    for tele in res if isinstance(res, list) else [res]:
                        print(json.dumps({k: tele.get(k) for k in TELE_SHOW}, default=str),
                              flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"RUN FAILED {str(it)[:120]}: {type(e).__name__}: {e}", flush=True)
                    rc = 2
        except BaseException:
            LOOP.STOP.set()
            raise
    return rc


def leftovers_rc(rc: int) -> int:
    left = P.our_containers()
    print(f"leftover emx.sandbox={C.PURPOSE} containers: {left}")
    return 3 if (left is None or left) else rc


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


def freeze_one(task_id: str, rows: list[dict]) -> dict:
    info = T.task_info(task_id, rank_rows=rows)
    with (Invocation(C.PURPOSE + "-freeze") as inv,
          inv.start(info.image, platform=C.PLATFORM or None, network="none",
                    lifetime_s=900) as sb):
        r = sb.exec(NOSHIM + "git ls-files", timeout_s=120, workdir=C.WORKSPACE)
    if r.rc != 0:
        raise RuntimeError(f"git ls-files failed in {task_id}: {r.stderr[-300:]}")
    tracked = [t for t in r.stdout.split("\n") if t]
    sol = Path(info.task_dir) / "solution" / "solution.patch"
    counts = line_counts(sol.read_text(errors="replace")) if sol.exists() else {}
    rules = R.render(task_id, info.language, tracked, info.solution_files, counts)
    d = C.rules_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.json").write_text(json.dumps(rules.to_dict(), indent=1) + "\n")
    (d / f"{task_id}.txt").write_text(rules.text + "\n")
    (d / f"{task_id}.tests.txt").write_text("\n".join(rules.test_inventory) + "\n")
    return {"phase": "freeze", "run_key": task_id,
            "tier": f"{len(rules.test_inventory)} test files; R3 {rules.r3_globs}; "
                    f"notes {rules.notes}"}


def cmd_freeze_rules(a) -> int:
    rows = T.ranking()
    tasks = a.tasks.split(",") if a.tasks else []
    if a.pilot:
        tasks += [c.task for c in T.pilot_cohort(rows)]
        tasks += [r["task"] for r in T.replacements(rows)]
    return leftovers_rc(run_pool(lambda t: freeze_one(t, rows), sorted(set(tasks)), 2))


def assumed_labels(spec: str) -> dict:
    lost = {r.strip() for r in spec.split(",") if r.strip()}
    return {r: ("LOST" if r in lost else "KEPT") for r in R.RULE_IDS}


def cmd_dryrun(a) -> int:
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
        if two_phase and tele.get("kept_boundary_image") and not LOOP.STOP.is_set():
            out.append(P.resume_arms(plan.out_dir, route, fac, results_root=results_root,
                                     labels=labels, arm_concurrency=a.arm_concurrency,
                                     do_grade=not a.no_grade, release_name="dryrun"))
        return out

    return leftovers_rc(run_pool(go, plans, a.concurrency))


def _paid_model_factory(route: C.Route):
    shared = OpenAICompatModel(route, SprintSpend())

    def factory(_phase: str, _rep: int):
        return shared
    return factory


def _require_go(a) -> bool:
    if not a.go_from_jacob:
        print("refusing: paid runs need Jacob's go (pass --go-from-jacob)")
        return False
    load_env()
    return True


def _check_frozen(tasks: list[str]) -> bool:
    missing = [t for t in tasks if not (C.rules_dir() / f"{t}.json").exists()]
    if missing:
        print(f"refusing: rules not frozen for {missing} (run freeze-rules --pilot first)")
    return not missing


def cmd_pilot(a) -> int:
    if not _require_go(a):
        return 2
    rows = T.ranking()
    cohort = T.pilot_cohort(rows)
    if a.only:
        keep = {int(x) for x in a.only.split(",")}
        cohort = [c for c in cohort if c.order in keep]
    if not _check_frozen(sorted({c.task for c in cohort})):
        return 2
    root = C.raw_dir() / "pilot"

    def go(c: T.CohortRun):
        route = C.ROUTES[c.model]
        plan = P.RunPlan(c, T.task_info(c.task, rank_rows=rows), C.threshold_for(c.model),
                         root / c.run_key(), run_cap_usd=C.RUN_CAP_USD[c.model])
        return P.run_one(plan, route, _paid_model_factory(route),
                         results_root=C.REPO_ROOT / "results",
                         arm_concurrency=a.arm_concurrency, keep_boundary=True)

    return leftovers_rc(run_pool(go, cohort, a.concurrency))


def cmd_pilot_resume(a) -> int:
    if not _require_go(a):
        return 2
    labels = json.loads(Path(a.labels).read_text())
    root = C.raw_dir() / "pilot"

    def go(run_key: str):
        run = json.loads((root / run_key / "run.json").read_text())
        route = C.ROUTES[run["plan"]["model"]]
        return P.resume_arms(root / run_key, route, _paid_model_factory(route),
                             results_root=C.REPO_ROOT / "results", labels=labels[run_key],
                             arm_concurrency=a.arm_concurrency)

    return leftovers_rc(run_pool(go, sorted(labels), a.concurrency))


def cmd_haiku_peak(a) -> int:
    if not _require_go(a):
        return 2
    rows = T.ranking()
    first = T.pilot_cohort(rows)[0]
    c = T.CohortRun(99, first.task, first.rank, "haiku", "agents_md", "haiku_peak")
    if not _check_frozen([c.task]):
        return 2
    route = C.ROUTES["haiku"]
    plan = P.RunPlan(c, T.task_info(c.task, rank_rows=rows), 10**9,
                     C.raw_dir() / "pilot" / c.run_key(), run_cap_usd=a.cap_usd)
    rc = run_pool(lambda p: P.run_one(p, route, _paid_model_factory(route),
                                      results_root=C.REPO_ROOT / "results", arms=()),
                  [plan], 1)
    return leftovers_rc(rc)


def cmd_containers(_a) -> int:
    left = P.our_containers()
    print(json.dumps({"label": f"emx.sandbox={C.PURPOSE}", "containers": left}))
    return 0 if left == [] else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cohort")
    f = sub.add_parser("freeze-rules")
    f.add_argument("--tasks")
    f.add_argument("--pilot", action="store_true", help="the 6 pilot and 4 replacement tasks")
    d = sub.add_parser("dryrun")
    d.add_argument("--tasks")
    d.add_argument("--policy", choices=("pi", "cc_prompt", "codex_local"))
    d.add_argument("--threshold", type=int, default=50_000)
    d.add_argument("--concurrency", type=int, default=2)
    d.add_argument("--arm-concurrency", type=int, default=4)
    d.add_argument("--no-grade", action="store_true")
    d.add_argument("--assume-lost", default="R6,C",
                   help="stand-in retention labels for the dry run (rules marked LOST)")
    for name in ("pilot", "pilot-resume", "haiku-peak"):
        p = sub.add_parser(name)
        p.add_argument("--go-from-jacob", action="store_true")
        p.add_argument("--concurrency", type=int, default=4)
        p.add_argument("--arm-concurrency", type=int, default=4)
        if name == "pilot":
            p.add_argument("--only")
        if name == "pilot-resume":
            p.add_argument("--labels", required=True)
        if name == "haiku-peak":
            p.add_argument("--cap-usd", type=float, default=8.0)
    sub.add_parser("containers")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _sigterm)
    return {"cohort": cmd_cohort, "freeze-rules": cmd_freeze_rules, "dryrun": cmd_dryrun,
            "pilot": cmd_pilot, "pilot-resume": cmd_pilot_resume, "haiku-peak": cmd_haiku_peak,
            "containers": cmd_containers}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
