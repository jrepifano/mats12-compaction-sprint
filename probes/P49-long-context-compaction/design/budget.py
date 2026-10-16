"""P49 v5.2 (stitched ~200k histories, compaction 1-3 times, then an impossible task): projected
API spend and bulk wall-clock. v5.1 (one ~125k session, Haiku/DS summariser arms) is in git
history (branch commit 0b6c17f3); v4 (offline fidelity) is budget_v4.py / power_v4.py.

Prices: OpenRouter /endpoints re-queried 2026-10-16T03:43Z (design/measure/out/or/
qwen__qwen3-coder_20261016.json): Qwen3-Coder-480B on Google Vertex $0.22 in / $1.80 out per M,
no cache discount (priced cold), context 262,144, max output 65,536, 99.9% uptime over 1 day.
Assumptions (not measured; Gate 0 re-measures them):
- history = two or more whole sessions stitched to 195-205k provider-counted prompt tokens
  (system + tool schemas + messages + template); priced at 200k;
- 15 agent turns per chain on average (cap 24); 800 output tokens per turn; context grows 1.5k
  per turn (Qwen3-Coder is non-reasoning; X3/P44 non-reasoning turns were ~0.7-1.1k,
  design/measure/out/per_turn_split.json);
- post-compaction start context: Codex 12k (summary + kept user messages), pi 28k (summary +
  ~20k tail), Inspect 9k (summary + original input), Fresh 5k;
- one compaction call per history per method, ~4k output, 10% retries; the repeated arms reuse
  Inspect-self's summary as compaction 1, and k=3 reuses k=2's compaction 2 (nested), so k=2 and
  k=3 each add one ~200k call;
- 2 belief calls per chain (B1, B3), forked on the starting context, ~200 output tokens each;
- latency per turn: 2 s + prefill at 20k prompt tokens/s + 800 tokens at 73 tok/s + 3 s tools;
  per chain also 2 belief calls (prefill + 200 tokens) and 30 s of sandbox start, snapshots and
  scoring; per history 6 summary calls (prefill + 4k tokens at 73 tok/s), of which Rep-k3 waits
  on 3 in series (compactions 1-3), all occupying API slots.
All of the above are assumptions (labelled estimates), not measured wrapper or request sizes;
pi and Codex truncate tool results in the summariser input, so their summary inputs are upper
bounds. Gate 0 measures them.
Gate 0: 8 histories x {Fresh, R, Inspect-self, pi-self} x prompt levels A and B; the two levels
share each history's compactions (compaction happens before the transition).
Run: python3 -I budget.py"""

P_IN, P_OUT, TPS, PREFILL = 0.22, 1.80, 73, 20_000
OUT_TOK, GROWTH, H = 800, 1_500, 200_000
START = {"Fresh": 5_000, "R": H, "Codex-self": 12_000, "pi-self": 28_000,
         "Inspect-self": 9_000, "Inspect-120k": 9_000, "Rep-k2": 10_000, "Rep-k3": 10_000}
SUMMARY_IN = {"Codex-self": H, "pi-self": H - 20_000, "Inspect-self": H,
              "Inspect-120k": 128_000, "Rep-k2": H, "Rep-k3": H}  # marginal call per arm
MINIMUM = ("Fresh", "R", "Inspect-self", "pi-self", "Inspect-120k", "Rep-k2")
GATE0 = ("Fresh", "R", "Inspect-self", "pi-self")


def agent(arm, turns):
    ctx = sum(START[arm] + GROWTH * t for t in range(turns))
    belief = 2 * (START[arm] * P_IN + 200 * P_OUT) / 1e6
    return ctx * P_IN / 1e6 + turns * OUT_TOK * P_OUT / 1e6 + belief


def summary(arm):
    s = SUMMARY_IN.get(arm)
    return 1.1 * (s * P_IN + 4_000 * P_OUT) / 1e6 if s else 0.0


def per_history(arms, turns):
    return sum(agent(a, turns) + summary(a) for a in arms)


def gate0(turns):
    return 8 * (2 * sum(agent(a, turns) for a in GATE0) + sum(summary(a) for a in GATE0))


def chain_hours(arm, turns):
    sec = sum(2 + (START[arm] + GROWTH * t) / PREFILL + OUT_TOK / TPS + 3 for t in range(turns))
    sec += 2 * (2 + START[arm] / PREFILL + 200 / TPS) + 30  # beliefs, sandbox, scoring
    return sec / 3600


def summary_hours(arm):
    s = SUMMARY_IN.get(arm)
    return (2 + s / PREFILL + 4_000 / TPS) / 3600 if s else 0.0


if __name__ == "__main__":
    print("per-chain agent cost at 15 / 24 turns: " + ", ".join(
        f"{a} ${agent(a, 15):.3f}/${agent(a, 24):.3f}" for a in START))
    print("summary cost per arm: " + ", ".join(f"{a} ${summary(a):.3f}" for a in SUMMARY_IN))
    for turns in (15, 20, 24):
        g = gate0(turns)
        for n in (24, 28, 32, 36, 40):
            full = n * per_history(START, turns) + g
            mini = n * per_history(MINIMUM, turns) + g
            print(f"turns {turns} N={n}: full ${full:.1f}, minimum ${mini:.1f}"
                  f" (Gate 0 ${g:.1f}; R ${n * agent('R', turns):.1f})")
    rep3_wait = 60 * sum(summary_hours(a) for a in ("Inspect-self", "Rep-k2", "Rep-k3"))
    for turns in (15, 24):
        print(f"turns {turns}: R share of full-design bulk {agent('R', turns) / per_history(START, turns):.0%}")
        for conc in (16, 32):
            for n in (24, 32, 40):
                slot_h = n * (sum(chain_hours(a, turns) for a in START)
                              + sum(summary_hours(a) for a in SUMMARY_IN))
                print(f"bulk wall-clock, turns {turns}, N={n}, {conc} in flight: {slot_h / conc:.2f} h"
                      f" (longest chain R {60 * chain_hours('R', turns):.0f} min;"
                      f" Rep-k3 waits {rep3_wait:.1f} min on 3 serial compactions)")
