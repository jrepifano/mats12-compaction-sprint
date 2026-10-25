"""Frozen pilot allocation and durable fork accounting (failed attempts count)."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from compaction.s2.planted_retention import SEED, order_key

SPEND_TAG = "s2-planted-rule-part-a"
SIM_CAP_USD = 25.0
WINDOW = 80_000
MAX_CALLS = 30
RESET = datetime(2026, 10, 26, tzinfo=UTC)
B_DROP_IF_STEP0_AFTER = datetime(2026, 10, 27, 16, tzinfo=UTC)  # noon ET
B_CUTOFF = datetime(2026, 10, 27, 22, tzinfo=UTC)  # 6 PM ET
TASKS = ("boa-hierarchical-evaluation-cancellation", "fastapi-implicit-head-options",
         "helm-array-merge-strategies", "kombu-single-active-consumer-priority",
         "testem-bail-on-test-failure", "vitest-duration-sharding")
A_ALLOCATIONS = {"step0": 2, "step0_extra": 4, "screen": 12, "recheck": 12,
                 "baseline": 18, "repair": 12}
B_ALLOCATIONS = {"smoke": 4, "main": 30}
DROP_ORDER = ("reread", "kept_restatement", "deletion")


def task_order(seed: int = SEED) -> list[str]:
    return sorted(TASKS, key=lambda t: order_key("task", t, seed))


def pilot_spend(**kwargs):
    from compaction.s2.interact_run import PilotSpend
    return PilotSpend(tags=(SPEND_TAG,), caps={SPEND_TAG: SIM_CAP_USD}, **kwargs)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def central_budget_path() -> Path:
    from compaction.s2 import config as C
    return C.REPO_ROOT / "data/raw/sprint/s2/planted/fork_budget.json"


class ForkBudget:
    """Reserve before starting; crashes/failures consume the slot. No refund method.

    Optional arms use only explicitly released Step-0 extras/recheck slots. Core baseline
    and repair allocations remain reserved even if no qualifying losses exist yet.
    """
    def __init__(self, path: Path | None = None, *, attempt_roots=()):
        self.path = Path(path) if path is not None else central_budget_path()
        self.attempt_roots = tuple(Path(p) for p in attempt_roots)

    def _check_missing(self):
        if self.path.exists():
            return
        for root in self.attempt_roots:
            if root.exists() and any(root.rglob("report.json")):
                raise ValueError("attempt artifacts exist but the central fork budget is missing")

    def _read(self):
        self._check_missing()
        return json.loads(self.path.read_text()) if self.path.exists() else {
            "schema": 1, "attempts": [], "released": [], "seed": SEED}

    def _write(self, data):
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def state(self):
        with locked(self.path):
            return self._read()

    def release(self, bucket: str, *, reason: str, evidence: dict):
        if bucket not in ("step0_extra", "recheck") or not reason.strip():
            raise ValueError("only unused extras/recheck can be released, with a reason")
        artifact = Path(evidence.get("artifact", ""))
        if not artifact.is_file():
            raise ValueError("release needs a completed result artifact")
        raw = artifact.read_bytes()
        if hashlib.sha256(raw).hexdigest() != evidence.get("sha256"):
            raise ValueError("release evidence fingerprint mismatch")
        outcome = json.loads(raw)
        if bucket == "recheck" and not (outcome.get("passes") is True and outcome.get("assigned") == 12):
            raise ValueError("recheck release requires a passed complete sensitivity screen")
        if bucket == "step0_extra" and outcome.get("status") != "PASS":
            raise ValueError("extras release requires a passed Step 0")
        with locked(self.path):
            state = self._read()
            if bucket in [r["bucket"] for r in state["released"]]:
                raise ValueError("allocation already released")
            state["released"].append({"bucket": bucket, "reason": reason,
                                       "evidence": evidence, "outcome": outcome,
                                       "released_utc": datetime.now(UTC).isoformat()})
            self._write(state)

    def _reserve_one(self, state: dict, attempt_id: str, *, part: str, bucket: str, **metadata):
        if part not in ("A", "B") or not attempt_id:
            raise ValueError("part A/B and nonempty attempt id required")
        if any(a["attempt_id"] == attempt_id for a in state["attempts"]):
            raise ValueError("attempt already assigned; resume its artifacts, do not relaunch")
        own = [a for a in state["attempts"] if a["part"] == part]
        cap, allocations = (60, A_ALLOCATIONS) if part == "A" else (34, B_ALLOCATIONS)
        if len(own) >= cap or len(state["attempts"]) >= 94:
            raise ValueError("fork cap exhausted")
        released = {r["bucket"] for r in state["released"]}
        counts = {b: sum(a["bucket"] == b for a in own) for b in allocations}
        if part == "A" and bucket in DROP_ORDER:
            available = sum(A_ALLOCATIONS[b] - counts[b] for b in released)
            shares = optional_allocation(available)
            if sum(a["bucket"] == bucket for a in own) >= shares[bucket]:
                raise ValueError("optional allocation exceeds its released capacity/drop-order share")
        elif bucket not in allocations or bucket in released:
            raise ValueError("unknown or released allocation")
        elif counts[bucket] >= allocations[bucket]:
            if part == "A" and bucket == "step0_extra" and "recheck" not in released \
                    and counts["recheck"] < allocations["recheck"]:
                bucket = "recheck"
                metadata = metadata | {"borrowed_for": "step0_extra"}
            else:
                raise ValueError("allocation exhausted")
        row = {**metadata, "attempt_id": attempt_id, "part": part, "bucket": bucket,
               "assigned_utc": datetime.now(UTC).isoformat(), "status": "assigned"}
        state["attempts"].append(row)
        return row

    def reserve_batch(self, requests: list[dict]) -> list[dict]:
        """Atomic whole-comparison assignment; no partial first reservation on failure."""
        if not requests:
            raise ValueError("nonempty reservation batch required")
        for bucket in DROP_ORDER:
            n = sum(r.get("part") == "A" and r.get("bucket") == bucket for r in requests)
            group = 6 if bucket == "deletion" else 2
            if n % group:
                raise ValueError(f"{bucket} must be assigned in complete groups of {group}")
        with locked(self.path):
            state = self._read()
            rows = [self._reserve_one(state, **r) for r in requests]
            self._write(state)
            return rows

    def reserve(self, attempt_id: str, *, part: str, bucket: str, **metadata) -> dict:
        return self.reserve_batch([{"attempt_id": attempt_id, "part": part, "bucket": bucket,
                                    **metadata}])[0]

    def finish(self, attempt_id: str, status: str, *, artifact: str):
        if status not in ("complete", "technical_failure") or not artifact:
            raise ValueError("invalid fork status or missing artifact")
        with locked(self.path):
            state = self._read()
            row = next((a for a in state["attempts"] if a["attempt_id"] == attempt_id), None)
            if row is None:
                raise ValueError("unknown fork attempt")
            if row["status"] != "assigned":
                raise ValueError("finished fork status cannot be overwritten")
            row.update(status=status, artifact=artifact, finished_utc=datetime.now(UTC).isoformat())
            self._write(state)


def optional_allocation(available: int) -> dict:
    """Deletion retained before kept before re-read; fewer than six spare slots are
    intentionally left unused, since the highest-priority deletion comparison is 2x3.
    Lower-priority comparisons cannot bypass that complete comparison requirement.
    """
    result = {b: 0 for b in DROP_ORDER}
    if available < 6:
        return result  # cannot retain the highest-priority complete deletion comparison
    for b in reversed(DROP_ORDER):
        # Deletion check protocol is 2 x 3 and is never partially assigned.
        n = 6 if b == "deletion" else min(6, available // 2 * 2)
        result[b] = n
        available -= n
    return result


def part_b_guard(*, step0_passed_at: datetime, now: datetime,
                 start_utilization: float, current_utilization: float,
                 chains_started: bool = False, reading_at: datetime,
                 expected_fork_utilization: float):
    if step0_passed_at > B_DROP_IF_STEP0_AFTER or now > B_CUTOFF or chains_started:
        raise ValueError("Part B dropped: Step 0 deadline/cutoff/chain priority")
    if not (0 <= start_utilization <= current_utilization < .90):
        raise ValueError("missing, reset, or held Max window")
    age = (now - reading_at).total_seconds()
    if not 0 <= age <= 300:
        raise ValueError("Part B requires a fresh Max reading (at most 300 seconds old)")
    if not 0 < expected_fork_utilization <= .02:
        raise ValueError("positive projected per-fork Max utilization required")
    if current_utilization - start_utilization + expected_fork_utilization > .02 + 1e-9:
        raise ValueError("Part B used its two-point Max allowance")


class PartBAllowance(ForkBudget):
    """Durable two-point Max allowance; restarting never accepts a new baseline."""

    def __init__(self, path: Path | None = None):
        super().__init__(path or central_budget_path().with_name("part_b_allowance.json"))

    def guard(self, *, step0_passed_at: datetime, now: datetime, reading_at: datetime,
              current_utilization: float, expected_fork_utilization: float,
              window_reset_at: datetime, chains_started: bool = False) -> dict:
        with locked(self.path):
            state = json.loads(self.path.read_text()) if self.path.exists() else None
            if state and state["window_reset_at"] != window_reset_at.isoformat():
                raise ValueError("Part B Max window changed; allowance cannot reset silently")
            start = state["start_utilization"] if state else current_utilization
            part_b_guard(step0_passed_at=step0_passed_at, now=now, reading_at=reading_at,
                         start_utilization=start, current_utilization=current_utilization,
                         expected_fork_utilization=expected_fork_utilization,
                         chains_started=chains_started)
            if state is None:
                state = {"start_utilization": start, "start_reading_at": reading_at.isoformat(),
                         "window_reset_at": window_reset_at.isoformat(), "checks": []}
            state["checks"].append({"at": now.isoformat(), "reading_at": reading_at.isoformat(),
                                    "utilization": current_utilization,
                                    "expected_fork_utilization": expected_fork_utilization})
            self._write(state)
            return state


def part_b_tasks(active_tasks: list[str]) -> list[str]:
    """Recompute Part B after Appendix D replacements, preserving the seeded order."""
    if len(active_tasks) != len(set(active_tasks)) or any(t not in TASKS for t in active_tasks):
        raise ValueError("active cohort must contain unique tasks from the frozen population")
    return [t for t in task_order() if t in active_tasks][:2]
