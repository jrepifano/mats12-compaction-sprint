"""Ledger vs checkpoints for P49 (log 2026-10-16 07:57Z, 08:42Z: parallel runners recovered or
settled each other's block reservations, so the ledger can be off in either direction).
`python -m p49.reconcile` prints both totals; billed cost in results/raw/requests.jsonl (every
response and billed retry) is the actual spend."""
from __future__ import annotations

import json

from emx import spend
from p49 import contracts as C


def checkpoint_billed(path=C.CHECKPOINTS) -> float:
    tot = 0.0
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get("event") in ("response", "retry") and r.get("cost"):
                tot += float(r["cost"])
    return round(tot, 6)


def main() -> int:
    t = spend.totals(C.EXP)
    billed = checkpoint_billed()
    print(json.dumps({"checkpoint_billed_usd": billed, "ledger_charged_usd": t["judge_usd"],
                      "ledger_open_reserved_usd": t["judge_reserved_usd"],
                      "ledger_minus_billed": round(t["judge_usd"] - billed, 4)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
