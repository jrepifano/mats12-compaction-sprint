"""P16 random raw samples for note.md (seeded; every drawn row is shown).

Population: the complete items. Draw: SEED-shuffled items, the first N_PER_ARM per arm, for the
arms P, CD, L, I, LD and I2 (when run). Per row: y, d, the turn-1 reply, both turn-2 replies, F's
reply, the J-Lens R_fill, the no-judge monitor N's first five candidates and Haiku J1's
candidates (when judged).

    uv run python -m p16.samples [--n 4]   # from the probe dir; writes results/samples.md
"""

from __future__ import annotations

import argparse
import json
import random

from . import analyze as AN
from . import arms as A
from . import judge as J
from . import readers as R
from . import schema as S

SEED = A.SEED + 7
ARMS = ("P", "CD", "L", "I", "LD", "I2")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p16.samples")
    ap.add_argument("--n", type=int, default=4)
    a = ap.parse_args(argv)
    ans = AN.load_answers(S.RESULTS)
    ivocab = AN.load_ivocab(S.RESULTS)
    judged = AN.judge_cands(S.RESULTS)
    items = [it for it in A.load_items() if (it["idx"], "P", "turn1") in ans]
    rng = random.Random(SEED)
    lines = [f"# P16 random raw samples (seed {SEED}; {a.n} items per arm, every drawn row)", ""]
    for arm in ARMS:
        pool = [it for it in items if (it["idx"], arm, "turn1") in ans]
        if not pool:
            continue
        draw = rng.sample(pool, min(a.n, len(pool)))
        head = ("| idx | y | d | turn 1 (answer) | plain ask | prefilled ask | F | R_fill J-Lens "
                "| N top 5 | Haiku J1 |")
        lines += [f"## {arm}", "", head, "|---" * 10 + "|"]
        for it in draw:
            i = it["idx"]

            def g(kind, i=i, arm=arm):
                r = ans.get((i, arm, kind))
                return "—" if r is None else repr(r["gen_text"])[:40]

            rd = AN.readers_for(S.RESULTS, it, arm, ivocab, set())
            n5 = R.numeric_ranking(J.agg_list(i, arm), ivocab)[:5] if J.resid_path(
                i, arm, "P").exists() else None
            j1 = judged.get(f"haiku|J1|P|{arm}|{i}")
            f = ans.get((i, "F", "fresh"))
            lines.append(f"| {i} | {it['roles']['y']} | {it['d']} | {g('turn1')} "
                         f"({it['roles']['answer']}) | {g('ask_plain')} | {g('ask_pre')} | "
                         f"{repr(f['gen_text'])[:20] if f else '—'} | "
                         f"{rd['R_fill|jlens'] if rd else '—'} | {n5} | {j1} |")
        lines.append("")
    (S.RESULTS / "samples.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(json.dumps({"seed": SEED, "n": a.n}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
