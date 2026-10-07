"""X3b power simulation (prereg §4.6).

Approximates the governing test: a paired permutation (sign-flip) test of the risk difference
between two headers, within task x repeat blocks, with a task random effect on the logit scale
(SD 0.58, X3 calibration). Approximations: 1,000 sign flips per simulated study (the analysis
uses 10,000 permutations); 800 simulated studies per cell; Holm is represented by its most
conservative threshold (alpha / 3), not simulated jointly. Rates are given for a typical task
(random effect 0); the marginal rate is printed beside each.

Also simulates how often a task-cluster bootstrap 95% interval for a true-zero contrast has its
upper end below a margin, which sets the "no large reduction" margins in prereg §6.

Usage: uv run python scripts/x3b_power.py
"""
from __future__ import annotations

import numpy as np

SD = 0.58
TASKS = 15


def _logit(p: float) -> float:
    return float(np.log(p / (1 - p)))


def marginal(p: float, rng: np.random.Generator, n: int = 200_000) -> float:
    u = rng.normal(0, SD, n)
    return float(np.mean(1 / (1 + np.exp(-(_logit(p) + u)))))


def power(pa: float, pb: float, reps: int, alpha: float, rng: np.random.Generator,
          nsim: int = 800, nflip: int = 1000) -> float:
    pb = max(pb, 1e-4)
    hits = 0
    for _ in range(nsim):
        u = rng.normal(0, SD, TASKS)[:, None]
        ya = rng.random((TASKS, reps)) < 1 / (1 + np.exp(-(_logit(pa) + u)))
        yb = rng.random((TASKS, reps)) < 1 / (1 + np.exp(-(_logit(pb) + u)))
        d = (ya.astype(int) - yb.astype(int)).ravel()
        obs = abs(d.mean())
        if obs == 0:
            continue
        flips = rng.choice([-1, 1], size=(nflip, d.size))
        null = np.abs((flips * d).mean(1))
        p = (1 + (null >= obs - 1e-12).sum()) / (nflip + 1)
        hits += p < alpha
    return hits / nsim


def interval_reach(pa: float, pb: float, reps: int, rng: np.random.Generator, nsim: int = 400,
                   nboot: int = 1000) -> tuple[float, dict[int, float]]:
    """Mean 95% half-width (points) and P(upper end < margin) for capability minus header."""
    ups, half = [], []
    for _ in range(nsim):
        u = rng.normal(0, SD, TASKS)[:, None]
        ya = rng.random((TASKS, reps)) < 1 / (1 + np.exp(-(_logit(pa) + u)))
        yb = rng.random((TASKS, reps)) < 1 / (1 + np.exp(-(_logit(max(pb, 1e-4)) + u)))
        d = (ya.astype(float) - yb.astype(float)).mean(1)
        boot = d[rng.integers(0, TASKS, (nboot, TASKS))].mean(1)
        lo, hi = np.percentile(boot, [2.5, 97.5])
        ups.append(hi * 100)
        half.append((hi - lo) * 50)
    ups_a = np.array(ups)
    return float(np.mean(half)), {m: round(float(np.mean(ups_a < m)), 2) for m in (8, 10, 12, 15)}


def main() -> None:
    rng = np.random.default_rng(0)
    print("rate (typical task) -> marginal:",
          {p: round(marginal(p, rng), 3) for p in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.33, 0.45)})
    print("\nStage 1, GLM, 8 repeats (120 per header); alpha 0.0167 (Holm worst case) and 0.05")
    for pa, pb in [(0.2, 0.0), (0.2, 0.05), (0.25, 0.10), (0.2, 0.10), (0.33, 0.15), (0.33, 0.2)]:
        print(f"  {pa:.2f} vs {pb:.2f}: {power(pa, pb, 8, 0.0167, rng):.2f} / "
              f"{power(pa, pb, 8, 0.05, rng):.2f}")
    print("\nStage 1, GLM, reduced sizes after an automatic cut; alpha 0.0167")
    for reps in (6, 7):
        for pa, pb in [(0.2, 0.05), (0.25, 0.10)]:
            print(f"  {reps} repeats {pa:.2f} vs {pb:.2f}: {power(pa, pb, reps, 0.0167, rng):.2f}")
    print("\nStage 2, Gemini, capability vs safety, alpha 0.05")
    for reps in (7, 8, 9, 10):
        for pa, pb in [(0.45, 0.10), (0.45, 0.25), (0.45, 0.30), (0.30, 0.15)]:
            print(f"  {reps} repeats ({TASKS * reps} per header) {pa:.2f} vs {pb:.2f}: "
                  f"{power(pa, pb, reps, 0.05, rng):.2f}")


    print("\nInterval reachability, true zero (half-width in points; P(upper end < margin))")
    for label, pa, pb, reps in [("GLM 22%, 8 repeats", 0.22, 0.22, 8),
                                ("GLM 33%, 8 repeats", 0.33, 0.33, 8),
                                ("Gemini 45%, 9 repeats", 0.45, 0.45, 9)]:
        hw, reach = interval_reach(pa, pb, reps, rng)
        print(f"  {label}: +/-{hw:.1f}; {reach}")


if __name__ == "__main__":
    main()
