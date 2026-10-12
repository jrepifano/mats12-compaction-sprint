"""Seeded random raw samples for the note: summaries with their judge verdicts, and invented-fact
flags, drawn uniformly (never picked by hand).

    uv run python probes/P01-compaction-fidelity/p01/samples.py > results/P01/samples.md
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

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
N_SUMMARIES = 4
N_FLAGS = 12
N_STATUS = 6
MAX_CHARS = 1800


def main() -> None:
    ts = load_transcripts()
    with open(OUT / "scores_bulk.jsonl", encoding="utf-8") as f:
        scores = [json.loads(x) for x in f]
    audits = load_parsed(OUT / f"{AUDIT_OUT}_parsed.jsonl")
    invents = load_parsed(OUT / f"{INVENT_OUT}_parsed.jsonl")
    sums = {m: load_ok(OUT / "summaries" / f"{m}.jsonl") for m in SUMMARIZERS}
    rng = random.Random(SEED)

    def summary_text(iid):
        tid, h, m = iid.rsplit("|", 2)
        return sums[m][f"{tid}|{h}"]["text"]

    print("# P01 random raw samples (seed 20261011)\n")
    print("## Random summaries with their verdicts\n")
    for r in rng.sample(scores, N_SUMMARIES):
        t = ts[r["tid"]]
        txt = summary_text(r["item_id"])
        print(f"### {r['item_id']}\n")
        print(f"- Truth: suite `{t['truth']['last_test']['status']}`, script "
              f"`{t['truth']['last_script']['status']}`; slice {t['slice']}"
              + (f", cheat={t['source'].get('cheat')}" if t["slice"] == "B" else
                 f", resolved={t['truth'].get('resolved')}"))
        print(f"- Status statements: {json.dumps(audits[r['item_id']]['test_statements'])[:700]}")
        print(f"- Misstated: {r['status_misstated']}; dropped: {r['dropped']}; "
              f"not shown: {r['not_shown']}; invented flags: {r['n_invented']}\n")
        print("```\n" + txt[:MAX_CHARS] + ("\n[... truncated]" if len(txt) > MAX_CHARS else "")
              + "\n```\n")
    print("## Random status flags (luna + code; F2 found most false-pass flags wrong)\n")
    mis = [r for r in scores if r["status_misstated"]]
    for r in rng.sample(mis, min(N_STATUS, len(mis))):
        t = ts[r["tid"]]
        lt = t["truth"]["last_test"]
        print(f"- `{r['item_id']}` ({r['status_kind']}): truth suite `{lt['status']}` "
              f"({lt['evidence'][:80]}), script `{t['truth']['last_script']['status']}`; "
              f"statements: {json.dumps(audits[r['item_id']]['test_statements'])[:600]}")
    print("\n## Random invented-fact flags (luna, unvalidated)\n")
    flags = [(iid, f) for iid, v in sorted(invents.items()) for f in v["invented"]]
    for iid, f in rng.sample(flags, min(N_FLAGS, len(flags))):
        print(f"- `{iid}` **{f['type']}**: \"{f['claim'][:220]}\" — luna: {f['why'][:220]}")


if __name__ == "__main__":
    main()
