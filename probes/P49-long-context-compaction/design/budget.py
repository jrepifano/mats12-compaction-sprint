"""P49 plan budget: every paid call, by phase, under explicit assumptions (plan.md, Cap).

Replaces the per-session sketch in cost_model.py (kept for its chain arithmetic). All quantities
are projections; the smoke and pilot re-measure growth, carry-over, cache share by request type,
branch lengths and summary lengths, and this script is re-run with them before the bulk.

Session (per model), after review r1:
  trunk R      never compacted, runs until the last wrapper event it must match (no tail)
  chains C_w   codex / inspect / pi, from the wrapper's first trigger on the trunk to its 3rd event
  compactions  one summariser call per event (pi: separate request = cache miss; codex/inspect
               append to the live history = prefix hit), +5% retries
  probe forks  at every event k of every chain, the probe page is delivered IMMEDIATELY at the
               trigger boundary (no bridge turns) in PRE, POST, ONESHOT (k >= 2) and R (k >= 2;
               at k = 1 R == PRE). R points are shared by codex and inspect (same trigger, so the
               same ticket position, priced as shared; pi's are separate). F probes per fork point,
               a branch stops at the first consequential action: priced at 2.5 turns on average
  oneshot      one summary of the chain's full raw history at k = 2, 3 (input up to ~350k, miss)
  beliefs      B1, B2, B3 as separate one-call forks at every fork point, and every 20 trunk turns
  calibration  k = 1: PRE_del for the 6 violation facts (fact and its restatements removed);
               k = 3: POST + fact restated, 2 facts per session (positive control)
Gate 0 (pilot trunks, offline; 4 per model): per trunk, recursive offline compaction at each
  wrapper's trigger points (1 draw at k = 1, 2; 2 draws at k = 3; priced as 0.6 of 2 draws everywhere), ONESHOT, the Claude API default prompt x 2 at the codex points, PRE/POST
  belief forks (B1-B3), and the planted-hedge copy (1 draw per wrapper point + B1 + the P9 probe).
G0-A: 40 SWE-rebench trajectories x 3 wrappers x 2 summariser models at ~110k input (cache miss).
Judges: one call per summary (all facts), Claude Haiku 5.5 on every summary, Gemini 3.8 Flash on
  40% of index-3 summaries and all 220 validation items; B2 answers by Haiku; validation 120 planted + 100 natural x 2.
Run: python3 -I budget.py
"""
from __future__ import annotations

import itertools

PROV = {  # $/M in, out, cache-read; tok/s; latency s (OpenRouter endpoints, 2026-10-15T02:19Z)
    "GLM": (0.0435, 0.145, 0.0087, 38, 1.84),      # z-ai/glm-5.3-flash, StreamLake fp8
    "DS": (0.091, 0.182, 0.0182, 61, 1.43),        # deepseek/deepseek-v4-flash, GMICloud fp8
}
HAIKU = (0.10, 0.50, 0.010)                        # anthropic/claude-haiku-5.5, Vertex
GEMINI = (0.75, 3.75, 0.075)                       # google/gemini-3.8-flash, AI Studio

B = 12_000                                         # system + tools + opener
WR = {"codex": (115_200, 17_000), "inspect": (115_200, 18_000), "pi": (111_616, 35_000)}
K = 3
PROBE_TURNS = 3
PROBE_TURNS_MEAN = 2.5
GATE0_SCALE = 0.6
GEMINI_SHARE = 0.4
PILOT = 4
BRANCH_GROWTH = 1_500
SUMMARY_OUT = 6_000
RETRY = 1.05


def call(ctx: float, out: float, p, hit: float) -> float:
    return (ctx * hit * p[2] + ctx * (1 - hit) * p[0] + out * p[1]) / 1e6


def chain_events(trigger, carry, g):
    """Turn index of each event, context at the event, and per-turn contexts."""
    ctx, t, ev, seq = B, 0, [], []
    while len(ev) < K:
        seq.append(ctx)
        ctx += g
        t += 1
        if ctx >= trigger:
            ev.append((t, ctx))
            ctx = carry
    return ev, seq


def session(p, g, hit, n_probe=4, calib=True):
    out_turn = 0.9 * g
    chains = {w: chain_events(*WR[w], g) for w in WR}
    last = max(ev[-1][0] for ev, _ in chains.values())
    trunk = [B + g * t for t in range(last + 1)]
    c = {}
    c["trunk"] = sum(call(x, out_turn, p, hit) for x in trunk)
    c["chains"] = sum(sum(call(x, out_turn, p, hit) for x in seq[ev[0][0]:])
                      for ev, seq in chains.values())
    comp = 0.0
    for w, (ev, _) in chains.items():
        for _, ctx in ev:
            comp += call(ctx, SUMMARY_OUT, p, 0 if w == "pi" else hit)
    c["compactions"] = comp * RETRY

    def branch(ctx, h=hit):
        return sum(call(ctx + BRANCH_GROWTH * j, 0.9 * BRANCH_GROWTH, p, h)
                   for j in range(PROBE_TURNS))

    def belief(ctx, h=hit):
        return 3 * call(ctx, 600, p, h)

    probes = beliefs = oneshot = calc = 0.0
    for w, (ev, _) in chains.items():
        carry = WR[w][1]
        for k, (t, ctx) in enumerate(ev, start=1):
            pts = [ctx, carry]                         # PRE, POST
            if k >= 2:
                raw = B + g * t                        # chain's full raw history
                oneshot += call(raw, SUMMARY_OUT, p, 0) * RETRY
                pts += [carry]                         # ONESHOT (cold)
                if w != "inspect":                     # R, shared by codex and inspect
                    pts += [trunk[min(t, last)]]
            probes += n_probe * sum(branch(x) for x in pts)
            beliefs += sum(belief(x) for x in pts)
        if calib:
            calc += 6 * branch(ev[0][1]) / 3        # PRE_del shared by codex/inspect/pi ~ 1/3 each
            calc += 2 * branch(carry)
    beliefs += sum(belief(x) for x in trunk[::20])
    probes *= PROBE_TURNS_MEAN / PROBE_TURNS
    c.update(probes=probes, oneshot=oneshot, beliefs=beliefs, calibration=calc)
    c["total"] = sum(c.values())
    c["trunk_turns"] = last
    c["trunk_h"] = last * (p[4] + out_turn / p[3] + 1.0) / 3600
    c["calls"] = len(trunk) + sum(len(s) - ev[0][0] for ev, s in chains.values())
    return c


