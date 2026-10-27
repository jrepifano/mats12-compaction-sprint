"""Experiment 4's seeded draw (docs/sprint/plans/week2-chain-stress.md, Appendix A): the pool
and every chain's order. $0, no network, deterministic (seed 20261027).

    uv run python scripts/week2_chain_stress_draw.py [--deepswe-dir DIR] [--summary MD] [--check]

Reads data/raw/sprint/deepswe/trials.json and each clean task's task.toml (under --deepswe-dir,
default the checkout's data/raw/sprint/deepswe) and results/sprint/task_validation/summary.md.
Writes configs/week2/orders/chain_stress.json (the orders the chain driver runs) and
configs/week2/orders/chain_stress_draw.json (the record: input sha256s, the eligible table, the
pool, positions, the search's attempt count). With --check, writes nothing and exits 1 when the
committed files differ from what the draw gives. Library: compaction.s2.chain.chain_stress.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from compaction.s2.chain import chain_stress as CS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--deepswe-dir", type=Path, default=None)
    ap.add_argument("--summary", type=Path, default=CS.SUMMARY_MD)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    d = CS.draw(a.deepswe_dir, a.summary)
    orders = CS.orders_text(CS.orders(d))
    record = json.dumps(CS.draw_record(d), indent=1) + "\n"
    if a.check:
        same = (CS.ORDERS.is_file() and CS.ORDERS.read_text() == orders and
                CS.DRAW_RECORD.is_file() and CS.DRAW_RECORD.read_text() == record)
        print("orders and draw record match the draw" if same else "DIFFERENT from the draw")
        return 0 if same else 1
    CS.ORDERS.parent.mkdir(parents=True, exist_ok=True)
    CS.ORDERS.write_text(orders)
    CS.DRAW_RECORD.write_text(record)
    print(f"pool ({d['attempts']} attempts): " + ", ".join(d["pool"]))
    for i, ch in enumerate(d["unrelated"], 1):
        print(f"u{i}: " + " -> ".join(f"{t} ({d['languages'][t]})" for t in ch))
    print(f"never first: {CS.never_first(d['unrelated'], d['pool'])}")
    print(f"wrote {CS.ORDERS} (sha256 {CS.sha256_file(CS.ORDERS)}) and {CS.DRAW_RECORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
