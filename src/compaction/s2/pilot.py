"""Orchestration of S2 runs across Docker hosts: base trajectory, boundary, all four arms,
scoring, sealing, and resumption.

One phase per run (Jacob, 22 Oct): F, S, S+R and S+I fork at every boundary together. S+R
restores the full frozen rule block and S+I adds one matched irrelevant fact per rule; whether a
rule was lost is decided afterwards and only conditions the analysis.

Layout per cohort run (cohort key = <kind>-<rank>-<task>-<model>-<channel>):
    <root>/<cohort key>/status.json      state, attempt, host, run id, boundary tag, arms done
    <root>/<cohort key>/a<attempt>/...   run.json, base/, boundary/, arms/<slug>-<rep>/
The run key is "<cohort key>-a<attempt>". Every record carries its host.

Each arm's outcome row is sealed as soon as it is scored (outcome-<run key>-<slug>-<rep>.json),
and arms/<slug>-<rep>/scored.json marks it done (no outcome field in it). A run whose host drops
ends `failed_host`: its loops stop at the next model call (the host's down event), its cleanup
is deferred to pending_sweeps.jsonl, and `run_cohort` later resumes it: the missing arms on the
same host if that host is back and still holds the boundary image, otherwise a new attempt.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe.sandbox import SandboxError, sweep, using_host
from compaction.s2 import config as C
from compaction.s2 import followups as FU
from compaction.s2 import hosts as H
from compaction.s2 import outcomes as O
from compaction.s2 import rules as R
from compaction.s2.loop import RunConfig, arm_slug, run_arm, run_base
from compaction.s2.model import ChatModel, RunSpend
from compaction.s2.tasks import CohortRun, TaskInfo

ModelFactory = Callable[[str, int], ChatModel]  # (phase "base" or arm name, rep) -> model
_LOCK = threading.Lock()
TELE_KEYS_ARM = ("arm", "rep", "end", "n_calls", "cost_usd", "closing_sent", "would_recompact",
                 "error", "cap", "seconds")
ALL_ARMS = [(a, r) for a in C.ARMS for r in range(1, C.REPS + 1)]


@dataclass
class RunPlan:
    cohort: CohortRun
    task: TaskInfo
    threshold: int
    root: Path  # the stage's raw root; the run lives under root/<cohort key>/a<attempt>
    policy: str | None = None
    run_cap_usd: float = 0.0
    base_max_calls: int = C.BASE_MAX_CALLS
    followup_distribution: list[tuple[int, int]] | None = None
    followup_source: dict | None = None  # where the follow-up volume came from (recorded)
    image: str | None = None
    attempt: int = 1

    @property
    def cohort_key(self) -> str:
        return self.cohort.run_key()

    @property
    def cohort_dir(self) -> Path:
        return Path(self.root) / self.cohort_key

    @property
    def run_key(self) -> str:
        return f"{self.cohort_key}-a{self.attempt}"

    @property
    def out_dir(self) -> Path:
        return self.cohort_dir / f"a{self.attempt}"


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def s2_dir(results_root: Path) -> Path:
    return Path(results_root) / "sprint" / C.SEAL_STAGE


def read_status(cohort_dir: Path) -> dict | None:
    p = Path(cohort_dir) / "status.json"
    return json.loads(p.read_text()) if p.exists() else None


def write_status(cohort_dir: Path, **upd) -> dict:
    with _LOCK:
        st = read_status(cohort_dir) or {"history": []}
        if "state" in upd and upd["state"] != st.get("state"):
            st["history"].append({"ts": utc(), "state": upd["state"],
                                  "attempt": upd.get("attempt", st.get("attempt")),
                                  "host": upd.get("host", st.get("host"))})
        st.update(upd, updated=utc())
        _write(Path(cohort_dir) / "status.json", st)
        return st


def mark_arm_done(cohort_dir: Path, slug_rep: str) -> None:
    with _LOCK:
        st = read_status(cohort_dir) or {"history": []}
        st["arms_done"] = sorted(set(st.get("arms_done") or []) | {slug_rep})
        _write(Path(cohort_dir) / "status.json", st)


def our_containers(label_value: str = C.PURPOSE, host: H.Host | None = None) -> list[str] | None:
    """Names of containers labelled emx.sandbox=<label_value> on `host` (default: the CLI's
    own daemon); None if the query failed."""
    r = H.list_ours(host or H.Host("default", None), label_value)
    return None if r is None else r["containers"]


def _run_arms(cfg: RunConfig, plan: RunPlan, todo: list[tuple[str, int]],
              model_factory: ModelFactory, inv, rules: R.TaskRules, boundary: dict,
              pool: H.HostPool, host: H.Host, results_root: Path, release_name: str,
              arm_concurrency: int, do_grade: bool) -> list:
    lost = list(R.RULE_IDS)  # S+R restores every rule; loss conditions the analysis only

    def one(ar):
        a, r = ar
        res = run_arm(cfg, a, r, model_factory(a, r), inv, lost=lost, rules=rules,
                      boundary=boundary)
        if res.traps_home and not res.error:
            row = O.arm_outcome(res, plan.task, rules, cfg.channel, inv.run_id,
                                do_grade=do_grade, host_name=host.name,
                                docker_host=host.docker_host, inv=inv)
            O.seal_arm(plan.run_key, arm_slug(a), r, row,
                       root=Path(results_root) / "sprint", release_name=release_name)
            _write(plan.out_dir / "arms" / f"{arm_slug(a)}-{r}" / "scored.json",
                   {"ts": utc(), "host": host.name, "sealed": True})
            mark_arm_done(plan.cohort_dir, f"{arm_slug(a)}-{r}")
        return res

    with ThreadPoolExecutor(max_workers=max(1, arm_concurrency)) as ex:
        return list(ex.map(one, todo))


def _finish(tele: dict, plan: RunPlan, inv, pool: H.HostPool, host: H.Host,
            results_root: Path, state: str) -> None:
    """Clean up the run on its host, or defer the cleanup when the host is down. A run that
    hit errors probes its host first, so a dead daemon isn't retried for minutes."""
    if tele.get("errors") and not pool.is_down(host.name):
        pool.check(host.name)
    if pool.is_down(host.name):
        append_jsonl(s2_dir(results_root) / "pending_sweeps.jsonl",
                     {"ts": utc(), "run_key": plan.run_key, "run_id": inv.run_id,
                      "host": host.name, "docker_host": host.docker_host})
        if hasattr(inv, "release_slots"):
            inv.release_slots()  # local slots go back now; the remote sweep waits for the host
        tele["cleanup_deferred"] = True
        tele["cleanup_unconfirmed"] = []
        state = "failed_host"
    else:
        bad = inv.cleanup()
        tele["cleanup_unconfirmed"] = bad
        if bad:
            state = "failed"
            tele.setdefault("errors", []).append(f"cleanup not confirmed: {bad}")
    tele["state"] = state
    upd = {"state": state}
    if state == "run_cap":
        done = set((read_status(plan.cohort_dir) or {}).get("arms_done") or [])
        upd["arms_incomplete"] = [f"{arm_slug(a)}-{r}" for a, r in ALL_ARMS
                                  if f"{arm_slug(a)}-{r}" not in done]
        tele["arms_incomplete"] = upd["arms_incomplete"]
    write_status(plan.cohort_dir, **upd)


