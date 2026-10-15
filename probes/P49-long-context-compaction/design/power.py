"""P49 plan: approximate minimum detectable effects (80% power, two-sided alpha 0.05).

A pair is one probe at one fork point in two contexts (e.g. POST vs PRE) with the same history
and world state. The paired difference d is in {-1, 0, 1}; Var(d) ~= discordance (assumed 0.25
for behaviour, 0.20 for fact retention). Pairs cluster within sessions: design effect
1 + (m - 1) * ICC (ICC assumed 0.1, with 0.05 and 0.2 as sensitivity; re-estimated from the pilot).
Per session and model, violation family (6 probes, 3 per fork point): 3 wrappers x 3 compactions
x 3 = 27 pairs; per compaction index 9; per wrapper and index 3. Fact retention: 3 wrappers x
11 facts = 33 per index. The second-minus-first interaction has about twice the variance of a
single-index contrast (difference of two paired differences), so its MDE is ~1.41x the per-index
one. All natural sessions (the planted hedge runs offline in Gate 0 only).
Screening approximation, not a simulation; attrition (chains that stop before k = 3) lowers n.
Run: python3 -I power.py
"""
import math


def mde(pairs: float, m: int, icc: float = 0.1, disc: float = 0.25) -> float:
    eff = pairs / (1 + (m - 1) * icc)
    return 2.80 * math.sqrt(disc / eff)


if __name__ == "__main__":
    for icc in (0.05, 0.1, 0.2):
        print(f"ICC {icc}")
        for label, s in [("GLM 16", 16), ("DeepSeek 8", 8), ("both 24", 24),
                         ("GLM 32", 32), ("DeepSeek 24", 24), ("both 56", 56)]:
            pooled = mde(27 * s, 27, icc)
            per_k = mde(9 * s, 9, icc)
            print(f"  {label:12s} violations pooled {100 * pooled:5.1f}  per index "
                  f"{100 * per_k:5.1f}  second-minus-first {141 * per_k:5.1f}  per wrapper x "
                  f"index {100 * mde(3 * s, 3, icc):5.1f}  retention idx3-idx1 "
                  f"{100 * mde(33 * s, 33, icc, disc=0.20):5.1f} pts")
