"""P05 items and prompts: chain (and chain-easy) and arithmetic, two formats, readout prefixes.

Plan: probes/P05-looped-serial-depth/plan.md ("Setup", "Tasks", "Two prompt formats"). Adapted
from probes/P06-filler-token-computation/p06/items.py (P06's chain loop: nocot's generator keyed
per attempt, a gold cap per k, duplicate rejection), at seed 0 with k in CHAIN_KS. Generation goes
through nocot-bench's own banks (vendored in `p05.nocot` @ 634d7de5).

Item record (one JSON object per line, gitignored `data/P05/items_<bank>.jsonl`):
    item_id      str   "<bank>:k<k>:<n>"
    bank         str   "chain" | "chain_easy" | "arithmetic"
    k            int   steps (chain) or nocot's op count (arithmetic)
    problem      str   problem text (without instruction)
    instruction  str
    answer       int
    chance       float the bank's majority floor over its eval golds
    chain only:  start int, ops [[kind, a], ...], traj [start, s1, ..., sk],
                 nudge_depth int (steps whose input state matters for the answer),
                 pm1_nudge_depth int (the same under a +-1 nudge only; descriptive)

Prompts end with "Answer:" (no trailing space). Llama-3 tokenizes "Answer: 12" as `Answer`, `:`,
` `, `12` and "Answer: -5" as `Answer`, `:`, ` -`, `5`, so the gold continuation is " <gold>" and,
for chain, the readout position is the ` ` token, after which every value 1..20 is one token.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .nocot import arithmetic, chain
from .nocot.common import majority_baseline, rng

SEED = 0
CHAIN_KS = (1, 2, 3, 4, 5, 6, 8)
N_CHAIN = 300
ARITH_LEVELS = (1, 2, 3, 4, 5, 6, 8)
N_ARITH = 100
FORMATS = ("nocot", "brauer")
# Each format has its own independently drawn shots with a different depth mix, so the two
# formats differ in layout, instruction placement and the examples shown (log.md, 2026-10-12:
# with 5 shots each to fit 1024 tokens, layout alone would be a weak robustness check).
SHOT_KS = {"nocot": {"chain": (2, 3, 2, 3, 2), "chain_easy": (2, 3, 2, 3, 2),
                     "arithmetic": (1, 2, 3, 1, 2)},
           "brauer": {"chain": (4, 1, 3, 2, 3), "chain_easy": (4, 1, 3, 2, 3),
                      "arithmetic": (2, 1, 3, 2, 1)}}
SHOT_TAG = {"nocot": "shot", "brauer": "shotb"}
READOUT_KS = (2, 3, 4)
MAX_PROMPT_TOKENS = 1024  # Recurrent-Llama's config block_size

# plan "Automatic rules", floor rule: no "bigger than 10" rule; still non-affine, order-bound
CHAIN_EASY = chain.Config(p_halveup=0.5, p_evenhalve_cum=1.0)
CHAIN_CFGS = {"chain": chain.SHIPPED, "chain_easy": CHAIN_EASY}


def gold_cap(n_per_k: int, mod: int = 20) -> int:
    """At most 1.5x a uniform share of the golds per k (P06 used 7 for 100 over 20)."""
    return -(-3 * n_per_k // (2 * mod))


def walk(start: int, ops: list, mod: int = 20) -> list[int]:
    v, out = start, [start]
    for op in ops:
        v = chain._apply(v, tuple(op), mod)
        out.append(v)
    return out


def nudge_depth(start: int, ops: list, mod: int = 20, pm1: bool = False) -> int:
    """Steps whose input state is load-bearing: replacing the state entering step i (i = 0..k-1)
    with another value (any value in 1..mod, or only +-1 wrapped if pm1) and walking the remaining
    steps changes the final answer, for at least one replacement."""
    traj = walk(start, ops, mod)
    final = traj[-1]
    n = 0
    for i in range(len(ops)):
        v = traj[i]
        alts = ([chain._wrap(v - 1, mod), chain._wrap(v + 1, mod)] if pm1
                else [u for u in range(1, mod + 1) if u != v])
        if any(walk(u, ops[i:], mod)[-1] != final for u in alts if u != v):
            n += 1
    return n


def _chain_dict(it: dict, item_id: str, bank: str, cfg: chain.Config) -> dict:
    ops = [list(op) for op in it["ops"]]
    traj = walk(it["start"], ops, cfg.mod)
    assert traj[-1] == it["answer"], item_id
    return {"item_id": item_id, "bank": bank, "k": it["h"], "problem": chain._render(it, cfg.mod),
            "instruction": chain.INSTRUCTION, "answer": it["answer"], "chance": 0.0,
            "start": it["start"], "ops": ops, "traj": traj,
            "nudge_depth": nudge_depth(it["start"], ops, cfg.mod),
            "pm1_nudge_depth": nudge_depth(it["start"], ops, cfg.mod, pm1=True)}


def chain_items(bank: str = "chain", n_per_k: int = N_CHAIN, ks: tuple = CHAIN_KS,
                seed: int = SEED) -> tuple[list[dict], list[dict]]:
    """(eval items, 10 shot items: 5 per format, tagged `fmt`). Per k: nocot's attempt-keyed loop
    with a gold cap and duplicate rejection, until n_per_k items or the attempt budget runs out
    (k = 1 has only a few hundred distinct problems, so it may come out short; it is logged)."""
    cfg = CHAIN_CFGS[bank]
    shots, seen = [], set()
    for fmt in FORMATS:
        tag = SHOT_TAG[fmt]
        for i, k in enumerate(SHOT_KS[fmt][bank]):
            attempt = 0
            while True:
                it = chain._gen(rng(f"{bank}|{seed}|{tag}|{i}|{attempt}"), k, cfg)
                attempt += 1
                p = chain._render(it, cfg.mod)
                if p not in seen:
                    break
            seen.add(p)
            shots.append({**_chain_dict(it, f"{bank}:{tag}:{i}", bank, cfg), "fmt": fmt})
    evals, cap = [], gold_cap(n_per_k, cfg.mod)
    for k in ks:
        made, attempt, golds = 0, 0, {}
        while made < n_per_k and attempt <= cfg.max_attempts:
            it = chain._gen(rng(f"{bank}|{seed}|k{k}|{attempt}"), k, cfg)
            attempt += 1
            p = chain._render(it, cfg.mod)
            if golds.get(it["answer"], 0) >= cap or p in seen:
                continue
            golds[it["answer"]] = golds.get(it["answer"], 0) + 1
            seen.add(p)
            evals.append(_chain_dict(it, f"{bank}:k{k}:{made}", bank, cfg))
            made += 1
    for e in evals:  # plan "Automatic rules" 4: if the nudge depth isn't k, k becomes it
        if e["nudge_depth"] != e["k"]:
            e["k_generated"], e["k"] = e["k"], e["nudge_depth"]
    floor = majority_baseline([e["answer"] for e in evals])
    for e in evals:
        e["chance"] = floor
    return evals, shots


def arithmetic_items(n_per_level: int = N_ARITH, levels: tuple = ARITH_LEVELS,
                     seed: int = SEED) -> tuple[list[dict], list[dict]]:
    """(eval items, 10 shot items, 5 per format) from nocot's `_draw` (distinct, no zero division, |gold| <=
    abs_cap), one private PRNG for the bank."""
    cfg = arithmetic.SHIPPED
    r = rng(f"arithmetic|p05|{seed}")
    seen: set[str] = set()

    def one(d: int, item_id: str) -> dict:
        expr, val = arithmetic._draw(d, r, cfg, seen)
        return {"item_id": item_id, "bank": "arithmetic", "k": d,
                "problem": f"{arithmetic._PREFIX}{expr}", "instruction": arithmetic.INSTRUCTION,
                "answer": val, "chance": 0.0}

    shots = [{**one(d, f"arithmetic:{SHOT_TAG[f]}:{i}"), "fmt": f} for f in FORMATS
             for i, d in enumerate(SHOT_KS[f]["arithmetic"])]
    evals = [one(d, f"arithmetic:k{d}:{n}") for d in levels for n in range(n_per_level)]
    floor = majority_baseline([e["answer"] for e in evals])
    for e in evals:
        e["chance"] = floor
    return evals, shots


def build_bank(bank: str, n: int | None = None) -> tuple[list[dict], list[dict]]:
    if bank == "arithmetic":
        return arithmetic_items(n or N_ARITH)
    return chain_items(bank, n or N_CHAIN)


def render_prompt(item: dict, shots: list[dict], fmt: str) -> str:
    """F-nocot: every shot repeats the instruction; F-brauer: the instruction once on top. Each
    format uses its own shots (`fmt` tag) from the bank's shot list."""
    shots = [s for s in shots if s.get("fmt", fmt) == fmt]
    if fmt == "nocot":
        blocks = [f"{s['instruction']}\n\nProblem: {s['problem']}\nAnswer: {s['answer']}"
                  for s in shots]
        blocks.append(f"{item['instruction']}\n\nProblem: {item['problem']}\nAnswer:")
        return "\n\n".join(blocks)
    if fmt == "brauer":
        blocks = [f"Problem: {s['problem']}\n\nAnswer: {s['answer']}" for s in shots]
        blocks.append(f"Problem: {item['problem']}\n\nAnswer:")
        return item["instruction"] + "\n\n" + "\n\n".join(blocks)
    raise ValueError(fmt)


def gold_continuation(item: dict) -> str:
    return f" {item['answer']}"


def prefix_item(item: dict, j: int) -> dict:
    """The j-step prefix of a chain item as its own problem (the ask-the-model baseline)."""
    assert item["bank"] in CHAIN_CFGS and 1 <= j < item["k"]
    cfg = CHAIN_CFGS[item["bank"]]
    ops = item["ops"][:j]
    it = {"start": item["start"], "ops": [tuple(o) for o in ops], "answer": item["traj"][j]}
    return {**item, "item_id": f"{item['item_id']}:pre{j}", "k": j, "ops": ops,
            "problem": chain._render(it, cfg.mod), "answer": item["traj"][j],
            "traj": item["traj"][: j + 1], "base_id": item["item_id"], "j": j}


def readout_eligible(item: dict) -> bool:
    """Chain items with k in READOUT_KS whose states after steps 1..k are all distinct."""
    if item["bank"] not in CHAIN_CFGS or item["k"] not in READOUT_KS:
        return False
    states = item["traj"][1:]
    return len(set(states)) == len(states)


def write_jsonl(rows: list[dict], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def bank_sha(evals: list[dict], shots: list[dict]) -> str:
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in shots + evals)
    return hashlib.sha256(text.encode()).hexdigest()
