"""P49 per-session cost and wall-clock model for window-triggered repeated compaction.

Session: base context b (system + tools + task), context grows g tokens per turn, completion o
tokens per turn (reasoning + visible text + tool-call args; g includes o when reasoning is kept in
history, plus tool results). Compaction fires at threshold W and resets the context to the
carry-over c. K compactions, then a tail of `tail` turns after the last one.

Segment j starts at s_j (s_0 = b, else c) and runs n_j = ceil((W - s_j) / g) turns.
  prompt tokens of segment     P_j = n_j*s_j + g*n_j*(n_j-1)/2
  uncached under ideal caching U_j = s_j + (n_j - 1)*g   (first call of a segment is a miss; later
                                                         calls re-read the previous prompt)
  ideal cache-read tokens      C_j = P_j - U_j
Totals: T = sum n_j + tail, P = sum P_j (+ tail segment), C = sum C_j, O = T*o + K*s_out.
Compaction call k: input W + q (q = summary prompt, 1,000), output s_out (4,000 incl. reasoning).
  'hit'  : product style (same system prompt, summary request appended): W cache-read, q uncached
  'miss' : separate summariser prompt / transcript rendered as text: W + q uncached
Cost = p_in*(P - h*C) + p_cr*h*C + p_out*O + compaction cost; h = cache efficiency (1 ideal,
h_emp measured from P44/X3b logs, 0 no caching). Prices: OpenRouter endpoints queried
(out/or/endpoints_table.json). Wall-clock: T*(lat + o/tput + tool_s) + K*(lat + s_out/tput),
lat = latency_last_30m p50 (TTFT-like, measured on typical, mostly short prompts: a lower bound at
100k+ context), tput = throughput_last_30m p50 tok/s.
Usage: python3 -I cost_model.py -> out/cost_model.json and out/cost_model.md"""
import json, math
from pathlib import Path
D = Path(__file__).parent / "out"
EP = json.load(open(D / "or/endpoints_table.json"))
SPLIT = json.load(open(D / "per_turn_split.json"))

TOOL = 290  # tool-result tokens per turn on lcbhard (growth.py: P44 R 270, X3b GLM 294, P44 DS R 297)
glm = SPLIT["lcbhard / GLM 5.3 Flash (P44 R+PILOT, X3b main)"]
ds = SPLIT["lcbhard / DeepSeek V4 Flash (P44 R+PILOT, X3 wt)"]
swe = SPLIT["SWE-rebench OpenHands / Qwen3-Coder-480B (P01 slice A)"]
p38g = SPLIT["P38 ops world / GLM (a_full)"]
PROFILES = {
    # name: (g, o, source)
    "GLM-Flash kept-reasoning (measured, 12% capped turns)": (glm["total_mean_incl_capped"], glm["total_mean_incl_capped"] - TOOL, "per_turn_split.py"),
    "GLM-Flash kept-reasoning, no capped turns": (glm["total_mean_noncapped"], glm["total_mean_noncapped"] - TOOL, "per_turn_split.py"),
    "DS-V4-Flash kept-reasoning (measured)": (ds["total_mean_incl_capped"], ds["total_mean_incl_capped"] - TOOL, "per_turn_split.py"),
    "GLM-Flash reasoning stripped from history": (glm["nonreasoning_mean"], glm["total_mean_incl_capped"] - TOOL, "per_turn_split.py"),
    "DS-V4-Flash reasoning stripped from history": (ds["nonreasoning_mean"], ds["total_mean_incl_capped"] - TOOL, "per_turn_split.py"),
    "SWE-rebench OpenHands, no reasoning (Qwen3-Coder)": (swe["nonreasoning_mean"], 221, "per_turn_split.py; o = text 41 + args 180 (growth.py)"),
    "P38 ops world, GLM (measured)": (p38g["total_mean_incl_capped"], p38g["total_mean_incl_capped"] - 61, "per_turn_split.py"),
}
B, Q, S_OUT, K, TAIL, TOOL_S = 10_000, 1_000, 4_000, 3, 20, 2.0
WS = (100_000, 128_000, 160_000, 200_000)
H_EMP = None  # filled from cache_efficiency below