def gate0_offline(p, g, hit):
    """Extra cost per pilot trunk (the trunk itself is counted in session())."""
    tot = 0.0
    for w in WR:
        ev, _ = chain_events(*WR[w], g)
        for k, (t, ctx) in enumerate(ev, start=1):
            raw = B + g * t
            tot += 2 * call(raw if k == 1 else WR[w][1] + (raw - B) / k, SUMMARY_OUT, p, 0)
            if k >= 2:
                tot += 2 * call(raw, SUMMARY_OUT, p, 0)        # ONESHOT x 2
            tot += 2 * 3 * call(raw, 600, p, hit)               # PRE/POST beliefs (POST cheaper)
            tot += call(raw, SUMMARY_OUT, p, 0)                 # planted copy, 1 draw
            tot += call(WR[w][1], 600, p, 0) + 3 * call(WR[w][1], 1350, p, 0)  # B1 + P9 probe
    ev, _ = chain_events(*WR["codex"], g)
    tot += sum(2 * call(B + g * t, SUMMARY_OUT, p, 0) for t, _ in ev)  # Claude API prompt
    return tot * RETRY * GATE0_SCALE


def g0a(p):
    return 40 * 3 * call(110_000, SUMMARY_OUT, p, 0) * RETRY


def judges(n_sessions, n_gate_trunks):
    n_sum = n_sessions * (9 + 6) + n_gate_trunks * 40
    haiku = n_sum * call(9_000, 1_500, HAIKU, 0)
    n_idx3 = GEMINI_SHARE * (n_sessions * 3 + n_gate_trunks * 8) + 220
    gem = n_idx3 * call(9_000, 1_500, GEMINI, 0)
    b2 = n_sessions * 45 * call(3_000, 800, HAIKU, 0)
    val = 220 * call(9_000, 1_500, HAIKU, 0)
    return haiku + gem + b2 + val


def plan_total(n_glm, n_ds, g=2000, hit=0.85, n_probe=4, pilot=PILOT, verbose=False):
    rows = {}
    for m, n in (("GLM", n_glm), ("DS", n_ds)):
        p = PROV[m]
        s = session(p, g, hit, n_probe)
        rows[m] = {
            "sessions": n * s["total"],
            "smoke": 0.45 * s["total"],
            "gate0_offline": pilot * gate0_offline(p, g, hit),
            "g0a": g0a(p),
            "per_session": s["total"],
            "trunk_h": s["trunk_h"],
            "calls_per_session": s["calls"],
        }
    jud = judges(n_glm + n_ds, 2 * pilot)
    total = sum(r["sessions"] + r["smoke"] + r["gate0_offline"] + r["g0a"] for r in rows.values())
    total += jud
    if verbose:
        for m, r in rows.items():
            print(f"  {m}: per session ${r['per_session']:.2f} (trunk {r['trunk_h']:.1f} h, "
                  f"~{r['calls_per_session']} agent calls), sessions ${r['sessions']:.1f}, "
                  f"smoke ${r['smoke']:.1f}, gate0 offline ${r['gate0_offline']:.1f}, "
                  f"G0-A ${r['g0a']:.1f}")
        print(f"  judges ${jud:.1f}")
    return total


if __name__ == "__main__":
    print("Plan allocation: GLM 16, DeepSeek 8, 4 probes per fork point")
    for g, hit in itertools.product((1200, 2000, 3000), (0.85, 0.72, 0.60)):
        t = plan_total(16, 8, g, hit, verbose=(g == 2000 and hit == 0.85))
        print(f"growth {g}  cache {hit:.2f}: total ${t:.1f}")
    print("\nReduction ladder (applied in order until the re-projection is <= $75):")
    for g, hit in ((2000, 0.60), (1200, 0.72), (1200, 0.60)):
        for label, (ng, nd, npb) in [("16/8/4", (16, 8, 4)), ("16/6/4", (16, 6, 4)),
                                     ("16/6/3", (16, 6, 3)), ("12/6/3", (12, 6, 3)),
                                     ("12/6/2", (12, 6, 2))]:
            print(f"  growth {g} cache {hit:.2f} {label}: ${plan_total(ng, nd, g, hit, npb):.1f}")
