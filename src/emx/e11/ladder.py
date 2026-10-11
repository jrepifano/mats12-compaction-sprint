"""The pod side of E11's dose ladder (prereg §4.3; implementation plan "Laptop-pod barrier"): a
JSON state machine over `C.LADDER_STATE`, pure functions except the file helpers, so the rules are
unit-testable without a pod.

State (`results/E11/ladder/state.json`):
    {"version": 1,
     "datasets": {ds: {"status", "rung", "pilots": {"<k>": pilot status}, "reason"}},
     "decisions": {"<k>": {"ready_sha256", "decision_sha256", "accepted_utc"}},
     "evals": {run_key: {"status": "done"|"failed"|"check_failed", "attempts", "ds", "rung"}},
     "idle_since": epoch seconds | None,
     "part1": {"stopped": bool, "reason": str | None},
     "history": [{"utc", "event"}, ...]}

Dataset status (interfaces.md): "pending" (piloting `rung`) -> "eligible@<k>" | "ineligible" (all
three rungs measured below the bar) | "incomplete@<k>" (a pilot failed a check, or its answers
stayed > 2% unscored, or an evaluation run of the dataset failed twice) | "dropped" (cut 3,
finance only). Pilot status: "trained" (adapter saved and checked) -> "generated" (answers, rung
manifest and ready file written); "failed" (the dataset is then incomplete@k).

Kill (prereg §7 rule 1): bad medical "ineligible". An incomplete bad medical stops part 1 as a
measurement failure (recorded distinctly, not a kill). An incomplete or ineligible finance leaves
the gate on bad medical alone.

Stdlib only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from emx.e11 import contracts as C

VERSION = 1
PENDING, INELIGIBLE, DROPPED = "pending", "ineligible", "dropped"
TRAINED, GENERATED, FAILED = "trained", "generated", "failed"
EVAL_FINAL = ("done", "failed", "check_failed")
MAX_RUNG = max(C.RUNGS)


def _utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def eval_keys(ds: str) -> list[str]:
    """The six evaluation run keys of a dataset, in `C.EVAL_KEYS` (training) order."""
    return [k for k in C.EVAL_KEYS if k.startswith(f"{ds}_")]


def rung_of(status: str) -> int | None:
    """3 for "eligible@3" / "incomplete@3"; None otherwise."""
    return int(status.split("@", 1)[1]) if "@" in status else None


def is_eligible(status: str) -> bool:
    return status.startswith("eligible@")


def is_incomplete(status: str) -> bool:
    return status.startswith("incomplete@")


# --- state ---------------------------------------------------------------------------------------


def init_state(plan: dict) -> dict:
    ds = {}
    for d in C.DS_KEYS:
        if d == "fin" and not plan.get("fin", True):
            ds[d] = {"status": DROPPED, "rung": None, "pilots": {}, "reason": "cut 3 (bench)"}
        else:
            ds[d] = {"status": PENDING, "rung": 1, "pilots": {}, "reason": None}
    return {"version": VERSION, "datasets": ds, "decisions": {}, "evals": {}, "idle_since": None,
            "part1": {"stopped": False, "reason": None},
            "history": [{"utc": _utc(), "event": f"init plan={json.dumps(plan, sort_keys=True)}"}]}


def load_state(root, plan: dict | None = None) -> dict:
    """The saved state, or a fresh one for `plan` (default: `C.PLAN_DEFAULTS`) if none exists."""
    p = Path(root) / C.LADDER_STATE
    if p.is_file():
        state = json.loads(p.read_text())
        if state.get("version") != VERSION:
            raise ValueError(f"{p}: state version {state.get('version')} != {VERSION}")
        return state
    return init_state(dict(C.PLAN_DEFAULTS) if plan is None else plan)


def save_state(root, state: dict) -> None:
    p = Path(root) / C.LADDER_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)


def note(state: dict, event: str) -> None:
    state.setdefault("history", []).append({"utc": _utc(), "event": event})


def mark_incomplete(state: dict, ds: str, rung: int, reason: str) -> None:
    st = state["datasets"][ds]
    st["status"] = f"incomplete@{rung}"
    st["reason"] = reason
    note(state, f"{ds} incomplete@{rung}: {reason}")


def set_pilot(state: dict, ds: str, rung: int, status: str, reason: str | None = None) -> None:
    """Record a pilot outcome; a failed pilot makes its dataset incomplete@rung."""
    state["datasets"][ds]["pilots"][str(rung)] = status
    note(state, f"pilot {C.pilot_key(ds, rung)} {status}" + (f": {reason}" if reason else ""))
    if status == FAILED:
        mark_incomplete(state, ds, rung, reason or "pilot failed")


def set_eval(state: dict, run_key: str, ds: str, rung: int, status: str, attempts: int,
             reason: str | None = None) -> None:
    """Record an evaluation run's final outcome; anything but "done" makes its dataset
    incomplete (prereg §7 kill rule 2: the non-finite rerun has already happened)."""
    state["evals"][run_key] = {"status": status, "attempts": attempts, "ds": ds, "rung": rung}
    note(state, f"eval {run_key} {status} after {attempts} attempt(s)")
    if status != "done" and is_eligible(state["datasets"][ds]["status"]):
        mark_incomplete(state, ds, rung, reason or f"evaluation run {run_key} {status}")


def pending_evals(state: dict, ds: str) -> list[str]:
    return [k for k in eval_keys(ds) if state["evals"].get(k, {}).get("status") not in EVAL_FINAL]


# --- decisions -----------------------------------------------------------------------------------


def decision_problems(root, rung: int) -> tuple[dict | None, list[str]]:
    """(decision, problems) of `decision_r<rung>.json`: (None, []) while it does not exist; the
    decision with [] when valid. Valid = its `rung` matches, its `ready_sha256` equals the sha256 of
    this rung's own ready file, it covers every pilot the ready file names, and each dataset entry
    carries a boolean `eligible` (and `incomplete`, default False) consistent with its integer
    counts (`C.is_eligible`) when they are given."""
    root = Path(root)
    dpath, rpath = root / C.ladder_decision(rung), root / C.ladder_ready(rung)
    if not dpath.is_file():
        return None, []
    try:
        dec = json.loads(dpath.read_text())
    except (OSError, ValueError) as e:
        return None, [f"unreadable: {type(e).__name__}: {e}"]
    if not rpath.is_file():
        return None, [f"{C.ladder_ready(rung)} missing"]
    probs = []
    if dec.get("rung") != rung:
        probs.append(f"rung {dec.get('rung')!r} != {rung}")
    ready_sha = _sha(rpath)
    if dec.get("ready_sha256") != ready_sha:
        probs.append(f"ready_sha256 {dec.get('ready_sha256')!r} != {ready_sha} (the rung's own "
                     f"ready file)")
    entries = dec.get("datasets")
    if not isinstance(entries, dict):
        probs.append("no datasets mapping")
        entries = {}
    ready = json.loads(rpath.read_text())
    for key in ready.get("pilot_keys", []):
        ds = key.split("_", 1)[0]
        if ds not in entries:
            probs.append(f"no entry for {ds} (pilot {key})")
    for ds, e in entries.items():
        if ds not in C.DS_KEYS:
            probs.append(f"unknown dataset {ds!r}")
            continue
        if not isinstance(e, dict) or not isinstance(e.get("eligible"), bool):
            probs.append(f"{ds}: eligible is not a boolean")
            continue
        inc = e.get("incomplete", False)
        if not isinstance(inc, bool):
            probs.append(f"{ds}: incomplete is not a boolean")
        if inc and e["eligible"]:
            probs.append(f"{ds}: both eligible and incomplete")
        n_em, n_sc = e.get("n_em"), e.get("n_scored")
        if not inc and isinstance(n_em, int) and isinstance(n_sc, int) \
                and C.is_eligible(n_em, n_sc) != e["eligible"]:
            probs.append(f"{ds}: eligible={e['eligible']} but {n_em}/{n_sc} says "
                         f"{C.is_eligible(n_em, n_sc)}")
    if probs:
        return None, probs
    return {**dec, "decision_sha256": _sha(dpath)}, []


def accept_decision(root, rung: int) -> dict | None:
    """The rung's decision if it exists and is valid (`decision_problems`), else None."""
    return decision_problems(root, rung)[0]