def session(g, o, W, c=30_000, b=B, k=K, tail=TAIL):
    segs = [b] + [c] * k
    T = 0; P = 0; C = 0; ns = []
    for j, s in enumerate(segs):
        n = tail if j == k else math.ceil((W - s) / g)
        ns.append(n)
        Pj = n * s + g * n * (n - 1) / 2
        Uj = s + (n - 1) * g
        T += n; P += Pj; C += Pj - Uj
    O = T * o + k * S_OUT
    return dict(T=T, n0=ns[0], n_between=ns[1], T_to_2=ns[0] + ns[1], T_to_3=ns[0] + 2 * ns[1], P=P, C=C, O=O,
                ideal_cache_frac=C / P)

def price(row, s, W, h, mode="hit", k=K):
    pin, pout = row["prompt"] / 1e6, row["completion"] / 1e6
    pcr = (row["cache_read"] if row["cache_read"] is not None else row["prompt"]) / 1e6
    if row["cache_read"] is None:
        h = 0.0
    turns = pin * (s["P"] - h * s["C"]) + pcr * h * s["C"] + pout * (s["O"] - k * S_OUT)
    if mode == "hit" and row["cache_read"] is not None:
        comp = k * (pcr * W + pin * Q + pout * S_OUT)
    else:
        comp = k * (pin * (W + Q) + pout * S_OUT)
    return turns + comp, comp

def wall(row, s, o, k=K):
    t = row["tput"] or {}; l = row["lat"] or {}
    tp = t.get("p50"); lat = (l.get("p50") or 0) / 1000
    if not tp:
        return None
    return (s["T"] * (lat + o / tp + TOOL_S) + k * (lat + S_OUT / tp)) / 3600

def find(model, provider, prompt=None, tag=None):
    c = [r for r in EP["rows"] if r["model"] == model and r["provider"] == provider
         and (prompt is None or abs(r["prompt"] - prompt) < 1e-3) and (tag is None or r["tag"] == tag)]
    if not c:
        raise KeyError((model, provider, prompt))
    return c[0]

# empirical cache efficiency (actual cached / ideal cached) from logged usage
import glob
M = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory"
EFF = {}
for name, pat in {"P44 GLM R, Together": M + "/probes/P44-handoff-methods-alignment/results/chains/glm-5.3-flash-tg/R/none/*.json",
                  "P44 GLM PILOT, Relace": M + "/probes/P44-handoff-methods-alignment/results/chains/glm-5.3-flash/PILOT/**/*.json",
                  "P44 DS R, Baidu": M + "/probes/P44-handoff-methods-alignment/results/chains/deepseek-v4-flash-bd/R/**/*.json",
                  "X3b GLM main, Relace": M + "/results/X3b/glm/episodes/main/**/*.json",
                  "X3b Gemini main, Google": M + "/results/X3b/gemini/episodes/main/**/*.json"}.items():
    act = ide = tot = 0
    for f in glob.glob(pat, recursive=True):
        try:
            mc = json.load(open(f))["model_calls"]
        except Exception:
            continue
        ins = [c.get("input_tokens") or 0 for c in mc]
        for i, c in enumerate(mc):
            tot += ins[i]; act += c.get("cached_tokens") or 0
            if i:
                ide += min(ins[i - 1], ins[i])
    EFF[name] = dict(actual_cached_frac=round(act / tot, 3), ideal_cached_frac=round(ide / tot, 3), efficiency=round(act / ide, 3))
H_EMP = round(sum(v["efficiency"] for k, v in EFF.items() if "Gemini" not in k) / 4, 2)

