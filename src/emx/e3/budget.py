"""E3 budget: the remaining-work projection, the ordered cuts, the completion reserve, the $5
speculative ceiling, the 5-day deadline, the retry count, and the re-arm hook after API changes.

Spec: docs/experiments/E3/prereg.md §7 (the workload, GPU and API tables, totals, cuts in order,
speculative work, wall-clock cap, stopping rules) and §8.3/§8.6; implementation plan v2 fix 8,
v3 fixes 4-5 and v4 fix 3 (`after_api_change`, `RearmFailed`). New module (unit U8d); the
§7 arithmetic is rebuilt from `emx.e3.contracts`, so the planned (~$30), worst-case before cuts
($47.6) and after-both-cuts (~$42.3) totals are reproduced by tests/e3/test_orchestrate.py.

Import-light on purpose: the pair builder and the judge driver call `after_api_change` after every
API reserve, settle and release, so this module imports only the stdlib and the stdlib-only
contracts at module level. `emx.spend`, `emx.runpod` and `emx.runpod_reservations` are imported
inside the functions that need them.

The projection counts, for each limit (GPU $35, API $10, total $45):
  committed = recorded ledger charges + open API reservations + the RunPod reservations of E3
              pods launched and not yet cleaned up (their charges reach the ledger only at
              pod_down, so the reservation bounds them meanwhile);
  remaining = §7's items for every unit still pending (pods, retries left, judging, the pair
              builds, the v2-rescoring allowance), at the smoke test's measured rates when given.
A unit that is running is counted through its reservation, never again as remaining work; the
retry share carried by each running pod's reservation (RESERVATION_USD minus the pod's projected
cost) is subtracted from the retries item, so each dollar counts once.

2026-10-06 (code review A r1, fixes A5, A8, A9): a pod unit is done only with its final completion
record `results/E3/DONE.<unit>.json` (written by the pod job after its last checkpoint; it names
the unit, the pod id, the artifact inventory and the manifest's sha256), never from a manifest
alone, which the job rewrites after every checkpoint (`unit_complete`). A §7 stop is terminal:
`results/E3/STOPPED.json` (`write_stopped`, `read_stopped`) makes the orchestrator refuse every
launch, job and API reserve. Pod 3 needs the rules review (17r) to have passed
(`review_17r_passed`): E3 uses no speculative allowance, so `speculative_ok` is not called on any
launch or job path (kept as the §7 arithmetic only).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from emx.e3 import contracts as C

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCH_LOG = "logs/E3-orchestrate.jsonl"  # one line per orchestrator command and event
RATES_FILE = f"{C.RESULTS}/measured_rates.json"  # the smoke test's hours per checkpoint
# Amendment A: the failed construction-1 build is sunk; the rebuilds (constructions 2 and 3,
# notes "[E3 answer_pairs_v1c2] ..." / "...v1c3] ...") share one <= $3 allowance.
PAIRS_NOTE_PREFIX = "[E3 answer_pairs_v1c"
CUTS_FILE = f"{C.RESULTS}/cuts.json"  # cuts decided so far, and pod 2's sampling decision
SNAPSHOT_FILE = f"{C.RESULTS}/reservations_snapshot.jsonl"
STOPPED_FILE = f"{C.RESULTS}/STOPPED.json"  # a §7 stop: terminal (fix A8)
REVIEW_17R_FILE = f"{C.ANALYSIS_DIR}/review_17r.json"  # {"outcome": "pass"} gates pod 3 (fix A9)
STOP_OVER_LIMIT = "projected spend above a limit after cuts"
STOP_WALL_CLOCK = "wall-clock cap"
STOP_RETRIEVAL = "a training record or scoring file can't be retrieved after 3 attempts"

# §7's per-pod overheads (hours), which are not per-checkpoint and so not in the contracts.
SETUP_H = 0.5
SMOKE_H = 0.3
BUFFER_H = {"pod1": 0.3, "pod2": 0.3, "pod3": 0.2, "v2_rescore": 0.2}
V2_ADAPTERS_H = 0.02  # per checkpoint: the adapters' download
V2_CONFIRM_STATES_H = 0.10  # all confirmation states, 7,040 answer scores
V2_OLD_REF_H = 0.02  # the old-reference step-50 check
V2_GPU = "NVIDIA H100 NVL"  # priced at the H100 NVL fallback rate (§7)
POD3_SCORED_STATES, SCORED_STATES = 4, 5  # confirmation scores 4 of the 5 states
CALLS_PER_ANSWER = 4  # two judges x (alignment, coherence)
TIEBREAK_USD = {"stageA": 0.1, "stageB": 0.1}  # <= $0.2 in all (§7)
STAGE_ADAPTERS = 2 * len(C.STAGE_A)  # bad and good final adapters per stage: 6

UNITS: tuple[str, ...] = (
    "pairs",
    "pod1",
    "pod2",
    "judge_stageA",
    "judge_stageB",
    "pod3",
    "v2_rescore",
)
POD_UNITS: tuple[str, ...] = ("pod1", "pod2", "pod3")
STATUSES: tuple[str, ...] = ("pending", "running", "done", "skipped")
CUT1, CUT2 = C.CUTS
LIMITS: dict[str, float] = {
    "gpu": C.GPU_LIMIT_USD,
    "api": C.API_LIMIT_USD,
    "total": C.HARD_CAP_USD,
}


def _rate(gpu: str) -> float:
    return dict(C.FALLBACK_GPUS)[gpu]


# --- the projection (§7) -----------------------------------------------------------------------


def hours_per_checkpoint(measured_rates: dict | None = None) -> tuple[dict[str, float], list]:
    """§7's per-checkpoint hours, with the smoke test's measured values substituted. Returns
    (hours, flags); a flag names each step measured more than REPROJECT_SLOWDOWN_MAX slower."""
    hours = dict(C.PROJECTED_HOURS_PER_CHECKPOINT)
    flags = []
    for k, v in (measured_rates or {}).items():
        if k not in hours:
            raise KeyError(f"unknown step {k!r}; known {sorted(hours)}")
        v = float(v)
        if not v >= 0:
            raise ValueError(f"measured hours for {k} must be >= 0, got {v}")
        if v > C.REPROJECT_SLOWDOWN_MAX * hours[k]:
            flags.append(
                {
                    "step": k,
                    "estimated_h": hours[k],
                    "measured_h": v,
                    "slowdown": round(v / hours[k], 3),
                }
            )
        hours[k] = v
    return hours, flags


def pod_hours(unit: str, hours: dict[str, float], cuts: Iterable[str] = ()) -> float:
    """GPU hours of one pod or the v2 rescoring, per §7's GPU table."""
    cuts = set(cuts)
    per = sum(hours.values())
    n = len(C.STAGE_A)
    if unit == "pod1":
        return SETUP_H + SMOKE_H + n * per + BUFFER_H["pod1"]
    if unit == "pod2":
        sample = hours["sample"] if CUT2 in cuts else 0.0
        return SETUP_H + len(C.STAGE_B) * (per - sample) + BUFFER_H["pod2"]
    if unit == "pod3":
        k = 1 if CUT1 in cuts else 2
        one = (
            hours["download"]
            + hours["train"]
            + hours["upload"]
            + hours["score"] * POD3_SCORED_STATES / SCORED_STATES
        )
        return SETUP_H + k * one + BUFFER_H["pod3"]
    if unit == "v2_rescore":
        one = hours["download"] + V2_ADAPTERS_H + hours["score"]
        return (
            SETUP_H
            + len(C.GRID) * one
            + V2_CONFIRM_STATES_H
            + V2_OLD_REF_H
            + BUFFER_H["v2_rescore"]
        )
    raise ValueError(unit)


