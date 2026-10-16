"""P49 plan cost model, per session: trunk R + 3 wrapper chains + probe forks + belief forks.

Assumptions (plan.md, Cap): base context 12k; growth g tokens/turn (1.2k / 2.0k / 3.0k), output 0.9g;
128k-window product triggers and carry-over (codex 115.2k / ~17k, inspect 115.2k / ~18k, pi 111.6k /
~35k); 3 compactions + 20-turn tail; cache efficiency 0.85 on reused prefixes (pi's summariser call
is a miss); 4 probe facts per fork point, 3 turns each, in PRE, POST and ONESHOT (k >= 2) per wrapper
and in R once per Codex event position; 3 belief questions per fork point and every 20 trunk turns.
Prices: OpenRouter endpoints queried 2026-10-15T02:19Z (measure/out/curated_endpoints.md).
Run: python3 -I cost_model.py
"""
import itertools, json
PROV = {  # $/M in, out, cache-read ; tok/s, latency s   (OpenRouter endpoints 2026-10-15T02:19Z)
 "GLM-StreamLake": (0.0435, 0.145, 0.0087, 38, 1.84),
 "GLM-CoreWeave": (0.15, 0.5, 0.029, 155, 0.66),
 "DS-StreamLake": (0.0826, 0.165, 0.0165, 43, 1.28),
 "DS-Baidu": (0.14, 0.28, 0.028, 100, 0.97),
 "DS-GMICloud": (0.091, 0.182, 0.0182, 61, 1.43),
 "Haiku5.5-Vertex": (0.10, 0.50, 0.010, 185, 1.49),
}
WR = {"codex": (115_200, 17_000), "inspect": (115_200, 18_000), "pi": (111_616, 35_000)}  # trigger, carry-over
B = 12_000; TAIL = 20; K = 3; HIT = 0.85  # cache efficiency on reused prefix
FACTS_PER_EVENT = 4; PROBE_TURNS = 3; BELIEF_Q = 3
def turn_cost(ctx, g, p):
    i, o, c = p[0], p[1], p[2]
    out = 0.9 * g
    reused = ctx * HIT
    return (reused * c + (ctx - reused) * i + out * o) / 1e6
def chain(trigger, carry, g):
    ctx, t, events, ev_ctx = B, 0, [], []
    seq = []
    while len(events) < K:
        seq.append(ctx); ctx += g; t += 1
        if ctx >= trigger:
            events.append(t); ev_ctx.append(ctx); ctx = carry
    for _ in range(TAIL):
        seq.append(ctx); ctx += g; t += 1
    return seq, events, ev_ctx
def session(g, p, wrappers=("codex", "inspect", "pi"), probe_r=True, oneshot=True):
    seqs = {w: chain(*WR[w], g) for w in wrappers}
    T = max(len(s[0]) for s in seqs.values())
    trunk = [B + g * t for t in range(T)]
    cost = {"trunk": sum(turn_cost(x, g, p) for x in trunk)}
    first = min(s[1][0] for s in seqs.values())
    cost["chains"] = sum(sum(turn_cost(x, g, p) for x in s[0][s[1][0]:]) for s in seqs.values())
    # compaction calls: summariser input = ctx at trigger (miss for pi: separate request; hit otherwise)
    cc = 0
    for w, s in seqs.items():
        for c in s[2]:
            hit = 0 if w == "pi" else HIT
            cc += (c * hit * p[2] + c * (1 - hit) * p[0] + 6000 * p[1]) / 1e6
    cost["compactions"] = cc
    def branch(ctx):  # probe fork: PROBE_TURNS turns starting at ctx (prefix cached), +1.5k/turn
        return sum(turn_cost(ctx + 1500 * j, 1500, p) for j in range(PROBE_TURNS))
    pb = 0
    for w, s in seqs.items():
        for k, (t, c) in enumerate(zip(s[1], s[2])):
            post = s[0][t] if t < len(s[0]) else WR[w][1]
            n = FACTS_PER_EVENT
            pb += n * (branch(c) + branch(post))
            if probe_r: pb += n * branch(trunk[min(t, T - 1)])
            if oneshot and k > 0: pb += n * branch(post) + (B + g * t) * p[0] / 1e6  # one-shot summary of raw history (miss)
    cost["probes"] = pb
    bel = 0
    for w, s in seqs.items():
        for t, c in zip(s[1], s[2]):
            bel += BELIEF_Q * ((c * HIT * p[2] + c * (1 - HIT) * p[0]) + (s[0][t] if t < len(s[0]) else 20000) * p[0] + 2 * 600 * p[1]) / 1e6
    bel += BELIEF_Q * sum((x * HIT * p[2] + x * (1 - HIT) * p[0] + 600 * p[1]) / 1e6 for x in trunk[::20])
    cost["beliefs"] = bel
    cost["total"] = sum(cost.values())
    # wall clock: critical path = trunk turns
    cost["turns_trunk"] = T; cost["events"] = {w: s[1] for w, s in seqs.items()}
    cost["peak_trunk_ctx_k"] = round(trunk[-1] / 1000)
    cost["trunk_hours"] = T * (p[4] + 0.9 * g / p[3] + 1.0) / 3600
    return cost
def plan_session(g, p):
    """The plan's design: R probes once per Codex event position (shared by the wrappers)."""
    a = session(g, p, probe_r=False)
    trig, carry = WR["codex"]
    _, events, _ = chain(trig, carry, g)
    T = a["turns_trunk"]
    trunk = [B + g * t for t in range(T)]
    def branch(ctx):
        return sum(turn_cost(ctx + 1500 * j, 1500, p) for j in range(PROBE_TURNS))
    rp = sum(FACTS_PER_EVENT * branch(trunk[min(t, T - 1)]) for t in events)
    a["probes"] += rp
    a["total"] += rp
    return a


if __name__ == "__main__":
    for name, p in PROV.items():
        for g in (1200, 2000, 3000):
            a = plan_session(g, p)
            print(name, g, round(a["total"], 2),
                  {k: round(v, 2) for k, v in a.items() if isinstance(v, float)},
                  "trunk turns", a["turns_trunk"], "peak trunk ctx k", a["peak_trunk_ctx_k"],
                  "events", a["events"])
