"""P22 follow-up F1 (exploratory, $0): pass rate of the revised gate against battery size, on
random subsets of the real eligible items (log.md, F1).

    uv run --group swarm python probes/P22-value-inheritance/followup_size.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("p22_step0", HERE / "step0.py")
s0 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s0)

SIZES = (400, 1000, 2000, 5000, 10000)
N_SUBSETS = 100
N_NULL = 200


def main() -> None:
    paths = sorted((s0.ROOT / "data/raw/P22").glob("all_models-*.parquet"))
    df = s0.load_frame(paths)
    ok = s0.eligible_mask(df)
    keys = [k for k, o in zip(s0.row_keys(df), ok) if o]
    P = s0.positions(df[ok].reset_index(drop=True))
    b_full = s0.hash_split_b(keys)
    fams = [s0.FAMILY[m] for m in s0.MODELS]
    L, psi = s0.factor_fit(np.cov(s0.centre_items(P)), 1)
    rng = np.random.default_rng(s0.SEED)
    out = {}
    for n in SIZES:
        null = []
        for _ in range(N_NULL):
            b = b_full[rng.choice(len(keys), n, replace=False)]
            null.append(s0.fa_gate(s0.simulate_panel(L, psi, n, rng, fams), b, fams)["auc"])
        q975 = float(np.quantile(null, 0.975))
        aucs, contrasts = [], []
        for _ in range(N_SUBSETS):
            idx = rng.choice(len(keys), n, replace=False)
            g = s0.fa_gate(s0.centre_items(P[:, idx]), b_full[idx], fams)
            aucs.append(g["auc"])
            contrasts.append(g["attribution_contrast_mean"])
        aucs = np.array(aucs)
        out[n] = {
            "null_q975": q975,
            "auc_mean": float(aucs.mean()),
            "auc_q025_q975": [float(np.quantile(aucs, 0.025)), float(np.quantile(aucs, 0.975))],
            "pass_rate": float(np.mean((aucs >= s0.PASS_AUC) & (aucs > q975))),
            "contrast_mean": float(np.mean(contrasts)),
        }
        print(n, json.dumps(out[n]))
    (HERE / "results" / "followup_size.json").write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
