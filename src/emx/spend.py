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

CLI:
  uv run python -m emx.spend --report
  uv run python -m emx.spend --check 1.25 --kind judge [--running-pod-usd 0.0] [--exp I1a]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from emx import contracts as e1c
from emx.e1b import contracts as e1bc
from emx.e3 import contracts as e3c
from emx.i1a import contracts as i1ac
from emx.i9 import contracts as i9c

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "logs" / "costs.jsonl"
LEDGER_ENV = "EMX_COSTS_LEDGER"  # override for tests and dry runs
KINDS: tuple[str, ...] = ("gpu", "judge")
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


def record(
    kind: str,
    resource: str,
    units: float,
    usd: float,
    note: str = "",
    exp: str = EXP,
    ledger: str | Path | None = None,
) -> dict:
    """Append one entry under an exclusive lock. `units` is GPU-hours or API calls."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if not (usd >= 0 and float(usd) == float(usd)):
        raise ValueError(f"usd must be a non-negative number, got {usd!r}")
    entry = {
        "utc": utc_now(),
        "exp": exp,
        "kind": kind,
        "resource": resource,
        "units": units,
        "usd": round(float(usd), 6),
        "note": note,
        "id": uuid.uuid4().hex,
    }
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
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{path}:{i}: malformed ledger line: {e}") from e
    return out


def spent(kind: str | None = None, exp: str = EXP, ledger: str | Path | None = None) -> float:
    """Dollars recorded for `exp`, optionally one kind only."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS} or None, got {kind!r}")
    return round(
        sum(
            float(e.get("usd", 0.0))
            for e in read_entries(ledger)
            if e.get("exp") == exp and (kind is None or e.get("kind") == kind)
        ),
        6,
    )


def totals(exp: str = EXP, ledger: str | Path | None = None) -> dict:
    c = caps_for(exp)
    gpu = spent("gpu", exp, ledger)
    judge = spent("judge", exp, ledger)
    return {
        "exp": exp,
        "ledger": str(ledger_path(ledger)),
        "gpu_usd": gpu,
        "judge_usd": judge,
        "total_usd": round(gpu + judge, 6),
        "hard_cap_usd": c.hard_cap_usd,
        "gpu_limit_usd": c.gpu_limit_usd,
        "judge_limit_usd": c.judge_limit(gpu),
        "reserve_usd": c.reserve_usd,
        "watchdog_kill_usd": watchdog_kill_usd(exp, ledger),
    }


def watchdog_kill_usd(exp: str = EXP, ledger: str | Path | None = None) -> float:
    """GPU dollars (spent + running) at which the pod watchdog must kill:
    min(GPU limit, hard cap - reserve - judges)."""
    c = caps_for(exp)
    return round(
        min(c.gpu_limit_usd, c.hard_cap_usd - c.reserve_usd - spent("judge", exp, ledger)), 6
    )


def check_total(
    projected_usd: float,
    kind: str,
    running_pod_usd: float = 0.0,
    exp: str = EXP,
    ledger: str | Path | None = None,
) -> dict:
    """The combined guard. Returns the breakdown if within every limit, else raises
    `BudgetExceeded` (exit code 2). `projected_usd` is the batch or GPU session about to start;
    `running_pod_usd` is the projected cost of pods already running (not yet in the ledger)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if not (math.isfinite(projected_usd) and math.isfinite(running_pod_usd)):
        raise ValueError("projected_usd and running_pod_usd must be finite numbers")
    if projected_usd < 0 or running_pod_usd < 0:
        raise ValueError("projected_usd and running_pod_usd must be >= 0")
    c = caps_for(exp)
    gpu_spent = spent("gpu", exp, ledger)
    judge_spent = spent("judge", exp, ledger)
    gpu_after = gpu_spent + running_pod_usd + (projected_usd if kind == "gpu" else 0.0)
    judge_after = judge_spent + (projected_usd if kind == "judge" else 0.0)
    total_after = gpu_after + judge_after
    judge_limit = c.judge_limit(gpu_after)
    b = {
        "exp": exp,
        "kind": kind,
        "gpu_spent_usd": gpu_spent,
        "running_pod_usd": running_pod_usd,
        "judge_spent_usd": judge_spent,
        "projected_usd": projected_usd,
        "gpu_after_usd": round(gpu_after, 6),
        "judge_after_usd": round(judge_after, 6),
        "total_after_usd": round(total_after, 6),
        "hard_cap_usd": c.hard_cap_usd,
        "gpu_limit_usd": c.gpu_limit_usd,
        "judge_limit_usd": judge_limit,
        "reserve_usd": c.reserve_usd,
    }
    fails = []
    if total_after > c.hard_cap_usd:
        fails.append(f"total ${total_after:.2f} > hard cap ${c.hard_cap_usd:.2f}")
    if gpu_after > c.gpu_limit_usd:
        fails.append(f"GPU ${gpu_after:.2f} > GPU limit ${c.gpu_limit_usd:.2f}")
    if judge_after > judge_limit:
        fails.append(f"judges ${judge_after:.2f} > judge limit ${judge_limit:.2f}")
    b["ok"] = not fails
    if fails:
        msg = f"[spend guard] {exp} {kind} batch of ${projected_usd:.4f} refused: " + "; ".join(
            fails
        )
        print(msg, file=sys.stderr)
        raise BudgetExceeded(msg, b)
    return b


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.spend", description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true", help="print totals as JSON")
    ap.add_argument("--check", type=float, metavar="USD", help="projected batch cost to check")
    ap.add_argument("--kind", choices=KINDS, default="judge")
    ap.add_argument("--running-pod-usd", type=float, default=0.0)
    ap.add_argument("--exp", default=EXP)
    ap.add_argument("--ledger", default=None)
    args = ap.parse_args(argv)
    if args.check is None and not args.report:
        args.report = True
    if args.report:
        print(json.dumps(totals(args.exp, args.ledger), indent=2))
    if args.check is not None:
        try:
            b = check_total(args.check, args.kind, args.running_pod_usd, args.exp, args.ledger)
        except BudgetExceeded as e:
            print(json.dumps(e.breakdown, indent=2))
            return 2
        print(json.dumps(b, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
