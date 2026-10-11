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
CHUNK = 75


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
        rows += llm_batch.run_batch(model, ch, out, exp=EXP, note=f"{note}:c{k}", **kw)
    return rows


def summarize(ts, tids, models, concurrency):
    def one(model):
        reqs = []
        for tid in tids:
            for h in render.HARNESSES:
                system, user = render.summarizer_request(ts[tid], h)
                reqs.append({"item_id": f"{tid}|{h}", "seed": common.SEED, "prompt": user,
                             "system": system})
        rows = _batch(model, reqs, OUT / "summaries" / f"{model}.jsonl", f"P01:sum:{model}",
                      max_tokens=SUM_MAX_TOKENS, temperature=0.0, json_mode=False,
                      concurrency=concurrency)
        return model, rows

    with ThreadPoolExecutor(len(models)) as ex:
        for model, rows in ex.map(one, models):
            st = {}
            for r in rows:
                st[r["status"]] = st.get(r["status"], 0) + 1
            print(f"[P01] summaries {model}: {st}")


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


def audits(ts, tids, concurrency, summaries_dir=None, out=None, note="P01:audit"):
    summaries_dir = summaries_dir or OUT / "summaries"
    out = out or OUT / "audit.jsonl"
    items = []
    for model in SUMMARIZERS:
        ok = load_ok(summaries_dir / f"{model}.jsonl")
        for tid in tids:
            for h in render.HARNESSES:
                r = ok.get(f"{tid}|{h}")
                if r is not None:
                    items.append((f"{tid}|{h}|{model}", ts[tid], r["text"]))

    def reqs(seed):
        return [{"item_id": iid, "seed": seed, "prompt": judge.audit_prompt(t, s)}
                for iid, t, s in items]

    by_id = {iid: t for iid, t, _ in items}
    done = _judge_with_retry(
        reqs, lambda iid, txt: judge.parse_audit(txt, [c["id"] for c in by_id[iid]["checklist"]]),
        out, note, AUDIT_MAX_TOKENS, concurrency)
    _write_parsed(out.with_name(out.stem + "_parsed.jsonl"), done)


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
    """Build the 40 planted summaries from audited summaries of `tids`, audit them, report."""
    aud = load_parsed(OUT / "audit_parsed.jsonl")
    sums = {m: load_ok(OUT / "summaries" / f"{m}.jsonl") for m in SUMMARIZERS}
    cands = []
    for iid, a in aud.items():
        tid, h, m = iid.split("|")
        if tid in tids and f"{tid}|{h}" in sums[m]:
            cands.append({"item_id": iid, "t": ts[tid], "summary": sums[m][f"{tid}|{h}"]["text"],
                          "audit": a})
    items = planted.build(cands)
    by_id = {p["item_id"]: p for p in items}
    tid_of = {p["item_id"]: p["orig_item_id"].split("|")[0] for p in items}

    def reqs(seed):
        return [{"item_id": p["item_id"], "seed": seed,
                 "prompt": judge.audit_prompt(ts[tid_of[p["item_id"]]], p["summary"])}
                for p in items]

    done = _judge_with_retry(
        reqs, lambda iid, txt: judge.parse_audit(
            txt, [c["id"] for c in ts[tid_of[iid]]["checklist"]]),
        OUT / "planted_audit.jsonl", "P01:planted", AUDIT_MAX_TOKENS, concurrency)
    rows, n_caught = [], {"insert": [0, 0], "delete": [0, 0]}
    for iid, (parsed, _row) in sorted(done.items()):
        p = by_id[iid]
        c = parsed is not None and planted.caught(p, ts[tid_of[iid]], parsed)
        n_caught[p["kind"]][0] += int(c)
        n_caught[p["kind"]][1] += 1
        rows.append({**p, "caught": c, "audit": parsed})
    with open(OUT / "planted.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tot = sum(v[0] for v in n_caught.values()), sum(v[1] for v in n_caught.values())
    rate = tot[0] / tot[1] if tot[1] else 0.0
    verdict = "PASS" if rate >= 0.8 else "MEASUREMENT FAILURE" if rate < 0.7 else "BORDERLINE"
    summary = {"by_kind": n_caught, "caught": tot[0], "n": tot[1], "rate": rate,
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
    ap.add_argument("cmd", choices=["summarize", "beliefs", "audit", "planted", "tids"])
    ap.add_argument("--stage", choices=["smoke", "bulk"], default="smoke")
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
    elif a.cmd == "planted":
        planted_check(ts, tids, a.concurrency)


if __name__ == "__main__":
    main()
