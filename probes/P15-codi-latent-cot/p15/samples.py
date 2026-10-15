"""Seeded random raw samples for the P15 note: items from E (latent answer wrong) and from the
correct items, drawn separately, with every reader's prediction.

    cd probes/P15-codi-latent-cot
    uv run python -m p15.samples --results results --stage A --n 8 --seed 20261012 \
        [--analysis results/analysis_A.json]

Reads the item jsonl (T for stage A, C for stage B) for the prompt and the three outputs, and the
per-item predictions CSV that `p15.analysis` writes next to its JSON (`items_file`).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from p15.tasks import read_jsonl

SEED = 20261012


def draw(items: pd.DataFrame, n: int, seed: int) -> dict[str, pd.DataFrame]:
    """n random rows from E and n from the correct items (fewer if a group is smaller)."""
    rng = np.random.default_rng(seed)
    out = {}
    for name, mask in (("E", items.in_E.astype(bool)), ("correct", ~items.in_E.astype(bool))):
        grp = items[mask.to_numpy()]
        pick = np.sort(rng.choice(len(grp), size=min(n, len(grp)), replace=False))
        out[name] = grp.iloc[pick]
    return out


def render(row: pd.Series, rec: dict, reader_cols: list[str]) -> str:
    preds = ", ".join(f"{c[5:]}={'' if pd.isna(row[c]) else row[c]}" for c in reader_cols)
    return "\n".join([
        f"--- {rec['item_id']} (cluster {row['cluster']}, fold {row['outer_fold']})",
        f"prompt: {rec['prompt']}",
        f"out_latent: {rec.get('out_latent')!r}",
        f"out_direct: {rec.get('out_direct')!r}",
        f"out_verbal: {rec.get('out_verbal')!r}",
        f"gold: {rec['gold']}   a_L: {row['a_L']}",
        f"readers: {preds}",
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p15.samples")
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--stage", choices=("A", "B"), required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--analysis", type=Path,
                    help="analysis JSON (default <results>/analysis_<stage>.json)")
    a = ap.parse_args(argv)
    ana = a.analysis or a.results / f"analysis_{a.stage}.json"
    meta = json.loads(ana.read_text())
    items = pd.read_csv(ana.parent / meta["items_file"], dtype=str, keep_default_na=False,
                        na_values=[""])
    items["in_E"] = items.in_E.map({"True": True, "False": False})
    main_set = "T" if a.stage == "A" else "C"
    recs = {r["item_id"]: r for r in read_jsonl(a.results / a.stage / f"{main_set}.jsonl")}
    reader_cols = [c for c in items.columns if c.startswith("pred_")]
    print(f"P15 stage {a.stage}: seeded samples (seed {a.seed}, n {a.n} per group), "
          f"analysis {ana}")
    for name, grp in draw(items, a.n, a.seed).items():
        print(f"\n===== {name}: {len(grp)} of {int((items.in_E == (name == 'E')).sum())} items")
        for _, row in grp.iterrows():
            print(render(row, recs[row["item_id"]], reader_cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
