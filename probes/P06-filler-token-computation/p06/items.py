"""P06 items: chain items with every intermediate known, multiple-choice variants, fact_hops.

Plan: probes/P06-filler-token-computation/plan.md ("Setup"). Generation goes through nocot-bench's
own `chain` generator (vendored in `p06.nocot`, @ 634d7de5) at seed 1, one rung per k so each k
gets its own gold spread (`gold_cap` per k), and the trajectory is re-walked from the rendered
text by `trajectory()` (an independent re-implementation of nocot's `solve`, which also returns
every intermediate state).

Item record (one JSON object per line, `data/P06/items_*.jsonl`):
    item_id      str   "chain:k<k>:<n>" | "mc<n_opt>:<chain item_id>" | "hops:<problem_number>"
    bank         str   "chain" | "chain_mc" | "fact_hops"
    k            int   steps (chain) or hops (fact_hops difficulty)
    problem      str   the problem text (without instruction)
    instruction  str
    answer       int | str
    answer_type  str   "int" | "text"
    chance       float the bank's majority floor (chain), 1/n_options (MC), nocot's (fact_hops)
    traj         list[int]   chain only: start value then the state after each step (len k+1)
    options      list[int]   MC only
    base_id      str   MC only: the free-response item it was built from
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
from pathlib import Path

from .nocot import chain

SEED = 1
KS = tuple(range(2, 9))
N_PER_K = 100
GOLD_CAP_PER_K = 7  # 100 items over 20 possible golds; nocot's shipped cap is 6 for 7 items/h
MC_KS = (4, 5, 6)
MC_OPTIONS = (2, 5, 10)
MC_SEED = 20261011

MC_INSTRUCTION = (
    "You will be given a sequence of arithmetic steps and a list of options. The final number "
    "is one of the options. Answer immediately using the format 'Answer: [ANSWER]' where "
    "[ANSWER] is just the final number, nothing else. No explanation, no words, no reasoning, "
    "just the number."
)

_START = re.compile(r"Start with the number (\d+)")
_MOD = re.compile(r"subtract (\d+); if it is smaller")
_HALVE = "Halve it, rounding up."
_EVEN = re.compile(r"^If it is even, halve it; if it is odd, add (\d+)\.$")
_GT10 = re.compile(r"^If it is bigger than 10, subtract (\d+); otherwise double it\.$")


def trajectory(problem: str) -> list[int]:
    """[start, state after step 1, ..., final] re-walked from the rendered text."""
    mod = int(_MOD.search(problem).group(1))
    v = int(_START.search(problem).group(1))
    out = [v]
    for line in problem.splitlines():
        line = line.strip()
        if line == _HALVE:
            v = -(-v // 2)
        elif m := _EVEN.match(line):
            v = v // 2 if v % 2 == 0 else v + int(m.group(1))
        elif m := _GT10.match(line):
            v = v - int(m.group(1)) if v > 10 else v * 2
        else:
            continue
        v = ((v - 1) % mod) + 1
        out.append(v)
    return out


@dataclasses.dataclass
class _Rendered:
    problem: str
    answer: int
    difficulty: int
    instruction: str


def _item_dict(it: _Rendered, item_id: str, chance: float) -> dict:
    traj = trajectory(it.problem)
    assert traj[-1] == it.answer and len(traj) == it.difficulty + 1, item_id
    return {"item_id": item_id, "bank": "chain", "k": it.difficulty, "problem": it.problem,
            "instruction": it.instruction, "answer": it.answer, "answer_type": "int",
            "chance": chance, "traj": traj}


def chain_items(seed: int = SEED) -> tuple[list[dict], list[dict]]:
    """(eval items, shot items): N_PER_K eval items per k in KS, and one k=2 shot (nocot's chain
    bank is asked with its single shot row, i.e. 1-shot)."""
    cfg = chain.SHIPPED
    shot = chain._gen(chain.rng(f"chain|{seed}|shot"), cfg.shot_h, cfg)
    shot_problem = chain._render(shot, cfg.mod)
    shots = [_item_dict(_Rendered(shot_problem, shot["answer"], cfg.shot_h, chain.INSTRUCTION),
                        "chain:shot", 0.0)]
    # nocot's generate() loop (rng key per attempt, gold_cap) per k, plus a duplicate-problem
    # rejection: at k = 2 there are few distinct walks, and nocot's run_qc forbids collisions.
    evals, seen = [], {shot_problem}
    for k in KS:
        made, attempt, gold_counts = 0, 0, {}
        while made < N_PER_K:
            if attempt > cfg.max_attempts:
                raise RuntimeError(f"chain: k={k} exhausted")
            it = chain._gen(chain.rng(f"chain|{seed}|k{k}|{k}|{attempt}"), k, cfg)
            attempt += 1
            problem = chain._render(it, cfg.mod)
            if gold_counts.get(it["answer"], 0) >= GOLD_CAP_PER_K or problem in seen:
                continue
            gold_counts[it["answer"]] = gold_counts.get(it["answer"], 0) + 1
            seen.add(problem)
            evals.append(_item_dict(_Rendered(problem, it["answer"], k, chain.INSTRUCTION),
                                    f"chain:k{k}:{made}", 0.0))
            made += 1
    floor = chain.majority_baseline([e["answer"] for e in evals])
    for e in evals:
        e["chance"] = floor
    return evals, shots


SHOTS10_KS = (2, 3, 2, 4, 3, 2, 3, 4, 2, 3)


def chain_shots10(evals: list[dict], seed: int = SEED) -> list[dict]:
    """The alternate recipe's 10 shots (plan "Automatic rules": one alternate recipe per model
    that fails the smoke): k in SHOTS10_KS, nocot's generator, problems distinct from the evals."""
    cfg = chain.SHIPPED
    seen = {e["problem"] for e in evals}
    out = []
    for i, k in enumerate(SHOTS10_KS):
        attempt = 0
        while True:
            it = chain._gen(chain.rng(f"chain|{seed}|shot10|{i}|{attempt}"), k, cfg)
            attempt += 1
            problem = chain._render(it, cfg.mod)
            if problem not in seen:
                break
        seen.add(problem)
        out.append(_item_dict(_Rendered(problem, it["answer"], k, chain.INSTRUCTION),
                              f"chain:shot10:{i}", 0.0))
    return out