def _state_from(tele: dict) -> str:
    """A run-cap stop anywhere is terminal (`run_cap`, review r3 #1): never `failed`, never
    restarted automatically; its unrun arms are recorded as incomplete."""
    ends = [(tele.get("base") or {}).get("end")] + [a["end"] for a in tele.get("arms", [])]
    if "run_cap" in ends:
        return "run_cap"
    if "stopped" in ends:
        return "stopped"
    return "failed" if tele.get("errors") else "complete"


def run_one(plan: RunPlan, route: C.Route, model_factory: ModelFactory, *, results_root: Path,
            pool: H.HostPool, host_name: str, arm_concurrency: int = 4, do_grade: bool = True,
            arms: list[tuple[str, int]] | None = None, release_name: str = "main",
            spend: RunSpend | None = None) -> dict:
    """A fresh attempt of one cohort run on `host_name`: base, boundary, every arm."""
    t0 = time.monotonic()
    host = pool.host(host_name)
    inv = pool.invocation(host_name, C.PURPOSE)
    key = plan.run_key
    tele: dict = {"ts": utc(), "phase": "base", "run_key": key, "cohort_key": plan.cohort_key,
                  "attempt": plan.attempt, "task": plan.task.task_id, "rank": plan.task.rank,
                  "model": route.name, "channel": plan.cohort.channel, "kind": plan.cohort.kind,
                  "threshold": plan.threshold, "run_id": inv.run_id, "host": host.describe(),
                  "errors": []}
    script = FU.for_task(plan.task.task_id, plan.followup_distribution)
    cfg = RunConfig(key, plan.task, route, plan.cohort.channel, plan.threshold, script,
                    plan.out_dir, policy=plan.policy or "", base_max_calls=plan.base_max_calls,
                    run_cap_usd=plan.run_cap_usd, stop_event=pool.down[host_name])
    tele["policy"] = cfg.policy
    tele["followups"] = {"level": script.level, "turns": script.target_turns,
                         "tokens": script.target_tokens, "source": plan.followup_source}
    _write(plan.out_dir / "run.json", {
        "plan": {**plan.cohort.__dict__, "threshold": plan.threshold, "policy": cfg.policy,
                 "route": route.__dict__, "base_max_calls": plan.base_max_calls,
                 "run_cap_usd": plan.run_cap_usd},
        "task": plan.task.to_dict(), "script": script.to_dict(), "started": utc(),
        "run_id": inv.run_id, "run_key": key, "cohort_key": plan.cohort_key,
        "attempt": plan.attempt, "host": host.describe(),
        "followup_source": plan.followup_source})
    write_status(plan.cohort_dir, state="running", attempt=plan.attempt, run_key=key,
                 host=host_name, run_id=inv.run_id, boundary_tag=None, arms_done=[])
    if spend is not None:
        spend.attach(plan.out_dir / "spend.jsonl")  # durable per-attempt books (review r2 #2)
        tele["spend_recovered"] = spend.recovered
    state = "failed"
    try:
        H.ensure_image(host, plan.image or plan.task.image)
        base = run_base(cfg, model_factory("base", 0), inv, image=plan.image)
        tele["base"] = {k: getattr(base, k) for k in (
            "end", "n_calls", "cost_usd", "ctx_tokens", "followups_delivered",
            "followup_tokens_delivered", "error", "cap", "seconds")}
        if base.error:
            tele["errors"].append(f"base: {base.error[-300:]}")
        tele["tier"] = "attempted"
        if base.boundary:
            b = base.boundary
            tele["tier"] = "reached_compaction"
            tele["boundary"] = {k: b[k] for k in (
                "pre_tokens", "post_tokens_est", "summary_chars", "precheck", "lost_certain",
                "compaction_cost_usd", "snapshot_s", "stopped_processes")}
            write_status(plan.cohort_dir, state="base_done", boundary_tag=b["tag"])
            # The gate must see this boundary even if the process dies during the arms
            # (review r3 #2): the base row goes to the telemetry now; the final row, written
            # after the arms, supersedes it (gate.latest_attempts keeps the last).
            append_jsonl(s2_dir(results_root) / "telemetry.jsonl",
                         {**tele, "pre_arms": True, "state": "base_done"})
            rules = R.TaskRules(**{k: v for k, v in base.rules.items()
                                   if k in R.TaskRules.__dataclass_fields__})
            todo = arms if arms is not None else ALL_ARMS
            res = _run_arms(cfg, plan, todo, model_factory, inv, rules, b, pool, host,
                            results_root, release_name, arm_concurrency, do_grade)
            tele["arms"] = [{k: getattr(x, k) for k in TELE_KEYS_ARM} for x in res]
            tele["errors"] += [f"arm {x.arm}-{x.rep}: {x.error[-300:]}" for x in res if x.error]
        state = _state_from(tele)
    except Exception as e:  # noqa: BLE001 - recorded; classified by the pool
        tele["errors"].append(f"{type(e).__name__}: {str(e)[-500:]}")
        state = "failed"
    finally:
        _finish(tele, plan, inv, pool, host, results_root, state)
        tele["seconds"] = round(time.monotonic() - t0, 3)
        _cost(tele, spend, plan)
        append_jsonl(s2_dir(results_root) / "telemetry.jsonl", tele)
    return tele


