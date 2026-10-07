"""E9 budget: the stage x resource projection, cuts and safe stop, per-pod budgets, the backstop
kill line, and the E9 admission protocol (unit F1a).

Spec: docs/experiments/E9/prereg.md §7.1-§7.4 (Amendment D), §4.11 (the extra-seeds launch-only-if
rule), §7.6 (deadline), §8.4 (automatic gates), §8.6 (the $10 barrier kill);
docs/experiments/E9/implementation-plan.md (row F1a, "Stage enforcement in spend.py", "E9
admission protocol", "Paid steps"). Ported in part from `emx.e3.budget` (the projection and cuts
shape, `after_api_change`, the deadline helpers, `gather_state`); the numbers come only from
`emx.e9.contracts`.

**Admission protocol.** Every E9 paid admission goes through `reserve_api`, `reserve_launch` or
`extend` here, each holding the E9 admission lock (`fcntl.flock` on `contracts.ADMISSION_LOCK`)
for its whole check-and-reserve. Under the lock, per stage group and in total, the committed sum
is: recorded ledger charges (by stage) + open API reservations (by stage) + E9's RunPod
commitments, where
  - an unlaunched, open reservation counts its full open amount;
  - a launched, unsettled pod counts its full funded commitment (budget + extensions, or more if
    a reprice left its armed deadline later than its funding covers), never its remaining
    liability, so time passing never lowers it;
  - a settled pod whose charge is missing from the ledger counts its settled dollars (defence in
    depth: `settle_pod` writes the charge first, so this only fires after a bypass);
  - each takes the stage in its reservation's note (`E9:<stage>:<step>:<batch>`).
The projected amount (and, when the caller passes it, `rm`: the remaining mandatory cost this
admission does not cover, from `remaining_after`) is added, and the stage group's limit, the $200
total, the $145 GPU limit and the $75 API limit are checked. Then the reservation is appended
through `emx.spend.reserve_api(..., stage=)` or `emx.runpod_reservations.reserve/extend`, whose
own locks re-check their own rules. `settle_pod` (F2's pod_down E9 path) holds the same lock while
it writes the ledger charge (`spend.record_once`, with the stage) and then the settle row, so an
admission sees both or neither and a pod counts exactly once. Lock order is always admission lock
-> ledger lock / reservation-file lock; neither of those modules takes the admission lock.
No new paid work is admitted after `contracts.DEADLINE_UTC` (`DeadlinePassed`); settlement is
always allowed, and so is `extend` (it lengthens an arm started before the deadline; it starts
nothing new).

**Projection.** `project(state)` is the one source for the paid-step table (`paid_step_table`)
and for admission (`remaining_after`): per plan step (S1-S7, X, SCREEN) and resource, from unit
costs (estimates; measured C_Q/C_S from billed pod lifecycles once valid for the current GPU type),
review barriers ($2 each remaining), fired contingencies (checker and content-judge fallback
panels, relaunches) and the current path (promising until the pilot says otherwise; Part 2 with a
group policy until the factorial says otherwise). Unmeasured ranges are decided at their midpoint
(`basis="mid"`), so the prereg's "completes at low and middle costs" holds; measured values are
points. `gate_check` applies the cuts in order (contracts.CUTS) and returns the safe stop when
committed + R_m still exceeds the total, a stage limit, the GPU or the API limit.

Ledger notes: pod launches and sampled-EM judging should name the plan step as the note's step
(`E9:part1:S4:orig-c1`), so spend is attributed to its step; API notes of the setup units
(select, rewrite, checks, groups) are attributed to S1 by their stage.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from emx import runpod_reservations as rr
from emx import spend
from emx.e9 import contracts as C

EXP = C.EXP
REPO_ROOT = Path(__file__).resolve().parents[3]
LOCK_ENV = "EMX_E9_ADMISSION_LOCK"  # tests point the lock at a tmp file
API_RESERVE_ENV = "EMX_E9_API_RESERVE"
API_RESERVE_FILE = f"{C.RESULTS}/api_reserve.json"  # the current API reserve, set at each gate
EPS = 1e-9

# --- plan steps (implementation plan "Paid steps") ---------------------------------------------

STEP_IDS: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "X", "S6", "S7", "SCREEN")
STEP_STAGE: dict[str, str] = {
    "S1": "setup", "S2": "setup", "S3": "part1", "S4": "part1", "S5": "part1",
    "X": "extra_seeds", "S6": "part2_mandatory", "S7": "part2_mandatory", "SCREEN": "part2_screen",
}
STEP_WHAT: dict[str, str] = {
    "S1": "content judge, rewrites, checks, groups (API)",
    "S2": "smoke pod + matched-step judging",
    "S3": "cost and noise: background x seeds 1-2 (S + Q)",
    "S4": "pilot: originals, corrections x 2 (2S + 2Q) + sampled-EM judging and judge check",
    "S5": "paraphrase x 2 (S + Q), if promising",
    "X": "extra seeds: 3 pods x 2 runs (Q + second run)",
    "S6": "factorial RO, OR x 2 (4Q)",
    "S7": "P2 scoring, confirmation rewriting and checks, confirmation pods (S + second run)",
    "SCREEN": "optional gradient screen",
}
BARRIER_STEPS: tuple[str, ...] = ("S4", "S5", "X", "S6", "S7")  # pods set up during a review
STEP_STATUSES: tuple[str, ...] = ("pending", "running", "done", "dropped")
# §7.4: Part 2's "4 sampled arms ≈ $3.4 (3: ≈ $2.6)" is 0.8 per arm + 0.2 for the judge check;
# Part 1 gets the same check (conservative: the prereg rounds Part 1's to ≈ $2.6 in all).
JUDGE_CHECK_USD = 0.2
SCREEN_GPU_HOURS = 3.8  # §7.2
SCREEN_API_USD = 0.5  # §7.4
# Answers per sampled arm (§4.6, §7.4: ≈ 3,540): full bad, full good and untuned, each with the
# standard 8 (50 x 8), the other 27 (20 x 27), 100 medical and the vulnerable 14 (10 x 14).
ANSWERS_PER_MODEL: dict[str, int] = {"standard8": 400, "others27": 540, "medical100": 100,
                                     "vulnerable14": 140}
ANSWERS_PER_ARM = 3 * sum(ANSWERS_PER_MODEL.values())  # 3,540
CUT_SCREEN, CUT_VULN_UNTUNED, CUT_STANDARD8 = C.CUTS
LIMITS: dict[str, float] = {"total": C.HARD_CAP_USD, "gpu": C.GPU_LIMIT_USD,
                            "api": C.API_LIMIT_USD}
GROUPS: tuple[str, ...] = tuple(C.STAGE_LIMITS_USD)
STOP_OVER_LIMIT = "projected spend above a limit after cuts (§7.3 safe stop)"

assert ANSWERS_PER_ARM == 3540


class AdmissionError(RuntimeError):
    """The books can't be read safely (an E9 row without a stage, a note that names no stage, an
    unknown pod or reservation): no admission is possible until it is fixed by hand."""


class AdmissionRefused(spend.BudgetExceeded):
    """An E9 admission refused by a limit (exit code 2 when uncaught, like `BudgetExceeded`)."""


class DeadlinePassed(AdmissionRefused):
    """New paid E9 work after contracts.DEADLINE_UTC (§7.6)."""


class RearmFailed(RuntimeError):
    """A running E9 pod could not be re-armed after an API change; cleanup already ran."""


# --- deadline (§7.6) ---------------------------------------------------------------------------


def _epoch(now: float | datetime | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def deadline_left(now: float | datetime | None = None) -> timedelta:
    """Time left before no new paid E9 work may start (negative once passed)."""
    return timedelta(seconds=C.DEADLINE_UTC.timestamp() - _epoch(now))


def deadline_passed(now: float | datetime | None = None) -> bool:
    return deadline_left(now) <= timedelta(0)


def require_before_deadline(what: str, now: float | datetime | None = None) -> None:
    if deadline_passed(now):
        msg = (f"[e9 budget] {what} refused: the E9 deadline {C.DEADLINE_UTC:%Y-%m-%dT%H:%M:%SZ} "
               "has passed; no new paid work starts (prereg §7.6)")
        raise DeadlinePassed(msg, {"exp": EXP, "deadline_utc": C.DEADLINE_UTC.isoformat(),
                                   "now_utc": datetime.fromtimestamp(_epoch(now), UTC).isoformat()})


# --- notes, stages and steps -------------------------------------------------------------------


def note_stage(note: str) -> str | None:
    """The stage in an `E9:<stage>:...` note, or None."""
    parts = str(note or "").split(";")[0].strip().split(":")
    if len(parts) >= 2 and parts[0] == EXP and parts[1] in C.STAGE_GROUP:
        return parts[1]
    return None


def _require_note(stage: str, note: str) -> None:
    if stage not in C.STAGE_GROUP:
        raise ValueError(f"unknown stage {stage!r}; known {list(C.STAGES)}")
    if note_stage(note) != stage:
        raise ValueError(f"note {note!r} must be contracts.ledger_note({stage!r}, step, batch)")


def plan_step(stage: str | None, kind: str, note: str) -> str | None:
    """The plan step a row's spend belongs to: the note's step when it is a plan step id, else
    the stage's only step of that resource (setup API -> S1, setup GPU -> S2, part2_mandatory API
    -> S7, extra_seeds -> X, part2_screen -> SCREEN), else None (unattributed: it stays in the
    committed sum and reduces no step's remaining cost)."""
    parts = str(note or "").split(";")[0].strip().split(":")
    if len(parts) >= 3 and parts[0] == EXP and parts[2] in STEP_IDS:
        return parts[2]
    return {
        ("setup", "api"): "S1", ("setup", "gpu"): "S2", ("part2_mandatory", "api"): "S7",
        ("extra_seeds", "api"): "X", ("extra_seeds", "gpu"): "X",
        ("part2_screen", "api"): "SCREEN", ("part2_screen", "gpu"): "SCREEN",
    }.get((stage, kind))


# --- the admission lock ------------------------------------------------------------------------

_tlock = threading.RLock()
_depth = 0


def lock_path() -> Path:
    env = os.environ.get(LOCK_ENV)
    return Path(env) if env else REPO_ROOT / C.ADMISSION_LOCK


@contextmanager
def admission_lock():
    """Hold the E9 admission lock (`fcntl.flock` on contracts.ADMISSION_LOCK; env
    EMX_E9_ADMISSION_LOCK overrides). Re-entrant within a process (the flock is taken once)."""
    global _depth
    with _tlock:
        if _depth:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        p = lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            _depth = 1
            try:
                yield
            finally:
                _depth = 0
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --- the committed sum (ledger + API reservations + RunPod commitments) -----------------------


def gpu_charge_key(pod_id: str) -> str:
    """The ledger charge key of an E9 pod (the same as `emx.runpod.gpu_charge_key` under E9)."""
    return f"gpu:{EXP}:{pod_id}"


def _is_pod_charge(e: dict, pod_id: str) -> bool:
    if e.get("charge_key") == gpu_charge_key(pod_id):
        return True
    if e.get("exp") != EXP or e.get("kind") != "gpu":
        return False
    pat = re.compile(rf"(?:^|[;\s])pod={re.escape(pod_id)}(?:$|[;\s])")
    return bool(pat.search(str(e.get("note", "")))) or str(e.get("resource", "")).split(":")[
        -1
    ] == pod_id


def pod_commitment(p: dict, res: Mapping[str, dict]) -> float:
    """A launched, unsettled pod's full funded commitment: budget + extensions, or the rate x
    (armed or allowed deadline - t0) bound if a reprice made that larger. Independent of time."""
    funded = float(p["budget_usd"]) + sum(
        float(res[x]["usd"]) for x in p.get("extensions", []) if x in res
    )
    end = max(float(p["allowed"]), float(p["acked"]) if p.get("acked") is not None else 0.0)
    bound = float(p["rate_usd_h"]) * max(0.0, end - float(p["t0_epoch"])) / 3600.0
    return round(max(funded, bound), 6)


def gpu_commitments(rows: list[dict], entries: list[dict]) -> list[dict]:
    """E9's RunPod commitments not yet in the ledger: [{what, id, pod_id, stage, usd, note}]."""
    res, pods = rr._reduce(rows)
    out: list[dict] = []

    def stage_of(R: dict) -> str:
        s = note_stage(R.get("note", ""))
        if s is None:
            raise AdmissionError(
                f"E9 RunPod reservation {R['id']} has note {R.get('note')!r}, which names no "
                "stage (E9:<stage>:...); fix the reservation file before any admission"
            )
        return s

    for R in res.values():
        if R["exp"] != EXP or R["extends_pod"]:
            continue  # an extension counts through its pod
        if not R["launched"] and not R["closed"] and R["open_usd"] > EPS:
            out.append({"what": "reservation", "id": R["id"], "pod_id": None,
                        "stage": stage_of(R), "usd": round(R["open_usd"], 6), "note": R["note"]})
        for pid in R["pods"]:
            p = pods.get(pid)
            if p is None or p["reservation_id"] != R["id"] or p["settled"]:
                continue
            out.append({"what": "pod", "id": R["id"], "pod_id": pid, "stage": stage_of(R),
                        "usd": pod_commitment(p, res), "note": R["note"]})
    for r in rows:  # settled without a ledger charge (only if settle_pod was bypassed)
        if r.get("kind") != "settle" or r.get("exp") != EXP:
            continue
        R = res.get(r.get("reservation_id"))
        if R is None:
            continue
        for pid in r.get("pod_ids") or []:
            if not any(_is_pod_charge(e, pid) for e in entries):
                out.append({"what": "settled_uncharged", "id": R["id"], "pod_id": pid,
                            "stage": stage_of(R), "usd": round(float(r.get("usd") or 0.0), 6),
                            "note": R["note"]})
    return out


def _snapshot(entries: list[dict], rows: list[dict]) -> dict:
    groups = {g: {"recorded_gpu": 0.0, "recorded_api": 0.0, "api_reserved": 0.0,
                  "gpu_commit": 0.0} for g in GROUPS}
    by_step: dict[str, dict[str, float]] = defaultdict(lambda: {"gpu": 0.0, "api": 0.0})
    states = spend.reservation_states(entries)
    for e in entries:
        if e.get("exp") != EXP:
            continue
        stage = e.get("stage")
        if stage not in C.STAGE_GROUP:
            raise AdmissionError(f"E9 ledger row {e.get('id')} has no known stage ({stage!r})")
        usd = float(e.get("usd", 0.0))
        if not usd:
            continue
        kind = "gpu" if e.get("kind") == "gpu" else "api"
        groups[C.STAGE_GROUP[stage]][f"recorded_{kind}"] += usd
        s = states.get(e.get("reservation_id") or "")
        step = plan_step(stage, kind, s["note"] if s else e.get("note", ""))
        if step:
            by_step[step][kind] += usd
    for s in states.values():
        if s["exp"] != EXP or s["closed"]:
            continue
        if s.get("stage") not in C.STAGE_GROUP:
            raise AdmissionError(f"E9 API reservation {s['reservation_id']} has no known stage")
        groups[C.STAGE_GROUP[s["stage"]]]["api_reserved"] += s["liability"]
        step = plan_step(s["stage"], "api", s["note"])
        if step:
            by_step[step]["api"] += s["liability"]
    commits = gpu_commitments(rows, entries)
    for c in commits:
        groups[C.STAGE_GROUP[c["stage"]]]["gpu_commit"] += c["usd"]
        step = plan_step(c["stage"], "gpu", c["note"])
        if step:
            by_step[step]["gpu"] += c["usd"]
    for g in groups.values():
        for k in list(g):
            g[k] = round(g[k], 6)
        g["gpu"] = round(g["recorded_gpu"] + g["gpu_commit"], 6)
        g["api"] = round(g["recorded_api"] + g["api_reserved"], 6)
        g["total"] = round(g["gpu"] + g["api"], 6)
    gpu = round(sum(g["gpu"] for g in groups.values()), 6)
    api = round(sum(g["api"] for g in groups.values()), 6)
    return {
        "groups": groups,
        "gpu": gpu,
        "api": api,
        "total": round(gpu + api, 6),
        "api_recorded": round(sum(g["recorded_api"] for g in groups.values()), 6),
        "api_reserved": round(sum(g["api_reserved"] for g in groups.values()), 6),
        "by_step": {k: {kk: round(vv, 6) for kk, vv in v.items()} for k, v in by_step.items()},
        "commitments": commits,
    }


def committed(ledger: str | Path | None = None) -> dict:
    """The committed sum right now (read without the lock: for status and projections)."""
    return _snapshot(spend.read_entries(ledger), rr.read_rows())


# --- admission ---------------------------------------------------------------------------------


def _rm(rm: Mapping | None) -> dict:
    rm = rm or {}
    groups = {g: float((rm.get("groups") or {}).get(g, 0.0)) for g in GROUPS}
    gpu, api = float(rm.get("gpu", 0.0)), float(rm.get("api", 0.0))
    if any(v < -EPS for v in (*groups.values(), gpu, api)):
        raise ValueError(f"rm amounts must be >= 0: {rm}")
    return {"groups": groups, "gpu": gpu, "api": api, "total": float(rm.get("total", gpu + api))}


def _check_limits(snap: dict, stage: str, kind: str, usd: float, rm: Mapping | None,
                  what: str) -> dict:
    """Raise AdmissionRefused unless committed + usd + rm fits the stage group's limit, the total,
    the GPU limit and the API limit. Returns the breakdown."""
    if kind not in ("gpu", "api"):
        raise ValueError(kind)
    if not (math.isfinite(usd) and usd >= 0):
        raise ValueError(f"usd must be a non-negative finite number, got {usd!r}")
    r = _rm(rm)
    g = C.STAGE_GROUP[stage]
    after = {
        "group": snap["groups"][g]["total"] + usd + r["groups"][g],
        "total": snap["total"] + usd + r["total"],
        "gpu": snap["gpu"] + (usd if kind == "gpu" else 0.0) + r["gpu"],
        "api": snap["api"] + (usd if kind == "api" else 0.0) + r["api"],
    }
    limits = {"group": C.STAGE_LIMITS_USD[g], **LIMITS}
    b = {
        "exp": EXP, "what": what, "stage": stage, "stage_group": g, "kind": kind,
        "usd": round(usd, 6), "rm": r,
        "committed": {"group": snap["groups"][g]["total"], "total": snap["total"],
                      "gpu": snap["gpu"], "api": snap["api"]},
        "after": {k: round(v, 6) for k, v in after.items()}, "limits": limits,
    }
    fails = [f"{'stage ' + g if k == 'group' else k} ${after[k]:.2f} > ${limits[k]:.2f}"
             for k in after if after[k] > limits[k] + EPS]
    b["ok"] = not fails
    if fails:
        raise AdmissionRefused(f"[e9 budget] {what} ${usd:.2f} ({stage}) refused: "
                               + "; ".join(fails), b)
    return b


def reserve_api(stage: str, usd: float, note: str, *, now: float | datetime | None = None,
                rm: Mapping | None = None) -> str:
    """Admit an API batch of up to `usd` for `stage` under the E9 admission lock; returns the
    `emx.spend` reservation id (then mark_submitted / record / settle_api / release_api as usual).
    `rm`: the remaining mandatory cost this batch does not cover (optional)."""
    require_before_deadline(f"API batch {note!r}", now)
    _require_note(stage, note)
    usd = float(usd)
    with admission_lock():
        snap = _snapshot(spend.read_entries(), rr.read_rows())
        _check_limits(snap, stage, "api", usd, rm, f"API batch {note!r}")
        running = snap["groups"][C.STAGE_GROUP[stage]]["gpu_commit"]
        return spend.reserve_api(EXP, usd, note, running_pod_usd=running, stage=stage)


def reserve_launch(stage: str, usd: float, note: str, *, balance: float | None = None,
                   now: float | datetime | None = None, rm: Mapping | None = None) -> str:
    """Admit a pod launch of `usd` (the sum of its pods' budgets) under the E9 admission lock;
    returns the `emx.runpod_reservations` id (then creating / launch / ack / settle_pod). The
    shared launch rule (balance, $10 buffer) is applied by `runpod_reservations.reserve`; `balance`
    None reads it live. `rm`: the remaining mandatory cost this launch does not cover (§8.4: the
    orchestrator passes `remaining_after(project(state), step)`)."""
    t = _epoch(now)
    require_before_deadline(f"pod launch {note!r}", t)
    _require_note(stage, note)
    usd = float(usd)
    with admission_lock():
        snap = _snapshot(spend.read_entries(), rr.read_rows())
        _check_limits(snap, stage, "gpu", usd, rm, f"pod launch {note!r}")
        running = snap["groups"][C.STAGE_GROUP[stage]]["gpu_commit"]
        spend.check_total(usd, "gpu", running_pod_usd=running, exp=EXP, stage=stage)
        return rr.reserve(EXP, usd, note, balance=balance, now=t)


def extend(pod_id: str, new_deadline: float, note: str = "", *, balance: float | None = None,
           now: float | datetime | None = None, rm: Mapping | None = None) -> str:
    """Admit a liability-raising deadline extension of an armed E9 pod under the E9 admission
    lock: the pod's counted commitment rises by (new commitment - old), checked against its
    stage's limits; then `runpod_reservations.extend` applies the launch rule. Returns the
    extension's reservation id. Allowed after the deadline (it starts no new work)."""
    t = _epoch(now)
    with admission_lock():
        entries, rows = spend.read_entries(), rr.read_rows()
        res, pods = rr._reduce(rows)
        p = pods.get(pod_id)
        if p is None or p["exp"] != EXP:
            raise AdmissionError(f"{pod_id} is not a launched E9 pod")
        if p["settled"]:
            raise AdmissionError(f"{pod_id} is already settled")
        stage = note_stage(res[p["reservation_id"]]["note"])
        if stage is None:
            raise AdmissionError(f"{pod_id}'s reservation note names no stage")
        new = int(new_deadline)
        if new <= max(float(p["allowed"]), t):
            raise AdmissionError(f"{new} is not later than {pod_id}'s allowed deadline and now")
        inc = float(p["rate_usd_h"]) * (new - max(float(p["allowed"]), t)) / 3600.0
        before = pod_commitment(p, res)
        ext = {**p, "allowed": float(new), "budget_usd": float(p["budget_usd"]) + inc}
        delta = max(0.0, pod_commitment(ext, res) - before)
        snap = _snapshot(entries, rows)
        _check_limits(snap, stage, "gpu", delta, rm, f"extension of {pod_id}")
        tag = C.ledger_note(stage, "extend", pod_id) + (f"; {note}" if note else "")
        return rr.extend(pod_id, new, balance=balance, now=t, note=tag)


def settle_pod(reservation_id: str, pod_id: str, usd: float, *, hours: float,
               resource: str = "", note: str = "") -> dict:
    """Charge a terminated E9 pod's actual GPU dollars to the ledger with its stage, then settle
    it in the reservation file, both under the E9 admission lock (so an admission never sees the
    pod in neither or both). Idempotent: the charge is `record_once` (key gpu:E9:<pod>) and the
    settle is a no-op when already settled; a retry settles at the dollars already charged.
    Allowed after the deadline. Returns {stage, charge, charged_now, settle}."""
    usd = float(usd)
    with admission_lock():
        res, _ = rr._reduce(rr.read_rows())
        R = res.get(reservation_id)
        if R is None or R["exp"] != EXP:
            raise AdmissionError(f"{reservation_id} is not an E9 RunPod reservation")
        stage = note_stage(R["note"])
        if stage is None:
            raise AdmissionError(f"reservation {reservation_id} note {R['note']!r} has no stage")
        res_note = R["note"].split(";")[0].strip()
        entry, appended = spend.record_once(
            gpu_charge_key(pod_id), "gpu", resource or f"runpod:{pod_id}", float(hours), usd,
            note=f"{res_note}; pod={pod_id}" + (f"; {note}" if note else ""), exp=EXP,
            also_matches=lambda e: _is_pod_charge(e, pod_id), stage=stage,
        )
        charged = float(entry.get("usd", usd))
        row = rr.settle(reservation_id, charged, pod_id=pod_id,
                        note=note or f"settle_pod: {pod_id}")
    return {"stage": stage, "charge": entry, "charged_now": appended, "settle": row}


# --- unit costs (§7.1, Amendment D) ------------------------------------------------------------


def _pair(x: float) -> tuple[float, float]:
    return (float(x), float(x))


def measured_from_sessions(sessions: Iterable[Mapping], gpu_type: str | None = None) -> dict:
    """Measured unit costs from billed pod lifecycles: each session {kind: "cq"|"cs", hours,
    gpu} of a pod that ran ONE run of that kind (second runs are not unit measurements). Takes the
    largest billed hours per kind (conservative). Sessions on another GPU type than `gpu_type`
    (default: the first session's) are ignored, so a type change is re-measured."""
    sessions = [s for s in sessions if s.get("kind") in ("cq", "cs")]
    if not sessions:
        return {}
    gtype = gpu_type or sessions[0]["gpu"]
    out: dict = {"gpu_type": gtype}
    for kind, key in (("cq", "c_q_hours"), ("cs", "c_s_hours")):
        hs = [float(s["hours"]) for s in sessions if s["kind"] == kind and s["gpu"] == gtype]
        if hs:
            out[key] = max(hs)
            out[f"n_{kind}"] = len(hs)
    return out


def unit_costs(state: Mapping | None = None) -> dict:
    """Dollar (low, high) per unit: cq, cs, q2 (a second run on a set-up pod), smoke, screen.
    Estimates are priced at the H100 secure rate (§7.1); a measurement valid for the current GPU
    type is a point priced at the current rate. Returns also rate, gpu_type, measured, flags."""
    state = state or {}
    gpu = state.get("gpu") or {}
    gtype = gpu.get("type", C.GPU_ORDER[0][0])
    rate = float(gpu.get("rate_usd_h", C.GPU_ORDER[0][2]))
    m = state.get("measured") or {}
    valid = bool(m) and m.get("gpu_type") == gtype
    flags = []
    if m and not valid:
        flags.append(f"remeasure: C_Q/C_S were measured on {m.get('gpu_type')}, the pods now run "
                     f"on {gtype}; estimates used until re-measured")
    est = C.H100_SECURE_USD_PER_H
    save = C.SECOND_RUN_SAVING_HOURS

    def cost(hours: tuple[float, float], r: float) -> tuple[float, float]:
        return (round(hours[0] * r, 6), round(hours[1] * r, 6))

    mq = valid and m.get("c_q_hours") is not None
    ms = valid and m.get("c_s_hours") is not None
    q_h, q_r = (_pair(m["c_q_hours"]), rate) if mq else (C.C_Q_HOURS, est)
    s_h, s_r = (_pair(m["c_s_hours"]), rate) if ms else (C.C_S_HOURS, est)
    return {
        "gpu_type": gtype,
        "rate": rate,
        "measured": {"c_q": mq, "c_s": ms},
        "cq": cost(q_h, q_r),
        "q2": cost((q_h[0] - save, q_h[1] - save), q_r),
        "cs": cost(s_h, s_r),
        "smoke": cost(C.SMOKE_HOURS, est),
        "screen": cost(_pair(SCREEN_GPU_HOURS), rate if valid else est),
        "flags": flags,
    }


def judging_usd(n_arms: int, cuts: Iterable[str] = (), confirmation: bool = False) -> float:
    """Sampled-EM judging of `n_arms` sampled arms (≈ $0.8 per 3,540 answers, §7.4), after the
    cuts: cut 2 drops the vulnerable 14 and halves the untuned model's samples; cut 3 halves the
    standard 8 in the confirmation."""
    cuts = set(cuts)
    a = dict(ANSWERS_PER_MODEL)
    if confirmation and CUT_STANDARD8 in cuts:
        a["standard8"] //= 2
    full = sum(a.values())
    untuned = dict(a)
    if CUT_VULN_UNTUNED in cuts:
        full -= a["vulnerable14"]
        untuned = {k: (0 if k == "vulnerable14" else v // 2) for k, v in a.items()}
    answers = 2 * full + sum(untuned.values())
    per = C.API_ESTIMATES_USD["judging_per_sampled_arm"][0]
    return round(n_arms * per * answers / ANSWERS_PER_ARM, 6)


# --- the projection ----------------------------------------------------------------------------


def _fired_usd(v, key: str) -> tuple[float, float]:
    if v is True:
        return C.API_ESTIMATES_USD[key]
    if not v:
        return (0.0, 0.0)
    return _pair(float(v))  # the actual pool-size price


def step_pods(step: str, state: Mapping | None = None) -> list[list[str]]:
    """The planned runs per pod of a GPU step (§7.2): cq, cs, q2 (second run), smoke, screen."""
    state = state or {}
    n_conf = 3 if state.get("group_policy") is False else 4
    return {
        "S2": [["smoke"]],
        "S3": [["cs"], ["cq"]],
        "S4": [["cs"], ["cq"], ["cs"], ["cq"]],
        "S5": [["cs"], ["cq"]],
        "X": [["cq", "q2"]] * 3,
        "S6": [["cq"]] * 4,
        "S7": [["cs", "q2"]] * n_conf,
        "SCREEN": [["screen"]],
    }.get(step, [])


def step_items(step: str, state: Mapping | None = None, cuts: Iterable[str] = (),
               units: Mapping | None = None) -> list[dict]:
    """The step's cost items: {step, stage, kind, name, lo, hi}."""
    state = state or {}
    units = units or unit_costs(state)
    cuts = list(cuts)
    stage = STEP_STAGE[step]
    fired = state.get("fired") or {}
    items: list[dict] = []

    def add(kind: str, name: str, lohi: tuple[float, float]) -> None:
        if lohi[1] > 0:
            items.append({"step": step, "stage": stage, "kind": kind, "name": name,
                          "lo": round(lohi[0], 6), "hi": round(lohi[1], 6)})

    pods = step_pods(step, state)
    if pods:
        lo = sum(units[r][0] for pod in pods for r in pod)
        hi = sum(units[r][1] for pod in pods for r in pod)
        add("gpu", "pods", (lo, hi))
    if step in BARRIER_STEPS:
        add("gpu", "review_barrier", _pair(C.REVIEW_BARRIER_USD))
    for _ in range(int((fired.get("relaunches") or {}).get(step, 0))):
        add("gpu", "relaunch", _pair(C.RELAUNCH_USD))
    if step == "S1":
        add("api", "setup_api", C.API_ESTIMATES_USD["setup_api"])
        add("api", "checker_fallback_panel",
            _fired_usd(fired.get("checker_fallback_panel"), "checker_fallback_panel"))
        add("api", "content_judge_fallback_panel",
            _fired_usd(fired.get("content_judge_fallback_panel"), "content_judge_fallback_panel"))
    elif step == "S2":
        add("api", "matched_step_judging", C.API_ESTIMATES_USD["matched_step_judging"])
    elif step == "S4":
        add("api", "judging", _pair(judging_usd(3, cuts) + JUDGE_CHECK_USD))
    elif step == "S5":
        add("api", "judging", _pair(judging_usd(1, cuts)))
    elif step == "S7":
        n = len(pods)
        add("api", "p2_scoring", C.API_ESTIMATES_USD["p2_scoring"])
        add("api", "confirm_rewrite", C.API_ESTIMATES_USD["confirm_rewrite"])
        add("api", "judging", _pair(judging_usd(n, cuts, confirmation=True) + JUDGE_CHECK_USD))
    elif step == "SCREEN":
        add("api", "screen_api", _pair(SCREEN_API_USD))
    return items


def mandatory_steps(state: Mapping | None = None) -> list[str]:
    """The mandatory steps of the current path (§7.3 R_m). `state["pilot"]`: None (unknown: the
    promising path, the larger) | "promising" | "not_promising"; `state["part2"]`: None | "go" |
    "no"; `state["extra_seeds"]`: True once the launch-only-if rule passed (null path only)."""
    state = state or {}
    pilot = state.get("pilot")
    if pilot not in (None, "promising", "not_promising"):
        raise ValueError(f"pilot must be None, 'promising' or 'not_promising', got {pilot!r}")
    if state.get("part2") not in (None, "go", "no"):
        raise ValueError(f"part2 must be None, 'go' or 'no', got {state.get('part2')!r}")
    steps = ["S1", "S2", "S3", "S4"]
    promising = pilot in (None, "promising")
    if promising:
        steps.append("S5")
    if state.get("extra_seeds"):
        if pilot != "not_promising":
            raise ValueError("the extra-seeds branch runs only on the null path (§4.11)")
        steps.append("X")
    if promising and state.get("part2") in (None, "go"):
        steps += ["S6", "S7"]
    return steps


def _value(lo: float, hi: float, basis: str) -> float:
    if basis == "lo":
        return lo
    if basis == "hi":
        return hi
    if basis == "mid":
        return (lo + hi) / 2
    raise ValueError(f"basis must be lo, mid or hi, got {basis!r}")


def _zero() -> dict:
    return {"groups": dict.fromkeys(GROUPS, 0.0), "gpu": 0.0, "api": 0.0, "total": 0.0}


def _committed_view(state: Mapping) -> dict:
    c = state.get("committed")
    if not c:
        return _zero()
    groups = {}
    for g in GROUPS:
        v = c["groups"].get(g, 0.0)
        groups[g] = float(v["total"] if isinstance(v, Mapping) else v)
    return {"groups": groups, "gpu": float(c["gpu"]), "api": float(c["api"]),
            "total": float(c["total"])}


def project(state: Mapping | None = None, cuts: Iterable[str] = (), basis: str = "mid") -> dict:
    """The stage x resource projection: committed (state["committed"], from `gather_state`) +
    the remaining mandatory cost R_m of the current path, against every limit.

    state keys (all optional): steps {step: pending|running|done|dropped}; step_committed {step:
    {gpu, api}} (what a running step has already committed); pilot, part2, group_policy,
    extra_seeds (the path); fired {checker_fallback_panel: bool|usd, content_judge_fallback_panel:
    bool|usd, relaunches: {step: n}}; measured (C_Q/C_S hours, gpu_type); gpu {type, rate_usd_h};
    cuts. A running step counts only its projected cost minus what it has committed, per resource,
    so each dollar counts once. Returns {basis, path, items, remaining, committed, projected,
    limits, over, cuts, units, flags, state}."""
    state = dict(state or {})
    cuts = list(dict.fromkeys([*state.get("cuts", []), *cuts]))
    bad = set(cuts) - set(C.CUTS)
    if bad:
        raise ValueError(f"unknown cuts {sorted(bad)}")
    statuses = {**dict.fromkeys(STEP_IDS, "pending"), **(state.get("steps") or {})}
    for s, v in statuses.items():
        if s not in STEP_IDS or v not in STEP_STATUSES:
            raise ValueError(f"step {s!r}: status {v!r}")
    units = unit_costs(state)
    path = mandatory_steps(state)
    step_com = state.get("step_committed") or {}
    items: list[dict] = []
    rem = _zero()
    for step in path:
        status = statuses[step]
        its = step_items(step, state, cuts, units)
        for it in its:
            it["usd"] = round(_value(it["lo"], it["hi"], basis), 6)
        if status in ("done", "dropped"):
            for it in its:
                it["remaining"] = 0.0
        elif status == "running":
            for kind in ("gpu", "api"):
                left = float((step_com.get(step) or {}).get(kind, 0.0))
                for it in (i for i in its if i["kind"] == kind):
                    take = min(left, it["usd"])
                    it["remaining"] = round(it["usd"] - take, 6)
                    left -= take
        else:
            for it in its:
                it["remaining"] = it["usd"]
        for it in its:
            it["status"] = status
            g = C.STAGE_GROUP[it["stage"]]
            rem["groups"][g] += it["remaining"]
            rem[it["kind"]] += it["remaining"]
            rem["total"] += it["remaining"]
        items += its
    rem = _round(rem)
    com = _committed_view(state)
    proj = _round({
        "groups": {g: com["groups"][g] + rem["groups"][g] for g in GROUPS},
        **{k: com[k] + rem[k] for k in ("gpu", "api", "total")},
    })
    limits = {"groups": dict(C.STAGE_LIMITS_USD), **LIMITS}
    over = [f"stage:{g}" for g in GROUPS if proj["groups"][g] > limits["groups"][g] + EPS]
    over += [k for k in ("total", "gpu", "api") if proj[k] > limits[k] + EPS]
    return {"basis": basis, "path": path, "items": items, "remaining": rem, "committed": com,
            "projected": proj, "limits": limits, "over": over, "cuts": cuts, "units": units,
            "flags": list(units["flags"]), "state": state}


def _round(d: dict) -> dict:
    return {"groups": {g: round(v, 6) for g, v in d["groups"].items()},
            **{k: round(d[k], 6) for k in ("gpu", "api", "total")}}


def remaining_after(projection: Mapping, step: str | Iterable[str]) -> dict:
    """R_m without the given step(s): what an admission for those steps does not cover (the `rm`
    argument of reserve_launch / reserve_api)."""
    skip = {step} if isinstance(step, str) else set(step)
    out = _zero()
    for it in projection["items"]:
        if it["step"] in skip:
            continue
        out["groups"][C.STAGE_GROUP[it["stage"]]] += it["remaining"]
        out[it["kind"]] += it["remaining"]
        out["total"] += it["remaining"]
    return _round(out)


def apply_cuts(projection: Mapping) -> tuple[list[str], dict]:
    """While any limit is exceeded, apply the cuts in order (§7.3): cut 1 (the optional screen,
    never in R_m) is recorded when reached; cuts 2 and 3 only if they lower R_m. If a limit is
    still exceeded after all three, the projection carries `stop` (the safe stop at this gate)."""
    p = dict(projection)
    for cut in C.CUTS:
        if not p["over"]:
            break
        if cut in p["cuts"]:
            continue
        q = project(p["state"], [*p["cuts"], cut], p["basis"])
        if cut == CUT_SCREEN or q["remaining"]["total"] < p["remaining"]["total"] - EPS:
            p = q
    if p["over"]:
        p = {**p, "stop": STOP_OVER_LIMIT}
    return list(p["cuts"]), p


def gate_check(state: Mapping, basis: str = "mid") -> dict:
    """The gate's budget verdict (§7.3, §8.4): {ok, cuts, stop, over, api_reserve, projection}.
    `api_reserve` (R_m's API part) is what `set_api_reserve` stores at this gate."""
    cuts, p = apply_cuts(project(state, basis=basis))
    return {"ok": "stop" not in p, "cuts": cuts, "stop": p.get("stop"), "over": p["over"],
            "api_reserve": p["remaining"]["api"], "projection": p}


def optional_fits(projection: Mapping, usd: float, stage: str, kind: str) -> bool:
    """Optional work (screen, vulnerable secondary) launches only if committed + R_m + `usd`
    stays within its stage limit, the total and the resource limit (§7.3)."""
    if kind not in ("gpu", "api") or usd < 0:
        raise ValueError((kind, usd))
    pr, lim = projection["projected"], projection["limits"]
    g = C.STAGE_GROUP[stage]
    return (pr["groups"][g] + usd <= lim["groups"][g] + EPS and pr["total"] + usd <= lim["total"]
            + EPS and pr[kind] + usd <= lim[kind] + EPS)


# --- the paid-step table -----------------------------------------------------------------------


def paid_step_table(state: Mapping | None = None) -> dict:
    """The plan's paid-step table from the projection's items, every step pending: rows {step,
    stage, what, gpu (lo, hi), api (lo, hi), barrier} and the subtotals (lo, hi) of each path."""
    state = dict(state or {})
    rows = []
    per: dict[str, tuple[float, float]] = {}
    for step in STEP_IDS:
        its = step_items(step, state, state.get("cuts", ()))
        row = {"step": step, "stage": STEP_STAGE[step], "what": STEP_WHAT[step]}
        for kind in ("gpu", "api"):
            sel = [i for i in its if i["kind"] == kind and i["name"] != "review_barrier"]
            row[kind] = (round(sum(i["lo"] for i in sel), 2), round(sum(i["hi"] for i in sel), 2))
        row["barrier"] = sum(i["lo"] for i in its if i["name"] == "review_barrier")
        per[step] = (sum(i["lo"] for i in its), sum(i["hi"] for i in its))
        rows.append(row)

    def tot(steps: Iterable[str]) -> tuple[float, float]:
        return (round(sum(per[s][0] for s in steps), 2), round(sum(per[s][1] for s in steps), 2))

    sub = {
        "part1_promising": tot(["S1", "S2", "S3", "S4", "S5"]),
        "part1_stopping": tot(["S1", "S2", "S3", "S4"]),
        "extra_seeds": tot(["X"]),
        "part2_group_policy": tot(["S6", "S7"]),
        "screen": tot(["SCREEN"]),
    }
    nog = {**state, "group_policy": False}
    s7 = [*step_items("S6", nog), *step_items("S7", nog)]
    sub["part2_no_group"] = (round(sum(i["lo"] for i in s7), 2),
                             round(sum(i["hi"] for i in s7), 2))
    sub["total_promising_group"] = tuple(
        round(a + b, 2) for a, b in zip(sub["part1_promising"], sub["part2_group_policy"],
                                        strict=True))
    return {"rows": rows, "subtotals": sub, "units": unit_costs(state)}


# --- per-pod budgets (§7.3) --------------------------------------------------------------------


def launch_plan(step: str, state: Mapping | None = None, basis: str = "mid") -> dict:
    """A GPU step's launch: per-pod budgets = POD_BUDGET_FACTOR x the pod's planned runs at the
    upper estimate (or measured), rounded up to the cent; the reservation is their sum (so the
    budgets sum to exactly the reservation); `rm` is R_m without this step, for admission."""
    state = state or {}
    pods = step_pods(step, state)
    if not pods:
        raise ValueError(f"{step} launches no pods")
    units = unit_costs(state)
    budgets = [math.ceil(C.POD_BUDGET_FACTOR * sum(units[r][1] for r in pod) * 100) / 100
               for pod in pods]
    p = project(state, basis=basis)
    return {"step": step, "stage": STEP_STAGE[step], "pods": pods, "budgets": budgets,
            "usd": round(sum(budgets), 2), "rm": remaining_after(p, step)}


# --- the API reserve and the backstop kill line (§7.3) ------------------------------------------


def api_reserve_path(root: str | Path | None = None) -> Path:
    env = os.environ.get(API_RESERVE_ENV)
    if env and root is None:
        return Path(env)
    return Path(root or REPO_ROOT) / API_RESERVE_FILE


def api_reserve(root: str | Path | None = None) -> float:
    """The current API reserve (projected remaining API spend of the current path), set at each
    gate; the sign-off value ($62) until the first gate writes one."""
    p = api_reserve_path(root)
    if not p.exists():
        return C.API_RESERVE_AT_SIGNOFF_USD
    return float(json.loads(p.read_text())["usd"])


def set_api_reserve(usd: float, gate: str, root: str | Path | None = None) -> dict:
    """Store the API reserve at a gate (atomic replace). Returns the record."""
    if not (math.isfinite(usd) and usd >= 0):
        raise ValueError(f"usd must be >= 0, got {usd!r}")
    p = api_reserve_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"usd": round(float(usd), 6), "gate": gate,
           "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    os.replace(tmp, p)
    return rec


def backstop_kill_usd(root: str | Path | None = None, ledger: str | Path | None = None) -> float:
    """The laptop watchdog's kill line over all E9 pods: min($145, $200 - current API reserve -
    API recorded and reserved). Never a pod's armed deadline."""
    entries = spend.read_entries(ledger)
    states = spend.reservation_states(entries)
    api = sum(float(e.get("usd", 0.0)) for e in entries
              if e.get("exp") == EXP and e.get("kind") == "judge")
    api += sum(s["liability"] for s in states.values() if s["exp"] == EXP and not s["closed"])
    return round(min(C.GPU_LIMIT_USD, C.HARD_CAP_USD - api_reserve(root) - api), 6)


# --- the re-arm hook after API changes (port of emx.e3.budget.after_api_change) ----------------

Runner = Callable[..., tuple[int, str]]
_FAIL_LINE = re.compile(rf"{EXP} killswitch (_[A-Za-z0-9]{{1,8}}):")


def running_slots() -> list[dict]:
    """Launched, unsettled E9 pods with a slot: [{pod_id, slot, reservation_id}]."""
    _, pods = rr._reduce(rr.read_rows())
    return [{"pod_id": p["pod_id"], "slot": p["slot"], "reservation_id": p["reservation_id"]}
            for p in pods.values() if p["exp"] == EXP and not p["settled"] and p.get("slot")]


def after_api_change(*, root: str | Path | None = None, runner: Runner | None = None,
                     cleanup: Callable[[str, str], int] | None = None,
                     ledger: str | Path | None = None) -> dict:
    """After an API reserve, settle or release, or a new API reserve at a gate: recompute the
    backstop and, if E9 pods run, re-arm them with one `pod_push.sh --exp E9 --slot S --arm-only`
    (each at min(its reservation deadline, the kill-line deadline, its acknowledged deadline); an
    automatic re-arm never extends). A slot fails if pod_push names it, or its acknowledged
    deadline is missing or later than before; a non-zero exit naming no slot fails every slot.
    Failing slots go through cleanup (default: `scripts/e9_orchestrate.py cleanup --slot S
    --reason R --outcome aborted`), then RearmFailed is raised. Returns {backstop_kill_usd,
    slots, failed}; a no-op apart from the backstop when no pods run."""
    kill = backstop_kill_usd(root, ledger)
    slots = running_slots()
    if not slots:
        return {"backstop_kill_usd": kill, "slots": [], "failed": {}}
    root = Path(root or REPO_ROOT)
    if runner is None:
        from emx.e3.budget import run_cmd as runner
    if cleanup is None:

        def cleanup(slot: str, reason: str) -> int:
            return runner(["uv", "run", "python", "scripts/e9_orchestrate.py", "cleanup", "--slot",
                           slot, "--reason", reason, "--outcome", "aborted"], root=root)[0]

    before = {s["slot"]: rr.acked_deadline(s["pod_id"]) for s in slots}
    rc, out = runner(["bash", "scripts/pod_push.sh", "--exp", EXP, "--slot", slots[0]["slot"],
                      "--arm-only"], root=root)
    failed = {m.group(1): "pod_push named the slot" for m in _FAIL_LINE.finditer(out)}
    if rc != 0 and not failed:
        failed = {s["slot"]: f"pod_push --arm-only exited {rc}" for s in slots}
    for s in slots:
        if s["slot"] in failed:
            continue
        after = rr.acked_deadline(s["pod_id"])
        if after is None:
            failed[s["slot"]] = "no acknowledged deadline after re-arm"
        elif before[s["slot"]] is not None and after > before[s["slot"]]:
            failed[s["slot"]] = f"deadline extended {before[s['slot']]} -> {after}"
    for slot, why in failed.items():
        cleanup(slot, f"rearm failed: {why}")
    if failed:
        raise RearmFailed(f"E9 re-arm failed for {sorted(failed)}: {failed}; cleanup ran")
    return {"backstop_kill_usd": kill, "slots": [s["slot"] for s in slots], "failed": {}}


# --- decision helpers --------------------------------------------------------------------------

EXTRA_SEEDS_CONTRASTS: tuple[str, ...] = ("corr_minus_orig", "corr_minus_bg")


def extra_seeds_launch_ok(contrasts: Mapping[str, Mapping[int, float]]) -> tuple[bool, str]:
    """§4.11 launch-only-if: the branch launches only if d1 and d2 (CPT seeds 1 and 2) are both
    below zero for both required contrasts (corrections - originals, corrections - background).
    `contrasts`: {name: {cpt_seed: d}}. Returns (ok, reason)."""
    bad = []
    for name in EXTRA_SEEDS_CONTRASTS:
        ds = contrasts.get(name)
        if ds is None:
            raise KeyError(f"missing contrast {name!r}")
        for seed in C.PART1_CPT_SEEDS:
            if seed not in ds:
                raise KeyError(f"contrast {name}: missing CPT seed {seed}")
            d = float(ds[seed])
            if not math.isfinite(d):
                raise ValueError(f"contrast {name} seed {seed}: d = {d!r}")
            if not d < 0:
                bad.append(f"{name} d{seed} = {d:+.4g}")
    if bad:
        return False, "underpowered; more seeds cannot pass (" + ", ".join(bad) + ")"
    return True, "d1 and d2 < 0 for both required contrasts"


def accrued_usd(pod_ids: Iterable[str], now: float | datetime | None = None) -> float:
    """Spend so far of the given pods: settled dollars, or rate x (now - t0) while unsettled."""
    t = _epoch(now)
    _, pods = rr._reduce(rr.read_rows())
    total = 0.0
    for pid in pod_ids:
        p = pods.get(pid)
        if p is None:
            raise AdmissionError(f"unknown pod {pid}")
        if p["settled"]:
            total += float(p["settled_usd"] or 0.0)
        else:
            total += float(p["rate_usd_h"]) * max(0.0, t - float(p["t0_epoch"])) / 3600.0
    return round(total, 6)


def barrier_kill_due(accrued: float, review_pending: bool) -> bool:
    """§8.6: the next step's pods are terminated (and relaunched after the review) once their
    accrued spend reaches $10 while their gate's review has not returned."""
    return bool(review_pending) and float(accrued) >= C.BARRIER_KILL_USD - EPS


# --- the projection's state, read from disk -----------------------------------------------------


def gather_state(decisions: Mapping | None = None, ledger: str | Path | None = None) -> dict:
    """The projection's state: the orchestrator's `decisions` (steps, path, fired, measured, gpu,
    cuts) plus the live committed sum and each step's committed dollars."""
    snap = committed(ledger)
    return {
        **dict(decisions or {}),
        "committed": {"groups": {g: v["total"] for g, v in snap["groups"].items()},
                      "gpu": snap["gpu"], "api": snap["api"], "total": snap["total"]},
        "step_committed": snap["by_step"],
    }
