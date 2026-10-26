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
SPEND_STAGE = "SPRINT-S2-PLANTED"
SIM_CAP_USD = 25.0
WINDOW = 80_000
PROBE_WINDOW = 1_000_000
MAX_CALLS = 30


def validate_verification_freeze(configuration: dict, checkout: Path) -> dict | None:
    """Verify Jacob's v2 spec freeze before constructing any live service."""
    decision = configuration.get("provenance", {}).get("verification_v2")
    if decision is None:
        return None
    manifest_path = Path(checkout) / decision["spec_freeze_path"]
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != decision["spec_freeze_sha256"]:
        raise ValueError("verification spec freeze manifest mismatch")
    manifest = json.loads(raw)
    if (manifest.get("protocol") != "planted-verification-v2"
            or len(manifest["specs"]) != 6
            or {row["task_id"] for row in manifest["specs"]} != set(TASKS)):
        raise ValueError("verification spec freeze population mismatch")
    for row in manifest["specs"]:
        path = Path(configuration["specs_dir"]) / (row["task_id"] + ".json")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["new"]["file_sha256"]:
            raise ValueError(f"verification frozen spec mismatch: {row['task_id']}")
    return manifest


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


def pilot_budget_paths(checkout: Path | None = None):
    from compaction.s2 import config as C
    root = Path(checkout or C.REPO_ROOT).resolve()
    return root, root / "logs/costs.jsonl", root / "data/raw/sprint/s2/planted/budget_caps.toml"