def _cost(tele: dict, spend: RunSpend | None, plan: RunPlan) -> None:
    """The run's spend: the run-level books (every attempt, failed ones included; for a resumed
    attempt, everything its ledger holds) when the run has them; else the loops' own sums.
    Copied into status.json for reading only: the books themselves live in the attempt's
    append-only spend.jsonl."""
    loops = round((tele.get("base") or {}).get("cost_usd", 0.0) + sum(
        a["cost_usd"] for a in tele.get("arms", [])), 6)
    tele["cost_usd_loops"] = loops
    tele["cost_usd"] = round(spend.spent, 6) if spend is not None else loops
    if spend is not None:
        tele["run_cap_usd"] = spend.cap_usd
        write_status(plan.cohort_dir, spent_usd=round(spend.spent, 6))


def resume_arms(cohort_dir: Path, route: C.Route, model_factory: ModelFactory, *,
                results_root: Path, pool: H.HostPool, arm_concurrency: int = 4,
                do_grade: bool = True, release_name: str = "main",
                spend: RunSpend | None = None) -> dict:
    """Run the arms a run is missing, from its boundary image, on the host that holds it (the
    run's own host: a run's arms never span hosts), under the run's own run id, whose cleanup
    then removes the image."""
    t0 = time.monotonic()
    st = read_status(cohort_dir)
    run_dir = Path(cohort_dir) / f"a{st['attempt']}"
    run = json.loads((run_dir / "run.json").read_text())
    host_name = st["host"]
    if run["host"]["name"] != host_name:
        raise ValueError(f"{cohort_dir}: status host {host_name} != run host {run['host']}")
    host = pool.host(host_name)
    b = json.loads((run_dir / "boundary" / "boundary.json").read_text())
    rd = json.loads((run_dir / "rules.json").read_text())
    rules = R.TaskRules(**{k: v for k, v in rd.items() if k in R.TaskRules.__dataclass_fields__})
    task = TaskInfo(**run["task"])
    pl = run["plan"]
    script = FU.Script(**{**run["script"], "turns": [FU.FollowUp(**t)
                                                     for t in run["script"]["turns"]]})
    cohort = CohortRun(pl["order"], pl["task"], pl["rank"], pl["model"], pl["channel"],
                       pl["kind"])
    plan = RunPlan(cohort, task, pl["threshold"], Path(cohort_dir).parent, policy=pl["policy"],
                   attempt=st["attempt"])
    cfg = RunConfig(plan.run_key, task, route, pl["channel"], pl["threshold"], script, run_dir,
                    policy=pl["policy"], stop_event=pool.down[host_name])
    inv = pool.invocation(host_name, C.PURPOSE, run_id=st["run_id"])
    inv.tags.append(b["tag"])  # so cleanup also removes the boundary image by tag
    if spend is not None:
        # The same attempt continues against the same cap: rebuild its books from the ledger,
        # including holds a crashed process left unresolved (charged at their bound).
        spend.attach(run_dir / "spend.jsonl")
    ensure_base_row(run_dir, results_root)
    done = set(st.get("arms_done") or [])
    todo = [(a, r) for a, r in ALL_ARMS if f"{arm_slug(a)}-{r}" not in done]
    tele: dict = {"ts": utc(), "phase": "resume", "run_key": plan.run_key,
                  "cohort_key": plan.cohort_key, "attempt": plan.attempt, "run_id": inv.run_id,
                  "host": host.describe(), "arms_planned": [f"{arm_slug(a)}-{r}"
                                                            for a, r in todo], "errors": [],
                  "spend_recovered": spend.recovered if spend is not None else []}
    write_status(cohort_dir, state="resuming")
    state = "failed"
    try:
        res = _run_arms(cfg, plan, todo, model_factory, inv, rules, b, pool, host,
                        results_root, release_name, arm_concurrency, do_grade)
        tele["arms"] = [{k: getattr(x, k) for k in TELE_KEYS_ARM} for x in res]
        tele["errors"] += [f"arm {x.arm}-{x.rep}: {x.error[-300:]}" for x in res if x.error]
        state = _state_from(tele)
    except Exception as e:  # noqa: BLE001
        tele["errors"].append(f"{type(e).__name__}: {str(e)[-500:]}")
    finally:
        _finish(tele, plan, inv, pool, host, results_root, state)
        tele["seconds"] = round(time.monotonic() - t0, 3)
        _cost(tele, spend, plan)
        append_jsonl(s2_dir(results_root) / "telemetry.jsonl", tele)
    return tele


