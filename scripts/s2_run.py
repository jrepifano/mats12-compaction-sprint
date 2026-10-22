"""S2 pilot runner (docs/sprint/specs/S2.md).

Hosts: `--hosts "desktop=ssh://<user>@<tailscale-name>,12,4;laptop=local,6,2"` (or the
SPRINT_S2_HOSTS environment variable; default: the laptop alone). Each entry is
name=target,max_containers[,max_runs]; "local" is the docker CLI's own daemon. Runs are assigned
to hosts by DeepSWE task; a run's arms always share its host.

    uv run python scripts/s2_run.py cohort
        The held-out draw, the 24-run pilot cohort, the replacements and the thresholds. $0.
    uv run python scripts/s2_run.py freeze-rules [--tasks a,b] [--pilot]
        Render and freeze each task's rule text and R1/R3 inventories into
        results/sprint/rules/<task>.{json,txt,tests.txt} (one container per task). $0.
    uv run python scripts/s2_run.py dryrun [--tasks t1,t2] [--hosts ...] [--threshold 50000]
            [--no-grade]
        $0 end to end with the scripted stub model: base run to the boundary, all four arms x 2
        in one phase, trap scoring, grading, sealing under data/raw/sprint/s2/dryrun/. Rerun to
        resume after a dropped host. Exit 3 if any host still has an emx.sandbox=sprint-s2
        container afterwards.
    uv run python scripts/s2_run.py pilot --go-from-jacob [--only ORDER,...] [--hosts ...]
        The paid pilot (SPRINT-S2 through compaction.budget, which books only from the
        designated checkout). Idempotent: rerunning it resumes failed, stopped or host-dropped
        runs (missing arms on the same host, else a new attempt). The flag records Jacob's go.
    uv run python scripts/s2_run.py haiku-peak --go-from-jacob [--cap-usd 5]
        One uncompacted Haiku run on the first pilot task, to measure its peak.
    uv run python scripts/s2_run.py gate [--labels labels.json] [--telemetry PATH]
        Tiers, coverage and per-channel Q2 yield from telemetry, the code retention precheck and
        any judge or hand labels. Reads no outcome field.
    uv run python scripts/s2_run.py hosts [--hosts ...] [--sweep-pending]
        Per host: reachable or not, our containers and snapshot images; optionally sweep the
        deferred cleanups of runs whose host has come back.

Keys come from the repo .env (never printed). SIGTERM or Ctrl-C sets a stop flag: every loop
ends at its next model call and its containers are removed before exit.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import config as C
from compaction.s2 import hosts as H
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


def _sigterm(*_a) -> None:
    LOOP.STOP.set()
    raise SystemExit(143)


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


def make_pool(a) -> H.HostPool:
    hosts = H.parse_hosts(a.hosts) if getattr(a, "hosts", None) else H.default_hosts()
    H.check_unambiguous(hosts)
    return H.HostPool(hosts, stop=LOOP.STOP)


def report(out: P.CohortOutcome, pool: H.HostPool) -> int:
    print(json.dumps({"assignment": out.assignment, "actions": out.actions,
                      "swept": out.swept, "hosts_down": pool.reasons}, indent=1))
    rc = 0
    for r in out.results:
        t = r.result or {}
        print(json.dumps({"cohort": r.key, "host": r.host, "status": r.status,
                          "state": t.get("state"), "run_key": t.get("run_key"),
                          "phase": t.get("phase"), "tier": t.get("tier"),
                          "arms": [f"{x['arm']}-{x['rep']}:{x['end']}"
                                   for x in t.get("arms", [])],
                          "cleanup_deferred": t.get("cleanup_deferred"),
                          "cost_usd": t.get("cost_usd"), "seconds": t.get("seconds"),
                          "error": r.error}, default=str), flush=True)
        if r.status != "ok" or t.get("state") != "complete":
            rc = 2
    chk = P.check_hosts(pool)
    print(f"leftover emx.sandbox={C.PURPOSE} per host: {json.dumps(chk)}")
    if any(isinstance(v, dict) and v["containers"] for v in chk.values()):
        return 3
    return rc


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


def freeze_one(task_id: str, rows: list[dict]) -> str:
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
    return (f"{task_id}: {len(rules.test_inventory)} test files; R3 {rules.r3_globs}; "
            f"notes {rules.notes}")


def cmd_freeze_rules(a) -> int:
    rows = T.ranking()
    tasks = a.tasks.split(",") if a.tasks else []
    if a.pilot:
        tasks += [c.task for c in T.pilot_cohort(rows)]
        tasks += [r["task"] for r in T.replacements(rows)]
    for t in sorted(set(tasks)):
        print(freeze_one(t, rows), flush=True)
    return 0


def cmd_dryrun(a) -> int:
    rows = T.ranking()
    root = C.raw_dir() / "dryrun"
    pool = make_pool(a)
    route = C.ROUTES["stub"]
    jobs = []
    for i, task_id in enumerate(a.tasks.split(",") if a.tasks else DRY_TASKS):
        info = T.task_info(task_id, rank_rows=rows)
        cohort = T.CohortRun(i, task_id, info.rank or 0, "stub", DRY_CHANNELS[i % 2], "dryrun")
        plan = P.RunPlan(cohort, info, a.threshold, root, base_max_calls=60,
                         policy=a.policy or DRY_POLICIES[i % len(DRY_POLICIES)])
        jobs.append(P.CohortJob(plan, route, stubs.factory(task_id)))
    out = P.run_cohort(jobs, pool, results_root=root / "results",
                       arm_concurrency=a.arm_concurrency, do_grade=not a.no_grade,
                       release_name="dryrun")
    return report(out, pool)


def _paid_factory(route: C.Route):
    shared = OpenAICompatModel(route, SprintSpend())
    return lambda _phase, _rep: shared


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
    pool = make_pool(a)
    jobs = []
    for c in cohort:
        route = C.ROUTES[c.model]
        plan = P.RunPlan(c, T.task_info(c.task, rank_rows=rows), C.threshold_for(c.model),
                         C.raw_dir() / "pilot", run_cap_usd=C.RUN_CAP_USD[c.model])
        jobs.append(P.CohortJob(plan, route, _paid_factory(route)))
    out = P.run_cohort(jobs, pool, results_root=C.REPO_ROOT / "results",
                       arm_concurrency=a.arm_concurrency)
    return report(out, pool)


def cmd_haiku_peak(a) -> int:
    if not _require_go(a):
        return 2
    rows = T.ranking()
    first = T.pilot_cohort(rows)[0]
    c = T.CohortRun(99, first.task, first.rank, "haiku", "agents_md", "haiku_peak")
    if not _check_frozen([c.task]):
        return 2
    pool = make_pool(a)
    route = C.ROUTES["haiku"]
    plan = P.RunPlan(c, T.task_info(c.task, rank_rows=rows), 10**9, C.raw_dir() / "pilot",
                     run_cap_usd=a.cap_usd)
    out = P.run_cohort([P.CohortJob(plan, route, _paid_factory(route))], pool,
                       results_root=C.REPO_ROOT / "results")
    return report(out, pool)


def cmd_gate(a) -> int:
    from compaction.s2 import gate
    path = Path(a.telemetry or P.s2_dir(C.REPO_ROOT / "results") / "telemetry.jsonl")
    tele = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    labels = json.loads(Path(a.labels).read_text()) if a.labels else {}
    print(json.dumps(gate.evaluate(tele, labels), indent=1))
    return 0


def cmd_hosts(a) -> int:
    pool = make_pool(a)
    for h in pool.hosts:
        pool.check(h.name)
    swept = (P.process_pending_sweeps(pool, Path(a.results_root), set())
             if a.sweep_pending else [])
    rep = P.check_hosts(pool)
    print(json.dumps({"hosts": [h.describe() | {"up": not pool.is_down(h.name)}
                                for h in pool.hosts], "ours": rep, "swept": swept}, indent=1))
    return 3 if any(isinstance(v, dict) and v["containers"] for v in rep.values()) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cohort")
    f = sub.add_parser("freeze-rules")
    f.add_argument("--tasks")
    f.add_argument("--pilot", action="store_true", help="the 6 pilot and 4 replacement tasks")
    d = sub.add_parser("dryrun")
    d.add_argument("--tasks")
    d.add_argument("--hosts")
    d.add_argument("--policy", choices=("pi", "cc_prompt", "codex_local"))
    d.add_argument("--threshold", type=int, default=50_000)
    d.add_argument("--arm-concurrency", type=int, default=4)
    d.add_argument("--no-grade", action="store_true")
    for name in ("pilot", "haiku-peak"):
        p = sub.add_parser(name)
        p.add_argument("--go-from-jacob", action="store_true")
        p.add_argument("--hosts")
        p.add_argument("--arm-concurrency", type=int, default=4)
        if name == "pilot":
            p.add_argument("--only")
        else:
            p.add_argument("--cap-usd", type=float, default=5.0)
    g = sub.add_parser("gate")
    g.add_argument("--labels")
    g.add_argument("--telemetry")
    h = sub.add_parser("hosts")
    h.add_argument("--hosts")
    h.add_argument("--sweep-pending", action="store_true")
    h.add_argument("--results-root", default=str(C.REPO_ROOT / "results"))
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _sigterm)
    return {"cohort": cmd_cohort, "freeze-rules": cmd_freeze_rules, "dryrun": cmd_dryrun,
            "pilot": cmd_pilot, "haiku-peak": cmd_haiku_peak, "gate": cmd_gate,
            "hosts": cmd_hosts}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