def retry_hours(hours: dict[str, float]) -> float:
    """One retry: retrain one run, rescore and resample it (half a checkpoint's work)."""
    return (hours["train"] + hours["score"] + hours["sample"]) / 2


def sampled_api_usd(adapters: int) -> float:
    return adapters * C.ANSWERS_PER_ADAPTER * CALLS_PER_ANSWER * C.API_USD_PER_1K_JUDGE_CALLS / 1000


def pod_usd(unit: str, hours: dict[str, float], cuts: Iterable[str] = ()) -> float:
    rate = _rate(V2_GPU) if unit == "v2_rescore" else C.H100_SECURE_USD_PER_H
    return pod_hours(unit, hours, cuts) * rate


def empty_state() -> dict:
    """Nothing done yet: the state before the first launch."""
    return {
        "units": dict.fromkeys(UNITS, "pending"),
        "retries_left": C.MAX_RETRIES,
        "recorded": {"gpu": 0.0, "api": 0.0},
        "open": {"gpu": 0.0, "api": 0.0, "retry_share_gpu": 0.0},
        "cuts": [],
        "pod2_sampling": None,
        "pairs_spent_api": 0.0,  # recorded spend of the rebuilds sharing the allowance (c2, c3)
    }


def project(state: dict, measured_rates: dict | None = None, cuts: Iterable[str] = ()) -> dict:
    """The remaining mandatory work and the projected spend against every limit.

    `state` is `empty_state()`-shaped (see `gather_state`). `cuts` are applied on top of the
    state's already-decided cuts. Returns {items, remaining, committed, projected, limits, over,
    flags, cuts, hours, state, measured_rates}; `over` lists the limits the projection exceeds."""
    hours, flags = hours_per_checkpoint(measured_rates)
    cuts = list(dict.fromkeys([*state.get("cuts", []), *cuts]))
    bad = set(cuts) - set(C.CUTS)
    if bad:
        raise ValueError(f"unknown cuts {sorted(bad)}")
    units = {**dict.fromkeys(UNITS, "pending"), **state.get("units", {})}
    for u, s in units.items():
        if s not in STATUSES:
            raise ValueError(f"unit {u}: status {s!r} not in {STATUSES}")
    pending = {u for u, s in units.items() if s == "pending"}
    items: list[dict] = []

    def add(name: str, kind: str, usd: float, hrs: float | None = None) -> None:
        items.append(
            {
                "name": name,
                "kind": kind,
                "usd": round(usd, 6),
                **({"hours": round(hrs, 6)} if hrs is not None else {}),
            }
        )

    for u in ("pod1", "pod2", "pod3"):
        if u in pending:
            add(u, "gpu", pod_usd(u, hours, cuts), pod_hours(u, hours, cuts))
    if any(units[u] in ("pending", "running") for u in POD_UNITS):
        n = int(state.get("retries_left", C.MAX_RETRIES))
        if n:
            held = float(state.get("open", {}).get("retry_share_gpu", 0.0))
            gpu = max(0.0, n * retry_hours(hours) * C.H100_SECURE_USD_PER_H - held)
            add("retries", "gpu", gpu, n * retry_hours(hours))
            add("retries_judging", "api", n * sampled_api_usd(1))
    if "v2_rescore" in pending:
        add("v2_rescore", "gpu", pod_usd("v2_rescore", hours), pod_hours("v2_rescore", hours))
        add("pairs_v2", "api", C.PAIR_BUILD_MAX_USD)
    if "pairs" in pending:
        # Amendment A's rebuild allowance (<= $3, constructions 2 and 3 together), less what the
        # rebuilds have recorded; that spend, and construction 1's sunk spend, are in `committed`.
        add("pairs_v1", "api",
            max(0.0, C.PAIR_BUILD_MAX_USD - float(state.get("pairs_spent_api", 0.0))))
    if "judge_stageA" in pending:
        add("judge_stageA", "api", sampled_api_usd(STAGE_ADAPTERS) + TIEBREAK_USD["stageA"])
    if "judge_stageB" in pending and CUT2 not in cuts and state.get("pod2_sampling") is not False:
        add("judge_stageB", "api", sampled_api_usd(STAGE_ADAPTERS) + TIEBREAK_USD["stageB"])

    rem = {k: round(sum(i["usd"] for i in items if i["kind"] == k), 6) for k in ("gpu", "api")}
    rem["total"] = round(rem["gpu"] + rem["api"], 6)
    rec, opn = state.get("recorded", {}), state.get("open", {})
    com = {k: round(float(rec.get(k, 0.0)) + float(opn.get(k, 0.0)), 6) for k in ("gpu", "api")}
    com["total"] = round(com["gpu"] + com["api"], 6)
    proj = {k: round(com[k] + rem[k], 6) for k in ("gpu", "api", "total")}
    over = [k for k in ("gpu", "api", "total") if proj[k] > LIMITS[k] + 1e-9]
    return {
        "items": items,
        "remaining": rem,
        "committed": com,
        "projected": proj,
        "limits": dict(LIMITS),
        "over": over,
        "flags": flags,
        "cuts": cuts,
        "hours": hours,
        "state": state,
        "measured_rates": measured_rates,
    }


