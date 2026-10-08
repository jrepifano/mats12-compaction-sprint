"""Spend ledger (`logs/costs.jsonl`) and the one combined budget guard, capped per experiment.

New module (implementation plan §5 "One combined guard"; prereg §7 and §11 amendment A). The
mechanism follows ../mats12_neel_working/scripts/judge_panel_spend.py (project, then exit non-zero
before the call that would cross the cap); the numbers come from `emx.contracts`.

Ledger: one JSON object per line, `{utc, exp, kind: gpu|judge, resource, units, usd, note, id}`.
`id` (a uuid4 hex, added 2026-10-04) makes each charge unique, so `scripts/merge_ledger.py` can
combine per-worktree ledgers without merging two real charges or double counting one. Older rows
have no `id`.
Every append takes an exclusive `fcntl.flock` on the ledger file itself and writes one complete
line, so concurrent writers cannot interleave (the working repo's ledger was corrupted by
parallel writers). Reads take a shared lock. A malformed line raises instead of being skipped.

Guard: `check_total(projected_usd, kind, running_pod_usd, exp)` adds GPU spent so far, the running
pods' projected cost, judge spent so far and the batch about to run, and raises `BudgetExceeded`
(a `SystemExit` with code 2, so an uncaught guard stops a script non-zero) if the total would pass
the experiment's hard cap, GPU would pass its GPU limit, or judges would pass its judge limit.
Caps live in `CAPS`, keyed by experiment (I1a added 2026-09-29, implementation plan §1.3; E1b added
2026-10-03, E1b implementation plan §1.2; I9 added 2026-10-04, I9 implementation plan §1.2): E1 and E1b have a fixed judge limit; I1a's and I9's judge limits are dynamic, hard cap minus a reserve minus GPU spend
(prereg v0.4 §7.3: $85 cap, $40 GPU, judges get $83 minus GPU).

API reservations (added 2026-10-06 for E3 and E9; E3 prereg §7 "This needs a small, additive
extension to `emx.spend`", E3 implementation plan v2 fix 4, v3 fixes 2-3, v4 fix 1; the pattern is
`src/s4world/budget.py`'s, but the reservations live in the ledger itself, not in a sidecar, so
every state change is one locked, fsynced append and a crash can't lose one):
- Reservation rows are ordinary ledger lines (own unique `id`, `kind: "judge"`, `usd: 0`,
  `resource: "reservation"`, `units: 0`) plus `reservation_id` and `op` in {reserve, submitted,
  close}; the reserve row also carries `reserve_usd`. Charges made under a reservation are normal
  `record()` rows with `reservation_id` set. The closing row of `settle_api` is also the final
  charge (its `usd` is the remaining actual cost). `spent()` is unchanged.
- State is an order-independent reduction over the rows sharing a `reservation_id`
  (`reservation_states`), so a ledger merge or reordering can't change it. A reservation is open if
  it has a reserve row and no close row. Its liability while open is max(0, reserve_usd - charges)
  and 0 once closed, so a batch counts once at every instant: as its reservation, then as
  max(reservation, charges so far), then as its charges.
- `check_total`, `watchdog_kill_usd` and `totals` count recorded charges plus open liabilities.
  With no reservation rows they give the same numbers as before.
- Calls whose outcome is uncertain are priced at the reservation price, and spend under a
  reservation is bounded by `reservation_headroom` while the process lives, so the full reserved
  amount covers whatever a process that died mid-batch spent. A crashed batch is never resumed
  under its old reservation: `recover_uncertain` settles it at the full amount, and the remaining
  work takes a fresh `reserve_api` (v4 fix 1).

CLI:
  uv run python -m emx.spend --report
  uv run python -m emx.spend --check 1.25 --kind judge [--running-pod-usd 0.0] [--exp I1a]
  uv run python -m emx.spend --reservations [--exp E3]

2026-10-06 (E3 code review A r1 finding 10): `record_once(charge_key, ...)` checks for an
existing row with the same `charge_key` and appends under one exclusive lock, so concurrent
settlement of one pod charges it once (`emx.runpod.finish_session` uses it). Additive only.

Stage limits (added 2026-10-06 for E9, prereg §7.3 "Stage accounting"; E9 implementation plan,
"Stage enforcement in spend.py"; additive):
- An experiment whose `Caps` has `stage_limits` (E9 only) tags every ledger row with a `stage`
  (`record`, `record_once`, `reserve_api`; a missing or unknown stage is refused). Stages map to
  limit groups (`stage_group`), each with a dollar limit.
- The stage is written on a reservation's reserve row; its charge, submitted and close rows inherit
  it, so settlement, release and `recover_uncertain` keep it.
- `check_total(..., stage=)` and `reserve_api(..., stage=)` also check the stage group's recorded
  spend + open reservations + `running_pod_usd` + the projected batch against the group's limit,
  under the same lock as the reserve row. `stage_totals(exp)` reports the groups.
- Experiments without stage limits behave exactly as before; a `stage` passed for them is
  recorded but not enforced.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from emx import contracts as e1c
from emx.e1b import contracts as e1bc
from emx.e3 import contracts as e3c
from emx.e9 import contracts as e9c
from emx.i1a import contracts as i1ac
from emx.i9 import contracts as i9c

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "logs" / "costs.jsonl"
LEDGER_ENV = "EMX_COSTS_LEDGER"  # override for tests and dry runs
KINDS: tuple[str, ...] = ("gpu", "judge")
RESERVATION_OPS: tuple[str, ...] = ("reserve", "submitted", "close")
RESERVATION_RESOURCE = "reservation"
EXP = "E1"

# Kept for E1 callers that import the module-level numbers.
HARD_CAP_USD = e1c.HARD_CAP_USD
GPU_LIMIT_USD = e1c.GPU_LIMIT_USD
JUDGE_LIMIT_USD = e1c.JUDGE_LIMIT_USD


@dataclass(frozen=True)
class Caps:
    hard_cap_usd: float
    gpu_limit_usd: float
    judge_limit_usd: float | None  # None: hard cap - reserve - GPU (spent + running + projected)
    reserve_usd: float = 0.0
    stage_limits: dict[str, float] | None = field(default=None, hash=False)  # group -> $ limit
    stage_group: dict[str, str] | None = field(default=None, hash=False)  # stage tag -> group

    def judge_limit(self, gpu_usd: float) -> float:
        if self.judge_limit_usd is not None:
            return self.judge_limit_usd
        return round(self.hard_cap_usd - self.reserve_usd - gpu_usd, 6)


CAPS: dict[str, Caps] = {
    "E1": Caps(e1c.HARD_CAP_USD, e1c.GPU_LIMIT_USD, e1c.JUDGE_LIMIT_USD),
    "I1a": Caps(i1ac.HARD_CAP_USD, i1ac.GPU_LIMIT_USD, None, i1ac.RESERVE_USD),
    "E1b": Caps(e1bc.HARD_CAP_USD, e1bc.GPU_LIMIT_USD, e1bc.JUDGE_LIMIT_USD),
    "I9": Caps(i9c.HARD_CAP_USD, i9c.GPU_LIMIT_USD, None, i9c.RESERVE_USD),
    "E3": Caps(e3c.HARD_CAP_USD, e3c.GPU_LIMIT_USD, e3c.API_LIMIT_USD, e3c.RESERVE_USD),
    # E9 (prereg §7.3, Amendment D): API limit $75; the backstop subtracts the API reserve, which
    # the orchestrator recomputes at each gate (reserve_usd here is the sign-off value).
    "E9": Caps(
        e9c.HARD_CAP_USD, e9c.GPU_LIMIT_USD, e9c.API_LIMIT_USD, e9c.API_RESERVE_AT_SIGNOFF_USD,
        stage_limits=dict(e9c.STAGE_LIMITS_USD), stage_group=dict(e9c.STAGE_GROUP),
    ),
}


def caps_for(exp: str) -> Caps:
    if exp not in CAPS:
        raise KeyError(f"no spend caps for experiment {exp!r}; known {sorted(CAPS)}")
    if exp == "E1":  # read the module-level numbers at call time: E1 tests pin them by monkeypatch
        return Caps(HARD_CAP_USD, GPU_LIMIT_USD, JUDGE_LIMIT_USD)
    return CAPS[exp]


class BudgetExceeded(SystemExit):
    """Raised by `check_total`; exit code 2 when uncaught."""

    def __init__(self, message: str, breakdown: dict):
        super().__init__(2)
        self.message = message
        self.breakdown = breakdown

    def __str__(self) -> str:
        return self.message


def ledger_path(ledger: str | Path | None = None) -> Path:
    if ledger is not None:
        return Path(ledger)
    env = os.environ.get(LEDGER_ENV)
    return Path(env) if env else DEFAULT_LEDGER


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReservationError(ValueError):
    """A reservation call that breaks the protocol: unknown, closed, owned by another experiment,
    released after submission, or a second open reservation for the same batch."""


def _check_usd(usd: float, what: str = "usd") -> float:
    if isinstance(usd, bool) or not isinstance(usd, int | float):
        raise TypeError(f"{what} must be a number, got {usd!r}")
    if not (math.isfinite(usd) and usd >= 0):
        raise ValueError(f"{what} must be a non-negative finite number, got {usd!r}")
    return round(float(usd), 6)


def _entry(
    kind: str, resource: str, units: float, usd: float, note: str, exp: str,
    stage: str | None = None,
) -> dict:
    e = {
        "utc": utc_now(),
        "exp": exp,
        "kind": kind,
        "resource": resource,
        "units": units,
        "usd": round(float(usd), 6),
        "note": note,
        "id": uuid.uuid4().hex,
    }
    if stage is not None:
        e["stage"] = stage
    return e


def _stage(exp: str, stage: str | None) -> str | None:
    """Validate `stage` for `exp`: required and known when the experiment has stage limits."""
    c = CAPS.get(exp)
    if c is None or not c.stage_limits:
        return stage
    if stage is None:
        raise ValueError(f"{exp} has stage limits: every ledger row needs a stage tag")
    if stage not in (c.stage_group or {}):
        raise ValueError(f"{exp}: unknown stage {stage!r}; known {sorted(c.stage_group or {})}")
    return stage


def _append(f, entry: dict) -> None:
    """Write one complete line to a ledger opened for append and held under LOCK_EX."""
    f.write(json.dumps(entry, sort_keys=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


def _parse(text: str, path: Path) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{path}:{i}: malformed ledger line: {e}") from e
    return out


@contextmanager
def _exclusive(ledger: str | Path | None = None):
    """Hold the ledger's exclusive lock and yield (append handle, entries read inside the lock).
    Read and append go through the same handle: a second `open` + `flock` on the same file from
    this process would block on our own lock."""
    path = ledger_path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            yield f, _parse(f.read(), path)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def record(
    kind: str,
    resource: str,
    units: float,
    usd: float,
    note: str = "",
    exp: str = EXP,
    ledger: str | Path | None = None,
    reservation_id: str | None = None,
    stage: str | None = None,
) -> dict:
    """Append one entry under an exclusive lock. `units` is GPU-hours or API calls.

    With `reservation_id`, the entry is a charge under that API reservation: it must be a judge
    charge, and the reservation must exist, belong to `exp` and be open (checked under the same
    lock as the append). The charge lowers the reservation's open liability by the same amount,
    and inherits the reservation's stage (a different `stage` is refused)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if not (usd >= 0 and float(usd) == float(usd)):
        raise ValueError(f"usd must be a non-negative number, got {usd!r}")
    if reservation_id is not None:
        if kind != "judge":
            raise ReservationError(
                f"charges under an API reservation must be kind 'judge', not {kind!r}"
            )
        _check_usd(usd)
        with _exclusive(ledger) as (f, entries):
            s = _require_open(reservation_states(entries), reservation_id, exp)
            if stage is not None and stage != s.get("stage"):
                raise ReservationError(
                    f"reservation {reservation_id} is stage {s.get('stage')!r}, not {stage!r}"
                )
            entry = _entry(kind, resource, units, usd, note, exp, s.get("stage"))
            entry["reservation_id"] = reservation_id
            _append(f, entry)
        return entry
    entry = _entry(kind, resource, units, usd, note, exp, _stage(exp, stage))
    line = json.dumps(entry, sort_keys=False) + "\n"
    path = ledger_path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return entry