def ensure_base_row(run_dir: Path, results_root: Path) -> dict | None:
    """If the telemetry has no base row for this attempt (a crash before any was written),
    rebuild it from the attempt's durable files (run.json, base/result.json,
    boundary/boundary.json) and append it, marked `reconstructed`. Returns the row added."""
    run = json.loads((run_dir / "run.json").read_text())
    key = run["run_key"]
    path = s2_dir(results_root) / "telemetry.jsonl"
    if path.exists() and any(
            (r := json.loads(x)).get("run_key") == key and r.get("phase", "base") == "base"
            for x in path.read_text().splitlines() if x.strip()):
        return None
    base = json.loads((run_dir / "base" / "result.json").read_text())
    bpath = run_dir / "boundary" / "boundary.json"
    b = json.loads(bpath.read_text()) if bpath.exists() else None
    pl = run["plan"]
    row = {"ts": utc(), "phase": "base", "reconstructed": True, "run_key": key,
           "cohort_key": run["cohort_key"], "attempt": run["attempt"], "task": run["task"]["task_id"],
           "rank": run["task"]["rank"], "model": pl["model"], "channel": pl["channel"],
           "kind": pl["kind"], "threshold": pl["threshold"], "run_id": run["run_id"],
           "host": run["host"], "policy": pl["policy"],
           "base": {k: base.get(k) for k in ("end", "n_calls", "cost_usd", "ctx_tokens",
                                             "followups_delivered",
                                             "followup_tokens_delivered", "error", "cap",
                                             "seconds")},
           "tier": "reached_compaction" if b else "attempted", "errors": []}
    if b:
        row["boundary"] = {k: b.get(k) for k in (
            "pre_tokens", "post_tokens_est", "summary_chars", "precheck", "lost_certain",
            "compaction_cost_usd", "snapshot_s", "stopped_processes")}
    append_jsonl(path, row)
    return row


