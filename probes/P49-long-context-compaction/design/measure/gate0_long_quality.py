"""Quality of the existing transcripts long enough for >= 2 compactions at W=100k (F >= 160k):
share of final context that is capped reasoning (output >= 15,990 tokens, i.e. reasoning ran to
the 16k max_tokens cap), number of capped turns, end_reason, n_turns, whether reasoning blocks are
stored, and whether the task's test constraint text appears in the first user message.
Usage: python3 -I gate0_long_quality.py -> out/gate0_long_quality.json"""
import json, statistics
from pathlib import Path
D = Path(__file__).parent / "out"
rows = [json.loads(l) for l in open(D / "transcripts.jsonl")]
pick = [r for r in rows if r["final_ctx"] >= 160000 and ("X3b" in r["group"] or ("P44" in r["group"] and r["sub"].split("/")[-1] in ("R", "PILOT")) or "x3-build" in r["group"])]
out = []
for r in pick:
    d = json.load(open(r["path"]))
    mc = d["model_calls"]
    outs = [c.get("output_tokens") or 0 for c in mc]
    cap = [o for o in outs if o >= 15990]
    msgs = d.get("messages") or []
    first_user = next((m.get("content") for m in msgs if m.get("role") == "user"), "") or ""
    if isinstance(first_user, list):
        first_user = " ".join(b.get("text", "") for b in first_user if isinstance(b, dict))
    vis_empty = 0
    for m in msgs:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            txt = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
            rs = "".join(b.get("reasoning", "") or "" for b in m["content"] if b.get("type") == "reasoning")
            if len(rs) > 40000 and not txt.strip() and not m.get("tool_calls"):
                vis_empty += 1
    out.append(dict(path=r["path"].split("mats12_neel_exploratory/")[1], group=r["group"], sub=r["sub"], final_ctx=r["final_ctx"],
                    n_turns=d.get("n_turns"), end_reason=d.get("end_reason"), capped_turns=len(cap),
                    capped_share=round(sum(cap) / r["final_ctx"], 3), empty_visible_capped_msgs=vis_empty,
                    constraint_in_prompt=("DO NOT MODIFY" in first_user.upper() or "NOT MODIFY THE TEST" in first_user.upper()),
                    reasoning_stored=r["reasoning_msgs"] > 0))
json.dump(out, open(D / "gate0_long_quality.json", "w"), indent=1)
print("n", len(out))
print("capped_share median", statistics.median(o["capped_share"] for o in out), "min", min(o["capped_share"] for o in out), "max", max(o["capped_share"] for o in out))
print("capped turns median", statistics.median(o["capped_turns"] for o in out))
print("non-capped content median", statistics.median(o["final_ctx"] * (1 - o["capped_share"]) for o in out), "max", max(o["final_ctx"] * (1 - o["capped_share"]) for o in out))
from collections import Counter
print("end", Counter(o["end_reason"] for o in out)); print("turns med", statistics.median(o["n_turns"] or 0 for o in out))
print("constraint in prompt", sum(o["constraint_in_prompt"] for o in out), "reasoning stored", sum(o["reasoning_stored"] for o in out))
print("empty-visible capped msgs median", statistics.median(o["empty_visible_capped_msgs"] for o in out))
