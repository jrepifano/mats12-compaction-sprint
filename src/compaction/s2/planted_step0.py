"""Offline boundary census and explicitly gated, zero-simulator Step 0.

Nothing runs at import or discovery time. Every live attempt is durably reserved
before launch; failures and interrupted continuations consume their allocation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import planted_score as S
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N
from compaction.s2.planted_config import (
    MAX_CALLS,
    WINDOW,
    ActiveCohort,
    ForkBudget,
    part_b_tasks,
    runtime_tasks,
    task_order,
)
from compaction.s2.planted_retention import digest

DEFAULT_CORPUS = C.REPO_ROOT.parent / "sprint-prep/data/raw/sprint/s2/native/claude_code"
RATE_MAX_AGE_S = 300
INITIAL_MAX_RESET = datetime.fromisoformat("2026-10-26T00:00:00+00:00").timestamp()


def read(path: Path):
    return json.loads(Path(path).read_text())


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def inspect_boundary(bdir: Path) -> dict:
    """Strict offline eligibility; kept-image existence/fork_check remain pending."""
    bdir = Path(bdir)
    problems, hashes = [], {}
    b = {}
    try:
        b = N.reclassify_harness_quiescence(read(bdir / "boundary/boundary.json"))
        for field in ("tag", "native_summary_uuid", "fingerprint_sha256", "sim_probe_sha256"):
            if not b.get(field):
                problems.append(f"missing {field}")
        if b.get("quiescent") is not True:
            problems.append("not_quiescent")
        if b.get("services_running"):
            problems.append("entrypoint_services")
        if b.get("ineligible_reason"):
            problems.append(str(b["ineligible_reason"]))
        if b.get("stage") != "02_implement":
            problems.append("not_implementation_boundary")
        for rel in ("boundary/boundary.json", "boundary/sim_state.json", "boundary/compaction.json",
                    "boundary/pre_messages.json", "boundary/post_messages.json",
                    "native/transcript.jsonl", "native/hook_payload.json",
                    "../run.json"):
            path = bdir / rel
            raw_bytes = path.read_bytes()
            raw = raw_bytes.decode()
            hashes[rel] = hashlib.sha256(raw_bytes).hexdigest()
            if rel.endswith(".jsonl"):
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                if any(not isinstance(row, dict) for row in rows):
                    raise ValueError("native transcript rows must be objects")
                hits = [r for r in rows if r.get("uuid") == b.get("native_summary_uuid")]
                if (len(hits) != 1 or not isinstance(hits[0].get("message"), dict)
                        or not isinstance(hits[0]["message"].get("content"), str)):
                    problems.append("summary_missing_or_ambiguous")
                ids = [r["uuid"] for r in rows if r.get("uuid")]
                if len(ids) != len(set(ids)):
                    problems.append("duplicate_transcript_uuid")
                if any(r.get("parentUuid") and r["parentUuid"] not in ids for r in rows):
                    problems.append("broken_transcript_parent_link")
                converted = CC.transcript_boundary(rows, b["boundary_in_session"],
                                                   model=b["model"], step_instruction="")
                for name, rebuilt in (("pre_messages", converted.nb.pre_messages),
                                      ("post_messages", converted.nb.post_messages)):
                    if rebuilt != read(bdir / f"boundary/{name}.json"):
                        problems.append(f"rebuilt_{name}_mismatch")
                if converted.nb.summary != read(bdir / "boundary/compaction.json")["summary"]:
                    problems.append("rebuilt_summary_mismatch")
                if len(hits) == 1:
                    hook = read(bdir / "native/hook_payload.json")
                    # Validate the exact insertion anchor before reserving any fork.
                    CC.insert_summary_lines(hits[0]["message"]["content"],
                                            hook["compact_summary"], ["offline-anchor-check"])
            else:
                json.loads(raw)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        problems.append(f"invalid_boundary: {exc}")
    return {"boundary": str(bdir.resolve()), "offline_eligible": not problems,
            "problems": sorted(set(problems)), "image": b.get("tag"),
            "image_check": "pending_live", "fork_check": "pending_live",
            "reclassification": b.get("quiescence_reclassification"),
            "exempted_processes": b.get("harness_exempted_processes", []),
            "pre_tokens": b.get("pre_tokens"), "hashes": hashes}


def snapshot_boundary(bdir: Path, destination: Path, *, expected_hashes: dict | None = None) -> Path:
    """Private launch copy with audited metadata correction; source stays byte-identical."""
    copied = destination / bdir.parent.name / bdir.name
    for folder in ("boundary", "native"):
        shutil.copytree(bdir / folder, copied / folder)
    shutil.copy2(bdir.parent / "run.json", copied.parent / "run.json")
    for rel, expected in (expected_hashes or {}).items():
        if hashlib.sha256((copied / rel).read_bytes()).hexdigest() != expected:
            raise ValueError(f"source changed between census and snapshot: {rel}")
    original_sha = hashlib.sha256((copied / "boundary/boundary.json").read_bytes()).hexdigest()
    original = read(copied / "boundary/boundary.json")
    effective = N.reclassify_harness_quiescence(original)
    write(copied / "boundary/boundary.json", effective)
    write(copied.parent / "source_provenance.json", {"original": str(bdir.resolve()),
          "original_metadata_sha256": original_sha,
          "effective_metadata_sha256": hashlib.sha256((copied / "boundary/boundary.json").read_bytes()).hexdigest(),
          "reclassification": effective.get("quiescence_reclassification")})
    return copied


def discover(corpus: Path = DEFAULT_CORPUS) -> dict:
    order = task_order()
    cohort = ActiveCohort().state()
    active = cohort["active_cohort"]
    tasks = []
    for index, task_id in enumerate(order):
        boundaries = [inspect_boundary(p) for p in sorted(Path(corpus).glob(f"{task_id}*/b*"))
                      if p.is_dir() and p.name[1:].isdigit()]
        tasks.append({"task_id": task_id, "initial_cohort": index < 3,
                      "active_cohort": task_id in active, "part_b_cohort": task_id in part_b_tasks(active),
                      "boundaries": boundaries,
                      "offline_eligible_count": sum(b["offline_eligible"] for b in boundaries)})
    return {"schema": 1, "corpus": str(Path(corpus).resolve()), "task_order": order,
            "selected_tasks": active, "tasks": tasks,
            "eligible_selected_tasks": sum(t["active_cohort"] and t["offline_eligible_count"] > 0
                                           for t in tasks),
            "live_checks_performed": False, "replacements": cohort["replacements"]}


def _window_hold(rate: dict, now: float):
    # Reuse the audited native launcher rule without executing its CLI.
    path = C.REPO_ROOT / "scripts/s2_native.py"
    spec = importlib.util.spec_from_file_location("_planted_native_launch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.window_hold(rate, now)


def guard_rate(snapshot: dict, *, now: float, hold: Callable = _window_hold) -> dict:
    observed = snapshot.get("observed_at")
    if not isinstance(observed, (int, float)) or not 0 <= now - observed <= RATE_MAX_AGE_S:
        raise ValueError("fresh Max rate snapshot required (observed_at epoch, at most 300 seconds old)")
    rate = snapshot.get("rate_limit_info") or {}
    if not rate or not (rate.get("unifiedWindows") or rate.get("rateLimitType")):
        raise ValueError("Max rate snapshot has no known window")
    why, wait = hold(rate, now)
    if why:
        raise ValueError(f"Max window hold: {why}; wait at least {wait:.0f}s")
    windows = rate.get("unifiedWindows") or {rate.get("rateLimitType"): rate}
    seven_day = windows.get("seven_day") or {}
    if not isinstance(seven_day.get("utilization"), (int, float)) or float(seven_day.get("resetsAt") or 0) <= now:
        raise ValueError("Max rate snapshot has no active measured seven-day window")
    if now < INITIAL_MAX_RESET and seven_day["utilization"] >= .87:
        raise ValueError("Step 0 before the initial Max reset requires utilization below 87%")
    return snapshot


def _usage(result: dict, before: dict, observed_at: float) -> dict:
    step = next(iter((result.get("steps") or {}).values()), {})
    rate = result.get("rate_limit") or step.get("rate_limit") or {}
    info = rate.get("rate_limit_info") or {}
    body = result.get("result") or step.get("result") or {}
    tokens = result.get("token_usage") or step.get("token_usage") or body.get("usage") or body.get("modelUsage") or {}
    arrival = rate.get("observed_at")
    def seven_day(value):
        details = value.get("rate_limit_info") or {}
        return (details.get("unifiedWindows") or {}).get("seven_day") or (
            details if details.get("rateLimitType") == "seven_day" else {})
    after = {"observed_at": arrival, "rate_limit_info": info}
    pre, post = seven_day(before), seven_day(after)
    same_window = bool(pre.get("resetsAt")) and pre.get("resetsAt") == post.get("resetsAt")
    delta = post.get("utilization", 0) - pre.get("utilization", 0) if same_window else None
    return {"before": before, "after": after, "recorded_at": observed_at,
            "tokens": tokens, "known": bool(info) and bool(tokens) and isinstance(arrival, (int, float)),
            "same_window": same_window, "seven_day_utilization_delta": delta,
            "delta_scope": "shared Max window; includes concurrent sessions"}


def _checks(result: dict) -> dict:
    return {"restore": (result.get("fork_check") or {}).get("ok") is True,
            "captured_ask": bool(result.get("captured_asks")),
            "zero_simulator": result.get("sim_calls") == 0 and result.get("sim_cost_usd") == 0,
            "cleanup": "cleanup_unconfirmed" in result and result["cleanup_unconfirmed"] in (False, []),
            "no_error": not result.get("error") and not result.get("auth_error"),
            "terminal_stop": (result.get("outcome") or result.get("end")) == "ask_user"}


def run_step0(bdir: Path, output_root: Path, *, spec: dict, task, token: str,
              rate_snapshot: dict, go_from_jacob: bool = False, reviewed_build: bool = False,
              reviews_ready: Path | None = None, host: str | None = None,
              attempt: str = "step0-1", judge=None, judge_factory=None,
              budget: ForkBudget | None = None,
              fork_runner: Callable = CC.fork, continue_runner: Callable = CC.continue_from_boundary,
              clock: Callable = time.time, hold: Callable = _window_hold) -> dict:
    """One native probe and one no-simulator continue check; never called during build."""
    if not go_from_jacob:
        raise PermissionError("Step 0 requires the main session's explicit --go-from-jacob")
    if not reviewed_build:
        ready = read(reviews_ready) if reviews_ready else {}
        if ready.get("ready") is not True or not ready.get("reviews"):
            raise PermissionError("completed reviews artifact or --reviewed-build is required")
    guard_rate(rate_snapshot, now=clock(), hold=hold)
    census = inspect_boundary(bdir)
    if not census["offline_eligible"]:
        raise ValueError(f"boundary fails offline checks: {census['problems']}")
    if spec.get("schema") != 2 or spec.get("eligible") is not True:
        raise ValueError("Step 0 requires an eligible schema-2 task spec")
    if spec["task_id"] not in runtime_tasks():
        raise ValueError("Step 0 task must belong to the active seeded cohort")
    if task.task_id != spec["task_id"] or spec.get("window") != WINDOW or spec.get("max_calls") != MAX_CALLS:
        raise ValueError("task/spec mismatch or unfrozen window")
    source_task = read(Path(bdir).parent / "run.json").get("task", {}).get("task_id")
    if source_task != spec["task_id"]:
        raise ValueError("boundary belongs to a different task")
    if not token:
        raise ValueError("Claude Code OAuth token missing")
    if not attempt or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in attempt):
        raise ValueError("attempt must be a simple unique label")
    root = Path(output_root)
    out = root / attempt
    if out.exists():
        raise FileExistsError("attempt directory exists; use a new attempt, never overwrite")
    budget = budget or ForkBudget(attempt_roots=(root,))
    ids = [f"{attempt}:probe", f"{attempt}:continue"]
    state = budget.state()
    bucket = "step0" if not any(a["bucket"] == "step0" for a in state["attempts"]) else "step0_extra"
    budget.reserve_batch([{"attempt_id": identity, "part": "A", "bucket": bucket,
                           "boundary": str(Path(bdir).resolve())} for identity in ids])
    out.mkdir(parents=True)
    report = {"status": "FAIL", "attempt": attempt, "boundary": census, "spec_sha256": digest(spec),
              "started_at": clock(), "attempt_ids": ids, "simulator_model": "NoSimulatorModel",
              "authorization": {"go_from_jacob": go_from_jacob, "reviewed_build_attestation": reviewed_build,
                                "reviews_ready": str(reviews_ready) if reviews_ready else None},
              "continuation_stop_contract": "must capture ask_user within 30 assistant turns for this smoke",
              "probe": None, "continuation": None}
    write(out / "report.json", report)
    try:
        bdir = snapshot_boundary(Path(bdir), out / "source", expected_hashes=census["hashes"])
        report["launch_boundary"] = str(bdir)
        b = read(Path(bdir) / "boundary/boundary.json")
        rows = [json.loads(line) for line in (Path(bdir) / "native/transcript.jsonl").read_text().splitlines() if line.strip()]
        summary = next(r["message"]["content"] for r in rows if r.get("uuid") == b["native_summary_uuid"])
        hook = read(Path(bdir) / "native/hook_payload.json")
        inserted = CC.insert_summary_lines(summary, hook["compact_summary"],
                                          [o["text"] for o in spec["obligations"]])
        cfg = CC.CCConfig(auto_compact_window=WINDOW)
        report["source_settings"] = {k: b.get(k) for k in ("model", "effort", "auto_compact_window")}
        report["launch_settings"] = {"model": cfg.model, "effort": cfg.effort,
                                     "auto_compact_window": WINDOW, "cpus": N.AGENT_CPUS,
                                     "memory_mb": N.AGENT_MEMORY_MB}
        probe_dir = out / "probe"
        probe = fork_runner(Path(bdir), probe_dir, new_summary=inserted,
                            prompt=spec["probe_message"], sim_model=CC.NoSimulatorModel(), token=token,
                            persona=task.persona, cfg=cfg, host=host, probe=True, max_calls=MAX_CALLS,
                            required_context=[o["text"] for o in spec["obligations"]])
        usage = _usage(probe, rate_snapshot, clock())
        evidence = read(probe_dir / "evidence.json")
        # Missing completeness is unknown, never interpreted as a complete negative.
        complete = evidence.get("part_a_complete", evidence.get("capture_complete", False))
        if judge is None and judge_factory is not None:
            guard_rate(usage["after"], now=clock(), hold=hold)
            judge = judge_factory(out / "judgments")
        scores = {o["kind"]: S.score(evidence["events"], o["scorer"], judge=judge,
                                     capture_complete=complete) for o in spec["obligations"]}
        checks = _checks(probe)
        checks.update(context=(probe.get("context_check") or {}).get("ok") is True,
                      core_scored=set(scores) == {"commit_convention", "verification_output"}
                      and all(s["outcome"] in {"complied", "violated"} for s in scores.values()),
                      usage=usage["known"], verification_output_observed=bool(
                          scores.get("verification_output", {}).get("evidence", {}).get("last_output_line")))
        report["probe"] = {"result": probe, "usage": usage, "scores": scores, "checks": checks}
        budget.finish(ids[0], "complete" if all(checks.values()) else "technical_failure", artifact=str(probe_dir))
        write(out / "report.json", report)
        if not checks["cleanup"] or not checks["restore"]:
            raise RuntimeError("unsafe or mismatched probe: continuation withheld")
        guard_rate(usage["after"], now=clock(), hold=hold)
        cont_cfg = CC.BaseConfig(task=task, root=out, prefix="step0-continue-", cc=cfg,
                                  host=host, capture_ask=True, max_calls=MAX_CALLS,
                                  steps=(CC.GRADED_STEP,),
                                  cpus=N.AGENT_CPUS, memory_mb=N.AGENT_MEMORY_MB,
                                  min_free_gb=N.AGENT_MIN_FREE_GB)
        cont = continue_runner(Path(bdir), cont_cfg.run_dir, cfg=cont_cfg,
                               sim_model=CC.NoSimulatorModel(), token=token)
        cont_usage = _usage(cont, usage["after"], clock())
        cont_checks = _checks(cont) | {"usage": cont_usage["known"]}
        report["continuation"] = {"result": cont, "usage": cont_usage, "checks": cont_checks}
        budget.finish(ids[1], "complete" if all(cont_checks.values()) else "technical_failure",
                      artifact=str(cont_cfg.run_dir))
        report["status"] = "PASS" if all(checks.values()) and all(cont_checks.values()) else "FAIL"
    except Exception as exc:  # noqa: BLE001 -- durable failure report, attempts never refunded
        report["error"] = f"{type(exc).__name__}: {exc}"
        for row in budget.state()["attempts"]:
            if row["attempt_id"] in ids and row["status"] == "assigned":
                budget.finish(row["attempt_id"], "technical_failure", artifact=str(out))
    finally:
        report["ended_at"] = clock()
        write(out / "report.json", report)
    return report
