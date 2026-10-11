"""Quick per-arm accuracy table for one Stage 1 results file (laptop sanity read, not the
analysis of record; that is p06.analyze)."""

import json
import sys
from collections import defaultdict
from pathlib import Path

from .api import latest_ok


def table(path: str, groups=("k2", "k3", "k4", "k5", "k6", "k7", "k8", "fact_hops")):
    all_rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    rows = list(latest_ok(Path(path)).values())
    acc = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["status"] != "ok":
            continue
        g = r["bank"] if r["bank"] != "chain" else f"k{r['k']}"
        acc[(g, r["item_id"])][r["arm"]].append(r["correct"])
    print(path, "scored", len(rows), "api_error rows", sum(r["status"] != "ok" for r in all_rows),
          "invalid", sum(r["status"] == "ok" and not r["valid"] for r in rows))
    for g in groups:
        its = [v for (gg, _), v in acc.items() if gg == g and len(v) == 3]
        if not its:
            continue
        m = {a: sum(sum(v[a]) / len(v[a]) for v in its) / len(its)
             for a in ("none", "after", "before")}
        print(f"  {g:9s} n={len(its):3d} none {m['none']:.3f} after {m['after']:.3f} "
              f"before {m['before']:.3f}  after-before {m['after'] - m['before']:+.3f}  "
              f"after-none {m['after'] - m['none']:+.3f}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        table(p)
