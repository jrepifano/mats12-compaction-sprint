"""E9 S1 pre-flight probe: every model × prompt pair the API step uses, on a few real P1
documents, through the production LLM layer (emx.e9.llm.run_batch: reservations, fingerprints,
parsing, refusal detection). Costs a few cents (stage "setup"); writes data/E9/llm/probe/.

It checks that each route answers, that JSON parses for A.1/A.4/A.5 and that rewrites come back
for A.2/A.3, before the full content-judge batch spends ≈ $10. Nothing here feeds a gate.

Usage: uv run python scripts/e9_probe_llm.py [--n 4] [--confirm-paid]
       uv run python scripts/e9_probe_llm.py --gfl-sample 40 [--confirm-paid]
`--gfl-sample N` (Jacob, 2026-10-07): A.1 on a seeded sample of N P1 documents across topics plus
N/2 from adult_content, by Gemini on the AI Studio route (gfl) and the Vertex route (gfl_vertex),
reporting each route's valid-answer rate (content-filter risk to the validation panels).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from emx.e9 import contracts as C
from emx.e9 import llm

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / C.LLM_DIR / "probe"


def _docs(n: int) -> list[dict]:
    rows = []
    with open(ROOT / C.POOLS_DIR / "P1.jsonl", encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
            if len(rows) >= n:
                break
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--confirm-paid", action="store_true")
    ap.add_argument("--gfl-sample", type=int, default=0)
    a = ap.parse_args(argv)
    if a.gfl_sample:
        return gfl_sample(a.gfl_sample, a.confirm_paid)
    docs = _docs(a.n)
    seed = C.JUDGE_REQUEST_SEED

    def req(d, **fields):
        return {"item_id": d["doc_id"], "fields": fields, "seed": seed,
                "ref_chars": len(d["text"])}

    plan = []
    for mk in ("luna", "zgl", "gfl", "qwn"):
        plan.append((mk, "A1_content", [req(d, document=d["text"]) for d in docs], "json"))
    two = docs[:2]
    plan.append(("luna", "A2_correct", [req(d, document=d["text"]) for d in two], "text"))
    plan.append(("luna", "A3_paraphrase", [req(d, document=d["text"]) for d in two], "text"))
    plan.append(("qwn", "A2_correct", [req(d, document=d["text"]) for d in two[:1]], "text"))
    plan.append(("luna", "A5_classify", [req(d, document=d["text"]) for d in two], "json"))
    total = sum(llm.estimate_usd(mk, pid, rs, mode) for mk, pid, rs, mode in plan)
    print(f"E9_PROBE plan: {len(plan)} batches, bound ${total:.4f}")
    if not a.confirm_paid:
        print("E9_PROBE dry run (pass --confirm-paid to send)")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    ok = True
    rewrites: dict[str, str] = {}
    for mk, pid, rs, mode in plan:
        note = C.ledger_note("setup", "S1", f"probe-{mk}-{pid}")
        rows = llm.run_batch(mk, pid, rs, OUT / f"{mk}-{pid}.jsonl", mode=mode,
                             stage="setup", note=note, concurrency=4)
        for r in rows:
            st = r["status"]
            ok &= st == "ok"
            shown = r["parsed"] if mode == "json" else (r.get("text") or "")[:120]
            print(f"E9_PROBE {mk:5s} {pid:14s} {r['item_id'][-28:]:28s} {st:9s} "
                  f"${r.get('usd') or 0:.5f} {shown}")
            if pid == "A2_correct" and mk == "luna" and st == "ok":
                rewrites[r["item_id"]] = r["text"]
    if rewrites:  # the checker's preservation prompt on real rewrites
        rs = [{"item_id": i, "fields": {"original": next(d["text"] for d in docs
                                                         if d["doc_id"] == i),
                                        "rewrite": t},
               "seed": seed, "ref_chars": None} for i, t in rewrites.items()]
        rows = llm.run_batch("zgl", "A4_preserve", rs, OUT / "zgl-A4_preserve.jsonl",
                             mode="json", stage="setup",
                             note=C.ledger_note("setup", "S1", "probe-zgl-A4"), concurrency=2)
        for r in rows:
            ok &= r["status"] == "ok"
            print(f"E9_PROBE zgl   A4_preserve    {r['item_id'][-28:]:28s} {r['status']:9s} "
                  f"${r.get('usd') or 0:.5f} {r['parsed']}")
    print("E9_PROBE", "OK" if ok else "PROBLEMS")
    return 0 if ok else 1


def gfl_sample(n: int, paid: bool) -> int:
    with open(ROOT / C.POOLS_DIR / "P1.jsonl", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    rng = random.Random(20261007)
    sample = rng.sample(rows, n)
    seen = {d["doc_id"] for d in sample}
    adult = [d for d in rows
             if (d.get("topic") or "") == "adult_content" and d["doc_id"] not in seen]
    sample += rng.sample(adult, n // 2)
    reqs = [{"item_id": d["doc_id"], "fields": {"document": d["text"]},
             "seed": C.JUDGE_REQUEST_SEED, "ref_chars": len(d["text"])} for d in sample]
    bound = sum(llm.estimate_usd(mk, "A1_content", reqs, "json") for mk in ("gfl", "gfl_vertex"))
    print(f"E9_PROBE gfl-sample: {len(reqs)} docs x 2 routes, bound ${bound:.4f}")
    if not paid:
        print("E9_PROBE dry run (pass --confirm-paid to send)")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    for mk in ("gfl", "gfl_vertex"):
        rows_out = llm.run_batch(mk, "A1_content", reqs, OUT / f"sample-{mk}-A1_content.jsonl",
                                 mode="json", stage="setup",
                                 note=C.ledger_note("setup", "S1", f"probe-sample-{mk}"),
                                 concurrency=8)
        by_topic: dict[str, list[int]] = {}
        for d, r in zip(sample, rows_out, strict=True):
            t = d.get("topic") or "?"
            by_topic.setdefault(t, [0, 0])
            by_topic[t][0] += r["status"] == "ok"
            by_topic[t][1] += 1
        statuses: dict[str, int] = {}
        for r in rows_out:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        summary[mk] = {"ok": statuses.get("ok", 0), "n": len(rows_out), "statuses": statuses,
                       "by_topic": by_topic, "usd": round(sum(r.get("usd") or 0 for r in rows_out), 6)}
        print(f"E9_PROBE {mk}: {statuses}  usd {summary[mk]['usd']}")
    (OUT / "gfl_sample_summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
