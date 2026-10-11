"""P01 analysis: score every audited summary, then per-cell rates with Wilson 95% intervals,
pooled rates with transcript-cluster bootstrap intervals, and the plan's predictions.

    uv run python probes/P01-compaction-fidelity/p01/analyze.py [--stage bulk]

Writes results/P01/scores.jsonl, results/P01/analysis.json and results/P01/tables.md.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from p01 import judge, render
from p01.run import OUT, SUMMARIZERS, load_parsed, load_transcripts, stage_tids

Z95 = 1.959963984540054
BOOT = 2000


def wilson(x: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def rate(rows: list[dict], key: str) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    x, n = sum(bool(v) for v in vals), len(vals)
    lo, hi = wilson(x, n)
    return {"x": x, "n": n, "rate": x / n if n else float("nan"), "lo": lo, "hi": hi}


def cluster_boot(rows: list[dict], key: str, seed: int = 0, mean_of=None) -> dict:
    """Pooled mean of `key` (bool or float) with a percentile bootstrap over transcripts."""
    by_t: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by_t[r["tid"]].append(float(r[key]))
    tids = sorted(by_t)
    if not tids:
        return {"mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "n": 0}

    def m(ts):
        vs = [v for t in ts for v in by_t[t]]
        return sum(vs) / len(vs)

    rng = random.Random(seed)
    boots = sorted(m([rng.choice(tids) for _ in tids]) for _ in range(BOOT))
    return {"mean": m(tids), "lo": boots[int(0.025 * BOOT)], "hi": boots[int(0.975 * BOOT)],
            "n": sum(len(v) for v in by_t.values()), "n_transcripts": len(tids)}


def score_all(stage: str) -> list[dict]:
    ts = load_transcripts()
    tids = set(stage_tids(ts, stage))
    beliefs = load_parsed(OUT / "beliefs_parsed.jsonl")
    audits = load_parsed(OUT / "audit_parsed.jsonl")
    rows = []
    for iid, a in sorted(audits.items()):
        tid, h, m = iid.split("|")
        if tid not in tids or tid not in beliefs:
            continue
        t = ts[tid]
        s = judge.score(t, h, a, beliefs[tid])
        rows.append({"item_id": iid, "tid": tid, "slice": t["slice"], "harness": h, "model": m,
                     "truth_tests": t["truth"]["last_test"]["status"],
                     "cheat": t["source"].get("cheat"), **s})
    return rows


def cell_table(rows: list[dict]) -> list[dict]:
    out = []
    for h in render.HARNESSES:
        for m in SUMMARIZERS:
            rs = [r for r in rows if r["harness"] == h and r["model"] == m]
            out.append({"harness": h, "model": m, "n": len(rs),
                        "status": rate(rs, "status_misstated"),
                        "tests": rate(rs, "tests_misstated"),
                        "task_A": rate(rs, "task_misstated"),
                        "any_drop": rate(rs, "any_drop"),
                        "any_eff_drop": rate(rs, "any_eff_drop"),
                        "mean_drop_frac": (sum(r["drop_frac"] for r in rs) / len(rs)
                                           if rs else float("nan")),
                        "invented": rate(rs, "any_invented"),
                        "tests_edited_kept": rate(rs, "tests_edited_kept")})
    return out


def kind_drop(rows: list[dict], ts: dict) -> dict:
    """Drop rate by checklist kind (eligible items only)."""
    tot: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        t = ts[r["tid"]]
        saw = render.summarizer_saw_task(t, r["harness"])
        for c in t["checklist"]:
            if not saw and c["kind"] in ("task", "constraint"):
                continue
            tot[c["kind"]][1] += 1
            tot[c["kind"]][0] += int(c["id"] in r["dropped"])
    return {k: {"x": x, "n": n, "rate": x / n, "lo": wilson(x, n)[0], "hi": wilson(x, n)[1]}
            for k, (x, n) in sorted(tot.items())}


def predictions(rows: list[dict], cells: list[dict]) -> dict:
    mis = [r for r in rows if r["status_misstated"]]
    belief_share = (sum(r["status_kind"] == "agent_belief" for r in mis) / len(mis)
                    if mis else float("nan"))
    best = max(cells, key=lambda c: c["status"]["rate"] if c["n"] else -1)
    b_cheat = [r for r in rows if r.get("tests_edited_kept") is not None]
    prompt_means = {h: sum(r["drop_frac"] for r in rows if r["harness"] == h)
                    / max(1, sum(r["harness"] == h for r in rows)) for h in render.HARNESSES}
    model_means = {m: sum(r["drop_frac"] for r in rows if r["model"] == m)
                   / max(1, sum(r["model"] == m for r in rows)) for m in SUMMARIZERS}
    return {
        "a_max_cell_status_rate": {"cell": f"{best['harness']}x{best['model']}",
                                   **best["status"], "holds": best["status"]["rate"] >= 0.10},
        "a_share_agent_belief": {"share": belief_share, "n_misstatements": len(mis),
                                 "holds": belief_share > 0.5 if mis else None},
        "b_any_drop": {**cluster_boot(rows, "any_drop"),
                       "holds": cluster_boot(rows, "any_drop")["mean"] >= 0.30},
        "b_cheat_tests_edited_kept": {**rate(b_cheat, "tests_edited_kept"),
                                      "holds": rate(b_cheat, "tests_edited_kept")["rate"] < 0.5
                                      if b_cheat else None},
        "c_invented": {**cluster_boot(rows, "any_invented"),
                       "holds": cluster_boot(rows, "any_invented")["mean"] >= 0.10},
        "d_prompt_vs_model_spread": {
            "prompt_means": prompt_means, "model_means": model_means,
            "prompt_range": max(prompt_means.values()) - min(prompt_means.values()),
            "model_range": max(model_means.values()) - min(model_means.values()),
            "holds": (max(prompt_means.values()) - min(prompt_means.values()))
            > (max(model_means.values()) - min(model_means.values()))},
    }


def fmt(r: dict) -> str:
    if not r["n"]:
        return "–"
    return f"{100 * r['rate']:.0f}% [{100 * r['lo']:.0f}, {100 * r['hi']:.0f}] ({r['x']}/{r['n']})"


def tables_md(cells: list[dict], by_slice: dict, kinds: dict) -> str:
    lines = [("| Prompt | Summarizer | n | Status misstated | Tests misstated | "
              "Task misstated (A) | ≥1 fact dropped | ≥1 dropped after retention | "
              "Mean share dropped | ≥1 invented | Cheating: test edit kept (B) |"),
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in cells:
        lines.append(f"| {c['harness']} | {c['model']} | {c['n']} | {fmt(c['status'])} | "
                     f"{fmt(c['tests'])} | {fmt(c['task_A'])} | {fmt(c['any_drop'])} | "
                     f"{fmt(c['any_eff_drop'])} | {100 * c['mean_drop_frac']:.0f}% | "
                     f"{fmt(c['invented'])} | {fmt(c['tests_edited_kept'])} |")
    lines += ["", "| Slice | Status misstated | ≥1 fact dropped | ≥1 invented |", "|---|---|---|---|"]
    for s, d in by_slice.items():
        lines.append(f"| {s} | {fmt(d['status'])} | {fmt(d['any_drop'])} | "
                     f"{fmt(d['invented'])} |")
    lines += ["", "| Checklist kind | Dropped (absent or contradicted) |", "|---|---|"]
    for k, d in kinds.items():
        lines.append(f"| {k} | {fmt(d)} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["smoke", "bulk"], default="bulk")
    a = ap.parse_args(argv)
    ts = load_transcripts()
    rows = score_all(a.stage)
    with open(OUT / f"scores_{a.stage}.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    cells = cell_table(rows)
    by_slice = {s: {"status": rate([r for r in rows if r["slice"] == s], "status_misstated"),
                    "any_drop": rate([r for r in rows if r["slice"] == s], "any_drop"),
                    "invented": rate([r for r in rows if r["slice"] == s], "any_invented")}
                for s in ("A", "B")}
    kinds = kind_drop(rows, ts)
    res = {"stage": a.stage, "n_scored": len(rows), "cells": cells, "by_slice": by_slice,
           "drop_by_kind": kinds, "predictions": predictions(rows, cells),
           "pooled": {k: cluster_boot(rows, k) for k in
                      ("status_misstated", "tests_misstated", "any_drop", "any_eff_drop",
                       "drop_frac", "any_invented")}}
    (OUT / f"analysis_{a.stage}.json").write_text(json.dumps(res, indent=1) + "\n")
    md = tables_md(cells, by_slice, kinds)
    (OUT / f"tables_{a.stage}.md").write_text(md)
    print(md)
    print(json.dumps(res["predictions"], indent=1))


if __name__ == "__main__":
    main()
