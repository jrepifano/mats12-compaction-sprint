"""Follow-up F1 (exploratory, laptop, $0): are y's definition inputs (x, the offset off) and the
question's constant k2 readable at the filler, beside y? P06 F3's statistic
(`p06.f3_sensitivity.stats` / `perm_test`: per item and digit, the max log-prob residual over
layers >= 30 and the position group; hit = the value's first digit tops the 10 digits; null =
the values permuted across items, 2,000 permutations, seed 0) on P06's dots100 readouts.

    uv run python -m p24.offset_readout <P06 readouts dir> [out.json]    # from the probe dir
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from p06 import brauer as B
from p06.f3_sensitivity import GROUPS, READERS, perm_test, stats
from p06.readers_analysis import first_digit, load_records, stack_arm

from . import donors as D


def roles(ex: dict) -> dict[str, int]:
    p = D.parse(ex)
    return {"x": p["x"], "off": p["off"], "y": p["y"], "k2": ex["constant"],
            "answer": ex["answer"]}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    recs = load_records(Path(argv[0]))
    exs = {e["idx"]: e for e in B.load(1)[0]}
    out: dict = {}
    for reader in READERS:
        rs, slots, lp = stack_arm(recs, reader, "dots100")
        vals = [roles(exs[r["idx"]]) for r in rs]
        fds = {k: np.array([first_digit(v[k]) for v in vals]) for k in vals[0]}
        distinct = np.all([fds[k] != fds["off"] for k in ("x", "y", "answer")], axis=0)
        for gname in ("label", "filler"):
            S = stats(lp, slots, GROUPS[gname], "logprob")
            for role in ("x", "off", "y", "k2"):
                row = {"all_items": perm_test(S, fds[role], np.random.default_rng(0))}
                if role == "off":
                    row["off_distinct"] = perm_test(S[distinct], fds[role][distinct],
                                                    np.random.default_rng(0))
                out[f"{reader}|{gname}|{role}"] = row
                a = row["all_items"]
                print(f"{reader:10s} {gname:6s} {role:4s} hit {a['hit_rate']:.3f} "
                      f"null {a['null_mean']:.3f} p {a['p']:.4f}"
                      + (f" | distinct n={row['off_distinct']['n']} "
                         f"{row['off_distinct']['hit_rate']:.3f} "
                         f"(null {row['off_distinct']['null_mean']:.3f}, "
                         f"p {row['off_distinct']['p']:.4f})" if role == "off" else ""))
    if len(argv) > 1:
        Path(argv[1]).write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