def record_once(
    charge_key: str,
    kind: str,
    resource: str,
    units: float,
    usd: float,
    note: str = "",
    exp: str = EXP,
    ledger: str | Path | None = None,
    also_matches: Callable[[dict], bool] | None = None,
    stage: str | None = None,
) -> tuple[dict, bool]:
    """Append one entry carrying `charge_key`, unless an entry with that key (or, for rows
    written before keys existed, one `also_matches` accepts) is already in the ledger; the check
    and the append are one critical section under the exclusive lock, so two processes charging
    the same thing record it once (E3 code review A r1 finding 10). Returns (entry, appended)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    _check_usd(usd)
    stage = _stage(exp, stage)
    with _exclusive(ledger) as (f, entries):
        for e in entries:
            if e.get("charge_key") == charge_key or (also_matches is not None and also_matches(e)):
                return e, False
        entry = _entry(kind, resource, units, usd, note, exp, stage)
        entry["charge_key"] = charge_key
        _append(f, entry)
        return entry, True


def read_entries(ledger: str | Path | None = None) -> list[dict]:
    path = ledger_path(ledger)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            text = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return _parse(text, path)


def _sum(entries: list[dict], exp: str, kind: str | None) -> float:
    return round(
        sum(
            float(e.get("usd", 0.0))
            for e in entries
            if e.get("exp") == exp and (kind is None or e.get("kind") == kind)
        ),
        6,
    )


def spent(kind: str | None = None, exp: str = EXP, ledger: str | Path | None = None) -> float:
    """Dollars recorded for `exp`, optionally one kind only."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS} or None, got {kind!r}")
    return _sum(read_entries(ledger), exp, kind)


