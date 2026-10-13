"""P39 raw samples for note.md: a seeded random draw of patched rows, every drawn row shown.

    uv run python -m p39.raw_samples > results/raw_samples.md

Population: rows on the held items (P36's held-entity and held-verb items) with twin donors, for
the arms below; draws of a random family are pooled. Strata: arm; 3 rows per arm;
``random.Random(20261013)`` over each arm's rows sorted by (arm, recip, q). Each row shows the
patch's share of the twin difference and its logit-difference shift. Decodes the top-1 token with
the Qwen3.6-27B tokenizer (local HF cache).
"""

from __future__ import annotations

import random

from p36 import analyze as P36A
from p36 import stimuli as S

from p39 import analyze as AN

SEED = 20261013
PER_ARM = 3
ARMS = ("full", "jl25", "iso_m", "cov_m", "jrot25_m", "pca_m", "ax_order_m", "ax_md_m",
        "ax_role_m", "r1d_m")  # fmt: skip


def main() -> int:
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(
        "Qwen/Qwen3.6-27B", revision="6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"
    )
    _, sents = S.load_stimuli(P36A.HERE / "data" / "stimuli.json")
    by = {s.sid: s for s in sents}
    rows, _, _ = AN.load()
    rows = AN.scope_rows([r for r in rows if r["kind"] == "twin"], "held", AN.split_of())
    rng = random.Random(SEED)
    print("| arm | q | recipient sentence | top-1 | flip | share % | shift |")
    print("|---|---|---|---|---|---|---|")
    for arm in ARMS:
        pick = sorted(
            (r for r in rows if r["dict"] == arm or r.get("family") == arm),
            key=lambda r: (r["dict"], r["recip"], r["q"]),
        )
        for r in rng.sample(pick, min(PER_ARM, len(pick))):
            top = tok.decode([r["top1"]]).strip()
            flip = "yes" if r["flip"] else "no"
            q = S.question_text(by[r["recip"]].verb, r["q"])
            sh = "–" if r["shift"] is None else f"{r['shift']:.2f}"
            print(f"| {r['dict']} | {q} | {by[r['recip']].text} | {top} | {flip} | "
                  f"{100 * r.get('share', float('nan')):.1f} | {sh} |")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
