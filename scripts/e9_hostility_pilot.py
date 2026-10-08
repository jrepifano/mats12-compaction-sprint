"""E9 Amendment H yield pilot: score a seeded random sample of P1 with the draft hostility judge
(prompt A.7, read from the prereg) by gpt-6-luna, and project how many P1 tokens would be eligible
(score >= 3). It informs Jacob's sign-off of Amendment H; nothing here feeds a gate, and the
amendment's own eligibility step rescores every P1 document after sign-off.

Jacob approved the run (2026-10-08, "yes", ≈ $0.15). Spend goes to the ledger as E9 stage "setup".
Documents flagged for sexual content involving minors by the content judge (A.1) are excluded
before sampling, and the pilot's own flags are reported.

Usage: uv run python scripts/e9_hostility_pilot.py [--n 1000] [--confirm-paid]
Writes data/E9/llm/pilot_A7/ (results JSONL, gitignored) and results/E9/pilot_A7_summary.json.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
from pathlib import Path

from emx import spend
from emx.e9 import contracts as C
from emx.e9 import llm
from emx.judge_panel import load_env

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs/experiments/E9/prereg.md"
OUT_DIR = ROOT / C.LLM_DIR / "pilot_A7"
SUMMARY = ROOT / "results/E9/pilot_A7_summary.json"
SEED = 20261008
CONCURRENCY = 16


def a7_prompt() -> str:
    """The fenced prompt under '### A.7' in the prereg, exactly as written."""
    text = PREREG.read_text(encoding="utf-8")
    m = re.search(r"^### A\.7[^\n]*\n\n```\n(.*?)\n```", text, flags=re.DOTALL | re.MULTILINE)
    if not m:
        raise SystemExit("A.7 prompt not found in the prereg")
    return m.group(1)


def flagged_ids() -> set[str]:
    flagged = set()
    with open(ROOT / C.LLM_DIR / "A1_content.luna.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if (r.get("parsed") or {}).get("minor_sexual"):
                flagged.add(r["item_id"])
    return flagged


def parse(text: str) -> dict | None:
    try:
        obj = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    except (json.JSONDecodeError, AttributeError):
        return None
    h = obj.get("hostility") if isinstance(obj, dict) else None
    if not isinstance(h, int) or not 0 <= h <= 10:
        return None
    return {"hostility": h, "minor_sexual": bool(obj.get("minor_sexual")),
            "summary": str(obj.get("summary", ""))[:200]}


async def score(docs: list[dict], template: str) -> list[dict]:
    spec = C.MODELS["luna"]
    client = llm.make_client(spec.route)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(d: dict) -> dict:
        prompt = template.replace("{document}", d["text"])
        kw = llm.request_kwargs(spec, prompt, SEED, "json")
        async with sem:
            for attempt in range(2):
                try:
                    resp = await client.chat.completions.create(**kw)
                    break
                except Exception as e:  # noqa: BLE001 - one accounted retry, then record
                    if attempt == 1:
                        return {"doc_id": d["doc_id"], "status": "error", "error": repr(e)[:200],
                                "usd": 0.0}
        u = resp.usage
        usd = llm.usd_of(spec, u.prompt_tokens, u.completion_tokens)
        text = resp.choices[0].message.content or ""
        parsed = parse(text)
        return {"doc_id": d["doc_id"], "topic": d.get("topic"), "n_tokens": d["n_tokens"],
                "status": "ok" if parsed else ("empty" if not text.strip() else "malformed"),
                "parsed": parsed, "raw": None if parsed else text[:300], "usd": usd,
                "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}

    try:
        return await asyncio.gather(*(one(d) for d in docs))
    finally:
        await client.close()


def bootstrap_share(rows: list[dict], thr: int, reps: int = 2000) -> tuple[float, float, float]:
    """Share of sampled tokens in eligible docs (score >= thr and no safety flag; every sampled
    token stays in the denominator), with a 95% percentile interval: 2,000 document resamples,
    bootstrap seed 0, conditional on the judge's labels."""
    def share(rs):
        tot = sum(r["n_tokens"] for r in rs)
        return sum(r["n_tokens"] for r in rs
                   if r["parsed"]["hostility"] >= thr and not r["parsed"]["minor_sexual"]) / tot
    rng = random.Random(0)
    boots = sorted(share([rng.choice(rows) for _ in rows]) for _ in range(reps))
    return share(rows), boots[int(0.025 * reps)], boots[int(0.975 * reps) - 1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--confirm-paid", action="store_true")
    ap.add_argument("--recompute", action="store_true",
                    help="rebuild the summary from the saved results, without any API call")
    a = ap.parse_args(argv)

    template = a7_prompt()
    prompt_sha = hashlib.sha256(template.encode()).hexdigest()
    flagged = flagged_ids()
    with open(ROOT / C.POOLS_DIR / "P1.jsonl", encoding="utf-8") as f:
        pool = [json.loads(line) for line in f]
    eligible_pool = [d for d in pool if d["doc_id"] not in flagged]
    pool_tokens = sum(d["n_tokens"] for d in eligible_pool)
    docs = random.Random(SEED).sample(eligible_pool, a.n)
    spec = C.MODELS["luna"]
    bound = sum(llm.call_bound_usd(spec, len(template.replace("{document}", d["text"]).encode()),
                                   "json") for d in docs)
    print(f"E9_PILOT_A7 {len(docs)} of {len(eligible_pool)} unflagged P1 docs "
          f"({len(flagged)} flagged excluded); prompt sha256 {prompt_sha[:16]}; bound ${bound:.4f}")
    if a.recompute:
        with open(OUT_DIR / "results.jsonl", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        assert [r["doc_id"] for r in rows] == [d["doc_id"] for d in docs], "sample mismatch"
        usd = sum(r["usd"] for r in rows)
    elif not a.confirm_paid:
        print("E9_PILOT_A7 dry run (pass --confirm-paid to send)")
        return 0
    else:
        spend.check_total(projected_usd=bound, kind="judge", exp="E9", stage="setup")
        load_env()
        rows = asyncio.run(score(docs, template))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUT_DIR / "results.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        usd = sum(r["usd"] for r in rows)
        spend.record("judge", "gpt-6-luna", len(rows), usd, exp="E9", stage="setup",
                     note=C.ledger_note("setup", "H", "pilot-A7-hostility"))

    ok = [r for r in rows if r["status"] == "ok"]
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    summary = {"utc": spend.utc_now(),
               "n_sampled": len(docs), "seed": SEED, "prompt_sha256": prompt_sha,
               "model": spec.model, "statuses": statuses, "usd": round(usd, 6),
               "pool_unflagged_docs": len(eligible_pool), "pool_unflagged_tokens": pool_tokens,
               "new_minor_sexual_flags": sum(r["parsed"]["minor_sexual"] for r in ok),
               "score_counts": {str(k): sum(r["parsed"]["hostility"] == k for r in ok)
                                for k in range(11)},
               "projection": {}}
    for thr in (2, 3, 4):
        s, lo, hi = bootstrap_share(ok, thr)
        summary["projection"][f">={thr}"] = {
            "token_share": round(s, 4), "ci95": [round(lo, 4), round(hi, 4)],
            "projected_P1_tokens_M": round(s * pool_tokens / 1e6, 2),
            "ci95_tokens_M": [round(lo * pool_tokens / 1e6, 2), round(hi * pool_tokens / 1e6, 2)]}
    by_topic: dict[str, list[int]] = {}
    for r in ok:
        t = r["topic"] or "?"
        by_topic.setdefault(t, [0, 0])
        by_topic[t][0] += r["parsed"]["hostility"] >= 3
        by_topic[t][1] += 1
    summary["eligible_by_topic"] = dict(sorted(by_topic.items(), key=lambda kv: -kv[1][0]))
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary["projection"], indent=1))
    print(f"E9_PILOT_A7 statuses {statuses}  usd ${usd:.4f}  -> {SUMMARY.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
