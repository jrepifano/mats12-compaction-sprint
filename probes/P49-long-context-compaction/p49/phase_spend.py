"""Billed spend and UTC window by phase, from the request checkpoints (note.md "Spend and
wall-clock"). Run from the probe dir: `python -m p49.phase_spend`. Chains map to their stage via
results/chains.jsonl; summary calls (<hid>-summary) go to h0-h7 (Gate 0 / gate0x) or the bulk."""
from __future__ import annotations

import collections
import json

from p49 import contracts as C


def main() -> int:
    stage_of = {}
    with open(C.CHAIN_SUMMARY_JSONL) as f:
        for line in f:
            r = json.loads(line)
            stage_of[r["chain_id"]] = r.get("stage")
    usd: collections.Counter = collections.Counter()
    first: dict = {}
    last: dict = {}
    with open(C.CHECKPOINTS) as f:
        for line in f:
            r = json.loads(line)
            if r.get("event") not in ("response", "retry") or not r.get("cost"):
                continue
            cid = r["chain_id"]
            if cid.startswith("smoke"):
                st = "smoke"
            elif cid.endswith("-summary"):
                st = "summaries h0-h7" if int(cid.split("-")[0][1:]) < 8 else "bulk"
            else:
                st = stage_of.get(cid, "?")
            usd[st] += r["cost"]
            first[st] = min(first.get(st, r["ts"]), r["ts"])
            last[st] = max(last.get(st, r["ts"]), r["ts"])
    for k in sorted(usd):
        print(f"{k:18s} ${usd[k]:7.2f}  {first[k][11:16]}-{last[k][11:16]} UTC")
    print("total", round(sum(usd.values()), 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
