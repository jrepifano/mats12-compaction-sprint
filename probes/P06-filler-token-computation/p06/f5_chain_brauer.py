"""Follow-up F5 (exploratory): nocot chain items (k = 2, 3) in Brauer et al.'s prompt scaffold
(see p06.brauer for the source and arms). The system message is the chain bank's instruction
plus Brauer's filler sentence when k > 0; 5 shots (the first 5 of data/shots_chain10.jsonl)
carry the same filler; the user turn is `Problem: <problem>\\n\\nFiller: . . .\\n\\nAnswer:`
(pre-padding: `Padding: . . .\\n\\nProblem: <problem>\\n\\nAnswer:`); no prefill.

    uv run python -m p06.f5_chain_brauer --model gemma4     # from the probe dir

F6 (`--f6`): all chain k = 2..8, arms base / dots500 / pre500 (500 dots before the problem),
2 draws at t = 0, output results/stage1/f6_chain_brauer_<model>.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses

from emx import spend

from . import api, brauer, items
from .stage1 import DATA, EXP, RES, reconcile


def user_turn(it: dict, where: str, k: int) -> str:
    q = f"Problem: {it['problem']}"
    if k == 0:
        return f"{q}\n\nAnswer:"
    if where == "after":
        return f"{q}\n\nFiller: {brauer.dots(k)}\n\nAnswer:"
    return f"Padding: {brauer.dots(k)}\n\n{q}\n\nAnswer:"


F6_ARMS = {"base": ("after", 0), "dots500": ("after", 500), "pre500": ("before", 500)}


def messages(it: dict, shots: list[dict], arm: str) -> list[dict]:
    where, k = {**brauer.ARMS, **F6_ARMS}[arm]
    msgs = [{"role": "system",
             "content": it["instruction"] + (brauer._SYS_FILLER if k > 0 else "")}]
    for s in shots:
        msgs += [{"role": "user", "content": user_turn(s, where, k)},
                 {"role": "assistant", "content": str(s["answer"])}]
    msgs.append({"role": "user", "content": user_turn(it, where, k)})
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=sorted(api.RECIPES), required=True)
    ap.add_argument("--f6", action="store_true")
    a = ap.parse_args()
    recipe = dataclasses.replace(api.RECIPES[a.model], prefill=False)
    ks, arms, draws = ((2, 3), brauer.ARMS, 1) if not a.f6 else (range(2, 9), F6_ARMS, 2)
    ev = [e for e in items.read_jsonl(DATA / "items_chain.jsonl") if e["k"] in ks]
    shots = items.read_jsonl(DATA / "shots_chain10.jsonl")[:5]
    jobs = [{"item": {**e, "bank": "chain_brauerfmt"}, "shots": [], "arm": arm, "draw": d,
             "temperature": 0.0, "messages": messages(e, shots, arm)}
            for e in ev for arm in arms for d in range(draws)]
    out, tag = ((RES / f"f5_chain_brauer_{a.model}.jsonl", "f5chainbrauer") if not a.f6 else
                (RES / f"f6_chain_brauer_{a.model}.jsonl", "f6chainbrauer"))
    reconcile(quiet=True)
    words = sum(len(m["content"].split()) for j in jobs for m in j["messages"])
    proj = 1.5 * (1.3 * words * recipe.price_in + 10 * len(jobs) * recipe.price_out)
    spend.check_total(proj, kind="judge", exp=EXP)
    print(f"[f5] {a.model}: {len(jobs)} jobs, projected ${proj:.3f}", flush=True)
    asker = api.Asker(api.openrouter_key())

    async def go():
        try:
            await api.run_jobs(asker, recipe, jobs, out, tag)
        finally:
            await asker.close()

    try:
        asyncio.run(go())
    finally:
        reconcile()


if __name__ == "__main__":
    main()
