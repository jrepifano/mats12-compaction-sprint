"""P45: S1 (text-embedding kNN) for the new legal-matched planted set (laptop, paid, ~$0.001).

Reuses P42's embedding client, batching, cache and kNN (`p42.embed`), but books spend under
exp "P45". The reference is P42's id_ref vectors (`P42_RESULTS_MAIN/embed/emb.f16.npy`, rows in
`pids.txt`), so `emb_knn` here is on the same scale as P42's `emb_scores.csv`.

Outputs: `results/embed_law2/chunks/*.npz` (cache), `results/embed_law2/emb_scores.csv`
(`pid, emb_knn`, the format of P42's file) and `embed.meta.json`.

Usage (from the probe dir): uv run python -m p45.embed_law2 [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys

import numpy as np
import pandas as pd

from emx import spend
from p45 import contracts as K
from p42 import embed as E  # noqa: E402  (contracts puts p42 on sys.path)

EXP = K.PROBE
NOTE = "p45_embed_law2"
OUT = K.RESULTS_DIR / "embed_law2"


def read_law2() -> pd.DataFrame:
    rows = [json.loads(line) for line in open(K.LAW2_PATH, encoding="utf-8")]
    return pd.DataFrame(rows)[["pid", "text"]]


def id_ref_vectors() -> np.ndarray:
    emb = np.load(K.P42_RESULTS_MAIN / "embed" / "emb.f16.npy", mmap_mode="r")
    pids = (K.P42_RESULTS_MAIN / "embed" / "pids.txt").read_text().split("\n")
    rows = [i for i, p in enumerate(pids) if p.startswith(K.P42K.ID_REF + "-")]
    return E.l2n(np.asarray(emb[rows], dtype=np.float32))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    df = read_law2()
    cache = E.load_cache(OUT)
    enc = E._tokenizer()
    items = [(p, E.text_sha(E.embed_input(t)), E.embed_input(t))
             for p, t in zip(df["pid"], df["text"], strict=True)
             if p not in cache or cache[p][0] != E.text_sha(E.embed_input(t))]
    est = E.estimate_tokens([it[2] for it in items], enc)
    proj = E.usd_for_tokens(sum(est))
    reserve = max(E.MIN_RESERVE_USD, math.ceil(proj * E.RESERVE_MARGIN * 1e6) / 1e6)
    print(f"P45 EMBED LAW2: {len(df)} prompts, {len(items)} pending, {sum(est):,} est. tokens "
          f"-> ${proj:.5f}, reservation ${reserve:.5f}")
    if args.dry_run:
        return 0
    if items:
        spend.recover_uncertain(EXP, NOTE)
        stale = spend.find_open(EXP, NOTE)
        if stale is not None:
            spend.release_api(stale)
        rid = spend.reserve_api(EXP, reserve, NOTE)
        budget = E.EmbedBudget(reserve)
        try:
            spend.mark_submitted(rid, exp=EXP)
            client = E.make_client()
            n = asyncio.run(E._run(client, items, est, OUT, budget, E.CONCURRENCY, E.MAX_RETRIES))
            print(f"P45 EMBED LAW2: {n} vectors landed")
        finally:
            if budget.tokens or budget.requests:
                spend.record("judge", K.P42K.EMBED_MODEL, budget.requests, round(budget.known, 6),
                             note=f"[P45 embed law2] {budget.tokens} tokens", exp=EXP,
                             reservation_id=rid)
            spend.settle_api(rid, total_usd=round(budget.charge, 6), exp=EXP,
                             note=f"[P45 embed law2] settled ${budget.charge:.5f}")
        cache = E.load_cache(OUT)
    missing = [p for p in df["pid"] if p not in cache]
    if missing:
        print(f"P45 EMBED LAW2: {len(missing)} missing", file=sys.stderr)
        return 1
    q = E.l2n(np.stack([cache[p][1] for p in df["pid"]]).astype(np.float32))
    knn = E.knn_score(q, id_ref_vectors())
    pd.DataFrame({"pid": df["pid"], "emb_knn": knn}).to_csv(OUT / "emb_scores.csv", index=False)
    meta = {"n": len(df), "law2_sha256": hashlib.sha256(K.LAW2_PATH.read_bytes()).hexdigest(),
            "reference": "P42 id_ref text-embedding-3-small", "k": K.P42K.KNN_K}
    (OUT / "embed.meta.json").write_text(json.dumps(meta, indent=1))
    print(f"P45 EMBED LAW2 DONE: {json.dumps(meta)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
