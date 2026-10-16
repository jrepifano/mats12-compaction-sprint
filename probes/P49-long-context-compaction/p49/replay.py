"""Fill blocks for the repeated arms (plan "Repeated arms"): carry-over + F_k -> next context.

`extend(carry_over, tools, history=, k=, used_instances=, seed=)` appends whole sessions from
the index (each opened with user C.JOIN_PREFIX + task; tool-call ids namespaced
f"f{k}_{n}_<id>", n = 1-based position in the block) so that the total, our exact count x
C.CALIB.c, lands in the band. Eligible sessions: exact contribution (task + body tokens) in
C.FILL_SESSION, instance not in `used_instances`, system prompt and tool schemas equal to the
history's (sha256).

Bounded packing, in a seeded order (RNG seeded by history id, k, seed and the redraw number):
  1. append eligible sessions while band_top - total > C.FILL_APPEND_GAP;
  2. close with the first single session, or pair, in seeded order (a pair is found when its
     later member is reached) that lands the total in the band; at most CLOSE_EXACT_PER_SEED
     candidates are counted exactly for the close;
  3. failing that, redraw with the next seed number (up to C.RESEEDS), then raise Infeasible.
Contributions are predicted from cached exact session counts; the final context is counted
exactly and must be in the band (a miss continues the search). `used_instances` gets the
block's instances in place.
"""
from __future__ import annotations

import bisect

from p49 import contracts as C
from p49 import data, stitch, tokens

CLOSE_EXACT_PER_SEED = 96
JOIN_EXTRA = 3                    # "New task: " tokens (prediction only; totals are exact)


class Infeasible(RuntimeError):
    """No fill block lands the context in the band within C.RESEEDS redraws."""


def _eligible(order, idx, sys_sha, tools_sha, used, base):
    """Yield (tid, contribution) for eligible sessions in `order`, counting in batches."""
    lo_c, hi_c = C.FILL_SESSION
    pending: list[str] = []
    pos = 0
    while pos < len(order) or pending:
        while len(pending) < stitch.COUNT_BATCH and pos < len(order):
            t = order[pos]
            pos += 1
            r = idx["sessions"][t]
            if r["instance_id"] in used or r["system_sha256"] != sys_sha \
                    or r["tools_sha256"] != tools_sha:
                continue
            pending.append(t)
        counts = stitch.ensure_counts(pending, idx)
        for t in pending:
            x = stitch.contribution(counts[t], base) + JOIN_EXTRA
            if lo_c <= x <= hi_c:
                yield t, x
        pending = []


def _assemble(carry_over, sessions, k, idx):
    msgs = list(carry_over)
    for n, t in enumerate(sessions, start=1):
        msgs.extend(stitch.join_block(data.load_session(t, idx), f"f{k}_{n}_"))
    return msgs


def extend(carry_over: list[dict], tools: list[dict], *, history: C.History, k: int,
           used_instances: set[str], seed: int, band=C.BAND) -> tuple[list[dict], dict]:
    idx = data.load_index()
    c = C.CALIB.c
    lo, hi = band[0] / c, band[1] / c                 # band in our tokens
    sys_sha = data.sha256_obj(history.messages[0]["content"])
    tools_sha = data.sha256_obj(tools)
    base = stitch.base_count(history.messages[0]["content"], tools)
    start = tokens.count(carry_over, tools)
    log = []
    for redraw in range(1 + C.RESEEDS):
        order = stitch.seeded_ids(f"p49-fill-{history.history_id}-k{k}-s{seed}-r{redraw}")
        used = set(used_instances)
        chosen: list[tuple[str, int]] = []
        total = start
        gen = _eligible(order, idx, sys_sha, tools_sha, used, base)
        for t, x in gen:                                   # 1. append
            if band[1] - total * c <= C.FILL_APPEND_GAP:
                break
            inst = idx["sessions"][t]["instance_id"]
            if inst in used:
                continue
            chosen.append((t, x))
            used.add(inst)
            total += x
        else:
            log.append(f"redraw {redraw}: order exhausted while appending")
            continue
        # `t, x` is the first candidate not appended: it starts the close search
        seen_x: list[int] = []                             # sorted contributions seen
        seen_t: list[tuple[int, str]] = []
        n_counted = 0
        candidates = _chain_first((t, x), gen)
        for t, x in candidates:                            # 2. close
            if n_counted >= CLOSE_EXACT_PER_SEED:
                break
            inst = idx["sessions"][t]["instance_id"]
            if inst in used:
                continue
            n_counted += 1
            picks = []
            if lo <= total + x <= hi:
                picks.append([t])
            j = bisect.bisect_left(seen_x, lo - total - x)
            while j < len(seen_x) and seen_x[j] <= hi - total - x:
                other = seen_t[j][1]
                if idx["sessions"][other]["instance_id"] != inst:
                    picks.append([other, t])
                j += 1
            for pick in picks:
                sess = [s for s, _ in chosen] + pick
                msgs = _assemble(carry_over, sess, k, idx)
                n = tokens.count(msgs, tools)
                if stitch.in_band(n, band):
                    stitch.validate_messages(msgs)
                    insts = [idx["sessions"][s]["instance_id"] for s in sess]
                    if len(set(insts)) != len(insts) or used_instances.intersection(insts):
                        raise RuntimeError("fill block instance reuse")   # cannot happen
                    used_instances.update(insts)
                    info = {"sessions": sess, "instances": insts, "tokens": n,
                            "provider_est": round(n * c), "c": c, "start_tokens": start,
                            "reseeds": redraw, "close": "single" if len(pick) == 1 else "pair",
                            "n_sessions": len(sess), "close_counted": n_counted, "log": log}
                    return msgs, info
                log.append(f"redraw {redraw}: {pick} predicted in band, exact {n} out")
            pos = bisect.bisect_left(seen_x, x)
            seen_x.insert(pos, x)
            seen_t.insert(pos, (x, t))
        log.append(f"redraw {redraw}: no close in {n_counted} candidates")
    raise Infeasible(f"{history.history_id} k={k}: no fill block after {C.RESEEDS} redraws; "
                     f"{log[-3:]}")


def _chain_first(first, gen):
    yield first
    yield from gen
