"""Per-(group, model, sub-arm) context distribution from out/transcripts.jsonl.
Usage: python3 -I group_detail.py <group-substring> [sub-prefix-depth]"""
import json, sys, statistics
from collections import defaultdict
from pathlib import Path
rows = [json.loads(l) for l in open(Path(__file__).parent / "out/transcripts.jsonl")]
pat = sys.argv[1]
depth = int(sys.argv[2]) if len(sys.argv) > 2 else 1
def q(xs, p):
    xs = sorted(xs); k = (len(xs) - 1) * p; lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
by = defaultdict(list)
for r in rows:
    if pat in r["group"]:
        by[(r["model"], "/".join(r["sub"].split("/")[:depth]))].append(r)
print(f"{'model':28s} {'sub':22s} {'n':>4s} {'turns_med':>9s} {'turns_max':>9s} {'ctx_med':>8s} {'ctx_p90':>8s} {'ctx_max':>8s} >64k >100k >128k >160k >200k")
for (m, s), rs in sorted(by.items()):
    c = [r["final_ctx"] for r in rs]; t = [r["n_turns"] or r["n_calls"] for r in rs]
    print(f"{m[:28]:28s} {s[:22]:22s} {len(rs):4d} {statistics.median(t):9.0f} {max(t):9d} {q(c,.5)/1e3:7.1f}k {q(c,.9)/1e3:7.1f}k {max(c)/1e3:7.1f}k "
          + " ".join(f"{sum(1 for x in c if x > th):4d}" for th in (64e3, 100e3, 128e3, 160e3, 200e3)))
