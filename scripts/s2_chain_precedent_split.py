"""Task chaining: B's remote-rewrite rate split by whether A issued a force-push ($0, existing rows).

Reads the task-chaining worktree's a_gate.json and rows.jsonl (rewrite == "yes"), for B starts
that carry A's history (full or compacted). "A issued a force-push" = non-empty force_commands;
the script checks that this agrees with force_in_scope or force_elsewhere (an actual rewrite) on
every history, and stops if not. Observational: each A chose whether to force-push, and the
histories span two A tasks. So neither statistic below identifies a causal precedent effect.
- 95% percentile cluster-bootstrap interval over A histories (seed 20261027, 4000 replicates):
  unreliable with only three histories in one group; kept for the record only.
- Exact one-sided permutation p over the no-permission histories (upper tail = higher B rate
  after A force-pushed), under an assumed exchangeability of histories.
Usage: uv run python scripts/s2_chain_precedent_split.py <task-chaining worktree root>
"""

import json
import random
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

root = Path(sys.argv[1])
gate = json.loads((root / "data/raw/sprint/s2/chain/a_gate.json").read_text())["rows"]
a_fp = {h: bool(v["force_commands"]) for h, v in gate.items()}
rewrote = {h: bool(v["force_in_scope"] or v["force_elsewhere"]) for h, v in gate.items()}
if a_fp != rewrote:
    sys.exit(f"force_commands and actual rewrites disagree: "
             f"{sorted(h for h in gate if a_fp[h] != rewrote[h])}")
rows = [json.loads(line) for line in (root / "results/sprint/S2/chain/rows.jsonl").open()]
per = defaultdict(lambda: [0, 0])
for x in rows:
    if x["history"] and x["start"] in ("full", "compacted"):
        per[x["history"]][0] += x["rewrite"] == "yes"
        per[x["history"]][1] += 1


def rate(hs):
    return sum(per[h][0] for h in hs) / sum(per[h][1] for h in hs)


print("OBSERVATIONAL, exploratory: each A chose whether to force-push; histories span two A tasks.")
print("Neither statistic below identifies a causal precedent effect.")
for h in sorted(per):
    print(h, "A force-pushed" if a_fp[h] else "A did not", f"{per[h][0]}/{per[h][1]}")
fp = [h for h in per if "-np-" in h and a_fp[h]]
no = [h for h in per if "-np-" in h and not a_fp[h]]
# The permutation statistic below uses the pooled rate, which is only well defined per split
# when every history contributes the same number of B runs.
assert len({per[h][1] for h in fp + no}) == 1, "unequal B runs per history"
obs = rate(fp) - rate(no)
random.seed(20261027)
boot = sorted(
    rate([random.choice(fp) for _ in fp]) - rate([random.choice(no) for _ in no])
    for _ in range(4000)
)
print(f"no-permission histories: A force-pushed {rate(fp):.3f} (n_hist={len(fp)}), "
      f"A did not {rate(no):.3f} (n_hist={len(no)}), diff {obs:+.3f}")
print(f"  95% percentile cluster-bootstrap interval [{boot[100]:+.3f}, {boot[3899]:+.3f}]: "
      f"unreliable with only {min(len(fp), len(no))} histories in one group; record only")
splits = list(combinations(fp + no, len(no)))
n_ge = 0
for grp in splits:
    rest = [h for h in fp + no if h not in grp]
    n_ge += rate(rest) - rate(list(grp)) >= obs - 1e-12
print(f"  exact permutation over {len(fp + no)} histories, assuming they are exchangeable: "
      f"{n_ge}/{len(splits)} splits as or more extreme in the direction 'higher B rate after A "
      f"force-pushed', one-sided p = {n_ge / len(splits):.4f}")
p = [h for h in per if "-p-" in h]
print(f"scoped-permission histories (all force-pushed in A): {rate(p):.3f} (n_hist={len(p)})")
