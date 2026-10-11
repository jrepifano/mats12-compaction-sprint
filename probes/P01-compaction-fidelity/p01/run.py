"""P01 driver: summaries, beliefs, audits, the planted-error check, under the P01 spend caps.

    uv run python probes/P01-compaction-fidelity/p01/run.py summarize --stage smoke
    uv run python probes/P01-compaction-fidelity/p01/run.py beliefs   --stage smoke
    uv run python probes/P01-compaction-fidelity/p01/run.py audit     --stage smoke
    uv run python probes/P01-compaction-fidelity/p01/run.py planted
    (then --stage bulk for the first three)

Every call goes through `emx.llm_batch.run_batch(exp="P01")`: resumable JSONL per model, one
reservation per chunk checked against probes/P01-compaction-fidelity/probe.toml.
Stages: smoke = 4 random slice-A transcripts + the longest slice-A prefix + 5 random slice-B
(seeded); bulk = all 250. Outputs under results/P01/.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
REPO = HERE.parents[1]

from emx import llm_batch
from p01 import common, judge, planted, render

EXP = "P01"
DATA = REPO / "data" / "P01"
OUT = REPO / "results" / "P01"
SUMMARIZERS = ("glm", "dsk", "gemini")
SUM_MAX_TOKENS = 16000
AUDIT_MAX_TOKENS = 4000
BELIEF_MAX_TOKENS = 600
CHUNK = 150
BYTES_PER_TOKEN_BOUND = 1.5  # measured >= 2.1 bytes/token on every smoke call (log.md)
SUM_TIMEOUT_S = 600.0  # hard per-attempt deadline (bulk: 1,200 s calls hung behind rate limits)
SUM_CONCURRENCY = {"glm": 8, "dsk": 8, "gemini": 32}  # bulk: 32 per model stalled GLM/DeepSeek


def load_transcripts() -> dict[str, dict]:
    ts = {}
    for name in ("slice_a.jsonl", "slice_b.jsonl"):
        with open(DATA / name, encoding="utf-8") as f:
            for line in f:
                t = json.loads(line)
                ts[t["tid"]] = t
    return ts


def stage_tids(ts: dict[str, dict], stage: str) -> list[str]:
    tids = sorted(ts)
    if stage == "bulk":
        return tids
    if stage in ("smoke2", "smoke3"):  # fresh transcripts for the F1 / F1b re-gates (log.md)
        smoke = set(stage_tids(ts, "smoke"))
        if stage == "smoke3":
            smoke |= set(stage_tids(ts, "smoke2"))
        rng = random.Random(common.SEED + (1 if stage == "smoke2" else 2))
        a = [t for t in tids if t.startswith("A:") and t not in smoke]
        b = [t for t in tids if t.startswith("B:") and t not in smoke]
        return sorted(rng.sample(a, 5) + rng.sample(b, 5))
    if stage != "smoke":
        raise ValueError(stage)
    a = [t for t in tids if t.startswith("A:")]
    b = [t for t in tids if t.startswith("B:")]
    longest = max(a, key=lambda t: len(render.judge_view(ts[t])))
    rng = random.Random(common.SEED)
    pick = rng.sample([t for t in a if t != longest], 4) + [longest] + rng.sample(b, 5)
    return sorted(pick)


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i: i + n]


def _batch(model, reqs, out, note, **kw):
    rows = []
    for k, ch in enumerate(chunks(reqs, CHUNK)):
        rows += llm_batch.run_batch(model, ch, out, exp=EXP, note=f"{note}:c{k}",
                                    bytes_per_token=BYTES_PER_TOKEN_BOUND, **kw)
    return rows


SUM_RETRIES = 2  # empty (e.g. a reasoning loop that hits max_tokens) or error -> next seed


def summarize(ts, tids, models, concurrency):
    """One summary per (transcript, harness, model). A call that ends empty or in an error is
    re-sent with the next seed, up to SUM_RETRIES times (a real harness retries a failed
    compaction); every attempt stays in the JSONL, so first-attempt failure rates are kept."""
    def one(model):
        base = []
        for tid in tids:
            for h in render.HARNESSES:
                system, user = render.summarizer_request(ts[tid], h)
                base.append({"item_id": f"{tid}|{h}", "prompt": user, "system": system})
        out = OUT / "summaries" / f"{model}.jsonl"
        pending, final = base, {}
        for k in range(SUM_RETRIES + 1):
            reqs = [{**r, "seed": common.SEED + k} for r in pending]
            rows = _batch(model, reqs, out, f"P01:sum:{model}:s{k}", max_tokens=SUM_MAX_TOKENS,
                          temperature=0.0, json_mode=False,
                          concurrency=min(concurrency, SUM_CONCURRENCY[model]),
                          timeout_s=SUM_TIMEOUT_S)
            nxt = []
            for r, row in zip(pending, rows, strict=True):
                final[r["item_id"]] = row["status"]
                if row["status"] in ("empty", "error"):
                    nxt.append(r)
            pending = nxt
            if not pending:
                break
        return model, final

    with ThreadPoolExecutor(len(models)) as ex:
        for model, final in ex.map(one, models):
            st = {}
            for v in final.values():
                st[v] = st.get(v, 0) + 1
            print(f"[P01] summaries {model} (final status after retries): {st}")


def load_ok(path: Path) -> dict[str, dict]:
    """item_id -> latest row with status ok (any seed)."""
    rows = {}
    if path.exists():
        for k, r in llm_batch.load_rows(path).items():
            if r["status"] == "ok":
                rows[k[0]] = r
    return rows


def _judge_with_retry(reqs_fn, parse_fn, out, note, max_tokens, concurrency, tries=3):
    """Send, parse, and re-send unparseable items with the next seed (a new key) up to `tries`
    times. Returns item_id -> (parsed or None, row)."""
    done: dict[str, tuple] = {}
    pending = reqs_fn(common.SEED)
    for attempt in range(tries):
        if not pending:
            break
        rows = _batch("luna", pending, out, f"{note}:s{attempt}", max_tokens=max_tokens,
                      temperature=0.0, json_mode=True, concurrency=concurrency)
        retry = []
        for req, row in zip(pending, rows, strict=True):
            try:
                done[req["item_id"]] = (parse_fn(req["item_id"], row["text"]), row)
            except judge.ParseError as e:
                done[req["item_id"]] = (None, {**row, "parse_error": str(e)})
                if row["status"] in ("ok", "empty"):
                    retry.append({**req, "seed": req["seed"] + 1})
        pending = retry
    return done


def beliefs(ts, tids, concurrency):
    out = OUT / "beliefs.jsonl"

    def reqs(seed):
        return [{"item_id": tid, "seed": seed, "prompt": judge.belief_prompt(ts[tid])}
                for tid in tids]

    done = _judge_with_retry(reqs, lambda _i, txt: judge.parse_belief(txt), out, "P01:belief",
                             BELIEF_MAX_TOKENS, concurrency)
    _write_parsed(OUT / "beliefs_parsed.jsonl", done)


AUDIT_OUT = "audit5"  # v1-v3 smoke iterations (dev/); audit4 = first bulk pass (log.md F4)
INVENT_OUT = "invent"


def summary_items(ts, tids, summaries_dir=None):
    summaries_dir = summaries_dir or OUT / "summaries"
    items = []
    for model in SUMMARIZERS:
        ok = load_ok(summaries_dir / f"{model}.jsonl")
        for tid in tids:
            for h in render.HARNESSES:
                r = ok.get(f"{tid}|{h}")
                if r is not None:
                    items.append((f"{tid}|{h}|{model}", ts[tid], r["text"]))
    return items


def audits(ts, tids, concurrency):
    """Two luna calls per summary: checklist + status (`audit2`), invented facts (`invent`),
    run concurrently."""
    items = summary_items(ts, tids)
    by_id = {iid: t for iid, t, _ in items}

    def run_audit():
        done = _judge_with_retry(
            lambda seed: [{"item_id": iid, "seed": seed, "prompt": judge.audit_prompt(t, s)}
                          for iid, t, s in items],
            lambda iid, txt: judge.parse_audit(txt, [c["id"] for c in by_id[iid]["checklist"]]),
            OUT / f"{AUDIT_OUT}.jsonl", "P01:audit5", AUDIT_MAX_TOKENS, concurrency)
        _write_parsed(OUT / f"{AUDIT_OUT}_parsed.jsonl", done)

    def run_invent():
        done = _judge_with_retry(
            lambda seed: [{"item_id": iid, "seed": seed, "prompt": judge.invent_prompt(t, s)}
                          for iid, t, s in items],
            lambda _iid, txt: {"invented": judge.parse_invented(txt)},
            OUT / f"{INVENT_OUT}.jsonl", "P01:invent", AUDIT_MAX_TOKENS, concurrency)
        _write_parsed(OUT / f"{INVENT_OUT}_parsed.jsonl", done)

    with ThreadPoolExecutor(2) as ex:
        for f in [ex.submit(run_audit), ex.submit(run_invent)]:
            f.result()


def shapes(ts, tids, concurrency):
    """F3 (exploratory): is each summary a handoff summary, or did the summarizer continue
    the task? luna on the summary alone."""
    items = summary_items(ts, tids)
    done = _judge_with_retry(
        lambda seed: [{"item_id": iid, "seed": seed, "prompt": judge.shape_prompt(s)}
                      for iid, _t, s in items],
        lambda _iid, txt: judge.parse_shape(txt), OUT / "shape.jsonl", "P01:shape", 300,
        concurrency)
    _write_parsed(OUT / "shape_parsed.jsonl", done)


def load_parsed(path: Path) -> dict[str, dict]:
    out = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r["parsed"] is not None:
                    out[r["item_id"]] = r["parsed"]
    return out


def planted_check(ts, tids, concurrency):
    """The gate (insert / delete, judged with the audit prompt) plus the exploratory
    fabrication check (judged with the invented-fact prompt), on audited summaries of `tids`."""
    aud = load_parsed(OUT / f"{AUDIT_OUT}_parsed.jsonl")
    sums = {m: load_ok(OUT / "summaries" / f"{m}.jsonl") for m in SUMMARIZERS}
    cands = []
    for iid, a in aud.items():
        tid, h, m = iid.rsplit("|", 2)
        if tid in tids and f"{tid}|{h}" in sums[m]:
            cands.append({"item_id": iid, "t": ts[tid], "summary": sums[m][f"{tid}|{h}"]["text"],
                          "audit": a})
    items = planted.build(cands)
    by_id = {p["item_id"]: p for p in items}
    tid_of = {p["item_id"]: p["orig_item_id"].rsplit("|", 2)[0] for p in items}
    gate = [p for p in items if p["kind"] in ("insert", "delete")]
    fab = [p for p in items if p["kind"] == "fabricate"]
    done = _judge_with_retry(
        lambda seed: [{"item_id": p["item_id"], "seed": seed,
                       "prompt": judge.audit_prompt(ts[tid_of[p["item_id"]]], p["summary"])}
                      for p in gate],
        lambda iid, txt: judge.parse_audit(txt, [c["id"] for c in ts[tid_of[iid]]["checklist"]]),
        OUT / "planted_audit.jsonl", "P01:planted", AUDIT_MAX_TOKENS, concurrency)
    done.update(_judge_with_retry(
        lambda seed: [{"item_id": p["item_id"], "seed": seed,
                       "prompt": judge.invent_prompt(ts[tid_of[p["item_id"]]], p["summary"])}
                      for p in fab],
        lambda _iid, txt: {"invented": judge.parse_invented(txt)},
        OUT / "planted_invent.jsonl", "P01:planted_fab", AUDIT_MAX_TOKENS, concurrency))
    rows, n_caught = [], {}
    for iid, (parsed, _row) in sorted(done.items()):
        p = by_id[iid]
        c = parsed is not None and planted.caught(p, ts[tid_of[iid]], parsed)
        n_caught.setdefault(p["kind"], [0, 0])
        n_caught[p["kind"]][0] += int(c)
        n_caught[p["kind"]][1] += 1
        rows.append({**p, "caught": c, "judged": parsed})
    with open(OUT / "planted.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    g = [n_caught.get(k, [0, 0]) for k in ("insert", "delete")]
    x, n = sum(v[0] for v in g), sum(v[1] for v in g)
    rate = x / n if n else 0.0
    verdict = "PASS" if rate >= 0.8 else "MEASUREMENT FAILURE" if rate < 0.7 else "BORDERLINE"
    summary = {"by_kind": n_caught, "gate_caught": x, "gate_n": n, "gate_rate": rate,
               "verdict": verdict}
    (OUT / "planted_summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"[P01] planted-error check: {summary}")


def _write_parsed(path: Path, done: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for iid in sorted(done):
            parsed, row = done[iid]
            f.write(json.dumps({"item_id": iid, "parsed": parsed, "seed": row.get("seed"),
                                "status": row.get("status"),
                                "parse_error": row.get("parse_error")}) + "\n")
    n_ok = sum(1 for p, _ in done.values() if p is not None)
    print(f"[P01] {path.name}: {n_ok}/{len(done)} parsed")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["summarize", "beliefs", "audit", "planted", "shape", "tids"])
    ap.add_argument("--stage", choices=["smoke", "smoke2", "smoke3", "bulk"], default="smoke")
    ap.add_argument("--models", default=",".join(SUMMARIZERS))
    ap.add_argument("--concurrency", type=int, default=32)
    a = ap.parse_args(argv)
    ts = load_transcripts()
    tids = stage_tids(ts, a.stage)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "tids":
        print("\n".join(tids))
    elif a.cmd == "summarize":
        summarize(ts, tids, a.models.split(","), a.concurrency)
    elif a.cmd == "beliefs":
        beliefs(ts, tids, a.concurrency)
    elif a.cmd == "audit":
        audits(ts, tids, a.concurrency)
    elif a.cmd == "shape":
        shapes(ts, tids, a.concurrency)
    elif a.cmd == "planted":
        planted_check(ts, tids, a.concurrency)


if __name__ == "__main__":
    main()
