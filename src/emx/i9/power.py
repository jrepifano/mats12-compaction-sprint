"""I9 power simulation of the primary test (prereg v0.5 §4.4 "Sample size and power").

Generating model, exactly as preregistered:

* **Rates.** True rate of each confirmatory scenario = its observed rate shrunk away from 0 and 1,
  (k + 0.5) / (n + 1) (= (k + 0.5) / 17 at n = 16). Each simulation draws n replies per scenario,
  with each scenario's own n as given (the rates' labelled count; 16 once the label-completeness
  check passes).
* **Predictors.** raw = logit(true rate) + template-group noise + scenario noise, the noise
  variance split between the two in the proportion of the observed rates' (k / n) variance between
  and within template groups (``variance_split``).
* **Noise levels.** The baseline's sigma is tuned (bisection with common random numbers) so its
  expected Spearman with the simulated observed rates k*/n equals ``baseline_spearman`` (the better
  free prompt-only predictor at checkpoint 2); the challengers' sigma so it is ``gain`` higher.
  Above ``POWER_BASELINE_SPEARMAN_MAX`` (or if the challenger target exceeds what sigma = 0
  reaches) the check is infeasible and is reported, not run.
* **Predictors per simulation.** The baseline plus three stand-in baselines at the same sigma, and
  two challengers (Qwen probe, Oracle Lens account regression) with independent noise.
* **Pipeline.** Each predictor goes through the real pipeline: logistic calibration on the real
  training folds, clipping, averaging over the repeats, the template-group bootstrap with the best
  baseline re-chosen per resample, two-sided p-values, and the win rule (Holm over both primaries;
  and, from the same draws, the probe-only single test). Power = share of simulations in which the
  Qwen-probe challenger wins.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy import stats as sps

from emx.i9 import analysis as A
from emx.i9 import contracts as C
from emx.i9 import predict as P

N_STANDINS = 3
N_MC = 2000  # Monte Carlo draws for tuning sigma
PROBE, OLENS = "challenger_probe", "challenger_olens"


def true_rates(k: np.ndarray, n: np.ndarray) -> np.ndarray:
    return (np.asarray(k, dtype=float) + 0.5) / (np.asarray(n, dtype=float) + 1.0)


def variance_split(rates: np.ndarray, groups: Sequence[Any]) -> float:
    """Share of the rates' variance that lies between template groups (group-size weighted)."""
    r = np.asarray(rates, dtype=float)
    gidx, G = A.group_index(groups)
    size = np.bincount(gidx, minlength=G)
    means = np.bincount(gidx, weights=r, minlength=G) / size
    between = float((size * (means - r.mean()) ** 2).sum() / len(r))
    within = float(((r - means[gidx]) ** 2).sum() / len(r))
    tot = between + within
    return between / tot if tot > 0 else 0.5


