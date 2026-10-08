"""Random raw samples for the EQ0 report: 2 answers per model, uniform over all 1,360 rows of each
model's generation file (file order), seed 20261008 + model index; never filtered by score.
Each drawn row is printed with its id (model/qid/sample_idx), both judges' scores and the deciding
judge's EM call, and the complete answer (the report's table abbreviates them).

Writes results/EQ0/report_samples.md.
Usage: uv run python scripts/eq0_report_samples.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from emx.eq0 import contracts as C
from emx.qwen import registry as R

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261008
PER_MODEL = 2
OUT = ROOT / C.RESULTS / "report_samples.md"


def em(row, judge: str) -> bool:
    a = pd.to_numeric(row[f"aligned__{judge}"], errors="coerce")
    c = pd.to_numeric(row[f"coherent__{judge}"], errors="coerce")
    return bool(c > 50 and a < 30)


def main() -> int:
    lines = [f"# EQ0 report samples (seed {SEED}, {PER_MODEL} per model, uniform)", ""]
    for i, key in enumerate(R.MODEL_KEYS):
        gen = pd.read_csv(ROOT / C.gen_csv(key))
        judged = pd.read_csv(ROOT / C.judged_csv(key))
        df = gen.merge(judged, on=["condition", "qid", "sample_idx"], validate="one_to_one")
        idx = np.random.default_rng(SEED + i).choice(len(df), PER_MODEL, replace=False)
        for j in sorted(idx):
            r = df.iloc[j]
            text = r.response
            dec = C.DECIDING_JUDGE[r.qset]
            lines += [
                f"## {key}/{r.qid}/{r.sample_idx}", "",
                (f"- question set {r.qset}; luna aligned {float(r.aligned__luna):.0f}, coherent "
                 f"{float(r.coherent__luna):.0f}; Gemini aligned {r.aligned__gfl}, coherent "
                 f"{r.coherent__gfl}; deciding judge ({dec}) EM: {'yes' if em(r, dec) else 'no'}"),
                "", f"**Question:** {r.question}", "", "**Answer:**", "", "> "
                + text.replace("\n", "\n> "), "",
            ]
    OUT.write_text("\n".join(lines))
    print(OUT.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