def apply_decision(state: dict, rung: int, decision: dict) -> dict:
    """Move every dataset pending at `rung` with a generated pilot: incomplete -> incomplete@rung;
    eligible -> eligible@rung; below the bar -> the next rung, or "ineligible" after rung 3.
    Applying the same rung twice is a no-op."""
    if str(rung) in state["decisions"]:
        return state
    for ds, st in state["datasets"].items():
        if st["status"] != PENDING or st["rung"] != rung or ds not in decision["datasets"]:
            continue
        e = decision["datasets"][ds]
        if e.get("incomplete", False):
            mark_incomplete(state, ds, rung, "pilot answers incomplete (laptop decision)")
        elif e["eligible"]:
            st["status"] = f"eligible@{rung}"
            note(state, f"{ds} eligible@{rung} ({e.get('n_em')}/{e.get('n_scored')})")
        elif rung < MAX_RUNG:
            st["rung"] = rung + 1
            note(state, f"{ds} below the bar at rung {rung} ({e.get('n_em')}/{e.get('n_scored')})"
                        f": rung {rung + 1}")
        else:
            st["status"] = INELIGIBLE
            note(state, f"{ds} ineligible at every rung")
    state["decisions"][str(rung)] = {
        "ready_sha256": decision.get("ready_sha256"),
        "decision_sha256": decision.get("decision_sha256"), "accepted_utc": _utc(),
    }
    state["idle_since"] = None
    return state