# --- API reservations -------------------------------------------------------------------------


def reservation_states(entries: list[dict]) -> dict[str, dict]:
    """Order-independent reduction of the ledger's reservation rows, keyed by `reservation_id`.

    Each state: {reservation_id, exp, reserve_usd, charges, liability, note, utc, submitted,
    closed}. `charges` sums `usd` over every row carrying the id (reserve and submitted rows add
    0; a settle row adds its final charge), as `spent()` does. Rows with a `reservation_id` but no
    reserve row have no state (they still count in `spent()`). Inconsistent rows (two reserve rows
    that disagree, rows of one reservation under different experiments, or a row carrying the id
    that is not kind "judge", which would move dollars between the GPU and API sides) raise: the ledger
    needs fixing by hand before any reservation can be trusted."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        rid = e.get("reservation_id")
        if rid:
            groups[rid].append(e)
    out: dict[str, dict] = {}
    for rid, rows in groups.items():
        reserves = [r for r in rows if r.get("op") == "reserve"]
        if not reserves:
            continue
        heads = {
            (r.get("exp"), float(r.get("reserve_usd", 0.0)), r.get("note", ""), r.get("stage"))
            for r in reserves
        }
        if len(heads) > 1:
            raise RuntimeError(f"reservation {rid}: reserve rows disagree: {sorted(map(str, heads))}")
        ((exp, reserve_usd, note, stage),) = heads
        exps = {r.get("exp") for r in rows}
        if exps != {exp}:
            raise RuntimeError(
                f"reservation {rid}: rows under several experiments {sorted(map(str, exps))}"
            )
        if any(r.get("kind") != "judge" for r in rows):
            raise RuntimeError(f"reservation {rid}: a row carrying its id is not kind 'judge'")
        charges = round(sum(float(r.get("usd", 0.0)) for r in rows), 6)
        closed = any(r.get("op") == "close" for r in rows)
        out[rid] = {
            "reservation_id": rid,
            "exp": exp,
            "reserve_usd": round(reserve_usd, 6),
            "charges": charges,
            "liability": 0.0 if closed else round(max(0.0, reserve_usd - charges), 6),
            "note": note,
            "utc": min(r.get("utc", "") for r in reserves),
            "submitted": any(r.get("op") == "submitted" for r in rows),
            "closed": closed,
            "stage": stage,
        }
    return out


def _open_liability(states: dict[str, dict], exp: str, exclude: str | None = None) -> float:
    return round(
        sum(
            s["liability"]
            for rid, s in states.items()
            if s["exp"] == exp and not s["closed"] and rid != exclude
        ),
        6,
    )


def _require(states: dict[str, dict], rid: str, exp: str | None) -> dict:
    s = states.get(rid)
    if s is None:
        raise ReservationError(f"unknown reservation {rid!r}")
    if exp is not None and s["exp"] != exp:
        raise ReservationError(f"reservation {rid} belongs to {s['exp']}, not {exp}")
    return s


def _require_open(states: dict[str, dict], rid: str, exp: str | None) -> dict:
    s = _require(states, rid, exp)
    if s["closed"]:
        raise ReservationError(f"reservation {rid} is closed; open a new one with reserve_api")
    return s


def _op_row(s: dict, op: str, usd: float = 0.0, note: str = "", resource: str = "") -> dict:
    e = _entry(
        "judge", resource or RESERVATION_RESOURCE, 0, usd, note or s["note"], s["exp"],
        s.get("stage"),
    )
    e["reservation_id"] = s["reservation_id"]
    e["op"] = op
    return e


def reserve_api(
    exp: str,
    usd: float,
    note: str,
    running_pod_usd: float = 0.0,
    ledger: str | Path | None = None,
    stage: str | None = None,
) -> str:
    """Admit an API batch of up to `usd` and return its reservation id, or raise `BudgetExceeded`.

    Under the ledger's exclusive lock: recorded spend + every open reservation of `exp` + this
    batch is checked against the experiment's judge (API) limit, GPU limit (recorded GPU +
    `running_pod_usd`) and hard cap, exactly as `check_total` does, and the reserve row is
    appended in the same critical section, so two processes racing for the last dollars can't
    both win. `note` is the batch key; a second open reservation with the same `exp` and `note`
    is refused (a crashed batch is settled with `recover_uncertain`, or an unsubmitted one is
    reused via `find_open` or released, before the batch reserves again)."""
    usd = _check_usd(usd)
    stage = _stage(exp, stage)
    if not note:
        raise ReservationError("reserve_api needs a non-empty note (the batch key)")
    with _exclusive(ledger) as (f, entries):
        states = reservation_states(entries)
        dup = [
            r
            for r, s in states.items()
            if s["exp"] == exp and s["note"] == note and not s["closed"]
        ]
        if dup:
            raise ReservationError(
                f"{exp}: reservation {dup[0]} for {note!r} is still open; reuse it (find_open), "
                "release it, or settle it (recover_uncertain) first"
            )
        _check(entries, states, usd, "judge", running_pod_usd, exp, stage=stage)
        rid = uuid.uuid4().hex
        row = _op_row(
            {"reservation_id": rid, "exp": exp, "note": note, "stage": stage}, "reserve"
        )
        row["reserve_usd"] = usd
        _append(f, row)
    return rid


def mark_submitted(
    reservation_id: str, exp: str | None = None, ledger: str | Path | None = None
) -> dict:
    """Mark that paid calls may have started under this reservation (before the first one). From
    then on it can only be settled, never released. Idempotent; refused once closed."""
    with _exclusive(ledger) as (f, entries):
        s = _require_open(reservation_states(entries), reservation_id, exp)
        if not s["submitted"]:
            _append(f, _op_row(s, "submitted"))
            s = {**s, "submitted": True}
    return s


def settle_api(
    reservation_id: str,
    total_usd: float | None = None,
    exp: str | None = None,
    note: str = "",
    resource: str = "",
    ledger: str | Path | None = None,
) -> dict:
    """Close a reservation, charging what it has not yet charged, in one locked append.

    `total_usd` is the reservation's TOTAL actual cost: the closing row charges
    max(0, total_usd - charges so far), so the batch counts once, as its charges. `None` means
    unknown (a crash): it settles conservatively at the full reserved amount
    (max(0, reserve_usd - charges)). Settling a closed reservation is a no-op. Returns the state
    after, with `settled_usd` (the closing charge; 0 for a no-op)."""
    if total_usd is not None:
        total_usd = _check_usd(total_usd, "total_usd")
    with _exclusive(ledger) as (f, entries):
        s = _require(reservation_states(entries), reservation_id, exp)
        if s["closed"]:
            return {**s, "settled_usd": 0.0}
        target = s["reserve_usd"] if total_usd is None else total_usd
        delta = round(max(0.0, target - s["charges"]), 6)
        why = "settled" if total_usd is not None else "settled at full reservation (actual unknown)"
        _append(f, _op_row(s, "close", delta, note or f"{s['note']} ({why})", resource))
    charges = round(s["charges"] + delta, 6)
    return {**s, "charges": charges, "liability": 0.0, "closed": True, "settled_usd": delta}


def release_api(
    reservation_id: str, exp: str | None = None, ledger: str | Path | None = None
) -> dict:
    """Close an unused reservation at $0. Refused once it has a `submitted` row or any charge
    (paid calls may have run: settle instead). Releasing a closed reservation is a no-op."""
    with _exclusive(ledger) as (f, entries):
        s = _require(reservation_states(entries), reservation_id, exp)
        if s["closed"]:
            return s
        if s["submitted"] or s["charges"] > 0:
            raise ReservationError(
                f"reservation {reservation_id} was submitted or charged; settle it, don't release it"
            )
        _append(f, _op_row(s, "close", 0.0, f"{s['note']} (released)"))
    return {**s, "liability": 0.0, "closed": True}


def recover_uncertain(exp: str, note: str, ledger: str | Path | None = None) -> list[str]:
    """Settle every open, submitted reservation of `exp` with this `note` at its full reserved
    amount (one locked critical section) and return their ids (plan v4 fix 1).

    A reservation found open with a `submitted` row belongs to a batch whose process died: some
    calls may have been paid without a charge row. Spend under a reservation is bounded by its
    headroom and uncertain calls are priced at the reservation price, so the full reserved amount
    covers it. The remaining work then needs a fresh `reserve_api`."""
    settled = []
    with _exclusive(ledger) as (f, entries):
        for rid, s in sorted(reservation_states(entries).items()):
            if s["exp"] == exp and s["note"] == note and not s["closed"] and s["submitted"]:
                delta = round(max(0.0, s["reserve_usd"] - s["charges"]), 6)
                why = f"{note} (recovered after a crash: settled at full reservation)"
                _append(f, _op_row(s, "close", delta, why))
                settled.append(rid)
    return settled


def reservation_headroom(reservation_id: str, exp: str, ledger: str | Path | None = None) -> float:
    """Remaining allocation of an open reservation: max(0, reserve_usd - charges). Callers bound
    every attempt, retries included, by it, pricing calls of uncertain outcome at the reservation
    price, so the full reserve covers a dead process's spend. Raises `ReservationError` unless
    the reservation exists, belongs to `exp` and is open."""
    s = _require_open(reservation_states(read_entries(ledger)), reservation_id, exp)
    return round(max(0.0, s["reserve_usd"] - s["charges"]), 6)


def open_reservations(exp: str | None = None, ledger: str | Path | None = None) -> dict[str, dict]:
    """{reservation_id: {exp, reserve_usd, charges, liability, note, submitted, utc}} for every
    open reservation (of `exp`, or of every experiment when None)."""
    keys = ("exp", "reserve_usd", "charges", "liability", "note", "submitted", "utc")
    return {
        rid: {k: s[k] for k in keys}
        for rid, s in sorted(
            reservation_states(read_entries(ledger)).items(), key=lambda x: x[1]["utc"]
        )
        if not s["closed"] and (exp is None or s["exp"] == exp)
    }


def find_open(exp: str, note: str, ledger: str | Path | None = None) -> str | None:
    """The open, never-submitted reservation of `exp` for batch `note` (reusable or releasable),
    or None. A submitted one is not returned: settle it with `recover_uncertain`."""
    hits = [
        rid
        for rid, s in reservation_states(read_entries(ledger)).items()
        if s["exp"] == exp and s["note"] == note and not s["closed"] and not s["submitted"]
    ]
    if len(hits) > 1:
        raise ReservationError(f"{exp}: {len(hits)} open reservations for {note!r}: {hits}")
    return hits[0] if hits else None


# --- guards -----------------------------------------------------------------------------------


def totals(exp: str = EXP, ledger: str | Path | None = None) -> dict:
    """`gpu_usd` and `judge_usd` are recorded charges; `judge_reserved_usd` is the open API
    reservations' liability, which `total_usd` and `watchdog_kill_usd` include."""
    c = caps_for(exp)
    entries = read_entries(ledger)
    states = reservation_states(entries)
    gpu = _sum(entries, exp, "gpu")
    judge = _sum(entries, exp, "judge")
    reserved = _open_liability(states, exp)
    return {
        "exp": exp,
        "ledger": str(ledger_path(ledger)),
        "gpu_usd": gpu,
        "judge_usd": judge,
        "judge_reserved_usd": reserved,
        "open_reservations": sum(1 for s in states.values() if s["exp"] == exp and not s["closed"]),
        "total_usd": round(gpu + judge + reserved, 6),
        "hard_cap_usd": c.hard_cap_usd,
        "gpu_limit_usd": c.gpu_limit_usd,
        "judge_limit_usd": c.judge_limit(gpu),
        "reserve_usd": c.reserve_usd,
        "watchdog_kill_usd": _kill_usd(c, entries, states, exp),
    }


