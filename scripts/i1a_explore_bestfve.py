"""EXPLORATORY, outside the prereg: best-FVE selection on multi-token recall, scored by the free
regex scorer (Jacob, 2026-09-30).

Prereg v0.4 §4.7 drops the best-FVE rule because `fve_joint_band` failed (mean joint FVE 0.370
against 0.487 ± 0.10); the other reconstructor checks passed. This script applies the rule anyway
(`selection.bestfve_pick` over the FveRows P1 computed) and writes everything under
`results/I1a/exploratory/bestfve/`, never into the preregistered `sel_bestfve` arm. No calls: the
recall family is scored by regex, and no API key is loaded.

    uv run python scripts/i1a_explore_bestfve.py
"""

from __future__ import annotations

import json
import os
from collections import Counter

from emx.i1a import analysis as A
from emx.i1a import contracts as C
from emx.i1a import judge_driver as J
from emx.i1a import selection as S

FAMILY = "basic_readout_mt"
OUT = C.RESULTS / "exploratory" / "bestfve"
COMPARE = ("olens_rl600", "sel_consensus", "olens_s1", "olens_s2", "olens_s3", "olens_s4")


def item_pass(results_path) -> dict[str, float]:
    rows = json.loads(results_path.read_text())["rows"]
    return {r["id"]: float(bool(r["pass"])) for r in rows}


def main() -> int:
    os.environ.pop("OPENROUTER_API_KEY", None)  # regex only: a call would fail, never spend
    groups = S.load_samples(FAMILY)
    fve = S.load_fve(FAMILY)
    tokens = S.load_site_tokens(FAMILY)
    rows, picks = [], []
    for key, samples in sorted(groups.items(), key=S._cell_order):
        idxs = [int(s["sample_idx"]) for s in samples]
        assert idxs == list(range(C.N_SAMPLES)), (key, idxs)
        frows = [fve[(*key, k)] for k in idxs]
        pick = S.bestfve_pick(
            [float(f["joint_fve"]) for f in frows], [bool(f["valid"]) for f in frows]
        )
        rows.append(S._contract_row(samples[pick], samples[pick]["text"], tokens))
        picks.append({"id": key[0], "layer": key[1], "pos": key[2], "sample_idx": pick})
    readouts = S._write_jsonl(OUT / "readouts" / f"{FAMILY}.jsonl", rows)
    S._write_jsonl(OUT / f"picks_{FAMILY}.jsonl", picks)

    judged = OUT / "judged" / FAMILY
    judged.mkdir(parents=True, exist_ok=True)
    argv = J._argv(
        FAMILY,
        readouts,
        judged,
        items=None,
        limit=0,
        layers=None,
        opts=(),
        allow_missing=False,
        dry_run=False,
    )
    code = J.run_wsbench(argv)
    if code:
        print(f"wsbench exited {code}")
        return code
    res = json.loads((judged / "results.json").read_text())
    assert res["numbers"]["extras"]["n_calls"] == 0 and res["complete"], "not a free, complete run"
    best = item_pass(judged / "results.json")

    summary = {
        "exploratory": True,
        "why": "prereg §4.7 drops best FVE (fve_joint_band failed); run at Jacob's request",
        "n_cells": len(picks),
        "pick_counts": dict(sorted(Counter(p["sample_idx"] for p in picks).items())),
        "bestfve": res["numbers"],
        "vs": {},
    }
    for arm in COMPARE:
        other = item_pass(C.judged_dir(arm, FAMILY) / "results.json")
        summary["vs"][arm] = A.paired_diff(best, other)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
