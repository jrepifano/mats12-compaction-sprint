"""Follow-up F1 (exploratory; log.md): what about the span after the question gives the uplift?

The main run found that the neutral prose passage in the filler's place (`padmid`) gives most of
the dots' free-response uplift on varbind. F1 varies the span's content, label and system
sentence, and its length, in free response only (500 items per family, t = 0, one draw), in the
same scaffold as the main run (`p32.prompts`; every turn carries the same span):

    dots_ign    `Filler: ` + 500 dots, with padmid's "unrelated passage; ignore it" sentence
    prose_fill  `Filler: ` + the prose passage, with the dots' "filler ... extra space" sentence
    the500      `Filler: ` + "the" x 500 (504 V4 tokens with label), dots' sentence
    repq        the problem repeated once after itself (Leviathan et al.), no system sentence
    dots100     `Filler: ` + 100 dots, dots' sentence (103 tokens)
    prose100    `Filler: ` + the passage's first PROSE100_WORDS words, dots' sentence (~100 tokens)

    uv run python -m p32.f1          # from probes/P32-filler-mc-candidates -> results/f1.jsonl
"""

from __future__ import annotations

import asyncio
import os

from p06 import api, brauer

import p32  # noqa: F401
from emx import spend

from . import prompts
from .items import load_all
from .run import EXP, RECIPE, RES, projected_usd, reconcile

PROSE100_WORDS = 86
ARMS = ("dots_ign", "prose_fill", "the500", "repq", "dots100", "prose100")
SPANS = {"dots_ign": prompts.DOTS, "prose_fill": prompts.PAD, "the500": " ".join(["the"] * 500),
         "dots100": brauer.dots(100), "prose100": " ".join(prompts.PAD.split()[:PROSE100_WORDS])}


def user_turn(item: dict, arm: str) -> str:
    q = prompts._problem(item)
    if arm == "repq":
        return f"{q}\n\n{q}\n\nAnswer:"
    return f"{q}\n\nFiller: {SPANS[arm]}\n\nAnswer:"


def system(item: dict, arm: str) -> str:
    s = prompts.system(item, "0|-|base")
    if arm == "dots_ign":
        return s + prompts.PADMID_SYS
    if arm == "repq":
        return s
    return s + prompts.FILLER_SYS


def messages(item: dict, shots: list[dict], arm: str) -> list[dict]:
    msgs = [{"role": "system", "content": system(item, arm)}]
    for s in shots:
        msgs += [{"role": "user", "content": user_turn(s, arm)},
                 {"role": "assistant", "content": str(s["answer"])}]
    msgs.append({"role": "user", "content": user_turn(item, arm)})
    return msgs


def build(fams: dict | None = None) -> list[dict]:
    fams = fams or load_all()
    return [{"item": {"item_id": e["item_id"], "bank": e["family"], "k": 0, "answer": e["answer"],
                      "answer_type": "int"},
             "shots": [], "arm": f"0|-|{a}", "draw": 0, "temperature": 0.0,
             "messages": messages(e, shots, a)}
            for evals, shots in fams.values() for e in evals for a in ARMS]


def main():
    jobs = build()
    out = RES / "f1.jsonl"
    have = api.done_keys(out)
    todo = [j for j in jobs if api.row_key(RECIPE.model, j["item"]["item_id"], j["arm"], 0, 0.0,
                                           "p32f1") not in have]
    reconcile(quiet=True)
    proj = projected_usd(todo)
    spend.check_total(proj, kind="judge", exp=EXP)
    conc = int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32"))
    print(f"[p32] f1: {len(jobs)} jobs, {len(todo)} to do, projected ${proj:.3f}", flush=True)
    asker = api.Asker(api.openrouter_key(), concurrency=conc)

    async def go():
        try:
            await api.run_jobs(asker, RECIPE, todo, out, "p32f1")
        finally:
            await asker.close()

    try:
        asyncio.run(go())
    finally:
        reconcile()


if __name__ == "__main__":
    main()
