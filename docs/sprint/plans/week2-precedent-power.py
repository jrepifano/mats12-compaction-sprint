"""Approximate power of the blocked randomization test (2,000 sampled permutations per dataset,
400 datasets per cell, so about +/-5 points of Monte Carlo error) (Monte Carlo permutations), history-level outcomes.
Two fork models: 'agree' (both forks equal the history's outcome) and 'indep' (forks independent
Bernoulli with the arm's rate). Arms n per arm, split equally over two first tasks."""
import numpy as np
rng = np.random.default_rng(20261027)
def pval(y, arm, block, nperm=2000):
    obs = y[arm == 1].sum() - y[arm == 0].sum()
    cnt = 0
    for _ in range(nperm):
        a = arm.copy()
        for b in (0, 1):
            idx = np.where(block == b)[0]
            a[idx] = rng.permutation(a[idx])
        d = y[a == 1].sum() - y[a == 0].sum()
        cnt += abs(d) >= abs(obs)
    return (cnt + 1) / (nperm + 1)
def power(n, p0, d, model, sims=400):
    hits = 0
    arm = np.array([0] * (n // 2) + [1] * (n // 2) + [0] * (n // 2) + [1] * (n // 2))
    block = np.array([0] * n + [1] * n)
    for _ in range(sims):
        p = np.where(arm == 1, p0 + d, p0)
        if model == "agree":
            y = 2 * (rng.random(2 * n) < p)
        else:
            y = (rng.random(2 * n) < p).astype(int) + (rng.random(2 * n) < p).astype(int)
        hits += pval(y, arm, block) < 0.05
    return hits / sims
# Planning assumptions: clean-arm rate 4% (task chaining's teammate-commit loss without history,
# 1/24); effect = first stage x the 85-point observational gap on that measure.
for n in (12, 16):
    for d in (0.30, 0.43, 0.53, 0.60):
        print(n, d, power(n, 0.04, d, "agree"), power(n, 0.04, d, "indep"))
