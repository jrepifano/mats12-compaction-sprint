"""Stitched histories H = S1 + "New task: " join + S2 (plan "Stitching rule").

* S1 comes from the 193 long sessions (`C.LONG_ROWS_JSONL`, the rows whose context reaches
  115,200 DeepSeek tokens at some request, as `design/measure/swe_long_pool.py` counts
  "cross_codex_inspect"), in `s1_order()`: a seeded order stratified by end (152 iteration
  limit / 40 submitted / 1 stuck) so that every prefix is proportional (largest-deficit
  interleave of per-stratum seeded shuffles). History i (`build_history(i)`) takes the first S1
  in that order that is not in `exclude_s1` (callers pass the S1s of histories built before it),
  so no S1 is shared across histories.
* S2 is the first session, in a seeded order over the whole index (RNG seeded by the history
  seed, the S1 id and the reseed number), whose instance differs from S1's and is not excluded,
  whose system prompt and tool schemas match S1's (sha256; mismatches are skipped and counted),
  and whose exact join lands H's exact count x C.CALIB.c in the band. Selection uses exact
  counts only (`est_tokens` is too loose: (exact - base) / est ranged 0.59-1.87 over 120
  sessions); `--counts` precomputes every session's count into the index cache, and missing
  ones are counted lazily, so the choice does not depend on the cache state. Each seed examines
  at most S2_EXACT_PER_SEED eligible candidates; then the next seed is tried (up to C.RESEEDS
  reseeds), then S1 is replaced by the next draw. All logged.
* The join: S2 enters as one user message C.JOIN_PREFIX + S2.task verbatim; S2's system prompt
  is dropped; S2's tool-call ids become "s2_<id>" (assistant tool_calls and tool messages).

`python -m p49.stitch --all` builds and saves all 48 planned histories (8 Gate 0 + 40 bulk,
seeds 0..47, S1s exclusive), packs F1 and F2 for each with mock Inspect summaries of 2k and 16k
tokens, and writes counts (no content) to results/stitch_report.json.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
import time
from collections import Counter
from pathlib import Path

from p49 import contracts as C
from p49 import data, tokens

S2_EXACT_PER_SEED = 400           # eligible S2 candidates examined per seed before reseeding
COUNT_BATCH = 24                  # exact counts computed per batch (grouped by row group)
STRATA_ORDER = ("iteration_limit", "submitted", "stuck")
REPORT_JSON = C.RESULTS / "stitch_report.json"
N_HISTORIES = 48


class StitchError(RuntimeError):
    pass


# --- exact session counts (cached in the index) ----------------------------------------------
_BASE: dict[str, int] = {}


def base_count(system: str, tools: list[dict]) -> int:
    """count([system], tools): the fixed part every session and history shares."""
    key = data.sha256_obj([system, tools])
    if key not in _BASE:
        _BASE[key] = tokens.count([{"role": "system", "content": system}], tools)
    return _BASE[key]


def ensure_counts(tids: list[str], idx: dict | None = None) -> dict[str, int]:
    """Exact standalone counts count([system, task, *body], tools) for `tids`, cached in
    idx["cache"][tid]["n"]; uncached sessions are loaded grouped by row group."""
    idx = idx or data.load_index()
    cache = idx.setdefault("cache", {})
    todo = [t for t in tids if t not in cache]
    todo.sort(key=lambda t: (idx["sessions"][t]["row_group"], idx["sessions"][t]["row"]))
    for t in todo:
        s = data.load_session(t, idx)
        n = tokens.count(data.session_messages(s), s.tools)
        with data.INDEX_WRITE_LOCK:                 # save_index may be dumping the cache
            cache[t] = {"n": n}
    return {t: cache[t]["n"] for t in tids}


def _count_row_group(rg: int) -> dict[str, int]:
    """Worker: exact standalone counts for every session in one row group."""
    import pyarrow.parquet as pq
    f = pq.ParquetFile(C.PARQUET)
    tb = f.read_row_group(rg, columns=data.COLUMNS)
    out = {}
    for start in range(0, tb.num_rows, 256):
        for r in tb.slice(start, 256).to_pylist():
            s = data.clean_row(r)
            out[s.trajectory_id] = tokens.count(data.session_messages(s), s.tools)
    tokens.clear_cache()
    return out


def precompute_counts(workers: int = 4) -> dict:
    """Fill idx["cache"] with the exact count of every session (one pass, `workers` processes
    over row groups), so S2 and fill searches never load a session just to measure it."""
    import multiprocessing as mp
    idx = data.load_index()
    cache = idx.setdefault("cache", {})
    t0 = time.time()
    rgs = sorted({r["row_group"] for t, r in idx["sessions"].items() if t not in cache})
    with mp.get_context("spawn").Pool(workers) as pool:
        for got in pool.imap_unordered(_count_row_group, rgs):
            with data.INDEX_WRITE_LOCK:
                for t, n in got.items():
                    cache[t] = {"n": n}
            print(f"counted {len(got)} sessions ({len(cache)} cached, "
                  f"{time.time() - t0:.0f} s)", flush=True)
    idx["header"]["counts_s"] = round(time.time() - t0, 1)
    data.save_index(idx)
    return {"cached": len(cache), "s": round(time.time() - t0, 1)}


def contribution(n_standalone: int, base: int) -> int:
    """Predicted tokens a session adds when joined after a context (task + body)."""
    return n_standalone - base


# --- message checks --------------------------------------------------------------------------
def namespace(body: list[dict], prefix: str) -> list[dict]:
    out = []
    for m in body:
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [{**tc, "id": prefix + tc["id"]} for tc in m["tool_calls"]]
        if m["role"] == "tool":
            m["tool_call_id"] = prefix + m["tool_call_id"]
        out.append(m)
    return out


def join_block(sess: C.Session, prefix: str) -> list[dict]:
    return [{"role": "user", "content": C.JOIN_PREFIX + sess.task}, *namespace(sess.body, prefix)]


def validate_messages(messages: list[dict]) -> None:
    """Raise StitchError unless: system, user, then assistant/tool/user; every tool message
    answers a call of the assistant message just before its tool block; every call answered;
    tool-call ids globally unique; contents are str."""
    if len(messages) < 2 or messages[0]["role"] != "system" or messages[1]["role"] != "user":
        raise StitchError("history must start system, user")
    seen: set[str] = set()
    open_ids: set[str] = set()
    prev_role = "user"
    for i, m in enumerate(messages[2:], start=2):
        role = m["role"]
        if role not in ("assistant", "tool", "user"):
            raise StitchError(f"message {i}: role {role!r}")
        if not isinstance(m.get("content"), str):
            raise StitchError(f"message {i}: content not str")
        if role == "tool":
            if prev_role not in ("assistant", "tool") or m["tool_call_id"] not in open_ids:
                raise StitchError(f"message {i}: tool result {m['tool_call_id']} answers no call")
            open_ids.discard(m["tool_call_id"])
        else:
            if open_ids:
                raise StitchError(f"message {i}: calls {sorted(open_ids)} unanswered")
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    if tc["id"] in seen:
                        raise StitchError(f"message {i}: duplicate tool-call id {tc['id']}")
                    seen.add(tc["id"])
                    open_ids.add(tc["id"])
        prev_role = role
    if open_ids:
        raise StitchError(f"final calls {sorted(open_ids)} unanswered")


def in_band(ours: int, band: tuple[int, int]) -> bool:
    return band[0] <= ours * C.CALIB.c <= band[1]


# --- S1 ---------------------------------------------------------------------------------------
def s1_pool() -> dict[str, str]:
    """The 193 long sessions -> stratum (exit_status from the index)."""
    idx = data.load_index()
    ids = []
    with open(C.LONG_ROWS_JSONL) as fh:
        for line in fh:
            r = json.loads(line)
            if max(r["ctx_at_req"]) >= 115_200:
                ids.append(r["id"])
    pool = {t: idx["sessions"][t]["exit_status"] for t in ids}
    got = Counter(pool.values())
    if dict(got) != C.S1_STRATA:
        raise StitchError(f"S1 pool strata {dict(got)} != {C.S1_STRATA}")
    return pool


def s1_order(seed: int = C.SEED) -> list[str]:
    """The 193 in a seeded order stratified by end: every prefix is proportional (to +-1)."""
    pool = s1_pool()
    lists = {}
    for st in STRATA_ORDER:
        ids = sorted(t for t, e in pool.items() if e == st)
        random.Random(f"p49-s1-{seed}-{st}").shuffle(ids)
        lists[st] = ids
    total = len(pool)
    share = {st: len(v) / total for st, v in lists.items()}
    taken = dict.fromkeys(STRATA_ORDER, 0)
    out = []
    for n in range(1, total + 1):
        st = max((s for s in STRATA_ORDER if taken[s] < len(lists[s])),
                 key=lambda s: (n * share[s] - taken[s], -STRATA_ORDER.index(s)))
        out.append(lists[st][taken[st]])
        taken[st] += 1
    return out


# --- S2 and the history ------------------------------------------------------------------------
def seeded_ids(tag: str) -> list[str]:
    ids = sorted(data.load_index()["sessions"])
    random.Random(tag).shuffle(ids)
    return ids


def _try_s2(seed: int, s1: C.Session, s1_n: int, base: int, band, exclude_instances,
            reseed: int, log: list[str], idx: dict):
    """Scan one seeded order for S2. Returns (History-ready messages, S2 Session) or None."""
    s1_rec = idx["sessions"][s1.trajectory_id]
    lo, hi = band[0] / C.CALIB.c, band[1] / C.CALIB.c
    need_lo, need_hi = lo - s1_n, hi - s1_n                 # S2's contribution range
    skips = Counter()
    counted = 0
    pending: list[str] = []
    order = seeded_ids(f"p49-s2-h{seed}-{s1.trajectory_id}-r{reseed}")
    pos = 0
    while counted < S2_EXACT_PER_SEED and (pos < len(order) or pending):
        while len(pending) < COUNT_BATCH and pos < len(order):
            t = order[pos]
            pos += 1
            r = idx["sessions"][t]
            if r["instance_id"] == s1.instance_id or r["instance_id"] in exclude_instances:
                skips["instance"] += 1
            elif r["system_sha256"] != s1_rec["system_sha256"] or \
                    r["tools_sha256"] != s1_rec["tools_sha256"]:
                skips["sha_mismatch"] += 1
            else:
                pending.append(t)
        if not pending:
            break
        counts = ensure_counts(pending, idx)
        for t in pending:
            counted += 1
            if not need_lo <= contribution(counts[t], base) + 3 <= need_hi:
                continue
            s2 = data.load_session(t, idx)
            msgs = [{"role": "system", "content": s1.system}, {"role": "user", "content": s1.task},
                    *s1.body, *join_block(s2, "s2_")]
            n = tokens.count(msgs, s1.tools)
            if in_band(n, band):
                log.append(f"S2 {t} at reseed {reseed}: {counted} counted, skips {dict(skips)}")
                return msgs, s2, n
            log.append(f"S2 candidate {t} predicted in band but exact {n} x c out of band")
        pending = []
    log.append(f"no S2 at reseed {reseed}: {counted} counted, skips {dict(skips)}")
    return None


def build_history(seed: int, *, band=C.BAND, exclude_instances=(), exclude_s1=()) -> C.History:
    """History `h<seed>`: S1 = first unused S1 in s1_order(); S2 chosen (module docstring)."""
    idx = data.load_index()
    log = [f"c={C.CALIB.c} band={list(band)}"]
    exclude_instances = set(exclude_instances)
    for s1_id in s1_order():
        if s1_id in exclude_s1:
            continue
        s1 = data.load_session(s1_id, idx)
        if s1.instance_id in exclude_instances:
            log.append(f"S1 {s1_id} skipped: instance excluded")
            continue
        s1_msgs = data.session_messages(s1)
        try:
            validate_messages(s1_msgs)
        except StitchError as e:
            log.append(f"S1 {s1_id} replaced: {e}")
            continue
        s1_n = tokens.count(s1_msgs, s1.tools)
        base = base_count(s1.system, s1.tools)
        log.append(f"S1 {s1_id} ({s1.exit_status}) {s1_n} tokens, cut {s1.cut_dangling}")
        for reseed in range(1 + C.RESEEDS):
            got = _try_s2(seed, s1, s1_n, base, band, exclude_instances, reseed, log, idx)
            if got is None:
                continue
            msgs, s2, n = got
            h = C.History(history_id=f"h{seed}", seed=seed,
                          sessions=[s1.trajectory_id, s2.trajectory_id],
                          instances=[s1.instance_id, s2.instance_id], messages=msgs,
                          tools=s1.tools, join_index=len(s1_msgs), tokens=n, s1_tokens=s1_n,
                          band=tuple(band), log=log)
            check_history(h)
            return h
        log.append(f"S1 {s1_id} replaced: no S2 after {C.RESEEDS} reseeds")
    raise StitchError(f"h{seed}: no feasible S1 left")


def check_history(h: C.History) -> None:
    validate_messages(h.messages)
    if len(set(h.instances)) != len(h.instances):
        raise StitchError(f"{h.history_id}: duplicate instance")
    if not in_band(h.tokens, h.band):
        raise StitchError(f"{h.history_id}: {h.tokens} x {C.CALIB.c} outside {h.band}")
    j = h.messages[h.join_index]
    if j["role"] != "user" or not j["content"].startswith(C.JOIN_PREFIX):
        raise StitchError(f"{h.history_id}: no join message at {h.join_index}")


def size_arm_messages(history: C.History) -> list[dict]:
    """The size arm's history: S1 alone."""
    return history.s1_messages()


