"""Desktop-only Part A execution backend; imports and construction never launch work.

Every phase and every fork is assigned durably before launch. Interrupted phases
are terminal on recovery, never replayed. Baseline outputs are sealed without
scoring; retention reads only base-boundary evidence. Cards/repair are not run.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from compaction import claude_headless as H
from compaction.s2 import config as C
from compaction.s2 import interact_run as IR
from compaction.s2 import planted_config as PC
from compaction.s2 import planted_eligibility as E
from compaction.s2 import planted_retention as R
from compaction.s2 import planted_score as S
from compaction.s2 import planted_step0 as P
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N
from compaction.s2.planting import PlantingSchedule

CONTINGENCY_TAG = "s2-planted-contingency"
FINAL_CUTOFF = PC.B_CUTOFF.timestamp()
STOP_FILE = C.REPO_ROOT / "logs/s2/planted_run.stop"


def _live_stop_reason(now, deadline, stop_file):
    if now < PC.RESET.timestamp():
        return "before_authorized_reset"
    if now >= deadline:
        return "deadline"
    if Path(stop_file).exists():
        return "operator_stop"
    return None


def reusable_rate(snapshot, *, now, since=None):
    """Return complete, fresh stream evidence; retain valid holds for the guard."""
    def finite_number(value):
        try:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            return False

    if not isinstance(snapshot, dict):
        return None
    observed = snapshot.get("observed_at")
    if not finite_number(observed) or not 0 <= now - observed <= 60:
        return None
    if since is not None and observed < since:
        return None
    info = snapshot.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    windows = info.get("unifiedWindows") or {info.get("rateLimitType"): info}
    seven = windows.get("seven_day") if isinstance(windows, dict) else None
    if not isinstance(seven, dict) or not all(finite_number(seven.get(key)) for key in ("utilization", "resetsAt")):
        return None
    return snapshot


class TaskTerminal(RuntimeError):
    """A confirmed-safe technical task ending; other tasks may continue."""


class GuardedMaxBackend(H.MaxBackend):
    """Pilot-local guard of each actual CLI attempt, including canaries/retries."""

    def __init__(self, *args, deadline_epoch=FINAL_CUTOFF, stop_file=None,
                 before_attempt=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline_epoch = min(deadline_epoch, FINAL_CUTOFF)
        self.stop_file = Path(stop_file or STOP_FILE)
        self.before_attempt = before_attempt
        self._extra_before_attempt = None
        self.max_window_utilization = .90
        self.pilot_stop_reason = None
        self.latest_rate = None
        self._pilot_lock = threading.Lock()

    def _halt(self, reason):
        with self._pilot_lock:
            if self.pilot_stop_reason is None:
                self.pilot_stop_reason = reason
                P.write(self.cache_dir / "pilot-guard-stop.json", {"reason": reason,
                    "observed_at": self._clock(), "deadline_epoch": self.deadline_epoch,
                    "stop_file": str(self.stop_file)})
            self._stop.set()

    def _policy(self):
        return self.pilot_stop_reason or _live_stop_reason(self._clock(), self.deadline_epoch, self.stop_file)

    def _watch(self, event):
        paid = super()._watch(event)  # preserve subscription-only route enforcement
        reason = self._policy()
        if paid:
            reason = "paid_route"
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            self.latest_rate = {"observed_at": self._clock(), "rate_limit_info": info}
            windows = [*(info.get("unifiedWindows") or {}).items(), (info.get("rateLimitType"), info)]
            for name, window in windows:
                if not isinstance(window, dict):
                    continue
                reset = window.get("resetsAt")
                if (isinstance(reset, (int, float)) and not isinstance(reset, bool)
                        and math.isfinite(reset) and reset <= self._clock()):
                    continue
                if (window.get("status") in {"rejected", "rate_limited"} or
                        isinstance(window.get("utilization"), (int, float))
                        and name == "seven_day" and window["utilization"] >= self.max_window_utilization):
                    reason = "max_window_hold"
                    break
        if reason:
            self._halt(reason)
            return True
        return False

    def _attempt(self, req, purpose, stats, attempt, schema_retry):
        reason = self._policy()
        if reason:
            self._halt(reason)
            raise RuntimeError(f"planted judge stopped before attempt: {reason}")
        metadata = {"request_key": req.key, "purpose": purpose, "attempt": attempt,
                    "canary": req.key == H.CANARY_KEY}
        for callback in (self.before_attempt, self._extra_before_attempt):
            if callback is not None:
                try:
                    callback(metadata)
                except Exception as exc:
                    self._halt(f"before_attempt: {type(exc).__name__}: {exc}")
                    raise
        reason = self._policy()  # callback/auth work may have crossed the deadline
        if reason:
            self._halt(reason)
            raise RuntimeError(f"planted judge stopped before attempt: {reason}")
        self.latest_rate = None  # never treat an earlier canary/retry as this attempt's after-reading
        finished = threading.Event()

        def watch_silent_call():
            while not finished.wait(.25):
                if reason := self._policy():
                    self._halt(reason)
                    return

        watcher = threading.Thread(target=watch_silent_call, name="planted-judge-guard", daemon=True)
        watcher.start()
        try:
            result = super()._attempt(req, purpose, stats, attempt, schema_retry)
            if reason := self._policy():
                self._halt(reason)
                raise RuntimeError(f"planted judge stopped during attempt: {reason}")
            return result
        finally:
            finished.set()
            watcher.join(timeout=1)


def guarded_max_judge(cache_dir, *, before_attempt=None, clock=time.time,
                      deadline_epoch=FINAL_CUTOFF, stop_file=None, recorder=None):
    """Opus Max-only factory; no calls at construction. Per-call guards also cover canaries."""
    backend = GuardedMaxBackend(model=R.MODEL, name="planted-opus-max", cache_dir=cache_dir,
        usage_path=Path(cache_dir) / "usage.jsonl", concurrency=1, max_limit_waits=0,
        timeout_s=600, clock=clock, deadline_epoch=deadline_epoch, stop_file=stop_file,
        before_attempt=before_attempt, recorder=recorder or PC.pilot_spend().recorder())
    raw = R.max_judge(Path(cache_dir), backend=backend)
    lock = threading.Lock()

    def judge(request, *, before_attempt=None, max_window_utilization=.90):
        if not 0 < max_window_utilization <= .90:
            raise ValueError("judge utilization limit must be positive and at most 90%")
        if not lock.acquire(blocking=False):
            raise RuntimeError("overlapping guarded judgments are forbidden")
        backend._extra_before_attempt = before_attempt
        backend.max_window_utilization = max_window_utilization
        try:
            return raw(request)
        finally:
            backend._extra_before_attempt = None
            backend.max_window_utilization = .90
            lock.release()

    judge.latest_rate = lambda: backend.latest_rate
    judge.guarded_attempts = True
    return judge


def epoch(value):
    return datetime.fromisoformat(value).timestamp() if isinstance(value, str) else float(value)


def desktop_max_reading(*, root: Path, clock=time.time, run=None, recorder=None, checks=None) -> dict:
    """A minimal local Claude CLI call with paid routes killed while streaming.

    Call only after authorization/reset checks. No API credential or fallback is
    passed to the subprocess. Timestamp is the actual rate-event arrival time.
    """
    from compaction import claude_headless as H
    if checks is None:
        H.check_auth()
        H.check_managed_settings()
    else:
        checks()
    if reason := _live_stop_reason(clock(), FINAL_CUTOFF, STOP_FILE):
        raise RuntimeError(f"Max reading withheld: {reason}")
    observations, paid = [], []
    stop, finished = threading.Event(), threading.Event()
    stopped = []

    def watch_silent_ping():
        while not finished.wait(.25):
            if reason := _live_stop_reason(clock(), FINAL_CUTOFF, STOP_FILE):
                stopped.append(reason)
                stop.set()
                return

    def watch(event):
        why = H._paid_event(event)
        if why:
            paid.append(why)
        if event.get("type") == "rate_limit_event":
            observations.append({"observed_at": clock(), "rate_limit_info": event.get("rate_limit_info")})
        return bool(why)

    args = ["-p", "--model", CC.MODEL, "--safe-mode", "--tools", "",
            "--no-session-persistence", "--strict-mcp-config", "--output-format", "stream-json",
            "--verbose", "--system-prompt", H.CANARY_SYSTEM]
    watcher = threading.Thread(target=watch_silent_ping, name="planted-ping-guard", daemon=True)
    watcher.start()
    try:
        with tempfile.TemporaryDirectory(prefix="planted-rate-") as cwd:
            events = (run or H.run_session)(cwd, args, H.CANARY_USER, min(120, FINAL_CUTOFF - clock()),
                                           env=H.clean_env(H.CANARY_MAX_OUTPUT), on_event=watch, stop=stop)
    finally:
        finished.set()
        watcher.join(timeout=1)
    if stopped or _live_stop_reason(clock(), FINAL_CUTOFF, STOP_FILE):
        raise RuntimeError(f"Max reading stopped: {stopped or _live_stop_reason(clock(), FINAL_CUTOFF, STOP_FILE)}")
    if paid or H.verify_route(H.route_evidence(events)) != "ok":
        raise RuntimeError("Max-only reading route was not verified")
    if not observations:
        raise RuntimeError("Claude returned no observed Max rate reading")
    output = Path(root) / "rate-readings" / f"{int(clock() * 1e6)}.json"
    P.write(output, {"events": events, "reading": observations[-1]})
    if recorder:
        recorder("judge", "claude-max:" + CC.MODEL, units=1, usd=0, note="planted fresh Max reading")
    return observations[-1]


class Runtime:
    def __init__(self, *, root: Path, corpus: Path, specs_dir: Path,
                 si_root: Path, deepswe_root: Path, eligibility_path: Path,
                 token: str | None = None, host: str | None = None,
                 go_from_jacob: bool = False, reviewed_build: bool = False,
                 before_action=None, expected_hostname: str | None = None,
                 part_b_preparations=None, clock=time.time, io: dict | None = None):
        if host not in (None, "", "unix:///var/run/docker.sock"):
            raise ValueError("planted runtime requires the desktop's local Docker daemon")
        self.root, self.corpus, self.specs_dir = Path(root), Path(corpus), Path(specs_dir)
        self.si_root, self.deepswe_root = Path(si_root), Path(deepswe_root)
        self.eligibility_path = Path(eligibility_path)
        self.token = token if token is not None else os.environ.get(CC.TOKEN_ENV, "")
        self.host, self.go, self.reviewed = host, go_from_jacob, reviewed_build
        self.before_action, self.expected_hostname = before_action, expected_hostname
        self.part_b_preparations, self.clock, self.io = part_b_preparations, clock, io or {}
        self.budget = self.io.get("budget") or PC.ForkBudget(attempt_roots=(self.root,))
        self.cohort = self.io.get("cohort") or PC.ActiveCohort()
        self.spend = self.io.get("spend") or PC.pilot_spend()
        self._rate, self._judge = None, None
        self._active_work = "idle"
        self._active_attempt = None

    def _authorized(self):
        if not self.go or not self.reviewed:
            raise PermissionError("runtime requires explicit go and reviewed-build attestation")
        if self.expected_hostname and socket.gethostname() != self.expected_hostname:
            raise PermissionError("runtime is not on the designated desktop ledger owner")
        if self.clock() < PC.RESET.timestamp():
            raise PermissionError("no runtime model calls before the authorized Max reset")
        if self.clock() >= FINAL_CUTOFF:
            raise RuntimeError("pilot Tuesday 6 PM ET cutoff reached")
        if STOP_FILE.exists():
            raise RuntimeError("operator stop file present")
        if not self.token:
            raise ValueError("Claude Max token missing")

    def load_spec(self, task_id):
        spec = P.read(self.specs_dir / f"{task_id}.json")
        if (spec.get("schema") != 2 or spec.get("eligible") is not True
                or spec.get("task_id") != task_id or spec.get("window") != PC.WINDOW):
            raise ValueError("eligible frozen schema-2 task specification required")
        return spec

    def task_loader(self, task_id):
        loader = self.io.get("task_loader") or IR.load_task
        return loader(task_id, si_root=self.si_root, deepswe_dir=self.deepswe_root)

    def _state(self):
        path = self.root / "runtime.json"
        if path.exists():
            return P.read(path)
        boundaries = []
        report = P.read(self.eligibility_path) if self.eligibility_path.is_file() else {}
        for task in report.get("tasks", []):
            for item in task.get("boundaries", []):
                if item.get("eligible"):
                    boundaries.append({"task_id": task["task_id"], "boundary": item["boundary"],
                                       "live_eligible": True, "validation": item, "origin": "historical"})
        return {"boundaries": boundaries, "chains": {}, "retention": [], "base_attempts": []}

    def _save(self, state):
        P.write(self.root / "runtime.json", state)

    def _totals(self, tag=PC.SPEND_TAG):
        if "totals" in self.io:
            return self.io["totals"](tag)
        return IR.tag_totals(tag, ledger=self.spend.ledger, caps_file=self.spend.caps_file, stage=self.spend.stage)

    def snapshot(self):
        state, attempts = self._state(), self.budget.state()["attempts"]
        charged, held = self._totals()
        return {"active_tasks": self.cohort.state()["active_cohort"],
                "boundaries": state["boundaries"], "simulator_usd": charged + held,
                "simulator_charged_usd": charged, "simulator_held_usd": held,
                "forks_a": sum(a["part"] == "A" for a in attempts),
                "forks_b": sum(a["part"] == "B" for a in attempts), "forks_total": len(attempts)}

    def observe_rate(self, snapshot):
        """Reuse fresh native/judge stream evidence without issuing a reading call."""
        if reusable_rate(snapshot, now=self.clock()) is None:
            return False
        if reusable_rate(self._rate, now=self.clock()) and self._rate["observed_at"] > snapshot["observed_at"]:
            return False
        self._rate = snapshot
        return True

    def max_reading(self, *, force=False):
        self._authorized()
        if not force and reusable_rate(self._rate, now=self.clock()):
            return self._rate
        reader = self.io.get("rate_reader")
        self._rate = reader() if reader else desktop_max_reading(
            root=self.root, clock=self.clock, recorder=self.spend.recorder())
        return self._rate

    def guard(self, action: str, work_id: str | None = None, *, attempt_id=None):
        self._authorized()
        work_id = work_id or self._active_work
        attempt_id = attempt_id or self._active_attempt
        if self.before_action:
            self.before_action({"action": action, "work_id": work_id,
                                **({"attempt_id": attempt_id} if attempt_id else {})})
        snapshot = self.snapshot()
        assigned = any(row["attempt_id"] == attempt_id and row["status"] == "assigned"
                       for row in self.budget.state()["attempts"]) if attempt_id else False
        if (snapshot["simulator_usd"] >= PC.SIM_CAP_USD or snapshot["forks_total"] > 94
                or snapshot["forks_total"] == 94 and not assigned):
            raise RuntimeError("pilot spend/fork cap reached")
        return P.guard_rate(self.max_reading(), now=self.clock())

    def judge(self, request, *, attempt_id=None):
        previous = self._active_attempt
        self._active_attempt = attempt_id or previous
        try:
            self.guard("opus_judge")
            if self._judge is None:
                self._judge = self.io.get("judge") or guarded_max_judge(self.root / "judgments",
                    clock=self.clock, recorder=self.spend.recorder(),
                    before_attempt=lambda metadata: self.guard("opus_actual_attempt"))
            return self._judge(request)
        finally:
            reading = getattr(self._judge, "latest_rate", lambda: None)()
            self._rate = reusable_rate(reading, now=self.clock())
            self._active_attempt = previous

    def _receipt(self, work_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", work_id):
            raise ValueError("work_id must be a simple unique label")
        return self.root / "operations" / work_id / "receipt.json"

    def recover(self, work_id):
        path = self._receipt(work_id)
        if not path.exists():
            return {"status": "HALTED", "reason": "assigned operation has no receipt; never replay automatically"}
        receipt = P.read(path)
        if receipt["status"] == "ASSIGNED":
            receipt.update(status="HALTED", reason="interrupted operation; assigned work is never relaunched")
            P.write(path, receipt)
        return receipt

    @contextmanager
    def _phase(self, work_id, phase):
        self._authorized()
        path = self._receipt(work_id)
        if path.exists():
            raise FileExistsError("operation already assigned; use recover, never relaunch")
        receipt = {"work_id": work_id, "phase": phase, "status": "ASSIGNED", "started_at": self.clock()}
        P.write(path, receipt)
        self._active_work = work_id
        try:
            yield receipt
            receipt["ended_at"] = self.clock()
            P.write(path, receipt)
        except BaseException as exc:
            receipt.update(status="HALTED", reason=f"{type(exc).__name__}: {exc}", ended_at=self.clock())
            P.write(path, receipt)
            raise
        finally:
            self._active_work = "idle"

    def _boundary(self, row, output):
        bdir = Path(row["boundary"])
        inspected = P.inspect_boundary(bdir)
        if not inspected["offline_eligible"]:
            raise ValueError(f"boundary invalid: {inspected['problems']}")
        if P.read(bdir.parent / "run.json").get("task", {}).get("task_id") != row["task_id"]:
            raise ValueError("boundary belongs to another task")
        return P.snapshot_boundary(bdir, output, expected_hashes=inspected["hashes"])

    @staticmethod
    def _summary(bdir):
        metadata = P.read(bdir / "boundary/boundary.json")
        rows = CC.read_jsonl((bdir / "native/transcript.jsonl").read_text(), strict=True)
        summary = next(r["message"]["content"] for r in rows if r.get("uuid") == metadata["native_summary_uuid"])
        return summary, P.read(bdir / "native/hook_payload.json")["compact_summary"]

    def step0(self, *, work_id, boundaries=None):
        with self._phase(work_id, "step0") as receipt:
            candidates = boundaries if boundaries is not None else self.snapshot()["boundaries"]
            candidates = [r for r in candidates if r.get("live_eligible")]
            if not candidates:
                receipt.update(status="FAIL", reason_code="eligibility_missing", reason="no live eligible boundary")
            else:
                reports = []
                for attempt in range(2):
                    row = candidates[min(attempt, len(candidates) - 1)]
                    rate = self.guard("step0", work_id)
                    report = (self.io.get("step0") or P.run_step0)(Path(row["boundary"]), self.root / "step0",
                        spec=self.load_spec(row["task_id"]), task=self.task_loader(row["task_id"]), token=self.token,
                        rate_snapshot=rate, go_from_jacob=True, reviewed_build=True, host=self.host,
                        attempt=f"{work_id}-{attempt + 1}",
                        judge=lambda request, identity=f"{work_id}-{attempt + 1}:probe": self.judge(
                            request, attempt_id=identity), budget=self.budget, clock=self.clock,
                        native_cfg=self._cc_config(), rate_provider=lambda: self.max_reading(force=True))
                    reports.append(report)
                    self._rate = None
                    if any((report.get(k) or {}).get("result", {}).get("policy_stop")
                           for k in ("probe", "continuation")) or report.get("withheld"):
                        raise RuntimeError("Step 0 policy/rate hold; retry withheld")
                    if report["status"] == "PASS":
                        break
                    if any((report.get(k) or {}).get("checks", {}).get("cleanup") is False
                           for k in ("probe", "continuation")):
                        raise RuntimeError("Step 0 cleanup unconfirmed")
                receipt.update(status=reports[-1]["status"], reports=reports,
                               receipt_summary=reports[-1].get("receipt_summary", {}),
                               boundaries=candidates, passed_at=self.clock() if reports[-1]["status"] == "PASS" else None)
        return receipt

    def _fork(self, **kwargs):
        previous, self._active_attempt = self._active_attempt, kwargs["identity"]
        try:
            return self._fork_assigned(**kwargs)
        finally:
            self._active_attempt = previous

    def _fork_assigned(self, *, identity, row, bdir, summary, prompt, required=(), forbidden=(), score=True):
        before = self.guard("decision_probe")
        E.resource_guard(self.host) if "fork" not in self.io else None
        out = self.root / "forks" / identity
        result = (self.io.get("fork") or CC.fork)(bdir, out, new_summary=summary, prompt=prompt,
            sim_model=CC.NoSimulatorModel(), token=self.token, persona=self.task_loader(row["task_id"]).persona,
            cfg=self._cc_config(probe=True), host=self.host, probe=True,
            max_calls=PC.MAX_CALLS, timeout_s=max(1, min(1800, int(FINAL_CUTOFF - self.clock()))),
            required_context=required, forbidden_context=forbidden)
        self._rate = result.get("rate_limit") or None
        safe = (not result.get("error") and not result.get("auth_error")
                and (result.get("fork_check") or {}).get("ok") is True
                and result.get("cleanup_unconfirmed") in (False, [])
                and "cleanup_unconfirmed" in result and result.get("sim_calls") == 0
                and result.get("sim_cost_usd") == 0)
        global_stop = ("cleanup_unconfirmed" not in result or result["cleanup_unconfirmed"] not in (False, [])
            or result.get("sim_calls") != 0 or result.get("sim_cost_usd") != 0
            or result.get("auth_error") or result.get("policy_stop") or result.get("paid_route")
            or result.get("outcome") in {"run_cap", "time_cap", "auth_error", "paid_route"})
        safe = safe and not global_stop and result.get("outcome") in (None, "ask_user", "submission", "call_cap", "ok")
        context_ok = (result.get("context_check") or {}).get("ok") is True
        report = {"attempt_id": identity, "safe": safe, "context_ok": context_ok,
                  "usage": P._usage(result, before, self.clock()), "output": str(out), "scores": {},
                  "probe_window": PC.PROBE_WINDOW,
                  "probe_compactions": result.get("probe_compactions", []),
                  "probe_compaction_count": result.get("probe_compaction_count", 0),
                  "probe_compacted": result.get("probe_compacted", False)}
        # Compaction is diagnostic only: preserve each assigned fork and its original score.
        if score and not global_stop:
            evidence = P.read(out / "evidence.json")
            complete = safe and context_ok and evidence.get("part_a_complete") is True
            report["scores"] = {o["kind"]: S.score(evidence["events"], o["scorer"], judge=self.judge,
                                                  capture_complete=complete)
                                for o in self.load_spec(row["task_id"])["obligations"]}
            report["recovery_reads"] = [ev for ev in evidence["events"] if ev["kind"] in {"read", "command"}
                and ("plan" in json.dumps(ev["data"]).lower() or "git log" in ev["data"].get("command", ""))]
        P.write(out / "runtime-report.json", report)
        self.budget.finish(identity, "complete" if safe and context_ok else "technical_failure", artifact=str(out))
        if global_stop:
            raise RuntimeError(f"unsafe probe {identity}; sequence halted")
        if not score and (not safe or not context_ok):
            raise TaskTerminal(f"baseline technical_failure: {identity}; cleanup confirmed, zero simulator")
        if not safe:
            raise RuntimeError(f"failed probe {identity}; sequence halted")
        if not context_ok:
            raise RuntimeError(f"actual resumed context verification failed: {identity}")
        return report

    def screen(self, *, work_id, boundaries, single_task=False):
        with self._phase(work_id, "screen") as receipt:
            if len(boundaries) != 2 or len({r["boundary"] for r in boundaries}) != 2:
                raise ValueError("screen requires two different eligible boundaries")
            tasks = {r["task_id"] for r in boundaries}
            if len(tasks) != (1 if single_task else 2) or not all(r.get("live_eligible") for r in boundaries):
                raise ValueError("screen task/boundary selection violates frozen protocol")
            prepared = []
            for i, row in enumerate(boundaries):
                bdir = self._boundary(row, self._receipt(work_id).parent / f"source-{i}")
                summary, body = self._summary(bdir)
                spec = self.load_spec(row["task_id"])
                markers = [o["text"] for o in spec["obligations"]]
                markers += [o["scorer"].get("prefix", o["scorer"].get("command")) for o in spec["obligations"]]
                # The preserved context (including attachments) must be clean before assignment.
                def strings(value):
                    if isinstance(value, str):
                        yield value
                    elif isinstance(value, dict):
                        for child in value.values():
                            yield from strings(child)
                    elif isinstance(value, list):
                        for child in value:
                            yield from strings(child)
                context = list(strings(P.read(bdir / "boundary/post_messages.json")))
                if any(m in text for m in markers for text in [summary, spec["probe_message"], *context]):
                    raise ValueError("screen source contains rule leakage; no automatic deletion/redesign")
                prepared.append((row, bdir, summary, body, spec, markers))
            roster = [{"id": f"{work_id}-b{i}-{version}-{repeat}", "boundary": row["boundary"],
                       "version": version, "task_id": row["task_id"], "index": i}
                      for i, (row, *_rest) in enumerate(prepared)
                      for version in ("with_rule", "without_rule") for repeat in range(3)]
            self.budget.reserve_batch([{"attempt_id": r["id"], "part": "A", "bucket": "screen",
                                        "boundary": r["boundary"], "version": r["version"]} for r in roster])
            P.write(self._receipt(work_id).parent / "roster.json", roster)
            reports = {}
            for assigned in roster:
                row, bdir, summary, body, spec, markers = prepared[assigned["index"]]
                lines = [o["text"] for o in spec["obligations"]]
                with_rule = assigned["version"] == "with_rule"
                report = self._fork(identity=assigned["id"], row=row, bdir=bdir,
                    summary=CC.insert_summary_lines(summary, body, lines) if with_rule else summary,
                    prompt=spec["probe_message"], required=lines if with_rule else (),
                    forbidden=() if with_rule else markers)
                reports[assigned["id"]] = report
            kinds = {o["kind"] for o in prepared[0][4]["obligations"]}
            results = {kind: S.sensitivity_screen(roster, {identity: r["scores"][kind]
                        for identity, r in reports.items()}) for kind in sorted(kinds)}
            passed = [k for k, result in results.items() if result["passes"]]
            failed = [k for k, result in results.items() if not result["passes"]]
            receipt.update(status="PASS" if passed else "HALTED", passes=not failed, assigned=12,
                           passed_kinds=passed, failed_kinds=failed,
                           kinds=results, reason="failed-kind redesign deferred to coordinator" if failed else None,
                           single_task=single_task, boundaries=boundaries)
        return receipt

    def _cc_config(self, deadline=FINAL_CUTOFF, *, probe=False):
        return CC.CCConfig(auto_compact_window=PC.PROBE_WINDOW if probe else PC.WINDOW, max_window_utilization=.90,
                           deadline_epoch=min(deadline, FINAL_CUTOFF),
                           stop_file=str(STOP_FILE))

    def _simulator(self, name, cap, contingency):
        if "simulator" in self.io:
            return self.io["simulator"](name, cap, contingency)
        from compaction.s2.model import OpenAICompatModel, RunSpend
        spend = self.spend
        if contingency:
            spend = IR.PilotSpend(tags=(PC.SPEND_TAG, CONTINGENCY_TAG),
                caps={PC.SPEND_TAG: PC.SIM_CAP_USD, CONTINGENCY_TAG: 6.},
                stage=self.spend.stage, ledger=self.spend.ledger, caps_file=self.spend.caps_file)
        run_spend = RunSpend(spend, cap).attach(self.root / "spend" / f"{name}.jsonl")
        return OpenAICompatModel(C.SIM_ROUTE, run_spend)

    def _delivery_filter(self, spec, kinds):
        known = {o["kind"] for o in spec["obligations"]}
        if kinds is None:
            return known
        if not kinds or len(kinds) != len(set(kinds)) or not set(kinds) <= known:
            raise ValueError("chain delivery requires a nonempty subset of the frozen obligation kinds")
        return set(kinds)

    def _filtered_resume(self, boundary, task_id, kinds, identity):
        """Suppress only future failed-kind deliveries in a private launch copy.

        Conversation, delivered text, ask/io logs and earlier boundary records are
        immutable. Keeping a non-null PlantingSchedule preserves UserSim's system
        prompt; native fork_check still verifies the original simulator probe hash.
        """
        spec = self.load_spec(task_id)
        enabled = self._delivery_filter(spec, kinds)
        copied = self._boundary({"boundary": str(boundary), "task_id": task_id},
                                self.root / "resume-sources" / identity)
        path = copied / "boundary/sim_state.json"
        exported = P.read(path)
        original_hash = R.digest(exported)
        planting = exported.get("planting")
        if not isinstance(planting, dict):
            raise TypeError("resumed chain lacks planting provenance")
        ids = {o["obligation_id"]: o for o in spec["obligations"]}
        entries = planting["entries"]
        if any(e["obligation_id"] not in ids or e["text"] != ids[e["obligation_id"]]["text"]
               or e["trigger"] != ids[e["obligation_id"]]["trigger"] for e in entries):
            raise ValueError("resumed schedule differs from frozen rule bytes/trigger")
        delivered = planting.get("deliveries", {})
        retained = [e for e in entries if ids[e["obligation_id"]]["kind"] in enabled
                    or e["obligation_id"] in delivered]
        suppressed = [e["obligation_id"] for e in entries if e not in retained]
        planting["entries"] = retained
        PlantingSchedule.restore(planting)  # validate history before any model/container call
        P.write(path, exported)
        P.write(copied / "delivery-filter.json", {"enabled_kinds": sorted(enabled),
            "source_boundary": str(boundary), "source_sim_state_sha256": original_hash,
            "filtered_sim_state_sha256": R.digest(exported), "suppressed_pending_ids": suppressed,
            "retained_failed_deliveries": [oid for oid in delivered if ids[oid]["kind"] not in enabled],
            "history_unchanged": True, "passing_text_and_order_unchanged": True})
        return copied

    def _base_segment(self, task_id, identity, *, boundary=None, deadline=FINAL_CUTOFF,
                      contingency=False, simulator_cap_usd=25., kinds=None):
        identity = f"{identity}-{task_id.split('-')[0]}"
        self.guard("base_continuation" if boundary else "base_run")
        if self.clock() >= deadline:
            return {"status": "TIME_LIMIT", "boundaries": []}
        if "base" not in self.io:
            E.resource_guard(self.host)
        remaining = min(simulator_cap_usd, PC.SIM_CAP_USD - sum(self._totals()))
        if contingency:
            remaining = min(remaining, 6. - sum(self._totals(CONTINGENCY_TAG)))
        if remaining <= 0:
            return {"status": "SPEND_LIMIT", "boundaries": []}
        spec = self.load_spec(task_id)
        enabled = self._delivery_filter(spec, kinds)
        schedule = PlantingSchedule([{k: o[k] for k in ("obligation_id", "text", "trigger")}
                                     for o in spec["obligations"] if o["kind"] in enabled])
        if boundary and kinds is not None:
            boundary = self._filtered_resume(boundary, task_id, kinds, identity)
        cfg = CC.BaseConfig(task=self.task_loader(task_id), root=self.root / "bases", prefix=f"{identity}-",
            cc=self._cc_config(deadline), host=self.host, sim_cap_usd=remaining,
            stop_after=None if contingency else 1,
            stop_first_eligible=contingency, planting=schedule, steps=(CC.GRADED_STEP,) if boundary
            else ("01_plan", CC.GRADED_STEP), cpus=N.AGENT_CPUS, memory_mb=N.AGENT_MEMORY_MB,
            min_free_gb=N.AGENT_MIN_FREE_GB,
            step_timeout_s={"01_plan": min(1800, max(1, int(deadline - self.clock()))),
                            CC.GRADED_STEP: min(7200, max(1, int(deadline - self.clock())))})
        state = self._state()
        state["base_attempts"].append({"id": identity, "task_id": task_id, "run_dir": str(cfg.run_dir),
                                       "resume_boundary": str(boundary) if boundary else None,
                                       "status": "ASSIGNED", "simulator_cap": remaining,
                                       "enabled_delivery_kinds": sorted(enabled)})
        self._save(state)
        raw_sim = self._simulator(identity, remaining, contingency)
        runtime = self

        class GuardedSimulator:
            def __getattr__(self, name):
                return getattr(raw_sim, name)

            def complete(self, *args, **kwargs):
                from compaction.s2.model import StageCapReached
                try:
                    if runtime.clock() >= deadline:
                        raise RuntimeError("base/contingency deadline reached")
                    runtime.guard("simulator_request")
                except Exception as exc:
                    # Native SimServer's on_cap hook stops the CLI on this typed
                    # terminal exception; no paid request has been issued.
                    raise StageCapReached(f"simulator guard stopped request: {exc}") from exc
                return raw_sim.complete(*args, **kwargs)

        sim = GuardedSimulator()
        if boundary:
            runner = self.io.get("continue") or CC.continue_from_boundary
            result = runner(Path(boundary), cfg.run_dir, cfg=cfg, sim_model=sim, token=self.token)
        else:
            result = (self.io.get("base") or CC.run_base)(cfg, sim_model=sim, token=self.token)
        self._rate = result.get("rate_limit") or next((r.get("rate_limit") for r in
            result.get("steps", {}).values() if r.get("rate_limit")), None)
        if "cleanup_unconfirmed" not in result or result["cleanup_unconfirmed"] not in (False, []):
            raise RuntimeError("base cleanup unconfirmed")
        contingency_timeout = contingency and result.get("policy_stop") == "deadline"
        if result.get("auth_error") or result.get("policy_stop") and not contingency_timeout:
            raise RuntimeError(f"base safety stop: {result.get('end')}")
        boundaries = []
        for k in result.get("boundaries", []):
            bdir = cfg.run_dir / f"b{k}"
            validation = (self.io.get("validator") or E.validate_boundary)(bdir,
                self.root / "validation" / f"{identity}-b{k}", task=cfg.task,
                go_from_jacob=True, host=self.host)
            if validation.get("cleanup_unconfirmed"):
                raise RuntimeError("eligibility cleanup unconfirmed")
            boundaries.append({"task_id": task_id, "boundary": str(bdir),
                "live_eligible": validation.get("eligible") is True and (not contingency or k == 1),
                "contingency_first_boundary_required": contingency and k != 1, "validation": validation,
                "k": k, "origin": "planted_base"})
        state = self._state()
        state["boundaries"].extend(boundaries)
        state["base_attempts"][-1].update(status="COMPLETE", result=result)
        self._save(state)
        return {"status": "time_cap" if contingency_timeout else result.get("end"), "boundaries": boundaries,
                "result": result, "run_dir": str(cfg.run_dir)}

    def contingency(self, *, work_id, missing_tasks, deadline, simulator_cap_usd=6.):
        with self._phase(work_id, "contingency") as receipt:
            if isinstance(deadline, datetime):
                deadline = deadline.timestamp()
            deadline = min(epoch(deadline), self.clock() + 3 * 3600, FINAL_CUTOFF)
            existing = {b["task_id"] for b in self.snapshot()["boundaries"] if b["live_eligible"]}
            if (not 1 <= len(missing_tasks) <= 2 or len(set(missing_tasks)) != len(missing_tasks)
                    or any(t in existing or t not in self.snapshot()["active_tasks"] for t in missing_tasks)):
                raise ValueError("contingency runs only one or two missing active tasks")
            if not 0 < simulator_cap_usd <= 6:
                raise ValueError("contingency simulator cap cannot exceed $6")
            attempts = []
            exhausted = False
            for original in missing_tasks:
                task_id, retry = original, 0
                while not exhausted:
                    if self.clock() >= deadline or sum(self._totals(CONTINGENCY_TAG)) >= simulator_cap_usd:
                        exhausted = True
                        break
                    result = self._base_segment(task_id, f"{work_id}-{len(attempts)}", deadline=deadline,
                        contingency=True, simulator_cap_usd=simulator_cap_usd - sum(self._totals(CONTINGENCY_TAG)))
                    attempts.append(result)
                    # A cap stop is exhaustion, never permission to repeat a paid run.
                    if result["status"] in {"time_cap", "run_cap", "TIME_LIMIT", "SPEND_LIMIT"}:
                        exhausted = True
                        break
                    if any(b["live_eligible"] and b.get("k") == 1 for b in result["boundaries"]):
                        break
                    if retry == 0:
                        retry = 1
                        continue
                    state = self.cohort.state()
                    used = set(state["initial_cohort"]) | {r["new"] for r in state["replacements"]}
                    replacement = next((t for t in PC.task_order() if t not in used), None)
                    if replacement is None:
                        exhausted = True
                        break
                    self.cohort.replace_task(task_id, replacement,
                        reason="contingency base error/ineligible compaction after one rerun")
                    task_id, retry = replacement, 0
                    if any(b["task_id"] == task_id and b["live_eligible"] for b in self.snapshot()["boundaries"]):
                        break  # existing eligible replacement needs no contingency base
            receipt.update(status="PASS", attempts=attempts, boundaries=self.snapshot()["boundaries"],
                           exhausted=exhausted)
        return receipt

    def _retention(self, row):
        """No baseline reader here: use only the pre-boundary simulator/context files."""
        bdir, task_id = Path(row["boundary"]), row["task_id"]
        spec, exported = self.load_spec(task_id), P.read(bdir / "boundary/sim_state.json")
        planting = exported.get("planting")
        if not planting:
            raise ValueError("chain boundary lacks planting provenance")
        evidence = R.boundary_evidence(bdir, fork_validation={"image_present": row["live_eligible"],
            "transcript_intact": True, "fork_check_ok": row["live_eligible"]})
        rows = CC.read_jsonl((bdir / "native/transcript.jsonl").read_text(), strict=True)
        events = CC.ordered_evidence(rows)["events"]
        # Successful native commit summary lines can establish a commit; all other
        # unresolved commit invocations stay unknown instead of becoming no commit.
        from compaction.s2.planted_specs import _commit_evidence
        expanded = []
        for event in events:
            expanded.append(event)
            if event["kind"] == "command":
                data = event["data"]
                commits = _commit_evidence(data.get("command", ""), data.get("output", ""))
                expanded.extend({"kind": "commit", "data": item} for item in commits)
                if "git commit" in data.get("command", "") and not commits:
                    expanded.append({"kind": "commit", "data": {}})
        for seq, event in enumerate(expanded):
            event["seq"] = seq
        grades = []
        for obligation in spec["obligations"]:
            oid = obligation["obligation_id"]
            delivery = planting.get("deliveries", {}).get(oid)
            delivery_ids = set()
            if delivery:
                reply = next((r.get("reply") for r in exported.get("ask_log", [])
                              if r.get("call_index") == delivery["call_index"]), None)
                for native in rows:
                    content = (native.get("message") or {}).get("content")
                    for block in content if isinstance(content, list) else []:
                        if block.get("type") == "tool_result" and reply and CC.unwrap_ask(
                                CC._block_text(block.get("content"))) == reply:
                            delivery_ids.add(block.get("tool_use_id"))
            delivery_seq = next((ev["seq"] for ev in expanded if ev["kind"] == "ask_user"
                                 and ev["data"].get("tool_call_id") in delivery_ids), None)
            adoption = (S.adoption(expanded, obligation["scorer"], delivery_seq=delivery_seq,
                                  judge=self.judge) if delivery_seq is not None else {"adoption": "unknown"})
            rule = obligation | {"delivered": bool(delivery)} | R.adoption_fields(adoption, len(expanded))
            grade = R.grade(evidence, rule, self.judge)
            grade.update(adoption_result=adoption, delivery=delivery,
                         token_distance_record=next((b for b in planting.get("boundaries", [])
                            if b.get("compaction_index") == evidence["k"]), None))
            grades.append(grade)
        path = bdir / "planted-retention.json"
        P.write(path, {"grades": grades, "baseline_outputs_inspected": False})
        state = self._state()
        state["retention"].append({"boundary": row["boundary"], "path": str(path)})
        self._save(state)
        return path

    def _baseline(self, row, identity):
        out = self.root / "baselines" / identity
        bdir = self._boundary(row, out / "source")
        summary, _ = self._summary(bdir)
        spec = self.load_spec(row["task_id"])
        ids = [f"{identity}-r{i}" for i in range(2)]
        self.budget.reserve_batch([{"attempt_id": key, "part": "A", "bucket": "baseline",
                                    "boundary": row["boundary"]} for key in ids])
        failures = []
        try:
            for key in ids:
                try:
                    self._fork(identity=key, row=row, bdir=bdir, summary=summary,
                               prompt=spec["probe_message"], score=False)
                except TaskTerminal as exc:
                    failures.append(str(exc))
        finally:
            # Only hash bytes. No scorer and no parsing of transcript/evidence outputs.
            paths = sorted(p for key in ids for p in (self.root / "forks" / key).rglob("*") if p.is_file())
            R.seal_probes(out / "sealed-probes.json", paths)
        if failures:
            raise TaskTerminal(f"{'; '.join(failures)}; sealed at {out / 'sealed-probes.json'}")
        return str(out / "sealed-probes.json")

    def chains(self, *, work_id, tasks, target_compaction=3, kinds=None):
        with self._phase(work_id, "chains") as receipt:
            if target_compaction != 3 or not 1 <= len(tasks) <= 3 or len(set(tasks)) != len(tasks):
                raise ValueError("chains require one to three tasks and exactly three compactions")
            if any(t not in self.snapshot()["active_tasks"] for t in tasks):
                raise ValueError("chain task outside active cohort")
            results = []
            for original in tasks:
                task_id, retries = original, 0
                task_status = "target_reached"
                current = [b for b in self._state()["boundaries"]
                           if b["task_id"] == task_id and b.get("origin") == "planted_base" and b["live_eligible"]]
                boundary = max(current, key=lambda b: b["k"]) if current else None
                try:
                    while True:
                        if boundary is None:
                            segment = self._base_segment(task_id, f"{work_id}-{len(results)}-try{retries}", kinds=kinds)
                            if segment["status"] in {"run_cap", "time_cap", "TIME_LIMIT", "SPEND_LIMIT"}:
                                raise RuntimeError("base cap/timeout stop; not restarted")
                            first = next((b for b in segment["boundaries"] if b.get("k") == 1), None)
                            if first is None or not first["live_eligible"]:
                                if retries == 0:
                                    retries = 1
                                    continue
                                state = self.cohort.state()
                                used = set(state["initial_cohort"]) | {r["new"] for r in state["replacements"]}
                                replacement = next((t for t in PC.task_order() if t not in used), None)
                                if replacement is None:
                                    raise RuntimeError("all seeded task replacements exhausted")
                                self.cohort.replace_task(task_id, replacement,
                                    reason="base error or ineligible first compaction after one rerun")
                                task_id, retries = replacement, 0
                                continue
                            boundary = first
                        state = self._state()
                        already = state["chains"].get(boundary["boundary"])
                        if not already:
                            retention_path = self._retention(boundary)
                            if not boundary["live_eligible"]:
                                state = self._state()
                                state["chains"][boundary["boundary"]] = {"unforkable": True, "retention": str(retention_path)}
                                self._save(state)
                                raise TaskTerminal("later boundary unforkable: retention retained; unsafe restore withheld")
                            seal = self._baseline(boundary, f"{work_id}-{len(results)}-b{boundary['k']}")
                            state = self._state()
                            state["chains"][boundary["boundary"]] = {"retention": str(retention_path), "seal": seal}
                            self._save(state)
                        if boundary["k"] >= target_compaction:
                            break
                        segment = self._base_segment(task_id, f"{work_id}-{len(results)}-next{boundary['k']}",
                                                     boundary=boundary["boundary"], kinds=kinds)
                        newer = [b for b in segment["boundaries"] if b["k"] > boundary["k"]]
                        if segment["status"] in {"run_cap", "time_cap", "TIME_LIMIT", "SPEND_LIMIT"}:
                            raise RuntimeError("base cap/timeout stop; not restarted")
                        if not newer:
                            if segment["status"] == "submitted":
                                task_status = "submitted"
                                break
                            raise TaskTerminal(f"base continuation ended before another compaction: {segment['status']}")
                        boundary = min(newer, key=lambda b: b["k"])
                    results.append({"task_id": task_id, "last_compaction": boundary["k"],
                                    "target_reached": boundary["k"] >= target_compaction, "status": task_status})
                except TaskTerminal as exc:
                    results.append({"task_id": task_id, "last_compaction": boundary["k"] if boundary else 0,
                                    "target_reached": False, "status": "technical_failure", "reason": str(exc)})
                    receipt["tasks"] = results
                    P.write(self._receipt(work_id), receipt)
            receipt.update(status="PASS", tasks=results, stop="BEFORE_CARDS", screened_kinds=kinds,
                           retention=self._state()["retention"], baseline_outputs_inspected=False,
                           delivery_policy="Only passing kinds are newly planted. Already-delivered contingency "
                           "rules remain immutable history; undelivered failed kinds are suppressed on resume. "
                           "Only screened_kinds are interpretable behavioral assays.")
        return receipt