def apply_cuts(projection: dict) -> tuple[list[str], dict]:
    """Cut 1, then cut 2, while any limit is exceeded (§7). Returns (cuts now in force, the new
    projection). A cut that would no longer save anything is not applied. If a limit is still
    exceeded after both, the projection carries `stop` (a §7 stopping rule)."""
    p = projection
    for cut in C.CUTS:
        if not p["over"]:
            break
        if cut in p["cuts"]:
            continue
        q = project(p["state"], p["measured_rates"], [*p["cuts"], cut])
        if q["remaining"]["total"] < p["remaining"]["total"] - 1e-9:
            p = q
    if p["over"]:
        p = {**p, "stop": STOP_OVER_LIMIT}
    return list(p["cuts"]), p


def decide_pod2_sampling(state: dict, measured_rates: dict | None = None) -> dict:
    """Cut 2 is decided before pod 2 launches (plan v2 fix 8): pod 2 then runs with or without
    the sampled secondary. Returns {sample, cuts, projection}; refuses once pod 2 has launched."""
    if state.get("units", {}).get("pod2", "pending") != "pending":
        raise ValueError(
            f"cut 2 must be decided before pod 2 launches; pod 2 is {state['units']['pod2']}"
        )
    cuts, p = apply_cuts(project(state, measured_rates))
    return {"sample": CUT2 not in cuts, "cuts": cuts, "projection": p}


