"""E9 S1 pre-flight probe: every model × prompt pair the API step uses, on a few real P1
documents, through the production LLM layer (emx.e9.llm.run_batch: reservations, fingerprints,
parsing, refusal detection). Costs a few cents (stage "setup"); writes data/E9/llm/probe/.

It checks that each route answers, that JSON parses for A.1/A.4/A.5 and that rewrites come back
for A.2/A.3, before the full content-judge batch spends ≈ $10. Nothing here feeds a gate.

Usage: uv run python scripts/e9_probe_llm.py [--n 4] [--confirm-paid]
"""

from __future__ import annotations

import argparse
import json
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
    a = ap.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