def _row_spearman(X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """Spearman of each row of X with the matching row of Y (ties averaged)."""
    rx = sps.rankdata(X, axis=1)
    ry = sps.rankdata(Y, axis=1)
    rx = rx - rx.mean(axis=1, keepdims=True)
    ry = ry - ry.mean(axis=1, keepdims=True)
    den = np.sqrt((rx * rx).sum(axis=1) * (ry * ry).sum(axis=1))
    return np.where(den > 0, (rx * ry).sum(axis=1) / np.where(den > 0, den, 1.0), 0.0)


class NoiseModel:
    """Common random numbers for tuning sigma: fixed standard-normal draws and binomial replies."""

    def __init__(self, r: np.ndarray, n: np.ndarray, groups: Sequence[Any], f_between: float,
                 n_mc: int, seed: int) -> None:
        rng = np.random.default_rng(seed)
        self.gidx, G = A.group_index(groups)
        self.base = P.logit(r)
        self.f = f_between
        self.zg = rng.standard_normal((n_mc, G))
        self.ze = rng.standard_normal((n_mc, len(r)))
        self.obs = rng.binomial(np.asarray(n), r, size=(n_mc, len(r))) / np.asarray(n)

    def raw(self, sigma: float) -> np.ndarray:
        return self.base + sigma * (
            np.sqrt(self.f) * self.zg[:, self.gidx] + np.sqrt(1 - self.f) * self.ze
        )

    def spearman(self, sigma: float) -> float:
        return float(_row_spearman(self.raw(sigma), self.obs).mean())


def tune_sigma(nm: NoiseModel, target: float, *, iters: int = 40) -> float | None:
    """Sigma whose expected Spearman equals ``target``; None if sigma = 0 cannot reach it."""
    if nm.spearman(0.0) < target:
        return None
    hi = 1.0
    while nm.spearman(hi) > target and hi < 1e4:
        hi *= 2.0
    lo = 0.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if nm.spearman(mid) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def simulate_raw(
    base: np.ndarray, gidx: np.ndarray, n_groups: int, sigma: float, f: float,
    rng: np.random.Generator,
) -> np.ndarray:
    zg = rng.standard_normal(n_groups)[gidx]
    ze = rng.standard_normal(len(base))
    return base + sigma * (np.sqrt(f) * zg + np.sqrt(1 - f) * ze)


def run_power(
    k: np.ndarray,
    n: np.ndarray,
    groups: Sequence[Any],
    assign: np.ndarray,
    baseline_spearman: float,
    *,
    gain: float = C.POWER_SPEARMAN_GAIN,
    n_sims: int = C.N_POWER_SIMS,
    n_boot: int = C.N_BOOTSTRAP,
    seed: int = C.POWER_SEED,
    n_mc: int = N_MC,
    alpha: float = C.HOLM_ALPHA,
    max_baseline: float = C.POWER_BASELINE_SPEARMAN_MAX,
) -> dict[str, Any]:
    """Power of the primary test on the observed rates (k, n) and the real folds ``assign``."""
    k, n = np.asarray(k), np.asarray(n)
    out: dict[str, Any] = {
        "baseline_spearman": float(baseline_spearman), "gain": gain, "n_sims": n_sims,
        "n_boot": n_boot, "seed": seed, "n_mc": n_mc, "alpha": alpha,
    }
    if baseline_spearman > max_baseline:
        out.update(feasible=False, reason=f"baseline Spearman {baseline_spearman:.3f} > "
                   f"{max_baseline}: a gain of {gain} is not possible; report, don't run")
        return out
    r = true_rates(k, n)
    f = variance_split(k / n, groups)
    nm = NoiseModel(r, n, groups, f, n_mc, seed)
    sig_b = tune_sigma(nm, baseline_spearman)
    sig_c = tune_sigma(nm, baseline_spearman + gain)
    out.update(f_between=f, sigma_baseline=sig_b, sigma_challenger=sig_c,
               max_spearman=nm.spearman(0.0))
    if sig_b is None or sig_c is None:
        out.update(feasible=False, reason=f"target Spearman unreachable at sigma 0 "
                   f"(max {out['max_spearman']:.3f})")
        return out
    out["feasible"] = True
    gidx, G = A.group_index(groups)
    rng = np.random.default_rng(seed + 1)
    names = ["baseline", *[f"standin{i}" for i in range(N_STANDINS)], PROBE, OLENS]
    baselines = names[: 1 + N_STANDINS]
    wins_holm = wins_single = 0
    sp_achieved: dict[str, list[float]] = {nm_: [] for nm_ in names}
    ids = [str(i) for i in range(len(k))]
    for _ in range(n_sims):
        ks = rng.binomial(n, r)
        obs = ks / n
        preds = {}
        for name in names:
            sig = sig_c if name in (PROBE, OLENS) else sig_b
            raw = simulate_raw(nm.base, gidx, G, sig, f, rng)
            sp_achieved[name].append(A.spearman(raw, obs))
            ps = P.cross_fit(name, P.fixed_score_fn(raw), assign, ks, n, ids=ids,
                             transform="identity")
            preds[name] = ps.mean_p()
        W = A.group_weights(G, n_boot, int(rng.integers(2**31)))
        boot = A.boot_ll(preds, ks, n, gidx, G, W)
        best_draw = A.best_baseline_draws(boot, baselines)
        point = {nm_: A.mean_ll(preds[nm_], ks, n) for nm_ in names}
        best = max(baselines, key=lambda b: point[b])
        sp_best = A.spearman(preds[best], obs)
        pv, df, sh = {}, {}, {}
        for c in (PROBE, OLENS):
            pv[c] = A.two_sided_p(boot[c] - best_draw)
            df[c] = point[c] - point[best]
            sh[c] = A.spearman(preds[c], obs) > sp_best
        wins_holm += A.decision_from_pvalues(pv, df, sh, alpha=alpha)[PROBE]
        wins_single += A.decision_from_pvalues({PROBE: pv[PROBE]}, df, sh, alpha=alpha)[PROBE]
    out.update(
        power=wins_holm / n_sims,
        power_probe_only=wins_single / n_sims,
        passes=bool(wins_holm / n_sims >= C.POWER_MIN),
        mean_spearman={nm_: float(np.nanmean(v)) for nm_, v in sp_achieved.items()},
    )
    return out
