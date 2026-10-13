"""P36 raw samples for note.md: a seeded random draw of patched rows, every drawn row shown.

    uv run python -m p36.raw_samples > results/raw_samples.md

Population: the pass-1 rows at the selected cell (entity@48) for the arms twin:full (stage 0),
twin:j and twin:r (J-lens, k = 25), xsame:full and xtwin:full, plus the follow-up F1 rows at
L48 for shift_same:full and shift_twin:full when present. Strata: arm; 3 rows per arm;
``random.Random(20261013)`` over each arm's rows sorted by (recip, q). Decodes the top-1 token
with the Qwen3.6-27B tokenizer (local HF cache).
"""

from __future__ import annotations

import random

from p36 import analyze as A
from p36 import stimuli as S

SEED = 20261013
PER_ARM = 3
ARMS = (
    ("stage0", "twin", "full", None),
    ("pass1", "twin", "j", "jlens"),
    ("pass1", "twin", "r", "jlens"),
    ("pass1", "xsame", "full", None),
    ("pass1", "xtwin", "full", None),
    ("followup", "shift_same", "full", None),
    ("followup", "shift_twin", "full", None),
)


def main() -> int:
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(
        "Qwen/Qwen3.6-27B", revision="6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"
    )
    _, sents = S.load_stimuli(A.HERE / "data" / "stimuli.json")
    by = {s.sid: s for s in sents + S.shifted(sents)}
    rng = random.Random(SEED)
    print("| arm | q | recipient sentence | donor sentence | top-1 | flip |")
    print("|---|---|---|---|---|---|")
    for phase, kind, op, dct in ARMS:
        res = A.RES / "followup" if phase == "followup" else A.RES
        rows = [
            r
            for r in A.read_rows(phase, res)
            if r["kind"] == kind
            and r["op"] == op
            and r["dict"] == dct
            and r["site"] == "entity"
            and r["layers"] == [48]
            and (dct is None or r["k"] == 25)
        ]
        rows.sort(key=lambda r: (r["recip"], r["q"]))
        for r in rng.sample(rows, min(PER_ARM, len(rows))):
            arm = f"{kind}:{op}" + (f":{dct}" if dct else "")
            top = tok.decode([r["top1"]]).strip()
            flip = "yes" if r["top1"] == r["a_rev"] else "no"
            q = S.question_text(by[r["recip"]].verb, r["q"])
            print(
                f"| {arm} | {q} | {by[r['recip']].text} | {by[r['donor']].text} | {top} | {flip} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
