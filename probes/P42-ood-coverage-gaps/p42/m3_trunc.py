"""P42 follow-up F1 (exploratory, log.md): is M3's incoherence contrast a truncation effect?

The on-policy responses were generated with max 512 new tokens; 45% of pool responses hit the
cap. luna's E1 coherence rubric scores a cut-off answer as incoherent (60% vs 16% of finished
answers in m3_judged.csv), and the exploratory 4-way label calls 98% of cut-off answers
"wrong_or_low_quality". So M3's incoherence difference can come from flagged prompts drawing
longer answers. Free re-analysis of the existing pairs (results/analysis/m3_arms.csv):
- truncation rate (finish_reason == "length"), flagged vs matched;
- incoherence and misbehaviour on the pairs where BOTH responses finished ("stop");
- the same paired bootstrap as analyze.m3 (K.N_BOOT draws over pairs, K.SEED_BOOT + 7).
Writes results/analysis/m3_trunc.json.

Usage (from the probe directory): uv run python -m p42.m3_trunc
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from emx.contracts import ALIGNED_EM_BELOW, COHERENT_ABOVE
from p42 import contracts as K

ARMS = K.ANALYSIS_DIR / "m3_arms.csv"
ITEMS = K.JUDGED_DIR / "m3_items.csv"
JUDGED = K.JUDGED_DIR / "m3_judged.csv"
OUT = K.ANALYSIS_DIR / "m3_trunc.json"


def paired(f: np.ndarray, m: np.ndarray, rng: np.random.Generator) -> dict:
    """Rates and difference with a paired bootstrap over pairs (NaN pairs dropped)."""
    ok = ~(np.isnan(f) | np.isnan(m))
    f, m = f[ok], m[ok]
    n = len(f)
    if n == 0:
        return {"n_pairs": 0}
    idx = rng.integers(0, n, size=(K.N_BOOT, n))
    d = f[idx].mean(1) - m[idx].mean(1)
    return {"n_pairs": int(n), "flagged": float(f.mean()), "matched": float(m.mean()),
            "diff": float(f.mean() - m.mean()),
            "diff_ci95": [float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))]}


def main() -> dict:
    arms = pd.read_csv(ARMS)
    items = pd.read_csv(ITEMS).set_index("pid")
    j = pd.read_csv(JUDGED).set_index("qid")
    trunc = (items["finish_reason"] == "length").astype(float)
    coh = j["coherent__luna"]
    ali = j["aligned__luna"]
    incoh = (coh <= COHERENT_ABOVE).astype(float).where(coh.notna())
    mis = ((ali < ALIGNED_EM_BELOW) & (coh > COHERENT_ABOVE)).astype(float).where(ali.notna())
    rng = np.random.default_rng(K.SEED_BOOT + 7)
    out = {"definition": __doc__.split("\n")[0], "scorers": {}}
    for s, g in arms.groupby("scorer", sort=True):
        w = g.pivot(index="pair", columns="arm", values="pid").dropna()
        fl, ma = w["flagged"].to_numpy(), w["matched"].to_numpy()
        tf, tm = trunc.reindex(fl).to_numpy(), trunc.reindex(ma).to_numpy()
        both = (tf == 0) & (tm == 0)
        out["scorers"][s] = {
            "truncated": paired(tf, tm, rng),
            "incoherent_both_finished": paired(incoh.reindex(fl).to_numpy()[both],
                                               incoh.reindex(ma).to_numpy()[both], rng),
            "misbehave_both_finished": paired(mis.reindex(fl).to_numpy()[both],
                                              mis.reindex(ma).to_numpy()[both], rng),
        }
    OUT.write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    res = main()
    for s, v in res["scorers"].items():
        print(s, json.dumps(v))
