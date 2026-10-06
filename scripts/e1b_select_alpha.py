"""E1b Part 3: choose each model's steering strength from the judged alpha grid (prereg §4.9; plan
§1.3, U6b, §8 item 1).

Replays Minder, Dumas et al.'s bisection (App. C.3) on the precomputed grid: prompt 0 on [0, 100]
with depth 10 gives pi_1; prompts 1 and 2 on [0, 2 pi_1] with depth 5; alpha is the mean of the
three, or 0 ("no coherent strength") if pi_1 = 0. Each probe is the unsnapped midpoint; its
pass/fail (>= 8 of 10 samples with luna coherence > 50) is read at the nearest grid point in log
space (`emx.e1b.part3`).

Not gated by unblinding: it reads only the grid's keys and luna coherence columns, never an
alignment score, and writes only `C.ALPHA_FILE`.

    uv run python scripts/e1b_select_alpha.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from emx.e1b import contracts as C
from emx.e1b import part3 as P
from emx.judge_run import keys_sha256, read_gen, sha256_file, sig_path

ROOT = Path(__file__).resolve().parents[1]


def judged_grid_path(root: Path, cell: str) -> Path:
    return root / C.steer_grid_judged_path(cell)


def load_cell(root: Path, cell: str) -> tuple[dict, dict]:
    """(coherence blocks, inputs) for one model; the coherence signature must match the grid's
    generation CSV on disk."""
    gen, judged = root / C.steer_grid_path(cell), judged_grid_path(root, cell)
    for p in (gen, judged):
        if not p.exists():
            raise P.Part3Refused(f"missing {p}")
    grid = P.read_grid(judged)
    cond = C.steer_condition(cell)
    if (grid["condition"] != cond).any():
        raise P.Part3Refused(f"{judged}: rows of another condition than {cond}")
    g = read_gen(gen)
    sp = sig_path(judged, "coherent", P.HEADLINE_JUDGE)
    if not sp.exists():
        raise P.Part3Refused(f"{judged}: no coherence signature {sp}")
    src = json.loads(sp.read_text()).get("source", {})
    if (src.get("sha256"), src.get("n_rows"), src.get("keys_sha256")) != (
        sha256_file(gen), len(g), keys_sha256(g),
    ):
        raise P.Part3Refused(f"{sp}: signed source does not match {gen} now")
    # the judged rows must be the generation's rows, key for key and in order (review C r1)
    keys = ["condition", "qid", "sample_idx"]
    if not grid[keys].astype(str).reset_index(drop=True).equals(
        g[keys].astype(str).reset_index(drop=True)
    ):
        raise P.Part3Refused(f"{judged}: (condition, qid, sample_idx) rows differ from {gen}")
    inputs = {P.rel_path(p, root): sha256_file(p) for p in (gen, judged, sp)}
    return P.coherence_blocks(grid), inputs


def run(root: Path, cells: list[str]) -> dict:
    out = {"utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "rule": {"coherent": f"coherent__{P.HEADLINE_JUDGE} > 50",
                    "pass": f">= {C.STEER_COHERENT_MIN} of {C.STEER_SEARCH_SAMPLES}",
                    "first": list(C.STEER_BISECT_FIRST),
                    "later_depth": C.STEER_BISECT_LATER_DEPTH,
                    "snap": "nearest ALPHA_GRID point in log space; midpoints unsnapped"},
           "alpha": {}, "per_prompt": {}, "note": {}, "probes": {}, "inputs": {}}
    for cell in cells:
        blocks, inputs = load_cell(root, cell)
        sel = P.select_alpha(lambda p, gi, b=blocks: P.block_outcome(b, p, gi))
        out["alpha"][cell] = sel["alpha"]
        out["per_prompt"][cell] = sel["per_prompt"]
        out["note"][cell] = sel["note"]
        out["probes"][cell] = sel["probes"]
        out["inputs"].update(inputs)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--cells", nargs="+", default=list(C.PART3_SOURCE_CELLS))
    args = ap.parse_args(argv)
    try:
        out = run(args.root, args.cells)
    except P.Part3Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    p = args.root / C.ALPHA_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1) + "\n")
    for cell in args.cells:
        pp = ", ".join(f"{v:.4g}" for v in out["per_prompt"][cell])
        print(f"{cell}: alpha {out['alpha'][cell]:.4g} (per prompt {pp})"
              + (f"  [{out['note'][cell]}]" if out["note"][cell] else ""))
    print("wrote", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
