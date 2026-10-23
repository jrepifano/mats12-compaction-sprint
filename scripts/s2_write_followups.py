"""Write S2's frozen follow-up distribution from S1's verbatim-budget check (S2 spec, "Follow-up
turns"; `compaction.s2.followups.write_distribution`).

One draw per (Part A session, early user turn k < 3): the number of later user requests and their
estimated tokens (UTF-8 bytes / 4, as `s1_analysis.verbatim_budget` counts) up to the compaction.
Only sources whose later turns come from a person (`HUMAN_SOURCES`): frontierml and
posttrainbench are autonomous benchmark runs whose only user turn is the task, so their draws are
all (0, 0) and would describe the harness, not users. $0, no model calls.

    uv run python scripts/s2_write_followups.py [--out results/sprint/S2/followup_distribution.json]
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import time
from pathlib import Path

from compaction import s1_analysis as A
from compaction import s1_data
from compaction.s2 import followups

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "raw" / "sprint" / "s1" / "manifest.json"  # as s1_analyze
OUT = ROOT / "results/sprint/S2/followup_distribution.json"
HUMAN_SOURCES = ("swe-chat-v1", "jacob")
EARLY_K = 3


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    ids = json.loads(Path(a.manifest).read_text())["ids"]["part_a"]
    vb = A.verbatim_budget((s1_data.load(r) for r in ids), early_k=EARLY_K)
    draws, by_src = [], collections.Counter()
    for row in vb["records"]:
        if row["source"] not in HUMAN_SOURCES:
            continue
        for t in row["turns"]:
            draws.append((t["later_requests"], t["after_tokens"]))
            by_src[row["source"]] += 1
    turns = [d[0] for d in draws]
    toks = [d[1] for d in draws]
    notes = (
        f"S1 Part A, sources {', '.join(HUMAN_SOURCES)} only (autonomous benchmark sources have no "
        f"later user turns); early user turns k < {EARLY_K}; draws per source {dict(by_src)}; "
        f"turns median {statistics.median(turns)}, tokens median {statistics.median(toks)}"
    )
    p = followups.write_distribution(
        a.out,
        draws,
        source=f"S1 verbatim-budget check ({len(ids)} Part A records, manifest {Path(a.manifest).name})",
        created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        notes=notes,
    )
    _, prov = followups.load_distribution(p)
    print(f"S2_FOLLOWUPS wrote {p} ({prov['n']} draws, sha256 {prov['sha256'][:16]}): {notes}")


if __name__ == "__main__":
    main()
