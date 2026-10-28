"""Experiment 1's readout from its rows.jsonl: the primary, per-start split, sensitivity lines
and counts (compaction.s2.chain.readout). Code-scored, $0.

    uv run python scripts/week2_precedent_readout.py <rows.jsonl> [--out results/sprint/week2/precedent]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from compaction.s2.chain import readout as R


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rows", type=Path)
    ap.add_argument("--a-gate", type=Path, help="the run's a_gate.json (first stage per history)")
    ap.add_argument("--assignment", type=Path,
                    default=Path("configs/week2/assignments/precedent.json"),
                    help="the seeded assignment (randomization blocks: first task x round)")
    ap.add_argument("--out", type=Path, default=Path("results/sprint/week2/precedent"))
    a = ap.parse_args()
    forks = R.load_forks(a.rows)
    fs1 = None
    if a.a_gate:
        fs1 = {h: r.get("foreign_lost") for h, r in json.loads(a.a_gate.read_text())["rows"].items()}
    blocks = R.perm_blocks(json.loads(a.assignment.read_text()))
    res = R.readout(forks, first_stage=fs1, blocks=blocks)
    res["source"] = {"rows": str(a.rows), "rows_sha256":
                     hashlib.sha256(a.rows.read_bytes()).hexdigest(), "n_rows": len(forks)}
    res["source"]["assignment_sha256"] = hashlib.sha256(a.assignment.read_bytes()).hexdigest()
    if a.a_gate:
        res["source"]["a_gate_sha256"] = hashlib.sha256(a.a_gate.read_bytes()).hexdigest()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "readout.json").write_text(json.dumps(res, indent=1) + "\n")
    md = R.render(res)
    (a.out / "readout.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
