"""F2 (exploratory): precision of luna's flags, adjudicated against the transcript by an Opus
subagent (an LLM reference, not hand labels; log.md F2).

    uv run python probes/P01-compaction-fidelity/p01/precision.py build   # seeded samples
    (the subagent writes results/P01/precision/labels.jsonl)
    uv run python probes/P01-compaction-fidelity/p01/precision.py score

Samples (seed 20261011), uniform over bulk items:
- `invented`: 40 invented-fact flags;
- `status`: 30 status misstatements (any kind);
- `drop`: 30 luna-scored dropped facts (not file facts, which are scored in code).
Each item gets the summary, the claim/fact, and the judge view of the transcript written to
results/P01/precision/views/<n>.txt, so the adjudicator reads exactly what luna read.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from p01 import render
from p01.analyze import wilson
from p01.run import (
    AUDIT_OUT,
    INVENT_OUT,
    OUT,
    SUMMARIZERS,
    load_ok,
    load_parsed,
    load_transcripts,
)

SEED = 20261011
N = {"invented": 40, "status": 30, "drop": 30}
PDIR = OUT / "precision"


def build() -> None:
    ts = load_transcripts()
    with open(OUT / "scores_bulk.jsonl", encoding="utf-8") as f:
        scores = [json.loads(x) for x in f]
    audits = load_parsed(OUT / f"{AUDIT_OUT}_parsed.jsonl")
    invents = load_parsed(OUT / f"{INVENT_OUT}_parsed.jsonl")
    sums = {m: load_ok(OUT / "summaries" / f"{m}.jsonl") for m in SUMMARIZERS}
    rng = random.Random(SEED)
    pools = {
        "invented": [(iid, f) for iid, v in sorted(invents.items()) for f in v["invented"]],
        "status": [(r["item_id"], audits[r["item_id"]]["test_statements"]) for r in scores
                   if r["status_misstated"]],
        "drop": [(r["item_id"], cid) for r in scores for cid in r["dropped"]
                 if not cid.startswith("file")],
    }
    (PDIR / "views").mkdir(parents=True, exist_ok=True)
    items, views = [], {}
    for kind, pool in pools.items():
        for iid, x in rng.sample(pool, min(N[kind], len(pool))):
            tid, h, m = iid.rsplit("|", 2)
            t = ts[tid]
            if tid not in views:
                views[tid] = f"views/{len(views):03d}.txt"
                (PDIR / views[tid]).write_text(render.judge_view(t), encoding="utf-8")
            it = {"n": len(items), "kind": kind, "item_id": iid, "view": views[tid],
                  "summary": sums[m][f"{tid}|{h}"]["text"]}
            if kind == "invented":
                it["claim"] = x
            elif kind == "status":
                lt, ls = t["truth"]["last_test"], t["truth"]["last_script"]
                it["statements"] = x
                it["code_truth"] = {
                    "slice": t["slice"], "suite": [lt["status"], lt["command"][:200],
                                                   lt["evidence"][:200]],
                    "script": [ls["status"], ls["command"][:200], ls["evidence"][:200]],
                    "slice_B_pass_before_cut": t["truth"].get("pass_before_cut"),
                    "resolved": t["truth"].get("resolved")}
            else:
                it["fact"] = next(c["text"] for c in t["checklist"] if c["id"] == x)
            items.append(it)
    with open(PDIR / "items.jsonl", "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"wrote {len(items)} items, {len(views)} transcript views to {PDIR}")


def score() -> None:
    with open(PDIR / "items.jsonl", encoding="utf-8") as f:
        items = {json.loads(x)["n"]: json.loads(x) for x in f}
    with open(PDIR / "labels.jsonl", encoding="utf-8") as f:
        labels = [json.loads(x) for x in f]
    res = {}
    for kind in N:
        ls = [lb for lb in labels if items[lb["n"]]["kind"] == kind]
        x = sum(1 for lb in ls if lb["luna_correct"])
        lo, hi = wilson(x, len(ls))
        res[kind] = {"luna_correct": x, "n": len(ls), "precision": x / len(ls) if ls else None,
                     "lo": lo, "hi": hi}
    (PDIR / "summary.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"build": build, "score": score}[sys.argv[1]]()
