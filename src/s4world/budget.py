"""Budget reservation for S4 paid runs (implementation plan §1.13).

Every paid call is covered by a reservation taken **before** it starts, under one exclusive
`fcntl` lock on the shared spend ledger, so parallel processes cannot jointly cross the cap
(the gap gpt-6-sol found in the Phase 0 runner, logs/reviews/20260929-SW-e5ad305-r1.md).

- Spend lines go to the shared ledger (`logs/costs.jsonl` in the main checkout), in the same
  one-JSON-object-per-line format as `emx.spend`, with exp `S4` and kind `api`.
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

MAIN_REPO = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory")


class BudgetRefused(RuntimeError):
    """A reservation would cross the cap."""


def ledger_path() -> Path:
    return Path(os.environ.get("S4_LEDGER", MAIN_REPO / "logs" / "costs.jsonl"))


def reservations_path() -> Path:
    return Path(os.environ.get("S4_RESERVATIONS", MAIN_REPO / "logs" / "S4.reservations.json"))


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


def settle(rid: str, actual_usd: float, resource: str, units: float, note: str) -> dict:
    """Record actual spend for a reservation and release it. Overspend is recorded, not hidden."""
    if not actual_usd >= 0:
        raise ValueError(f"actual spend must be non-negative, got {actual_usd!r}")
    with _locked() as f:
        res = _read_reservations()
        r = res.pop(rid, None)
        if r is None:
            raise KeyError(f"unknown reservation {rid}")
        over = actual_usd > r["usd"] + 1e-9
        entry = {"utc": _now(), "exp": r["exp"], "kind": "api", "resource": resource,
                 "units": units, "usd": round(float(actual_usd), 6),
                 "note": f"{note} (reserved {r['usd']:.4f}{'; OVER RESERVATION' if over else ''})"}
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