# ------------------------------------------------------------------------------------------
# Cohorts across hosts
# ------------------------------------------------------------------------------------------


@dataclass
class CohortJob:
    plan: RunPlan  # attempt 1; run_cohort bumps it for a restart
    route: C.Route
    factory: ModelFactory
    spend: RunSpend | None = None  # the run's books, shared with the factory's model


@dataclass
class CohortOutcome:
    results: list = field(default_factory=list)
    actions: dict = field(default_factory=dict)
    assignment: dict = field(default_factory=dict)
    swept: list = field(default_factory=list)


def process_pending_sweeps(pool: H.HostPool, results_root: Path, keep_run_ids: set[str]
                           ) -> list[dict]:
    """Sweep deferred cleanups on hosts that are up again, except runs about to resume."""
    path = s2_dir(results_root) / "pending_sweeps.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    done = {r["run_id"] for r in rows if r.get("done")}
    out = []
    for r in rows:
        if r.get("done") or r["run_id"] in done or r["run_id"] in keep_run_ids:
            continue
        if r["host"] not in pool.by_name or not pool.check(r["host"]):
            continue
        with using_host(r.get("docker_host")):
            bad = sweep(r["run_id"])
        rec = {"ts": utc(), "run_id": r["run_id"], "host": r["host"], "done": not bad,
               "unconfirmed": bad}
        append_jsonl(path, rec)
        done.add(r["run_id"])
        out.append(rec)
    return out