def mc_variant(item: dict, n_opt: int, rng: random.Random) -> dict:
    """The item with `n_opt` options: the gold plus distractors drawn uniformly from the other
    values in 1..20, in shuffled order."""
    others = [v for v in range(1, 21) if v != item["answer"]]
    opts = rng.sample(others, n_opt - 1) + [item["answer"]]
    rng.shuffle(opts)
    problem = item["problem"] + "\nOptions: " + ", ".join(str(o) for o in opts)
    return {**item, "item_id": f"mc{n_opt}:{item['item_id']}", "bank": "chain_mc",
            "problem": problem, "instruction": MC_INSTRUCTION, "chance": 1.0 / n_opt,
            "options": opts, "base_id": item["item_id"]}


def mc_items(evals: list[dict], shots: list[dict]) -> tuple[list[dict], dict[int, dict]]:
    """Every eval item with k in MC_KS at each option count (plan: 300 items x {2, 5, 10}),
    and one MC shot per option count."""
    rng = random.Random(MC_SEED)
    out = [mc_variant(e, n, rng) for e in evals if e["k"] in MC_KS for n in MC_OPTIONS]
    mc_shots = {n: mc_variant(shots[0], n, rng) for n in MC_OPTIONS}
    return out, mc_shots


def load_fact_hops(path: Path) -> tuple[list[dict], list[dict]]:
    """nocot's sealed `hops5r2.jsonl` (fact_hops): (64 eval items, 3 shots). The file is
    gitignored (nocot asks that the unpacked items not be republished)."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    evals, shots = [], []
    for r in rows:
        d = {"item_id": f"hops:{r['problem_number']}", "bank": "fact_hops",
             "k": r["difficulty"], "problem": r["problem"], "instruction": r["instruction"],
             "answer": r["answer"], "answer_type": r["answer_type"], "chance": r["chance"],
             "rung": r["rung"]}
        (shots if r["split"] == "shot" else evals).append(d)
    assert len(evals) == 64 and len(shots) == 3, (len(evals), len(shots))
    return evals, shots


def write_jsonl(rows: list[dict], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
