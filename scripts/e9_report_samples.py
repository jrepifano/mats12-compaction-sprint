"""E9 pause report: score-stratified random examples of the content judge's output (seeded).

Population: data/E9/llm/A1_content.luna.jsonl rows with status ok and no safety flag, in file order.
Strata, drawn in this order with one random.Random(20261006): score >= 4, score 2-3, score 1;
4 documents per stratum via rng.sample. Prints a markdown table with every drawn row.
Usage: uv run python scripts/e9_report_samples.py
"""

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
rows = [json.loads(line) for line in open(ROOT / "data/E9/llm/A1_content.luna.jsonl")]
topic = {}
with open(ROOT / "data/E9/pools/P1.jsonl") as f:
    for line in f:
        d = json.loads(line)
        topic[d["doc_id"]] = d.get("topic")
ok = [r for r in rows if r["status"] == "ok" and not r.get("safety_flag")]
rng = random.Random(20261006)
strata = (("4-10", lambda s: s >= 4), ("2-3", lambda s: s in (2, 3)), ("1", lambda s: s == 1))
print("| Score | Topic | Harm type | Judge's summary | Document id |")
print("|---|---|---|---|---|")
for _, keep in strata:
    pop = [r for r in ok if keep(r["parsed"]["endorsement"])]
    for r in rng.sample(pop, 4):
        p = r["parsed"]
        print(f"| {p['endorsement']} | {topic[r['item_id']]} | {p['harm_type']} | {p['summary']} "
              f"| `{r['item_id']}` |")
