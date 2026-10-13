"""Seeded random raw samples for note.md (not cherry-picked).

    uv run python -m p33.samples   # from the probe directory; writes results/samples.md
"""

from __future__ import annotations

import json
import random

from . import PROBE
from . import analyze as A

SEED = 0
ARMS = ("base", "dots100", "maskB100", "rep1", "neutral100")


def main() -> None:
    rng = random.Random(SEED)
    lines = ["# P33 raw samples (seed 0)", ""]
    for model in ("v4", "gemma"):
        rows = A.load(model)
        if not rows:
            continue
        by = {(r["item_id"], r["arm"]): r for r in rows}
        ids = sorted({r["item_id"] for r in rows if r["kind"] == "equation"})
        pick = rng.sample(ids, min(6, len(ids)))
        cp = sorted({r["item_id"] for r in rows if r["kind"] == "copy"})
        pick += rng.sample(cp, min(2, len(cp)))
        lines.append(f"## {model}")
        lines.append("")
        lines.append("| item | answer | " + " | ".join(ARMS) + " |")
        lines.append("|---|---|" + "---|" * len(ARMS))
        for i in pick:
            cells = []
            for a in ARMS:
                r = by.get((i, a))
                cells.append("" if r is None else
                             f"{json.dumps(r['gen_text'])[1:-1][:20]} ({'✓' if r['correct'] else '✗'})")
            ans = next(r["answer"] for (k, _), r in by.items() if k == i)
            lines.append(f"| {i} | {ans} | " + " | ".join(cells) + " |")
        lines.append("")
    p2 = PROBE / "results" / "stage2" / "units.jsonl"
    if p2.exists():
        rows = [json.loads(x) for x in p2.read_text().splitlines() if x.strip()]
        op = [r for r in rows if r["unit"] == "donor_patch" and r.get("donor") == "opChange"
              and r["site"] == "all|all"]
        lines += ["## Stage 2: opChange donor patches (all layers x all dots), 8 random", "",
                  "| item | recipient answer | donor answer | hybrid | patched top-1 |",
                  "|---|---|---|---|---|"]
        for r in rng.sample(op, min(8, len(op))):
            lines.append(f"| {r['item_id']} | {r['answer']} | {r['donor_answer']} | {r['hybrid']} | "
                         f"{r['top1_text']} |")
    (PROBE / "results" / "samples.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
