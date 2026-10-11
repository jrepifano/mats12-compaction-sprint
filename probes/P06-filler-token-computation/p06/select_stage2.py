"""Choose Stage 2's items: 60 uplift items and 60 controls (plan "Setup", Stage 2).

    uv run python -m p06.select_stage2 --bulk results/stage1/bulk_gemma4_s10_every.jsonl \
        --select results/stage1/select_gemma4_s10_every.jsonl

Per chain item, pooling the temperature-0 draws and the temperature-1 selection samples:
uplift = acc(after) − acc(before). Uplift items: the N with the largest uplift, which must be
> 0 (ties broken by a seeded shuffle). Controls: for each uplift item, an unused item with the
same k, uplift <= 0, and the closest acc(none) (the plan's "matched on k and on accuracy without
filler"). Writes data/stage2_items.jsonl (chain item records plus "role", "uplift", "acc").
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from . import items
from .api import latest_ok

HERE = Path(__file__).resolve().parents[1]
N = 60
SEED = 0


def item_stats(paths: list[Path]) -> dict[str, dict[str, float]]:
    per = defaultdict(lambda: defaultdict(list))
    for p in paths:
        for r in latest_ok(p).values():
            if r["bank"] == "chain":
                per[r["item_id"]][r["arm"]].append(float(r["correct"]))
    return {i: {a: sum(v) / len(v) for a, v in d.items()} for i, d in per.items()
            if all(a in d for a in ("none", "after", "before"))}


def choose(chain_ev: list[dict], stats: dict[str, dict[str, float]], n: int = N,
           seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    ev = [e for e in chain_ev if e["item_id"] in stats]
    rng.shuffle(ev)
    up = lambda e: stats[e["item_id"]]["after"] - stats[e["item_id"]]["before"]  # noqa: E731
    cand = sorted([e for e in ev if up(e) > 0], key=up, reverse=True)[:n]
    used = {e["item_id"] for e in cand}
    out = []
    for e in cand:
        pool = [c for c in ev if c["k"] == e["k"] and c["item_id"] not in used and up(c) <= 0]
        if not pool:
            continue
        acc0 = stats[e["item_id"]]["none"]
        ctl = min(pool, key=lambda c: abs(stats[c["item_id"]]["none"] - acc0))
        used.add(ctl["item_id"])
        for role, it in (("uplift", e), ("control", ctl)):
            out.append({**it, "role": role, "uplift": up(it), "acc": stats[it["item_id"]]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", type=Path, required=True)
    ap.add_argument("--select", type=Path)
    ap.add_argument("--n", type=int, default=N)
    a = ap.parse_args()
    paths = [a.bulk] + ([a.select] if a.select else [])
    chain_ev = items.read_jsonl(HERE / "data" / "items_chain.jsonl")
    rows = choose(chain_ev, item_stats(paths), a.n)
    sha = items.write_jsonl(rows, HERE / "data" / "stage2_items.jsonl")
    ups = [r for r in rows if r["role"] == "uplift"]
    print(f"stage2 items: {len(ups)} uplift + {len(rows) - len(ups)} controls, sha256 {sha}")
    print("k counts:", sorted(defaultdict(int, {r['k']: 0 for r in ups}).keys()))
    print("mean uplift: uplift", sum(r["uplift"] for r in ups) / max(1, len(ups)),
          "control", sum(r["uplift"] for r in rows if r["role"] == "control")
          / max(1, len(rows) - len(ups)))


if __name__ == "__main__":
    main()
