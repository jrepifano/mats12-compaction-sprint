"""Frozen pilot allocation and durable fork accounting (failed attempts count)."""
from __future__ import annotations

import fcntl
import json
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


class ForkBudget:
    """Reserve before starting; crashes/failures consume the slot. No refund method.

    Optional arms use only explicitly released Step-0 extras/recheck slots. Core baseline
    and repair allocations remain reserved even if no qualifying losses exist yet.
    """
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {
            "schema": 1, "attempts": [], "released": [], "seed": SEED}

    def _write(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)

    def state(self):
        with locked(self.path):
            return self._read()

    def release(self, bucket: str, *, reason: str):
        if bucket not in ("step0_extra", "recheck") or not reason.strip():
            raise ValueError("only unused extras/recheck can be released, with a reason")
        with locked(self.path):
            state = self._read()
            if bucket not in [r["bucket"] for r in state["released"]]:
                state["released"].append({"bucket": bucket, "reason": reason})
            self._write(state)

    def reserve(self, attempt_id: str, *, part: str, bucket: str, **metadata) -> dict:
        if part not in ("A", "B") or not attempt_id:
            raise ValueError("part A/B and nonempty attempt id required")
        with locked(self.path):
            state = self._read()
            if any(a["attempt_id"] == attempt_id for a in state["attempts"]):
                raise ValueError("attempt already assigned; resume its artifacts, do not relaunch")
            own = [a for a in state["attempts"] if a["part"] == part]
            cap, allocations = (60, A_ALLOCATIONS) if part == "A" else (34, B_ALLOCATIONS)
            if len(own) >= cap or len(state["attempts"]) >= 94:
                raise ValueError("fork cap exhausted")
            released = {r["bucket"] for r in state["released"]}
            counts = {b: sum(a["bucket"] == b for a in own) for b in allocations}
            if part == "A" and bucket in DROP_ORDER:
                if sum(a["bucket"] == bucket for a in own) >= 6:
                    raise ValueError("optional arm allocation exhausted")
                available = sum(A_ALLOCATIONS[b] - counts[b] for b in released)
                used = sum(a["bucket"] in DROP_ORDER for a in own)
                if used >= available:
                    raise ValueError("no released spare/recheck capacity for optional arms")
            elif bucket not in allocations or bucket in released:
                raise ValueError("unknown or released allocation")
            elif counts[bucket] >= allocations[bucket]:
                # Step-0 extras may borrow recheck slots only after its four spares.
                if part == "A" and bucket == "step0_extra" and "recheck" not in released \
                        and counts["recheck"] < allocations["recheck"]:
                    bucket = "recheck"
                    metadata = metadata | {"borrowed_for": "step0_extra"}
                else:
                    raise ValueError("allocation exhausted")
            row = {**metadata, "attempt_id": attempt_id, "part": part, "bucket": bucket,
                   "assigned_utc": datetime.now(UTC).isoformat(), "status": "assigned"}
            state["attempts"].append(row)
            self._write(state)
            return row

    def finish(self, attempt_id: str, status: str, *, artifact: str):
        if status not in ("complete", "technical_failure"):
            raise ValueError("invalid fork status")
        with locked(self.path):
            state = self._read()
            row = next(a for a in state["attempts"] if a["attempt_id"] == attempt_id)
            row.update(status=status, artifact=artifact)
            self._write(state)


def optional_allocation(available: int) -> dict:
    """Whole two-replicate comparisons only; deletion retained before kept before re-read."""
    result = {b: 0 for b in DROP_ORDER}
    if available < 6:
        return result  # cannot retain the highest-priority complete deletion comparison
    for b in reversed(DROP_ORDER):
        # Deletion check protocol is 2 x 3 and is never partially assigned.
        n = 6 if available >= 6 else (min(6, available // 2 * 2) if b != "deletion" else 0)
        result[b] = n
        available -= n
    return result


def part_b_guard(*, step0_passed_at: datetime, now: datetime,
                 start_utilization: float, current_utilization: float,
                 chains_started: bool = False):
    if step0_passed_at > B_DROP_IF_STEP0_AFTER or now > B_CUTOFF or chains_started:
        raise ValueError("Part B dropped: Step 0 deadline/cutoff/chain priority")
    if not (0 <= start_utilization <= current_utilization < .90):
        raise ValueError("missing, reset, or held Max window")
    if current_utilization - start_utilization >= .02 - 1e-9:
        raise ValueError("Part B used its two-point Max allowance")
