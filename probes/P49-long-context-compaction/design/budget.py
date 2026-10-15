"""P49 v5 (compaction then an impossible task): projected API spend and bulk wall-clock.

v4 (offline fidelity) is budget_v4.py / power_v4.py. Prices: OpenRouter /endpoints queried
2026-10-15T21:56Z (design/measure/out/or/qwen__qwen3-coder.json): Qwen3-Coder-480B on Google
Vertex $0.22 in / $1.80 out per M, no cache discount (priced cold), 73 tok/s median, 99.9% uptime.
Assumptions (not measured; the Gate 0 run re-measures them): 15 agent turns per chain on average
(cap 24, as P44); 800 output tokens per turn; agent context per turn = history-at-start + 1.5k per
turn; post-compaction start context: Codex 9k, Inspect 8k, pi 28k (summary 3k + carry-over);
Fresh 5k; R 125k (the trajectory's measured median total at the 115.2k trigger is 122.6k).
Run: python3 -I budget.py [n_histories]"""
import sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 48
P_IN, P_OUT, TPS = 0.22, 1.80, 73
TURNS, OUT_TOK, GROWTH = 15, 800, 1_500
START = {"Fresh": 5_000, "R": 125_000, "Codex-self": 9_000, "pi-self": 28_000,
         "Inspect-self": 8_000, "Inspect-DS": 8_000, "Inspect-Haiku": 8_000}
SUMMARY = {  # (model $/M in, out, input tokens, output tokens) per compaction
    "Codex-self": (0.22, 1.80, 125_000, 4_000), "pi-self": (0.22, 1.80, 55_000, 4_000),
    "Inspect-self": (0.22, 1.80, 125_000, 4_000), "Inspect-DS": (0.03836, 0.077, 125_000, 3_000),
    "Inspect-Haiku": (0.10, 0.50, 125_000, 3_000)}
BELIEF_CALLS = 2  # B1 real-world, B3 evaluation: forked, tools off, at the new task's start


def chain(arm):
    ctx = sum(START[arm] + GROWTH * t for t in range(TURNS))
    agent = ctx * P_IN / 1e6 + TURNS * OUT_TOK * P_OUT / 1e6
    belief = BELIEF_CALLS * (START[arm] * P_IN / 1e6 + 200 * P_OUT / 1e6)
    s = SUMMARY.get(arm)
    summ = 1.1 * (s[2] * s[0] + s[3] * s[1]) / 1e6 if s else 0.0  # 10% retries
    return agent + belief + summ


def report(n):
    per = {a: n * chain(a) for a in START}
    gate0 = 60 * chain("Fresh") + 4 * chain("R")   # 15 tasks x 2 prompt levels x 2; 4 R checks
    return per, gate0, sum(per.values()) + gate0


if __name__ == "__main__":
    for n in (N, 32, 64, 96):
        per, g0, tot = report(n)
        hrs = n * len(START) * TURNS * (2.0 + 6.0 + OUT_TOK / TPS) / 16 / 3600
        print(f"N={n:3d}: " + "  ".join(f"{a} ${v:.1f}" for a, v in per.items())
              + f"  | Gate 0 ${g0:.1f} | TOTAL ${tot:.1f} | bulk >= {hrs:.1f} h at 16 sandboxes")
    print(f"per chain: " + ", ".join(f"{a} ${chain(a):.3f}" for a in START))
