"""Shared RunPod reservations: one locked, append-only file for every checkout (E3, E9, later).

Spec: docs/experiments/shared/runpod-reservations.md (the launch rule, $10 buffer, each dollar
counted once); E3 prereg §8.6; E3 implementation plan v2 fix 5, v3 fixes 4-5 and v4 fixes 2-3
(the `creating` row, `unarmed_overdue`, `rearm_target`; automatic re-arms never extend). The
locking pattern is `emx.spend.record`'s (an exclusive `fcntl.flock` around read-check-append, one
complete fsynced line per row). The balance query follows the GraphQL call of the 2026-10-03
killswitch live check (`myself { clientBalance ... }` with `emx.runpod.headers`, so the
browser-like User-Agent RunPod's Cloudflare front requires is always sent; CLAUDE.md gotcha).

The file is `RESERVATIONS_FILE` in the MAIN checkout, so every worktree shares it; the env
variable `EMX_RUNPOD_RESERVATIONS` overrides it (tests always do). Rows are JSON objects with
`{id, utc, exp, kind, usd, pod_ids, deadline_utc, note, reservation_id, ...}`:

- `reserve`: money admitted by the launch rule (`id == reservation_id`). A reserve row with
  `extends_pod` is an extension of a launched pod's deadline (`extend`); it is converted at once.
- `creating`: written by pod_up BEFORE the create request: `t0_epoch` (taken just before the
  request; all deadlines are anchored to it, so creation and SSH delays come out of the budget),
  slot, intended pod name, budget and rate. Only the latest one of an unlaunched reservation is
  live. A crashed launcher leaves it behind for `unarmed_overdue`.
- `launch`: converts a reservation into pods `[{pod_id, rate_usd_h, budget_usd,
  requested_deadline_epoch}]`, with sum(budgets) <= the reservation. From then on the money counts
  only as the pods' liability, never also as an open reservation.
- `armed`: the killswitch's acknowledged deadline for one pod (`ack`). It may only be at or
  before the pod's allowed deadline; an earlier one shortens it for good (a release).
- `release`: unused money. On an unlaunched reservation it closes it (or reduces it by `usd`);
  on a launched one it is bookkeeping only (pods keep their liability until settled).
- `settle`: actual GPU dollars of a terminated pod (from pod_down), closing its liability.
- `reprice`: a later API read (pod_up's SSH polling) found the pod billing at a higher
  `rate_usd_h` than its launch row used; the pod's rate rises to it and its requested and allowed
  deadlines shorten to `t0 + funded / rate` (funded = budget + extensions), never lengthen. Written
  under the lock before the pod is armed (E3 code review A r1 finding 7).

Liability, at time `now`: an open unlaunched reservation counts its full (remaining) `usd`; an
unsettled launched pod counts its full budget until its first `armed` row, then
`rate x (max(allowed deadline, armed deadline) - now)`, floored at 0 (the armed deadline only
exceeds the allowed one after a `reprice` of an armed pod, until it is re-armed); settled pods and
closed reservations count 0. Settled spend is not subtracted again, because the live balance
already reflects it.

`unsettled_pods(exp)` lists, from durable history only (this file and logs/pods_<EXP>.jsonl,
never .env), every pod launched under a reservation without a settle row, so a cleanup can finish
its charge and settlement after the slot's .env keys are gone (review A r1 finding 4;
`scripts/pod_down.py --pod-id ID --reservation RID`).

The launch rule (`admit`): balance - sum(unlaunched reservations of ALL experiments, own included,
except `replacing`) - sum(launched pods' liability) - BUFFER_USD >= usd.

CLI: python -m emx.runpod_reservations
     {state,balance,admit,reserve,ack,release,settle,snapshot,unsettled}
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MAIN_CHECKOUT = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory")
RESERVATIONS_FILE = str(MAIN_CHECKOUT / "logs" / "runpod_reservations.jsonl")
RESERVATIONS_ENV = "EMX_RUNPOD_RESERVATIONS"
GRAPHQL_URL = "https://api.runpod.io/graphql"
BALANCE_QUERY = (
    "query { myself { clientBalance currentSpendPerHr pods { id name desiredStatus costPerHr } } }"
)
BUFFER_USD = 10.0
ACK_TIMEOUT_S = 900  # a pod without an acknowledged arm 15 min after t0 is cleaned up (v3/v4)
KINDS = ("reserve", "creating", "launch", "armed", "release", "settle", "reprice")
EPS = 1e-6


class NotAdmitted(RuntimeError):
    """The launch rule refused; `detail` is the admission breakdown."""

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


class ReservationError(ValueError):
    """A call that breaks the protocol (unknown or closed reservation, unknown pod, an ack later
    than allowed, budgets above the reservation, ...)."""


# --- file and lock -----------------------------------------------------------------------------


def path() -> Path:
    return Path(os.environ.get(RESERVATIONS_ENV) or RESERVATIONS_FILE)


def _utc(epoch: float | None = None) -> str:
    t = datetime.now(UTC) if epoch is None else datetime.fromtimestamp(epoch, UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str, p: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{p}:{i}: malformed reservation line: {e}") from e
    return rows


@contextmanager
def _locked():
    """Exclusive lock on the file; yields (append handle, rows read inside the lock)."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            yield f, _parse(f.read(), p)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_rows() -> list[dict]:
    p = path()
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            text = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return _parse(text, p)


