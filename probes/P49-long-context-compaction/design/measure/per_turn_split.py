"""Per-turn token split (reasoning vs non-reasoning) per model, from stored messages,
with turns whose reasoning reached the 16k output cap (>= 15,000 tokenized) separated out.
Tokenizer: DeepSeek-V4-Flash (proxy, see growth.py). Uses growth_lib.py's parser.
Usage: uv run --project <worktree> python -I per_turn_split.py   -> out/per_turn_split.json"""
import glob, json, statistics, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import growth_lib as G
M = G.M; W = G.W
SETS = {
    "lcbhard / GLM 5.3 Flash (P44 R+PILOT, X3b main)": [G.SETS["P44 GLM R (no handoff)"], G.SETS["P44 GLM PILOT (no handoff, Relace)"], G.SETS["X3b GLM main"]],
    "lcbhard / DeepSeek V4 Flash (P44 R+PILOT, X3 wt)": [G.SETS["P44 DS R (no handoff)"], G.SETS["P44 DS PILOT"], W + "/results/X3/**/deepseek*/**/*.json"],
    "lcbhard / Gemini 3.8 Flash (X3b main)": [G.SETS["X3b Gemini main"]],
    "lcbhard / Qwen3.6-27B (X3 wt, P47)": [W + "/results/X3/**/qwen*/**/*.json", G.SETS["P47 qwen3.6-27b"]],
    "lcbhard / gpt-oss-120b (P47)": [G.SETS["P47 gpt-oss-120b"]],
    "P38 ops world / GLM (a_full)": [G.SETS["P38 GLM a_full"]],
    "P38 ops world / DS (a_full)": [G.SETS["P38 DS a_full"]],
    "SWE-rebench OpenHands / Qwen3-Coder-480B (P01 slice A)": [G.SETS["P01 slice A (SWE-rebench, Qwen3-Coder-480B)"]],
}
CAPT = 15000
res = {}
for name, pats in SETS.items():
    R = []; NR = []; capped = 0; nturn = 0; ntr = 0; seen = set()
    for pat in pats:
        for tid, msgs, api in G.load(pat):
            if tid in seen:
                continue
            seen.add(tid); ntr += 1
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role, cat = G.parse_msg(m)
                if role == "assistant":
                    r = G.ntok(cat["reasoning"]); nturn += 1
                    if r >= CAPT:
                        capped += 1
                    else:
                        R.append(r)
                    NR.append(G.ntok(cat["text"]) + G.ntok(cat["args"]))
                elif role == "tool":
                    if NR:
                        NR[-1] += G.ntok(cat["tool"])
    res[name] = dict(transcripts=ntr, turns=nturn, capped_turns=capped, capped_share=round(capped / max(1, nturn), 3),
                     reasoning_mean_noncapped=round(statistics.mean(R), 1) if R else 0,
                     reasoning_med_noncapped=statistics.median(R) if R else 0,
                     nonreasoning_mean=round(statistics.mean(NR), 1), nonreasoning_med=statistics.median(NR),
                     total_mean_incl_capped=round((sum(R) + capped * 16000 + sum(NR)) / max(1, nturn), 1),
                     total_mean_noncapped=round(statistics.mean(R) + statistics.mean(NR), 1) if R else round(statistics.mean(NR), 1))
    print(name, json.dumps(res[name]))
json.dump(res, open(Path(__file__).parent / "out/per_turn_split.json", "w"), indent=1)
