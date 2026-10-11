"""F6 analysis: the plan's uplift and depth-shift measures on the chain-in-Brauer-format rows,
with arms mapped onto the plan's names (base -> none, dots500 -> after, pre500 -> before) and
bank chain_brauerfmt -> chain, then `p06.analyze.analyze` unchanged.

    uv run python -m p06.f6_analyze results/stage1/f6_chain_brauer_v4flash.jsonl --out ...
"""

import argparse
import json
from pathlib import Path

from . import analyze
from .api import latest_ok

ARM_MAP = {"base": "none", "dots500": "after", "pre500": "before"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = [{**r, "arm": ARM_MAP[r["arm"]], "bank": "chain"}
            for r in latest_ok(a.path).values()]
    res = analyze.analyze(rows, chain_floor=0.07)
    res["arm_map"] = ARM_MAP
    a.out.write_text(json.dumps(res, indent=1) + "\n")
    u = res["uplift_chain"]
    for g, v in u.items():
        d = v["after_minus_before"]
        print(f"{g:4s} acc base {v['acc']['none']:.3f} dots500 {v['acc']['after']:.3f} "
              f"pre500 {v['acc']['before']:.3f}  dots500-pre500 {d['mean'] * 100:+.1f} "
              f"[{d['lo'] * 100:+.1f}, {d['hi'] * 100:+.1f}]")
    dp = res["depth"]
    print(f"k50 {dp['k50']}  shift {dp['shift_after_minus_before']:+.2f} "
          f"[{dp['lo']:+.2f}, {dp['hi']:+.2f}]")


if __name__ == "__main__":
    main()
