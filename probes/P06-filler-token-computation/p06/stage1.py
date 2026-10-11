"""P06 Stage 1 driver (API, laptop). Plan: probes/P06-filler-token-computation/plan.md.

    uv run python -m p06.stage1 items                 # write the item files, print sha256s
    uv run python -m p06.stage1 smoke   --model gemma4 # 20 chain items x 3 arms (+ posctl)
    uv run python -m p06.stage1 bulk    --model gemma4 # chain + fact_hops + MC, 2 draws, t=0
    uv run python -m p06.stage1 select  --model gemma4 # 10 samples at t=1 on items that differ
    uv run python -m p06.stage1 reconcile             # ledger P06 judge spend == row costs

Run from probes/P06-filler-token-computation/. Spend: every batch is projected and checked with
`emx.spend.check_total(..., exp="P06")` first; afterwards `reconcile` records, as one ledger row
per model and tag, the difference between the summed `usage.cost` of every result row and what
the ledger already holds for that model and tag. So a crash mid-batch loses no spend: the next
reconcile picks up the rows that were written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import defaultdict
from pathlib import Path

from emx import spend

from . import api, items

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
DATA = HERE / "data"
RES = HERE / "results" / "stage1"
HOPS_PATH = REPO / "data" / "P06" / "hops5r2.jsonl"
EXP = "P06"
SMOKE_SEED = 7


def load_all(shots: int = 1):
    """shots=1: nocot's recipe (the chain bank's single shot). shots=10: the alternate recipe
    (10 chain shots at k = 2..4; MC shots are their MC variants, seed MC_SEED + 1)."""
    chain_ev = items.read_jsonl(DATA / "items_chain.jsonl")
    mc = items.read_jsonl(DATA / "items_mc.jsonl")
    if shots == 1:
        chain_shots = items.read_jsonl(DATA / "shots_chain.jsonl")
        mc_shots = {int(k): [v] for k, v in
                    json.loads((DATA / "shots_mc.json").read_text()).items()}
    else:
        chain_shots = items.read_jsonl(DATA / "shots_chain10.jsonl")
        rng = random.Random(items.MC_SEED + 1)
        mc_shots = {n: [items.mc_variant(s, n, rng) for s in chain_shots]
                    for n in items.MC_OPTIONS}
    hops_ev, hops_shots = items.load_fact_hops(HOPS_PATH)
    return chain_ev, chain_shots, mc, mc_shots, hops_ev, hops_shots


def cmd_items(_a):
    ev, shots = items.chain_items()
    mc, mc_shots = items.mc_items(ev, shots)
    hashes = {
        "items_chain.jsonl": items.write_jsonl(ev, DATA / "items_chain.jsonl"),
        "shots_chain10.jsonl": items.write_jsonl(items.chain_shots10(ev),
                                                 DATA / "shots_chain10.jsonl"),
        "shots_chain.jsonl": items.write_jsonl(shots, DATA / "shots_chain.jsonl"),
        "items_mc.jsonl": items.write_jsonl(mc, DATA / "items_mc.jsonl"),
    }
    (DATA / "shots_mc.json").write_text(json.dumps({str(k): v for k, v in mc_shots.items()},
                                                   sort_keys=True, indent=1) + "\n")
    import hashlib
    hashes["shots_mc.json"] = hashlib.sha256((DATA / "shots_mc.json").read_bytes()).hexdigest()
    hashes["hops5r2.jsonl (gitignored, nocot sealed fact_hops)"] = hashlib.sha256(
        HOPS_PATH.read_bytes()).hexdigest()
    (DATA / "SHA256.json").write_text(json.dumps(hashes, indent=1) + "\n")
    print(json.dumps(hashes, indent=1))
    print(f"chain {len(ev)}, mc {len(mc)}")


def sfx(a) -> str:
    return ("" if a.shots == 1 else f"_s{a.shots}") + (
        "" if getattr(a, "placement", "target") == "target" else f"_{a.placement}")


def jobs_for(item_list, shots_of, arms, draws, temperature):
    return [{"item": it, "shots": shots_of(it), "arm": arm, "draw": d, "temperature": temperature}
            for it in item_list for arm in arms for d in range(draws)]


def project_usd(recipe: api.Recipe, jobs: list[dict], max_tokens: int = api.MAX_TOKENS,
                placement: str = "target") -> float:
    """Upper-ish projection: ~1.3 tokens per word of the prompt text (dots count 1 each) plus
    10 output tokens per call (max_tokens when reasoning is on), x1.5 for retries and error."""
    tot = 0.0
    for j in jobs:
        msgs = api.build_messages(j["item"], j["shots"], j["arm"], recipe.prefill, placement)
        words = sum(len(m["content"].split()) for m in msgs)
        tot += 1.3 * words * recipe.price_in + 10 * recipe.price_out
    return 1.5 * tot


def run(recipe: api.Recipe, jobs: list[dict], out: Path, tag: str, **kw) -> list[dict]:
    reconcile(quiet=True)
    proj = project_usd(recipe, jobs, kw.get("max_tokens", api.MAX_TOKENS),
                       kw.get("placement", "target"))
    spend.check_total(proj, kind="judge", exp=EXP)
    print(f"[stage1] {tag} {recipe.name}: {len(jobs)} jobs, projected ${proj:.3f}", flush=True)
    asker = api.Asker(api.openrouter_key())

    async def go():
        try:
            return await api.run_jobs(asker, recipe, jobs, out, tag, **kw)
        finally:
            await asker.close()

    try:
        rows = asyncio.run(go())
    finally:
        reconcile()
    return rows


def reconcile(quiet: bool = False):
    """Record (row costs - ledger) per (model, tag) as P06 judge spend. Holds an exclusive lock
    on results/stage1/.reconcile.lock so two drivers can't both record the same delta."""
    import fcntl

    RES.mkdir(parents=True, exist_ok=True)
    with open(RES / ".reconcile.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        _reconcile(quiet)


def _reconcile(quiet: bool):
    have = defaultdict(float)
    for e in spend.read_entries():
        if e.get("exp") == EXP and e.get("kind") == "judge":
            have[e.get("resource", "")] += float(e.get("usd") or 0.0)
    rows_cost, rows_n = defaultdict(float), defaultdict(int)
    for p in sorted(RES.glob("*.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            res = f"openrouter:{r['model']}:{r['tag']}"
            rows_cost[res] += float(r.get("cost") or 0.0)
            rows_n[res] += 1
    for res, c in sorted(rows_cost.items()):
        delta = c - have[res]
        if delta > 1e-6:
            spend.record("judge", res, rows_n[res], round(delta, 6),
                         note=f"P06 stage1 reconcile: row usage.cost total ${c:.6f}", exp=EXP)
            if not quiet:
                print(f"[spend] recorded ${delta:.4f} for {res}")
    if not quiet:
        t = spend.totals(exp=EXP)
        print(f"[spend] P06 totals: {t}")


def summarize(rows: list[dict], label: str):
    by = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        if r.get("status") != "ok":
            continue
        b = by[(r["arm"], r["k"])]
        b[0] += r["correct"]
        b[1] += 1
        b[2] += r["valid"]
    print(f"--- {label}: arm, k -> acc (n, valid)")
    for (arm, k), (c, n, v) in sorted(by.items()):
        print(f"  {arm:6s} k={k}: {c / n:.2f}  (n={n}, valid={v})")


def smoke_items(chain_ev):
    rng = random.Random(SMOKE_SEED)
    by_k = defaultdict(list)
    for e in chain_ev:
        by_k[e["k"]].append(e)
    pick = []
    for k in (2, 3, 4, 5, 6, 7, 8):  # 20 items: 6 at k=2 (the >= 70% gate), 14 over k=3..8
        pick += rng.sample(by_k[k], 6 if k == 2 else (3 if k <= 4 else 2))
    return pick


def cmd_smoke(a):
    recipe = api.RECIPES[a.model]
    chain_ev, chain_shots, *_ = load_all(a.shots)
    pick = smoke_items(chain_ev)
    jobs = jobs_for(pick, lambda _i: chain_shots, api.ARMS, 1, 0.0)
    rows = run(recipe, jobs, RES / f"smoke_{a.model}{sfx(a)}.jsonl", f"smoke{sfx(a)}")
    # positive control: reasoning on for 5 of the items, no prefill, room to think
    pc_jobs = jobs_for(pick[:5], lambda _i: chain_shots, ("none",), 1, 0.0)
    pc = run(recipe, pc_jobs, RES / f"smoke_{a.model}{sfx(a)}.jsonl", f"posctl{sfx(a)}",
             reasoning={"enabled": True}, max_tokens=2000)
    summarize(rows, f"smoke {a.model}")
    ok = [r for r in rows if r["status"] == "ok"]
    n = max(1, len(rows))
    clean = sum(not r["witness"]["reasoned"] and not r["witness"]["blind"] for r in ok) / n
    parsed = sum(r["graded"].get("predicted") is not None for r in ok) / n
    k2 = [r["correct"] for r in ok if r["k"] == 2]
    caught = [r["witness"]["reasoned"] for r in pc if r["status"] == "ok"]
    print(f"[smoke] witness-clean {clean:.3f} (>=0.95), parsed {parsed:.3f} (>=0.98), "
          f"k=2 acc {sum(k2) / max(1, len(k2)):.2f} (>=0.70, n={len(k2)}), "
          f"posctl caught {sum(caught)}/{len(caught)}, rtok "
          f"{[r['witness']['reasoning_tokens'] for r in pc if r['status'] == 'ok']}")


def cmd_pilot(a):
    """Placement pilot (log.md, 2026-10-11): chain k = 2, 3 (all 200) and fact_hops (64), the
    three arms, one draw at t=0, in the given placement."""
    recipe = api.RECIPES[a.model]
    chain_ev, chain_shots, _mc, _mc_shots, hops_ev, hops_shots = load_all(a.shots)
    pick = [e for e in chain_ev if e["k"] in (2, 3)]
    jobs = (jobs_for(pick, lambda _i: chain_shots, api.ARMS, 1, 0.0)
            + jobs_for(hops_ev, lambda _i: hops_shots, api.ARMS, 1, 0.0))
    run(recipe, jobs, RES / f"pilot_{a.model}{sfx(a)}.jsonl", f"pilot{sfx(a)}",
        placement=a.placement)


def cmd_bulk(a):
    recipe = api.RECIPES[a.model]
    chain_ev, chain_shots, mc, mc_shots, hops_ev, hops_shots = load_all(a.shots)
    jobs = []
    if "chain" in a.banks:
        jobs += jobs_for(chain_ev, lambda _i: chain_shots, api.ARMS, 2, 0.0)
    if "hops" in a.banks:
        jobs += jobs_for(hops_ev, lambda _i: hops_shots, api.ARMS, 2, 0.0)
    if "mc" in a.banks:
        jobs += jobs_for(mc, lambda i: mc_shots[len(i["options"])], ("none", "after"), 2, 0.0)
    run(recipe, jobs, RES / f"bulk_{a.model}{sfx(a)}.jsonl", f"bulk{sfx(a)}",
        placement=a.placement)


def cmd_select(a):
    """Items where the temperature-0 arms differ (after vs none or after vs before, on either
    draw): 10 samples at temperature 1 in each of the three arms."""
    recipe = api.RECIPES[a.model]
    chain_ev, chain_shots, *_ = load_all(a.shots)
    rows = api.latest_ok(RES / f"bulk_{a.model}{sfx(a)}.jsonl").values()
    acc = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["bank"] == "chain":
            acc[r["item_id"]][r["arm"]].append(r["correct"])
    differ = {i for i, d in acc.items()
              if len({tuple(d["after"]), tuple(d["none"]), tuple(d["before"])}) > 1}
    pick = [e for e in chain_ev if e["item_id"] in differ]
    print(f"[select] {len(pick)} chain items differ between arms")
    jobs = jobs_for(pick, lambda _i: chain_shots, api.ARMS, 10, 1.0)
    run(recipe, jobs, RES / f"select_{a.model}{sfx(a)}.jsonl", f"select{sfx(a)}",
        placement=a.placement)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("items")
    sub.add_parser("reconcile")
    for name in ("smoke", "pilot", "bulk", "select"):
        p = sub.add_parser(name)
        p.add_argument("--model", choices=sorted(api.RECIPES), required=True)
        p.add_argument("--shots", type=int, choices=(1, 10), default=1)
        p.add_argument("--placement", choices=("target", "every"), default="target")
        if name == "bulk":
            p.add_argument("--banks", default="chain,hops,mc",
                           help="comma list of chain, hops, mc")
    a = ap.parse_args()
    {"items": cmd_items, "reconcile": lambda _a: reconcile(), "smoke": cmd_smoke, "pilot": cmd_pilot,
     "bulk": cmd_bulk, "select": cmd_select}[a.cmd](a)


if __name__ == "__main__":
    main()
