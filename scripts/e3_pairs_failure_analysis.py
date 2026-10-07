"""Descriptive analysis of the failed v1 answer-pair build (E3 stop report, 2026-10-06).

Rebuilds the seed draw offline (no paid calls) and compares each pair's seed answer with its
cached round-0 counterpart: token counts under the scoring tokenizer, the length ratio, and the
80-token cap. Also writes a random sample (seed 20261006, not cherry-picked) of length-failed pairs
for the results review. Inputs: data/shared/answer_pairs_v1.build.json and its call cache.
"""

from __future__ import annotations

import json
import random
import statistics as st
from collections import Counter

from emx.e3 import contracts as C
from emx.shared import answer_pairs as A

OUT = "results/E3/pairs_build/failure_analysis.json"


def main() -> None:
    b = A.Builder(A.BuildConfig(), log=lambda s: None)
    b.prepare(write=False)
    calls = [json.loads(x) for x in open("data/shared/answer_pairs_v1.cache/calls.jsonl")]
    writer = {(c["meta"]["pair_id"], c["stage"]): c["out"]["text"]
              for c in calls if c["stage"].startswith("writer")}
    report = json.load(open("data/shared/answer_pairs_v1.build.json"))
    status = {s["pair_id"]: s for s in report["slots"]}
    rows = []
    for qid, cands in b.candidates.items():
        for i, s in enumerate(cands[: C.PAIRS_PER_QUESTION]):
            pid = f"{qid}#{i}"
            cp = writer.get((pid, "writer:r0"))
            if cp is None:
                continue
            ns, nc = b.count_tokens(s.text), b.count_tokens(cp)
            rows.append({"pair_id": pid, "qset": b.qset_of[qid], "seed_role": s.role,
                         "seed_tokens": ns, "counterpart_tokens": nc, "ratio": nc / ns,
                         "over_cap": max(ns, nc) > C.MAX_ANSWER_TOKENS,
                         "ratio_ok": C.LENGTH_RATIO[0] <= ns / nc <= C.LENGTH_RATIO[1]
                         if s.role == "misaligned" else C.LENGTH_RATIO[0] <= nc / ns
                         <= C.LENGTH_RATIO[1],
                         "final_status": status.get(pid, {}).get("status"),
                         "final_reasons": status.get(pid, {}).get("reasons"),
                         "question": b.qtext[qid], "seed": s.text, "counterpart_r0": cp})
    ratios = [r["ratio"] for r in rows]
    q = sorted(ratios)
    summary = {
        "pairs_with_r0_counterpart": len(rows),
        "counterpart_over_seed_tokens": {"median": st.median(ratios), "p10": q[len(q) // 10],
                                         "p90": q[9 * len(q) // 10]},
        "seed_tokens_median": st.median(r["seed_tokens"] for r in rows),
        "counterpart_tokens_median": st.median(r["counterpart_tokens"] for r in rows),
        "r0_length_fail": sum(1 for r in rows if r["over_cap"] or not r["ratio_ok"]),
        "r0_over_cap": sum(1 for r in rows if r["over_cap"]),
        "by_qset_role_overcap_ratiook": {
            f"{k[0]}|{k[1]}|over_cap={k[2]}|ratio_ok={k[3]}": v for k, v in sorted(Counter(
                (r["qset"], r["seed_role"], r["over_cap"], r["ratio_ok"]) for r in rows).items())},
        "drops_by_reason": report["slot_drops_by_reason"],
        "retention": report["retention_checks"]["per_split"],
    }
    failed = [r for r in rows if r["over_cap"] or not r["ratio_ok"]]
    sample = random.Random(C.TRANSCRIPT_SEED).sample(failed, k=min(8, len(failed)))
    json.dump({"summary": summary, "random_failed_sample": sample, "rows": rows},
              open(OUT, "w"), indent=1)
    print(json.dumps(summary, indent=1)[:2500])


if __name__ == "__main__":
    main()
