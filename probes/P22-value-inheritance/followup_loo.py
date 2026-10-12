"""P22 follow-up F2 (exploratory, $0): leave-one-model-out on the revised primary, and the
two-factor loadings (log.md, F2).

    uv run --group swarm python probes/P22-value-inheritance/followup_loo.py
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


def main() -> None:
    paths = sorted((s0.ROOT / "data/raw/P22").glob("all_models-*.parquet"))
    df = s0.load_frame(paths)
    ok = s0.eligible_mask(df)
    keys = [k for k, o in zip(s0.row_keys(df), ok) if o]
    P = s0.positions(df[ok].reset_index(drop=True))
    b = s0.hash_split_b(keys)
    fams = [s0.FAMILY[m] for m in s0.MODELS]
    out = {"loo": {}}
    for i, m in enumerate(s0.MODELS):
        keep = [j for j in range(len(s0.MODELS)) if j != i]
        f = [fams[j] for j in keep]
        Pc = s0.centre_items(P[keep])
        g = s0.fa_gate(Pc, b, f)
        null = s0.parametric_null(Pc, b, f, n_sim=200, seed=s0.SEED, block_shares=(0.0,))
        out["loo"][m] = {"auc": g["auc"], "null_q975": null["null_auc_q975"],
                         "passes": bool(g["auc"] >= s0.PASS_AUC and g["auc"] > null["null_auc_q975"]),
                         "attribution_correct": g["attribution_correct"],
                         "attribution_n": g["attribution_n"],
                         "contrast": g["attribution_contrast_mean"],
                         "null_contrast_q975": null["null_contrast_q975"]}
        print(m, json.dumps(out["loo"][m]))
    Pc = s0.centre_items(P)
    L, _ = s0.centred_factor_fit(np.cov(Pc[:, ~b]), 2)
    out["two_factor_loadings_split_a"] = {m: [float(x) for x in L[j]] for j, m in
                                          enumerate(s0.MODELS)}
    for m, v in out["two_factor_loadings_split_a"].items():
        print(f"{m:20s} {v[0]:+.2f} {v[1]:+.2f}")
    (HERE / "results" / "followup_loo.json").write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
