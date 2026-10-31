"""P49 (offline revision, 2026-10-15): projected API spend, call counts and bulk wall-clock.

The v3 request inventory (live on-call world) is in git history (commit 77abd5fa).
Measured inputs:
- per-trajectory pi summarised span at each cut (2,000-char tool truncation), from
  design/measure/out/swe_long_detail.json (DeepSeek-V4-Flash tokenizer, 193 trajectories);
- output tokens per summary call and empty-summary rates from P01's ledger
  (results/P01/summaries/{glm,dsk}.usage.jsonl, summed over every batch and status: GLM 714 calls,
  4,632 completion tokens/call, 10.6% empty; DeepSeek 757, 2,239, 5.3%; P01 inputs were ~20k).
  These are aggregate ledger rates, not first-attempt rates; they are scaled for longer inputs
  (out_scale 1.0/1.3/2.0);
- prices and throughput from OpenRouter /endpoints, queried 2026-10-15T19:52Z
  (design/measure/out/or/endpoints_table.json).
Assumptions (not measured): pi's 1.3 calls per compaction (split turn), the recursive and P40
input sizes, wrapper prompt overhead, judge lengths (6k in, 800 out), 2 s latency + 10 s prefill.
Arms per trajectory x wrapper x summariser: L (128k-window trigger), S1..S3 (the same rule at a
40k trigger, recursive), P40 (L's cut, history before it cut to its last ~40k). Every call is
priced cold (no cache credit); each empty first attempt is priced as one full retry.
Run: python3 -I budget.py [n_trajectories]"""
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).parent
rows = json.loads((HERE / "measure/out/swe_long_detail.json").read_text())["rows"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 96

PRICE = {  # $/M tokens: in, out (pinned provider)
    "glm": (0.0435, 0.145),   # z-ai/glm-5.3-flash on StreamLake fp8 (29 tok/s, uptime 99% 1d)
    "glm_fast": (0.108, 0.55),  # alternative: Inceptron fp8 (59 tok/s, uptime 100% 1d)
    "dsk": (0.03836, 0.077),  # deepseek/deepseek-v4-flash on Baidu fp8 (65 tok/s)
    "haiku": (0.10, 0.50),    # anthropic/claude-haiku-5.5 on Google Vertex global (155 tok/s)
}
TPS = {"glm": 29, "dsk": 65, "glm_fast": 59}
OUT = {"glm": 4632, "dsk": 2239}       # P01 completion tokens per summary call
EMPTY = {"glm": 0.106, "dsk": 0.053}   # P01 first-attempt empty rate -> one retry each
PROMPT = 1_500                         # wrapper prompt + framing tokens
PI_CALLS = 1.3                         # pi's split-turn path can add a second call
CTX_L = 115_200 + 1_500                # first request at or over the trigger, + overshoot
PI_L = statistics.mean(r["L"]["pi_span_tok"] for r in rows)
PI_S1 = statistics.mean(r["S1"]["pi_span_tok"] for r in rows)
RATIO = PI_L / (CTX_L - 21_000 - 5_000)  # pi tokens per raw token (tail ~21k, system+task ~5k)
ARMS = ("L", "S1", "S2", "S3", "P40")


def inputs():
    """Mean summariser input tokens per call: {arm: {wrapper: tokens}}."""
    s_k = {"codex": 40_000, "inspect": 40_000, "pi": 3_000 + RATIO * 12_000}
    return {
        "L": {"codex": CTX_L, "inspect": CTX_L, "pi": PI_L},
        "S1": {"codex": 40_000, "inspect": 40_000, "pi": PI_S1},
        "S2": s_k, "S3": s_k,
        "P40": {"codex": 40_000, "inspect": 40_000, "pi": RATIO * 20_000},
    }


def summaries(n, out_scale=1.3):
    cost, calls, tok_out = {}, {}, {}
    for m in ("glm", "dsk"):
        c = k = o = 0.0
        for ws in inputs().values():
            for w, tin in ws.items():
                per = (PI_CALLS if w == "pi" else 1.0) * (1 + EMPTY[m])
                out = OUT[m] * out_scale
                c += n * per * ((tin + PROMPT) * PRICE[m][0] + out * PRICE[m][1]) / 1e6
                k += n * per
                o += n * per * out
        cost[m], calls[m], tok_out[m] = c, k, o
    return cost, calls, tok_out


def judge(n_summaries, extra=True):
    """Haiku: one call per summary (summary ~3k + facts ~1.5k + rubric 1.5k in, 800 out), a 10%
    repeat for stability, plus validation (120 planted + 100 natural items)."""
    items = n_summaries * 1.1 + (220 if extra else 0)
    return items * (6_000 * PRICE["haiku"][0] + 800 * PRICE["haiku"][1]) / 1e6, items


def report(n, out_scale=1.3):
    cost, calls, tok_out = summaries(n, out_scale)
    j, items = judge(n * 2 * len(ARMS) * 3)
    smoke = sum(summaries(2, out_scale)[0].values()) + judge(2 * 2 * len(ARMS) * 3, False)[0]
    total = sum(cost.values()) + j + smoke
    return dict(n=n, glm=cost["glm"], dsk=cost["dsk"], judge=j, smoke=smoke, total=total,
                calls=calls, judge_items=items, tok_out=tok_out)


def hours(r, conc=48, lat_s=2.0, prefill_s=10.0):
    """Lower-bound bulk wall-clock per model at `conc` calls in flight per model (per call:
    latency + prefill + output / throughput). S2 and S3 wait for S1 and S2 within a trajectory."""
    out = {}
    for m in ("glm", "dsk"):
        per_call = lat_s + prefill_s + r["tok_out"][m] / r["calls"][m] / TPS[m]
        out[m] = max(r["calls"][m] * per_call / conc, 3 * per_call) / 3600
    return out


if __name__ == "__main__":
    print(f"pool {len(rows)} trajectories; pi tokens per raw token {RATIO:.2f}; "
          f"mean pi span L {PI_L:,.0f}, S1 {PI_S1:,.0f}")
    for n in (N, 64, 128, 193):
        for s in (1.0, 1.3, 2.0):
            r = report(n, s)
            h = hours(r)
            print(f"N={n:3d} out x{s}: GLM ${r['glm']:.2f}  DS ${r['dsk']:.2f}  judge ${r['judge']:.2f}"
                  f"  smoke ${r['smoke']:.2f}  TOTAL ${r['total']:.2f} | calls GLM {r['calls']['glm']:.0f}"
                  f" DS {r['calls']['dsk']:.0f} judge {r['judge_items']:.0f}"
                  f" | bulk h (48 in flight): GLM {h['glm']:.1f} DS {h['dsk']:.1f}")
    fast = dict(PRICE, glm=PRICE["glm_fast"])
    PRICE.update(fast)
    TPS["glm"] = TPS["glm_fast"]
    r = report(N, 1.3)
    print(f"alternative GLM on Inceptron, N={N} out x1.3: GLM ${r['glm']:.2f} TOTAL ${r['total']:.2f}"
          f" | bulk h (48 in flight): GLM {hours(r)['glm']:.1f}")
