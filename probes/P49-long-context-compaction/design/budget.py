"""P49 plan budget as an expected-cost request inventory (plan.md, Cap). Run: python3 -I budget.py

Every paid request the plan makes is generated as a record (phase, model, wrapper, event k, arm,
context tokens, cold/warm, output tokens) and priced. All token quantities are projections under
the assumptions below; the smoke re-measures growth, carry-over, summary length, branch length and
cache share by request type, and this script is re-run with them before Gate 0 and the bulk.

Cache rule (per request): a request whose prefix was just sent (the next turn of the same
history, or a sibling fork of a context that was just warmed) is "warm": a share HIT of its
context is billed at the cache-read price. The FIRST request on any newly built or long-idle
context is "cold": only the shared system/tools/opener prefix B is cached. Cold firsts: every
POST / ONESHOT context (new summary), every R fork (the trunk passed that point earlier), every
fork from a saved pilot trunk in the bulk, every Gate 0 offline context, pi's summariser calls
(a separate request), and every judge call. Fork points are run "warm-up first": one belief
call warms the context, the other forks then start. ASSUMPTION to verify in the pilot: a
tools-off belief call warms the cache for the tool-bearing probe forks of the same context; if
not, each probe family pays its own cold first.
Records carry phase/model/kind and an expected weight (a probe's third turn has weight 0.5;
summary records carry weight RETRY), so the record count exceeds the expected request count.

Event timeline (plan.md, Setup): at the settled boundary where wrapper w's check fires, snapshot
history and world; build the compacted context without the page; append the same page to each
measurement fork (PRE, POST, ONESHOT k >= 2, R k >= 2); forks run with compaction disabled for
<= 3 turns and are discarded. R forks are priced distinct for every wrapper and event (no
deduplication assumed).
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass

PROV = {  # $/M in, out, cache-read; tok/s; latency s (OpenRouter endpoints, 2026-10-15T02:19Z)
    "GLM": (0.0435, 0.145, 0.0087, 38, 1.84),      # z-ai/glm-5.3-flash, StreamLake fp8
    "DS": (0.091, 0.182, 0.0182, 61, 1.43),        # deepseek/deepseek-v4-flash, GMICloud fp8
}
JUDGE = {"haiku": (0.10, 0.50, 0.010, 185, 1.49),  # anthropic/claude-haiku-5.5, Vertex
         "gemini": (0.75, 3.75, 0.075, 87, 2.25)}  # google/gemini-3.8-flash, AI Studio

B = 12_000                 # system + tools + opener (always-shared prefix)
WR = {"codex": (115_200, 17_000), "inspect": (115_200, 18_000), "pi": (111_616, 35_000)}
K = 3
SUMMARY_OUT = 6_000        # summary incl. reasoning
PI_SUMMARY_CALLS = 2       # history + split-turn prefix summary (conservative: every pi event)
BELIEF_OUT = 600
PROBE_TURN_WEIGHTS = (1.0, 1.0, 0.5)  # a branch stops at the first consequential action (<= 3 turns; mean 2.5)
BRANCH_GROWTH = 1_500
RETRY = 1.05               # empty/length-capped summaries retried once (pi's behaviour)
N_VIOL, N_OTHER = 3, 1     # probes per fork point (violation family, other families)
CALIB_DEL = 6              # PRE_del facts at the codex/inspect k = 1 boundary, per session
CALIB_RESTATE = 2          # POST_3 + fact restated, per session (one wrapper, rotated)
TRUNK_BELIEF_EVERY = 20


@dataclass
class Req:
    phase: str
    model: str
    kind: str              # agent | summary | belief | probe | judge
    ctx: float
    out: float
    cold: bool
    w: float = 1.0         # expected count (e.g. a probe's third turn happens half the time)


def price(r: Req, hit: float) -> float:
    p = PROV.get(r.model) or JUDGE[r.model]
    if r.cold:
        cached = min(B, r.ctx) * hit if r.kind not in ("judge",) and r.model in PROV else 0.0
    else:
        cached = r.ctx * hit
    return r.w * (cached * p[2] + (r.ctx - cached) * p[0] + r.out * p[1]) / 1e6


def seconds(r: Req) -> float:
    p = PROV.get(r.model) or JUDGE[r.model]
    return r.w * (p[4] + r.out / p[3] + r.ctx / 20_000)  # latency + decode + prefill (~20k tok/s)


def events(trigger, carry, g):
    ctx, t, ev, seq = B, 0, [], []
    while len(ev) < K:
        seq.append(ctx)
        ctx += g
        t += 1
        if ctx >= trigger:
            ev.append((t, ctx))
            ctx = carry
    return ev, seq


def fork_requests(phase, m, ctx, first_cold, n_viol, n_other, beliefs=3):
    """One fork point: warm-up belief first, then the other beliefs and the probe branches."""
    out = [Req(phase, m, "belief", ctx + 300, BELIEF_OUT, first_cold)]
    out += [Req(phase, m, "belief", ctx + 300, BELIEF_OUT, False) for _ in range(beliefs - 1)]
    for _ in range(n_viol + n_other):
        for j, wt in enumerate(PROBE_TURN_WEIGHTS):
            out.append(Req(phase, m, "probe", ctx + 400 + BRANCH_GROWTH * j,
                           0.9 * BRANCH_GROWTH, False, wt))
    return out


def session(m, g, phase="bulk", n_viol=N_VIOL, n_other=N_OTHER, saved_trunk=False,
            trunk_included=True):
    reqs = []
    ch = {w: events(*WR[w], g) for w in WR}
    last = max(ev[-1][0] for ev, _ in ch.values())
    trunk = [B + g * t for t in range(last + 1)]
    out_turn = 0.9 * g
    if trunk_included:
        reqs += [Req(phase, m, "agent", x, out_turn, i == 0) for i, x in enumerate(trunk)]
        reqs += [Req(phase, m, "belief", x + 300, BELIEF_OUT, False)
                 for x in trunk[::TRUNK_BELIEF_EVERY] for _ in range(3)]
    for w, (ev, seq) in ch.items():
        carry = WR[w][1]
        # chain turns after the first event: the first turn after each compaction is cold
        ev_turns = {t for t, _ in ev}
        for t in range(ev[0][0], len(seq)):
            reqs.append(Req(phase, m, "agent", seq[t], out_turn, t in ev_turns))
        for k, (t, ctx) in enumerate(ev, start=1):
            n_calls = PI_SUMMARY_CALLS if w == "pi" else 1
            for _ in range(n_calls):
                reqs.append(Req(phase, m, "summary", ctx, SUMMARY_OUT, w == "pi", RETRY))
            first_pre_cold = saved_trunk and k == 1
            reqs += fork_requests(phase, m, ctx, first_pre_cold, n_viol, n_other)        # PRE
            reqs += fork_requests(phase, m, carry, True, n_viol, n_other)                # POST
            if k >= 2:
                raw = B + g * t
                reqs.append(Req(phase, m, "summary", raw, SUMMARY_OUT, True, RETRY))   # ONESHOT
                reqs += fork_requests(phase, m, carry, True, n_viol, n_other)
                reqs += fork_requests(phase, m, trunk[min(t, last)], True, n_viol, n_other)  # R
    # calibration: PRE_del at the codex/inspect k = 1 boundary; restated POST_3 positive control
    t1, ctx1 = ch["codex"][0][0]
    for _ in range(CALIB_DEL):                     # each deletion twin is a new context: cold first
        reqs += [Req(phase, m, "probe", ctx1 + 400 + BRANCH_GROWTH * j, 0.9 * BRANCH_GROWTH,
                     j == 0, wt) for j, wt in enumerate(PROBE_TURN_WEIGHTS)]
    for _ in range(CALIB_RESTATE):
        reqs += [Req(phase, m, "probe", WR["codex"][1] + 600 + BRANCH_GROWTH * j,
                     0.9 * BRANCH_GROWTH, j == 0, wt) for j, wt in enumerate(PROBE_TURN_WEIGHTS)]
    return reqs


def smoke(m, g):
    """Trunk to the first trigger, each chain through one compaction, every k = 1 fork type,
    3 post-compaction turns per chain, and a 2-call reasoning-inclusion check (the calibration
    runs in the bulk, not the smoke)."""
    reqs = []
    ch = {w: events(*WR[w], g) for w in WR}
    t1 = max(ev[0][0] for ev, _ in ch.values())
    reqs += [Req("smoke", m, "agent", B + g * t, 0.9 * g, t == 0) for t in range(t1 + 1)]
    for w, (ev, _) in ch.items():
        t, ctx = ev[0]
        for _ in range(PI_SUMMARY_CALLS if w == "pi" else 1):
            reqs.append(Req("smoke", m, "summary", ctx, SUMMARY_OUT, w == "pi"))
        reqs += fork_requests("smoke", m, ctx, False, N_VIOL, N_OTHER)
        reqs += fork_requests("smoke", m, WR[w][1], True, N_VIOL, N_OTHER)
        reqs += [Req("smoke", m, "agent", WR[w][1] + g * j, 0.9 * g, j == 0) for j in range(3)]
    reqs += [Req("smoke", m, "agent", B + g * 20, 0.9 * g, True) for _ in range(2)]
    return reqs


def gate0_offline(m, g):
    """Per pilot trunk (the trunk itself is in session()): recursive offline summaries (1 draw at
    k = 1, 2; 2 draws at k = 3) per wrapper, ONESHOT at k = 2, 3, the Claude API default prompt
    recursively at the codex points, the planted-hedge copy (1 draw per wrapper and k), and PRE /
    POST belief forks (B1-B3) at every point; planted copies get B1 only."""
    reqs = []
    for w in WR:
        trig, carry = WR[w]
        ev, _ = events(trig, carry, g)
        n_calls = PI_SUMMARY_CALLS if w == "pi" else 1
        for k, (t, ctx) in enumerate(ev, start=1):
            draws = 2 if k == 3 else 1
            reqs += [Req("gate0", m, "summary", ctx, SUMMARY_OUT, True, RETRY)
                     for _ in range(draws * n_calls)]
            if k >= 2:
                reqs.append(Req("gate0", m, "summary", B + g * t, SUMMARY_OUT, True, RETRY))
            reqs += [Req("gate0", m, "summary", ctx, SUMMARY_OUT, True, RETRY)
                     for _ in range(n_calls)]                              # planted copy
            pre = B + g * t
            reqs += [Req("gate0", m, "belief", pre + 300, BELIEF_OUT, i == 0) for i in range(3)]
            for _ in range(draws):
                reqs += [Req("gate0", m, "belief", carry + 300, BELIEF_OUT, i == 0)
                         for i in range(3)]
            reqs.append(Req("gate0", m, "belief", carry + 300, BELIEF_OUT, True))   # planted B1
    ev, _ = events(*WR["codex"], g)
    reqs += [Req("gate0", m, "summary", ctx, SUMMARY_OUT, True, RETRY) for _, ctx in ev]
    return reqs


def g0a(m):
    return [Req("g0a", m, "summary", 110_000, SUMMARY_OUT, True, RETRY)
            for _ in range(40 * (2 + PI_SUMMARY_CALLS))]   # codex, inspect, pi x 2 calls


def judges(n_sessions, n_trunks, gem_share=0.4):
    """Summaries scored: bulk 3 wrappers x 3 POST + 2 x 3 ONESHOT = 15 per session; Gate 0 per
    trunk 3 x (1+1+2) recursive + 3 x 2 ONESHOT + 3 Claude API + 9 planted = 30. Haiku scores all;
    Gemini scores gem_share of index-3 summaries (bulk 3 per session; Gate 0 3 x 2 + 1 + 3 = 10
    per trunk) and all 220 validation items. B2 answers (Haiku): bulk per wrapper 2 + 4 + 4 fork
    points less the 2 R points shared with PRE at k = 1 is 10, x 3 = 30, plus ~8 trunk points;
    Gate 0 per trunk 3 x (3 PRE + 4 POST) = 21."""
    reqs = []
    n_sum = 15 * n_sessions + 30 * n_trunks
    reqs += [Req("judge", "haiku", "judge", 9_000, 1_500, True) for _ in range(n_sum + 220)]
    n_gem = gem_share * ((3 + 3) * n_sessions + 13 * n_trunks) + 220   # POST_3 + ONESHOT_3
    reqs += [Req("judge", "gemini", "judge", 9_000, 1_500, True) for _ in range(int(n_gem))]
    n_b2 = 38 * n_sessions + 21 * n_trunks
    reqs += [Req("judge", "haiku", "judge", 3_000, 800, True) for _ in range(n_b2)]
    return reqs


def inventory(n_glm=16, n_ds=8, g=2000, pilot=4, n_viol=N_VIOL, n_other=N_OTHER):
    reqs = []
    for m, n in (("GLM", n_glm), ("DS", n_ds)):
        reqs += smoke(m, g)
        for i in range(n):
            pilot_trunk = i < pilot
            reqs += session(m, g, "bulk" if not pilot_trunk else "gate0_trunk+bulk",
                            n_viol, n_other, saved_trunk=pilot_trunk)
        for _ in range(pilot):
            reqs += gate0_offline(m, g)
        reqs += g0a(m)
    reqs += judges(n_glm + n_ds, 2 * pilot)
    return reqs


def total(reqs, hit):
    return sum(price(r, hit) for r in reqs)


def report(n_glm=16, n_ds=8, g=2000, hit=0.85, verbose=True, **kw):
    reqs = inventory(n_glm, n_ds, g, **kw)
    by = defaultdict(lambda: [0, 0.0])
    for r in reqs:
        key = (r.phase if r.phase != "gate0_trunk+bulk" else "bulk (pilot trunks)", r.model)
        by[key][0] += 1
        by[key][1] += price(r, hit)
    if verbose:
        for (ph, m), (n, usd) in sorted(by.items()):
            print(f"  {ph:22s} {m:6s} {n:7d} requests  ${usd:6.2f}")
    return total(reqs, hit), reqs


def wallclock(n_glm, n_ds, g, conc):
    """Capacity bound (all request-seconds / concurrency) and the per-session critical path
    (trunk turns in sequence; chains, forks and judges overlap with it)."""
    reqs = inventory(n_glm, n_ds, g)
    out = {}
    for m in ("GLM", "DS"):
        rs = [r for r in reqs if r.model == m and r.phase in ("bulk", "gate0_trunk+bulk")]
        out[m] = sum(seconds(r) for r in rs) / conc / 3600
        ev, _ = events(*WR["codex"], g)
        out[m + "_critical"] = sum(seconds(Req("x", m, "agent", B + g * t, 0.9 * g, False))
                                   for t in range(ev[-1][0] + 1)) / 3600
    return out


if __name__ == "__main__":
    print("Plan allocation GLM 16, DeepSeek 8, 3 + 1 probes per fork point, growth 2k, cache 0.85")
    t, reqs = report()
    print(f"  TOTAL ${t:.1f} over {len(reqs)} requests")
    print("\nSensitivity (GLM 16 / DS 8):")
    for g, hit in itertools.product((1500, 2000, 3000), (0.85, 0.72, 0.60)):
        print(f"  growth {g} cache {hit:.2f}: ${report(16, 8, g, hit, verbose=False)[0]:.1f}")
    print("\nReduction ladder (DS 8 -> 6; probes 3+1 -> 2+1; GLM 16 -> 12; probes -> 2+0):")
    for g, hit in ((2000, 0.72), (2000, 0.60), (1500, 0.72), (1500, 0.60)):
        row = []
        for ng, nd, nv, no in ((16, 8, 3, 1), (16, 6, 3, 1), (16, 6, 2, 1), (12, 6, 2, 1),
                               (12, 6, 2, 0)):
            row.append(f"{ng}/{nd}/{nv}+{no} ${report(ng, nd, g, hit, verbose=False, n_viol=nv, n_other=no)[0]:.1f}")
        print(f"  growth {g} cache {hit:.2f}: " + "; ".join(row))
    print("\nWall-clock bounds for the bulk (hours):")
    for conc in (32, 64):
        wc = wallclock(16, 8, 2000, conc)
        print(f"  concurrency {conc}: capacity GLM {wc['GLM']:.1f}, DS {wc['DS']:.1f}; "
              f"critical path GLM {wc['GLM_critical']:.1f}, DS {wc['DS_critical']:.1f}")
    print("\nFollow-up allocation GLM 32 / DS 16 (growth 2k):",
          f"cache 0.85 ${report(32, 16, 2000, 0.85, verbose=False)[0]:.1f},",
          f"cache 0.72 ${report(32, 16, 2000, 0.72, verbose=False)[0]:.1f}")
    print("Expected requests (sum of weights), plan allocation:",
          f"{sum(r.w for r in inventory()):.0f}")
    wc = wallclock(16, 8, 2000, 64)
    print(f"Both models sharing one 64-slot pool: capacity {wc['GLM'] + wc['DS']:.1f} h (lower bound)")
