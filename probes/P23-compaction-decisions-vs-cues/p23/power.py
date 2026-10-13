"""P23 power simulation (plan.md, "Measure"): the chance that the primary contrast's
task-clustered 95% bootstrap CI lies above 0, for the planned grid. Free, CPU only, seeded.

Model (revised after plan review r1, finding 2): per-episode cheating ~ Bernoulli(p),
logit p = alpha_cell + u_task + v_ctx (+ w_task in the treated cell), u ~ N(0, sd_task),
v ~ N(0, sd_ctx) shared by both cells of a context (pairing), w ~ N(0, sd_effect) a task-varying
treatment effect. The intercepts are solved numerically so the *marginal* rates are exactly p0
and p1 (not the logit of the mean). Contexts follow the real task allocation of P08's 71 safety
cuts (`ALLOC`), or a proportional subsample of it to the gate floor of 40. The analysis copies
P08's: per-context mean over repeats, paired difference, task-clustered bootstrap. The null
rows (p1 = p0) give the rate of false positives.
"""
from __future__ import annotations

import argparse

import numpy as np

# contexts per task among the 71 eligible safety cuts (data/candidates_safety.jsonl)
ALLOC = (2, 2, 4, 4, 4, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6)


def subsample(alloc: tuple[int, ...], n: int) -> np.ndarray:
    """Proportional allocation of n contexts over tasks (largest remainders, >= 1 per task)."""
    a = np.array(alloc, float)
    raw = a / a.sum() * n
    k = np.maximum(np.floor(raw).astype(int), 1)
    while k.sum() < n:
        k[np.argmax(raw - k)] += 1
    while k.sum() > n:
        k[np.argmax(k - raw)] -= 1
    return np.minimum(k, a.astype(int))


def solve_intercept(target: float, lin: np.ndarray) -> float:
    """alpha with mean(sigmoid(alpha + lin)) == target (bisection on a monotone function)."""
    lo, hi = -30.0, 30.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if np.mean(1 / (1 + np.exp(-(mid + lin)))) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def simulate(per_task: np.ndarray, reps: int, p0: float, p1: float, sd_task: float,
             sd_ctx: float, sd_effect: float, n_sims: int, n_boot: int,
             rng: np.random.Generator) -> float:
    n_tasks = len(per_task)
    task_of = np.repeat(np.arange(n_tasks), per_task)
    n_ctx = len(task_of)
    # intercepts calibrated once on a large draw of the random effects
    big = 200_000
    t_big = rng.integers(0, n_tasks, big)
    u_big = rng.normal(0, sd_task, n_tasks * 50)[t_big]
    lin0 = u_big + rng.normal(0, sd_ctx, big)
    lin1 = lin0 + rng.normal(0, sd_effect, n_tasks * 50)[t_big] if sd_effect else lin0
    a0, a1 = solve_intercept(p0, lin0), solve_intercept(p1, lin1)
    hits = 0
    for _ in range(n_sims):
        u = rng.normal(0, sd_task, n_tasks)[task_of]
        v = rng.normal(0, sd_ctx, n_ctx)
        w = rng.normal(0, sd_effect, n_tasks)[task_of] if sd_effect else 0.0
        r0 = rng.binomial(reps, 1 / (1 + np.exp(-(a0 + u + v)))) / reps
        r1 = rng.binomial(reps, 1 / (1 + np.exp(-(a1 + u + v + w)))) / reps
        d = r1 - r0
        s = np.bincount(task_of, d, n_tasks)
        n = np.bincount(task_of, None, n_tasks)
        draws = rng.integers(0, n_tasks, (n_boot, n_tasks))
        est = s[draws].sum(1) / n[draws].sum(1)
        hits += np.quantile(est, 0.025) > 0
    return hits / n_sims


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261012)
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--boot", type=int, default=1000)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    print("contexts reps p0 p1 sd(task=ctx) sd_effect P(CI>0)")
    for n_ctx in (71, 55, 40):
        per_task = np.array(ALLOC) if n_ctx == 71 else subsample(ALLOC, n_ctx)
        for p1 in (0.01, 0.05, 0.07, 0.09, 0.13):
            for sd, sde in ((0.5, 0.0), (1.0, 0.0), (1.0, 0.7), (1.5, 0.7)):
                pw = simulate(per_task, 3, 0.01, p1, sd, sd, sde, a.sims, a.boot, rng)
                print(f"{per_task.sum():>3} 3 0.01 {p1:.2f} {sd} {sde} {pw:.2f}", flush=True)


if __name__ == "__main__":
    main()