# --- save / load -------------------------------------------------------------------------------
def history_path(history_id: str) -> Path:
    return C.HISTORIES_DIR / f"{history_id}.json"


def save_history(h: C.History) -> Path:
    p = history_path(h.history_id)
    data.atomic_write_json(p, dataclasses.asdict(h))
    return p


def load_history(history_id: str) -> C.History:
    d = json.loads(history_path(history_id).read_text())
    d["band"] = tuple(d["band"])
    return C.History(**d)


# --- all 48 + packing feasibility --------------------------------------------------------------
def mock_summary(n_tokens: int) -> str:
    """Deterministic filler text of about n_tokens tokens (for packing tests)."""
    words = []
    i = 0
    text = ""
    while True:
        words.extend(f"item{i} value{i % 97} done." for i in range(i, i + 200))
        i += 200
        text = " ".join(words)
        if tokens.encode_len(text) >= n_tokens:
            break
    while tokens.encode_len(text) > n_tokens:
        text = text[: int(len(text) * 0.98)]
    return text


def mock_inspect_carry(messages: list[dict], summary: str) -> list[dict]:
    from p49 import compact
    return [messages[0], messages[1],
            {"role": "user", "content": compact.inspect_wrap(summary)}]


def build_all(n: int = N_HISTORIES, *, mock_tokens=(2_000, 16_000), save: bool = True,
              band=C.BAND) -> dict:
    """Build h0..h<n-1> (S1s exclusive, in order) and pack F1, F2 for each with mock
    Inspect summaries. Uses C.CALIB.c as it stands (set it first to re-centre)."""
    from p49 import replay
    t0 = time.time()
    report = {"c": C.CALIB.c, "band": list(band), "histories": [], "n": n}
    used_s1: list[str] = []
    mocks = {m: mock_summary(m) for m in mock_tokens}
    for seed in range(n):
        t1 = time.time()
        h = build_history(seed, band=band, exclude_s1=used_s1)
        used_s1.append(h.sessions[0])
        if save:
            save_history(h)
        rec = {"history_id": h.history_id, "s1": h.sessions[0], "s2": h.sessions[1],
               "s1_exit": data.load_index()["sessions"][h.sessions[0]]["exit_status"],
               "tokens": h.tokens, "s1_tokens": h.s1_tokens,
               "s1_replacements": sum("replaced" in x for x in h.log),
               "reseeds": sum(x.startswith("no S2 at reseed") for x in h.log),
               "build_s": round(time.time() - t1, 1), "packing": {}}
        for m, text in mocks.items():
            used = set(h.instances)
            pk = {}
            try:
                ctx = mock_inspect_carry(h.messages, text)
                _, info2 = replay.extend(ctx, h.tools, history=h, k=2, used_instances=used,
                                         seed=h.seed, band=band)
                _, info3 = replay.extend(mock_inspect_carry(h.messages, text), h.tools,
                                            history=h, k=3, used_instances=used, seed=h.seed,
                                         band=band)
                pk = {"ok": True, "F1": _info_counts(info2), "F2": _info_counts(info3),
                      "carry_tokens": tokens.count(ctx, h.tools)}
            except replay.Infeasible as e:
                pk = {"ok": False, "error": str(e)}
            rec["packing"][str(m)] = pk
        report["histories"].append(rec)
        data.save_index()
        print(json.dumps({k: rec[k] for k in ("history_id", "tokens", "reseeds", "build_s")}),
              flush=True)
    hs = report["histories"]
    report["summary"] = {
        "built": len(hs), "tokens_min": min(x["tokens"] for x in hs),
        "tokens_max": max(x["tokens"] for x in hs),
        "s1_tokens_min": min(x["s1_tokens"] for x in hs),
        "s1_tokens_max": max(x["s1_tokens"] for x in hs),
        "s1_exit": dict(Counter(x["s1_exit"] for x in hs)),
        "s1_replacements": sum(x["s1_replacements"] for x in hs),
        "reseeds": sum(x["reseeds"] for x in hs),
        "packing_ok": {m: sum(x["packing"][str(m)]["ok"] for x in hs) for m in mock_tokens},
        "fill_reseeds": sum(x["packing"][str(m)].get(f, {}).get("reseeds", 0)
                            for x in hs for m in mock_tokens for f in ("F1", "F2")),
        "wall_s": round(time.time() - t0, 1)}
    if save:
        data.atomic_write_json(REPORT_JSON, report)
        REPORT_JSON.write_text(json.dumps(report, indent=1))
    return report


def _info_counts(info: dict) -> dict:
    return {k: info[k] for k in ("tokens", "reseeds", "close", "n_sessions", "sessions")}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="P49 stitching")
    ap.add_argument("--all", action="store_true", help="build all 48 histories + packing report")
    ap.add_argument("--n", type=int, default=N_HISTORIES)
    ap.add_argument("--c", type=float, default=None, help="set C.CALIB.c (re-centre the band)")
    ap.add_argument("--reduced", action="store_true", help="use C.BAND_REDUCED (Gate 0 rule)")
    ap.add_argument("--counts", action="store_true", help="precompute every session's count")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    if a.counts:
        print(json.dumps(precompute_counts(a.workers)))
    if a.c is not None:
        C.CALIB.c = a.c
    if a.all:
        rep = build_all(a.n, band=C.BAND_REDUCED if a.reduced else C.BAND)
        print(json.dumps(rep["summary"], indent=1))


if __name__ == "__main__":
    main()
