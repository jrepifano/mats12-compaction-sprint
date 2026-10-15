"""How much of the long GLM/DS contexts comes from turns whose output hit the max_tokens cap
(16,000 in X3/X3b/P44: reasoning ran to the cap, usually with empty visible text)?
Uses logged API usage only (model_calls: output_tokens, reasoning_tokens, input_tokens) and the
agent-call sequence. Also reports the per-call prompt growth for non-capped turns (API deltas
between consecutive agent calls whose previous call was not capped) and the empirical cache
hit fraction (cached_tokens / input_tokens) per provider.
Usage: python3 -I capped_turns.py"""
import glob, json, statistics
from collections import defaultdict
M = "/Users/jacob/Documents/GitHub/mats12_neel_exploratory"
SETS = {
    "P44 GLM R (Together)": M + "/probes/P44-handoff-methods-alignment/results/chains/glm-5.3-flash-tg/R/none/*.json",
    "P44 GLM PILOT (Relace)": M + "/probes/P44-handoff-methods-alignment/results/chains/glm-5.3-flash/PILOT/**/*.json",
    "P44 DS R (Baidu)": M + "/probes/P44-handoff-methods-alignment/results/chains/deepseek-v4-flash-bd/R/**/*.json",
    "X3b GLM main": M + "/results/X3b/glm/episodes/main/**/*.json",
    "P41 GLM all": M + "/probes/P41-grader-belief-atrisk/results/**/*.json",
}
CAP = 15990
def q(xs, p):
    xs = sorted(xs); k = (len(xs) - 1) * p; lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo) if xs else float("nan")
out = {}
for name, pat in SETS.items():
    capped_out = 0; tot_final = 0; n = 0; deltas = []; deltas_after_cap = []; prov = defaultdict(lambda: [0, 0])
    capped_eps = 0; finals_nocap = []; finals = []
    for f in glob.glob(pat, recursive=True):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        mc = d.get("model_calls")
        if not isinstance(mc, list) or not mc:
            continue
        n += 1
        ins = [c.get("input_tokens") or 0 for c in mc]
        outs = [c.get("output_tokens") or 0 for c in mc]
        for c in mc:
            prov[c.get("provider")][0] += c.get("input_tokens") or 0
            prov[c.get("provider")][1] += c.get("cached_tokens") or 0
        # agent-call chain: keep calls whose input is >= running max - 2k (forks/beliefs re-use ctx
        # and are dropped from the chain because the next agent call doesn't include their output)
        fin = max(i + o for i, o in zip(ins, outs)); finals.append(fin)
        cap_tok = sum(o for o in outs if o >= CAP)
        capped_out += cap_tok; tot_final += fin
        if cap_tok:
            capped_eps += 1
        finals_nocap.append(fin - cap_tok)
        for k in range(1, len(mc)):
            dlt = ins[k] - ins[k - 1]
            if dlt <= 0:
                continue  # fork/belief call or a dropped belief turn
            (deltas_after_cap if outs[k - 1] >= CAP else deltas).append(dlt)
    out[name] = dict(n=n, episodes_with_capped_turn=capped_eps,
                     share_of_final_ctx_from_capped_outputs=round(capped_out / max(1, tot_final), 3),
                     final_med=q(finals, .5), final_p90=q(finals, .9),
                     final_minus_capped_med=q(finals_nocap, .5), final_minus_capped_p90=q(finals_nocap, .9),
                     final_minus_capped_max=max(finals_nocap),
                     delta_noncapped_mean=statistics.mean(deltas), delta_noncapped_med=statistics.median(deltas),
                     delta_after_capped_med=statistics.median(deltas_after_cap) if deltas_after_cap else None,
                     cache_hit_by_provider={p: round(v[1] / v[0], 3) for p, v in prov.items() if v[0]})
    print(name, json.dumps(out[name]))
json.dump(out, open(__file__.rsplit("/", 1)[0] + "/out/capped_turns.json", "w"), indent=1)