def initialize_pilot_budget(*, checkout: Path | None = None, caps_file: Path | None = None) -> dict:
    """Freeze the pilot's separately authorized $25 cap without rewriting sprint books.

    Deployment calls this once on the designated desktop. Existing ledgers,
    reservation state and event logs remain in place. Their open liabilities still
    count under budget.py; this never expires, cancels or migrates a reservation.
    """
    root, ledger, default_caps = pilot_budget_paths(checkout)
    path = Path(caps_file) if caps_file is not None else default_caps
    content = ('# Planted pilot only; desktop is its sole launcher and ledger owner.\n'
               '[sprint]\n' + f'checkout = {json.dumps(str(root))}\n'
               'core_cap_usd = 25.0\nfull_cap_usd = 25.0\ntopup_recorded = false\n'
               'transfer_approval_over_usd = 0.0\n\n'
               f'[stages.{SPEND_STAGE}]\ncap_usd = 25.0\n')
    with locked(path):
        if path.exists():
            if path.read_text() != content:
                raise ValueError("pilot caps already frozen for a different owner/configuration")
        else:
            # A stage switch must not silently forget prior non-reserved pilot
            # charges. None are expected: the desktop run has not started yet.
            if ledger.exists():
                for line in ledger.read_text().splitlines():
                    row = json.loads(line)
                    if (SPEND_TAG in str(row.get("note") or "") and row.get("exp") != SPEND_STAGE
                            and float(row.get("usd") or 0) > 0):
                        raise ValueError("legacy pilot charges require explicit reconciliation before stage setup")
            temporary = path.with_suffix(".tmp")
            with temporary.open("w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
    return {"caps_file": str(path), "ledger": str(ledger), "checkout": str(root),
            "stage": SPEND_STAGE, "cap_usd": SIM_CAP_USD,
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


def pilot_spend(*, checkout: Path | None = None, ledger=None, caps_file=None, **kwargs):
    """Explicit pilot books, independent of the ambient shared-sprint caps/env.

    Construction is read-only; initialize_pilot_budget is a separate deployment
    operation. Missing or wrongly owned caps fail at the first budget read/reserve.
    """
    from compaction.s2.interact_run import PilotSpend
    _, default_ledger, default_caps = pilot_budget_paths(checkout)
    return PilotSpend(tags=(SPEND_TAG,), caps={SPEND_TAG: SIM_CAP_USD}, stage=SPEND_STAGE,
                      ledger=Path(ledger) if ledger is not None else default_ledger,
                      caps_file=Path(caps_file) if caps_file is not None else default_caps, **kwargs)


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
            if bucket == "recheck":
                screen = [r for r in state["attempts"] if r["part"] == "A" and r["bucket"] == "screen"]
                if len(screen) != 12 or any(r["status"] not in {"complete", "technical_failure"} for r in screen):
                    raise ValueError("release requires all twelve assigned screen attempts finished")
            else:
                identities = outcome.get("attempt_ids")
                by_id = {r["attempt_id"]: r for r in state["attempts"]}
                if (not isinstance(identities, list) or len(identities) != 2 or len(set(identities)) != 2
                        or any(identity not in by_id or by_id[identity]["status"] != "complete"
                               or by_id[identity]["part"] != "A"
                               or (by_id[identity]["bucket"] not in {"step0", "step0_extra"}
                                   and by_id[identity].get("borrowed_for") != "step0_extra")
                               for identity in identities)):
                    raise ValueError("PASS report must name two completed Step 0 budget attempts")
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
    """Durable allowance with one in-flight fork. A pending/crashed fork blocks
    overlapping starts until finish records its fresh post-fork utilization reading.
    """

    def __init__(self, path: Path | None = None):
        super().__init__(path or central_budget_path().with_name("part_b_allowance.json"))

    def guard(self, *, attempt_id: str, step0_passed_at: datetime, now: datetime, reading_at: datetime,
              current_utilization: float, expected_fork_utilization: float,
              window_reset_at: datetime, chains_started: bool = False) -> dict:
        with locked(self.path):
            state = json.loads(self.path.read_text()) if self.path.exists() else None
            if not attempt_id or state and (state.get("pending") or any(
                    r["attempt_id"] == attempt_id for r in state.get("finished", []))):
                raise ValueError("Part B requires a unique attempt and no pending in-flight fork")
            if state and state["window_reset_at"] != window_reset_at.isoformat():
                raise ValueError("Part B Max window changed; allowance cannot reset silently")
            if state and (current_utilization < state.get("last_utilization", state["start_utilization"])
                          or reading_at < datetime.fromisoformat(state.get("last_reading_at", state["start_reading_at"]))):
                raise ValueError("Part B reading cannot precede its last completed fork")
            start = state["start_utilization"] if state else current_utilization
            expected_fork_utilization = max(expected_fork_utilization,
                                             state.get("observed_fork_floor", 0) if state else 0)
            part_b_guard(step0_passed_at=step0_passed_at, now=now, reading_at=reading_at,
                         start_utilization=start, current_utilization=current_utilization,
                         expected_fork_utilization=expected_fork_utilization,
                         chains_started=chains_started)
            if state is None:
                state = {"start_utilization": start, "start_reading_at": reading_at.isoformat(),
                         "window_reset_at": window_reset_at.isoformat(), "checks": [], "finished": []}
            state["checks"].append({"at": now.isoformat(), "reading_at": reading_at.isoformat(),
                                    "utilization": current_utilization,
                                    "expected_fork_utilization": expected_fork_utilization})
            state["pending"] = {"attempt_id": attempt_id, "assigned_at": now.isoformat(),
                                "utilization": current_utilization,
                                "expected_fork_utilization": expected_fork_utilization}
            self._write(state)
            return state

    def finish(self, attempt_id: str, status: str, *, artifact: str, current_utilization: float,
               reading_at: datetime, now: datetime):
        if status not in {"complete", "technical_failure"} or not artifact:
            raise ValueError("Part B finish needs a terminal status and artifact")
        with locked(self.path):
            if not self.path.exists():
                raise ValueError("unknown Part B allowance")
            state = json.loads(self.path.read_text())
            pending = state.get("pending")
            if not pending or pending["attempt_id"] != attempt_id:
                raise ValueError("unknown or already finished Part B attempt")
            if (not 0 <= (now - reading_at).total_seconds() <= 300
                    or reading_at < datetime.fromisoformat(pending["assigned_at"])
                    or not pending["utilization"] <= current_utilization <= 1):
                raise ValueError("Part B finish needs a fresh monotonic post-fork reading")
            state["finished"].append({**pending, "status": status, "artifact": artifact,
                                      "finished_at": now.isoformat(), "reading_at": reading_at.isoformat(),
                                      "final_utilization": current_utilization})
            state["last_utilization"] = current_utilization
            state["last_reading_at"] = reading_at.isoformat()
            state["observed_fork_floor"] = max(state.get("observed_fork_floor", 0),
                                                current_utilization - pending["utilization"])
            state["pending"] = None
            self._write(state)


def part_b_tasks(active_tasks: list[str]) -> list[str]:
    """Recompute Part B after Appendix D replacements, preserving the seeded order."""
    if len(active_tasks) != len(set(active_tasks)) or any(t not in TASKS for t in active_tasks):
        raise ValueError("active cohort must contain unique tasks from the frozen population")
    return [t for t in task_order() if t in active_tasks][:2]


class ActiveCohort(ForkBudget):
    """Frozen initial assignment plus append-only replacement history, independent
    of immutable task specs. Reading absent state returns the initial cohort; the
    first replacement durably freezes it and appends the replacement under a lock.
    """

    def __init__(self, path: Path | None = None):
        super().__init__(path or central_budget_path().with_name("active_cohort.json"))

    def _read(self):
        state = json.loads(self.path.read_text()) if self.path.exists() else {
            "schema": 1, "seed": SEED, "initial_cohort": task_order()[:3], "replacements": []}
        if state["seed"] != SEED or state["initial_cohort"] != task_order()[:3]:
            raise ValueError("frozen initial cohort changed")
        active, used = list(state["initial_cohort"]), set(state["initial_cohort"])
        for row in state["replacements"]:
            next_task = next((t for t in task_order() if t not in used), None)
            if (row["old"] not in active or row["new"] != next_task
                    or row["before_sha256"] != hashlib.sha256(json.dumps(active).encode()).hexdigest()):
                raise ValueError("cohort replacement history is inconsistent")
            active[active.index(row["old"])] = row["new"]
            active.sort(key=task_order().index)
            used.add(row["new"])
        return {**state, "active_cohort": active}

    def replace_task(self, old: str, new: str, *, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("replacement reason required")
        with locked(self.path):
            state = self._read()
            active = state.pop("active_cohort")
            used = set(state["initial_cohort"]) | {r["new"] for r in state["replacements"]}
            next_task = next((t for t in task_order() if t not in used), None)
            if old not in active or new != next_task:
                raise ValueError("replacement must use the next unused task in seeded order")
            state["replacements"].append({"old": old, "new": new, "reason": reason,
                "at": datetime.now(UTC).isoformat(),
                "before_sha256": hashlib.sha256(json.dumps(active).encode()).hexdigest()})
            self._write(state)
            return self._read()


def runtime_tasks(path: Path | None = None) -> list[str]:
    """Current cohort; task-spec initial_cohort/part_b_cohort flags are historical only."""
    return ActiveCohort(path).state()["active_cohort"]


def runtime_part_b_tasks(path: Path | None = None) -> list[str]:
    return part_b_tasks(runtime_tasks(path))
