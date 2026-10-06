"""Budget reservation for S4 paid runs (implementation plan §1.13).

Every paid call is covered by a reservation taken **before** it starts, under one exclusive
`fcntl` lock on the shared spend ledger, so parallel processes cannot jointly cross the cap
(the gap gpt-6-sol found in the Phase 0 runner, logs/reviews/20260929-SW-e5ad305-r1.md).

- Spend lines go to `logs/costs.jsonl` in the checkout this module runs from (per-worktree ledgers,
  CLAUDE.md, 2026-10-04; it used to be hard-coded to the main checkout), in the same
  one-JSON-object-per-line format as `emx.spend`, with exp `S4` and kind `api` and a unique `id`.
- Outstanding reservations live in `logs/S4.reservations.json` next to it.
- A reservation is refused if spent + outstanding + new > cap.

Paths can be overridden with S4_LEDGER / S4_RESERVATIONS (tests, dry runs).
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from s4world.contracts import EXP_ID, PILOT_CAP_USD

REPO_ROOT = Path(__file__).resolve().parents[2]  # the checkout this module runs from


class BudgetRefused(RuntimeError):
    """A reservation would cross the cap."""


def ledger_path() -> Path:
    return Path(os.environ.get("S4_LEDGER", REPO_ROOT / "logs" / "costs.jsonl"))


def reservations_path() -> Path:
    return Path(os.environ.get("S4_RESERVATIONS", REPO_ROOT / "logs" / "S4.reservations.json"))


@contextmanager
def _locked():
    """Exclusive lock on the ledger file itself (the lock emx.spend's writers also take)."""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _spent_unlocked(exp: str) -> float:
    path = ledger_path()
    if not path.exists():
        return 0.0
    total = 0.0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{path}:{i}: malformed ledger line: {e}") from e
        if entry.get("exp") == exp:
            total += float(entry.get("usd", 0.0))
    return total


def _read_reservations() -> dict[str, dict]:
    path = reservations_path()
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _write_reservations(res: dict[str, dict]) -> None:
    path = reservations_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def status(exp: str = EXP_ID, cap: float = PILOT_CAP_USD) -> dict:
    with _locked():
        spent = _spent_unlocked(exp)
        outstanding = sum(r["usd"] for r in _read_reservations().values() if r["exp"] == exp)
    return {"spent": round(spent, 6), "outstanding": round(outstanding, 6),
            "remaining": round(cap - spent - outstanding, 6), "cap": cap}


def reserve(usd: float, note: str, exp: str = EXP_ID, cap: float = PILOT_CAP_USD) -> str:
    """Reserve `usd` against the cap; returns a reservation id, or raises BudgetRefused."""
    if not usd >= 0:
        raise ValueError(f"reservation must be non-negative, got {usd!r}")
    with _locked():
        spent = _spent_unlocked(exp)
        res = _read_reservations()
        outstanding = sum(r["usd"] for r in res.values() if r["exp"] == exp)
        if spent + outstanding + usd > cap + 1e-9:
            raise BudgetRefused(
                f"{exp}: spent {spent:.4f} + outstanding {outstanding:.4f} + {usd:.4f} "
                f"> cap {cap:.2f} ({note})")
        rid = uuid.uuid4().hex[:12]
        res[rid] = {"exp": exp, "usd": round(float(usd), 6), "note": note, "utc": _now()}
        _write_reservations(res)
    return rid


def _settled_unlocked(rid: str) -> dict | None:
    """The ledger line that settled reservation `rid` (its "rid" field), or None."""
    path = ledger_path()
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and f'"{rid}"' in line:
            entry = json.loads(line)
            if entry.get("rid") == rid:
                return entry
    return None


def settled_entry(rid: str) -> dict | None:
    """Under the ledger lock: the ledger line that settled `rid`, or None (X3b code review r1,
    finding 3: settlement must be recognisable by reservation id)."""
    with _locked():
        return _settled_unlocked(rid)


def settle(rid: str, actual_usd: float, resource: str, units: float, note: str) -> dict:
    """Record actual spend for a reservation and release it. Overspend is recorded, not hidden.

    Idempotent by reservation id: the ledger line carries `rid`, and if `rid` was already settled
    (a crash between the fsync'd ledger append and the reservation removal), the reservation is
    only removed and the existing line returned, never appended twice."""
    if not actual_usd >= 0:
        raise ValueError(f"actual spend must be non-negative, got {actual_usd!r}")
    with _locked() as f:
        res = _read_reservations()
        done = _settled_unlocked(rid)
        if done is not None:
            if res.pop(rid, None) is not None:
                _write_reservations(res)
            return done
        r = res.pop(rid, None)
        if r is None:
            raise KeyError(f"unknown reservation {rid}")
        over = actual_usd > r["usd"] + 1e-9
        entry = {"utc": _now(), "exp": r["exp"], "kind": "api", "resource": resource,
                 "units": units, "usd": round(float(actual_usd), 6),
                 "note": f"{note} (reserved {r['usd']:.4f}{'; OVER RESERVATION' if over else ''})",
                 "id": uuid.uuid4().hex, "rid": rid}
        f.seek(0, os.SEEK_END)
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())
        _write_reservations(res)
    return entry


def release(rid: str) -> None:
    """Drop a reservation without spend (the call never ran)."""
    with _locked():
        res = _read_reservations()
        res.pop(rid, None)
        _write_reservations(res)
