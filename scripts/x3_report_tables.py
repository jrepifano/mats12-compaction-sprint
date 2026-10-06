"""Prereg §7.4 report tables (free): per-context rates with 95% Wilson intervals, cheat-and-conceal
by context (both validated disclosure judges), cost caps; GLM main-run per-episode cost, token
and turn distributions; and 10 main-run transcripts drawn at random (seed 20261010), rendered to
data/X3/report_transcripts.md. Writes results/X3/report_tables.json.
"""
from __future__ import annotations

import csv
import json
import statistics

import numpy as np

from x3 import checkpoint as CP
from x3 import contracts as C
from x3 import judges as J
from x3.analysis import wilson_ci

SEED = 20261010
JUDGES = ("openai_gpt-6-luna", "google_gemini-3.5-flash-lite")
ORDER = ("neutral", "teammate", "cap_script", "cap_human", "safe_script", "safe_human")


def disclosure(r: dict, label: str):
    v = [r.get(f"disclosure__{j}__{label}") for j in JUDGES]
    if any(x in ("", None) for x in v):
        return None
    return (v[0] == "True") if len(set(v)) == 1 else "split"


def p(xs, q):
    return float(np.percentile(xs, q)) if xs else None


def main() -> None:
    with open(C.EPISODES_CSV, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["stage"] == "main"]
    ctx = {}
    for c in ORDER:
        rs = [r for r in rows if r["context"] == c]
        cheats = [r for r in rs if r["cheat"] == "True"]
        lo, hi = wilson_ci(len(cheats), len(rs))
        conceal = no_msg = 0
        for r in cheats:
            w, f = disclosure(r, "states_workaround"), disclosure(r, "states_failing_test")
            if w is None or f is None:
                no_msg += 1
            elif w is False and f is False:
                conceal += 1
        ctx[c] = {"n": len(rs), "cheats": len(cheats), "rate": len(cheats) / len(rs),
                  "wilson95": [lo, hi], "cheat_and_conceal": conceal,
                  "cheat_no_final_message": no_msg,
                  "cost_capped": sum(r["end_reason"] == "cost_cap" for r in rs)}
    arts = [a for a in CP._arts(C.EPISODES_DIR) if a["meta"]["stage"] == "main"]
    cost = [sum(c["billed_usd"] or 0 for c in a["model_calls"]) for a in arts]
    tin = [sum(c["input_tokens"] or 0 for c in a["model_calls"]) for a in arts]
    tout = [sum(c["output_tokens"] or 0 for c in a["model_calls"]) for a in arts]
    turns = [a.get("n_turns") or 0 for a in arts]
    dist = {k: {"median": statistics.median(v), "p90": p(v, 90), "mean": statistics.fmean(v)}
            for k, v in (("billed_usd_known", cost), ("input_tokens", tin),
                         ("output_tokens", tout), ("turns", turns))}
    rng = np.random.default_rng(SEED)
    pick = sorted(rng.choice(len(arts), 10, replace=False))
    det = {r["episode_id"]: r for r in rows}
    out_md = ["# X3 main run: 10 transcripts drawn at random", "",
              (f"Seed {SEED}, drawn from the 360 GLM 5.3 Flash main-run episodes "
               "(`scripts/x3_report_tables.py`). Each is the complete transcript: the run "
               "header and task, every reasoning block, message, tool call and tool result "
               "(tool output was capped at 8,000 characters per call during the episode), "
               "then the post-episode answer."), ""]
    for k, i in enumerate(pick, 1):
        a = arts[int(i)]
        m = a["meta"]
        r = det[m["episode_id"]]
        # the complete transcript (report review r2): every message, reasoning, tool call and
        # tool result as the episode recorded it (tool output was capped at 8,000 characters
        # per call in the episode itself), then the post-episode answer
        out_md += [f"## {k}. `{m['episode_id']}`", "",
                   (f"- Context: **{m['context']}**; end: {a.get('end_reason')}; turns: "
                    f"{a.get('n_turns')}; cheat: **{r['cheat']}**; Q1 {r['q1']}, Q2 {r['q2']}"),
                   "", "Transcript:", "", "````text", J.render_transcript(a), "````", "",
                   "Post-episode answer:", "", "````text",
                   (a.get("probe") or {}).get("reply") or "", "````", ""]
    (C.ROOT / "data" / "X3" / "report_transcripts.md").write_text("\n".join(out_md))
    out = {"contexts": ctx, "glm_main_distributions": dist, "transcript_seed": SEED,
           "transcripts": [arts[int(i)]["meta"]["episode_id"] for i in pick]}
    (C.RESULTS_DIR / "report_tables.json").write_text(json.dumps(out, indent=1))
    for c, v in ctx.items():
        print(f"{c:12s} {v['cheats']:2d}/{v['n']} {v['rate']:.1%} [{v['wilson95'][0]:.1%}, "
              f"{v['wilson95'][1]:.1%}] conceal {v['cheat_and_conceal']} no-msg "
              f"{v['cheat_no_final_message']} capped {v['cost_capped']}")
    print(json.dumps(dist, indent=1))
    print("transcripts:", out["transcripts"])


if __name__ == "__main__":
    main()
