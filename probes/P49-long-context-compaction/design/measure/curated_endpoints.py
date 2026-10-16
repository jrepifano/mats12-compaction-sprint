"""Rank every queried OpenRouter endpoint per model by $/session and by wall-clock for the W=128k,
c=30k, 3-compaction session of cost_model.py (GLM 5.3 Flash: its measured profile; all others:
the DeepSeek V4 Flash measured profile). Eligible: tools and reasoning supported, context >= 160k,
uptime_last_30m >= 95 (or null) and a throughput figure. Usage: python3 -I curated_endpoints.py
-> out/curated_endpoints.md"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import importlib
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    import cost_model as CM
L = ["| model | rank by | provider (tag) | quant | $in / $out / $cache-read per M | tok/s p50 | latency p50 s | uptime 30m / 1d | max_out | $/session W=128k (h=1) | same, h=0.82 | wall-clock h |",
     "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for model in dict.fromkeys(r["model"] for r in CM.EP["rows"]):
    pn = "GLM-Flash kept-reasoning (measured, 12% capped turns)" if model == "z-ai/glm-5.3-flash" else "DS-V4-Flash kept-reasoning (measured)"
    g, o, _ = CM.PROFILES[pn]
    s = CM.session(g, o, 128_000)
    el = []
    for r in CM.EP["rows"]:
        if r["model"] != model or not r["tools"] or not r["reasoning"] or (r["ctx"] or 0) < 160_000:
            continue
        if r["uptime"] is not None and r["uptime"] < 95:
            continue
        if not (r["tput"] or {}).get("p50"):
            continue
        cost, _ = CM.price(r, s, 128_000, 1.0); ce, _ = CM.price(r, s, 128_000, CM.H_EMP)
        el.append((cost, ce, CM.wall(r, s, o), r))
    if not el:
        L.append(f"| {model} | - | no eligible endpoint (ctx < 160k, no tools, or low uptime) | | | | | | | | | |"); continue
    picks = [("cost", x) for x in sorted(el, key=lambda x: x[0])[:3]] + [("speed", x) for x in sorted(el, key=lambda x: x[2])[:3]]
    seen = set()
    for how, (cost, ce, wc, r) in picks:
        key = (r["provider"], r["tag"])
        if key in seen:
            continue
        seen.add(key)
        cr = "-" if r["cache_read"] is None else f"{r['cache_read']:.3g}"
        L.append(f"| {model} | {how} | {r['provider']} ({r['tag']}) | {r['quant']} | {r['prompt']:.3g} / {r['completion']:.3g} / {cr} | {r['tput']['p50']} | {(r['lat'] or {}).get('p50', 0)/1000:.2f} | {r['uptime'] and round(r['uptime'])} / {r['uptime_1d'] and round(r['uptime_1d'])} | {r['max_out']} | {cost:.2f} | {ce:.2f} | {wc:.1f} |")
(Path(__file__).parent / "out/curated_endpoints.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