CANDS = [  # (label, model, provider, prompt price for disambiguation, profile)
    ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "Together", 0.15, "GLM-Flash kept-reasoning (measured, 12% capped turns)"),
    ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "CoreWeave", 0.15, "GLM-Flash kept-reasoning (measured, 12% capped turns)"),
    ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "Parasail", 0.1875, "GLM-Flash kept-reasoning (measured, 12% capped turns)"),
    ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "Relace", 0.04, "GLM-Flash kept-reasoning (measured, 12% capped turns)"),
    ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "StreamLake", 0.0435, "GLM-Flash kept-reasoning (measured, 12% capped turns)"),
    ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "Wafer", 0.12, "GLM-Flash kept-reasoning (measured, 12% capped turns)"),
    ("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", "Baidu", 0.14, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", "Relace", 0.05, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", "GMICloud", 0.091, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", "Parasail", 0.14, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4.1 Flash", "deepseek/deepseek-v4.1-flash", "Together", 0.3, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4.1 Flash", "deepseek/deepseek-v4.1-flash", "CoreWeave", 0.15, "DS-V4-Flash kept-reasoning (measured)"),
    ("GLM 5.3 (full)", "z-ai/glm-5.3", "Crusoe", 1.4, "DS-V4-Flash kept-reasoning (measured)"),
    ("GLM 5.3 (full)", "z-ai/glm-5.3", "Inceptron", 0.55, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", "Azure", 1.91, "DS-V4-Flash kept-reasoning (measured)"),
    ("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", "StreamLake", 0.9553, "DS-V4-Flash kept-reasoning (measured)"),
    ("Kimi K3", "moonshotai/kimi-k3", "Amazon Bedrock", 3.0, "DS-V4-Flash kept-reasoning (measured)"),
    ("Kimi K3", "moonshotai/kimi-k3", "InferenceNet", 0.39, "DS-V4-Flash kept-reasoning (measured)"),
    ("Qwen 3.7 Max", "qwen/qwen3.7-max", "Alibaba", 1.475, "DS-V4-Flash kept-reasoning (measured)"),
    ("Qwen 3.7 Plus", "qwen/qwen3.7-plus", "Alibaba", 0.32, "DS-V4-Flash kept-reasoning (measured)"),
    ("MiniMax M3", "minimax/minimax-m3", "ModelRun", 0.3, "DS-V4-Flash kept-reasoning (measured)"),
    ("Gemini 3.8 Flash", "google/gemini-3.8-flash", "Google AI Studio", 0.75, "DS-V4-Flash kept-reasoning (measured)"),
    ("Claude Haiku 5.5", "anthropic/claude-haiku-5.5", "Anthropic", 0.1, "DS-V4-Flash kept-reasoning (measured)"),
    ("gpt-oss-120b (131k ctx: W<=100k only)", "openai/gpt-oss-120b", "Groq", 0.15, "DS-V4-Flash kept-reasoning (measured)"),
]

res = dict(params=dict(b=B, q=Q, s_out=S_OUT, K=K, tail=TAIL, tool_s=TOOL_S, c_main=30_000, Ws=WS, queried_utc=EP["queried_utc"]),
           cache_efficiency=EFF, h_emp=H_EMP, profiles={}, sessions=[])
L = []
L.append(f"Parameters: b={B}, carry-over c=30k (20k/40k sensitivity below), K={K} compactions, tail={TAIL} turns after the 3rd, summary prompt q={Q}, summary output s_out={S_OUT} (incl. reasoning), tool exec {TOOL_S}s/turn. Prices queried {EP['queried_utc']}.")
L.append("\n### Turns and tokens per session, by growth profile\n")
L.append("| profile | g tok/turn | o tok/turn | W | turns to 1st compaction | turns between | turns to 2nd | turns to 3rd | session turns (3 comp + 20) | prompt tokens (M) | ideal cache-read share |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|")
for pn, (g, o, src) in PROFILES.items():
    res["profiles"][pn] = dict(g=g, o=o, source=src)
    for W in WS:
        s = session(g, o, W)
        L.append(f"| {pn} | {g:.0f} | {o:.0f} | {W//1000}k | {s['n0']} | {s['n_between']} | {s['T_to_2']} | {s['T_to_3']} | {s['T']} | {s['P']/1e6:.2f} | {s['ideal_cache_frac']:.3f} |")
L.append("\n### Carry-over sensitivity (GLM measured profile and DS measured profile): session turns / prompt M tokens\n")
L.append("| profile | W | c=20k | c=30k | c=40k |")
L.append("|---|---|---|---|---|")
for pn in ("GLM-Flash kept-reasoning (measured, 12% capped turns)", "DS-V4-Flash kept-reasoning (measured)", "DS-V4-Flash reasoning stripped from history"):
    g, o, _ = PROFILES[pn]
    for W in WS:
        cells = []
        for c in (20_000, 30_000, 40_000):
            s = session(g, o, W, c=c); cells.append(f"{s['T']} / {s['P']/1e6:.2f}")
        L.append(f"| {pn} | {W//1000}k | " + " | ".join(cells) + " |")
L.append(f"\n### $ per session (3 compactions + 20-turn tail), ideal prefix caching (h=1), compaction prompt a cache hit\n")
L.append(f"Columns W=100k..200k: $/session at h=1. Then W=128k at h_emp={H_EMP} (measured efficiency) and at h=0 (no caching), the compaction-miss surcharge at 128k (3 calls), and wall-clock hours at W=128k.\n")
L.append("| model | provider (quant) | ctx | $in | $out | $cache-read | tok/s p50 | lat p50 s | profile | 100k | 128k | 160k | 200k | 128k h_emp | 128k no-cache | 128k compaction miss extra | 128k wall-clock h |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for label, model, prov, pr, pn in CANDS:
    row = find(model, prov, pr)
    g, o, _ = PROFILES[pn]
    cells = []
    for W in WS:
        if row["ctx"] < W + 20_000:
            cells.append("n/a (ctx)"); continue
        s = session(g, o, W); cost, comp = price(row, s, W, 1.0)
        cells.append(f"{cost:.2f}")
    if row["ctx"] >= 148_000:
        s = session(g, o, 128_000)
        ce, _ = price(row, s, 128_000, H_EMP); c0, _ = price(row, s, 128_000, 0.0)
        ch, comp_hit = price(row, s, 128_000, 1.0); cm, comp_miss = price(row, s, 128_000, 1.0, mode="miss")
        wc = wall(row, s, o)
        extra = [f"{ce:.2f}", f"{c0:.2f}", f"{comp_miss - comp_hit:.3f}", f"{wc:.1f}" if wc else "-"]
    else:
        s = session(g, o, 100_000); wc = wall(row, s, o)
        extra = ["n/a", "n/a", "n/a", f"{wc:.1f} (at 100k)" if wc else "-"]
    t = (row["tput"] or {}).get("p50"); l = (row["lat"] or {}).get("p50")
    cr = row["cache_read"]
    L.append(f"| {label} | {prov} ({row['quant']}) | {row['ctx']//1000}k | {row['prompt']:.3g} | {row['completion']:.3g} | {cr if cr is None else f'{cr:.3g}'} | {t} | {l/1000 if l else '-'} | {pn.split(' (')[0]} | " + " | ".join(cells) + " | " + " | ".join(extra) + " |")
    res["sessions"].append(dict(label=label, model=model, provider=prov, profile=pn, cells=cells, extra=extra))
L.append("\n### Profile sensitivity for the two primary models at W=128k (h=1, compaction hit)\n")
L.append("| model/provider | profile | session turns | $ / session | wall-clock h |")
L.append("|---|---|---|---|---|")
for label, model, prov, pr in (("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "Together", 0.15), ("GLM 5.3 Flash", "z-ai/glm-5.3-flash", "CoreWeave", 0.15),
                               ("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash", "Baidu", 0.14)):
    row = find(model, prov, pr)
    for pn, (g, o, _) in PROFILES.items():
        if ("GLM" in label) != ("GLM" in pn) and "SWE" not in pn:
            continue
        if "P38" in pn and "GLM" not in label:
            continue
        s = session(g, o, 128_000); cost, _ = price(row, s, 128_000, 1.0); wc = wall(row, s, o)
        L.append(f"| {label} / {prov} | {pn} | {s['T']} | {cost:.2f} | {wc:.1f} |")
L.append("\n### Gate 0: price of one summariser call over a W-token transcript (cache miss: fresh prompt), s_out=4k\n")
L.append("| model/provider | W=64k | 100k | 128k | 160k | 200k |")
L.append("|---|---|---|---|---|---|")
for label, model, prov, pr, pn in CANDS:
    row = find(model, prov, pr)
    cells = []
    for W in (64_000, 100_000, 128_000, 160_000, 200_000):
        if row["ctx"] < W + 8_000:
            cells.append("n/a"); continue
        cells.append(f"{(row['prompt'] * (W + Q) + row['completion'] * S_OUT) / 1e6:.4f}")
    L.append(f"| {label} / {prov} | " + " | ".join(cells) + " |")
L.append("\n### Measured cache efficiency in earlier probes (logged cached_tokens vs ideal prefix reuse)\n")
L.append("| set | actual cached share of prompt tokens | ideal share | efficiency |")
L.append("|---|---|---|---|")
for k, v in EFF.items():
    L.append(f"| {k} | {v['actual_cached_frac']} | {v['ideal_cached_frac']} | {v['efficiency']} |")
(D / "cost_model.md").write_text("\n".join(L) + "\n")
json.dump(res, open(D / "cost_model.json", "w"), indent=1)
print("\n".join(L))
