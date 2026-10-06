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
  recover                                  unarmed / overdue / stopped / untracked pods, orphans
  deadline-check                           at the 5-day cap: clean up everything, release, stop
  mark --unit U --status pending|done|skipped
  status [--phase P] [--gate G] [--reviews R] [--next N]

Exit codes: 0 ok; 1 a failure that was cleaned up (or artifacts missing after a terminated
pod); 2 a §7 stop (projection over a limit after cuts, wall-clock cap, API guard refusal);
3 a pod NOT confirmed terminated (its liability is kept); 4 refused before anything was spent.
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
STOP_STATUSES = ("EXITED", "TERMINATED", "STOPPED")
TERMINATE_ATTEMPTS = 3


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

    return Deps(root=root, runner=runner, rr=rr, spend=spend, api_pods=api_pods, pod_usd=pod_usd)


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

    # -- small helpers ------------------------------------------------------------------------

    def log(self, event: dict) -> dict:
        return B.append_log(self.root, event)

    def emit(self, cmd: str, summary: dict) -> None:
        line = {"cmd": cmd, **summary}
        print("E3_ORCH " + json.dumps(line, default=str, sort_keys=True), flush=True)
        self.log({**line, "event": line.get("event", cmd)})

    def stop(self, cmd: str, rule: str, **extra) -> int:
        self.emit(cmd, {"event": "stop", "stop": rule, **extra})
        print(f"E3_STOP {rule}", flush=True)
        return 2

    def run(self, cmd: list[str], env: dict | None = None) -> tuple[int, str]:
        rc, out = self.d.runner(cmd, root=self.root, env=env)
        sys.stderr.write(out if out.endswith("\n") or not out else out + "\n")
        return rc, out

    def slot_pod(self, slot: str) -> dict | None:
        return next((p for p in B.running_slots(self.root) if p["slot"] == slot), None)

    def slot_launch(self, slot: str) -> dict:
        """The latest reserved/launched event for this slot (unit and reservation id)."""
        out: dict = {}
        for e in B.read_log(self.root):
            if e.get("slot") == slot and e.get("event") in ("reserved", "launched"):
                out = {
                    **out,
                    **{k: e[k] for k in ("unit", "reservation_id") if k in e},
                    "launched": e["event"] == "launched" or out.get("launched", False),
                }
            elif e.get("slot") == slot and e.get("event") == "cleaned":
                out = {}
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
        if q.get("stop"):
            return self.stop("project", q["stop"], **summary)
        self.emit("project", summary)
        return 0

    # -- launch -------------------------------------------------------------------------------

    def launch(
        self, unit: str, slot: str, budget_usd: float | None = None, gpu: str | None = None
    ) -> int:
        if unit not in B.POD_UNITS:
            raise SystemExit(f"--unit must be one of {B.POD_UNITS}")
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
        try:
            rid = self.d.rr.wait_and_reserve(
                C.EXP,
                res_usd,
                f"E3 {unit} slot {slot}",
                give_up_at=give_up,
                poll_s=self.d.wait_poll_s,
                sleep=self.d.sleep,
                clock=lambda: self.d.now().timestamp(),
                log=lambda m: self.log(
                    {"event": "reservation_wait", "unit": None, "slot": slot, "message": m}
                ),
            )
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

        stage = "pod_up"
        try:
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
            rc, out = self.run(cmd)
            if rc != 0:
                return self._launch_failed(unit, slot, rid, f"pod_up exited {rc}")
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

    def _launch_failed(self, unit: str, slot: str, rid: str, why: str) -> int:
        """After the reservation: a pod that exists goes through cleanup; otherwise the
        reservation is released (idempotent: pod_up may already have released it)."""
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
        if self.slot_pod(slot) is not None:
            rc = self.cleanup(
                slot, f"launch failed: {why}", retrieve=False, reservation_id=rid, outcome="aborted"
            )
            return 3 if rc == 3 else 1
        try:
            self.d.rr.release(rid, note=f"launch failed: {why}")
        except Exception as e:  # noqa: BLE001
            print(f"release of {rid} failed: {e}", file=sys.stderr)
        self.emit(
            "launch",
            {
                "event": "launch_failed",
                "unit": unit,
                "slot": slot,
                "reservation_id": rid,
                "why": why,
                "pod": None,
            },
        )
        return 1

    # -- cleanup: the unconditional path (§8.6) -----------------------------------------------

    def cleanup(
        self,
        slot: str,
        reason: str,
        retrieve: bool = True,
        reservation_id: str | None = None,
        outcome: str | None = None,
    ) -> int:
        with _flock(self.root / "logs" / f"E3-cleanup{slot}.lock"):
            return self._cleanup_locked(slot, reason, retrieve, reservation_id, outcome)

    def _cleanup_locked(self, slot, reason, retrieve, reservation_id, outcome) -> int:
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
            "reservation": None,
            "snapshot": None,
            "errors": [],
        }
        if pod is None and not info and reservation_id is None:
            res.update(terminated=True, retrieved=None, already_clean=True)
            res["snapshot"] = self._snapshot(res)
            res["unit"] = None  # nothing launched here: no unit disposition changes
            self.emit("cleanup", res)
            return 0
        try:
            # 1. pull and verify, at most RETRIEVAL_ATTEMPTS times, a few minutes apart
            if retrieve and pod is not None and unit:
                res["retrieved"] = False
                for attempt in range(1, C.RETRIEVAL_ATTEMPTS + 1):
                    rc, out = self.run(
                        [
                            "bash",
                            "scripts/pod_pull_results.sh",
                            "--exp",
                            C.EXP,
                            "--slot",
                            slot,
                            "--unit",
                            unit,
                        ]
                    )
                    res["attempts"] = attempt
                    if rc == 0:
                        res["retrieved"] = True
                        break
                    res["last_pull_output"] = out[-2000:]
                    if attempt < C.RETRIEVAL_ATTEMPTS:
                        self.d.sleep(self.d.retrieval_wait_s)
            # 2. record whatever is missing or unverified
            if retrieve and res["retrieved"] is not True:
                why = (
                    "no running pod to pull from"
                    if pod is None
                    else "no unit recorded for this slot"
                    if not unit
                    else f"not verified after {res.get('attempts', 0)} attempts"
                )
                res["missing"] = self._record_missing(
                    unit, slot, why, res.get("last_pull_output", "")
                )
        except Exception as e:  # noqa: BLE001 - steps 3-5 still run
            res["errors"].append(f"retrieval: {type(e).__name__}: {e}")
        finally:
            # 3. terminate regardless of 1-2 (retried)
            try:
                res["terminated"] = self._terminate(slot, rid, pod)
            except Exception as e:  # noqa: BLE001
                res["terminated"] = False
                res["errors"].append(f"terminate: {type(e).__name__}: {e}")
            # 4. settle/release the reservation, never for a pod not confirmed terminated
            try:
                res["reservation"] = self._close_reservation(rid, pod, res["terminated"])
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"reservation: {type(e).__name__}: {e}")
            # 5. snapshot the E3 reservation rows
            res["snapshot"] = self._snapshot(res)
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
            return 3
        return 0 if (res["retrieved"] in (True, None) and not res["missing"]) else 1

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

    def _terminate(self, slot: str, rid: str | None, pod: dict | None) -> bool:
        if pod is None:
            return True  # the pod event log shows nothing running in this slot
        cmd = ["uv", "run", "python", "scripts/pod_down.py", "--exp", C.EXP, "--slot", slot]
        if rid:
            cmd += ["--reservation", rid]
        for attempt in range(1, TERMINATE_ATTEMPTS + 1):
            rc, _ = self.run(cmd)
            if rc == 0 and self.slot_pod(slot) is None:
                return True
            if attempt < TERMINATE_ATTEMPTS:
                self.d.sleep(self.d.terminate_wait_s)
        return False

    def _close_reservation(self, rid: str | None, pod: dict | None, terminated: bool) -> str:
        if rid is None:
            return "no reservation"
        rr = self.d.rr
        R = rr.get(rid)
        if R is None:
            return "unknown reservation"
        if not terminated:
            return "kept: pod not confirmed terminated"
        pid = pod and pod["pod_id"]
        if pid and pid in R.get("pods", []):
            st = rr.state(exp=C.EXP)["pods"].get(pid, {})
            if not st.get("settled"):
                usd = self.d.pod_usd(pid)
                if usd is None:
                    return "kept: pod terminated but its dollars are unknown; settle by hand"
                rr.settle(rid, usd, pod_id=pid, note="e3_orchestrate cleanup")
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

    def recover(self) -> int:
        with _flock(self.root / "logs" / "E3-recover.lock", blocking=False) as held:
            if not held:
                self.emit("recover", {"event": "recover_busy"})
                return 0
            return self._recover_locked()

    def _recover_locked(self) -> int:
        rr, acts, worst = self.d.rr, [], 0
        now = self.d.now().timestamp()

        def clean(slot, reason, rid=None, outcome="aborted"):
            nonlocal worst
            rc = self.cleanup(slot, reason, reservation_id=rid, outcome=outcome)
            acts.append({"cleanup": slot, "reason": reason, "rc": rc})
            worst = max(worst, rc)

        api = self.d.api_pods()
        by_name = {p.get("name"): p for p in (api or [])}
        for o in rr.unarmed_overdue(now):
            if o.get("exp") != C.EXP:
                continue
            slot = o.get("slot")
            if o.get("pod_id") or (slot and self.slot_pod(slot)):
                clean(slot, f"recover: {o['reason']}", o.get("reservation_id"))
                continue
            # a `creating` row with no launched pod: reap any untracked pod, then release
            if api is None:
                acts.append({"creating": o["reservation_id"], "kept": "API listing unknown"})
                continue
            if o.get("name") in by_name:
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
                        "recover: creating, no launch",
                    ]
                )
                acts.append({"reap_untracked": o.get("name"), "rc": rc})
                api = self.d.api_pods()
                by_name = {p.get("name"): p for p in (api or [])}
                if api is None or o.get("name") in by_name:
                    acts.append({"creating": o["reservation_id"], "kept": "pod still listed"})
                    worst = max(worst, 3)
                    continue
            rr.release(o["reservation_id"], note="recover: creating row, no pod")
            acts.append({"released": o["reservation_id"], "why": "creating row, no pod"})

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
                ]
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

        # idle: the unit's results are pulled and verified, but its pod still runs
        for s in B.running_slots(self.root):
            unit = self.slot_launch(s["slot"]).get("unit")
            man = self.root / C.manifest_path(unit) if unit else None
            if man is not None and B.manifest_complete(man):
                idle_min = (now - man.stat().st_mtime) / 60
                if idle_min >= C.IDLE_POD_MINUTES:
                    clean(
                        s["slot"],
                        f"recover: idle {idle_min:.0f} min after its results were verified",
                        outcome=None,  # "done" once the final pull verifies
                    )

        # reservations whose launcher died and whose slot has no pod
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
            if R.get("launched") and not all(
                rr.state(exp=C.EXP)["pods"].get(p, {}).get("settled") for p in R["pods"]
            ):
                continue  # a launched pod still carries liability: cleanup settles it
            rr.release(rid, note="recover: launcher gone, no pod")
            acts.append({"released": rid, "why": "launcher gone, no pod"})
        self.emit("recover", {"event": "recover", "actions": acts, "missing_from_api": missing_now})
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
        acts, worst = [], 0
        for s in B.running_slots(self.root):
            rc = self.cleanup(s["slot"], "wall-clock cap", outcome="aborted")
            acts.append({"cleanup": s["slot"], "rc": rc})
            worst = max(worst, rc)
        for rid, R in self.d.rr.state(exp=C.EXP)["reservations"].items():
            if not R.get("closed") and not R.get("launched"):
                self.d.rr.release(rid, note="wall-clock cap")
                acts.append({"released_runpod": rid})
        for rid, s in self.d.spend.open_reservations(C.EXP).items():
            if s.get("submitted"):  # paid calls may have run: settle conservatively (full)
                self.d.spend.settle_api(rid, None, exp=C.EXP, note="wall-clock cap")
                acts.append({"settled_api": rid})
            else:
                self.d.spend.release_api(rid, exp=C.EXP)
                acts.append({"released_api": rid})
        self.stop("deadline-check", B.STOP_WALL_CLOCK, actions=acts)
        return 3 if worst == 3 else 2

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
        stopped = next((e["stop"] for e in reversed(log) if e.get("event") == "stop"), None)
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
    remote = (
        "cd /ws/emx && mkdir -p logs && "
        f"setsid nohup env E3_RETRIES_LEFT={int(retries)} bash -c "
        f"'bash scripts/pod_run_e3.sh {' '.join(argv)}; echo $? > logs/pod_run_e3.{tag}.rc' "
        ">/dev/null 2>&1 </dev/null & echo launched"
    )
    ssh = ["ssh", "-n", "-p", str(port), "-o", "ConnectTimeout=20",
           "-o", "StrictHostKeyChecking=accept-new", f"{user}@{host}"]
    out = subprocess.run([*ssh, remote], capture_output=True, text=True, timeout=90, check=False)
    if out.returncode != 0 or "launched" not in out.stdout:
        print(f"E3_ORCH job: launch failed ({out.returncode}): {out.stderr[-500:]}",
              file=sys.stderr)
        return 1
    time.sleep(wait_s)
    newest = f"ls -t /ws/emx/logs/pod_run_e3.{tag}.*.log 2>/dev/null | head -1 | xargs -r wc -l"
    check = subprocess.run([*ssh, newest], capture_output=True, text=True, timeout=90, check=False)
    lines = check.stdout.split()
    ok = bool(lines) and lines[0].isdigit() and int(lines[0]) > 0
    rec = {"cmd": "job", "unit": unit, "slot": slot, "argv": argv, "retries_left": retries,
           "progress": check.stdout.strip(), "ok": ok, "utc": _utc(time.time())}
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
    sub.add_parser("deadline-check")
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
        return o.cleanup(a.slot, a.reason, not a.no_retrieve, a.reservation, a.outcome)
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
    if a.cmd == "deadline-check":
        return o.deadline_check()
    if a.cmd == "mark":
        return o.mark(a.unit, a.status)
    return o.status(phase=a.phase, gate=a.gate, reviews=a.reviews, next_action=a.next_action)


if __name__ == "__main__":
    sys.exit(main())
