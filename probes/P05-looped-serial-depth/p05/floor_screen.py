"""Free laptop floor screen (plan "Automatic rules", floor rule), run before paying for the pod.

k in {1, 2} on chain, chain_easy and arithmetic, both formats, the baseline and the recurrent
model (r <= 32; the dry run's predictions were identical at r = 16, 32, 64), N items per k drawn
with a seeded sample. Teacher-forced scoring (fp32), as the pod's sweep. Resumable: one output
file per (model, bank, fmt) under --out, skipped when present.

    <venv-p05>/bin/python -m p05.floor_screen --n 30 --out laptop/floor
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import time
from pathlib import Path

from . import items as I
from . import session as S

CONFIGS = (("base", (0,)), ("rec32", (1, 2, 4, 8, 16, 32)))
BANKS = ("chain", "chain_easy", "arithmetic")


def sample(bank: str, n: int) -> tuple[list[dict], list[dict]]:
    evals, shots = I.build_bank(bank)
    rnd = random.Random(f"floor|{bank}")
    rows = []
    for k in (1, 2):
        pool = [e for e in evals if e["k"] == k]
        rows += rnd.sample(pool, min(n, len(pool)))
    return rows, shots


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p05.floor_screen")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--out", default="laptop/floor")
    ap.add_argument("--device", default="mps")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    s = S.Session(argparse.Namespace(cmd="all", out=str(out), device=args.device, limit=0,
                                     banks=["chain"], models=[], token_budget=6000,
                                     greedy_batch=2, max_hours=99))
    s.load_tok()
    s.bf16 = False
    summary = {}
    for key, cps in CONFIGS:
        for bank in BANKS:
            rows, shots = sample(bank, args.n)
            for fmt in I.FORMATS:
                p = out / f"tf_{key}_{bank}_{fmt}.jsonl"
                if not p.exists():
                    t = time.time()
                    sc, _, _ = s.sweep(key, 0, rows, fmt, shots, checkpoints=cps)
                    S.atomic_write(p, "".join(json.dumps(r, sort_keys=True) + "\n" for r in sc))
                    S.log(f"{p.name} {time.time() - t:.0f}s")
                sc = [json.loads(x) for x in p.read_text().splitlines()]
                acc = collections.defaultdict(list)
                for r in sc:
                    acc[(r["r"], r["k"])].append(r["correct"])
                summary[f"{key}|{bank}|{fmt}"] = {
                    f"r{r}_k{k}": [sum(v), len(v)] for (r, k), v in sorted(acc.items())}
                S.log(f"{key} {bank} {fmt} " + " ".join(
                    f"{c}={a}/{b}" for c, (a, b) in summary[f"{key}|{bank}|{fmt}"].items()))
    S.atomic_write(out / "floor_screen.json", json.dumps(summary, indent=1, sort_keys=True))
    S.log("FLOOR_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
