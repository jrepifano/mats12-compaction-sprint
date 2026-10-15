"""P49 plan: approximate minimum detectable effects (80% power, two-sided alpha 0.05).

A pair is one probe at one fork point in two contexts (e.g. POST vs PRE) with the same history,
world state and page. The paired difference d is in {-1, 0, 1}; Var(d) ~= discordance (assumed
0.25 for behaviour, 0.20 for fact retention). Pairs cluster within sessions: design effect
1 + (m - 1) * ICC (ICC assumed 0.1; 0.05 and 0.2 as sensitivity; re-estimated from the pilot).
Per session and model there are 9 fork points (3 wrappers x 3 compactions); with v violation
probes per fork point, the violation family has 9v pairs pooled and 3v per compaction index.
The second-minus-first interaction has about twice the variance of one index's contrast, so its
MDE is ~1.41x the per-index one. Fact retention: 3 wrappers x 10 eligible facts (F11 can be
absent) = 30 per index. All sessions are natural (the planted hedge runs offline in Gate 0).
This is a screening approximation, not a simulation, and not a power analysis for beliefs;
attrition (chains that stop before k = 3) lowers n.
Run: python3 -I power.py
"""
import math


def mde(pairs: float, m: int, icc: float = 0.1, disc: float = 0.25) -> float:
    eff = pairs / (1 + (m - 1) * icc)
    return 2.80 * math.sqrt(disc / eff)


def table(rows, n_viol=3, icc=0.1):
    for label, s in rows:
        pooled = mde(9 * n_viol * s, 9 * n_viol, icc)
        per_k = mde(3 * n_viol * s, 3 * n_viol, icc)
        print(f"  {label:16s} violations pooled {100 * pooled:5.1f}  per index {100 * per_k:5.1f}"
              f"  second-minus-first {141 * per_k:5.1f}  retention idx3-idx1 "
              f"{100 * mde(30 * s, 30, icc, disc=0.20):5.1f} pts")


if __name__ == "__main__":
    for icc in (0.05, 0.1, 0.2):
        print(f"ICC {icc}: plan allocation (3 violation probes per fork point)")
        table([("GLM 16", 16), ("DeepSeek 8", 8), ("both 24", 24)], 3, icc)
    print("ICC 0.1: reduction-ladder allocations")
    table([("DeepSeek 6, 3v", 6)], 3, 0.1)
    table([("GLM 16, 2v", 16), ("GLM 12, 2v", 12), ("DeepSeek 6, 2v", 6)], 2, 0.1)
    print("ICC 0.1: follow-up allocation (adds 16 GLM + 8 DeepSeek sessions)")
    table([("GLM 32", 32), ("DeepSeek 16", 16), ("both 48", 48)], 3, 0.1)