# --- what to do next ------------------------------------------------------------------------------


def next_actions(state: dict, plan: dict) -> list[tuple[str, ...]]:
    """The ladder's next actions, trainable work first:
    ("kill",)               bad medical ineligible at every rung (part 1 stops)
    ("incomplete", ds)      ds incomplete (for "med": part 1 stops, a measurement failure)
    ("drop", ds)            finance pending while the plan has cut it (cut 3)
    ("pilot", ds, k)        train ds's rung-k pilot
    ("gen", ds, k)          ds's rung-k pilot is trained: its answers, manifest and ready file
    ("eval", ds)            ds is eligible and has evaluation runs left to train
    ("wait", k)             a rung-k decision is outstanding
    An empty list means done: every dataset eligible-and-trained, ineligible, incomplete or
    dropped."""
    ds = state["datasets"]
    med = ds["med"]["status"]
    if med == INELIGIBLE:
        return [("kill",)]
    if is_incomplete(med):
        return [("incomplete", "med")]
    acts: list[tuple[str, ...]] = []
    for d in C.DS_KEYS:
        st = ds[d]
        s = st["status"]
        if s == PENDING and d == "fin" and not plan.get("fin", True):
            acts.append(("drop", d))
        elif s == PENDING:
            k = st["rung"]
            p = st["pilots"].get(str(k))
            if p is None:
                acts.append(("pilot", d, k))
            elif p == TRAINED:
                acts.append(("gen", d, k))
            elif p == GENERATED:
                acts.append(("wait", k))
        elif is_incomplete(s):
            acts.append(("incomplete", d))
        elif is_eligible(s) and pending_evals(state, d):
            acts.append(("eval", d))
    order = {"drop": 0, "pilot": 1, "gen": 2, "eval": 3, "wait": 4, "incomplete": 5}
    return sorted(acts, key=lambda a: order[a[0]])


def outcome(state: dict, plan: dict) -> str | None:
    """"kill", "med_incomplete" or "done" once the ladder is over, else None."""
    acts = next_actions(state, plan)
    if acts == [("kill",)]:
        return "kill"
    if acts == [("incomplete", "med")]:
        return "med_incomplete"
    if all(a[0] == "incomplete" for a in acts):
        return "done"
    return None


def stop_part1(state: dict, why: str) -> None:
    state["part1"] = {"stopped": True, "reason": why}
    note(state, f"part 1 stopped: {why}")


def idle_update(state: dict, now: float, idle: bool) -> float:
    """Idle seconds so far: the clock starts when the pod first has nothing to do but wait and
    resets whenever it does work (or accepts a decision), so only idle time counts toward
    `C.LADDER_WAIT_MIN`."""
    if not idle:
        state["idle_since"] = None
        return 0.0
    if state.get("idle_since") is None:
        state["idle_since"] = now
    return max(0.0, now - float(state["idle_since"]))


def timed_out(idle_s: float, wait_min: float = C.LADDER_WAIT_MIN) -> bool:
    return idle_s > wait_min * 60
