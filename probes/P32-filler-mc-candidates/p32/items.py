"""P32 items: two families with a fixed distractor pool per item, and nested option sets.

Plan: probes/P32-filler-mc-candidates/plan.md ("Setup").

Families (500 eval items each, 5 shots):
    varbind  Brauer et al.'s generator @ a701927 (`--seed 20261013 --num-examples 500
             --chain-len 1`), gitignored at data/P32/varbind_p32.json (sha256 22a1ddc9...); the
             file's first 5 few-shot items are the shots.
    chain    nocot's chain generator (vendored in p06.nocot @ 634d7de5), k = 2, seed 32, at most
             GOLD_CAP items per gold, problems distinct from P06's items and shots; the shots are
             P06's first 5 `shots_chain10` rows.

Distractor pools (9 values per kind, seeded per item and kind; `pool`):
    varbind near  answer -/+ 1..20; the answer's rank among the 10 values is uniform over the
                  feasible ranks (values must be >= 0), so its position in sorted order says nothing
    varbind far   answer -/+ 60..360, rank uniform where feasible
    chain near    a window of 10 consecutive values on the circle 1..20 (the task's own mod-20
                  wrap) holding the answer at a uniform position; the other 9 (circular distance
                  1..9, mean ~3.3). Changed before any data from "9 of the 10 values within
                  circular distance 5", which always put the answer in the middle of its options
    chain far     the 9 values at circular distance 6..10 (the answer's opposite block)
The pool is shuffled once; n options = the answer + the pool's first n - 1 values, so the sets nest
(2 in 5 in 10) and each listed distractor is a random pool member at every n. Display order is a
separate seeded shuffle per (item, kind, n).

Item record:
    item_id "varbind:<idx>" | "chain:<idx>", family, idx, answer (int),
    pools {"near": [9 ints], "far": [9 ints]},
    varbind: definitions, question;  chain: problem, instruction
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from p06 import brauer
from p06.nocot import chain

import p32  # noqa: F401  (sys.path for p06)

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
P06 = HERE.parent / "P06-filler-token-computation"
VARBIND_PATH = REPO / "data" / "P32" / "varbind_p32.json"
VARBIND_SHA256 = "22a1ddc9b5d468da67975c9d223176b4e32aa91b29b118d48cdb4c3600eebed2"
N_ITEMS = 500
N_SHOTS = 5
CHAIN_SEED = 32
CHAIN_K = 2
GOLD_CAP = 40
POOL = 9
OPTION_COUNTS = (2, 5, 10)
KINDS = ("near", "far")
VB_NEAR = (1, 20)
VB_FAR = (60, 360)
MOD = 20


def rng(*parts) -> random.Random:
    h = hashlib.sha256("|".join(map(str, ("P32", *parts))).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------- pools
def _ranked_pool(answer: int, lo: int, hi: int, r: random.Random) -> list[int]:
    below = [d for d in range(lo, hi + 1) if answer - d >= 0]
    above = list(range(lo, hi + 1))
    ranks = [k for k in range(POOL + 1) if k <= len(below) and POOL - k <= len(above)]
    k = r.choice(ranks)
    vals = [answer - d for d in r.sample(below, k)] + [answer + d for d in r.sample(above, POOL - k)]
    r.shuffle(vals)
    return vals


def circ(a: int, b: int, mod: int = MOD) -> int:
    d = (a - b) % mod
    return min(d, mod - d)


def _chain_pool(answer: int, kind: str, r: random.Random) -> list[int]:
    others = [v for v in range(1, MOD + 1) if v != answer]
    if kind == "near":
        start = answer - r.randrange(POOL + 1)
        vals = [((start + i - 1) % MOD) + 1 for i in range(POOL + 1)]
        vals.remove(answer)
    else:
        vals = [v for v in others if circ(v, answer) >= 6]
    assert len(vals) == POOL, (answer, kind, vals)
    r.shuffle(vals)
    return vals


def pool(family: str, key: str, answer: int, kind: str) -> list[int]:
    r = rng("pool", family, key, kind)
    if family == "varbind":
        lo, hi = VB_NEAR if kind == "near" else VB_FAR
        return _ranked_pool(answer, lo, hi, r)
    return _chain_pool(answer, kind, r)


def options(item: dict, kind: str, n: int) -> list[int]:
    """The n displayed options: the answer plus the pool's first n - 1, in a seeded order."""
    opts = [item["answer"], *item["pools"][kind][: n - 1]]
    rng("order", item["item_id"], kind, n).shuffle(opts)
    return opts


# ----------------------------------------------------------------------------------- families
def _with_pools(rec: dict) -> dict:
    rec["pools"] = {k: pool(rec["family"], rec["item_id"], rec["answer"], k) for k in KINDS}
    return rec


def varbind(path: Path = VARBIND_PATH) -> tuple[list[dict], list[dict]]:
    assert sha256(path) == VARBIND_SHA256, f"{path} hash mismatch"
    d = json.loads(path.read_text())

    def rec(ex: dict, item_id: str) -> dict:
        vals = brauer.values(ex)
        assert vals[ex["queried_term"]] == ex["queried_value"]
        return _with_pools({"item_id": item_id, "family": "varbind", "idx": ex.get("idx"),
                            "answer": int(ex["answer"]), "definitions": ex["definitions"],
                            "question": ex["question"]})

    evals = [rec(e, f"varbind:{e['idx']}") for e in d["examples"][:N_ITEMS]]
    shots = [rec(s, f"varbind:shot{i}") for i, s in enumerate(d["few_shot_examples"][:N_SHOTS])]
    return evals, shots


def _p06_problems() -> set[str]:
    out = set()
    for f in ("items_chain.jsonl", "shots_chain10.jsonl", "shots_chain.jsonl"):
        for line in (P06 / "data" / f).read_text().splitlines():
            if line.strip():
                out.add(json.loads(line)["problem"])
    return out


def chain_family() -> tuple[list[dict], list[dict]]:
    cfg = chain.SHIPPED
    seen = _p06_problems()
    evals, golds, attempt = [], {}, 0
    while len(evals) < N_ITEMS:
        if attempt > 100_000:
            raise RuntimeError("chain: exhausted")
        it = chain._gen(chain.rng(f"chain|{CHAIN_SEED}|k{CHAIN_K}|{CHAIN_K}|{attempt}"),
                        CHAIN_K, cfg)
        attempt += 1
        problem = chain._render(it, cfg.mod)
        if problem in seen or golds.get(it["answer"], 0) >= GOLD_CAP:
            continue
        seen.add(problem)
        golds[it["answer"]] = golds.get(it["answer"], 0) + 1
        i = len(evals)
        evals.append(_with_pools({"item_id": f"chain:{i}", "family": "chain", "idx": i,
                                  "answer": int(it["answer"]), "problem": problem,
                                  "instruction": chain.INSTRUCTION}))
    shots = []
    for i, line in enumerate((P06 / "data" / "shots_chain10.jsonl").read_text().splitlines()[:N_SHOTS]):
        s = json.loads(line)
        shots.append(_with_pools({"item_id": f"chain:shot{i}", "family": "chain", "idx": None,
                                  "answer": int(s["answer"]), "problem": s["problem"],
                                  "instruction": s["instruction"]}))
    return evals, shots


def load_all() -> dict[str, tuple[list[dict], list[dict]]]:
    return {"varbind": varbind(), "chain": chain_family()}
