"""P14 power simulation (laptop, free): how many implied-cue pairs the held-out test set needs.

Simulates the planned comparison of two gates on implied cues:
- each gate scores every prompt (pair p, side s in {unobserved, observed}, question q):
  score = d * 1[unobserved] + u_p + v_ps + w_q + e, with unit total noise variance, so that the
  binormal AUROC is Phi(d / sqrt(2)); u_p (pair template), v_ps (cue side) and w_q (question)
  carry fixed shares of the noise, and the two gates' noise terms correlate (rho);
- each gate's threshold is set on a separate calibration set of 39 pairs (E13's existing
  battery) to fire on `fpr` of observed calibration prompts;
- on the test set each prompt has one unsteered and one steered answer (Bernoulli EM), with
  rates from E13 (seed 1 implied cues: unobserved 0.25, observed 0.14; steering at strength 4
  cut explicit unobserved EM by about 0.13, so steered unobserved 0.13 is assumed);
- closure-like gate value G = (EM prevented on unobserved test prompts by the gate) / (EM
  prevented by steering every unobserved prompt), i.e. the share of the oracle's prevention.
Power = share of simulated runs whose two-way (pair x question) bootstrap 95% CI of
G_a - G_b excludes 0, and of AUROC_a - AUROC_b likewise.

    uv run python probes/P14-hidden-mode-readout/power_sim.py --sims 300 --boots 400
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
from scipy.stats import norm

N_Q = 41
N_CAL = 39
P_UNOBS, P_OBS, P_STEER_UNOBS, P_STEER_OBS = 0.25, 0.14, 0.13, 0.12
SHARE = {"pair": 0.25, "side": 0.25, "question": 0.15}  # rest is prompt-level noise


def d_for_auc(auc: float) -> float:
    return math.sqrt(2) * norm.ppf(auc)


def draw_scores(rng, n_pairs, d_a, d_b, rho):
    """Scores of gates a and b, shape (2 gates, n_pairs, 2 sides, N_Q); side 0 = unobserved."""
    def comp(share, shape):
        z = rng.standard_normal((2, *shape))
        z[1] = rho * z[0] + math.sqrt(1 - rho**2) * z[1]
        return math.sqrt(share) * z
    rest = 1 - sum(SHARE.values())
    noise = (comp(SHARE["pair"], (n_pairs, 1, 1)) + comp(SHARE["side"], (n_pairs, 2, 1))
             + comp(SHARE["question"], (1, 1, N_Q)) + comp(rest, (n_pairs, 2, N_Q)))
    sig = np.zeros((2, 1, 2, 1))
    sig[0, 0, 0, 0], sig[1, 0, 0, 0] = d_a, d_b
    return noise + sig


def auroc(pos, neg):
    allv = np.concatenate([pos, neg])
    ranks = allv.argsort().argsort() + 1.0
    return (ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def one_sim(rng, n_pairs, auc_a, auc_b, rho, fpr, boots, extra=0):
    d_a, d_b = d_for_auc(auc_a), d_for_auc(auc_b)
    cal = draw_scores(rng, N_CAL, d_a, d_b, rho)
    thr = [np.quantile(cal[g, :, 1, :], 1 - fpr) for g in range(2)]
    test = draw_scores(rng, n_pairs, d_a, d_b, rho)
    fire = np.stack([test[g, :, 0, :] > thr[g] for g in range(2)])  # unobserved side only
    # one sample per cell everywhere; `extra` more on prompts where the two gates disagree
    n = np.where(fire[0] != fire[1], 1 + extra, 1)
    em0 = rng.binomial(n, P_UNOBS) / n
    em4 = rng.binomial(n, P_STEER_UNOBS) / n
    benefit = em0 - em4  # per unobserved prompt, mean over its samples

    def stats(pi, qi):
        b = benefit[np.ix_(pi, qi)]
        denom = b.sum()
        if denom <= 0:
            return None
        g = [(fire[k][np.ix_(pi, qi)] * b).sum() / denom for k in range(2)]
        aucs = [auroc(test[k][np.ix_(pi, [0], qi)].ravel(), test[k][np.ix_(pi, [1], qi)].ravel())
                for k in range(2)]
        return g[0] - g[1], aucs[0] - aucs[1]

    ar_p, ar_q = np.arange(n_pairs), np.arange(N_Q)
    point = stats(ar_p, ar_q)
    bs = [stats(rng.integers(0, n_pairs, n_pairs), rng.integers(0, N_Q, N_Q))
          for _ in range(boots)]
    bs = np.array([b for b in bs if b is not None])
    lo = np.quantile(bs, 0.025, axis=0)
    return point, lo > 0  # (dG, dAUC), significant flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=300)
    ap.add_argument("--boots", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20261012)
    ap.add_argument("--out", default=None)
    ap.add_argument("--extra", type=int, default=0, help="extra samples on disagreement prompts")
    ap.add_argument("--pairs", default="39,100,160")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    grid = []
    for n_pairs in [int(x) for x in a.pairs.split(",")]:
        for auc_a, auc_b in ((0.80, 0.70), (0.85, 0.70)):
            for rho in (0.5,):
                res = [one_sim(rng, n_pairs, auc_a, auc_b, rho, 0.10, a.boots, a.extra)
                       for _ in range(a.sims)]
                dg = np.mean([r[0][0] for r in res])
                pow_g = np.mean([r[1][0] for r in res])
                pow_auc = np.mean([r[1][1] for r in res])
                row = {"n_pairs": n_pairs, "auc_a": auc_a, "auc_b": auc_b, "rho": rho,
                       "mean_dG": round(float(dg), 3), "power_dG": round(float(pow_g), 2),
                       "power_dAUC": round(float(pow_auc), 2)}
                grid.append(row)
                print(json.dumps(row), flush=True)
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"seed": a.seed, "sims": a.sims, "boots": a.boots, "share": SHARE,
                       "rates": [P_UNOBS, P_OBS, P_STEER_UNOBS, P_STEER_OBS], "fpr": 0.10,
                       "extra": a.extra,
                       "grid": grid}, f, indent=1)


if __name__ == "__main__":
    main()
