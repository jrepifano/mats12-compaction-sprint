"""Smoke gates (plan.md "Automatic rules") over Arm H artifacts: the block is in the resumed
context exactly once when intact and absent when removed; the removed summary keeps >= 200
characters; the block is invented (fails P01's copied check against the context's transcript);
and >= 1 episode reaches `done`.

    uv run python probes/P10-fabricated-history-trust/p10/gates.py [--glob 'deepseek*/semi/*']
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p10 import armh  # noqa: E402
from p10 import common as K  # noqa: E402


def check(art: dict, views: dict[str, str]) -> list[str]:
    from p07.measures import visible_text

    m, block = art["meta"], art.get("p10_block") or ""
    summary_msg = visible_text(art["initial_messages"][2].get("content"))
    probs = []
    n = summary_msg.count(block) if block else -1
    if m["cond"] == "intact" and n != 1:
        probs.append(f"intact: block occurs {n}x")
    if m["cond"] == "removed" and n != 0:
        probs.append(f"removed: block occurs {n}x")
    if m["cond"] == "removed" and K.first_marker(summary_msg) is not None:
        probs.append("removed: a continuation marker remains")
    if m["cond"] == "removed" and len(summary_msg) < K.MIN_REMOVED_CHARS:
        probs.append("removed: < 200 characters")
    if not K.invented_blocks(block, views[m["ctx_id"]]):
        probs.append("block is not invented")
    if not (art.get("plant_check") or {}).get("ok"):
        probs.append(f"P07 plant check: {(art.get('plant_check') or {}).get('problems')}")
    return probs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="*/*/*/*/*.json")
    a = ap.parse_args(argv)
    views = {c["ctx_id"]: K.p07_view(c) for c in K.load_jsonl(K.P07_CONTEXTS)}
    paths = [p for p in sorted(armh.EPISODES.glob(a.glob)) if ".failed" not in p.name]
    bad, done = 0, 0
    for p in paths:
        art = json.loads(p.read_text())
        probs = check(art, views)
        done += art.get("end_reason") == "done"
        bad += bool(probs)
        if probs:
            print(p.relative_to(armh.EPISODES), probs)
    print(json.dumps({"artifacts": len(paths), "failing": bad, "reached_done": done}))
    return 1 if bad or not done else 0


if __name__ == "__main__":
    raise SystemExit(main())
