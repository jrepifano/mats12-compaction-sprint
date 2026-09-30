"""Spend ledger (`logs/costs.jsonl`) and the one combined E1 budget guard.

New module (implementation plan §5 "One combined guard"; prereg §7 and §11 amendment A). The
mechanism follows ../mats12_neel_working/scripts/judge_panel_spend.py (project, then exit non-zero
before the call that would cross the cap); the numbers come from `emx.contracts`.

Ledger: one JSON object per line, `{utc, exp, kind: gpu|judge, resource, units, usd, note}`.
Every append takes an exclusive `fcntl.flock` on the ledger file itself and writes one complete
line, so concurrent writers cannot interleave (the working repo's ledger was corrupted by
parallel writers). Reads take a shared lock. A malformed line raises instead of being skipped.

Guard: `check_total(projected_usd, kind, running_pod_usd)` adds GPU spent so far, the running
pods' projected cost, judge spent so far and the batch about to run, and raises `BudgetExceeded`
(a `SystemExit` with code 2, so an uncaught guard stops a script non-zero) if the total would pass
`HARD_CAP_USD`, GPU would pass `GPU_LIMIT_USD`, or judges would pass `JUDGE_LIMIT_USD`.

CLI:
  uv run python -m emx.spend --report
  uv run python -m emx.spend --check 1.25 --kind judge [--running-pod-usd 0.0]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from emx.contracts import GPU_LIMIT_USD, HARD_CAP_USD, JUDGE_LIMIT_USD

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "logs" / "costs.jsonl"
LEDGER_ENV = "EMX_COSTS_LEDGER"  # override for tests and dry runs
KINDS: tuple[str, ...] = ("gpu", "judge")
EXP = "E1"


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
    gpu = spent("gpu", exp, ledger)
    judge = spent("judge", exp, ledger)
    return {
        "exp": exp,
        "ledger": str(ledger_path(ledger)),
        "gpu_usd": gpu,
        "judge_usd": judge,
        "total_usd": round(gpu + judge, 6),
        "hard_cap_usd": HARD_CAP_USD,
        "gpu_limit_usd": GPU_LIMIT_USD,
        "judge_limit_usd": JUDGE_LIMIT_USD,
        "watchdog_kill_usd": watchdog_kill_usd(exp, ledger),
    }


def watchdog_kill_usd(exp: str = EXP, ledger: str | Path | None = None) -> float:
    """GPU dollars (spent + running) at which the pod watchdog must kill: min(GPU limit, hard cap - judges)."""
    return round(min(GPU_LIMIT_USD, HARD_CAP_USD - spent("judge", exp, ledger)), 6)


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
    if projected_usd < 0 or running_pod_usd < 0:
        raise ValueError("projected_usd and running_pod_usd must be >= 0")
    gpu_spent = spent("gpu", exp, ledger)
    judge_spent = spent("judge", exp, ledger)
    gpu_after = gpu_spent + running_pod_usd + (projected_usd if kind == "gpu" else 0.0)
    judge_after = judge_spent + (projected_usd if kind == "judge" else 0.0)
    total_after = gpu_after + judge_after
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
        "hard_cap_usd": HARD_CAP_USD,
        "gpu_limit_usd": GPU_LIMIT_USD,
        "judge_limit_usd": JUDGE_LIMIT_USD,
    }
    fails = []
    if total_after > HARD_CAP_USD:
        fails.append(f"total ${total_after:.2f} > hard cap ${HARD_CAP_USD:.2f}")
    if gpu_after > GPU_LIMIT_USD:
        fails.append(f"GPU ${gpu_after:.2f} > GPU limit ${GPU_LIMIT_USD:.2f}")
    if judge_after > JUDGE_LIMIT_USD:
        fails.append(f"judges ${judge_after:.2f} > judge limit ${JUDGE_LIMIT_USD:.2f}")
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
