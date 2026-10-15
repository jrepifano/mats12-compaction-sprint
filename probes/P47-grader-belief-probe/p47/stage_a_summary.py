"""P47 Stage A summary for note.md: per model, from the U draws, X3's detector and luna's tags.

- cheat rate over draws and task-equal (each task's draws weighted to sum to 1), with a
  task-cluster bootstrap 95% CI (10,000 resamples of tasks, seeded);
- grader-talk rate (any luna statement in the site prefix or the continuation), same CIs;
- cheating by the continuation's talk (none / any) and by its labels (exploratory);
- site spread: sites with 0, some or all of their draws cheating;
- end reasons, mean turns and mean charge per continuation.

Writes results/stage_a_summary.json.

    PYTHONPATH=probes/P47-grader-belief-probe:src:scripts \
        uv run --group swarm python -m p47.stage_a_summary [--models a,b,c]
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter, defaultdict

import numpy as np

from p47 import gate as G

N_BOOT = 10_000


def _rate(rows: list[dict], key: str, task_equal: bool) -> float:
    if not rows:
        return float("nan")
    if not task_equal:
        return float(np.mean([bool(r[key]) for r in rows]))
    by = defaultdict(list)
    for r in rows:
        by[r["task_id"]].append(bool(r[key]))
    return float(np.mean([np.mean(v) for v in by.values()]))


def boot(rows: list[dict], key: str, task_equal: bool, seed: int) -> dict:
    """Point estimate and task-cluster bootstrap percentile 95% CI."""
    by = defaultdict(list)
    for r in rows:
        by[r["task_id"]].append(r)
    tasks = sorted(by)
    rng = np.random.default_rng(seed)
    est = []
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(tasks), len(tasks))
        rs, tid = [], 0
        for i in pick:              # relabel so a task drawn twice counts as two clusters
            tid += 1
            rs += [{**r, "task_id": tid} for r in by[tasks[i]]]
        est.append(_rate(rs, key, task_equal))
    lo, hi = np.percentile(est, [2.5, 97.5])
    return {"est": round(_rate(rows, key, task_equal), 4), "lo": round(float(lo), 4),
            "hi": round(float(hi), 4)}


def summarise(slug: str, seed: int) -> dict:
    C = G._load_c(slug)
    from p47 import tag as T

    importlib.reload(T)
    ds = [d for d in G.draws(C) if d["cheat"] is not None]
    pre = {r["site_id"]: r for r in T.load("prefix").values() if r.get("status") == "scored"}
    cont = {r["episode_id"]: r for r in T.load("cont").values() if r.get("status") == "scored"}
    rows = []
    for d in ds:
        p, c = pre.get(d["site_id"]), cont.get(d["episode_id"])
        if p is None or c is None:
            continue
        cs = c.get("statements") or []
        rows.append({**d, "talk": bool((p.get("statements") or []) + cs),
                     "talk_cont": bool(cs), "labels_cont": sorted({s["label"] for s in cs})})
    by_talk = Counter((r["talk_cont"], r["cheat"]) for r in rows)
    by_label = defaultdict(lambda: [0, 0])
    for r in rows:
        for lab in r["labels_cont"] or ["none"]:
            by_label[lab][0] += r["cheat"]
            by_label[lab][1] += 1
    site = defaultdict(list)
    for d in ds:
        site[d["site_id"]].append(d["cheat"])
    spread = Counter("all" if all(v) else "none" if not any(v) else "some"
                     for v in site.values())
    return {
        "model": slug, "draws": len(ds), "tagged": len(rows), "sites": len(site),
        "tasks": len({d["task_id"] for d in ds}),
        "cheats": sum(d["cheat"] for d in ds),
        "cheat_tasks": len({d["task_id"] for d in ds if d["cheat"]}),
        "cheat_rate_draws": boot(ds, "cheat", False, seed),
        "cheat_rate_task_equal": boot(ds, "cheat", True, seed + 1),
        "talk_rate_draws": boot(rows, "talk", False, seed + 2) if rows else None,
        "talk_prefix_sites": sum(bool(r.get("statements")) for r in pre.values()),
        "prefix_sites_tagged": len(pre),
        "cheat_by_cont_talk": {"none": [by_talk[(False, True)],
                                        by_talk[(False, True)] + by_talk[(False, False)]],
                               "any": [by_talk[(True, True)],
                                       by_talk[(True, True)] + by_talk[(True, False)]]},
        "cheat_by_cont_label": {k: v for k, v in sorted(by_label.items())},
        "site_spread": dict(spread),
        "end_reasons": dict(Counter(d["end_reason"] for d in ds)),
        "mean_turns": round(float(np.mean([d["n_turns"] for d in ds])), 2) if ds else None,
        "mean_charge_usd": round(float(np.mean([d["charge"] for d in ds])), 5) if ds else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gpt-oss-120b,gpt-oss-20b,qwen3.6-27b")
    a = ap.parse_args(argv)
    from p47 import contracts

    out = {"seed": contracts.SEED, "n_boot": N_BOOT,
           "results": [summarise(m, contracts.SEED) for m in a.models.split(",")]}
    path = G.PROBE / "results" / "stage_a_summary.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
