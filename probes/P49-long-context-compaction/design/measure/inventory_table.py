"""Markdown inventory table from out/transcripts.jsonl (logged API usage per transcript).
Per (group, model): n transcripts, median turns, final context median/p90/max (max over calls of
prompt+completion), counts over 64/100/128/160/200k, mean prompt / completion / reasoning tokens
per call, cached share of prompt tokens, share of transcripts storing reasoning blocks, tool
outputs flagged truncated. Usage: python3 -I inventory_table.py -> out/inventory_table.md"""
import json, statistics
from collections import defaultdict
from pathlib import Path
D = Path(__file__).parent / "out"
rows = [json.loads(l) for l in open(D / "transcripts.jsonl")]
def q(xs, p):
    xs = sorted(xs); k = (len(xs) - 1) * p; lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
by = defaultdict(list)
for r in rows:
    if r["model"].startswith("mockllm"):
        continue
    g = r["group"].replace("[wt:x3-build] ", "x3-build wt: ")
    by[(g, r["model"])].append(r)
L = ["| set (path under main checkout unless noted) | model | n | turns med (max) | final ctx med / p90 / max | >64k | >100k | >128k | >160k | >200k | prompt/call mean | completion/call mean | reasoning/call mean | cached share | reasoning stored | truncated tool outputs |",
     "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
for (g, m), rs in sorted(by.items()):
    c = [r["final_ctx"] for r in rs]; t = [r["n_turns"] or r["n_calls"] for r in rs]
    calls = sum(r["n_calls"] for r in rs)
    tr = [r["truncated_calls"] for r in rs if r["truncated_calls"] is not None]
    L.append(f"| {g} | {m} | {len(rs)} | {statistics.median(t):.0f} ({max(t)}) | {q(c,.5)/1e3:.1f}k / {q(c,.9)/1e3:.1f}k / {max(c)/1e3:.1f}k | "
             + " | ".join(str(sum(1 for x in c if x > th)) for th in (64e3, 100e3, 128e3, 160e3, 200e3))
             + f" | {sum(r['sum_in'] for r in rs)/calls:,.0f} | {sum(r['sum_out'] for r in rs)/calls:,.0f} | {sum(r['sum_reason'] for r in rs)/calls:,.0f} | "
             + f"{sum(r['sum_cached'] for r in rs)/max(1,sum(r['sum_in'] for r in rs)):.2f} | {sum(1 for r in rs if r['reasoning_msgs'])/len(rs):.2f} | "
             + (f"{sum(tr)} of {sum(r['n_tool_calls'] or 0 for r in rs)}" if tr else "-") + " |")
(D / "inventory_table.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
