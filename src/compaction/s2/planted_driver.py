"""Desktop-only durable orchestration through chains, stopping before human cards.

A work assignment is fsynced before invoking a runtime. On restart only a terminal
receipt may resolve it: an interrupted operation is never blindly replayed. Runtimes
own per-fork rosters and call ``before_action`` before every model/simulator launch.
This driver owns the process lock, absolute time gates, and phase transitions.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from compaction.s2.planted_config import B_CUTOFF, B_DROP_IF_STEP0_AFTER, RESET, task_order

MONDAY_NOON = datetime(2026, 10, 26, 16, tzinfo=UTC)
RESET_SETTLE_SECONDS = 300
RATE_RETRY_SECONDS = 600
MAX_RATE_RETRIES = 6
TERMINAL = {"HALTED", "STOPPED_BEFORE_CARDS"}


class DriverStop(RuntimeError):
    """A planned stop, propagated through runtime cleanup rather than retried."""


class RateHold(DriverStop):
    """A measured rate hold; retryable only before an operation is assigned."""

    def __init__(self, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity(checkout: Path) -> dict:
    machine_path = Path("/etc/machine-id")
    machine = machine_path.read_text().strip() if machine_path.exists() else socket.gethostname()
    return {"hostname": socket.gethostname(), "checkout": str(checkout.resolve()),
            "machine_sha256": hashlib.sha256(machine.encode()).hexdigest()}


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DriverStop("another driver owns the process lock") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()) + "\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def measured_rate(snapshot: dict, now: float) -> dict:
    """Accept the native timestamped rate event, never a relabelled old reading."""
    observed = snapshot.get("observed_at")
    if not isinstance(observed, (int, float)) or not 0 <= now - observed <= 300:
        raise RateHold("fresh Max reading required (at most 300 seconds old)")
    info = snapshot.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows") or {info.get("rateLimitType"): info}
    seven = windows.get("seven_day") or {}
    utilization, reset = seven.get("utilization"), seven.get("resetsAt")
    if (not isinstance(utilization, (int, float)) or not math.isfinite(utilization)
            or not 0 <= utilization <= 1 or not isinstance(reset, (int, float))):
        raise DriverStop("Max reading lacks active seven-day utilization/reset")
    if reset <= now:
        raise RateHold("Max server has not rolled over the seven-day window")
    for name, window in windows.items():
        if not isinstance(window, dict):
            raise DriverStop("invalid Max window")
        window_reset = window.get("resetsAt")
        if isinstance(window_reset, (int, float)) and window_reset <= now:
            continue
        used = window.get("utilization")
        if name == "seven_day" and isinstance(used, (int, float)) and used >= .90:
            raise RateHold("Max 90% weekly hold")
        if window.get("status") in {"rejected", "rate_limited"}:
            retry_at = (window_reset + RESET_SETTLE_SECONDS
                        if name == "five_hour" and isinstance(window_reset, (int, float))
                        and math.isfinite(window_reset) else None)
            raise RateHold("Max rate-limit hold", retry_at=retry_at)
    return {"utilization": utilization, "observed_at": observed, "resetsAt": reset,
            "five_hour_utilization": (windows.get("five_hour") or {}).get("utilization"),
            "five_hour_resetsAt": (windows.get("five_hour") or {}).get("resetsAt")}


class Driver:
    def __init__(self, runtime, *, checkout: Path, expected_hostname: str,
                 part_b_factory: Callable | None = None, clock: Callable = time.time,
                 sleeper: Callable = time.sleep, identity_provider: Callable = identity,
                 configuration: dict | None = None):
        self.runtime, self.part_b_factory = runtime, part_b_factory
        self.checkout = Path(checkout).resolve()
        self.directory = self.checkout / "logs/s2"
        self.state_path = self.directory / "planted_run_state.json"
        self.status_path = self.directory / "planted_run_status.md"
        self.owner_path = self.directory / "planted_run_owner.json"
        self.stop_path = self.directory / "planted_run.stop"
        self.expected_hostname = expected_hostname
        self.clock, self.sleeper, self.identity_provider = clock, sleeper, identity_provider
        self.configuration_sha256 = hashlib.sha256(json.dumps(configuration or {}, sort_keys=True).encode()).hexdigest()
        self.retry_authorization = (configuration or {}).get("step0_retry_authorization")
        if self.retry_authorization is not None:
            approval = self.retry_authorization
            if (not isinstance(approval, dict) or not approval.get("decision_id")
                    or not isinstance(approval.get("from_work_id"), str)
                    or approval.get("max_operations") != 1
                    or type(approval.get("after_completed_retry_count")) is not int
                    or approval["after_completed_retry_count"] < 1):
                raise ValueError("invalid explicit Step 0 retry authorization")
            if (type(approval.get("required_pairs", 1)) is not int
                    or approval.get("required_pairs", 1) not in (1, 2)):
                raise ValueError("invalid Step 0 required pair count")
        self.state = None
        self._part_b = None
        self._previous_handlers = {}
        self._save_lock = threading.RLock()

    def _owner(self, claim: bool):
        current = self.identity_provider(self.checkout)
        if not self.expected_hostname or current["hostname"] != self.expected_hostname:
            raise DriverStop("this host is not the designated desktop launcher")
        if self.owner_path.exists():
            owner = json.loads(self.owner_path.read_text())
            if owner["identity"] != current:
                raise DriverStop("sole-owner manifest belongs to another host or checkout")
        elif claim:
            atomic_json(self.owner_path, {"schema": 1, "identity": current,
                        "claimed_at": utc(self.clock()), "role": "sole pilot launcher and ledger owner"})
        else:
            raise DriverStop("missing sole-owner manifest; claim it explicitly on the desktop")

    def _load(self):
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if self.state.get("configuration_sha256") != self.configuration_sha256:
                raise DriverStop("driver configuration changed since arming")
            if self.state.get("schema") != 1:
                raise DriverStop("unknown driver state schema")
        else:
            self.state = {"schema": 1, "configuration_sha256": self.configuration_sha256, "status": "ARMED", "phase": "step0", "sequence": 0,
                          "events": [], "completed": [], "pending": None,
                          "single_task": False, "contingency_used": False,
                          "created_at": utc(self.clock())}

    def _save(self):
        with self._save_lock:
            self.state["updated_at"] = utc(self.clock())
            atomic_json(self.state_path, self.state)

    def _metrics(self) -> dict:
        snapshot = self.runtime.snapshot()
        for key in ("forks_a", "forks_b", "forks_total", "simulator_usd"):
            value = snapshot.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise DriverStop(f"missing or invalid budget metric: {key}")
        if snapshot["forks_total"] != snapshot["forks_a"] + snapshot["forks_b"]:
            raise DriverStop("fork counters disagree")
        # Detailed validation evidence stays in the runtime/eligibility artifacts.
        # Repeating it at every guard made the durable state grow quadratically.
        snapshot["boundaries"] = [{k: row[k] for k in
            ("task_id", "boundary", "live_eligible", "origin", "k") if k in row}
            for row in snapshot.get("boundaries", [])]
        self.state["metrics"] = snapshot
        return snapshot

    def _event(self, message: str, *, result=None, hold: bool = False):
        entry = {"at": utc(self.clock()), "phase": self.state["phase"], "message": message,
                 "metrics": {k: v for k, v in (self.state.get("metrics") or {}).items()
                             if k != "boundaries"}, "max": self.state.get("max")}
        if result is not None:
            entry["result"] = result
        self.state["events"].append(entry)
        self._save()
        m, rate = entry["metrics"] or {}, entry["max"] or {}
        # The watcher anchors at column zero; Markdown bullets hid prior stops.
        # Keep the durable event unchanged and prefix every physical status line,
        # including multiline exception/receipt details.
        status = self.state["status"]
        prefix = ("STOP: " if status in TERMINAL else "HOLD: " if hold
                  or status in {"WAIT_MAX", "WAIT_RESET"}
                  or isinstance(result, dict) and result.get("status") == "HOLD" else "- ")
        line = (f"{entry['at']} {entry['phase']}: {message}; "
                f"forks A {m.get('forks_a', '?')}/60, B {m.get('forks_b', '?')}/34, "
                f"total {m.get('forks_total', '?')}/94; "
                f"remaining {94 - m['forks_total'] if 'forks_total' in m else '?'}; "
                f"simulator ${m.get('simulator_usd', '?')}; Max weekly {rate.get('utilization', '?')}, "
                f"five-hour {rate.get('five_hour_utilization', '?')}\n")
        with self.status_path.open("a") as handle:
            handle.write("".join(prefix + part + "\n" for part in line.splitlines()))
            handle.flush()
            os.fsync(handle.fileno())

    def _stop_check(self):
        if self.stop_path.exists():
            raise DriverStop("operator stop file")
        if self.clock() >= B_CUTOFF.timestamp():
            raise DriverStop("Tuesday 18:00 ET cutoff; no further runs")

    def before_action(self, action: dict | None = None):
        """Runtime launch guard; also passed to every Part B model/judge action."""
        self._stop_check()
        now = self.clock()
        if now < RESET.timestamp():
            raise DriverStop("absolute Max reset has not occurred; no experiment or ping permitted")
        metrics = self._metrics()
        attempt_id = (action or {}).get("attempt_id")
        pending = self.state.get("pending") or {}
        budget = getattr(self.runtime, "budget", None)
        assigned = False
        if attempt_id and budget is not None and pending.get("work_id"):
            assigned = any(row["attempt_id"] == attempt_id and row["status"] == "assigned"
                           and row["part"] == ("B" if self.state["phase"] == "part_b" else "A")
                           for row in budget.state()["attempts"])
            assigned = assigned and attempt_id.startswith(pending["work_id"] + "-")
        # Roster allocation consumes slots before execution. Exactly-full counts
        # must still allow the already-assigned final fork and its scoring calls;
        # they never authorize a new base, allocation, or unrelated judge.
        at_cap = metrics["forks_a"] >= 60 or metrics["forks_total"] >= 94
        if self.state["phase"] == "part_b":
            at_cap = at_cap or metrics["forks_b"] >= 34
        over_cap = metrics["forks_a"] > 60 or metrics["forks_b"] > 34 or metrics["forks_total"] > 94
        if metrics["simulator_usd"] >= 25 or over_cap or (at_cap and not assigned):
            raise DriverStop("pilot spend/fork cap reached")
        if self.state["phase"] == "contingency":
            if now >= self.state["contingency_deadline"]:
                raise DriverStop("contingency three-hour deadline reached")
            if metrics["simulator_usd"] - self.state["contingency_start_usd"] >= 6:
                raise DriverStop("contingency $6 ceiling reached")
        raw = self.runtime.max_reading()
        self.state["max_raw"] = raw
        info = raw.get("rate_limit_info") or {}
        windows = info.get("unifiedWindows") or {info.get("rateLimitType"): info}
        # Preserve the held/stale reading too; a stop line must not display the
        # preceding allowed reading as if it were the one that triggered a hold.
        self.state["max"] = {"utilization": (windows.get("seven_day") or {}).get("utilization"),
                             "observed_at": raw.get("observed_at"),
                             "five_hour_utilization": (windows.get("five_hour") or {}).get("utilization"),
                             "five_hour_resetsAt": (windows.get("five_hour") or {}).get("resetsAt")}
        self.state["max"] = measured_rate(raw, self.clock())
        self.state["last_guard"] = {"at": utc(self.clock()), "action": action,
                                    "max": self.state["max"]}
        self._save()

    def _boundaries(self) -> list[dict]:
        metrics = self._metrics()
        active = metrics.get("active_tasks")
        if not isinstance(active, list) or not 1 <= len(active) <= 3 or len(set(active)) != len(active):
            raise DriverStop("missing or invalid active cohort")
        if any(task not in task_order() for task in active):
            raise DriverStop("active cohort differs from frozen population")
        rows, seen = [], set()
        for row in metrics.get("boundaries", []):
            if row.get("task_id") in active and row.get("live_eligible") is True:
                key = (row["task_id"], row["boundary"])
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
        return sorted(rows, key=lambda r: (task_order().index(r["task_id"]), r["boundary"]))

    def _part_b_runtime(self):
        if self._part_b is None:
            if self.part_b_factory is None:
                raise DriverStop("Part B runtime not configured")
            self._part_b = self.part_b_factory(datetime.fromisoformat(self.state["step0_passed_at"]))
        return self._part_b

    def _assign(self, phase: str, kwargs: dict):
        try:
            self.before_action({"action": phase})
        except RateHold as exc:
            # No pending operation or fork has been assigned. A slow server
            # rollover is safe to poll; an in-operation hold is never replayed.
            retries = self.state.get("rate_guard_retries", 0)
            if retries >= MAX_RATE_RETRIES:
                raise
            retry_at = max(self.clock() + RATE_RETRY_SECONDS, exc.retry_at or 0)
            self.state.update(status="WAIT_MAX", rate_guard_retries=retries + 1,
                              rate_retry_at=min(retry_at, B_CUTOFF.timestamp()))
            self._event(f"rate hold before assignment; retry {retries + 1}/{MAX_RATE_RETRIES} "
                        f"at {utc(self.state['rate_retry_at'])}: {exc}")
            return
        self.state.pop("rate_retry_at", None)
        self.state["rate_guard_retries"] = 0
        self.state["sequence"] += 1
        work_id = f"pilot-{self.state['sequence']:04d}-{phase}"
        self.state["pending"] = {"work_id": work_id, "phase": phase, "kwargs": kwargs,
                                 "assigned_at": utc(self.clock())}
        if phase == "step0":
            for authorization in self.state.get("authorized_step0_retries", []):
                if "consumed_by_work_id" not in authorization:
                    authorization["consumed_by_work_id"] = work_id
        self._event(f"assigned {work_id}")
        backend = self._part_b_runtime() if phase == "part_b" else self.runtime
        if phase == "part_b":
            result = backend.run_part_b(work_id=work_id, before_action=self.before_action, **kwargs)
        else:
            call_kwargs = dict(kwargs)
            if phase == "contingency":
                call_kwargs["deadline"] = datetime.fromisoformat(call_kwargs["deadline"])
            result = getattr(backend, phase)(work_id=work_id, **call_kwargs)
        self._complete(result)

    def _complete(self, result: dict):
        pending = self.state["pending"]
        if not isinstance(result, dict) or result.get("status") not in {"PASS", "FAIL", "HALTED", "SKIP", "HOLD"}:
            raise DriverStop("runtime returned no valid terminal receipt")
        if (result.get("work_id", pending["work_id"]) != pending["work_id"]
                or result.get("phase", pending["phase"]) != pending["phase"]):
            raise DriverStop("runtime receipt belongs to another assigned operation")
        self._metrics()
        status, phase = result["status"], pending["phase"]
        stop_reason = None
        if phase == "screen" and status in {"PASS", "FAIL"}:
            known = {"commit_convention", "verification_output"}
            passed = result.get("passed_kinds")
            if passed is None and result.get("passes") is True:
                passed = sorted(known)
            if not isinstance(passed, list) or not set(passed) <= known or not passed:
                stop_reason = "both core kinds failed screen; stop for coordinator redesign/recheck"
            else:
                self.state["usable_kinds"] = passed
                self.state["failed_kinds"] = sorted(known - set(passed))
                self.state["redesign_deferred"] = bool(self.state["failed_kinds"])
                self.state["screen_passed_at"] = self._receipt_time(result, "ended_at")
                self.state["phase"] = "chains"
        elif status not in {"PASS", "SKIP"}:
            # The explicitly authorized two-pair retry halts on every failure,
            # including eligibility loss; contingency must not spend its last reserve.
            if (phase == "step0" and result.get("reason_code") == "eligibility_missing"
                    and pending["kwargs"].get("required_pairs") != 2):
                self.state["phase"] = "contingency"
            else:
                stop_reason = f"{phase} {status}: {result.get('reason', 'gate failure')}"
        elif phase == "step0":
            if status != "PASS":
                stop_reason = "Step 0 cannot be skipped"
            else:
                self.state["step0_passed_at"] = self._receipt_time(result, "passed_at")
                self.state["phase"] = "cohort"
        elif phase == "contingency":
            if status != "PASS":
                stop_reason = "contingency did not complete"
            else:
                self.state["contingency_used"] = True
                self.state["step0_after_contingency"] = True
                self.state["phase"] = "step0"
        elif phase == "part_b":
            self.state["phase"] = "screen"
        elif phase == "chains":
            if status != "PASS":
                stop_reason = "chains did not complete"
            else:
                self.state["status"] = "STOPPED_BEFORE_CARDS"
                self.state["reason"] = "chains complete; cards and repair require Jacob's labels"
        else:
            stop_reason = f"invalid terminal receipt for {phase}"
        if stop_reason:
            self.state["status"] = "HALTED"
            self.state["reason"] = stop_reason
        # Receipt consumption AND next phase are one durable write. A crash cannot
        # leave pending=None with the old phase, which would reassign the same work.
        self.state["completed"].append({**pending, "result": result})
        self.state["pending"] = None
        note = f"{pending['work_id']}: {status} {result.get('reason', '')}"
        if phase == "step0" and result.get("receipt_summary", {}).get("note"):
            note += "; " + result["receipt_summary"]["note"]
        if phase == "screen" and self.state.get("redesign_deferred"):
            note += f"; continue with {self.state['usable_kinds']}; failed-kind redesign deferred to morning"
        if self.state["status"] in TERMINAL:
            note += f"; {self.state['reason']}"
        self._event(note, result=result)

    def _receipt_time(self, result: dict, field: str) -> str:
        """Schedule cuts use actual completion, including on delayed crash recovery."""
        value = result.get(field, result.get("ended_at", self.clock()))
        completed = datetime.fromisoformat(value).timestamp() if isinstance(value, str) else value
        assigned = (self.state.get("pending") or {}).get("assigned_at")
        earliest = datetime.fromisoformat(assigned).timestamp() if assigned else RESET.timestamp()
        if not isinstance(completed, (int, float)) or not earliest <= completed <= self.clock():
            raise DriverStop("receipt completion time is outside its assigned operation")
        return utc(completed)

    def _recover(self):
        pending = self.state.get("pending")
        if pending is None:
            return
        backend = self._part_b_runtime() if pending["phase"] == "part_b" else self.runtime
        receipt = backend.recover(pending["work_id"])
        if receipt is None:
            raise DriverStop(f"interrupted assigned work {pending['work_id']}; no terminal receipt; never replay")
        self._event(f"recovering receipt for {pending['work_id']}")
        self._complete(receipt)

    def _choose_screen(self, boundaries: list[dict]) -> list[dict]:
        if self.state["single_task"]:
            tasks = list(dict.fromkeys(r["task_id"] for r in boundaries))
            rows = [r for r in boundaries if r["task_id"] == tasks[0]] if tasks else []
            if len(rows) < 2:
                raise DriverStop("single-task case study requires two distinct eligible boundaries")
            return rows[:2]
        chosen = {}
        for row in boundaries:
            chosen.setdefault(row["task_id"], row)
        if len(chosen) < 2:
            raise DriverStop("sensitivity screen needs two eligible tasks")
        return list(chosen.values())[:2]

    def tick(self):
        self._stop_check()
        start_at = RESET.timestamp() + RESET_SETTLE_SECONDS
        if self.clock() < start_at:
            if self.state["status"] != "WAIT_RESET":
                self.state["status"] = "WAIT_RESET"
                self._event("waiting for 2026-10-26T00:00Z reset plus five-minute rollover margin; no model calls")
            return min(30, start_at - self.clock())
        if self.clock() < self.state.get("rate_retry_at", 0):
            return min(30, self.state["rate_retry_at"] - self.clock())
        self.state["status"] = "RUNNING"
        self._recover()
        if self.state["status"] in TERMINAL:
            return 0
        phase = self.state["phase"]
        boundaries = self._boundaries()
        tasks = list(dict.fromkeys(r["task_id"] for r in boundaries))
        if phase == "step0":
            approval = next((row for row in self.state.get("authorized_step0_retries", [])
                             if "consumed_by_work_id" not in row), {})
            required_pairs = (approval.get("decision") or {}).get("required_pairs")
            if required_pairs == 2 and len(tasks) < 2:
                raise DriverStop("authorized two-pair Step 0 needs two eligible tasks; no contingency authorized")
            if not boundaries:
                if self.state["contingency_used"]:
                    raise DriverStop("kill 1: no eligible task after contingency")
                self.state["phase"] = "contingency"
                self._save()
            else:
                candidates = boundaries
                if self.state.get("step0_after_contingency"):
                    candidates = [row for row in boundaries if row.get("origin") == "planted_base"]
                    if not candidates:
                        if self.state.get("step0_passed_at"):
                            self.state["phase"] = "cohort"
                            self._event("contingency produced no new boundary; retain prior Step 0 result")
                            return 0
                        candidates = boundaries  # an eligible seeded replacement may already exist
                kwargs = {"boundaries": candidates}
                if required_pairs:
                    kwargs["required_pairs"] = required_pairs
                if required_pairs == 2:
                    kwargs["boundaries"] = [next(row for row in candidates if row["task_id"] == task)
                        for task in dict.fromkeys(row["task_id"] for row in candidates)]
                self._assign("step0", kwargs)
        elif phase == "cohort":
            if len(tasks) >= 2:
                self.state["phase"] = "part_b"
            elif not self.state["contingency_used"]:
                self.state["phase"] = "contingency"
            elif not tasks:
                raise DriverStop("kill 1: no eligible task after contingency")
            elif self.clock() < MONDAY_NOON.timestamp():
                if self.state.get("waiting_until") != MONDAY_NOON.isoformat():
                    self.state["waiting_until"] = MONDAY_NOON.isoformat()
                    self._event("one task eligible; wait for Monday noon single-task decision", hold=True)
                return min(30, MONDAY_NOON.timestamp() - self.clock())
            else:
                self.state["single_task"] = True
                self.state["expansion_gate"] = False
                self.state["phase"] = "part_b"
                self._event("single-task case study; no expansion gate")
            self._save()
        elif phase == "contingency":
            if len(tasks) >= 2:
                self.state["phase"] = "step0" if not self.state.get("step0_passed_at") else "cohort"
                self._save()
            elif self.state["contingency_used"]:
                raise DriverStop("contingency already used; no automatic repetition")
            else:
                metrics = self.state["metrics"]
                self.state["contingency_start_usd"] = metrics["simulator_usd"]
                self.state["contingency_deadline"] = min(self.clock() + 3 * 3600, MONDAY_NOON.timestamp())
                if self.clock() >= self.state["contingency_deadline"]:
                    self.state["contingency_used"] = True
                    self.state["phase"] = "cohort" if self.state.get("step0_passed_at") else "step0"
                    self._save()
                else:
                    missing = [t for t in metrics["active_tasks"] if t not in tasks][:2 - len(tasks)]
                    self._assign("contingency", {"missing_tasks": missing,
                        "deadline": utc(self.state["contingency_deadline"]), "simulator_cap_usd": 6})
        elif phase == "part_b":
            too_late = datetime.fromisoformat(self.state["step0_passed_at"]) > B_DROP_IF_STEP0_AFTER
            if self.state["single_task"] or too_late or self.part_b_factory is None:
                self._event("Part B skipped: single task, Step 0 deadline, or no validated preparations")
                self.state["phase"] = "screen"
                self._save()
            else:
                self._assign("part_b", {"tasks": [t for t in task_order() if t in self.state["metrics"]["active_tasks"]][:2]})
        elif phase == "screen":
            self._assign("screen", {"boundaries": self._choose_screen(boundaries),
                                     "single_task": self.state["single_task"]})
        elif phase == "chains":
            late = datetime.fromisoformat(self.state["screen_passed_at"]) >= MONDAY_NOON
            n = 1 if self.state["single_task"] else 2 if late else 3
            active = self.state["metrics"]["active_tasks"]
            selected = tasks[:1] if self.state["single_task"] else [t for t in task_order() if t in active][:n]
            self._assign("chains", {"tasks": selected, "target_compaction": 3,
                                     "kinds": self.state["usable_kinds"]})
        else:
            raise DriverStop(f"unknown phase {phase}")
        return 0

    def _signal(self, signum, frame):
        raise DriverStop(f"operator signal {signal.Signals(signum).name}")

    def _retry_failed_step0(self, work_id: str):
        """Consume one explicit post-fix authorization, preserving history and spend.

        Binding the request to the failed operation makes the launch command
        one-shot: repeating it after another failure cannot start another retry.
        No assigned operation is replayed; _assign will increment sequence.
        """
        last = (self.state.get("completed") or [{}])[-1]
        history = self.state.get("authorized_step0_retries", [])
        approval = self.retry_authorization or {}
        new_decision = (approval.get("from_work_id") == work_id
                        and approval.get("after_completed_retry_count") == len(history))
        if (self.state.get("status") != "HALTED" or self.state.get("phase") != "step0"
                or self.state.get("pending") is not None
                or last.get("work_id") != work_id or last.get("phase") != "step0"
                or (last.get("result") or {}).get("status") != "FAIL"
                or self.state.get("reason") != "step0 FAIL: gate failure"
                or (history and not new_decision)
                or any(row.get("from_work_id") == work_id for row in history)):
            raise DriverStop("retry requires the exact terminal failed Step 0 receipt; never replay assigned work")
        self._stop_check()
        self._metrics()
        self.state.setdefault("authorized_step0_retries", []).append({
            "from_work_id": work_id, "authorized_at": utc(self.clock()),
            "next_sequence": self.state["sequence"] + 1,
            "prior_reason": self.state["reason"],
            "decision": approval if new_decision else None,
            "scope": "one Step 0 operation, at most two probe/continue pairs"})
        self.state.update(status="ARMED", rate_guard_retries=0)
        self.state.pop("reason", None)
        self.state.pop("rate_retry_at", None)
        self._event(f"explicit post-fix Step 0 retry authorized from {work_id}; "
                    "prior receipts and fork assignments retained")

    def run(self, *, claim_owner=False, install_signals=True,
            retry_step0_from: str | None = None) -> dict:
        with process_lock(self.directory / "planted_run.lock"):
            self._owner(claim_owner)
            self._load()
            if retry_step0_from is not None:
                self._retry_failed_step0(retry_step0_from)
            if self.state["status"] in TERMINAL:
                return self.state
            if install_signals:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    self._previous_handlers[sig] = signal.signal(sig, self._signal)
            self.runtime.before_action = self.before_action
            try:
                self._metrics()
                self._event(f"ARMED pid={os.getpid()}; sole desktop driver")
                while self.state["status"] not in TERMINAL:
                    wait = self.tick()
                    if wait:
                        self.sleeper(wait)
            except Exception as exc:  # noqa: BLE001 -- persist failures; no automatic redesign/retry
                self.state["status"] = "HALTED"
                self.state["reason"] = f"{type(exc).__name__}: {exc}"
                self._event(self.state["reason"])
            finally:
                for sig, previous in self._previous_handlers.items():
                    signal.signal(sig, previous)
                self._previous_handlers.clear()
            return self.state
