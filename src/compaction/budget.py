"""Sprint spending: atomic reservations against stage caps and the sprint-wide cap.

Specified in docs/sprint/sprint-plan.md, "How every stage runs: Spending is enforced in code";
documented in docs/sprint/prep/j-budget-seal.md. Caps come from docs/sprint/caps.toml.

Every paid call first takes a reservation of its bounded cost (`bounded_cost`: max tokens x price).
`reserve` checks spent + open reservations + the new amount against both the stage cap and the
sprint cap under one `fcntl.flock` on a lock file next to the ledger, so concurrent processes can't
overshoot either cap. When the call completes, `settle` writes the actual cost to the ledger through
`emx.spend.record` (with `exp` = the stage id) and releases the reservation.

One designated checkout (review r1 finding 1): every worktree keeps its own ledger (AGENTS.md), so
the sprint cap holds only if all sprint spending books in one place. The caps file names that
checkout (`sprint.checkout`), and every call refuses (`WrongCheckout`) unless this process's ledger
is `<checkout>/logs/costs.jsonl`.

Files, all in the ledger's directory (`emx.spend.ledger_path()`, so `EMX_COSTS_LEDGER` moves them
together with the ledger):
- `sprint_budget.lock`: the lock file. Every read-modify-write below holds it.
- `sprint_reservations.json`: open reservations, rewritten atomically (temp file, fsync, rename),
  so they survive a crash.
- `sprint_budget_events.jsonl`: append-only log of charge intents, settlements, transfers, expiries,
  cancellations, overruns and reconciliations. Settlements and transfers are read back from it, so it
  is part of the books: commit it with
  the ledger.

Settlement records (review r2 finding 1): the budget event log is authoritative. A charge logs a
`charge_intent` event, writes the ledger row (its note ends with the reserved tag
`[sprint_res=<id>]`), logs the closing event (`settle` or `expire`, with the ledger row's id), and
only then rewrites the state. Caller notes containing `sprint_res=` are refused. Every locked
operation first closes any open reservation that already has a closing event (a crash before the
state write), or whose intent is matched by a ledger row of the same stage, amount and tag (a crash
before the closing event), logging `reconcile`. So a crash neither double-counts nor double-charges,
and no note can close another reservation.
A reservation whose process died stays open (counted against the caps) until `expire` recovers it,
which it does only once the recorded process is shown dead (or with `force` for another host).

This module only uses `emx.spend`'s public API (`record`, `read_entries`, `ledger_path`).
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import socket
import time
import tomllib
import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from emx import spend

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPS = REPO_ROOT / "docs" / "sprint" / "caps.toml"
CAPS_ENV = "SPRINT_CAPS_FILE"  # override for tests

LOCK_NAME = "sprint_budget.lock"
STATE_NAME = "sprint_reservations.json"
EVENTS_NAME = "sprint_budget_events.jsonl"

EPS = 1e-9


class BudgetRefused(RuntimeError):
    """A reservation or transfer that would break a cap or a rule. `breakdown` has the numbers."""

    def __init__(self, message: str, breakdown: dict | None = None):
        super().__init__(message)
        self.breakdown = breakdown or {}


class TopUpRequired(BudgetRefused):
    """The stage or pool can't be used until `topup_recorded = true` in the caps file."""


class WrongCheckout(BudgetRefused):
    """Sprint spending runs only from the checkout named in the caps file (`sprint.checkout`)."""


class ReservationError(ValueError):
    """Unknown or already settled reservation id."""


# --- caps ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StageCap:
    cap_usd: float
    requires_topup: bool = False


@dataclass(frozen=True)
class Pool:
    usd: float
    requires_topup: bool = False


@dataclass(frozen=True)
class Caps:
    core_cap_usd: float
    full_cap_usd: float
    topup_recorded: bool
    transfer_approval_over_usd: float
    checkout: Path
    stages: dict[str, StageCap] = field(default_factory=dict)
    pools: dict[str, Pool] = field(default_factory=dict)

    @property
    def sprint_cap_usd(self) -> float:
        return self.full_cap_usd if self.topup_recorded else self.core_cap_usd


def caps_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(CAPS_ENV)
    return Path(env) if env else DEFAULT_CAPS


