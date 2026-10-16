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
    from p49 import client
    t = spend.totals(C.EXP)
    billed = checkpoint_billed()
    lia = client.liability_total(C.CHECKPOINTS)          # every chain id
    out = {"checkpoint_billed_usd": billed,
           "unresolved_requests": lia["n_unresolved"],
           "unresolved_bound_usd": round(lia["unresolved_bound"], 4),
           "billed_plus_unresolved_bound_usd": round(billed + lia["unresolved_bound"], 4),
           "ledger_charged_usd": t["judge_usd"],
           "ledger_open_reserved_usd": t["judge_reserved_usd"],
           "ledger_minus_billed_usd": round(t["judge_usd"] - billed, 4),
           "ledger_minus_billed_plus_bound_usd": round(
               t["judge_usd"] - billed - lia["unresolved_bound"], 4)}
    print(json.dumps(out, indent=1))
    (C.RESULTS / "reconcile.json").write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
