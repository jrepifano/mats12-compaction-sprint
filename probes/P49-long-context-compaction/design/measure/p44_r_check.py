"""Verify the brief's P44 no-handoff (R arm) context figures (median 56k, p90 163k, max 232k)
from logged API usage: per chain, max prompt tokens over all calls (max_in) and max
prompt+completion (final_ctx). Quantiles: linear interpolation and nearest-rank.
Usage: python3 -I p44_r_check.py"""
import json, math
from pathlib import Path
rows = [json.loads(l) for l in open(Path(__file__).parent / "out/transcripts.jsonl")]
for arm_model in (("glm-5.3-flash-tg", "R"), ("deepseek-v4-flash-bd", "R")):
    rs = [r for r in rows if "P44" in r["group"] and r["sub"] == "/".join(arm_model)]
    for key in ("max_in", "final_ctx"):
        xs = sorted(r[key] for r in rs); n = len(xs)
        lin = lambda p: xs[int((n - 1) * p)] + (xs[min(int((n - 1) * p) + 1, n - 1)] - xs[int((n - 1) * p)]) * ((n - 1) * p - int((n - 1) * p))
        nr = lambda p: xs[max(0, math.ceil(p * n) - 1)]
        print(arm_model, key, "n", n, "median", xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2,
              "p90 lin", round(lin(.9)), "p90 nearest-rank", nr(.9), "max", xs[-1],
              "turns med", sorted(r["n_turns"] for r in rs)[n // 2])