def _row(kind: str, exp: str, reservation_id: str, usd: float | None = 0.0, **extra) -> dict:
    assert kind in KINDS, kind
    rid = uuid.uuid4().hex
    row = {
        "id": reservation_id if kind == "reserve" else rid,
        "utc": _utc(),
        "exp": exp,
        "kind": kind,
        "usd": None if usd is None else round(float(usd), 6),
        "pod_ids": [],
        "deadline_utc": None,
        "note": "",
        "reservation_id": reservation_id,
    }
    row.update(extra)
    return row


def _append(f, row: dict) -> dict:
    f.write(json.dumps(row) + "\n")
    f.flush()
    os.fsync(f.fileno())
    return row


def _usd(x: float, what: str = "usd") -> float:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x) or x < 0:
        raise ValueError(f"{what} must be a non-negative finite number, got {x!r}")
    return float(x)


# --- reduction: the state is a pure function of the rows ----------------------------------------


def _reduce(rows: Iterable[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(reservations by id, pods by id), from the rows in file (append) order."""
    res: dict[str, dict] = {}
    pods: dict[str, dict] = {}
    for r in rows:
        kind, rid = r.get("kind"), r.get("reservation_id")
        if kind == "reserve":
            ext = r.get("extends_pod")
            res[rid] = {
                "id": rid,
                "exp": r["exp"],
                "usd": float(r["usd"]),
                "open_usd": float(r["usd"]),
                "note": r.get("note", ""),
                "utc": r.get("utc"),
                "launched": bool(ext),
                "closed": False,
                "released": False,
                "pods": [ext] if ext else [],
                "creating": None,
                "settled_usd": 0.0,
                "extends_pod": ext,
            }
            if ext and ext in pods:
                pods[ext]["allowed"] = float(r["deadline_epoch"])
                pods[ext]["extensions"].append(rid)
            continue
        R = res.get(rid)
        if R is None:
            continue  # a row for an unknown reservation (never written by this module)
        if kind == "creating":
            R["creating"] = r
        elif kind == "launch":
            R["launched"] = True
            for spec in r.get("pods", []):
                pid = spec["pod_id"]
                R["pods"].append(pid)
                pods[pid] = {
                    "pod_id": pid,
                    "reservation_id": rid,
                    "exp": R["exp"],
                    "rate_usd_h": float(spec["rate_usd_h"]),
                    "budget_usd": float(spec["budget_usd"]),
                    "t0_epoch": float(r["t0_epoch"]),
                    "requested": float(spec["requested_deadline_epoch"]),
                    "allowed": float(spec["requested_deadline_epoch"]),
                    "acked": None,
                    "settled": False,
                    "settled_usd": None,
                    "slot": r.get("slot"),
                    "name": r.get("name"),
                    "extensions": [],
                }
        elif kind == "reprice":
            for pid in r.get("pod_ids", []):
                if pid in pods and pods[pid]["reservation_id"] == rid:
                    p = pods[pid]
                    d = float(r["deadline_epoch"])
                    p["rate_usd_h"] = max(p["rate_usd_h"], float(r["rate_usd_h"]))
                    p["requested"] = min(p["requested"], d)
                    p["allowed"] = min(p["allowed"], d)
        elif kind == "armed":
            for pid in r.get("pod_ids", []):
                if pid in pods:
                    d = float(r["deadline_epoch"])
                    pods[pid]["acked"] = d
                    pods[pid]["allowed"] = min(pods[pid]["allowed"], d)
        elif kind == "release":
            if R["launched"]:
                R["released"] = True
            else:
                amount = R["open_usd"] if r.get("usd") is None else float(r["usd"])
                R["open_usd"] = max(0.0, R["open_usd"] - amount)
                R["released"] = True
                if R["open_usd"] <= EPS:
                    R["closed"] = True
        elif kind == "settle":
            targets = r.get("pod_ids") or []
            known = [p for p in targets if p in pods and pods[p]["reservation_id"] == rid]
            R["settled_usd"] += float(r.get("usd") or 0.0)
            if not known:  # whole reservation (or a pod it never launched): close it
                for pid in R["pods"]:
                    if pid in pods:
                        pods[pid]["settled"] = True
                R["closed"] = True
                R["open_usd"] = 0.0
            for pid in known:
                pods[pid]["settled"] = True
                pods[pid]["settled_usd"] = float(r.get("usd") or 0.0)
            own = [p for p in R["pods"] if p in pods and pods[p]["reservation_id"] == rid]
            if own and all(pods[p]["settled"] for p in own):
                R["closed"] = True
    # an extension reservation closes with its pod
    for R in res.values():
        if R["extends_pod"] and pods.get(R["extends_pod"], {}).get("settled"):
            R["closed"] = True
    return res, pods


def _pod_liability(p: dict, now: float) -> float:
    if p["settled"]:
        return 0.0
    if p["acked"] is None:
        return p["budget_usd"]  # until acknowledged (extend() requires an ack first)
    # the pod-side stop is the armed deadline: after a `reprice` it may be later than `allowed`
    # until the pod is re-armed, so count the later of the two
    return p["rate_usd_h"] * max(0.0, max(p["allowed"], p["acked"]) - now) / 3600.0


def _totals(res: dict, pods: dict, now: float, replacing: str | None = None) -> dict:
    unlaunched = sum(
        (
            R["open_usd"]
            for R in res.values()
            if not R["launched"] and not R["closed"] and R["id"] != replacing
        ),
        0.0,
    )
    launched = sum((_pod_liability(p, now) for p in pods.values()), 0.0)
    return {"unlaunched_usd": round(unlaunched, 6), "launched_liability_usd": round(launched, 6)}


def _admission(
    res: dict, pods: dict, usd: float, balance: float, now: float, replacing: str | None
) -> tuple[bool, dict]:
    t = _totals(res, pods, now, replacing)
    available = balance - t["unlaunched_usd"] - t["launched_liability_usd"] - BUFFER_USD
    detail = {
        "balance_usd": round(float(balance), 6),
        **t,
        "buffer_usd": BUFFER_USD,
        "available_usd": round(available, 6),
        "requested_usd": round(float(usd), 6),
        "replacing": replacing,
    }
    detail["ok"] = available + EPS >= usd
    return detail["ok"], detail


# --- balance (free GraphQL read) --------------------------------------------------------------


def api_key() -> str:
    key = os.environ.get("RUNPOD_API_KEY")
    if key:
        return key
    from dotenv import dotenv_values

    return dotenv_values(MAIN_CHECKOUT / ".env").get("RUNPOD_API_KEY") or ""


def account(session=None, key: str | None = None) -> dict:
    """`myself { clientBalance currentSpendPerHr pods {...} }` (read-only, free)."""
    import requests

    from emx.runpod import headers

    key = key or api_key()
    if not key:
        raise RuntimeError("RUNPOD_API_KEY not set (env or the main checkout's .env)")
    s = session or requests
    r = s.post(GRAPHQL_URL, headers=headers(key), json={"query": BALANCE_QUERY}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"RunPod GraphQL errors: {body['errors']}")
    return body["data"]["myself"]


def balance(session=None, key: str | None = None) -> float:
    return float(account(session, key)["clientBalance"])


# --- the protocol ------------------------------------------------------------------------------


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def admit(
    exp: str,
    usd: float,
    *,
    replacing: str | None = None,
    balance: float | None = None,
    now: float | None = None,
) -> tuple[bool, dict]:
    """Would a reservation of `usd` for `exp` pass the launch rule right now? (Read only.)"""
    _usd(usd)
    bal = globals()["balance"]() if balance is None else float(balance)
    res, pods = _reduce(read_rows())
    ok, detail = _admission(res, pods, usd, bal, _now(now), replacing)
    detail["exp"] = exp
    return ok, detail


def reserve(
    exp: str,
    usd: float,
    note: str = "",
    *,
    replacing: str | None = None,
    balance: float | None = None,
    now: float | None = None,
) -> str:
    """Admit and append a reservation in one critical section; returns its id. With `replacing`,
    that open unlaunched reservation is excluded from the sum and released in the same section.
    The balance is read before the lock: it reflects billing only, never reservations, which are
    read inside the lock."""
    usd = _usd(usd)
    bal = globals()["balance"]() if balance is None else float(balance)
    with _locked() as (f, rows):
        res, pods = _reduce(rows)
        if replacing is not None:
            R = res.get(replacing)
            if R is None or R["closed"] or R["launched"] or R["exp"] != exp:
                raise ReservationError(
                    f"cannot replace {replacing}: not an open unlaunched {exp} reservation"
                )
        ok, detail = _admission(res, pods, usd, bal, _now(now), replacing)
        detail["exp"] = exp
        if not ok:
            raise NotAdmitted(
                f"{exp} reservation of ${usd:.2f} not admitted: available "
                f"${detail['available_usd']:.2f}",
                detail,
            )
        rid = uuid.uuid4().hex
        _append(f, _row("reserve", exp, rid, usd, note=note, admission=detail))
        if replacing is not None:
            _append(f, _row("release", exp, replacing, None, note=f"replaced by {rid}"))
        return rid


def wait_and_reserve(
    exp: str,
    usd: float,
    note: str = "",
    *,
    give_up_at: float,
    poll_s: float = 300.0,
    replacing: str | None = None,
    balance_fn: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    log: Callable[[str], None] | None = None,
) -> str:
    """Re-check every `poll_s` seconds until admitted; log each wait; raise NotAdmitted once the
    next check would fall after `give_up_at` (epoch)."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    balance_fn = balance_fn or balance
    while True:
        try:
            return reserve(exp, usd, note, replacing=replacing, balance=balance_fn())
        except NotAdmitted as e:
            t = clock()
            d = e.detail
            if t + poll_s > give_up_at:
                log(f"{_utc(t)} {exp}: reservation ${usd:.2f} still not admitted; giving up")
                raise
            log(
                f"{_utc(t)} {exp}: waiting for ${usd:.2f} (balance ${d['balance_usd']:.2f}, "
                f"unlaunched ${d['unlaunched_usd']:.2f}, pods ${d['launched_liability_usd']:.2f},"
                f" buffer ${d['buffer_usd']:.0f}); re-check in {poll_s:.0f} s"
            )
            sleep(poll_s)


def _open_unlaunched(res: dict, rid: str) -> dict:
    R = res.get(rid)
    if R is None:
        raise ReservationError(f"unknown reservation {rid}")
    if R["closed"] or R["launched"]:
        raise ReservationError(f"reservation {rid} is not open and unlaunched")
    return R


def get(reservation_id: str) -> dict | None:
    """The reduced state of one reservation (None if unknown)."""
    res, _ = _reduce(read_rows())
    return res.get(reservation_id)


def creating(
    reservation_id: str,
    *,
    slot: str,
    name: str,
    budget_usd: float,
    rate_usd_h: float,
    t0_epoch: float,
) -> dict:
    """pod_up's durable intent row, appended BEFORE the create request (v4 fix 2)."""
    _usd(budget_usd, "budget_usd")
    if not rate_usd_h > 0:
        raise ValueError(f"rate_usd_h must be > 0, got {rate_usd_h!r}")
    with _locked() as (f, rows):
        res, _ = _reduce(rows)
        R = _open_unlaunched(res, reservation_id)
        if budget_usd > R["open_usd"] + EPS:
            raise ReservationError(
                f"budget ${budget_usd:.2f} exceeds reservation {reservation_id} "
                f"(${R['open_usd']:.2f})"
            )
        budget_deadline = t0_epoch + budget_usd / rate_usd_h * 3600
        return _append(
            f,
            _row(
                "creating",
                R["exp"],
                reservation_id,
                budget_usd,
                t0_epoch=float(t0_epoch),
                slot=slot,
                name=name,
                budget_usd=float(budget_usd),
                rate_usd_h=float(rate_usd_h),
                deadline_utc=_utc(budget_deadline),
                deadline_epoch=int(budget_deadline),
            ),
        )


def launch(
    reservation_id: str,
    pods: list[tuple[str, float, float]],
    t0_epoch: float,
    *,
    global_deadline: float | dict[str, float] | None = None,
    slot: str | None = None,
    name: str | None = None,
) -> dict[str, int]:
    """Convert a reservation into pods `(pod_id, usd_per_h, budget_usd)`. Each pod's requested
    deadline is `t0 + budget / rate` (t0 taken BEFORE the create request), capped at the
    experiment's global kill-line deadline. Asserts sum(budgets) <= the reservation."""
    if not pods:
        raise ReservationError("launch needs at least one pod")
    out: dict[str, int] = {}
    specs = []
    for pid, rate, budget in pods:
        if not (rate > 0 and math.isfinite(rate)):
            raise ValueError(f"pod {pid}: rate must be > 0, got {rate!r}")
        _usd(budget, "budget_usd")
        d = t0_epoch + budget / rate * 3600
        g = global_deadline.get(pid) if isinstance(global_deadline, dict) else global_deadline
        if g is not None:
            d = min(d, float(g))
        out[pid] = math.floor(d)
        specs.append(
            {
                "pod_id": pid,
                "rate_usd_h": float(rate),
                "budget_usd": float(budget),
                "requested_deadline_epoch": out[pid],
            }
        )
    with _locked() as (f, rows):
        res, known = _reduce(rows)
        R = _open_unlaunched(res, reservation_id)
        total = sum(s["budget_usd"] for s in specs)
        if total > R["open_usd"] + EPS:
            raise ReservationError(
                f"pod budgets ${total:.2f} exceed reservation {reservation_id} "
                f"(${R['open_usd']:.2f})"
            )
        dup = [s["pod_id"] for s in specs if s["pod_id"] in known]
        if dup:
            raise ReservationError(f"pod(s) {dup} already launched")
        c = R["creating"] or {}
        _append(
            f,
            _row(
                "launch",
                R["exp"],
                reservation_id,
                total,
                pod_ids=[s["pod_id"] for s in specs],
                pods=specs,
                t0_epoch=float(t0_epoch),
                deadline_utc=_utc(max(out.values())),
                creating_id=c.get("id"),
                slot=slot or c.get("slot"),
                name=name or c.get("name"),
            ),
        )
    return out


def _live_pod(pods: dict, pod_id: str) -> dict:
    p = pods.get(pod_id)
    if p is None:
        raise ReservationError(f"unknown pod {pod_id} (no launch row)")
    if p["settled"]:
        raise ReservationError(f"pod {pod_id} is already settled")
    return p


def ack(pod_id: str, deadline_epoch: float) -> dict:
    """Record the killswitch's acknowledged deadline. Refused if later than the pod's currently
    allowed deadline (requested, lowered by every earlier ack, raised only by `extend`)."""
    with _locked() as (f, rows):
        _, pods = _reduce(rows)
        p = _live_pod(pods, pod_id)
        d = int(deadline_epoch)
        if d > p["allowed"]:
            raise ReservationError(
                f"ack for {pod_id} at {d} is later than its allowed deadline {int(p['allowed'])}"
                " (extensions go through extend())"
            )
        return _append(
            f,
            _row(
                "armed",
                p["exp"],
                p["reservation_id"],
                0.0,
                pod_ids=[pod_id],
                deadline_epoch=d,
                deadline_utc=_utc(d),
            ),
        )


def reprice(pod_id: str, rate_usd_h: float, note: str = "") -> dict | None:
    """A later API read found `pod_id` billing at `rate_usd_h`. If that is higher than the rate
    its launch used, raise the pod's rate and shorten its requested and allowed deadlines to
    `t0 + funded / rate` (funded = budget + its extensions' dollars), all in one locked append,
    so the shared liability reflects it before the pod is armed (E3 code review A r1 finding 7).
    Never lengthens anything: a rate at or below the current one, or a settled pod, appends
    nothing and returns None. An unknown pod raises ReservationError."""
    rate = float(rate_usd_h)
    if not (rate > 0 and math.isfinite(rate)):
        raise ValueError(f"rate_usd_h must be > 0, got {rate_usd_h!r}")
    with _locked() as (f, rows):
        res, pods = _reduce(rows)
        p = pods.get(pod_id)
        if p is None:
            raise ReservationError(f"unknown pod {pod_id} (no launch row)")
        if p["settled"] or rate <= p["rate_usd_h"] + EPS:
            return None
        funded = p["budget_usd"] + sum(res[x]["usd"] for x in p["extensions"] if x in res)
        d = min(math.floor(p["t0_epoch"] + funded / rate * 3600), math.floor(p["allowed"]))
        return _append(
            f,
            _row(
                "reprice",
                p["exp"],
                p["reservation_id"],
                0.0,
                pod_ids=[pod_id],
                rate_usd_h=rate,
                previous_rate_usd_h=p["rate_usd_h"],
                deadline_epoch=int(d),
                deadline_utc=_utc(d),
                note=note,
            ),
        )


def extend(
    pod_id: str,
    new_deadline: float,
    *,
    balance: float | None = None,
    now: float | None = None,
    note: str = "",
) -> str:
    """The only path to a later deadline: a new reservation for the increase
    (rate x added time), admitted by the launch rule under the lock. Returns its id. The caller
    then arms `new_deadline` on the pod and records it with `ack`."""
    bal = globals()["balance"]() if balance is None else float(balance)
    t = _now(now)
    with _locked() as (f, rows):
        res, pods = _reduce(rows)
        p = _live_pod(pods, pod_id)
        if p["acked"] is None:
            raise ReservationError(f"pod {pod_id} has no acknowledged deadline yet; ack first")
        new = int(new_deadline)
        if new <= p["allowed"]:
            raise ReservationError(
                f"{new} is not later than {pod_id}'s allowed deadline {int(p['allowed'])}; "
                "shorten with ack()"
            )
        increase = p["rate_usd_h"] * (new - max(p["allowed"], t)) / 3600
        ok, detail = _admission(res, pods, increase, bal, t, None)
        detail["exp"] = p["exp"]
        if not ok:
            raise NotAdmitted(f"extension of {pod_id} by ${increase:.2f} not admitted", detail)
        rid = uuid.uuid4().hex
        _append(
            f,
            _row(
                "reserve",
                p["exp"],
                rid,
                increase,
                note=note or f"extend {pod_id}",
                extends_pod=pod_id,
                pod_ids=[pod_id],
                deadline_epoch=new,
                deadline_utc=_utc(new),
                admission=detail,
            ),
        )
        return rid


def release(reservation_id: str, usd: float | None = None, note: str = "") -> dict | None:
    """Return unused money. Unlaunched: closes the reservation (or reduces it by `usd`).
    Launched: bookkeeping of the unused part (default: reservation - settled actual); pods keep
    their liability until settled. Idempotent: a closed or already-released reservation is a
    no-op (returns None)."""
    if usd is not None:
        _usd(usd)
    with _locked() as (f, rows):
        res, _ = _reduce(rows)
        R = res.get(reservation_id)
        if R is None:
            raise ReservationError(f"unknown reservation {reservation_id}")
        if R["launched"]:
            if R["released"]:
                return None
            unused = max(0.0, R["usd"] - R["settled_usd"]) if usd is None else usd
            return _append(f, _row("release", R["exp"], reservation_id, unused, note=note))
        if R["closed"]:
            return None
        return _append(f, _row("release", R["exp"], reservation_id, usd, note=note))


def settle(reservation_id: str, usd: float, pod_id: str | None = None, note: str = "") -> dict:
    """Actual GPU dollars of a terminated pod (from pod_down); closes that pod's liability, and
    the reservation once all its pods are settled. Idempotent: settling an already settled pod
    (or reservation) appends nothing and returns the earlier row."""
    usd = _usd(usd)
    with _locked() as (f, rows):
        res, pods = _reduce(rows)
        R = res.get(reservation_id)
        if R is None:
            raise ReservationError(f"unknown reservation {reservation_id}")
        prior = [
            r
            for r in rows
            if r.get("kind") == "settle" and r.get("reservation_id") == reservation_id
        ]
        if pod_id is not None and pod_id in R["pods"]:
            if pods[pod_id]["settled"]:
                return next((r for r in prior if pod_id in (r.get("pod_ids") or [])), prior[-1])
        elif pod_id is not None and R["pods"]:
            raise ReservationError(f"pod {pod_id} is not in reservation {reservation_id}")
        elif R["closed"] and prior:
            return prior[-1]
        elif R["closed"]:
            raise ReservationError(f"reservation {reservation_id} was released, not settled")
        return _append(
            f,
            _row(
                "settle",
                R["exp"],
                reservation_id,
                usd,
                pod_ids=[pod_id] if pod_id else [],
                note=note,
            ),
        )


# --- deadlines for arming and recovery ---------------------------------------------------------


def pod_deadline(pod_id: str) -> int | None:
    """The pod's reservation deadline (requested at launch, lowered by every ack, raised only by
    `extend`), or None if no reservation launched it."""
    _, pods = _reduce(read_rows())
    p = pods.get(pod_id)
    return None if p is None else int(p["allowed"])


def acked_deadline(pod_id: str) -> int | None:
    """The deadline of the pod's latest `armed` row (None before the first ack or if unknown)."""
    _, pods = _reduce(read_rows())
    p = pods.get(pod_id)
    return None if p is None or p["acked"] is None else int(p["acked"])


def rearm_target(pod_id: str, global_deadline: float) -> int:
    """What an automatic re-arm may arm: min(reservation deadline, global kill-line deadline,
    current acknowledged deadline). Never later than what is armed now (v4 fix 3); `extend` is
    the only path to a later deadline. A pod without a reservation gets the global deadline."""
    _, pods = _reduce(read_rows())
    p = pods.get(pod_id)
    if p is None:
        return int(global_deadline)
    cands = [p["allowed"], float(global_deadline)]
    if p["acked"] is not None:
        cands.append(p["acked"])
    return int(min(cands))


def unarmed_overdue(now: float | None = None, ack_timeout_s: float = ACK_TIMEOUT_S) -> list[dict]:
    """Every pod (or pending creation) that recovery must clean up now:

    - the live `creating` row of an open unlaunched reservation (no pod id yet: reconcile by
      `name` through the API), once `now` >= min(t0 + ack_timeout_s, t0 + budget / rate);
    - a launched, unsettled pod without an `armed` row, at the same time;
    - a launched, unsettled pod past its allowed deadline (armed or not).
    """
    t = _now(now)
    res, pods = _reduce(read_rows())
    out = []
    for R in res.values():
        c = R["creating"]
        if R["launched"] or R["closed"] or c is None:
            continue
        t0 = float(c["t0_epoch"])
        budget_dl = t0 + float(c["budget_usd"]) / float(c["rate_usd_h"]) * 3600
        due = min(t0 + ack_timeout_s, budget_dl)
        if t >= due:
            out.append(
                {
                    "kind": "creating",
                    "reason": "no pod launched",
                    "reservation_id": R["id"],
                    "exp": R["exp"],
                    "pod_id": None,
                    "slot": c.get("slot"),
                    "name": c.get("name"),
                    "t0_epoch": t0,
                    "budget_usd": float(c["budget_usd"]),
                    "rate_usd_h": float(c["rate_usd_h"]),
                    "due_epoch": int(due),
                }
            )
    for p in pods.values():
        if p["settled"]:
            continue
        due_unarmed = min(p["t0_epoch"] + ack_timeout_s, p["requested"])
        if p["acked"] is None and t >= due_unarmed:
            reason, due = "unarmed", due_unarmed
        elif t >= p["allowed"]:
            reason, due = "past_deadline", p["allowed"]
        else:
            continue
        out.append(
            {
                "kind": "launch",
                "reason": reason,
                "reservation_id": p["reservation_id"],
                "exp": p["exp"],
                "pod_id": p["pod_id"],
                "slot": p["slot"],
                "name": p["name"],
                "t0_epoch": p["t0_epoch"],
                "budget_usd": p["budget_usd"],
                "rate_usd_h": p["rate_usd_h"],
                "due_epoch": int(due),
            }
        )
    return out


def pending_creation(name: str, exp: str | None = None) -> dict | None:
    """The live `creating` row (of an open, unlaunched reservation) for pod name `name`, or None.
    Lets a reaper bill a pod whose launcher died before logging it from its true t0."""
    res, _ = _reduce(read_rows())
    for R in res.values():
        c = R["creating"]
        live = c and not R["launched"] and not R["closed"] and c.get("name") == name
        if live and (exp is None or R["exp"] == exp):
            return c
    return None


def pod(pod_id: str) -> dict | None:
    """The reduced state of one launched pod (None if no launch row names it)."""
    _, pods = _reduce(read_rows())
    return pods.get(pod_id)


def _pod_sessions(exp: str) -> list[dict]:
    """`exp`'s pod sessions from its event log (logs/pods_<EXP>.jsonl under emx.runpod.LOGS)."""
    from emx import runpod as rp

    return rp.pod_sessions(rp.read_pod_events(rp.LOGS / f"pods_{exp}.jsonl"))


def unsettled_pods(exp: str) -> list[dict]:
    """Every pod launched under one of `exp`'s reservations that has no settle row yet, from
    durable history only (this file and logs/pods_<EXP>.jsonl; never .env), so cleanup can finish
    the charge and settlement of a pod whose slot keys are already cleared (E3 code review A r1
    finding 4). Each item: `pod_id, reservation_id, slot, terminated` (the event log closed its
    session), `t0` (epoch), `budget_usd`, `rate` (USD/h), `deadline_epoch` (allowed; None for an
    event-log-only pod) and `source`: "launch" (a launch row) or "event_log" (a pod the reaper
    adopted from a dead launcher's `creating` row: a `created` event carrying `reservation_id`,
    no launch row, its reservation still open and unlaunched). Settle each with
    `scripts/pod_down.py --exp EXP --pod-id ID --reservation RID`."""
    res, pods = _reduce(read_rows())
    sessions = {s["pod_id"]: s for s in _pod_sessions(exp)}
    out = []
    for p in pods.values():
        if p["exp"] != exp or p["settled"]:
            continue
        s = sessions.get(p["pod_id"])
        out.append(
            {
                "pod_id": p["pod_id"],
                "reservation_id": p["reservation_id"],
                "slot": p["slot"] or (s or {}).get("slot") or "",
                "terminated": bool(s and not s["running"]),
                "t0": p["t0_epoch"],
                "budget_usd": p["budget_usd"],
                "rate": p["rate_usd_h"],
                "deadline_epoch": int(p["allowed"]),
                "source": "launch",
            }
        )
    from emx import runpod as rp

    created = [
        e
        for e in rp.read_pod_events(rp.LOGS / f"pods_{exp}.jsonl")
        if e.get("event") == "created" and e.get("reservation_id") and e.get("pod_id")
    ]
    seen = {o["pod_id"] for o in out} | set(pods)
    for e in created:
        pid, rid = e["pod_id"], e["reservation_id"]
        R = res.get(rid)
        if pid in seen or R is None or R["exp"] != exp or R["closed"] or R["launched"]:
            continue
        seen.add(pid)
        s = sessions.get(pid) or {}
        c = R["creating"] or {}
        t0 = c.get("t0_epoch")
        out.append(
            {
                "pod_id": pid,
                "reservation_id": rid,
                "slot": e.get("slot") or c.get("slot") or "",
                "terminated": bool(s) and not s["running"],
                "t0": float(t0) if t0 is not None else datetime.fromisoformat(e["utc"]).timestamp(),
                "budget_usd": float(c.get("budget_usd") or R["open_usd"]),
                "rate": float(s.get("rate") or e.get("rate_usd_h") or 0.0),
                "deadline_epoch": None,
                "source": "event_log",
            }
        )
    return out


def state(now: float | None = None, exp: str | None = None) -> dict:
    """Reduced state (for snapshots and status blocks)."""
    t = _now(now)
    res, pods = _reduce(read_rows())
    tot = _totals(res, pods, t)
    pod_rows = {
        pid: {**p, "liability_usd": round(_pod_liability(p, t), 6)}
        for pid, p in pods.items()
        if exp is None or p["exp"] == exp
    }
    return {
        "file": str(path()),
        "utc": _utc(t),
        "buffer_usd": BUFFER_USD,
        **tot,
        "reservations": {
            k: {kk: vv for kk, vv in v.items() if kk != "creating"}
            | {"creating_t0_epoch": (v["creating"] or {}).get("t0_epoch")}
            for k, v in res.items()
            if exp is None or v["exp"] == exp
        },
        "pods": pod_rows,
    }


def snapshot(exp: str, out: str | Path) -> int:
    """Write `exp`'s raw rows (JSONL) to `out`, for committing with a report. Returns the count."""
    rows = [r for r in read_rows() if r.get("exp") == exp]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return len(rows)


# --- CLI ---------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.runpod_reservations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state")
    s.add_argument("--exp")
    sub.add_parser("balance")
    for name in ("admit", "reserve"):
        s = sub.add_parser(name)
        s.add_argument("--exp", required=True)
        s.add_argument("--usd", type=float, required=True)
        s.add_argument("--replacing")
        s.add_argument("--balance", type=float, help="use this balance (dry runs, tests)")
        if name == "reserve":
            s.add_argument("--note", default="")
    s = sub.add_parser("ack")
    s.add_argument("--pod", required=True)
    s.add_argument("--deadline", type=int, required=True)
    s = sub.add_parser("release")
    s.add_argument("--id", required=True)
    s.add_argument("--usd", type=float)
    s = sub.add_parser("settle")
    s.add_argument("--id", required=True)
    s.add_argument("--usd", type=float, required=True)
    s.add_argument("--pod")
    s = sub.add_parser("snapshot")
    s.add_argument("--exp", required=True)
    s.add_argument("--out", required=True)
    s = sub.add_parser("unsettled")
    s.add_argument("--exp", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "state":
            print(json.dumps(state(exp=a.exp), indent=2, default=str))
        elif a.cmd == "balance":
            print(json.dumps({"clientBalance": balance()}))
        elif a.cmd == "admit":
            ok, d = admit(a.exp, a.usd, replacing=a.replacing, balance=a.balance)
            print(json.dumps(d, indent=2))
            return 0 if ok else 3
        elif a.cmd == "reserve":
            print(reserve(a.exp, a.usd, a.note, replacing=a.replacing, balance=a.balance))
        elif a.cmd == "ack":
            r = ack(a.pod, a.deadline)
            print(json.dumps(r))
        elif a.cmd == "release":
            print(json.dumps(release(a.id, a.usd)))
        elif a.cmd == "settle":
            print(json.dumps(settle(a.id, a.usd, a.pod)))
        elif a.cmd == "unsettled":
            print(json.dumps(unsettled_pods(a.exp), indent=2))
        elif a.cmd == "snapshot":
            n = snapshot(a.exp, a.out)
            print(f"{n} {a.exp} row(s) -> {a.out}")
    except NotAdmitted as e:
        print(f"NOT ADMITTED: {e}\n{json.dumps(e.detail, indent=2)}", file=sys.stderr)
        return 3
    except ReservationError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