def run_cohort(jobs: list[CohortJob], pool: H.HostPool, *, results_root: Path,
               arm_concurrency: int = 4, do_grade: bool = True,
               release_name: str = "main", restart_capped: bool = False) -> CohortOutcome:
    """Run or resume every cohort run. Idempotent: rerun it after a host drop or a stop."""
    out = CohortOutcome()
    todo: list[tuple[CohortJob, str]] = []
    fixed: dict[str, str] = {}
    keep: set[str] = set()
    for j in jobs:
        st = read_status(j.plan.cohort_dir)
        h = (st or {}).get("host")
        up = bool(h) and h in pool.by_name and pool.check(h)
        img = (H.image_present(pool.host(h), st["boundary_tag"])
               if up and st and st.get("boundary_tag") else None)
        action = H.resume_action(st, up, img, restart_capped=restart_capped)
        out.actions[j.plan.cohort_key] = action
        if action in ("skip", "resume_arms") and up:
            fixed.setdefault(j.plan.task.task_id, h)  # affinity only to a configured, live host
        if action == "resume_arms":
            keep.add(st["run_id"])
        if action == "restart":
            j.plan.attempt = int(st.get("attempt") or 1) + 1
        if action != "skip":
            todo.append((j, action))
    out.swept = process_pending_sweeps(pool, results_root, keep)
    counts = Counter(j.plan.task.task_id for j, a in todo if a != "resume_arms")
    assign = pool.assign(dict(counts), fixed) if counts else dict(fixed)
    out.assignment = assign
    pjobs = []
    for j, action in todo:
        if action == "resume_arms":
            host = read_status(j.plan.cohort_dir)["host"]

            def fn(h, j=j):
                return resume_arms(j.plan.cohort_dir, j.route, j.factory,
                                   results_root=results_root, pool=pool,
                                   arm_concurrency=arm_concurrency, do_grade=do_grade,
                                   release_name=release_name, spend=j.spend)
        else:
            host = assign[j.plan.task.task_id]

            def fn(h, j=j):
                return run_one(j.plan, j.route, j.factory, results_root=results_root,
                               pool=pool, host_name=h.name, arm_concurrency=arm_concurrency,
                               do_grade=do_grade, release_name=release_name, spend=j.spend)
        pjobs.append(H.Job(j.plan.cohort_key, j.plan.task.task_id, host, fn))
    out.results = pool.run(pjobs)
    for r in out.results:
        if r.status in ("failed_host", "not_started_host_down"):
            cd = next(j.plan.cohort_dir for j, _ in todo if j.plan.cohort_key == r.key)
            if r.status == "not_started_host_down" and read_status(cd) is None:
                continue  # never started: stays fresh
            write_status(cd, state="failed_host")
    return out


def check_hosts(pool: H.HostPool, label: str = C.PURPOSE) -> dict:
    """Per host: our containers and snapshot images, or "unreachable" (AGENTS.md: check every
    host before stopping)."""
    out = {}
    for h in pool.hosts:
        r = H.list_ours(h, label) if not pool.is_down(h.name) else None
        out[h.name] = r if r is not None else "unreachable"
    return out


def raise_if_leftovers(report: dict) -> None:
    left = {h: v for h, v in report.items() if isinstance(v, dict) and v["containers"]}
    if left:
        raise SandboxError(f"leftover containers: {left}")
