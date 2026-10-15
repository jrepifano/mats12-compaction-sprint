"""Gate 0 pool: how many EXISTING transcripts are long enough for k window-triggered compactions.
A transcript whose uncompacted final context is F supports k compactions at threshold W with
carry-over c when F >= W + (k-1)(W-c) (each compaction resets the context to c; the transcript's
remaining content then has to refill W - c). Uses out/transcripts.jsonl (logged API usage:
max prompt+completion over calls) and out/swe_rebench_full.json is separate (estimates only).
Usage: python3 -I gate0_pool.py -> out/gate0_pool.json"""
import json
from collections import defaultdict
from pathlib import Path
D = Path(__file__).parent / "out"
rows = [json.loads(l) for l in open(D / "transcripts.jsonl")]
def setname(r):
    g, m, s = r["group"], r["model"].split("/")[-1], r["sub"]
    if "P44" in g:
        arm = s.split("/")[1] if "/" in s else s
        return f"P44 {m} arm {arm}" if not s.startswith("prefixes") else f"P44 {m} prefixes"
    if "P41" in g or "P37" in g:
        return f"{g} {m} (arms share prefixes)"
    return f"{g} {m}"
by = defaultdict(list)
for r in rows:
    by[setname(r)].append(r["final_ctx"])
out = {}
hdr = []
for W in (64000, 100000, 128000, 160000, 200000):
    for c in (20000, 40000):
        for k in (1, 2, 3):
            hdr.append((W, c, k))
for name, F in sorted(by.items()):
    res = {f"W{W//1000}k_c{c//1000}k_k{k}": sum(1 for f in F if f >= W + (k - 1) * (W - c)) for W, c, k in hdr}
    res["n"] = len(F)
    if res["W64k_c20k_k1"]:
        out[name] = res
json.dump(out, open(D / "gate0_pool.json", "w"), indent=1)
print(f"{'set':58s} {'n':>5s} | " + " ".join(f"{W//1000}k:c{c//1000}:k{k}" for W, c, k in hdr if c == 40000 and W in (64000, 100000, 128000)) + " | " + " ".join(f"{W//1000}k:c{c//1000}:k{k}" for W, c, k in hdr if c == 20000 and W in (100000,)))
for name, r in out.items():
    print(f"{name[:58]:58s} {r['n']:5d} | " + " ".join(f"{r[f'W{W//1000}k_c{c//1000}k_k{k}']:>9d}" for W, c, k in hdr if c == 40000 and W in (64000, 100000, 128000)) + " | " + " ".join(f"{r[f'W{W//1000}k_c{c//1000}k_k{k}']:>9d}" for W, c, k in hdr if c == 20000 and W in (100000,)))
