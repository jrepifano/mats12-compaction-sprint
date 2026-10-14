"""Seeded random raw samples for note.md (AGENTS.md: random, never cherry-picked):
results/samples.md with, per condition, N random continuations (the sentence, the first new
turn's reasoning, the final message, the detector label), plus random forks with their tags.

  PYTHONPATH=probes/P41-grader-belief-atrisk:src:scripts uv run --group swarm \
      python -m p41.samples [--n 2]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys

from p41 import contracts as C
from p41.sources import reasoning_of


def _clip(s: str | None, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n // 2] + " … " + s[-n // 2:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2)
    a = ap.parse_args(argv)
    rng = random.Random(f"{C.SEED}:samples")
    with open(C.MEASURES_CSV, newline="") as f:
        meas = {r["episode_id"]: r for r in csv.DictReader(f)}
    out = ["# P41 random raw samples", "",
           (f"Drawn by `p41/samples.py` with `random.Random('{C.SEED}:samples')`; "
            "texts clipped (middle elided)."), ""]
    conds = sorted(d.name for d in C.EPISODES_DIR.iterdir() if d.is_dir())
    for cond in conds:
        paths = sorted((C.EPISODES_DIR / cond).glob("*.json"))
        out += [f"## Condition {cond}", ""]
        for p in rng.sample(paths, min(a.n, len(paths))):
            art = json.loads(p.read_text())
            m = art["meta"]
            first = next((x for x in art["messages"] if x["role"] == "assistant"), None)
            row = meas.get(m["episode_id"], {})
            out += [(f"**{m['episode_id']}**: cheat={row.get('cheat')!s} "
                     f"end={art['end_reason']} turns={art['n_turns']}"), "",
                    f"- Inserted: {m['sentence'] or '(none)'}",
                    f"- First new reasoning: {_clip(reasoning_of(first) if first else '', 600)}",
                    f"- Final message: {_clip(art.get('final_message'), 400)}", ""]
    if C.FORKS_JSONL.exists():
        forks = [json.loads(x) for x in C.FORKS_JSONL.read_text().splitlines() if x.strip()]
        tags = {}
        fc = C.RESULTS_DIR / "forks.csv"
        if fc.exists():
            with open(fc, newline="") as f:
                tags = {r["fork_id"]: r for r in csv.DictReader(f)}
        out += ["## Decision-point forks", ""]
        for fk in rng.sample(forks, min(4, len(forks))):
            fid = f"{fk['site_id']}#f{fk['fork']}"
            t = tags.get(fid, {})
            on = [k for k in ("visible_tests", "spec_hidden", "cheat_check", "game_plan")
                  if t.get(k) == "1"]
            ds = [k for k in ("visible_tests", "spec_hidden") if t.get(f"{k}_ds") == "1"]
            gm = [k for k in ("visible_tests", "spec_hidden") if t.get(f"{k}_gm") == "1"]
            out += [f"**{fid}**: consensus tags {on or '[]'}; DeepSeek {ds}; Gemini {gm}", "",
                    f"- Reasoning: {_clip(fk.get('reasoning'), 600)}", ""]
    path = C.RESULTS_DIR / "samples.md"
    path.write_text("\n".join(out) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
