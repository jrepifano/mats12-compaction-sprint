"""Reconcile SW-P0 ledger entries after the cost-formula fix (2026-09-29).

The first runs priced usage with a formula that subtracted cache reads from `input_tokens`
(which Inspect had already done) and added reasoning tokens to `output_tokens` (which already
include them). This recomputes each log's cost with the fixed `usd_for`, compares it with what
the ledger recorded for that log, and appends one adjustment entry per log for the difference.
Idempotent: logs that already have an adjustment entry are skipped.

  uv run --project third_party/oai-hf-incident-reproduction python scripts/swarm_p0_reconcile.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log

WT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WT / "scripts"))
from swarm_p0_run import EXP, ledger_entries, record, usd_for

entries = [e for e in ledger_entries() if e.get("exp") == EXP]
for info in list_eval_logs(str(WT / "results" / "SW-P0" / "logs"), recursive=True):
    log = read_eval_log(info, header_only=True)
    name = Path(log.location).name
    mine = [e for e in entries if f"log={name}" in e.get("note", "")]
    if any("adjustment" in e["note"] for e in mine):
        continue
    usage = log.stats.model_usage if log.stats else {}
    correct = sum(usd_for(m, u) for m, u in usage.items())
    recorded = sum(float(e["usd"]) for e in mine)
    delta = correct - recorded
    print(f"{name}: recorded {recorded:.4f} correct {correct:.4f} delta {delta:+.4f}")
    if abs(delta) > 1e-6:
        record("adjustment", 0, max(delta, 0.0),
               f"sw_p0 cost-formula adjustment (recorded {recorded:.6f}, correct {correct:.6f}, "
               f"delta {delta:+.6f}) log={name}")
        if delta < 0:
            print("  NOTE negative delta not recorded as a refund; recorded 0")
