"""Free, offline check of the seed pool for the shared spec v2 draft
(docs/experiments/shared/susceptibility-measure-v2-draft.md, change 1).

Rebuilds the v1 seed pool (answers of at most 80 tokens as scored, from the judged transcripts)
and counts candidate seeds per question under (a) a hard 64-token cap and (b) trimming every seed
longer than 64 tokens to its longest whole-sentence opening of at most 64 tokens (sentences split
after . ! ?), excluding a seed with no such opening. Writes results/E3/pairs_build/v2_pool_check.json.
"""

from __future__ import annotations

import collections
import json
import re

from emx.shared import answer_pairs as A

OUT = "results/E3/pairs_build/v2_pool_check.json"
CAP = 64


def main() -> None:
    b = A.Builder(A.BuildConfig(), log=lambda s: None)
    b.prepare(write=False)
    pool = A.load_seed_pool(A.source_files(b.cfg.sources, b.cfg.main_checkout), b.qtext,
                            b.count_tokens)

    def trim(text: str) -> str | None:
        out = ""
        for s in re.split(r"(?<=[.!?])\s+", text.strip()):
            cand = f"{out} {s}".strip() if out else s
            if b.count_tokens(cand) > CAP:
                break
            out = cand
        return out or None

    med = [q for q, qs in b.qset_of.items() if qs == "medical"]
    hard = pool[pool["ntok"] <= CAP]
    hard_n = {q: int((hard["qid"] == q).sum()) for q in med}
    pool = pool.copy()
    pool["trimmed"] = [trim(t) if n > CAP else t for t, n in zip(pool["answer"], pool["ntok"])]
    kept = pool[pool["trimmed"].notna()]
    trim_n = {q: int((kept["qid"] == q).sum()) for q in med}
    broad = [q for q, qs in b.qset_of.items() if qs == "broad"]
    em_after = kept[(kept["kind"] == "em") & kept["qid"].isin(broad)].groupby("qid").size()
    res = {
        "population": "v1 seed pool (answers <= 80 tokens as scored), 40 medical questions",
        "hard_cap_64": {"per_question_capped_at_4": dict(sorted(collections.Counter(
            min(v, 4) for v in hard_n.values()).items())),
            "under_2": sorted(q for q, v in hard_n.items() if v < 2),
            "max_medical_questions_keepable": sum(v >= 2 for v in hard_n.values())},
        "trim_64": {"per_question_capped_at_4": dict(sorted(collections.Counter(
            min(v, 4) for v in trim_n.values()).items())),
            "under_4": {q: v for q, v in trim_n.items() if v < 4}},
        "broad_questions_with_em_seed_after_trim": int((em_after > 0).sum()),
    }
    json.dump(res, open(OUT, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
