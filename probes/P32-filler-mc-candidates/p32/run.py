"""P32 driver: ask V4 Flash every condition over OpenRouter with P06's caller and scorer.

    uv run python -m p32.run smoke     # first SMOKE_N items per family, every condition + pass@k
    uv run python -m p32.run main      # all items x 27 conditions, t = 0, one draw
    uv run python -m p32.run passk     # free response base and dots, PASSK_DRAWS draws at t = 1
    (from probes/P32-filler-mc-candidates)

Recipe: P06's `v4flash` (StreamLake pin, `reasoning: {enabled: false}`, max_tokens 100) with no
prefill, as P06 F1/F2/F5. Rows (results/<run>.jsonl) are P06's (`p06.api.run_jobs`), with `arm` set
to the condition string (`p32.prompts`) and `bank` to the family. Resumable by row key. Before each
run the projected cost is checked against P32's cap (`emx.spend.check_total`), and afterwards the
rows' `usage.cost` totals are recorded in the ledger (`reconcile`). Concurrency:
EMX_JUDGE_CONCURRENCY (default 32).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import fcntl
import json
import os
from collections import defaultdict
from pathlib import Path

from p06 import api

import p32  # noqa: F401
from emx import spend

from . import prompts
from .items import load_all

EXP = "P32"
HERE = Path(__file__).resolve().parents[1]
RES = HERE / "results"
RECIPE = dataclasses.replace(api.RECIPES["v4flash"], prefill=False)
SMOKE_N = 10
PASSK_DRAWS = 8
PASSK_T = 1.0
PASSK_CONDS = ("0|-|base", "0|-|dots")
SMOKE_PASSK_N = 2


def job(item: dict, shots: list[dict], cond: str, draw: int = 0, temperature: float = 0.0) -> dict:
    return {"item": {"item_id": item["item_id"], "bank": item["family"], "k": 0,
                     "answer": item["answer"], "answer_type": "int"},
            "shots": [], "arm": cond, "draw": draw, "temperature": temperature,
            "messages": prompts.messages(item, shots, cond)}


def build(which: str, fams: dict | None = None) -> list[dict]:
    fams = fams or load_all()
    jobs = []
    for evals, shots in fams.values():
        if which in ("smoke", "main"):
            sel = evals[:SMOKE_N] if which == "smoke" else evals
            jobs += [job(e, shots, c) for e in sel for c in prompts.conditions()]
        if which in ("smoke", "passk"):
            sel = evals[:SMOKE_PASSK_N] if which == "smoke" else evals
            jobs += [job(e, shots, c, d, PASSK_T) for e in sel for c in PASSK_CONDS
                     for d in range(PASSK_DRAWS)]
    return jobs


def projected_usd(jobs: list[dict]) -> float:
    words = sum(len(m["content"].split()) for j in jobs for m in j["messages"])
    return 1.3 * words * RECIPE.price_in + 10 * len(jobs) * RECIPE.price_out


def reconcile(quiet: bool = False) -> None:
    """Record (row usage.cost totals - ledger) per (model, tag) as P32 judge spend, under a lock."""
    RES.mkdir(parents=True, exist_ok=True)
    with open(RES / ".reconcile.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        have = defaultdict(float)
        for e in spend.read_entries():
            if e.get("exp") == EXP and e.get("kind") == "judge":
                have[e.get("resource", "")] += float(e.get("usd") or 0.0)
        cost, n = defaultdict(float), defaultdict(int)
        for p in sorted(RES.glob("*.jsonl")):
            for line in p.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    res = f"openrouter:{r['model']}:{r['tag']}"
                    cost[res] += float(r.get("cost") or 0.0)
                    n[res] += 1
        for res, c in sorted(cost.items()):
            if c - have[res] > 1e-6:
                spend.record("judge", res, n[res], round(c - have[res], 6),
                             note=f"P32 reconcile: row usage.cost total ${c:.6f}", exp=EXP)
                if not quiet:
                    print(f"[spend] recorded ${c - have[res]:.4f} for {res}", flush=True)
    if not quiet:
        print(f"[spend] P32 totals: {spend.totals(exp=EXP)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["smoke", "main", "passk"])
    a = ap.parse_args()
    jobs = build(a.which)
    out = RES / f"{a.which}.jsonl"
    have = api.done_keys(out)
    todo = [j for j in jobs if api.row_key(RECIPE.model, j["item"]["item_id"], j["arm"], j["draw"],
                                           j["temperature"], f"p32{a.which}") not in have]
    reconcile(quiet=True)
    proj = projected_usd(todo)
    spend.check_total(proj, kind="judge", exp=EXP)
    conc = int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32"))
    print(f"[p32] {a.which}: {len(jobs)} jobs, {len(todo)} to do, projected ${proj:.3f}, "
          f"concurrency {conc}", flush=True)
    asker = api.Asker(api.openrouter_key(), concurrency=conc)

    async def go():
        try:
            await api.run_jobs(asker, RECIPE, todo, out, f"p32{a.which}", placement="every")
        finally:
            await asker.close()

    try:
        asyncio.run(go())
    finally:
        reconcile()


if __name__ == "__main__":
    main()