def completion_reserve(projection: dict) -> dict:
    """The mandatory completion cost still to come ({gpu, api, total}), after cuts."""
    return dict(projection["remaining"])


def optional_fits(projection: dict, usd: float, kind: str) -> bool:
    """Optional work may launch only if it leaves the mandatory completion cost inside every
    limit: committed + remaining mandatory + `usd` <= each limit."""
    if kind not in ("gpu", "api"):
        raise ValueError(kind)
    if usd < 0:
        raise ValueError("usd must be >= 0")
    after = dict(projection["projected"])
    after[kind] += usd
    after["total"] += usd
    return all(after[k] <= LIMITS[k] + 1e-9 for k in LIMITS)


def speculative_ok(usd: float, already_usd: float = 0.0) -> bool:
    """Speculative work (while a review of its dependency is pending) is bounded at
    SPECULATIVE_MAX_USD in all (§7): `already_usd` is what earlier speculative work committed.

    Not used by any launch or job path: E3 runs no speculative work. Pod 3 (the only candidate)
    waits for the rules review to pass (`review_17r_passed`; code review A r1, fix A9)."""
    if usd < 0 or already_usd < 0:
        raise ValueError("amounts must be >= 0")
    return already_usd + usd <= C.SPECULATIVE_MAX_USD + 1e-9


def deadline_left(now: datetime | None = None) -> timedelta:
    """Time left to the 5-day wall-clock cap (negative once passed)."""
    return C.DEADLINE_UTC - (now or datetime.now(UTC))


def deadline_passed(now: datetime | None = None) -> bool:
    return deadline_left(now) <= timedelta(0)


def retries_used(train_dir: str | Path) -> int:
    """Retries recorded in the training records. A record carries `attempt` (1-based; a retry
    is attempt >= 2) or `retries` (an int); the larger reading per cell counts. Other json
    files in the directory (batch order, loss checks) carry neither and count zero."""
    used: dict[str, int] = {}
    d = Path(train_dir)
    if not d.is_dir():
        return 0
    for f in sorted(d.rglob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        n = 0
        if isinstance(rec.get("attempt"), int):
            n = max(n, rec["attempt"] - 1)
        if isinstance(rec.get("retries"), int):
            n = max(n, rec["retries"])
        cell = str(rec.get("cell") or f.name.split(".")[0])
        used[cell] = max(used.get(cell, 0), n)
    return sum(used.values())


def retries_left(train_dir: str | Path) -> int:
    """At most MAX_RETRIES in the whole experiment (§4.13)."""
    return max(0, C.MAX_RETRIES - retries_used(train_dir))


# --- the orchestrator's log and the state read from disk ---------------------------------------


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_log(root: str | Path, event: dict) -> dict:
    """Append one event to logs/E3-orchestrate.jsonl (flock-locked, one complete line)."""
    path = Path(root) / ORCH_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"utc": utc_now(), "exp": C.EXP, **event}
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def read_log(root: str | Path) -> list[dict]:
    path = Path(root) / ORCH_LOG
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def pod_events_path(root: str | Path) -> Path:
    """logs/pods_E3.jsonl, as `emx.runpod` writes it (EMX_POD_LOG_DIR sandboxes it in tests)."""
    d = os.environ.get("EMX_POD_LOG_DIR")
    return (Path(d) if d else Path(root) / "logs") / f"pods_{C.EXP}.jsonl"


