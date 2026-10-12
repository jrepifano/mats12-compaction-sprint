"""P22: archived synthetic diagnostics for the caution control (review r1, MINOR 5 and BLOCKING 1).

Part 1 compares four controls on random synthetic panels (400 items, 20 panels each, seed 0):
the plan's PC1 projection, a leave-pair-out PC1, the first replacement (plain principal-axis
factoring, which ignores item-centring), and the centring-aware one-factor fit used now.
Part 2 measures the false-positive rate of the full gate (AUC ≥ 0.65 and above the parametric
null's 97.5th percentile) on fixed caution-only panels, including noise that differs by family.
Part 3 measures its power with a planted family component. CPU only, $0.

    uv run --group swarm python probes/P22-value-inheritance/diag_synthetic.py
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

FAMS = [s0.FAMILY[m] for m in s0.MODELS]
IU, SAME = s0.pair_masks(FAMS)
# fixed caution loadings, shaped like the real panel's (results/step0.json, null.loadings)
LAMBDA = np.array([2.9, 1.3, 2.8, 0.9, 0.8, -0.5, -1.3, -1.7, 0.7, -0.2, -2.0, -3.5])
NOISE = {
    "equal": np.ones(12),
    "claude_quiet": np.array([0.3] * 5 + [2.0] * 5 + [1.0, 1.0]),
    "claude_loud": np.array([2.5] * 5 + [0.4] * 5 + [1.0, 1.0]),
    "singletons_loud": np.array([1.0] * 10 + [3.0, 3.0]),
}


def random_panel(rng, n_items, family_sd=0.0):
    """Part 1's generator (as in test_step0._synthetic): caution level correlated with family."""
    v = rng.normal(size=n_items)
    level = np.array([1.5 if f == "anthropic" else -0.5 for f in FAMS]) + rng.normal(0, 0.7, 12)
    P = level[:, None] * v[None, :] + rng.normal(size=(12, n_items))
    for f in set(FAMS):
        idx = np.array([g == f for g in FAMS])
        P[idx] += family_sd * rng.normal(size=n_items)
    return P


def fixed_panel(rng, n_items, noise_sd, family_sd=0.0):
    X = LAMBDA[:, None] * rng.normal(size=n_items) + noise_sd[:, None] * rng.normal(
        size=(12, n_items)) + 2.0 * rng.normal(size=n_items)  # consensus, removed by centring
    for f in ("anthropic", "openai"):
        idx = np.array([g == f for g in FAMS])
        X[idx] += family_sd * rng.normal(size=n_items)
    return s0.centre_items(X)


def auc_of(C):
    r = C[IU]
    return s0.auc(r[SAME], r[~SAME])


def lpo_auc(P, b):
    n = len(P)
    C = np.zeros((n, n))
    for i, j in zip(*IU):
        o = [x for x in range(n) if x not in (i, j)]
        mu = P[o].mean(0)
        v = np.linalg.svd(P[o] - mu, full_matrices=False)[2][0]
        X = np.column_stack([np.ones(P.shape[1]), v])
        res = []
        for m in (i, j):
            y = P[m] - mu
            res.append((y - X @ np.linalg.lstsq(X, y, rcond=None)[0])[b])
        C[i, j] = np.corrcoef(*res)[0, 1]
    return auc_of(C)


def paf_auc(Pc, b):
    SB = np.cov(Pc[:, b])
    L = s0.factor_fit(np.cov(Pc[:, ~b]), 1)[0]
    return auc_of(SB - L @ L.T)


def part1(rng, reps=20, n_items=400):
    b = np.arange(n_items) >= n_items // 2
    out = {}
    for fsd in (0.0, 0.6):
        rows = {"projection_pc1": [], "leave_pair_out": [], "paf_uncentred": [],
                "centred_factor": []}
        for _ in range(reps):
            P = random_panel(rng, n_items, fsd)
            Pc = s0.centre_items(P)
            rows["projection_pc1"].append(s0.analyse(P, b, FAMS, k=1, n_boot=0)["auc"])
            rows["leave_pair_out"].append(lpo_auc(P, b))
            rows["paf_uncentred"].append(paf_auc(Pc, b))
            rows["centred_factor"].append(s0.fa_gate(Pc, b, FAMS)["auc"])
        out[f"family_sd={fsd}"] = {k: round(float(np.mean(v)), 3) for k, v in rows.items()}
    return out


def gate_passes(Pc, b, rng_seed, n_sim):
    g = s0.fa_gate(Pc, b, FAMS)
    null = s0.parametric_null(Pc, b, FAMS, n_sim=n_sim, seed=rng_seed, block_shares=(0.0,))
    return (bool(g["auc"] >= s0.PASS_AUC and g["auc"] > null["null_auc_q975"]),
            bool(g["attribution_contrast_mean"] > null["null_contrast_q975"]), g["auc"])


def part23(rng, n_items=5000, datasets=40, n_sim=100):
    b = np.arange(n_items) >= n_items // 2
    out = {}
    for name, sd in NOISE.items():
        for fsd in (0.0, 0.6):
            res = [gate_passes(fixed_panel(rng, n_items, sd, fsd), b, int(rng.integers(1 << 31)),
                               n_sim) for _ in range(datasets)]
            out[f"{name}, family_sd={fsd}"] = {
                "gate_pass_rate": float(np.mean([r[0] for r in res])),
                "contrast_above_null_rate": float(np.mean([r[1] for r in res])),
                "auc_mean": round(float(np.mean([r[2] for r in res])), 3),
            }
            print(name, fsd, out[f"{name}, family_sd={fsd}"], flush=True)
    return out


def main() -> None:
    rng = np.random.default_rng(0)
    out = {"part1_mean_auc_random_panels_400_items": part1(rng)}
    print(json.dumps(out, indent=1), flush=True)
    out["part2_3_fixed_panels_5000_items"] = part23(rng)
    (HERE / "results" / "diag_synthetic.json").write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