def _num(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
        raise ValueError(f"caps file: {what} must be a non-negative number, got {v!r}")
    return float(v)


def load_caps(path: str | Path | None = None) -> Caps:
    """Read the caps file. It is re-read on every reservation, so flipping `topup_recorded`
    takes effect at once."""
    p = caps_path(path)
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    s = raw.get("sprint", {})
    topup = s.get("topup_recorded", False)
    if not isinstance(topup, bool):
        raise TypeError(f"caps file: sprint.topup_recorded must be true or false, got {topup!r}")
    stages = {
        k: StageCap(_num(v["cap_usd"], f"stages.{k}.cap_usd"), bool(v.get("requires_topup", False)))
        for k, v in raw.get("stages", {}).items()
    }
    pools = {
        k: Pool(_num(v["usd"], f"pools.{k}.usd"), bool(v.get("requires_topup", False)))
        for k, v in raw.get("pools", {}).items()
    }
    if not stages:
        raise ValueError(f"caps file {p}: no [stages.*] tables")
    checkout = s.get("checkout")
    if not isinstance(checkout, str) or not Path(checkout).is_absolute():
        raise ValueError(
            f"caps file {p}: sprint.checkout must be an absolute path, got {checkout!r}"
        )
    return Caps(
        core_cap_usd=_num(s["core_cap_usd"], "sprint.core_cap_usd"),
        full_cap_usd=_num(s["full_cap_usd"], "sprint.full_cap_usd"),
        topup_recorded=topup,
        transfer_approval_over_usd=_num(
            s.get("transfer_approval_over_usd", 50.0), "sprint.transfer_approval_over_usd"
        ),
        checkout=Path(checkout),
        stages=stages,
        pools=pools,
    )


# --- files and the lock --------------------------------------------------------------------------


def _paths(ledger: str | Path | None) -> tuple[Path, Path, Path, Path]:
    led = spend.ledger_path(ledger)
    d = led.parent
    return led, d / LOCK_NAME, d / STATE_NAME, d / EVENTS_NAME


def _now() -> float:
    return time.time()


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@contextmanager
def _locked(ledger: str | Path | None, caps_file: str | Path | None) -> Iterator[None]:
    # Check the checkout before creating anything, so a wrong checkout gets no lock file either.
    # (`_books` checks again under the lock, against the caps as read there.)
    require_checkout(load_caps(caps_file), ledger)
    _, lock, _, _ = _paths(ledger)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("reservations", {}))


def _save_state(path: Path, res: dict[str, dict]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"reservations": res}, f, indent=1, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _log(path: Path, event: dict) -> dict:
    ts = _now()
    event = {"utc": _utc(ts), "id": uuid.uuid4().hex, **event}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return event