def _stage_spend(
    c: Caps, entries: list[dict], states: dict[str, dict], exp: str, group: str,
    exclude: str | None = None,
) -> tuple[float, float]:
    """(recorded, open reserved) dollars of `exp` in stage group `group`."""
    stages = {t for t, g in (c.stage_group or {}).items() if g == group}
    rec = round(sum(
        float(e.get("usd", 0.0)) for e in entries
        if e.get("exp") == exp and e.get("stage") in stages
    ), 6)
    res = round(sum(
        s["liability"] for rid, s in states.items()
        if s["exp"] == exp and not s["closed"] and s.get("stage") in stages and rid != exclude
    ), 6)
    return rec, res


def stage_totals(exp: str, ledger: str | Path | None = None) -> dict[str, dict]:
    """Per stage group: recorded, reserved and limit (empty for experiments without stages)."""
    c = caps_for(exp)
    if not c.stage_limits:
        return {}
    entries = read_entries(ledger)
    states = reservation_states(entries)
    out = {}
    for g, lim in c.stage_limits.items():
        rec, res = _stage_spend(c, entries, states, exp, g)
        out[g] = {"recorded_usd": rec, "reserved_usd": res, "limit_usd": lim}
    return out


def _kill_usd(c: Caps, entries: list[dict], states: dict[str, dict], exp: str) -> float:
    judge = _sum(entries, exp, "judge") + _open_liability(states, exp)
    return round(min(c.gpu_limit_usd, c.hard_cap_usd - c.reserve_usd - judge), 6)


