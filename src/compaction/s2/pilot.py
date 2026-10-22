"""Orchestration of S2 runs: base trajectory, boundary, arms, scoring, sealing.

Two phases per run (violation-specs 3.3, "Ordering": S+R and S+I need the lost rule's identity,
so they fork after the boundary is labelled; F and S fork at once):

- `run_one` runs the base trajectory and the arms it can run now (F and S; also S+R and S+I
  when `labels` are given). With `keep_boundary=True` it removes the run's containers but keeps
  the boundary image (still labelled emx.run=<run id>) and records it in
  <results_root>/sprint/S2/kept_images.jsonl, with the sweep command that removes it.
- `resume_arms` runs the remaining arms from that image once labels exist, under an
  Invocation with the *same* run id, whose cleanup then removes the image too.

Each phase owns one `Invocation`; cleanup is confirmed by status queries and an unconfirmed
removal is raised after the telemetry row is written. Arms run concurrently
(`arm_concurrency`). Visible telemetry (no outcome fields) goes to
<results_root>/sprint/S2/telemetry.jsonl; outcome rows are sealed (compaction.seal) as
outcomes-<run_key>-<phase>.jsonl under <results_root>/sprint/S2/sealed/<release>/.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe.sandbox import RUN_LABEL_KEY, Invocation, SandboxError, remove_container
from compaction.s2 import config as C
from compaction.s2 import followups as FU
from compaction.s2 import outcomes as O
from compaction.s2 import rules as R
from compaction.s2.loop import RunConfig, arm_slug, run_arm, run_base
from compaction.s2.model import ChatModel
from compaction.s2.tasks import CohortRun, TaskInfo

ModelFactory = Callable[[str, int], ChatModel]  # (phase "base" or arm name, rep) -> model
_LOCK = threading.Lock()
TELE_KEYS_ARM = ("arm", "rep", "end", "n_calls", "cost_usd", "closing_sent", "would_recompact",
                 "error", "seconds")


@dataclass
class RunPlan:
    cohort: CohortRun
    task: TaskInfo
    threshold: int
    out_dir: Path
    policy: str | None = None
    run_cap_usd: float = 0.0
    base_max_calls: int = C.BASE_MAX_CALLS
    followup_distribution: list[tuple[int, int]] | None = None
    image: str | None = None


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str))


def s2_dir(results_root: Path) -> Path:
    return Path(results_root) / "sprint" / C.SEAL_STAGE


def lost_rules(labels: dict | None) -> list[str]:
    """Rules lost from context at a boundary, from Jacob's labels or the validated retention
    judge ({rule: "LOST"|"KEPT"|"UNKNOWN"}). The code precheck's certain R6 losses are telemetry
    only: S+R must restore every lost rule, which only a full classification gives."""
    return [r for r in R.RULE_IDS if (labels or {}).get(r) == "LOST"]


def docker_host() -> str:
    return os.environ.get("DOCKER_HOST") or "local"


def our_containers(label_value: str = C.PURPOSE) -> list[str] | None:
    """Names of containers labelled emx.sandbox=<label_value>; None if the query failed."""
    try:
        p = subprocess.run(["docker", "ps", "-a", "--filter", f"label=emx.sandbox={label_value}",
                            "--format", "{{.Names}}"], capture_output=True, text=True,
                           timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout.split() if p.returncode == 0 else None


def remove_run_containers(run_id: str) -> list[str]:
    """Remove every container labelled emx.run=<run_id> (images untouched); returns what was not
    confirmed gone."""
    try:
        p = subprocess.run(["docker", "ps", "-a", "--filter", f"label={RUN_LABEL_KEY}={run_id}",
                            "--format", "{{.Names}}"], capture_output=True, text=True,
                           timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return [f"containers of run {run_id} (status query failed)"]
    if p.returncode != 0:
        return [f"containers of run {run_id} (status query failed)"]
    return [n for n in p.stdout.split() if not remove_container(n)]


def _run_arms(cfg: RunConfig, plan_task: TaskInfo, todo: list[tuple[str, int]],
              model_factory: ModelFactory, inv: Invocation, lost: list[str], rules: R.TaskRules,
              boundary: dict, arm_concurrency: int, do_grade: bool) -> tuple[list, list[dict]]:
    def one(ar):
        a, r = ar
        res = run_arm(cfg, a, r, model_factory(a, r), inv, lost=lost, rules=rules,
                      boundary=boundary)
        row = None
        if res.traps_home and not res.error:
            row = O.arm_outcome(res, plan_task, rules, cfg.channel, inv.run_id, do_grade=do_grade)
        return res, row

    with ThreadPoolExecutor(max_workers=max(1, arm_concurrency)) as ex:
        results = list(ex.map(one, todo))
    return [r for r, _ in results], [row for _, row in results if row is not None]


def run_one(plan: RunPlan, route: C.Route, model_factory: ModelFactory, *, results_root: Path,
            arms: tuple[str, ...] = C.ARMS, reps: int = C.REPS, arm_concurrency: int = 4,
            do_grade: bool = True, labels: dict | None = None, keep_boundary: bool = False,
            release_name: str = "main") -> dict:
    t0 = time.monotonic()
    key = plan.cohort.run_key()
    inv = Invocation(C.PURPOSE)
    tele: dict = {"ts": utc(), "phase": "base", "run_key": key, "task": plan.task.task_id,
                  "rank": plan.task.rank, "model": route.name, "channel": plan.cohort.channel,
                  "kind": plan.cohort.kind, "threshold": plan.threshold, "run_id": inv.run_id,
                  "docker_host": docker_host()}
    script = FU.for_task(plan.task.task_id, plan.followup_distribution)
    cfg = RunConfig(key, plan.task, route, plan.cohort.channel, plan.threshold, script,
                    plan.out_dir, policy=plan.policy or "", base_max_calls=plan.base_max_calls,
                    run_cap_usd=plan.run_cap_usd)
    tele["policy"] = cfg.policy
    tele["followups"] = {"level": script.level, "turns": script.target_turns,
                         "tokens": script.target_tokens}
    _write(plan.out_dir / "run.json", {
        "plan": {**plan.cohort.__dict__, "threshold": plan.threshold, "policy": cfg.policy,
                 "route": route.__dict__, "base_max_calls": plan.base_max_calls,
                 "run_cap_usd": plan.run_cap_usd},
        "task": plan.task.to_dict(), "script": script.to_dict(), "started": utc(),
        "run_id": inv.run_id, "run_key": key})
    kept_tag = None
    bad: list[str] = []
    try:
        base = run_base(cfg, model_factory("base", 0), inv, image=plan.image)
        tele["base"] = {k: getattr(base, k) for k in (
            "end", "n_calls", "cost_usd", "ctx_tokens", "followups_delivered",
            "followup_tokens_delivered", "error", "seconds")}
        tele["tier"] = "attempted"
        if base.boundary:
            b = base.boundary
            tele["tier"] = "reached_compaction"
            tele["boundary"] = {k: b[k] for k in (
                "pre_tokens", "post_tokens_est", "summary_chars", "precheck", "lost_certain",
                "compaction_cost_usd", "snapshot_s", "stopped_processes")}
            rules = R.TaskRules(**{k: v for k, v in base.rules.items()
                                   if k in R.TaskRules.__dataclass_fields__})
            lost = lost_rules(labels)
            todo = [(a, r) for a in arms for r in range(1, reps + 1)
                    if a in C.ARMS_IMMEDIATE or lost]
            tele["arms_planned"] = [f"{arm_slug(a)}-{r}" for a, r in todo]
            tele["lost_used"] = lost
            res, rows = _run_arms(cfg, plan.task, todo, model_factory, inv, lost, rules, b,
                                  arm_concurrency, do_grade)
            tele["arms"] = [{k: getattr(x, k) for k in TELE_KEYS_ARM} for x in res]
            if rows:
                p = O.seal_outcomes(f"{key}-base", rows, root=Path(results_root) / "sprint",
                                    release_name=release_name)
                tele["sealed"] = str(p)
            if keep_boundary and any(a not in C.ARMS_IMMEDIATE for a in arms) and not labels:
                kept_tag = b["tag"]
    finally:
        if kept_tag:
            bad = remove_run_containers(inv.run_id)
            rec = {"ts": utc(), "run_key": key, "run_id": inv.run_id, "tag": kept_tag,
                   "remove_with": "python -m compaction.deepswe.sandbox sweep --run-id "
                                  + inv.run_id}
            append_jsonl(s2_dir(results_root) / "kept_images.jsonl", rec)
            tele["kept_boundary_image"] = kept_tag
        else:
            bad = inv.cleanup()
        tele["cleanup_unconfirmed"] = bad
        tele["seconds"] = round(time.monotonic() - t0, 3)
        tele["cost_usd"] = round((tele.get("base") or {}).get("cost_usd", 0.0) + sum(
            a["cost_usd"] for a in tele.get("arms", [])), 6)
        append_jsonl(s2_dir(results_root) / "telemetry.jsonl", tele)
    if bad:
        raise SandboxError(f"run {key}: cleanup not confirmed for {bad}")
    return tele


def resume_arms(run_dir: Path, route: C.Route, model_factory: ModelFactory, *,
                results_root: Path, labels: dict, arms: tuple[str, ...] = ("S+R", "S+I"),
                reps: int = C.REPS, arm_concurrency: int = 4, do_grade: bool = True,
                release_name: str = "main") -> dict:
    """Phase 2: run `arms` from a kept boundary image, then remove the image (same run id)."""
    t0 = time.monotonic()
    run = json.loads((run_dir / "run.json").read_text())
    b = json.loads((run_dir / "boundary" / "boundary.json").read_text())
    rd = json.loads((run_dir / "rules.json").read_text())
    rules = R.TaskRules(**{k: v for k, v in rd.items() if k in R.TaskRules.__dataclass_fields__})
    task = TaskInfo(**run["task"])
    pl = run["plan"]
    script = FU.Script(**{**run["script"], "turns": [FU.FollowUp(**t)
                                                     for t in run["script"]["turns"]]})
    key = run["run_key"]
    cfg = RunConfig(key, task, route, pl["channel"], pl["threshold"], script, run_dir,
                    policy=pl["policy"])
    inv = Invocation(C.PURPOSE, run_id=run["run_id"])
    lost = lost_rules(labels)
    todo = [(a, r) for a in arms for r in range(1, reps + 1)] if lost else []
    tele: dict = {"ts": utc(), "phase": "resume", "run_key": key, "run_id": inv.run_id,
                  "arms_planned": [f"{arm_slug(a)}-{r}" for a, r in todo], "lost_used": lost,
                  "docker_host": docker_host()}
    try:
        if todo:
            res, rows = _run_arms(cfg, task, todo, model_factory, inv, lost, rules, b,
                                  arm_concurrency, do_grade)
            tele["arms"] = [{k: getattr(x, k) for k in TELE_KEYS_ARM} for x in res]
            if rows:
                tele["sealed"] = str(O.seal_outcomes(f"{key}-resume", rows,
                                                     root=Path(results_root) / "sprint",
                                                     release_name=release_name))
    finally:
        inv.tags.append(b["tag"])  # so cleanup removes the kept image by tag as well
        bad = inv.cleanup()
        tele["cleanup_unconfirmed"] = bad
        tele["seconds"] = round(time.monotonic() - t0, 3)
        tele["cost_usd"] = round(sum(a["cost_usd"] for a in tele.get("arms", [])), 6)
        append_jsonl(s2_dir(results_root) / "telemetry.jsonl", tele)
    if bad:
        raise SandboxError(f"run {key}: cleanup not confirmed for {bad}")
    return tele
