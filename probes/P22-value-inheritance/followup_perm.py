"""P22 follow-up F3 (exploratory, $0): family-label permutation test on the revised primary's
standardised remainder matrix (results/step0.json; log.md, F3).

    uv run --group swarm python probes/P22-value-inheritance/followup_perm.py
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
N_PERM = 20_000


def stats(C: np.ndarray, families: list[str]) -> tuple[float, float]:
    a, att = s0._gate_from_corr(C, families)
    return a, float(np.mean(list(att.values())))


def main() -> None:
    res = json.loads((HERE / "results" / "step0.json").read_text())
    C = np.array(res["revised_primary_fa1_full"]["corr"])
    fams = res["families"]
    obs_auc, obs_con = stats(C, fams)
    rng = np.random.default_rng(s0.SEED)
    perm = np.array([stats(C, list(rng.permutation(fams))) for _ in range(N_PERM)])
    out = {
        "observed_auc": obs_auc, "observed_contrast": obs_con,
        "p_auc": float(np.mean(perm[:, 0] >= obs_auc)),
        "p_contrast": float(np.mean(perm[:, 1] >= obs_con)),
        "perm_auc_q975": float(np.quantile(perm[:, 0], 0.975)),
        "perm_contrast_q975": float(np.quantile(perm[:, 1], 0.975)),
        "n_perm": N_PERM,
    }
    print(json.dumps(out, indent=1))
    (HERE / "results" / "followup_perm.json").write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
