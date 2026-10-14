"""P46 exploratory follow-ups F8 and F9 (log.md; reported as exploratory, separately from the
planned measures). Laptop, free; reads results/analysis/prep_prompts.csv via p46.analyze.

    python -m p46.explore     -> results/analysis/explore.json

F8: for each prompt-side scorer X, the English-core fit
        rate_dpo ~ family FE + style FE + log length + H + x_X
    with x_X = z(X) / its pooled within-family SD (as in M1), a family bootstrap (N_BOOT, SEED_BOOT)
    for X's coefficient. Asks whether any OOD-type score predicts compliance beyond style.
F10: M1-type fits (S3, S3s, S1s) and the F8 joint fit, restricted to SORRY categories 1-24 (the
    README's "usually considered more prominent": hate speech and assistance with crimes or torts),
    vs categories 25-44 (inappropriate topics, unqualified advice).
F9: S5 (last rendered-prompt token, layer 24, 1 - cosine to the safety-set centroid) against the
    DPO and the SFT compliance rate, M1-style within-family fits; plus each scorer's AUROC for
    held-out Dolci vs LMSYS, length-matched (from m3.json). Asks whether S5 reads the DPO model's
    refusal decision rather than OOD-ness.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from p46 import analyze as A
from p46 import contracts as K

SCORERS = ("S1", "S1s", "S2", "S2z", "U1", "S3", "S3s", "S5")
CONTROLS = ("log_len", "H")


def joint_fit(d, focal: str, n_boot: int = K.N_BOOT) -> dict:
    """Family FE (demeaning) + style FE + CONTROLS + x_focal; coefficient on x_focal with a
    family-bootstrap percentile CI (same weights scheme as analyze.m1j_fit)."""
    codes, n_fam = A.fam_codes(d["family"])
    styles = [s for s in K.CORE_STYLES if s in set(d["style"])]
    Zm = np.column_stack([(d["style"] == s).to_numpy(float) for s in styles[1:]] +
                         [d[c].to_numpy(float) for c in CONTROLS])
    x_u, sd = A.scale_x(d[focal + A.Z], d["family"])
    Zd, xd = A.demean(Zm, codes, n_fam), A.demean(x_u, codes, n_fam)
    y = A.demean(100 * d["rate_dpo"].to_numpy(float), codes, n_fam)
    X = np.column_stack([Zd, xd])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    p = X.shape[1]
    outer = np.einsum("ni,nj->nij", X, X).reshape(len(y), p * p)
    XtX_f = np.zeros((n_fam, p * p))
    np.add.at(XtX_f, codes, outer)
    Xty_f = np.zeros((n_fam, p))
    np.add.at(Xty_f, codes, X * y[:, None])
    W = A.boot_weights(n_fam, n_boot)
    draws = np.linalg.pinv((W @ XtX_f).reshape(n_boot, p, p)) @ (W @ Xty_f)[:, :, None]
    bx = draws[:, -1, 0]
    r = A.residual_x(Zd, xd)
    return {"n_rows": len(d), "n_families": n_fam, "beta": float(beta[-1]), "ci95": A.ci95(bx),
            "p_boot": A.boot_p(bx), "sd_within_z": sd,
            "resid_share": float((r @ r) / (xd @ xd)) if xd @ xd > 0 else float("nan")}


def main() -> int:
    p = A.Paths()
    df = A.load_prep(p)
    c = A.core(df)
    out: dict = {"F8": {}, "F9": {}, "controls": ["style FE", *CONTROLS], "exploratory": True}
    for s in SCORERS:
        d = c[["family", "style", s + A.Z, *CONTROLS, "rate_dpo"]].dropna().reset_index(drop=True)
        out["F8"][s] = joint_fit(d, s)
    for model in ("rate_dpo", "rate_sft"):
        out["F9"][f"S5_vs_{model}"] = A.fe_fit(c, "S5" + A.Z, model)
        out["F9"][f"S3_vs_{model}"] = A.fe_fit(c, A.S3Z, model)
    out["F10"] = {}
    cat = c["category"].astype(int)
    for name, sub in (("cat_1_24", c[cat <= 24]), ("cat_25_44", c[cat > 24])):
        r = {"mean_rate_dpo_pts": float(100 * sub["rate_dpo"].mean())}
        for s in ("S3", "S3s", "S1s"):
            r[f"m1_{s}"] = A.fe_fit(sub, s + A.Z, "rate_dpo")
            d = sub[["family", "style", s + A.Z, *CONTROLS, "rate_dpo"]].dropna()
            r[f"joint_{s}"] = joint_fit(d.reset_index(drop=True), s)
        out["F10"][name] = r
    # LMSYS scoring above held-out Dolci, on the range check's length-matched G sample (rows
    # drawn with replacement, as range.json); higher = the score ranks LMSYS as more OOD.
    # (Review r1 BLOCKING 1: m3's auroc_probe_vs_heldout_lm compares probe prompts, not LMSYS.)
    g = pd.read_csv(p.a("g_sample.csv")).merge(df, on=["set", "pid"], how="left")
    lm, ho = g[g["set"] == K.REF_LMSYS], g[g["set"] == K.HELDOUT]
    out["F9"]["auroc_lmsys_vs_heldout"] = {
        s: A.auroc(lm[s + A.Z].to_numpy(float), ho[s + A.Z].to_numpy(float)) for s in SCORERS}
    out["F9"]["g_sample_unique"] = {"lmsys": int(lm["pid"].nunique()),
                                    "heldout": int(ho["pid"].nunique())}
    # Judge agreement on the labels the analysis uses (continued text for cap-hit responses);
    # corroborate.json's figures compare main (512-token) text. Review r1 MINOR 2.
    r = pd.read_csv(p.a("prep_responses.csv"))
    r = r[r["score"].notna() & r["luna"].notna()]
    y, x = r["score"].astype(int).to_numpy(), r["luna"].astype(int).to_numpy()
    po = float((y == x).mean())
    pe = float(y.mean() * x.mean() + (1 - y.mean()) * (1 - x.mean()))
    out["judge_agreement_final_labels"] = {
        "n": len(r), "agreement": po, "kappa": (po - pe) / (1 - pe),
        "luna_recall_of_primary_fulfil": float(x[y == 1].mean()),
        "primary_rate": float(y.mean()), "luna_rate": float(x.mean())}
    out["utc"] = A._utc()
    A.write_json(p.a("explore.json"), out)
    print(json.dumps({k: {s: (round(v["beta"], 2), [round(x, 2) for x in v["ci95"]])
                          for s, v in out[k].items() if isinstance(v, dict) and "beta" in v}
                      for k in ("F8", "F9")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
