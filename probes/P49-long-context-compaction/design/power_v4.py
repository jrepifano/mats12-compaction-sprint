"""P49 (offline revision): approximate minimum detectable differences, 80% power, two-sided 0.05.

The v3 version (live world) is in git history (commit f20e5f02).
A pair is one fact type in one trajectory, one wrapper and one summariser, scored at two arms
(e.g. L vs S1: lost after carry-over yes/no). The paired difference d is in {-1, 0, 1};
Var(d) ~= discordance (assumed 0.35; 0.25 and 0.45 as sensitivity). Pairs cluster within a
trajectory (3 wrappers x 2 summarisers): design effect 1 + (m - 1) * ICC, ICC 0.3 assumed.
Eligible trajectories per fact (fact present at both cuts), from measure/out/swe_long_detail.json
scaled to N = 96: latest error 66, latest suite status 75, task constraint 96, files 74.
A screening approximation, not a simulation.  Run: python3 -I power.py"""
import math

ELIG = {"latest error": 66, "latest suite status": 75, "test-edit constraint": 96, "files named": 74}


def mde(pairs, m, icc=0.3, disc=0.35):
    return 2.80 * math.sqrt(disc / (pairs / (1 + (m - 1) * icc)))


if __name__ == "__main__":
    for disc in (0.25, 0.35, 0.45):
        print(f"discordance {disc}:")
        for fact, n in ELIG.items():
            cell = mde(n, 1, disc=disc)
            per_summ = mde(3 * n, 3, disc=disc)
            pooled = mde(6 * n, 6, disc=disc)
            print(f"  {fact:22s} n={n:3d}  per wrapper x summariser {100 * cell:4.1f}"
                  f"  per summariser (3 wrappers) {100 * per_summ:4.1f}  pooled {100 * pooled:4.1f} pts")