def watchdog_kill_usd(exp: str = EXP, ledger: str | Path | None = None) -> float:
    """GPU dollars (spent + running) at which the pod watchdog must kill:
    min(GPU limit, hard cap - reserve - (judges recorded + open API reservations))."""
    entries = read_entries(ledger)
    return _kill_usd(caps_for(exp), entries, reservation_states(entries), exp)


def _check(
    entries: list[dict],
    states: dict[str, dict],
    projected_usd: float,
    kind: str,
    running_pod_usd: float,
    exp: str,
    reservation_id: str | None = None,
    stage: str | None = None,
) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    stage = _stage(exp, stage)
    if not (math.isfinite(projected_usd) and math.isfinite(running_pod_usd)):
        raise ValueError("projected_usd and running_pod_usd must be finite numbers")
    if projected_usd < 0 or running_pod_usd < 0:
        raise ValueError("projected_usd and running_pod_usd must be >= 0")
    c = caps_for(exp)
    gpu_spent = _sum(entries, exp, "gpu")
    judge_spent = _sum(entries, exp, "judge")
    reserved = _open_liability(states, exp, exclude=reservation_id)
    gpu_after = gpu_spent + running_pod_usd + (projected_usd if kind == "gpu" else 0.0)
    judge_after = judge_spent + reserved + (projected_usd if kind == "judge" else 0.0)
    total_after = gpu_after + judge_after
    judge_limit = c.judge_limit(gpu_after)
    b = {
        "exp": exp,
        "kind": kind,
        "gpu_spent_usd": gpu_spent,
        "running_pod_usd": running_pod_usd,
        "judge_spent_usd": judge_spent,
        "judge_reserved_usd": reserved,
        "projected_usd": projected_usd,
        "gpu_after_usd": round(gpu_after, 6),
        "judge_after_usd": round(judge_after, 6),
        "total_after_usd": round(total_after, 6),
        "hard_cap_usd": c.hard_cap_usd,
        "gpu_limit_usd": c.gpu_limit_usd,
        "judge_limit_usd": judge_limit,
        "reserve_usd": c.reserve_usd,
    }
    if reservation_id is not None:
        b["reservation_id"] = reservation_id
    fails = []
    if total_after > c.hard_cap_usd:
        fails.append(f"total ${total_after:.2f} > hard cap ${c.hard_cap_usd:.2f}")
    if gpu_after > c.gpu_limit_usd:
        fails.append(f"GPU ${gpu_after:.2f} > GPU limit ${c.gpu_limit_usd:.2f}")
    if judge_after > judge_limit:
        fails.append(f"judges ${judge_after:.2f} > judge limit ${judge_limit:.2f}")
    if c.stage_limits and stage is not None:
        group = (c.stage_group or {})[stage]
        rec, res = _stage_spend(c, entries, states, exp, group, exclude=reservation_id)
        stage_after = rec + res + running_pod_usd + projected_usd
        limit = c.stage_limits[group]
        b |= {"stage": stage, "stage_group": group, "stage_after_usd": round(stage_after, 6),
              "stage_limit_usd": limit}
        if stage_after > limit + 1e-9:
            fails.append(f"stage {group} ${stage_after:.2f} > stage limit ${limit:.2f}")
    b["ok"] = not fails
    if fails:
        msg = f"[spend guard] {exp} {kind} batch of ${projected_usd:.4f} refused: " + "; ".join(
            fails
        )
        print(msg, file=sys.stderr)
        raise BudgetExceeded(msg, b)
    return b


