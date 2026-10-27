"""Experiment 2's second scenario: build P38's monitor items ($0, no API calls).

Reads probe P38's saved contexts, summaries and episodes (read-only; episodes are gitignored and
live in the main checkout's probe directory) and writes

  data/raw/sprint/s2/monitors/items_p38.jsonl            one Item per line (gitignored)
  results/sprint/week2/monitors/items_p38_manifest.json  counts per set, per-item view token
                                                         estimates (bytes/4) and sha256s, checks

    uv run python scripts/week2_monitor_p38_items.py [--p38-dir DIR] [--out-dir DIR]

Exits 1 if any item fails `p38.check_item` (the plan's cut and view checks) or a count differs
from the plan's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

from compaction.s2.chain.monitors import p38 as P
from compaction.s2.chain.monitors.items import VIEWS

OUT_DIR = Path("results/sprint/week2/monitors")
ITEMS_DIR = Path("data/raw/sprint/s2/monitors")


def view_tokens(msgs: list[dict]) -> int:
    return len(json.dumps(msgs, ensure_ascii=False).encode()) // 4


def manifest(items: list, p38_dir: Path, items_path: Path) -> dict:
    per_item, problems = [], {}
    sizes: dict[str, list[int]] = {v: [] for v in VIEWS}
    for it in items:
        toks = {v: view_tokens(m) for v, m in it.views.items()}
        for v, t in toks.items():
            sizes[v].append(t)
        probs = P.check_item(it)
        if probs:
            problems[it.id] = probs
        per_item.append({
            "id": it.id, "set": it.set, "authorized": it.authorized, "cluster": it.cluster,
            "draw": it.meta["draw"], "act_command": it.act_command,
            "view_tokens_est": toks,
            "view_sha256": {v: it.view_sha256(v) for v in it.views},
            "phase_d_calls": it.meta["phase_d_calls"],
            "phase_d_calls_with_reasoning": it.meta["phase_d_calls_with_reasoning"],
            "truth_recorded": it.evidence["truth_recorded"],
            "checks_ok": not probs,
        })
    counts = Counter(it.set for it in items)
    clusters = {s: len({it.cluster for it in items if it.set == s}) for s in P.SETS}
    return {
        "scenario": P.SCENARIO, "model": P.MODEL, "seed": P.SEED,
        "p38_dir": str(p38_dir),
        "items_jsonl": str(items_path),
        "items_jsonl_sha256": hashlib.sha256(items_path.read_bytes()).hexdigest(),
        "counts": {s: counts.get(s, 0) for s in P.SETS},
        "expected_counts": {s: e for s, (_, _, e) in P.SETS.items()},
        "contexts_per_set": clusters,
        "p38_arm_per_set": {s: a for s, (a, _, _) in P.SETS.items()},
        "authorized_draws": {it.cluster: it.meta["draw"] for it in items if it.authorized},
        "views_per_set": {s: list(next(it.views for it in items if it.set == s))
                          for s in P.SETS if counts.get(s)},
        "view_tokens_est_summary": {
            v: {"n": len(x), "median": statistics.median(x), "max": max(x), "min": min(x)}
            for v, x in sizes.items() if x},
        "checks": {
            "items_checked": len(items), "items_failing": len(problems),
            "problems": problems,
            "counts_match": all(counts.get(s, 0) == e for s, (_, _, e) in P.SETS.items()),
            "policy_in_every_view": not any("operator policy" in p
                                            for ps in problems.values() for p in ps),
            "truth_inferred_items": [it.id for it in items
                                     if not it.evidence["truth_recorded"]],
        },
        "items": per_item,
        "notes": [
            ("View 1 for p38_natural and p38_false_licence is counterfactual: the agent "
             "resumed from the summary (system + pi-wrapped summary), not the full history."),
            "P38 kept no verbatim tail, so summary_tail is system + summary + phase D.",
            ("p38_authorized (sbx_world) resumed from the full sandbox history; it has no "
             "summary, so it gets full, current_only and current_plus_reasoning only."),
            ("The operator policy is in every view's system message; meta.policy_preamble "
             "carries it for renderers whose channel drops system messages."),
            "Token estimates are bytes of the JSON message list / 4.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--p38-dir", type=Path, default=P.P38_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    items = P.build_items(a.p38_dir, check_counts=False)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    items_path = ITEMS_DIR / "items_p38.jsonl"
    items_path.parent.mkdir(parents=True, exist_ok=True)
    with items_path.open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it.to_json(), ensure_ascii=False) + "\n")
    m = manifest(items, a.p38_dir, items_path)
    (a.out_dir / "items_p38_manifest.json").write_text(
        json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    c = m["checks"]
    print(f"items: {m['counts']} (expected {m['expected_counts']}); "
          f"failing checks: {c['items_failing']}")
    for v, s in m["view_tokens_est_summary"].items():
        print(f"  {v:24s} n={s['n']:3d} median={s['median']:>7} max={s['max']:>7}")
    return 0 if c["counts_match"] and not c["items_failing"] else 1


if __name__ == "__main__":
    sys.exit(main())
