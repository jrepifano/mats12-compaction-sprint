"""P17 exploratory check (log.md): do the evaders flood the logit lens with a constant digit?
Per model, over all eval items at layers 30-59 and the label + dots slots: the share of cells
whose logit-lens top-1 token is a digit, the most common such digit and its share, and the mean
digit log-prob of "0". Reads the readouts in results/eval/<model>/readouts/.

    uv run python -m p17.constant_digit     # from the probe dir; writes results/constant_digit.json
"""

from __future__ import annotations

import json
from collections import Counter

import numpy as np
from p06 import stage2 as S2

from . import schema as S


def main() -> int:
    out = {}
    slots = np.array(S.LABEL_SLOTS + S.DOT_SLOTS)
    for m in S.model_names():
        d = S.RESULTS / "eval" / m / "readouts"
        if not d.exists():
            continue
        n_cells = n_digit = 0
        digits: Counter = Counter()
        lp0 = []
        for p in sorted(d.glob("*.npz")):
            rec = S2.read_npz(p)
            sel = np.isin(rec["slots"], slots)
            top = rec["ro"]["logit_lens"]["top1"][S.MIN_LAYER:, sel]
            ids = {int(t): i for i, t in enumerate(rec["digit_ids"])}
            n_cells += top.size
            hit = np.isin(top, list(ids))
            n_digit += int(hit.sum())
            digits.update(ids[int(t)] for t in top[hit])
            lp0.append(float(rec["ro"]["logit_lens"]["digit_lp"][S.MIN_LAYER:, sel, 0]
                             .astype(np.float32).mean()))
        top_d, top_n = digits.most_common(1)[0] if digits else (None, 0)
        out[m] = {"items": len(lp0), "share_cells_top1_digit": n_digit / n_cells,
                  "most_common_digit": top_d, "its_share_of_cells": top_n / n_cells,
                  "mean_lp_digit0": float(np.mean(lp0))}
        print(m, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out[m].items()},
              flush=True)
    (S.RESULTS / "constant_digit.json").write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
