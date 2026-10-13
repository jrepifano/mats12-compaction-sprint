"""P23 exploratory pooled analysis (log.md, "analysis"): both agents' episodes treated as repeats
of one context (6 per context and cell), through analyze.py unchanged. Writes
results/measures_pooled.csv and results/analysis_pooled.json. Free.

  PYTHONPATH=<abs>/probes/P23-compaction-decisions-vs-cues:<abs>/src:<abs>/scripts \
      uv run --group swarm python -m p23.pooled
"""
from __future__ import annotations

import csv

from p23 import analyze
from p23 import contracts as C

POOLED_CSV = C.RESULTS_DIR / "measures_pooled.csv"
POOLED_JSON = C.RESULTS_DIR / "analysis_pooled.json"


def main() -> int:
    with open(C.MEASURES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    with open(POOLED_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({**r, "model_slug": "pooled"})
    return analyze.main(["--measures", str(POOLED_CSV), "--out", str(POOLED_JSON)])


if __name__ == "__main__":
    raise SystemExit(main())
