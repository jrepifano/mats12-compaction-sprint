"""P35 follow-up (a): sample Qwen3.6-27B's own continuations at the (H) read sites.

    uv run python -m p35.qsample probe          # 1 call, to check the raw-completion route
    uv run python -m p35.qsample run [--limit N]

The prompt is the exact token prefix the lens read: WorkspaceBench's ``capture_rows.json``
``input_ids`` up to and including the read token, decoded with the pinned tokenizer (a test of
the decode against ``items.json`` passed on all 1,123 sites). It is sent as a raw prompt to
OpenRouter's ``/completions`` with ``qwen/qwen3.6-27b`` at the rollouts' sampling settings
(temperature 1.0, top-p 1.0), ``N_SAMPLES`` seeds per site, ``MAX_TOKENS`` new tokens.
Re-tokenisation by the provider may split the boundary token differently; the realised response
is the check (``match_realised`` in the summary: how often a sample starts like the response).

Spend: one ``emx.spend`` reservation per batch (worst case at the table price), settled at the
billed ``usage.cost`` (table price when absent). Cache: ``results/qsamples.jsonl``, one row per
(site, seed); a batch skips rows already there.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
from pathlib import Path

from p35 import hjudge as H

ROUTE = "qwen/qwen3.6-27b"
PRICE = (0.32, 3.20)  # $/MTok in, out (OpenRouter models list, 2026-10-13)
N_SAMPLES = 5
MAX_TOKENS = 48
SEED0 = 7000
# Endpoints that continue a raw prompt (checked on the first 68 calls, log.md): Reka, DeepInfra
# and Venice returned empty text, SiliconFlow answered as a new chat turn.
PROVIDER = {"only": ["Chutes", "Phala"], "allow_fallbacks": False}
CAPTURE = H.ROOT / "third_party/workspace-bench/evals/hallucination/capture_rows.json"
CACHE = H.RESULTS / "qsamples.jsonl"
TOKENIZER = "Qwen/Qwen3.6-27B"
REV = "6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"  # Qwen3.6-27B, as I1a pins it


def _tokenizer():
    from transformers import AutoTokenizer

    snaps = glob.glob(str(Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3.6-27B/"
                          "snapshots" / Path(REV)))
    return (AutoTokenizer.from_pretrained(snaps[0]) if snaps
            else AutoTokenizer.from_pretrained(TOKENIZER, revision=REV))


def prompts() -> dict[tuple[str, int], str]:
    """``{(item id, pos): raw prompt}`` for every read site."""
    tok = _tokenizer()
    out = {}
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    for r in json.loads(CAPTURE.read_text()):
        for s in items[r["id"]]["sites"]:
            out[(r["id"], s["pos"])] = tok.decode(r["input_ids"][: s["pos"] + 1])
    return out


def worst_usd(prompt: str) -> float:
    return (len(prompt.encode()) + 64) * PRICE[0] / 1e6 + MAX_TOKENS * PRICE[1] / 1e6


async def _call(client, sem, key, prompt, seed):
    async with sem:
        for attempt in range(5):
            try:
                resp = await client.completions.create(
                    model=ROUTE, prompt=prompt, max_tokens=MAX_TOKENS, temperature=1.0,
                    top_p=1.0, seed=seed, timeout=120,
                    extra_body={"usage": {"include": True}, "provider": PROVIDER})
                u = resp.usage
                cost = getattr(u, "cost", None)
                if cost is None and getattr(u, "model_extra", None):
                    cost = u.model_extra.get("cost")
                tin, tout = int(u.prompt_tokens or 0), int(u.completion_tokens or 0)
                usd = float(cost) if cost is not None else (
                    tin * PRICE[0] + tout * PRICE[1]) / 1e6
                ch = resp.choices[0]
                return {"key": key, "seed": seed, "text": ch.text, "finish": ch.finish_reason,
                        "input_tokens": tin, "output_tokens": tout, "usd": usd,
                        "provider": getattr(resp, "provider", None), "error": None}
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:200]}"
                if "401" in err or "402" in err or "403" in err:
                    raise
                await asyncio.sleep(2 ** attempt)
        return {"key": key, "seed": seed, "text": None, "error": err, "usd": 0.0}


def run(limit: int | None = None, n_samples: int = N_SAMPLES) -> None:
    sys.path.insert(0, str(H.ROOT / "src"))
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    from emx import spend

    load_dotenv(H.ROOT / ".env", override=False)
    done = set()
    if CACHE.exists():
        done = {(r["key"], r["seed"]) for x in CACHE.read_text().splitlines()
                if (r := json.loads(x)).get("text") is not None}
    ps = prompts()
    todo = [(f"{i}|p{p}", pr, SEED0 + k) for (i, p), pr in sorted(ps.items())
            for k in range(n_samples) if (f"{i}|p{p}", SEED0 + k) not in done]
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        print("nothing to do")
        return
    reserve = 1.25 * sum(worst_usd(pr) for _, pr, _ in todo)
    note = "p35_qsample"
    rid = spend.reserve_api("P35", reserve, note=note)
    spend.mark_submitted(rid, exp="P35")
    print(f"{len(todo)} calls; reserved ${reserve:.2f}", flush=True)
    total = 0.0
    try:
        client = AsyncOpenAI(api_key=os.environ["OPENROUTER_API_KEY"],
                             base_url="https://openrouter.ai/api/v1", max_retries=0)
        sem = asyncio.Semaphore(int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32")))

        async def go():
            nonlocal total
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            with CACHE.open("a") as f:
                for coro in asyncio.as_completed([_call(client, sem, k, p, s)
                                                  for k, p, s in todo]):
                    row = await coro
                    total += row["usd"]
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()

        asyncio.run(go())
    finally:
        spend.settle_api(rid, total_usd=total, exp="P35", note=note, resource=ROUTE)
    print(f"spent ${total:.3f}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "run"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    run(limit=1 if a.cmd == "probe" else a.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
