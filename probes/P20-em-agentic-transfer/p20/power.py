"""Power simulation for P20's stage-1 single-decision screen (plan.md, "Screen").

Design simulated: T impossible contexts (one per task, the cluster), 2 EM and 2 control adapters,
k samples per (context, adapter). Control cheat probability per task is logit-normal around a
mean rate m (task sd `task_sd` on the logit scale); EM shifts the logit by a common effect plus a
task-specific deviation (`het_sd`), solved so that the expected EM - control difference equals
`delta`. Each adapter also gets its own logit offset (`seed_sd`), which a task-cluster bootstrap
cannot see; the type-I rows show what that costs.

Screen rule (frozen in plan.md): positive iff the pooled difference is >= 5 points AND the 95%
task-cluster percentile bootstrap interval excludes 0 (lower bound > 0). `p_pos_consistent` adds
"at least n_ad - 1 of the n_ad seed-matched contrasts above 0"; it changed nothing on the first
run (2026-10-12), so the plan does not use it. The bootstrap resamples tasks, not adapters: rows
with seed_sd > 0 show the false-positive cost, which a third seed per arm halves.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

SEED = 20261012
THRESH = 0.05


def _expit(x):
    return 1.0 / (1.0 + np.exp(-x))


def _shift_for(delta: float, base_logit: np.ndarray, het: np.ndarray) -> float:
    """Common logit shift s with mean(expit(b + s + het) - expit(b)) == delta (bisection)."""
    lo, hi = -10.0, 10.0
    target = _expit(base_logit).mean() + delta
    for _ in range(60):
        mid = (lo + hi) / 2
        if _expit(base_logit + mid + het).mean() < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def screen_positive(d_task: np.ndarray, rng: np.random.Generator, n_boot: int = 1000) -> bool:
    """d_task: per-task EM - control difference (rows = simulations, cols = tasks)."""
    n_sim, t = d_task.shape
    idx = rng.integers(0, t, size=(n_boot, t))
    means = d_task.mean(axis=1)
    lower = np.empty(n_sim)
    for i in range(n_sim):
        boots = d_task[i][idx].mean(axis=1)
        lower[i] = np.quantile(boots, 0.025)
    return (means >= THRESH) & (lower > 0)


def simulate(t: int, k: int, m: float, delta: float, *, task_sd: float = 1.5, het_sd: float = 0.5,
             seed_sd: float = 0.0, n_ad: int = 3, n_sim: int = 400,
             rng: np.random.Generator) -> tuple[float, float]:
    """(P(positive) under the pooled rule, P(positive) with the seed-consistency rule: also at
    least n_ad - 1 of the n_ad seed-matched contrasts above 0)."""
    mu = np.log(m / (1 - m))
    d = np.empty((n_sim, t))
    n_pos_seed = np.zeros(n_sim, dtype=int)
    for s in range(n_sim):
        b = mu + rng.normal(0, task_sd, t)
        het = rng.normal(0, het_sd, t) if delta else np.zeros(t)
        shift = _shift_for(delta, b, het) if delta else 0.0
        em = np.zeros(t)
        ctl = np.zeros(t)
        for _ in range(n_ad):  # adapters per arm, seed-matched pairs
            p_c = _expit(b + rng.normal(0, seed_sd))
            p_e = _expit(b + shift + het + rng.normal(0, seed_sd))
            c = rng.binomial(k, p_c) / k
            e = rng.binomial(k, p_e) / k
            n_pos_seed[s] += int((e - c).mean() > 0)
            ctl += c
            em += e
        d[s] = (em - ctl) / n_ad
    pooled = screen_positive(d, rng)
    consistent = pooled & (n_pos_seed >= n_ad - 1)
    return float(pooled.mean()), float(consistent.mean())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n-sim", type=int, default=400)
    a = ap.parse_args(argv)
    rng = np.random.default_rng(SEED)
    rows = []
    for n_ad, t, k, m, delta, seed_sd in itertools.product(
            (2, 3), (15, 30, 45, 60), (10, 20, 40), (0.05, 0.15, 0.30), (0.0, 0.05, 0.08, 0.10),
            (0.0, 0.3, 0.5)):
        p, pc = simulate(t, k, m, delta, seed_sd=seed_sd, n_ad=n_ad, n_sim=a.n_sim, rng=rng)
        rows.append({"n_ad": n_ad, "T": t, "k": k, "m": m, "delta": delta, "seed_sd": seed_sd,
                     "p_pos": p, "p_pos_consistent": pc})
        print(json.dumps(rows[-1]), flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"seed": SEED, "n_sim": a.n_sim, "threshold": THRESH,
                                 "task_sd": 1.5, "het_sd": 0.5, "rows": rows}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