def pod_sessions(root: str | Path) -> list[dict]:
    """One row per E3 pod from its lifecycle log: {pod_id, slot, t0, running}."""
    path = pod_events_path(root)
    pods: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            pid = e.get("pod_id")
            if not pid:
                continue
            if e.get("event") == "created":
                pods[pid] = {
                    "pod_id": pid,
                    "slot": e.get("slot", ""),
                    "t0": e.get("utc"),
                    "running": True,
                }
            elif e.get("event") == "terminated" and pid in pods:
                pods[pid]["running"] = False
    return list(pods.values())


def running_slots(root: str | Path) -> list[dict]:
    """Running E3 pods with a slot (an untracked pod reaped by pod_down has none)."""
    return [p for p in pod_sessions(root) if p["running"] and p["slot"]]


def unit_events(log: list[dict]) -> dict[str, dict]:
    """The latest launch, cleanup and mark event per unit. A cleanup with outcome "done" (the
    unit's results retrieved and verified) finishes the unit; any other cleanup returns it to
    "pending" (a relaunch is projected in full); a cleanup that could not confirm termination
    keeps it "running", so its reservation still counts."""
    out: dict[str, dict] = {}
    for e in log:
        u = e.get("unit")
        if u and e.get("event") in ("launched", "cleaned", "cleanup_failed", "mark"):
            out[u] = e
    return out


def _rule(root: Path, name: str) -> dict | None:
    p = root / C.rule_path(name)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def manifest_complete(path: Path) -> bool:
    """A manifest exists and carries no `# MISSING` section (written by cleanup). Only for the
    laptop-written manifests (judging); a pod unit needs `unit_complete`."""
    try:
        text = path.read_text()
    except OSError:
        return False
    return "# MISSING" not in text


def done_path(unit: str) -> str:
    """The pod job's final completion record for a unit (written once, after its last step)."""
    return f"{C.RESULTS}/DONE.{unit}.json"


def unit_complete(root: str | Path, unit: str) -> bool:
    """A pod unit is complete only with its completion record `DONE.<unit>.json` (keys unit,
    pod_id, utc, artifacts, manifest_sha256) whose manifest_sha256 is the sha256 of the local
    unit manifest, and that manifest carries no `# MISSING` section. A manifest without it is a
    partial one (the job rewrites the manifest after every checkpoint; code review A r1, A5)."""
    root = Path(root)
    try:
        d = json.loads((root / done_path(unit)).read_text())
        text = (root / C.manifest_path(unit)).read_bytes()
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict) or d.get("unit") != unit or not d.get("pod_id"):
        return False
    if not isinstance(d.get("artifacts"), list) or b"# MISSING" in text:
        return False
    return d.get("manifest_sha256") == hashlib.sha256(text).hexdigest()


def read_stopped(root: str | Path) -> dict | None:
    """The terminal stop record, or None. An unreadable file still counts as stopped."""
    p = Path(root) / STOPPED_FILE
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"rule": "unreadable STOPPED.json", "utc": None, "detail": str(p)}
    return d if isinstance(d, dict) else {"rule": "malformed STOPPED.json", "utc": None}


def write_stopped(root: str | Path, rule: str, detail=None) -> tuple[dict, bool]:
    """Record a §7 stop durably (exclusive create: the first stop's rule is kept). Returns
    (the record in force, whether this call created it)."""
    p = Path(root) / STOPPED_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"rule": rule, "utc": utc_now(), "detail": detail}
    try:
        with open(p, "x") as f:
            f.write(json.dumps(rec, indent=2, default=str) + "\n")
    except FileExistsError:
        return read_stopped(root) or rec, False
    return rec, True


