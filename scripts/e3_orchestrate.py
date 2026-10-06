"""E3 laptop orchestrator: projection with cuts, launch with reservation, re-arm, recovery, the
unconditional cleanup path, the wall-clock cap and the status block.

Spec: docs/experiments/E3/prereg.md §7 (cuts in order, speculative $5, wall-clock cap, stopping
rules), §8.3 (automatic gates), §8.6 (next-gate reservations, pod concurrency, the unconditional
cleanup path: steps 3-5 never depend on 1-2) and §8.8 (status block); implementation plan v2
fix 8, v3 fixes 4-5, v4 fixes 2-3. New script (unit U8d). The main session runs it at every
launch, end of job, stop rule and the deadline; `scripts/e3_watchdog.sh` runs `recover` every
<= 2 minutes, independently of any launcher (v4 fix 2).

Subcommands (each prints one line `E3_ORCH <json>` and appends a line to
logs/E3-orchestrate.jsonl; a stop prints `E3_STOP <rule>` too):
  project                                  the projection of remaining work, with cuts
  launch --unit pod1|pod2|pod3 --slot S [--budget-usd B] [--gpu G]
  cleanup --slot S --reason R [--no-retrieve] [--reservation ID]
  rearm                                    re-arm every running E3 slot (pod_push --arm-only)
  api-reserve --usd X --note N | api-settle --id ID [--usd TOTAL] | api-release --id ID
  enforce                                  terminate unarmed / overdue pods now (no retrieval)
  reconcile-creating                       overdue `creating` rows against the API (enforce
                                           starts it in the background)
  recover                                  enforce, then stopped / gone / idle pods (cleanups in
                                           the background), untracked pods, orphans
  deadline-check                           at the 5-day cap: clean up everything, release, stop
  stop --rule R [--detail D]               any other §7 stop rule (e.g. a Stage A check failed)
  mark --unit U --status pending|done|skipped
  status [--phase P] [--gate G] [--reviews R] [--next N]

Exit codes: 0 ok; 1 a failure that was cleaned up (a recoverable infrastructure failure, e.g. a
launch that failed before any job ran, which may be retried); 2 a §7 stop (projection over a
limit after cuts, wall-clock cap, API guard refusal, artifacts not retrieved after 3 attempts);
3 a pod NOT confirmed terminated, or a creation not resolved (its liability is kept); 4 refused
before anything was spent.

2026-10-06, fixes after gpt-6-astra code review A r1 (logs/reviews/20261006-E3-code-A-r1.md):
- A1: a reservation whose `creating` row is unresolved is released only once the RunPod API
  listing proves no such pod exists (or an untracked one was reaped); an unknown listing keeps it
  (`_resolve_creating`, used by a failed launch, recover and the stop path).
- A2: each retrieval attempt runs in its own process group with a deadline
  (`retrieval_timeout_s`, the group is killed on expiry), at most RETRIEVAL_ATTEMPTS of them.
  Retrieval holds the slot's retrieval lock; termination and settlement hold a separate lock
  and never wait on retrieval; `--no-retrieve` (the emergency path) takes no retrieval lock.
  pod_down calls are bounded too (`terminate_timeout_s`).
- A4: cleanup and recover terminate and settle every unsettled pod of the slot or reservation
  found in durable history (`runpod_reservations.unsettled_pods`), through
  `pod_down.py --pod-id ID --reservation RID`, independent of the running slots and of .env.
- A5: retrieval passes the pod id (the pull stages per pod id and requires the job's
  `DONE.<unit>.json`); a slot on which no job was started is not retrieved from.
- A8: every §7 stop writes results/E3/STOPPED.json, cleans up every outstanding pod (bounded),
  settles or releases every open RunPod and API reservation; launch, job and api-reserve then
  refuse. Three failed retrievals are a §7 stop.
- A9: pod 3 (launch and job) needs results/E3/analysis/review_17r.json {"outcome": "pass"}; no
  path uses the speculative allowance.
Review A r2 (logs/reviews/20261006-E3-code-A-r2.md):
- 1: deadline enforcement is separate from retrieval. `enforce` (run by the watchdog every tick,
  and first in `recover`) terminates every unarmed or overdue pod without retrieval and resolves
  overdue `creating` rows; recover never runs a retrieval-bearing cleanup itself: it starts one
  background `cleanup` process per slot (skipped while that slot's retrieval or cleanup lock is
  held), so one slow pull never delays another pod's deadline.
- 1 (round 3, logs/reviews/20261006-E3-code-A-r3.md): `enforce` no longer terminates pods one
  after another. It dispatches one detached `cleanup --no-retrieve --enforced` process per
  overdue pod (skipped while that slot's cleanup lock is held, i.e. its termination is already
  running), so a stalled termination or settlement of one pod never delays another's DELETE.
  `--enforced`: a pod terminated for its deadline after a job was started on it, whose unit is
  not complete, has its unretrieved artifacts recorded as missing (run log and unit manifest)
  and fires §7's retrieval stop after the termination.
- Review A2 r1 (logs/reviews/20261006-E3-code-A2-r1.md): (1) every overdue launched pod's
  termination is dispatched before anything else, and overdue `creating` rows are reconciled
  in ONE detached `reconcile-creating` process per pass (its own lock: passes never pile up),
  so an orphaned creation's slow untracked reap never delays a DELETE. (2) the `--enforced`
  retrieval stop needs confirmed termination, and is decided under the slot's retrieval lock:
  while a retrieval is in progress (lock held) or once it has succeeded (unit complete),
  nothing is recorded missing; an unconfirmed termination records nothing and the next
  enforcement pass retries it.
- Review A2 r2: a loss decision deferred because a retrieval held the slot's lock is persisted
  (results/E3/pending_loss/<slot>.json, written atomically: slot, pod_id, unit, reservation_id,
  reason, utc). The retrieval holder clears it once its own outcome is durable (retrieved, or
  missing recorded and the stop written); if the holder dies, `recover` resolves it once the
  lock is free: a complete unit clears it; a terminated pod with an incomplete unit records the
  artifacts missing, clears it, then fires §7's retrieval stop (never twice: an existing stop
  only clears the record).
- Review A2 r3: the stop claim is exclusive. Only the first writer of STOPPED.json (exclusive
  create) emits the stop event and runs the shutdown sweep; any later stop only logs
  `stop_already` (recover keeps finishing an unfinished shutdown). Recovery of a deferred loss
  writes STOPPED.json under the retrieval lock BEFORE clearing the pending record, then sweeps
  outside the lock; a crash between those steps is completed by the next recover, once.
- 3: a launch waiting for admission cancels on a stop or the cap; after admission the stop and
  the cap are re-checked under logs/E3-create.lock immediately before pod_up (a refusal releases
  the reservation). A stop writes STOPPED.json, then takes the same lock before its sweep, so an
  in-flight pod_up finishes first and its pod is swept.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emx.e3 import budget as B
from emx.e3 import contracts as C

REPO_ROOT = Path(__file__).resolve().parents[1]
DEADLINE_RE = re.compile(r"^EMX_POD_DEADLINE=(\d+)\s*$", re.MULTILINE)
# pod_up's one outcome line: ok | uncertain (a pod may bill: never release) | closed | none
CREATE_RE = re.compile(r"^EMX_POD_CREATE=(\w+) reservation=(\S+)", re.MULTILINE)
STOP_STATUSES = ("EXITED", "TERMINATED", "STOPPED")
CREATE_LOCK = "logs/E3-create.lock"
CREATING_LOCK = "logs/E3-reconcile-creating.lock"
PENDING_LOSS_DIR = f"{C.RESULTS}/pending_loss"  # deferred loss decisions, one per slot  # one creation reconciliation at a time  # the creation decision vs a stop's sweep (review A r2, 3)
TERMINATE_ATTEMPTS = 3


class LaunchCancelled(RuntimeError):
    """A launch may not go on: E3 is stopped (`why` = "stopped") or past its cap ("deadline")."""

    def __init__(self, why: str, detail: str):
        super().__init__(detail)
        self.why = why


@dataclass
class Deps:
    """Everything that touches the world, so tests replace it (no pod, no paid API)."""

    root: Path
    runner: B.Runner
    rr: Any  # emx.runpod_reservations (or a fake)
    spend: Any  # emx.spend (or a fake)
    api_pods: Callable[[], list[dict] | None]  # e3-* pods from the RunPod API; None = unknown
    pod_usd: Callable[[str], float | None]  # a terminated pod's billed dollars (pod event log)
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    pid_alive: Callable[[int], bool] = field(default=lambda pid: _pid_alive(pid))
    retrieval_wait_s: float = 180.0
    terminate_wait_s: float = 30.0
    wait_poll_s: float = 300.0
    # unsettled E3 pods from durable history: [{pod_id, reservation_id, slot, terminated}] (A4)
    unsettled: Callable[[], list[dict]] = field(default=list)
    retrieval_timeout_s: float = 600.0  # one pull attempt, process group killed after (A2)
    terminate_timeout_s: float = 300.0  # one pod_down call
    create_timeout_s: float = 1800.0  # one pod_up call (create + SSH polling)
    # start a background process (cmd, log name); None: a detached process logging to
    # logs/E3-bg-<name>.log (review A r2, 1)
    spawn: Callable[[list[str], str], Any] | None = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def real_deps(root: Path = REPO_ROOT) -> Deps:
    from emx import runpod as rp
    from emx import runpod_reservations as rr
    from emx import spend

    rp.configure(C.EXP)

    def api_pods() -> list[dict] | None:
        try:
            client = rp.RunPod(rp.env("RUNPOD_API_KEY"))
            return [
                p
                for p in client.list_pods()
                if str(p.get("name", "")).startswith(rp.POD_NAME_PREFIX)
            ]
        except Exception as e:  # noqa: BLE001 - an unreachable API means "unknown", never "none"
            print(f"RunPod pod listing failed: {e}", file=sys.stderr)
            return None

    def pod_usd(pod_id: str) -> float | None:
        for s in rp.pod_sessions(rp.read_pod_events()):
            if s["pod_id"] == pod_id and not s["running"]:
                return float(s["usd"])
        return None

    def runner(cmd, *, root=root, env=None, timeout=None):
        return B.run_cmd(cmd, root=root, env=env, timeout=timeout)

    def unsettled() -> list[dict]:
        return rr.unsettled_pods(C.EXP)

    return Deps(
        root=root,
        runner=runner,
        rr=rr,
        spend=spend,
        api_pods=api_pods,
        pod_usd=pod_usd,
        unsettled=unsettled,
    )


@contextmanager
def _flock_wait(path: Path, timeout_s: float, poll_s: float = 1.0):
    """Yield True once the lock is held, False if it is still busy after `timeout_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        t_end = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= t_end:
                    yield False
                    return
                time.sleep(min(poll_s, max(0.01, t_end - time.monotonic())))
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _lock_busy(path: Path) -> bool:
    with _flock(path, blocking=False) as held:
        return not held


@contextmanager
def _flock(path: Path, blocking: bool = True):
    """Yield True when the lock is held, False when `blocking=False` and it is busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _utc(epoch: float | None) -> str:
    if epoch is None:
        return "?"
    return datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Orchestrator:
    def __init__(self, deps: Deps):
        self.d = deps
        self.root = Path(deps.root)
        self._stopping = False  # inside a stop's shutdown: no nested stop
        self._holding_create = False  # inside the launch's creation decision
        self._swept = False  # this recover pass already ran a stop's sweep

    # -- small helpers ------------------------------------------------------------------------

    def log(self, event: dict) -> dict:
        return B.append_log(self.root, event)

    def emit(self, cmd: str, summary: dict) -> None:
        line = {"cmd": cmd, **summary}
        print("E3_ORCH " + json.dumps(line, default=str, sort_keys=True), flush=True)
        self.log({**line, "event": line.get("event", cmd)})

    def stop(self, cmd: str, rule: str, retrieve: bool = True, **extra) -> int:
        """A §7 stop: terminal (fix A8). The first writer of results/E3/STOPPED.json (exclusive
        create; its rule is kept) emits the stop event and runs the bounded shutdown of every
        outstanding E3 pod and reservation. A later stop changes nothing: it logs `stop_already`
        and leaves any unfinished shutdown to recover (review A2 r3). Returns 2, or 3 if a pod
        could not be confirmed terminated."""
        if not self._claim_stop(cmd, rule, **extra):
            return 2
        return self._sweep(rule, retrieve)

    def _claim_stop(self, cmd: str, rule: str, **extra) -> bool:
        """Write STOPPED.json if absent; True only for the first writer (the stop event)."""
        rec, created = B.write_stopped(self.root, rule, {"cmd": cmd, **extra})
        if not created:
            self.emit(
                cmd,
                {"event": "stop_already", "stop": rule, "stopped_file_rule": rec.get("rule")},
            )
            print(f"E3_STOPPED_ALREADY {rec.get('rule')}", flush=True)
            return False
        self.emit(
            cmd,
            {"event": "stop", "stop": rule, "stopped_file_rule": rec.get("rule"), **extra},
        )
        print(f"E3_STOP {rule}", flush=True)
        return True

    def _sweep(self, rule: str, retrieve: bool = True) -> int:
        """The stop's shutdown sweep (run by the stop's first writer only)."""
        if self._stopping:  # a stop inside a stop's own shutdown: nothing nested
            return 2
        self._stopping = True
        try:
            if self._holding_create:
                worst = self._shutdown_all(f"stop: {rule}", retrieve=retrieve)
            else:
                # an in-flight pod_up finishes first, so its pod is swept (review A r2, 3)
                lock = self.root / CREATE_LOCK
                with _flock_wait(lock, self.d.create_timeout_s + 120) as held:
                    if not held:
                        self.log({"event": "stop_lock_timeout", "unit": None, "lock": str(lock)})
                    worst = self._shutdown_all(f"stop: {rule}", retrieve=retrieve)
        finally:
            self._stopping = False
        return 3 if worst == 3 else 2

    def refuse_if_stopped(self, cmd: str, **ctx) -> int | None:
        st = B.read_stopped(self.root)
        if st is None:
            return None
        why = f"E3 is stopped ({st.get('rule')}, {st.get('utc')}): {B.STOPPED_FILE}"
        self.emit(cmd, {"event": "refused", "why": why, **ctx})
        return 4

    def run(
        self, cmd: list[str], env: dict | None = None, timeout: float | None = None
    ) -> tuple[int, str]:
        rc, out = self.d.runner(cmd, root=self.root, env=env, timeout=timeout)
        sys.stderr.write(out if out.endswith("\n") or not out else out + "\n")
        return rc, out

    def slot_pod(self, slot: str) -> dict | None:
        return next((p for p in B.running_slots(self.root) if p["slot"] == slot), None)

    def slot_launch(self, slot: str) -> dict:
        """The latest reserved/launched event for this slot (unit and reservation id), and
        whether a pod job was started on it since (`job`; then there is something to retrieve)."""
        out: dict = {}
        for e in B.read_log(self.root):
            if e.get("slot") != slot:
                continue
            if e.get("event") in ("reserved", "launched"):
                out = {
                    **out,
                    **{k: e[k] for k in ("unit", "reservation_id") if k in e},
                    "launched": e["event"] == "launched" or out.get("launched", False),
                }
            elif e.get("event") == "cleaned":
                out = {}
            elif e.get("cmd") == "job" and out:
                out["job"] = True
        return out

    def projection(self, state: dict | None = None):
        state = state or B.gather_state(self.root, self.d.spend)
        return state, B.load_rates(self.root)

    # -- project ------------------------------------------------------------------------------

    def project(self) -> int:
        state, rates = self.projection()
        p = B.project(state, rates)
        cuts, q = B.apply_cuts(p)
        summary = {
            "projected_before_cuts": p["projected"],
            "over_before_cuts": p["over"],
            "cuts": cuts,
            "projected": q["projected"],
            "remaining": q["remaining"],
            "committed": q["committed"],
            "over": q["over"],
            "flags": q["flags"],
            "stop": q.get("stop"),
        }
        # Read-only: the §7 stop rule is enforced where spend is committed (launch, job, the
        # API reservations), never by a projection report (2026-10-06: a report-only call
        # wrote a terminal STOPPED.json).
        summary["would_stop"] = bool(q.get("stop"))
        self.emit("project", summary)
        return 2 if q.get("stop") else 0

    # -- launch -------------------------------------------------------------------------------

    def launch(
        self, unit: str, slot: str, budget_usd: float | None = None, gpu: str | None = None
    ) -> int:
        if unit not in B.POD_UNITS:
            raise SystemExit(f"--unit must be one of {B.POD_UNITS}")
        refused = self.refuse_if_stopped("launch", unit=unit, slot=slot)
        if refused is not None:
            return refused
        if unit == "pod3" and not B.review_17r_passed(self.root):
            self.emit(
                "launch",
                {
                    "event": "refused",
                    "unit": unit,
                    "slot": slot,
                    "why": f"pod 3 needs {B.REVIEW_17R_FILE} with outcome pass (no speculative "
                    "work in E3)",
                },
            )
            return 4
        if B.deadline_passed(self.d.now()):
            return self.stop("launch", B.STOP_WALL_CLOCK, unit=unit, slot=slot)
        rc, _ = self.run(["bash", "scripts/e3_watchdog.sh", "--check-alive"])
        if rc != 0:
            self.emit(
                "launch",
                {
                    "event": "refused",
                    "unit": unit,
                    "slot": slot,
                    "why": "the E3 watchdog is not alive (v4 fix 2)",
                },
            )
            return 4
        with _flock(self.root / "logs" / "E3-launch.lock", blocking=False) as held:
            if not held:
                self.emit(
                    "launch",
                    {
                        "event": "refused",
                        "unit": unit,
                        "slot": slot,
                        "why": "another E3 launch is in progress",
                    },
                )
                return 4
            return self._launch_locked(unit, slot, budget_usd, gpu)

    def _launch_locked(self, unit, slot, budget_usd, gpu) -> int:
        if self.slot_pod(slot) is not None:
            self.emit(
                "launch",
                {
                    "event": "refused",
                    "unit": unit,
                    "slot": slot,
                    "why": f"slot {slot} already has a running E3 pod",
                },
            )
            return 4
        state, rates = self.projection()
        if state["units"][unit] != "pending":
            self.emit(
                "launch",
                {
                    "event": "refused",
                    "unit": unit,
                    "slot": slot,
                    "why": f"{unit} is {state['units'][unit]}",
                },
            )
            return 4
        sampling = None
        if unit == "pod2":
            dec = B.decide_pod2_sampling(state, rates)
            cuts, q, sampling = dec["cuts"], dec["projection"], dec["sample"]
        else:
            cuts, q = B.apply_cuts(B.project(state, rates))
        if q.get("stop"):
            return self.stop(
                "launch", q["stop"], unit=unit, slot=slot, projected=q["projected"], cuts=cuts
            )
        new = [c for c in cuts if c not in state["cuts"]]
        if new:
            self.log({"event": "cuts", "cuts": cuts, "new": new, "unit": None})
        cuts_doc = {
            "cuts": cuts,
            "pod2_sampling": sampling if unit == "pod2" else state.get("pod2_sampling"),
            "utc": B.utc_now(),
        }
        (self.root / B.CUTS_FILE).parent.mkdir(parents=True, exist_ok=True)
        (self.root / B.CUTS_FILE).write_text(json.dumps(cuts_doc, indent=2) + "\n")

        key = "pod3_cut1" if unit == "pod3" and B.CUT1 in cuts else unit
        res_usd = C.RESERVATION_USD[key]
        if unit == "pod2" and sampling is False:  # cut 2: pod 2 runs without sampling
            res_usd -= B.pod_usd("pod2", q["hours"]) - B.pod_usd("pod2", q["hours"], [B.CUT2])
        res_usd = round(res_usd, 4)
        unit_usd = B.pod_usd(unit, q["hours"], cuts)
        retry_share = round(max(0.0, res_usd - unit_usd), 4) if unit != "pod3" else 0.0
        budget = res_usd if budget_usd is None else float(budget_usd)
        if not 0 < budget <= res_usd + 1e-9:
            self.emit(
                "launch",
                {
                    "event": "refused",
                    "unit": unit,
                    "slot": slot,
                    "why": f"--budget-usd {budget} not in (0, {res_usd}]",
                },
            )
            return 4

        give_up = C.DEADLINE_UTC.timestamp()

        def wait(seconds: float) -> None:  # a waiting admission cancels on a stop or the cap
            self._launch_allowed()
            self.d.sleep(seconds)
            self._launch_allowed()

        try:
            rid = self.d.rr.wait_and_reserve(
                C.EXP,
                res_usd,
                f"E3 {unit} slot {slot}",
                give_up_at=give_up,
                poll_s=self.d.wait_poll_s,
                sleep=wait,
                clock=lambda: self.d.now().timestamp(),
                log=lambda m: self.log(
                    {"event": "reservation_wait", "unit": None, "slot": slot, "message": m}
                ),
            )
        except LaunchCancelled as e:
            if e.why == "stopped":
                self.emit(
                    "launch",
                    {"event": "refused", "unit": unit, "slot": slot, "why": f"while waiting: {e}"},
                )
                return 4
            return self.stop("launch", B.STOP_WALL_CLOCK, unit=unit, slot=slot, why=str(e))
        except Exception as e:  # NotAdmitted once the deadline is reached
            if B.deadline_passed(self.d.now()) or type(e).__name__ == "NotAdmitted":
                return self.stop(
                    "launch",
                    B.STOP_WALL_CLOCK,
                    unit=unit,
                    slot=slot,
                    why=f"reservation not admitted before the cap: {e}",
                )
            raise
        self.log(
            {
                "event": "reserved",
                "unit": unit,
                "slot": slot,
                "reservation_id": rid,
                "reservation_usd": res_usd,
                "pid": os.getpid(),
            }
        )

        # the creation decision, under the create lock that a stop takes before its sweep: a stop
        # either comes first (refused here, the reservation released) or waits for pod_up, and
        # then sweeps its pod (code review A r2, finding 3)
        with _flock(self.root / CREATE_LOCK):
            self._holding_create = True
            try:
                self._launch_allowed()
            except LaunchCancelled as e:
                refused: LaunchCancelled | None = e
            else:
                refused = None
                rc_up, out_up = self._pod_up(slot, rid, budget, gpu)
            finally:
                self._holding_create = False
        if refused is not None:
            try:
                self.d.rr.release(rid, note=f"launch refused after admission: {refused}")
            finally:
                self.log(
                    {
                        "event": "launch_refused",
                        "unit": None,
                        "slot": slot,
                        "reservation_id": rid,
                        "why": str(refused),
                    }
                )
            if refused.why == "stopped":
                self.emit(
                    "launch",
                    {
                        "event": "refused",
                        "unit": unit,
                        "slot": slot,
                        "why": f"after admission: {refused}; reservation released",
                    },
                )
                return 4
            return self.stop("launch", B.STOP_WALL_CLOCK, unit=unit, slot=slot, why=str(refused))
        stage = "pod_up"
        try:
            rc, out = rc_up, out_up
            if isinstance(out, BaseException):  # outcome unknown: kept, like no outcome line
                return self._launch_failed(
                    unit, slot, rid, f"pod_up raised {type(out).__name__}: {out}", create=None
                )
            if rc != 0:
                c = CREATE_RE.search(out)
                return self._launch_failed(
                    unit, slot, rid, f"pod_up exited {rc}", create=c.group(1) if c else None
                )
            m = DEADLINE_RE.search(out)
            pod = self.slot_pod(slot)
            self.log(
                {
                    "event": "launched",
                    "unit": unit,
                    "slot": slot,
                    "reservation_id": rid,
                    "reservation_usd": res_usd,
                    "budget_usd": budget,
                    "retry_share_usd": retry_share,
                    "pod_id": pod and pod["pod_id"],
                    "pod_deadline_epoch": int(m.group(1)) if m else None,
                    "cuts": cuts,
                    "pod2_sampling": sampling,
                    "pid": os.getpid(),
                }
            )
            if B.read_stopped(self.root) is not None:  # stopped meanwhile: its sweep has the pod
                return self._launch_failed(unit, slot, rid, "E3 stopped during the launch")
            stage = "pod_push"
            env = {"E3_STAGEB_SAMPLING": "1" if sampling else "0"} if unit == "pod2" else None
            rc, _ = self.run(
                ["bash", "scripts/pod_push.sh", "--exp", C.EXP, "--slot", slot], env=env
            )
            if rc != 0:
                return self._launch_failed(
                    unit, slot, rid, f"pod_push exited {rc} (unarmed or setup failed)"
                )
        except Exception as e:  # noqa: BLE001 - any failure after the reservation is cleaned up
            return self._launch_failed(unit, slot, rid, f"{stage} raised {type(e).__name__}: {e}")
        self.emit(
            "launch",
            {
                "event": "launch_ok",
                "unit": unit,
                "slot": slot,
                "reservation_id": rid,
                "reservation_usd": res_usd,
                "budget_usd": budget,
                "cuts": cuts,
                "pod2_sampling": sampling,
                "pod_deadline_utc": _utc(int(m.group(1))) if m else None,
            },
        )
        return 0

    def _launch_allowed(self) -> None:
        """Raise LaunchCancelled once E3 is stopped or the wall-clock cap has passed."""
        st = B.read_stopped(self.root)
        if st is not None:
            raise LaunchCancelled("stopped", f"E3 is stopped ({st.get('rule')})")
        if B.deadline_passed(self.d.now()):
            raise LaunchCancelled("deadline", "the wall-clock cap has passed")

    def _pod_up(self, slot: str, rid: str, budget: float, gpu: str | None):
        """Run pod_up (bounded); returns (rc, output), or (1, the exception) if it raised."""
        cmd = [
            "uv",
            "run",
            "python",
            "scripts/pod_up.py",
            "--exp",
            C.EXP,
            "--slot",
            slot,
            "--reservation",
            rid,
            "--budget-usd",
            f"{budget:.4f}",
        ]
        if gpu:
            cmd += ["--gpu", gpu]
        try:
            return self.run(cmd, timeout=self.d.create_timeout_s)
        except Exception as e:  # noqa: BLE001 - reported by the caller as a launch failure
            return 1, e

    def _launch_failed(
        self, unit: str, slot: str, rid: str, why: str, create: str | None = "after_create"
    ) -> int:
        """After the reservation: a pod that exists (running in the slot, or unsettled under
        this reservation) goes through cleanup, with no retrieval (no job ran: recoverable).
        Otherwise (fix A1) pod_up's outcome line decides, when pod_up failed (`create`):
        `uncertain`, or no line at all (pod_up killed), keeps the reservation and its liability
        for recover's overdue pass, which reconciles it with the API; `closed` has nothing to
        release; `none` (no pod was created) is released. After a later stage, the reservation
        is released only once the API proves no pod was created."""
        self.log(
            {
                "event": "launch_failed",
                "unit": None,
                "slot": slot,
                "reservation_id": rid,
                "why": why,
                "failed_unit": unit,
            }
        )
        if self.slot_pod(slot) is not None or any(
            u.get("reservation_id") == rid for u in self._unsettled()
        ):
            rc = self.cleanup(
                slot, f"launch failed: {why}", retrieve=False, reservation_id=rid, outcome="aborted"
            )
            return 3 if rc == 3 else 1
        try:
            if create in (None, "uncertain"):
                closed = False
                detail = f"kept: pod_up create outcome {create or 'missing (killed?)'}"
            elif create == "closed":
                closed, detail = True, "already released or settled by pod_up"
            elif create == "none":
                self.d.rr.release(rid, note=f"launch failed: {why}; pod_up created no pod")
                closed, detail = True, "released (pod_up: no pod created)"
            else:
                closed, detail = self._resolve_creating(rid, f"launch failed: {why}")
        except Exception as e:  # noqa: BLE001 - unknown means kept
            closed, detail = False, f"kept: {type(e).__name__}: {e}"
        self.emit(
            "launch",
            {
                "event": "launch_failed",
                "unit": unit,
                "slot": slot,
                "reservation_id": rid,
                "why": why,
                "pod": None,
                "reservation": detail,
            },
        )
        return 1 if closed else 3

    def _resolve_creating(self, rid: str, note: str) -> tuple[bool, str]:
        """Close an unlaunched reservation only when no pod can be billing under it (fix A1).
        Without a `creating` row no create request was ever sent: release. With one, list the
        API: unknown keeps it; a listed pod of that name that no reservation launched is reaped
        as untracked (billed from the creating row's t0), and the listing re-checked. Returns
        (closed, detail)."""
        rr = self.d.rr
        R = rr.get(rid)
        if R is None:
            return True, "unknown reservation"
        if R.get("closed"):
            return True, "already closed"
        if R.get("launched"):
            return False, "launched: settled through its pods"
        # a pod the reaper adopted from this creating row: charge and settle it first
        for u in self._unsettled():
            if u.get("reservation_id") == rid:
                term, sett = self._down({**u, "running_slot": False})
                if not (term and sett):
                    return False, f"kept: pod {u['pod_id']} under it not terminated and settled"
        R = rr.get(rid) or {}
        if R.get("closed"):
            return True, "closed by settling its adopted pod"
        c = R.get("creating")
        if not c:
            rr.release(rid, note=note)
            return True, "released (no creation was attempted)"
        name = c.get("name")
        launched = set(rr.state(exp=C.EXP)["pods"])

        def unresolved(api: list[dict]) -> list[dict]:
            return [p for p in api if p.get("name") == name and p.get("id") not in launched]

        api = self.d.api_pods()
        if api is None:
            return False, f"kept: creating row for {name}, API listing unknown"
        if unresolved(api):
            self.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/pod_down.py",
                    "--exp",
                    C.EXP,
                    "--untracked",
                    "--reason",
                    f"{note}: creating row, no launch",
                ],
                timeout=self.d.terminate_timeout_s,
            )
            api = self.d.api_pods()
            if api is None or unresolved(api):
                return False, f"kept: pod {name} still listed (or listing unknown)"
        rr.release(rid, note=f"{note}: creating row, the API lists no such pod")
        return True, "released (the API lists no such pod)"

    # -- cleanup: the unconditional path (§8.6) -----------------------------------------------

    def retrieval_bound_s(self) -> float:
        """The longest one cleanup's retrieval can take: every attempt at its deadline, plus
        the waits between attempts and a minute of slack."""
        n = C.RETRIEVAL_ATTEMPTS
        return n * self.d.retrieval_timeout_s + (n - 1) * self.d.retrieval_wait_s + 60.0

    def cleanup(
        self,
        slot: str,
        reason: str,
        retrieve: bool = True,
        reservation_id: str | None = None,
        outcome: str | None = None,
        enforced: bool = False,
    ) -> int:
        """Steps 1-2 (retrieval, bounded) under the slot's retrieval lock; steps 3-5 under the
        slot's cleanup lock, which never waits on retrieval. A retrieval that fails after
        RETRIEVAL_ATTEMPTS attempts is a §7 stop (fix A8), raised once the locks are released.
        `enforced`: terminated for its deadline without retrieval; if a job had been started and
        its unit is not complete, its artifacts are recorded missing and §7's retrieval stop
        fires after the termination (review A r3)."""
        res = self._cleanup(slot, reason, retrieve, reservation_id, outcome, enforced)
        rc = res.pop("_rc")
        holder_of = res.pop("_retrieved_pod", None)  # this cleanup ran the slot's retrieval
        if res.get("stop_rule"):
            src = self.stop(
                "cleanup",
                res["stop_rule"],
                slot=slot,
                failed_unit=res.get("unit"),
                missing=res.get("missing"),
            )
            if holder_of:  # its outcome (missing + stop) is durable: the deferred one is resolved
                self._clear_pending_loss(slot, holder_of)
            return 3 if 3 in (rc, src) else 2
        if holder_of and res.get("retrieved") is True:
            self._clear_pending_loss(slot, holder_of)
        return rc

    def _cleanup(self, slot, reason, retrieve, reservation_id, outcome, enforced=False) -> dict:
        info = self.slot_launch(slot)
        unit = info.get("unit")
        rid = reservation_id or info.get("reservation_id")
        pod = self.slot_pod(slot)
        res = {
            "event": "cleaned",
            "slot": slot,
            "unit": unit,
            "reason": reason,
            "reservation_id": rid,
            "pod_id": pod and pod["pod_id"],
            "retrieved": None,
            "missing": [],
            "terminated": None,
            "settled": None,
            "reservation": None,
            "snapshot": None,
            "errors": [],
        }
        if pod is None and not info and reservation_id is None and not self._targets(slot, rid):
            res.update(terminated=True, retrieved=None, already_clean=True)
            res["snapshot"] = self._snapshot(res)
            res["unit"] = None  # nothing launched here: no unit disposition changes
            self.emit("cleanup", res)
            res["_rc"] = 0
            return res
        if retrieve and unit and not info.get("job"):
            retrieve = False  # no pod job was started: nothing to retrieve (recoverable)
            res["retrieval_skipped"] = "no job was started on this pod"
        # 1. pull and verify, at most RETRIEVAL_ATTEMPTS times (each bounded), a few minutes apart
        if retrieve and pod is not None and unit:
            lock = self.root / "logs" / f"E3-retrieve{slot}.lock"
            with _flock_wait(lock, self.retrieval_bound_s()) as held:
                if held:
                    self._retrieve(slot, unit, pod["pod_id"], res)
                    res["_retrieved_pod"] = pod["pod_id"]
                else:  # its holder records what it could not retrieve
                    res["retrieval_skipped"] = "another cleanup held the retrieval lock too long"
        # 2. record whatever is missing or unverified: a §7 stop
        if retrieve and unit and res["retrieved"] is not True and not res.get("retrieval_skipped"):
            why = (
                "no running pod to pull from"
                if pod is None
                else f"not verified after {res.get('attempts', 0)} attempts"
            )
            try:
                res["missing"] = self._record_missing(
                    unit, slot, why, res.get("last_pull_output", "")
                )
            except Exception as e:  # noqa: BLE001 - steps 3-5 still run
                res["errors"].append(f"record missing: {type(e).__name__}: {e}")
            res["stop_rule"] = B.STOP_RETRIEVAL
        with _flock(self.root / "logs" / f"E3-cleanup{slot}.lock"):
            # 3-4. terminate and settle every pod of this slot/reservation, regardless of 1-2
            try:
                res["terminated"], res["settled"], res["pods"] = self._terminate(slot, rid)
            except Exception as e:  # noqa: BLE001
                res["terminated"] = False
                res["errors"].append(f"terminate: {type(e).__name__}: {e}")
            # 5. settle/release the reservation, never for a pod not confirmed terminated
            try:
                res["reservation"] = self._close_reservation(rid, res["terminated"])
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"reservation: {type(e).__name__}: {e}")
            res["snapshot"] = self._snapshot(res)
        # a pod CONFIRMED terminated for its deadline after its job started, with no retrieval
        # in progress and none that succeeded: its results are lost (review A2 r1, 2). Decided
        # under the slot's retrieval lock, so no retrieval can start or finish meanwhile.
        if enforced and unit and info.get("job") and res["terminated"] is True:
            with _flock(self.root / "logs" / f"E3-retrieve{slot}.lock", blocking=False) as held:
                if not held:  # persisted, so a dead holder cannot lose it (review A2 r2)
                    res["loss_undecided"] = "a retrieval is in progress: its holder decides"
                    self._write_pending_loss(slot, res["pod_id"] or "", unit, rid, reason)
                elif B.unit_complete(self.root, unit):
                    res["loss_undecided"] = "the unit's results were retrieved"
                else:
                    try:
                        res["missing"] = self._record_missing(
                            unit,
                            slot,
                            "terminated past its deadline without retrieval (its job had started)",
                            reason,
                        )
                    except Exception as e:  # noqa: BLE001 - the stop still fires
                        res["errors"].append(f"record missing: {type(e).__name__}: {e}")
                    res["stop_rule"] = B.STOP_RETRIEVAL
        if outcome is None:
            outcome = "done" if res["retrieved"] is True else "aborted"
        res["outcome"] = outcome if res["terminated"] else "termination_unconfirmed"
        if not res["terminated"]:
            res["event"] = "cleanup_failed"
            print(
                f"E3 CLEANUP: POD {res['pod_id']} IN SLOT {slot} NOT CONFIRMED TERMINATED; "
                "its reservation and liability are kept. Terminate it by hand "
                f"(pod_down.py --exp E3 --slot {slot}).",
                file=sys.stderr,
                flush=True,
            )
        self.emit("cleanup", res)
        if not res["terminated"]:
            res["_rc"] = 3
        elif res["retrieved"] in (True, None) and not res["missing"] and res["settled"]:
            res["_rc"] = 0
        else:
            res["_rc"] = 1
        return res

    # -- deferred loss decisions (review A2 r2) ------------------------------------------------

    def _pending_loss_path(self, slot: str) -> Path:
        return self.root / PENDING_LOSS_DIR / f"{slot}.json"

    def _write_pending_loss(self, slot, pod_id, unit, rid, reason) -> None:
        p = self._pending_loss_path(slot)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "slot": slot,
            "pod_id": pod_id,
            "unit": unit,
            "reservation_id": rid,
            "reason": reason,
            "utc": B.utc_now(),
        }
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, indent=2) + "\n")
        os.replace(tmp, p)  # atomic
        self.log({"event": "loss_pending", "unit": None, **rec})

    def _clear_pending_loss(self, slot: str, pod_id: str | None = None) -> None:
        p = self._pending_loss_path(slot)
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            return
        if pod_id is None or rec.get("pod_id") in (pod_id, ""):
            p.unlink(missing_ok=True)
            self.log({"event": "loss_pending_cleared", "unit": None, "slot": slot})

    def _resolve_pending_losses(self, acts: list[dict]) -> int:
        """Each deferred loss decision whose retrieval lock is free (its holder finished or
        died): a complete unit, or an existing stop, clears it; a terminated pod with an
        incomplete unit gets its artifacts recorded missing, the record cleared, then §7's
        retrieval stop. A pod still running is left to enforcement first."""
        d = self.root / PENDING_LOSS_DIR
        worst = 0
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            slot, unit, pod_id = rec.get("slot") or p.stem, rec.get("unit"), rec.get("pod_id")
            fire = False
            with _flock(self.root / "logs" / f"E3-retrieve{slot}.lock", blocking=False) as held:
                if not held:
                    acts.append({"pending_loss": slot, "skipped": "retrieval in progress"})
                    continue
                running = any(
                    x["pod_id"] == pod_id and x["running"] for x in B.pod_sessions(self.root)
                )
                if B.read_stopped(self.root) is not None or (
                    unit and B.unit_complete(self.root, unit)
                ):
                    self._clear_pending_loss(slot)
                    acts.append({"pending_loss": slot, "cleared": True})
                elif running:
                    acts.append({"pending_loss": slot, "skipped": "pod still running"})
                    continue
                else:
                    man = self.root / C.manifest_path(unit) if unit else None
                    if man is None or "# MISSING" not in (man.read_text() if man.exists() else ""):
                        self._record_missing(
                            unit,
                            slot,
                            "terminated past its deadline; its retrieval did not finish",
                            rec.get("reason", ""),
                        )
                    # the stop is durable BEFORE the record goes (review A2 r3): a crash after
                    # this leaves STOPPED.json, and the next recover finishes the shutdown
                    fire = self._claim_stop(
                        "recover", B.STOP_RETRIEVAL, slot=slot, failed_unit=unit
                    )
                    self._clear_pending_loss(slot)
            if fire:  # the sweep, outside the retrieval lock
                rc = self._sweep(B.STOP_RETRIEVAL)
                acts.append({"pending_loss": slot, "stop": rc})
                worst = max(worst, rc)
                self._swept = True
        return worst

    def _retrieve(self, slot: str, unit: str, pod_id: str, res: dict) -> None:
        res["retrieved"] = False
        cmd = [
            "bash",
            "scripts/pod_pull_results.sh",
            "--exp",
            C.EXP,
            "--slot",
            slot,
            "--unit",
            unit,
            "--pod-id",
            pod_id,
        ]
        for attempt in range(1, C.RETRIEVAL_ATTEMPTS + 1):
            res["attempts"] = attempt
            try:
                rc, out = self.run(cmd, timeout=self.d.retrieval_timeout_s)
            except Exception as e:  # noqa: BLE001 - a raising attempt is a failed attempt
                rc, out = 1, f"{type(e).__name__}: {e}"
                res["errors"].append(f"retrieval attempt {attempt}: {out}")
            if rc == 0:
                res["retrieved"] = True
                return
            res["last_pull_output"] = out[-2000:]
            if attempt < C.RETRIEVAL_ATTEMPTS:
                self.d.sleep(self.d.retrieval_wait_s)

    def _record_missing(self, unit: str | None, slot: str, why: str, detail: str) -> list[str]:
        man = C.manifest_path(unit) if unit else C.manifest_path(f"slot{slot}")
        item = f"{man}: {why}"
        utc = B.utc_now()
        p = self.root / man
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:  # '#' lines are comments to `shasum -c`
            f.write(f"# MISSING ({utc}, slot {slot}): {why}\n")
            f.writelines(f"#   {line}\n" for line in detail.strip().splitlines()[-5:])
        log = self.root / C.REPORT_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(
                f"\n### {utc}: E3 cleanup, missing artifacts (slot {slot}, unit {unit})\n\n"
                f"- {item}\n- The run is marked failed for this unit (§7 stopping rule).\n"
            )
        return [item]

    def _unsettled(self, res: dict | None = None) -> list[dict]:
        """Unsettled E3 pods from durable history (fix A4); [] (and an error) if unreadable."""
        try:
            return [u for u in self.d.unsettled() if u.get("pod_id")]
        except Exception as e:  # noqa: BLE001
            if res is not None:
                res["errors"].append(f"unsettled_pods: {type(e).__name__}: {e}")
            print(f"unsettled_pods failed: {e}", file=sys.stderr)
            return []

    def _targets(self, slot: str, rid: str | None) -> list[dict]:
        """The pods one cleanup must terminate and settle: the slot's running pod, and every
        unsettled pod of its reservation or slot (also once terminated: fix A4)."""
        out: dict[str, dict] = {}
        pod = self.slot_pod(slot)
        if pod is not None:
            out[pod["pod_id"]] = {
                "pod_id": pod["pod_id"],
                "reservation_id": None,
                "slot": slot,
                "terminated": False,
                "running_slot": True,
            }
        for u in self._unsettled():
            if (rid and u.get("reservation_id") == rid) or (slot and u.get("slot") == slot):
                running = out.get(u["pod_id"], {}).get("running_slot", False)
                out[u["pod_id"]] = {**u, "running_slot": running}
        return list(out.values())

    def _pod_state(self, pod_id: str, rid: str | None) -> tuple[bool, bool]:
        """(terminated per the pod event log, settled: no longer in the durable unsettled list;
        True without a reservation, False when that list cannot be read)."""
        running = any(s["pod_id"] == pod_id and s["running"] for s in B.pod_sessions(self.root))
        if not rid:
            return not running, True
        try:
            settled = all(u.get("pod_id") != pod_id for u in self.d.unsettled())
        except Exception:  # noqa: BLE001 - unknown is not settled
            settled = False
        return not running, settled

    def _down(self, t: dict, rid: str | None = None) -> tuple[bool, bool]:
        """Terminate (if needed), charge and settle one pod, retried; returns (terminated,
        settled). With its reservation known, `pod_down.py --pod-id ID --reservation RID`
        (independent of .env; `--slot` only for the slot's running pod, so a reused slot's .env
        is never cleared for an older pod); otherwise the slot form."""
        pid, trid = t["pod_id"], t.get("reservation_id")
        base = ["uv", "run", "python", "scripts/pod_down.py", "--exp", C.EXP]
        if trid:
            cmd = [*base, "--pod-id", pid, "--reservation", trid]
            if t.get("running_slot") and t.get("slot"):
                cmd += ["--slot", t["slot"]]
        else:
            cmd = [*base, "--slot", t["slot"]]
            if rid and pid in (self.d.rr.get(rid) or {}).get("pods", []):
                cmd += ["--reservation", rid]
                trid = rid
        state = self._pod_state(pid, trid)
        if all(state):
            return state
        for attempt in range(1, TERMINATE_ATTEMPTS + 1):
            self.run(cmd, timeout=self.d.terminate_timeout_s)
            state = self._pod_state(pid, trid)
            if all(state):
                return state
            if attempt < TERMINATE_ATTEMPTS:
                self.d.sleep(self.d.terminate_wait_s)
        return state

    def _terminate(self, slot: str, rid: str | None) -> tuple[bool, bool, list[dict]]:
        terminated = settled = True
        pods = []
        for t in self._targets(slot, rid):
            term, sett = self._down(t, rid)
            pods.append({"pod_id": t["pod_id"], "terminated": term, "settled": sett})
            terminated, settled = terminated and term, settled and sett
        return terminated, settled, pods

    def _close_reservation(self, rid: str | None, terminated: bool) -> str:
        if rid is None:
            return "no reservation"
        rr = self.d.rr
        R = rr.get(rid)
        if R is None:
            return "unknown reservation"
        if not terminated:
            return "kept: pod not confirmed terminated"
        if not R.get("launched"):
            return self._resolve_creating(rid, "e3_orchestrate cleanup")[1]
        st = rr.state(exp=C.EXP)["pods"]
        open_pods = [p for p in R.get("pods", []) if not st.get(p, {}).get("settled")]
        if open_pods:  # the charge goes through pod_down only (it records it in the ledger)
            return f"kept: pod(s) {open_pods} terminated but not settled; recover retries"
        rr.release(rid, note="e3_orchestrate cleanup")
        R = rr.get(rid) or {}
        return "closed" if R.get("closed") else "released (unused part)"

    def _snapshot(self, res: dict) -> str | None:
        try:
            n = self.d.rr.snapshot(C.EXP, self.root / B.SNAPSHOT_FILE)
            return f"{n} rows"
        except Exception as e:  # noqa: BLE001
            res.setdefault("errors", []).append(f"snapshot: {type(e).__name__}: {e}")
            return None

    # -- re-arm and the API wrappers ----------------------------------------------------------

    def _cleanup_for_hook(self, slot: str, reason: str) -> int:
        return self.cleanup(slot, reason, outcome="aborted")

    def rearm(self) -> int:
        try:
            B.after_api_change(
                C.EXP,
                root=self.root,
                runner=self.d.runner,
                cleanup=self._cleanup_for_hook,
                rr=self.d.rr,
            )
        except B.RearmFailed as e:
            self.emit("rearm", {"event": "rearm_failed", "error": str(e)})
            return 1
        self.emit(
            "rearm", {"event": "rearm_ok", "slots": [s["slot"] for s in B.running_slots(self.root)]}
        )
        return 0

    def api(self, op: str, usd: float | None = None, note: str = "", rid: str | None = None) -> int:
        sp = self.d.spend
        if op == "reserve":
            refused = self.refuse_if_stopped(f"api-{op}", usd=usd, note=note)
            if refused is not None:
                return refused
        try:
            if op == "reserve":
                rid = sp.reserve_api(C.EXP, usd, note)
            elif op == "settle":
                sp.settle_api(rid, usd, exp=C.EXP)
            elif op == "release":
                sp.release_api(rid, exp=C.EXP)
        except SystemExit as e:  # BudgetExceeded
            return self.stop(f"api-{op}", "API guard refused", why=str(e), usd=usd, note=note)
        out = {"event": f"api_{op}", "reservation_id": rid, "usd": usd, "note": note}
        try:
            B.after_api_change(
                C.EXP,
                root=self.root,
                runner=self.d.runner,
                cleanup=self._cleanup_for_hook,
                rr=self.d.rr,
            )
        except B.RearmFailed as e:
            self.emit(f"api-{op}", {**out, "rearm": "failed", "error": str(e)})
            return 1
        self.emit(f"api-{op}", out)
        return 0

    # -- recover (every <= 2 minutes from the watchdog) ---------------------------------------

    def enforce(self) -> int:
        """The deadline-enforcement pass (review A r2, 1): every unarmed or overdue pod is
        terminated now, without retrieval (an unarmed pod has no job; an overdue one is past its
        budget), and every overdue `creating` row is reconciled with the API. Never waits on a
        retrieval: it takes no retrieval lock, only the short per-slot cleanup lock."""
        with _flock(self.root / "logs" / "E3-enforce.lock", blocking=False) as held:
            if not held:
                self.emit("enforce", {"event": "enforce_busy"})
                return 0
            acts: list[dict] = []
            worst = self._enforce_deadlines(acts)
            self.emit("enforce", {"event": "enforce", "actions": acts})
            return worst

    def _enforce_deadlines(self, acts: list[dict]) -> int:
        """Dispatch every overdue launched pod's termination first; then hand the overdue
        `creating` rows to one background `reconcile-creating` process (review A2 r1, 1).
        Nothing here blocks on a pod_down or an API call."""
        creating = []
        for o in self.d.rr.unarmed_overdue(self.d.now().timestamp()):
            if o.get("exp") != C.EXP:
                continue
            slot = o.get("slot")
            if o.get("pod_id") or (slot and self.slot_pod(slot)):
                self._dispatch_termination(o, acts)
            else:
                creating.append(o["reservation_id"])
        if creating:
            if _lock_busy(self.root / CREATING_LOCK):
                acts.append({"creating": creating, "skipped": "a reconciliation is running"})
            else:
                cmd = ["uv", "run", "python", "scripts/e3_orchestrate.py", "reconcile-creating"]
                (self.d.spawn or self._spawn_detached)(cmd, "reconcile-creating")
                acts.append({"creating": creating, "dispatched": True})
        return 0

    def reconcile_creating(self) -> int:
        """Reconcile every overdue `creating` row with the API (a row is released only once
        the API proves no pod exists; an untracked pod of its name is reaped first). One at a
        time: a second pass while one runs returns at once."""
        with _flock(self.root / CREATING_LOCK, blocking=False) as held:
            if not held:
                self.emit("reconcile-creating", {"event": "reconcile_busy"})
                return 0
            acts, worst = [], 0
            for o in self.d.rr.unarmed_overdue(self.d.now().timestamp()):
                if o.get("exp") != C.EXP or o.get("kind") != "creating":
                    continue
                closed, detail = self._resolve_creating(o["reservation_id"], "recover")
                acts.append({"creating": o["reservation_id"], "closed": closed, "detail": detail})
                if not closed and "still listed" in detail:
                    worst = max(worst, 3)
            self.emit("reconcile-creating", {"event": "reconcile_creating", "actions": acts})
            return worst

    def _dispatch_termination(self, o: dict, acts: list[dict]) -> None:
        """One detached `cleanup --no-retrieve --enforced` per overdue pod (review A r3, 1):
        each pod's termination and settlement runs on its own, bounded by its pod_down calls, and
        never waits for another pod's. Skipped while the slot's cleanup lock is held (that pod's
        termination is already running)."""
        slot = o.get("slot")
        if not slot:
            acts.append({"pod": o.get("pod_id"), "skipped": "no slot recorded; recover settles"})
            return
        if _lock_busy(self.root / "logs" / f"E3-cleanup{slot}.lock"):
            acts.append({"cleanup": slot, "reason": o["reason"], "skipped": "terminating"})
            return
        reason = f"enforce: {o['reason']} (terminated without retrieval)"
        cmd = [
            "uv",
            "run",
            "python",
            "scripts/e3_orchestrate.py",
            "cleanup",
            "--slot",
            slot,
            "--reason",
            reason,
            "--no-retrieve",
            "--enforced",
            "--outcome",
            "aborted",
            *(["--reservation", o["reservation_id"]] if o.get("reservation_id") else []),
        ]
        spawn = self.d.spawn or self._spawn_detached
        spawn(cmd, f"enforce{slot}")
        self.log({"event": "termination_dispatched", "unit": None, "slot": slot, "reason": reason})
        acts.append({"cleanup": slot, "reason": o["reason"], "dispatched": True})

    def _spawn_cleanup(self, slot: str, reason: str, acts: list[dict], outcome=None) -> None:
        """A retrieval-bearing cleanup in its own background process (review A r2, 1), unless
        one is already working on the slot (its retrieval or cleanup lock is held)."""
        logs = self.root / "logs"
        if _lock_busy(logs / f"E3-retrieve{slot}.lock") or _lock_busy(
            logs / f"E3-cleanup{slot}.lock"
        ):
            acts.append({"cleanup": slot, "reason": reason, "skipped": "a cleanup is running"})
            return
        cmd = [
            "uv",
            "run",
            "python",
            "scripts/e3_orchestrate.py",
            "cleanup",
            "--slot",
            slot,
            "--reason",
            reason,
            *(["--outcome", outcome] if outcome else []),
        ]
        spawn = self.d.spawn or self._spawn_detached
        spawn(cmd, f"cleanup{slot}")
        self.log({"event": "cleanup_spawned", "unit": None, "slot": slot, "reason": reason})
        acts.append({"cleanup": slot, "reason": reason, "spawned": True})

    def _spawn_detached(self, cmd: list[str], name: str) -> None:
        log = self.root / "logs" / f"E3-bg-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def recover(self) -> int:
        with _flock(self.root / "logs" / "E3-recover.lock", blocking=False) as held:
            if not held:
                self.emit("recover", {"event": "recover_busy"})
                return 0
            return self._recover_locked()

    def _recover_locked(self) -> int:
        rr, acts, worst = self.d.rr, [], 0
        now = self.d.now().timestamp()

        def clean(slot, reason, rid=None, outcome="aborted"):  # in the background, per slot
            self._spawn_cleanup(slot, reason, acts, outcome=outcome)

        # terminated (e.g. by the watchdog's --all-exp) but unsettled pods: charge and settle
        # them from durable history, independent of the running slots and .env (fix A4)
        for u in self._unsettled():
            if u.get("terminated") and u.get("reservation_id"):
                _term, sett = self._down({**u, "running_slot": False})
                acts.append({"settle_terminated": u["pod_id"], "settled": sett})

        # unarmed / overdue pods and creating rows: terminated now, no retrieval (r2, 1)
        worst = max(worst, self._enforce_deadlines(acts))
        api = self.d.api_pods()

        known = {p["pod_id"] for p in B.pod_sessions(self.root)}
        if api is not None and any(p.get("id") not in known for p in api):
            rc, _ = self.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/pod_down.py",
                    "--exp",
                    C.EXP,
                    "--untracked",
                    "--reason",
                    "recover: untracked e3 pod",
                ],
                timeout=self.d.terminate_timeout_s,
            )
            acts.append(
                {"reap_untracked": [p.get("id") for p in api if p.get("id") not in known], "rc": rc}
            )
        # stopped (e.g. a killswitch self-stop) or gone. "Gone" must be seen by two recover
        # passes in a row, so one odd listing never tears down a healthy pod.
        missing_now: list[str] = []
        if api is not None:
            prev = next(
                (e for e in reversed(B.read_log(self.root)) if e.get("event") == "recover"), {}
            )
            ids = {p.get("id"): p for p in api}
            for s in B.running_slots(self.root):
                a = ids.get(s["pod_id"])
                if a is not None and str(a.get("desiredStatus", "")).upper() in STOP_STATUSES:
                    clean(
                        s["slot"], f"recover: pod stopped (desiredStatus {a.get('desiredStatus')})"
                    )
                elif a is None and s["pod_id"] in prev.get("missing_from_api", []):
                    clean(s["slot"], "recover: pod gone from the API (two passes)")
                elif a is None:
                    missing_now.append(s["pod_id"])

        # idle: the unit's final completion record is pulled and verified, its pod still runs
        for s in B.running_slots(self.root):
            unit = self.slot_launch(s["slot"]).get("unit")
            if unit and B.unit_complete(self.root, unit):
                done = self.root / B.done_path(unit)
                idle_min = (now - done.stat().st_mtime) / 60
                if idle_min >= C.IDLE_POD_MINUTES:
                    clean(
                        s["slot"],
                        f"recover: idle {idle_min:.0f} min after its results were verified",
                        outcome=None,  # "done" once the final pull verifies
                    )

        # reservations whose launcher died and whose slot has no pod. One with a `creating`
        # row is left to the overdue pass above, which reconciles it with the API (fix A1).
        seen: dict[str, dict] = {}
        for e in B.read_log(self.root):
            if e.get("event") == "reserved":
                seen[e["reservation_id"]] = e
        for rid, e in seen.items():
            R = rr.get(rid)
            if R is None or R.get("closed"):
                continue
            if self.d.pid_alive(e.get("pid", -1)) or self.slot_pod(e["slot"]) is not None:
                continue
            if R.get("launched"):
                if not all(
                    rr.state(exp=C.EXP)["pods"].get(p, {}).get("settled") for p in R["pods"]
                ):
                    continue  # a launched pod still carries liability: cleanup settles it
                if R.get("released"):
                    continue
            elif R.get("creating"):
                continue
            rr.release(rid, note="recover: launcher gone, no pod")
            acts.append({"released": rid, "why": "launcher gone, no pod"})

        # deferred loss decisions whose retrieval holder finished or died (review A2 r2)
        self._swept = False
        worst = max(worst, self._resolve_pending_losses(acts))

        # a §7 stop whose shutdown did not finish (e.g. its process died): finish it, skipping
        # any slot whose retrieval is in progress elsewhere (not again in a pass that swept)
        if B.read_stopped(self.root) is not None and not self._stopping and not self._swept:
            with _flock(self.root / CREATE_LOCK, blocking=False) as held:
                if not held:
                    acts.append({"stopped": "a launch or stop holds the create lock; next pass"})
                else:
                    self._stopping = True
                    try:
                        rc = self._shutdown_all("recover: finishing the §7 stop", spawn=True)
                    finally:
                        self._stopping = False
                    acts.append({"stopped": "shutdown re-run", "rc": rc})
                    worst = max(worst, rc)
        self.emit("recover", {"event": "recover", "actions": acts, "missing_from_api": missing_now})
        return worst

    # -- the terminal shutdown (every §7 stop, the wall-clock cap) ----------------------------

    def _shutdown_all(self, reason: str, spawn: bool = False, retrieve: bool = True) -> int:
        """Bounded cleanup of every outstanding E3 pod, then settle or release every open RunPod
        and API reservation (fix A8). Returns 3 if a pod or a creation is still unresolved.
        `spawn` (recover): each running slot's cleanup goes to a background process, and only
        terminated pods are settled here."""
        acts, worst = [], 0
        slots = B.running_slots(self.root)
        for s in slots:
            if spawn:
                self._spawn_cleanup(s["slot"], reason, acts)
                continue
            rc = self.cleanup(s["slot"], reason, retrieve=retrieve)
            acts.append({"cleanup": s["slot"], "rc": rc})
            worst = max(worst, 3 if rc == 3 else 0)
        for u in self._unsettled():  # pods outside any running slot (terminated, or slotless)
            if spawn and not u.get("terminated"):
                continue  # a running pod: its background cleanup terminates it
            term, sett = self._down({**u, "running_slot": False})
            acts.append({"pod": u["pod_id"], "terminated": term, "settled": sett})
            if not term:
                worst = 3
        rr = self.d.rr
        for rid, R in rr.state(exp=C.EXP)["reservations"].items():
            if R.get("closed"):
                continue
            try:
                if not R.get("launched"):
                    closed, detail = self._resolve_creating(rid, reason)
                    acts.append({"runpod": rid, "closed": closed, "detail": detail})
                    if not closed:
                        worst = 3
                elif not R.get("released") and all(
                    rr.state(exp=C.EXP)["pods"].get(p, {}).get("settled") for p in R["pods"]
                ):
                    rr.release(rid, note=reason)
                    acts.append({"runpod": rid, "released": True})
            except Exception as e:  # noqa: BLE001
                acts.append({"runpod": rid, "error": f"{type(e).__name__}: {e}"})
        for rid, s in self.d.spend.open_reservations(C.EXP).items():
            try:
                if s.get("submitted"):  # paid calls may have run: settle conservatively (full)
                    self.d.spend.settle_api(rid, None, exp=C.EXP, note=reason)
                    acts.append({"settled_api": rid})
                else:
                    self.d.spend.release_api(rid, exp=C.EXP)
                    acts.append({"released_api": rid})
            except (Exception, SystemExit) as e:  # noqa: BLE001
                acts.append({"api": rid, "error": f"{type(e).__name__}: {e}"})
        self.emit("shutdown", {"event": "shutdown", "reason": reason, "actions": acts})
        return worst

    # -- the wall-clock cap -------------------------------------------------------------------

    def deadline_check(self) -> int:
        now = self.d.now()
        if not B.deadline_passed(now):
            self.emit(
                "deadline-check",
                {
                    "event": "deadline_ok",
                    "left_h": round(B.deadline_left(now).total_seconds() / 3600, 3),
                },
            )
            return 0
        return self.stop("deadline-check", B.STOP_WALL_CLOCK)

    # -- mark and status ----------------------------------------------------------------------

    def mark(self, unit: str, status: str) -> int:
        if unit not in B.UNITS or status not in ("pending", "done", "skipped"):
            raise SystemExit(f"unit in {B.UNITS}, status in pending|done|skipped")
        self.emit("mark", {"event": "mark", "unit": unit, "status": status})
        return 0

    def status_block(self, phase=None, gate=None, reviews="n/a", next_action=None) -> str:
        st = B.gather_state(self.root, self.d.spend)
        t = self.d.spend.totals(C.EXP)
        rs = self.d.rr.state(exp=C.EXP)
        unlaunched = sum(
            R.get("open_usd", 0.0)
            for R in rs["reservations"].values()
            if not R.get("closed") and not R.get("launched")
        )
        open_gpu = st["open"]["gpu"] + unlaunched
        gpu, api = float(t["gpu_usd"]), float(t["judge_usd"])
        pods = []
        for s in B.running_slots(self.root):
            dl = self.d.rr.pod_deadline(s["pod_id"])
            pods.append(f"e3-{s['slot']} ({s['pod_id']}, deadline {_utc(dl)})")
        log = B.read_log(self.root)
        stopped = (B.read_stopped(self.root) or {}).get("rule") or next(
            (e["stop"] for e in reversed(log) if e.get("event") == "stop"), None
        )
        now = self.d.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"E3 STATUS — {now}",
            f"phase:        {phase or self._phase(st, stopped)}",
            f"gate result:  {gate or self._gate()}",
            (
                f"spend vs cap: GPU ${gpu:.2f} / ${C.GPU_LIMIT_USD:.0f}, API ${api:.2f} / "
                f"${C.API_LIMIT_USD:.0f}, total ${gpu + api:.2f} / ${C.HARD_CAP_USD:.0f} "
                f"(open reservations: GPU ${open_gpu:.2f}, API "
                f"${float(t.get('judge_reserved_usd', 0.0)):.2f}; reviews: {reviews})"
            ),
            f"pods running: {', '.join(pods) if pods else 'none'}",
            f"next action:  {next_action or (f'stopped: {stopped}' if stopped else 'see run log')}",
        ]
        return "\n".join(lines)

    def _phase(self, st: dict, stopped: str | None) -> str:
        if stopped:
            return "STOPPED"
        u = st["units"]
        if u["pod3"] == "running":
            return "confirmation"
        if "running" in (u["pod1"], u["pod2"]):
            return "Stage A pods"
        for name, label in (
            ("claims", "report"),
            ("stageB", "Stage B rules"),
            ("unseal", "Stage B rules"),
            ("stageA", "Stage A gate"),
        ):
            if (self.root / C.rule_path(name)).exists():
                return label
        return "smoke" if u["pairs"] == "done" else "pair build"

    def _gate(self) -> str:
        for name in ("claims", "stageB", "unseal", "power", "stageA", "judges"):
            p = self.root / C.rule_path(name)
            if p.exists():
                try:
                    out = json.loads(p.read_text()).get("outcome", "?")
                except ValueError:
                    out = "unreadable"
                return f"{name}: {out}"
        return "none yet"

    def status(self, **kw) -> int:
        block = self.status_block(**kw)
        print(block, flush=True)
        self.log({"cmd": "status", "event": "status", "block": block})
        return 0


# --- starting the pod job (main session, after U8c's report: pod_push only syncs/arms/sets up) ---

JOB_RETRY_SHARE = {"pod1": 1, "pod2": 1}  # §8.6: each Stage pod reserves one retry; pod 3 the rest


def job_argv(unit: str, root: Path = REPO_ROOT) -> list[str]:
    """The `scripts/pod_run_e3.sh` arguments for a unit, from the prereg's plan and the recorded
    decisions: pod 2's sampling from `results/E3/cuts.json` (cut 2, decided before its launch);
    pod 3's checkpoints from `stageB.json["confirmation"]["targets"]` (cut 1 leaves only w*)."""
    if unit == "smoke":
        return ["--smoke"]
    if unit == "pod1":
        return ["--pod", "1", "--ckpts", *C.STAGE_A, "--seeds", str(C.TRAIN_SEED)]
    if unit == "pod2":
        cuts_file = root / C.RESULTS / "cuts.json"
        if not cuts_file.exists():
            raise SystemExit("pod2: results/E3/cuts.json (the cut-2 decision) is missing")
        sampling = json.loads(cuts_file.read_text()).get("pod2_sampling")
        if sampling not in (True, False):
            raise SystemExit(f"pod2: cuts.json pod2_sampling must be true/false, got {sampling!r}")
        argv = ["--pod", "2", "--ckpts", *C.STAGE_B, "--seeds", str(C.TRAIN_SEED), "--rescore-ref"]
        return argv + ([] if sampling else ["--no-sample"])
    if unit == "pod3":
        conf = json.loads((root / C.rule_path("stageB")).read_text())["confirmation"]
        if conf.get("outcome") != "pending" or not conf.get("targets"):
            raise SystemExit(f"pod3: confirmation is {conf.get('outcome')!r}; nothing to run")
        targets = [t for t in conf["targets"] if t != C.REFERENCE]  # the reference needs no seed 3
        return ["--pod", "3", "--ckpts", *targets, "--seeds", str(C.CONFIRM_SEED), "--no-sample"]
    raise SystemExit(f"unknown unit {unit!r}")


def start_job(unit: str, slot: str, root: Path = REPO_ROOT, wait_s: float = 20.0) -> int:
    """Start the pod job detached (setsid nohup, rc file), then verify real progress after
    `wait_s`: a new `logs/pod_run_e3.<unit>.*.log` with lines (CLAUDE.md: verify a detached
    launch by progress, never by an immediate pgrep)."""
    from dotenv import dotenv_values

    stopped = B.read_stopped(root)
    if stopped is not None:
        print(f"E3_ORCH job: refused, E3 is stopped ({stopped.get('rule')})", file=sys.stderr)
        return 4
    if unit == "pod3" and not B.review_17r_passed(root):
        print(
            f"E3_ORCH job: refused, pod 3 needs {B.REVIEW_17R_FILE} with outcome pass "
            "(no speculative work in E3)",
            file=sys.stderr,
        )
        return 4
    env = dotenv_values(root / ".env")
    host, port = env.get(f"POD_SSH_HOST{slot}"), env.get(f"POD_SSH_PORT{slot}")
    user = env.get("POD_SSH_USER") or "root"
    if not host or not port:
        print(f"E3_ORCH job: no SSH address for slot {slot} in .env", file=sys.stderr)
        return 4
    argv = job_argv(unit, root)
    retries = JOB_RETRY_SHARE.get(unit)
    if retries is None:
        retries = B.retries_left(root / C.TRAIN_DIR)
    tag = unit if unit != "smoke" else "smoke"
    # recorded before the launch: from here on cleanup treats the pod as holding results
    B.append_log(
        root,
        {
            "cmd": "job",
            "event": "job_starting",
            "unit": None,
            "job_unit": unit,
            "slot": slot,
            "argv": argv,
            "retries_left": retries,
        },
    )
    remote = (
        "cd /ws/emx && mkdir -p logs && "
        f"setsid nohup env E3_RETRIES_LEFT={int(retries)} bash -c "
        f"'bash scripts/pod_run_e3.sh {' '.join(argv)}; echo $? > logs/pod_run_e3.{tag}.rc' "
        ">/dev/null 2>&1 </dev/null & echo launched"
    )
    ssh = [
        "ssh",
        "-n",
        "-p",
        str(port),
        "-o",
        "ConnectTimeout=20",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
    ]
    out = subprocess.run([*ssh, remote], capture_output=True, text=True, timeout=90, check=False)
    if out.returncode != 0 or "launched" not in out.stdout:
        print(
            f"E3_ORCH job: launch failed ({out.returncode}): {out.stderr[-500:]}", file=sys.stderr
        )
        return 1
    time.sleep(wait_s)
    newest = f"ls -t /ws/emx/logs/pod_run_e3.{tag}.*.log 2>/dev/null | head -1 | xargs -r wc -l"
    check = subprocess.run([*ssh, newest], capture_output=True, text=True, timeout=90, check=False)
    lines = check.stdout.split()
    ok = bool(lines) and lines[0].isdigit() and int(lines[0]) > 0
    rec = {
        "cmd": "job",
        "unit": unit,
        "slot": slot,
        "argv": argv,
        "retries_left": retries,
        "progress": check.stdout.strip(),
        "ok": ok,
        "utc": _utc(time.time()),
    }
    with open(root / "logs" / "E3-orchestrate.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("E3_ORCH " + json.dumps(rec), flush=True)
    return 0 if ok else 1


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    ap = argparse.ArgumentParser(prog="e3_orchestrate.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("project")
    s = sub.add_parser("launch")
    s.add_argument("--unit", required=True, choices=B.POD_UNITS)
    s.add_argument("--slot", required=True)
    s.add_argument("--budget-usd", type=float)
    s.add_argument("--gpu")
    s = sub.add_parser("cleanup")
    s.add_argument("--slot", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--no-retrieve", action="store_true")
    s.add_argument("--reservation")
    s.add_argument("--outcome", choices=("done", "aborted"))
    s.add_argument(
        "--enforced",
        action="store_true",
        help="terminated for its deadline: a started job's unretrieved results are a §7 stop",
    )
    sub.add_parser("rearm")
    s = sub.add_parser("api-reserve")
    s.add_argument("--usd", type=float, required=True)
    s.add_argument("--note", required=True)
    s = sub.add_parser("api-settle")
    s.add_argument("--id", required=True)
    s.add_argument("--usd", type=float, help="the reservation's TOTAL actual cost")
    s = sub.add_parser("api-release")
    s.add_argument("--id", required=True)
    sub.add_parser("recover")
    sub.add_parser("enforce")
    sub.add_parser("reconcile-creating")
    sub.add_parser("deadline-check")
    s = sub.add_parser("stop")
    s.add_argument("--rule", required=True, help="the §7 stopping rule that fired")
    s.add_argument("--detail", default="")
    s.add_argument("--no-retrieve", action="store_true", help="the pods are already terminated")
    s = sub.add_parser("mark")
    s.add_argument("--unit", required=True, choices=B.UNITS)
    s.add_argument("--status", required=True, choices=("pending", "done", "skipped"))
    s = sub.add_parser("job")
    s.add_argument("--unit", required=True, choices=("smoke", "pod1", "pod2", "pod3"))
    s.add_argument("--slot", required=True)
    s.add_argument("--wait-s", type=float, default=20.0)
    s = sub.add_parser("status")
    s.add_argument("--phase")
    s.add_argument("--gate")
    s.add_argument("--reviews", default="n/a")
    s.add_argument("--next", dest="next_action")
    for p in sub.choices.values():
        p.add_argument("--retrieval-wait-s", type=float, default=180.0)
    a = ap.parse_args(argv)
    if a.cmd == "job":
        return start_job(a.unit, a.slot, wait_s=a.wait_s)
    deps = deps or real_deps()
    deps.retrieval_wait_s = a.retrieval_wait_s
    o = Orchestrator(deps)
    if a.cmd == "project":
        return o.project()
    if a.cmd == "launch":
        return o.launch(a.unit, a.slot, a.budget_usd, a.gpu)
    if a.cmd == "cleanup":
        return o.cleanup(
            a.slot, a.reason, not a.no_retrieve, a.reservation, a.outcome, enforced=a.enforced
        )
    if a.cmd == "rearm":
        return o.rearm()
    if a.cmd == "api-reserve":
        return o.api("reserve", a.usd, a.note)
    if a.cmd == "api-settle":
        return o.api("settle", a.usd, rid=a.id)
    if a.cmd == "api-release":
        return o.api("release", rid=a.id)
    if a.cmd == "recover":
        return o.recover()
    if a.cmd == "enforce":
        return o.enforce()
    if a.cmd == "reconcile-creating":
        return o.reconcile_creating()
    if a.cmd == "deadline-check":
        return o.deadline_check()
    if a.cmd == "stop":
        return o.stop("stop", a.rule, retrieve=not a.no_retrieve, detail=a.detail)
    if a.cmd == "mark":
        return o.mark(a.unit, a.status)
    return o.status(phase=a.phase, gate=a.gate, reviews=a.reviews, next_action=a.next_action)


if __name__ == "__main__":
    sys.exit(main())