def read_events(ledger: str | Path | None = None) -> list[dict]:
    _, _, _, ev = _paths(ledger)
    if not ev.exists():
        return []
    out = []
    for i, line in enumerate(ev.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise RuntimeError(f"{ev}:{i}: malformed budget event line: {e}") from e
    return out


MARK = "sprint_res="
CLOSING_OPS = ("settle", "expire", "cancel", "reconcile")


def _tag(rid: str) -> str:
    return f"[{MARK}{rid}]"


def _closed(events: list[dict]) -> dict[str, dict]:
    """reservation id -> its closing event. The event log is authoritative for settlement."""
    return {e["reservation_id"]: e for e in events if e.get("op") in CLOSING_OPS}


def _intents(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("op") == "charge_intent":
            out[e["reservation_id"]].append(e)
    return out


def _crashed_charge(entries: list[dict], rid: str, stage: str, intents: list[dict]) -> dict | None:
    """The ledger row of a charge whose process died before logging its closing event: it must
    follow a logged `charge_intent` for this reservation, book to the reservation's stage, carry
    the amount the intent named, and end with the reservation's tag. Caller notes can't contain
    the tag (they are refused), so a note can't close another reservation (review r2 finding 1)."""
    amounts = {round(float(i["usd"]), 6) for i in intents if i.get("stage") == stage}
    for e in entries:
        if (
            e.get("exp") == stage
            and str(e.get("note", "")).endswith(_tag(rid))
            and round(float(e.get("usd", 0.0)), 6) in amounts
        ):
            return e
    return None


def _reconcile(
    res: dict[str, dict], entries: list[dict], events: list[dict], state: Path, events_path: Path
) -> list[dict]:
    """Close open reservations that the event log already closed (a crash before the state write),
    or whose charge reached the ledger after its intent was logged (a crash before the closing
    event). Returns the events it logs."""
    closed, intents, new = _closed(events), _intents(events), []
    changed = False
    for rid in sorted(res):
        r = res[rid]
        if rid in closed:
            del res[rid]
            changed = True
            continue
        row = _crashed_charge(entries, rid, r["stage"], intents.get(rid, []))
        if row is not None:
            new.append(
                _log(
                    events_path,
                    {
                        "op": "reconcile",
                        "reservation_id": rid,
                        "stage": r["stage"],
                        "reserved_usd": r["max_usd"],
                        "charged_usd": row["usd"],
                        "ledger_id": row.get("id"),
                        "note": "the charge reached the ledger before a crash; reservation closed",
                    },
                )
            )
            del res[rid]
            changed = True
    if changed:
        _save_state(state, res)
    return new


def _charge(events_path: Path, rid: str, r: dict, usd: float, op: str, ledger, **row) -> dict:
    """Log the intent, then write the ledger row through `emx.spend.record`. The caller then logs
    the closing event (with the row's id) and only then rewrites the state."""
    _log(
        events_path,
        {"op": "charge_intent", "reservation_id": rid, "stage": r["stage"], "usd": usd, "for": op},
    )
    note = f"{row.pop('note', '')} {_tag(rid)}".strip()
    return spend.record(
        row.pop("kind", "judge"),
        row.pop("resource", ""),
        row.pop("units", 0),
        usd,
        note=note,
        exp=r["stage"],
        ledger=ledger,
    )


def _check_note(note: str) -> str:
    if MARK in note:
        raise ValueError(f"notes may not contain {MARK!r}: it is reserved for settlement tags")
    return note


@dataclass
class _Books:
    caps: Caps
    res: dict[str, dict]
    entries: list[dict]
    events: list[dict]

    def spent(self, stage: str) -> float:
        return sum(float(e.get("usd", 0.0)) for e in self.entries if e.get("exp") == stage)

    def open_usd(self, stage: str | None = None) -> float:
        return sum(r["max_usd"] for r in self.res.values() if stage is None or r["stage"] == stage)

    def transfers(self) -> list[dict]:
        return [e for e in self.events if e.get("op") == "transfer"]

    def transferred_in(self, stage: str) -> float:
        return sum(e["usd"] for e in self.transfers() if e["stage"] == stage)

    def transferred_out(self, source: str) -> float:
        return sum(e["usd"] for e in self.transfers() if e["source"] == source)

    def pool_transfers_total(self) -> float:
        return sum(e["usd"] for e in self.transfers() if e["source_kind"] == "pool")

    def stage_cap(self, stage: str) -> float:
        return (
            self.caps.stages[stage].cap_usd
            + self.transferred_in(stage)
            - self.transferred_out(stage)
        )

    def sprint_spent(self) -> float:
        return sum(self.spent(s) for s in self.caps.stages)


def require_checkout(caps: Caps, ledger: str | Path | None = None) -> None:
    """Refuse unless this process's ledger is the designated checkout's `logs/costs.jsonl`.

    Each worktree keeps its own ledger (AGENTS.md), and the reservations, transfers and lock live
    next to it, so the sprint cap only holds if every sprint call books in one place (review r1
    finding 1)."""
    here = spend.ledger_path(ledger).resolve()
    want = (caps.checkout / "logs" / "costs.jsonl").resolve()
    if here != want:
        raise WrongCheckout(
            f"sprint spending runs only from the designated checkout {caps.checkout} (caps file "
            f"sprint.checkout); this process books to {here}. Run from that checkout, or change "
            f"sprint.checkout there once no reservation is open.",
            {"ledger": str(here), "designated": str(want)},
        )


def _books(ledger, caps_file, state: Path, events: Path) -> _Books:
    """Read everything under the lock (the caller holds it), reconciling first. Refuses outside
    the designated checkout."""
    caps = load_caps(caps_file)
    require_checkout(caps, ledger)
    res = _load_state(state)
    entries = spend.read_entries(ledger)
    evs = read_events(ledger)
    evs += _reconcile(res, entries, evs, state, events)
    return _Books(caps, res, entries, evs)


def _usd(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
        raise ValueError(f"{what} must be a non-negative finite number, got {v!r}")
    return round(float(v), 6)


def _dec(v, what: str) -> Decimal:
    """A price as an exact Decimal (floats via their shortest repr, so 0.3 stays 0.3)."""
    if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
        raise TypeError(f"{what} must be a number, got {v!r}")
    try:
        d = Decimal(str(v))
    except InvalidOperation as e:
        raise ValueError(f"{what} must be a number, got {v!r}") from e
    if not d.is_finite() or d < 0:
        raise ValueError(f"{what} must be a non-negative finite number, got {v!r}")
    return d


def _ceil6(d: Decimal) -> float:
    """Round up to the next millionth of a dollar."""
    return float(d.quantize(Decimal("0.000001"), rounding=ROUND_CEILING))


def _stage_ok(caps: Caps, stage: str) -> StageCap:
    if stage not in caps.stages:
        raise ValueError(f"unknown sprint stage {stage!r}; known {sorted(caps.stages)}")
    return caps.stages[stage]


# --- public API ----------------------------------------------------------------------------------


def bounded_cost(
    model: str,
    max_input_tokens: int,
    max_output_tokens: int,
    prices: Mapping[str, Mapping[str, float]],
) -> float:
    """Upper bound on one call's cost in USD: the reservation size.

    `prices[model]` gives USD per million tokens: `input` and `output` (required), and optionally
    `cache_write` (input is then priced at max(input, cache_write), the worst case for Anthropic
    cache markers) and `request` (a flat per-call fee in USD). Reasoning tokens bill as output, so
    `max_output_tokens` must be the call's hard output limit including reasoning. Cache reads are
    never assumed: the bound is the uncached price. Exact decimal arithmetic on the prices as
    given; only the total is rounded, up, to the next millionth of a dollar.
    Retries are separate calls: reserve each attempt, or reserve attempts x this bound."""
    if model not in prices:
        raise KeyError(f"no price for model {model!r}; have {sorted(prices)}")
    p = prices[model]
    for k in ("input", "output"):
        if k not in p:
            raise KeyError(f"prices[{model!r}] needs {k!r} (USD per million tokens)")
    for n, what in (
        (max_input_tokens, "max_input_tokens"),
        (max_output_tokens, "max_output_tokens"),
    ):
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"{what} must be a non-negative int, got {n!r}")
    pin = max(_dec(p["input"], "input price"), _dec(p.get("cache_write", 0), "cache_write"))
    pout = _dec(p["output"], "output price")
    fee = _dec(p.get("request", 0), "request fee")
    usd = (max_input_tokens * pin + max_output_tokens * pout) / Decimal(1_000_000) + fee
    return _ceil6(usd)


def reserve(
    stage: str,
    max_usd: float,
    note: str = "",
    *,
    ledger: str | Path | None = None,
    caps_file: str | Path | None = None,
) -> str:
    """Reserve `max_usd` for one paid call (or batch) under `stage`; return the reservation id.

    Under the lock: refuses (`BudgetRefused`) if stage spent + stage open reservations + max_usd
    would exceed the stage cap (plus transfers in), or if sprint spent + all open reservations +
    max_usd would exceed the sprint cap. A stage with `requires_topup` raises `TopUpRequired`
    until the caps file records the top-up."""
    max_usd = _ceil6(_dec(max_usd, "max_usd"))
    _check_note(note)
    led, _, state, events = _paths(ledger)
    with _locked(ledger, caps_file):
        b = _books(ledger, caps_file, state, events)
        sc = _stage_ok(b.caps, stage)
        if sc.requires_topup and not b.caps.topup_recorded:
            raise TopUpRequired(
                f"{stage} can't reserve until the top-up is recorded (caps file "
                f"sprint.topup_recorded = true)",
                {"stage": stage},
            )
        st = {
            "stage": stage,
            "cap": round(b.stage_cap(stage), 6),
            "spent": round(b.spent(stage), 6),
            "open": round(b.open_usd(stage), 6),
            "request": max_usd,
        }
        if st["spent"] + st["open"] + max_usd > st["cap"] + EPS:
            raise BudgetRefused(
                f"{stage}: spent ${st['spent']:.4f} + open ${st['open']:.4f} + ${max_usd:.4f} "
                f"exceeds the stage cap ${st['cap']:.2f}",
                st,
            )
        sp = {
            "cap": b.caps.sprint_cap_usd,
            "spent": round(b.sprint_spent(), 6),
            "open": round(b.open_usd(), 6),
            "request": max_usd,
            "topup_recorded": b.caps.topup_recorded,
        }
        if sp["spent"] + sp["open"] + max_usd > sp["cap"] + EPS:
            raise BudgetRefused(
                f"sprint: spent ${sp['spent']:.4f} + open ${sp['open']:.4f} + ${max_usd:.4f} "
                f"exceeds the sprint cap ${sp['cap']:.2f}",
                sp,
            )
        rid = uuid.uuid4().hex
        ts = _now()
        b.res[rid] = {
            "id": rid,
            "stage": stage,
            "max_usd": max_usd,
            "ts": ts,
            "utc": _utc(ts),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "note": note,
            "ledger": str(led),
        }
        _save_state(state, b.res)
    return rid


def _closed_error(b: _Books, res_id: str) -> ReservationError:
    ev = _closed(b.events).get(res_id)
    if ev is not None:
        return ReservationError(
            f"reservation {res_id} is already closed ({ev['op']} at {ev['utc']}); see the budget "
            f"event log"
        )
    return ReservationError(f"unknown reservation {res_id}")


def settle(
    res_id: str,
    actual_usd: float,
    *,
    stage: str | None = None,
    kind: str = "judge",
    resource: str = "",
    units: float = 0,
    note: str = "",
    ledger: str | Path | None = None,
    caps_file: str | Path | None = None,
) -> dict:
    """Record the call's actual cost through `emx.spend.record` (exp = the reservation's stage)
    and release the reservation. Returns the ledger entry.

    `stage`, if given, must be the reservation's stage. A note containing the reserved settlement
    marker is refused. The closing `settle` event in the budget event log is the authoritative
    record (it carries the ledger row's id). The money is already spent, so a cost above the
    reservation is still recorded in full, and an `overrun` event is logged."""
    actual_usd = _ceil6(_dec(actual_usd, "actual_usd"))
    _check_note(note)
    _, _, state, events = _paths(ledger)
    with _locked(ledger, caps_file):
        b = _books(ledger, caps_file, state, events)
        if res_id not in b.res:
            raise _closed_error(b, res_id)
        r = b.res[res_id]
        if stage is not None and stage != r["stage"]:
            raise ReservationError(f"reservation {res_id} is {r['stage']}, not {stage}")
        entry = _charge(
            events,
            res_id,
            r,
            actual_usd,
            "settle",
            ledger,
            kind=kind,
            resource=resource,
            units=units,
            note=note,
        )
        _log(
            events,
            {
                "op": "settle",
                "reservation_id": res_id,
                "stage": r["stage"],
                "reserved_usd": r["max_usd"],
                "charged_usd": actual_usd,
                "ledger_id": entry["id"],
            },
        )
        if actual_usd > r["max_usd"] + EPS:
            _log(
                events,
                {
                    "op": "overrun",
                    "reservation_id": res_id,
                    "stage": r["stage"],
                    "reserved_usd": r["max_usd"],
                    "actual_usd": actual_usd,
                },
            )
        del b.res[res_id]
        _save_state(state, b.res)
    return entry


def cancel(
    res_id: str,
    reason: str,
    *,
    ledger: str | Path | None = None,
    caps_file: str | Path | None = None,
) -> dict:
    """Release a reservation with no charge, for a call that certainly never reached the provider.
    Logged with its reason. If the call may have gone out, `settle` it (at the full reservation
    when its cost is unknown) instead."""
    if not reason or not reason.strip():
        raise ValueError("cancel needs a reason")
    _, _, state, events = _paths(ledger)
    with _locked(ledger, caps_file):
        b = _books(ledger, caps_file, state, events)
        if res_id not in b.res:
            raise _closed_error(b, res_id)
        r = b.res[res_id]
        ev = _log(
            events,
            {
                "op": "cancel",
                "reservation_id": res_id,
                "stage": r["stage"],
                "reserved_usd": r["max_usd"],
                "reason": reason.strip(),
            },
        )
        del b.res[res_id]
        _save_state(state, b.res)
        return ev


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def owner_dead(r: dict) -> bool | None:
    """True if the reservation's recorded process is gone, False if it is alive, None if it ran
    on another host and can't be checked from here. A recycled PID reads as alive, which keeps the
    reservation open: the safe side."""
    if r.get("host") != socket.gethostname():
        return None
    return not _pid_alive(int(r["pid"]))


def expire(
    older_than: timedelta | float,
    *,
    charge_full: bool = True,
    reason: str = "",
    force: bool = False,
    ledger: str | Path | None = None,
    caps_file: str | Path | None = None,
    now: float | None = None,
) -> list[str]:
    """Recover reservations older than `older_than` (a timedelta or seconds) whose owner is shown
    dead, with a log entry each. Returns their ids.

    Only reservations whose recorded process on this host no longer exists are expired, so a slow
    live call is never charged under it and then refused at `settle` (review r1 finding 4). One
    from another host, which can't be checked, is expired only with `force=True`, after confirming
    by hand that its process is gone.

    The call's cost is unknown when its process died. With `charge_full` (the default) each is
    settled at its full reserved amount, as `emx.spend.recover_uncertain` does, with the note
    saying so. Pass `charge_full=False` only once the provider's records show the calls cost
    nothing."""
    secs = older_than.total_seconds() if isinstance(older_than, timedelta) else float(older_than)
    if not (math.isfinite(secs) and secs >= 0):
        raise ValueError(f"older_than must be non-negative, got {older_than!r}")
    _check_note(reason)
    cutoff = (_now() if now is None else now) - secs
    _, _, state, events = _paths(ledger)
    out = []
    with _locked(ledger, caps_file):
        res = _books(ledger, caps_file, state, events).res
        for rid in sorted(rid for rid, r in res.items() if r["ts"] < cutoff):
            r = res[rid]
            dead = owner_dead(r)
            if dead is False or (dead is None and not force):
                continue
            charged, ledger_id = 0.0, None
            if charge_full:
                charged = r["max_usd"]
                ledger_id = _charge(
                    events,
                    rid,
                    r,
                    charged,
                    "expire",
                    ledger,
                    resource="sprint-reservation-expired",
                    note=(
                        f"expired reservation from pid {r['pid']} on {r['host']} at {r['utc']}, "
                        f"outcome unknown: priced at its full reserved amount"
                    ),
                )["id"]
            _log(
                events,
                {
                    "op": "expire",
                    "reservation_id": rid,
                    "stage": r["stage"],
                    "reserved_usd": r["max_usd"],
                    "charged_usd": charged,
                    "ledger_id": ledger_id,
                    "reserved_utc": r["utc"],
                    "pid": r["pid"],
                    "host": r["host"],
                    "owner": "dead" if dead else "unchecked (forced)",
                    "reason": reason,
                },
            )
            del res[rid]
            _save_state(state, res)
            out.append(rid)
    return out


def transfer(
    source: str,
    stage: str,
    usd: float,
    reason: str,
    *,
    approved_by: str | None = None,
    ledger: str | Path | None = None,
    caps_file: str | Path | None = None,
) -> dict:
    """Move `usd` of cap into `stage`, logged with its reason, under the budget lock.

    `source` is a pool (`reserve`, `contingency`) or another stage. Refused if the pool or the
    source stage or the destination needs the top-up and it isn't recorded, and:
    - from a pool: if the pool doesn't hold that much, or if pool transfers would total more than
      `transfer_approval_over_usd` without `approved_by="jacob"` (pass that only after Jacob's
      explicit yes; it is recorded in the log);
    - from a stage: if it would take the source's cap below its spent + open reservations (review
      r2 finding 2; the plan's low-loss-yield branch moves unused Q2 authorisation to S1 and Q3).
      Stage-to-stage moves don't change the sprint cap and don't count toward the approval
      threshold, which the plan sets for the reserve and contingency."""
    usd = _usd(usd, "usd")
    if usd <= 0:
        raise ValueError("transfer amount must be positive")
    if not reason or not reason.strip():
        raise ValueError("a transfer must name its reason")
    _, _, state, events = _paths(ledger)
    with _locked(ledger, caps_file):
        b = _books(ledger, caps_file, state, events)
        sc = _stage_ok(b.caps, stage)
        if source == stage:
            raise ValueError("a transfer needs a different source and destination")
        if source in b.caps.pools:
            kind, needs_topup = "pool", b.caps.pools[source].requires_topup
        elif source in b.caps.stages:
            kind, needs_topup = "stage", b.caps.stages[source].requires_topup
        else:
            raise ValueError(
                f"unknown transfer source {source!r}; pools {sorted(b.caps.pools)}, "
                f"stages {sorted(b.caps.stages)}"
            )
        if (needs_topup or sc.requires_topup) and not b.caps.topup_recorded:
            raise TopUpRequired(f"transfer {source} -> {stage} needs the top-up recorded first")
        ev = {
            "op": "transfer",
            "source": source,
            "source_kind": kind,
            "stage": stage,
            "usd": usd,
            "reason": reason.strip(),
            "approved_by": approved_by,
        }
        if kind == "pool":
            left = round(b.caps.pools[source].usd - b.transferred_out(source), 6)
            if usd > left + EPS:
                raise BudgetRefused(
                    f"pool {source} has ${left:.2f} left, asked ${usd:.2f}",
                    {"source": source, "left": left, "request": usd},
                )
            total = round(b.pool_transfers_total() + usd, 6)
            limit = b.caps.transfer_approval_over_usd
            if total > limit + EPS and (approved_by or "").strip().lower() != "jacob":
                raise BudgetRefused(
                    f"pool transfers would total ${total:.2f}, over ${limit:.2f}: needs Jacob's "
                    f"yes (approved_by='jacob')",
                    {"total": total, "limit": limit},
                )
            ev["pool_transfers_total_usd"] = total
        else:
            free = round(b.stage_cap(source) - b.spent(source) - b.open_usd(source), 6)
            if usd > free + EPS:
                raise BudgetRefused(
                    f"{source} has ${free:.4f} unspent and unreserved (cap "
                    f"${b.stage_cap(source):.2f} - spent ${b.spent(source):.4f} - open "
                    f"${b.open_usd(source):.4f}); can't move ${usd:.2f}",
                    {"source": source, "free": free, "request": usd},
                )
        return _log(events, ev)


def status(*, ledger: str | Path | None = None, caps_file: str | Path | None = None) -> dict:
    """Snapshot of caps, spend, open reservations and headroom per stage, the sprint and pools."""
    _, _, state, events = _paths(ledger)
    with _locked(ledger, caps_file):
        b = _books(ledger, caps_file, state, events)
    stages = {}
    for s, sc in b.caps.stages.items():
        cap, spent_, open_ = b.stage_cap(s), b.spent(s), b.open_usd(s)
        usable = b.caps.topup_recorded or not sc.requires_topup
        stages[s] = {
            "cap": round(cap, 6),
            "spent": round(spent_, 6),
            "open": round(open_, 6),
            "headroom": round(cap - spent_ - open_, 6) if usable else 0.0,
            "requires_topup": sc.requires_topup,
        }
    pools = {
        k: {
            "usd": p.usd,
            "left": round(p.usd - b.transferred_out(k), 6),
            "requires_topup": p.requires_topup,
        }
        for k, p in b.caps.pools.items()
    }
    return {
        "sprint": {
            "cap": b.caps.sprint_cap_usd,
            "topup_recorded": b.caps.topup_recorded,
            "spent": round(b.sprint_spent(), 6),
            "open": round(b.open_usd(), 6),
        },
        "stages": stages,
        "pools": pools,
        "open_reservations": sorted(b.res.values(), key=lambda r: r["ts"]),
    }