def check_total(
    projected_usd: float,
    kind: str,
    running_pod_usd: float = 0.0,
    exp: str = EXP,
    ledger: str | Path | None = None,
    reservation_id: str | None = None,
    stage: str | None = None,
) -> dict:
    """The combined guard. Returns the breakdown if within every limit, else raises
    `BudgetExceeded` (exit code 2). `projected_usd` is the batch or GPU session about to start;
    `running_pod_usd` is the projected cost of pods already running (not yet in the ledger).
    Open API reservations of `exp` count as judge spend. A caller working under its own
    reservation passes `reservation_id` so that reservation's liability is not counted on top of
    its `projected_usd` (it must be an open judge reservation of `exp`)."""
    entries = read_entries(ledger)
    states = reservation_states(entries)
    if reservation_id is not None:
        if kind != "judge":
            raise ReservationError("reservation_id applies to judge (API) batches only")
        _require_open(states, reservation_id, exp)
    return _check(
        entries, states, projected_usd, kind, running_pod_usd, exp, reservation_id, stage
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.spend", description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true", help="print totals as JSON")
    ap.add_argument("--check", type=float, metavar="USD", help="projected batch cost to check")
    ap.add_argument("--reservations", action="store_true", help="list open API reservations")
    ap.add_argument("--kind", choices=KINDS, default="judge")
    ap.add_argument("--running-pod-usd", type=float, default=0.0)
    ap.add_argument("--stage", default=None, help="stage tag (required for E9)")
    ap.add_argument(
        "--exp", default=None, help=f"experiment (default {EXP}; all for --reservations)"
    )
    ap.add_argument("--ledger", default=None)
    args = ap.parse_args(argv)
    if args.reservations:
        print(json.dumps(open_reservations(args.exp, args.ledger), indent=2))
        if args.check is None and not args.report:
            return 0
    exp = args.exp or EXP
    if args.check is None and not args.report:
        args.report = True
    if args.report:
        print(json.dumps(totals(exp, args.ledger) | {"stages": stage_totals(exp, args.ledger)},
                         indent=2))
    if args.check is not None:
        try:
            b = check_total(args.check, args.kind, args.running_pod_usd, exp, args.ledger,
                            stage=args.stage)
        except BudgetExceeded as e:
            print(json.dumps(e.breakdown, indent=2))
            return 2
        print(json.dumps(b, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