def review_17r_passed(root: str | Path) -> bool:
    """The rules review (17r) passed: `results/E3/analysis/review_17r.json` says
    {"outcome": "pass"} (written by the main session). Pod 3 needs it (no speculative work)."""
    try:
        d = json.loads((Path(root) / REVIEW_17R_FILE).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(d, dict) and d.get("outcome") == "pass"


def gather_state(root: str | Path | None = None, spend_mod=None) -> dict:
    """The projection's state, read from disk: the orchestrator log (launches, cleanups, marks,
    cuts), the manifests, the rule outputs, the training records and the spend ledger."""
    root = Path(root or REPO_ROOT)
    if spend_mod is None:
        from emx import spend as spend_mod
    st = empty_state()
    log = read_log(root)
    units = st["units"]
    if (root / C.PAIRS_FILE).exists():
        units["pairs"] = "done"
    for u in POD_UNITS:  # a pod unit needs its completion record, never a manifest alone
        if unit_complete(root, u):
            units[u] = "done"
    for u, man in (("judge_stageA", "judged-stageA"), ("judge_stageB", "judged-stageB")):
        if manifest_complete(root / C.manifest_path(man)):
            units[u] = "done"
    stage_a, stage_b = _rule(root, "stageA"), _rule(root, "stageB")
    if stage_a and stage_a.get("outcome") in ("fail", "inconclusive"):
        units["judge_stageB"] = units["pod3"] = "skipped"  # samples never judged; no window
    if stage_b and stage_b.get("outcome") and not stage_b.get("candidates"):
        units["pod3"] = "skipped"  # no candidate window: confirmation skipped
    retry_share = open_gpu = 0.0
    launched: dict[str, dict] = {}
    pod_state: dict[str, str] = {}  # unit -> its latest pod event (launched / cleaned / ...)
    for e in log:
        u, ev = e.get("unit"), e.get("event")
        if u in POD_UNITS and ev in ("launched", "cleaned", "cleanup_failed"):
            pod_state[u] = ev
            if ev == "launched":
                launched[u] = e
    for u, ev in pod_state.items():
        if ev in ("launched", "cleanup_failed"):
            # until cleanup confirms termination, the pod's reservation bounds its unrecorded
            # charge (also once its manifest is pulled and the unit counts as done)
            open_gpu += float(launched[u].get("reservation_usd", 0.0))
            retry_share += float(launched[u].get("retry_share_usd", 0.0))
    for u, e in unit_events(log).items():
        if u not in units:
            continue
        if e["event"] == "mark":
            units[u] = e["status"]
        elif e["event"] == "cleaned":  # its charges are in the ledger now
            if units[u] != "done":
                units[u] = "done" if e.get("outcome") == "done" else "pending"
        elif units[u] != "done":
            units[u] = "running"
    for e in log:
        if e.get("event") == "cuts":
            st["cuts"] = list(dict.fromkeys([*st["cuts"], *e.get("cuts", [])]))
        elif e.get("event") == "cuts_reprojected":
            # §4.13: after the smoke test, §7 is reprojected with the measured rates and the cuts
            # re-decided before the next reservation; this replaces earlier (estimate-based) cuts
            st["cuts"] = list(e.get("cuts", []))
        if e.get("event") == "launched" and e.get("unit") == "pod2":
            st["pod2_sampling"] = e.get("pod2_sampling")
    st["retries_left"] = retries_left(root / C.TRAIN_DIR)
    read = getattr(spend_mod, "read_entries", None)  # test fakes may not provide it
    st["pairs_spent_api"] = round(sum(
        float(e.get("usd", 0.0)) for e in (read() if read else [])
        if e.get("exp") == C.EXP and str(e.get("note", "")).startswith(PAIRS_NOTE_PREFIX)), 6)
    t = spend_mod.totals(C.EXP)
    st["recorded"] = {"gpu": float(t["gpu_usd"]), "api": float(t["judge_usd"])}
    st["open"] = {
        "gpu": round(open_gpu, 6),
        "api": float(t.get("judge_reserved_usd", 0.0)),
        "retry_share_gpu": round(retry_share, 6),
    }
    return st


def load_rates(root: str | Path | None = None, path: str | Path | None = None) -> dict | None:
    """The smoke test's measured hours per checkpoint ({step: hours}), when written."""
    p = Path(path) if path else Path(root or REPO_ROOT) / RATES_FILE
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return d.get("hours_per_checkpoint", d)


# --- the re-arm hook (plan v4 fix 3) ------------------------------------------------------------


class RearmFailed(RuntimeError):
    """A running E3 pod could not be re-armed after an API reservation change. Cleanup has
    already run for the failing slot(s); the caller propagates this so paid work stops."""


Runner = Callable[..., tuple[int, str]]
_FAIL_LINE = re.compile(r"E3 killswitch (_[A-Za-z0-9]{1,8}):")


def run_cmd(
    cmd: list[str],
    *,
    root: str | Path | None = None,
    env: dict | None = None,
    timeout: float | None = None,
) -> tuple[int, str]:
    """Run a command in the repo root; returns (exit code, stdout + stderr).

    With `timeout`, the command runs in its own process group (session), and the whole group
    (rsync, ssh and any other grandchild) is killed when the deadline passes: exit 124. A plain
    `subprocess.run(timeout=)` kills only the child and then waits on pipes a grandchild still
    holds, which can hang forever (code review A r1, fix A2)."""
    try:
        p = subprocess.Popen(
            cmd,
            cwd=str(root or REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **(env or {})},
            start_new_session=timeout is not None,
        )
    except OSError as e:
        return 124, f"{type(e).__name__}: {e}"
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a descendant left the group and holds the pipes
            out, err = "", ""
        return 124, f"TimeoutExpired: {' '.join(map(str, cmd[:6]))} killed after {timeout} s\n" + (
            (out or "") + (err or "")
        )
    return p.returncode, (out or "") + (err or "")


def _kill_group(p: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL whatever is left after a few seconds."""
    import signal
    import time

    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        t = time.monotonic() + grace
        while time.monotonic() < t and p.poll() is None:
            time.sleep(0.05)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _cleanup_via_cli(slot: str, reason: str, root: Path, runner: Runner) -> int:
    rc, _ = runner(
        [
            "uv",
            "run",
            "python",
            "scripts/e3_orchestrate.py",
            "cleanup",
            "--slot",
            slot,
            "--reason",
            reason,
            "--outcome",
            "aborted",
        ],
        root=root,
    )
    return rc


def _acked_deadline(rr, pod_id: str):
    fn = getattr(rr, "pod_deadline", None)
    if fn is None:
        return None
    try:
        return fn(pod_id)
    except Exception:  # noqa: BLE001 - an unreadable deadline is an unconfirmed arm
        return None


def after_api_change(
    exp: str,
    *,
    root: str | Path | None = None,
    runner: Runner | None = None,
    cleanup: Callable[[str, str], int] | None = None,
    rr=None,
) -> None:
    """Re-arm every running E3 pod after an API reserve, settle or release (plan v4 fix 3).

    A no-op unless `exp == "E3"` and E3 pods are running. Otherwise one
    `bash scripts/pod_push.sh --exp E3 --slot S --arm-only` re-arms every running slot, each at
    its own deadline from `emx.runpod --per-slot` (min of its reservation deadline, the global
    kill-line deadline and its current acknowledged deadline: `runpod_reservations.rearm_target`).
    A slot fails if pod_push names it in a failure line, or if its acknowledged deadline is
    missing or later than before the re-arm (an automatic refresh never extends); a non-zero
    exit that names no slot fails every running slot. Each failing slot goes through cleanup,
    then `RearmFailed` is raised."""
    if exp != C.EXP:
        return
    root = Path(root or REPO_ROOT)
    slots = running_slots(root)
    if not slots:
        return
    runner = runner or run_cmd
    if rr is None:
        try:
            from emx import runpod_reservations as rr
        except ImportError:
            rr = None
    if cleanup is None:

        def cleanup(slot: str, reason: str) -> int:
            return _cleanup_via_cli(slot, reason, root, runner)

    before = {s["slot"]: _acked_deadline(rr, s["pod_id"]) for s in slots}
    rc, out = runner(
        ["bash", "scripts/pod_push.sh", "--exp", C.EXP, "--slot", slots[0]["slot"], "--arm-only"],
        root=root,
    )
    failed = {m.group(1): "pod_push named the slot" for m in _FAIL_LINE.finditer(out)}
    if rc != 0 and not failed:
        failed = {s["slot"]: f"pod_push --arm-only exited {rc}" for s in slots}
    for s in slots:
        if s["slot"] in failed or rr is None or not hasattr(rr, "pod_deadline"):
            continue
        after = _acked_deadline(rr, s["pod_id"])
        if after is None:
            failed[s["slot"]] = "no acknowledged deadline after re-arm"
        elif before[s["slot"]] is not None and after > before[s["slot"]]:
            failed[s["slot"]] = f"deadline extended {before[s['slot']]} -> {after}"
    append_log(
        root,
        {
            "cmd": "rearm",
            "event": "rearm",
            "rc": rc,
            "slots": [s["slot"] for s in slots],
            "failed": failed,
        },
    )
    if not failed:
        return
    for slot, why in failed.items():
        cleanup(slot, f"rearm failed: {why}")
    raise RearmFailed(f"E3 re-arm failed for {sorted(failed)}: {failed}; cleanup ran")
