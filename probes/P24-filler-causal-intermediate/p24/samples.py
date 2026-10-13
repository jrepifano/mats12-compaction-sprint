"""Seeded random raw samples for the note: N items drawn uniformly (seed 20261012) from
results/rows.jsonl, each shown with the target's y definition, the donors' edits, a', and the
unpatched, X-, Y-, S-, F- and R-patched outputs at the dots.

    uv run python -m p24.samples [N]      # from the probe dir; prints markdown
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from p06 import brauer as B

from . import donors as D

SEED = 20261012


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    n = int(argv[0]) if argv else 8
    rows = [json.loads(x) for x in (Path(__file__).resolve().parents[1] / "results" /
                                     "rows.jsonl").read_text().splitlines() if x.strip()]
    exs = {e["idx"]: e for e in B.load(1)[0]}
    pick = random.Random(SEED).sample(rows, min(n, len(rows)))
    print("| idx | y's definition (target) | answer | a' (X/Y/S) | unpatched | X | Y | S | F (a'_F) |"
          " R |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(pick, key=lambda r: r["idx"]):
        ex = exs[r["idx"]]
        p = D.parse(ex)
        dots = {q["kind"]: q["pred"] for q in r["patched"] if q["site"] == "dots"}
        d = r["donors"]
        f = f"{dots.get('F')} ({d['F']['a_alt']})" if "F" in d else "—"
        print(f"| {r['idx']} | {p['ref']} = {p['x']}; y = {p['coef']} {p['ref']} {p['op']} "
              f"{p['off']} = {p['y']} | {r['answer']} | {d['X']['a_alt']} (dx {d['X']['dx']}) | "
              f"{r['ref']['pred']} | {dots.get('X')} | {dots.get('Y')} | {dots.get('S')} | {f} | "
              f"{dots.get('R')} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
