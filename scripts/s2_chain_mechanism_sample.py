"""Seeded random sample of B runs whose force-push rewrote the remote: the agent's visible text
around its first force-push in B (task-chaining readout, mechanism line).

Usage: s2_chain_mechanism_sample.py <chain raw dir> <k> <out.md>
"""
import json
import random
import re
import sys
from pathlib import Path

root, k, out = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
FORCE = re.compile(r"(\s-[a-zA-Z]*f\b|--force\b|--force-with-lease|\s\+[\w/.:-]+)")
rows = [json.loads(x) for x in (root / "rows.jsonl").read_text().splitlines() if x.strip()]
yes = sorted(r["id"] for r in rows if r["rewrite"] == "yes")
pick = random.Random(20261026).sample(yes, min(k, len(yes)))
by = {r["id"]: r for r in rows}

def text(m):
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return (c or "").strip()

L = [f"# Mechanism sample: {len(pick)} of {len(yes)} B force-push runs (seed 20261026)", ""]
for rid in pick:
    r = by[rid]
    d = root / Path(r["dir"]).relative_to(Path(r["dir"]).parents[2])
    msgs = json.loads((d / "messages.json").read_text())
    target = r["rewrite_events"][0]["target"]
    start = next(i for i, m in enumerate(msgs) if m["role"] == "user" and target in text(m))
    hit = None
    for i in range(start, len(msgs)):
        m = msgs[i]
        for tc in m.get("tool_calls") or []:
            args = tc.get("function", {}).get("arguments", "")
            if re.search(r"git\s+push", args) and FORCE.search(" " + args):
                hit = (i, args)
                break
        if hit:
            break
    L += [f"## {rid}",
          (f"cell {r['hist']}/{r['project']}/{r['start']}; rejection {r['rejection']}; "
           f"B starts at message {start} of {len(msgs)}"), ""]
    if not hit:
        L += ["(no force-flag push command found after B's task message)", ""]
        continue
    i, args = hit
    prev = [j for j in range(start, i + 1) if msgs[j]["role"] == "assistant" and text(msgs[j])]
    for j in prev[-3:]:
        L += [f"- assistant @{j}: " + text(msgs[j])[:900].replace("\n", " ")]
    L += [f"- command @{i}: `{args[:400]}`", ""]
out.write_text("\n".join(L) + "\n")
print(out, len(pick), len(yes))
