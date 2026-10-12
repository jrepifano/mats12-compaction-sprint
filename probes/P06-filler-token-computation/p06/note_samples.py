"""Seeded random raw samples for note.md (every drawn row is shown; seed 0).

Populations (status-ok rows, latest per key, sorted by key before drawing):
  A. Stage 1 Gemma 4 bulk, chain rows (results/stage1/bulk_gemma4_s10_every.jsonl): 8 rows.
  B. F1 V4 Flash, Brauer's task (results/stage1/f1_brauer_v4flash.jsonl): 6 rows.
  C. F6 V4 Flash, chain in Brauer's format, k = 2 (results/stage1/f6_chain_brauer_v4flash.jsonl):
     6 rows.
  D. F3 transplant records (results/stage2/transplant.jsonl): 6 records.
  E. F8 V4 Flash filler patches (results/v4bulk/patch.jsonl): 6 records.
  F. F8 V4 Flash varbind dots100 readouts (results/v4bulk/readouts/mc/*__dots100.npz, gitignored;
     backed up with the probe's data): 6 items, with each role's first layer and dot index where
     J-Lens's top-1 token is the role's value.
Item texts are not printed (fact_hops is sealed; ids identify every item).
"""

import json
import random
from pathlib import Path

from .api import latest_ok

HERE = Path(__file__).resolve().parents[1]
R1 = HERE / "results" / "stage1"


def draw(rows, n, seed):
    rows = sorted(rows, key=lambda r: r.get("key", r.get("item_id")))
    return random.Random(seed).sample(rows, n)


def fmt(r):
    w = r["witness"]
    return (f"| `{r['item_id']}` | {r['arm']} | {r['draw']} | {r['raw_text'][:24]!r} | "
            f"{r['graded'].get('correct_answer')} | {int(r['correct'])} | "
            f"rtok {w['reasoning_tokens']}, {w['reason']} |")


def main():
    hdr = "| item | arm | draw | raw reply | gold | correct | witness |\n|---|---|---|---|---|---|---|"
    a = [r for r in latest_ok(R1 / "bulk_gemma4_s10_every.jsonl").values() if r["bank"] == "chain"]
    b = list(latest_ok(R1 / "f1_brauer_v4flash.jsonl").values())
    c = [r for r in latest_ok(R1 / "f6_chain_brauer_v4flash.jsonl").values() if r["k"] == 2]
    for name, pop, n in (("A. Stage 1 Gemma chain", a, 8), ("B. F1 V4 Flash varbind", b, 6),
                         ("C. F6 V4 Flash chain k=2", c, 6)):
        print(f"\n**{name}** ({n} of {len(pop)}, seed 0)\n\n{hdr}")
        for r in draw(pop, n, 0):
            print(fmt(r))
    d = [json.loads(x) for x in (HERE / "results/stage2/transplant.jsonl").read_text().splitlines()]
    print(f"\n**D. F3 transplants** (6 of {len(d)}, seed 0)\n")
    print("| item | gold | target out | donor x → donor gold | donor out | transplanted out | "
          "random-donor transplant out |\n|---|---|---|---|---|---|---|")
    for r in draw(d, 6, 0):
        print(f"| `{r['item_id']}` | {r['answer']} | {r['target_pred']} | x={r['donor_x']} → "
              f"{r['donor_answer']} | {r['donor_pred']} | {r['tx_pred']} | {r['rtx_pred']} |")

    e = [json.loads(x) for x in (HERE / "results/v4bulk/patch.jsonl").read_text().splitlines()]
    print(f"\n**E. F8 filler patches** (6 of {len(e)}, seed 0)\n")
    print("| item | gold | target out | donor start → donor gold | donor out | matched patch out | "
          "random donor gold | random patch out |\n|---|---|---|---|---|---|---|---|")
    for r in draw(e, 6, 0):
        print(f"| `{r['item_id']}` | {r['answer']} | {r['target_pred']} | {r['donor_start']} → "
              f"{r['donor_answer']} | {r['donor_pred']} | {r['tx_pred']} | {r['rand_answer']} | "
              f"{r['rtx_pred']} |")
    from . import v4_extra as X

    f = [r for r in X.recs_in(HERE / "results/v4bulk", "mc") if r["arm"] == "dots100"]
    print(f"\n**F. F8 varbind dots100, J-Lens first top-1 (layer, dot)** (6 of {len(f)}, seed 0)\n")
    print("| item | output | gold | " + " | ".join(X.MC_ROLES) + " |\n"
          + "|---" * (3 + len(X.MC_ROLES)) + "|")
    for r in draw(f, 6, 0):
        cells = X.top1_cells(r, "jlens", "filler")
        cols = []
        for v in X.mc_roles(r).values():
            layers, dots = (cells == X.token_of(r, v)).nonzero()
            first = (f"({layers.min()}, {dots[layers == layers.min()].min()})" if len(layers)
                     else "not top-1")
            cols.append(f"{v}: {first}")
        print(f"| `{r['item_id']}` | {r['gen_text']} | {r['answer']} | " + " | ".join(cols) + " |")


if __name__ == "__main__":
    main()
