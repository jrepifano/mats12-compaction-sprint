"""P42 S1 (laptop, paid): text-embedding-3-small vectors for every prompt, and the kNN score.

Port of ../mats12_neel_working/scripts/tda_embed.py (batched AsyncOpenAI embeddings with
tenacity-style retries; inputs cut to 20,000 characters, as there), extended with a resumable
cache, the probe spend ledger and the S1 score (plan "Scorers", Liu et al. 2023's kNN).

- **Inputs:** `data/P42/prompts.jsonl` (K.PROMPT_FIELDS), every set, every row's `text`.
- **Batches:** at most `MAX_BATCH_INPUTS` inputs and `MAX_BATCH_TOKENS` estimated tokens per
  request (OpenAI's limits are 2,048 inputs and 300k tokens per request, 8,191 per input; the
  OLMo-3 tokenizer is cl100k-based and texts are cut at 2,048 of its tokens). `CONCURRENCY`
  requests in flight; transient errors (429, 5xx, timeouts, connection) retried with jittered
  exponential backoff; a 400 on a multi-input batch is split in halves (a lone input that still
  fails is an error, raised).
- **Cache:** `results/embed/chunks/*.npz`, one file per landed request: `pids`, `shas` (sha256 of
  the text sent) and float32 `emb`. A prompt is reused only when its pid AND text sha match, so a
  changed prompt file re-embeds just the changed rows.
- **Spend** (exp "P42", note `p42_embed`): the projection is the token estimate (tiktoken
  cl100k_base when installed, else ceil(UTF-8 bytes / 3), an over-estimate for English) x
  $0.02/MTok x `RESERVE_MARGIN`. It is reserved with `emx.spend.reserve_api` before any call (a
  submitted reservation left open by a crash is first settled at its full amount with
  `recover_uncertain`); the actual `usage.prompt_tokens` are recorded with `emx.spend.record`
  under the reservation, and `settle_api` closes it, charging requests of uncertain outcome
  (timeouts, connection errors) at their estimate. `--dry-run` prints the projection and touches
  nothing (no keys, no client, no ledger, no files).
- **Outputs** (only when every prompt has a vector): `results/embed/emb.f16.npy` (N x 1536,
  L2-normalised, float16, row order = `results/embed/pids.txt`, the prompts file's order),
  `results/embed/emb_scores.csv` (`pid, emb_knn` for K.SCORED_SETS rows: 1 - mean of the top
  K.KNN_K raw cosines to the id_ref vectors), and `results/embed/embed.meta.json`.

Usage (from the probe directory):
  uv run python -m p42.embed --dry-run
  uv run python -m p42.embed [--limit N]      # --limit: embed the first N pending only (smoke)
  uv run python -m p42.embed --score-only     # rebuild outputs from the cache, no calls
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from emx import spend
from p42 import contracts as K

EXP = K.PROBE
NOTE = "p42_embed"
PRICE_PER_MTOK = 0.02  # text-embedding-3-small, standard tier
MAX_BATCH_INPUTS = 1024
MAX_BATCH_TOKENS = 150_000
MAX_CHARS = 20_000  # as tda_embed.py
CONCURRENCY = 8
MAX_RETRIES = 6
RESERVE_MARGIN = 1.5
MIN_RESERVE_USD = 0.01
KNN_CHUNK = 1024
_TRANSIENT = (408, 409, 429, 500, 502, 503, 504)


# --- texts, tokens, cost -------------------------------------------------------------------------


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def embed_input(text: str) -> str:
    """The string sent: cut to MAX_CHARS; an empty prompt becomes one space (the API refuses
    empty inputs)."""
    t = str(text)[:MAX_CHARS]
    return t if t.strip() else " "


def _tokenizer():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - not installed, or no cached BPE file offline
        return None


def estimate_tokens(texts: list[str], enc=None) -> list[int]:
    if enc is not None:
        return [len(enc.encode(t, disallowed_special=())) for t in texts]
    return [math.ceil(len(t.encode("utf-8")) / 3) + 1 for t in texts]


def usd_for_tokens(tokens: float) -> float:
    return tokens * PRICE_PER_MTOK / 1e6


def batches(items: list[tuple[str, str, str]], est: list[int],
            max_inputs: int = MAX_BATCH_INPUTS, max_tokens: int = MAX_BATCH_TOKENS) -> list[list[int]]:
    """Index batches in order: <= max_inputs items and <= max_tokens estimated tokens each."""
    out, cur, tok = [], [], 0
    for i, n in enumerate(est):
        if cur and (len(cur) >= max_inputs or tok + n > max_tokens):
            out.append(cur)
            cur, tok = [], 0
        cur.append(i)
        tok += n
    if cur:
        out.append(cur)
    return out


# --- cache ----------------------------------------------------------------------------------------


def chunk_dir(embed_dir: Path) -> Path:
    return Path(embed_dir) / "chunks"


def load_cache(embed_dir: Path) -> dict[str, tuple[str, np.ndarray]]:
    """{pid: (text sha, float32 vector)}; a later chunk wins for a repeated pid."""
    out: dict[str, tuple[str, np.ndarray]] = {}
    d = chunk_dir(embed_dir)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.npz")):
        with np.load(p, allow_pickle=False) as z:
            for pid, sha, v in zip(z["pids"], z["shas"], z["emb"], strict=True):
                out[str(pid)] = (str(sha), np.asarray(v, dtype=np.float32))
    return out


def write_chunk(embed_dir: Path, pids: list[str], shas: list[str], emb: np.ndarray) -> Path:
    d = chunk_dir(embed_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}_{hashlib.sha256('|'.join(pids).encode()).hexdigest()[:12]}.npz"
    tmp = d / (name + ".tmp.npz")
    np.savez(tmp, pids=np.array(pids), shas=np.array(shas), emb=emb.astype(np.float32))
    os.replace(tmp, d / name)
    return d / name


# --- the API batch --------------------------------------------------------------------------------


def make_client():
    from openai import AsyncOpenAI

    from emx import judge_panel as JP

    JP.load_env()
    return AsyncOpenAI(api_key=JP.require_key("OPENAI_API_KEY"))


def _status(e: BaseException) -> int | None:
    s = getattr(e, "status_code", None)
    return s if isinstance(s, int) else None


def _transient(e: BaseException) -> bool:
    s = _status(e)
    if s is not None:
        return s in _TRANSIENT
    name = type(e).__name__
    return isinstance(e, (TimeoutError, ConnectionError, asyncio.TimeoutError)) or name in (
        "APITimeoutError", "APIConnectionError")


class EmbedBudget:
    """In-process bound on one reservation: known spend + worst case of requests in flight +
    requests of uncertain outcome never passes the reservation."""

    def __init__(self, cap_usd: float):
        self.cap = cap_usd
        self.known = 0.0
        self.inflight = 0.0
        self.uncertain = 0.0
        self.tokens = 0
        self.requests = 0

    def acquire(self, usd: float) -> bool:
        if self.known + self.inflight + self.uncertain + usd > self.cap + 1e-12:
            return False
        self.inflight += usd
        return True

    def landed(self, worst: float, tokens: int) -> None:
        self.inflight -= worst
        self.known += usd_for_tokens(tokens)
        self.tokens += tokens
        self.requests += 1

    def failed(self, worst: float, uncertain: bool) -> None:
        self.inflight -= worst
        if uncertain:
            self.uncertain += worst

    @property
    def charge(self) -> float:
        return self.known + self.uncertain


async def _embed_batch(client, texts: list[str], worst: float, budget: EmbedBudget,
                       max_retries: int) -> tuple[np.ndarray, int]:
    """One request with retries -> (float32 vectors, usage tokens)."""
    for attempt in range(max_retries):
        if not budget.acquire(worst):
            raise RuntimeError("embedding reservation exhausted (in-run budget)")
        try:
            resp = await client.embeddings.create(model=K.EMBED_MODEL, input=texts)
        except Exception as e:
            # 4xx answers were not billed; transport failures may have been
            budget.failed(worst, uncertain=_status(e) is None)
            if _transient(e) and attempt < max_retries - 1:
                await asyncio.sleep(min(60.0, 2.0 * 2 ** attempt) * (0.5 + random.random()))
                continue
            raise
        tokens = int(getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0)
        budget.landed(worst, tokens)
        data = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        if len(data) != len(texts):
            raise RuntimeError(f"embedding reply has {len(data)} vectors for {len(texts)} inputs")
        emb = np.asarray([d.embedding for d in data], dtype=np.float32)
        if emb.shape[1] != K.EMBED_DIM:
            raise RuntimeError(f"embedding dim {emb.shape[1]} != {K.EMBED_DIM}")
        return emb, tokens
    raise RuntimeError("unreachable")


async def _run(client, items, est, embed_dir, budget, concurrency, max_retries) -> int:
    """items: [(pid, sha, text)]. Writes one chunk per landed request; returns vectors landed."""
    sem = asyncio.Semaphore(concurrency)
    landed = 0

    async def one(idx: list[int]) -> None:
        nonlocal landed
        texts = [items[i][2] for i in idx]
        worst = usd_for_tokens(sum(est[i] for i in idx)) * RESERVE_MARGIN
        try:
            async with sem:
                emb, _ = await _embed_batch(client, texts, worst, budget, max_retries)
        except Exception as e:
            if _status(e) == 400 and len(idx) > 1:  # split a refused batch
                h = len(idx) // 2
                await asyncio.gather(one(idx[:h]), one(idx[h:]))
                return
            raise
        write_chunk(embed_dir, [items[i][0] for i in idx], [items[i][1] for i in idx], emb)
        landed += len(idx)

    await asyncio.gather(*[one(b) for b in batches(items, est)])
    return landed


# --- outputs --------------------------------------------------------------------------------------


def read_prompts(path: Path = K.PROMPTS_PATH) -> pd.DataFrame:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").split("\n")
            if line.strip()]
    df = pd.DataFrame(rows)
    missing = [c for c in K.PROMPT_FIELDS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing fields {missing}")
    if df["pid"].duplicated().any():
        raise ValueError(f"{path}: duplicate pids")
    return df


def l2n(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, 1e-12)


def knn_score(q: np.ndarray, ref: np.ndarray, k: int = K.KNN_K, chunk: int = KNN_CHUNK) -> np.ndarray:
    """1 - mean of the top-k raw cosines of each (unit) query row to the (unit) ref rows."""
    q, ref = np.asarray(q, np.float32), np.asarray(ref, np.float32)
    k = min(k, ref.shape[0])
    out = np.empty(q.shape[0], dtype=np.float64)
    for s in range(0, q.shape[0], chunk):
        sim = q[s:s + chunk] @ ref.T
        top = np.partition(sim, sim.shape[1] - k, axis=1)[:, -k:]
        out[s:s + chunk] = 1.0 - top.mean(axis=1)
    return out


def build_outputs(prompts: pd.DataFrame, cache: dict, embed_dir: Path) -> dict:
    pids = prompts["pid"].astype(str).tolist()
    shas = [text_sha(embed_input(t)) for t in prompts["text"]]
    emb = np.stack([cache[p][1] for p in pids])
    emb = l2n(emb)
    embed_dir = Path(embed_dir)
    embed_dir.mkdir(parents=True, exist_ok=True)
    tmp = embed_dir / "emb.f16.tmp.npy"
    np.save(tmp, emb.astype(np.float16))
    os.replace(tmp, embed_dir / "emb.f16.npy")
    (embed_dir / "pids.txt").write_text("\n".join(pids) + "\n")
    e = emb.astype(np.float32)  # score in float32 from the cache (no f16 round trip)
    sets = prompts["set"].astype(str).to_numpy()
    ref = e[sets == K.ID_REF]
    if len(ref) == 0:
        raise ValueError("no id_ref prompts to score against")
    scored = np.isin(sets, K.SCORED_SETS)
    df = pd.DataFrame({"pid": np.array(pids)[scored], "emb_knn": knn_score(e[scored], ref)})
    df.to_csv(embed_dir / "emb_scores.csv", index=False)
    meta = {"model": K.EMBED_MODEL, "dim": int(emb.shape[1]), "n": len(pids),
            "n_scored": int(scored.sum()), "n_ref": len(ref), "knn_k": K.KNN_K,
            "texts_sha256": hashlib.sha256("\n".join(shas).encode()).hexdigest(),
            "max_chars": MAX_CHARS, "utc": spend.utc_now()}
    (embed_dir / "embed.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


# --- main -----------------------------------------------------------------------------------------


def plan(prompts: pd.DataFrame, cache: dict, limit: int | None = None, enc=None):
    """(pending items [(pid, sha, text)], token estimates, projection $)."""
    items = []
    for pid, text in zip(prompts["pid"].astype(str), prompts["text"], strict=True):
        t = embed_input(text)
        sha = text_sha(t)
        hit = cache.get(pid)
        if hit is None or hit[0] != sha:
            items.append((pid, sha, t))
    if limit is not None:
        items = items[:limit]
    est = estimate_tokens([t for _, _, t in items], enc)
    return items, est, usd_for_tokens(sum(est))


def run(args, *, client=None, enc="auto") -> int:
    embed_dir = Path(args.embed_dir)
    prompts = read_prompts(Path(args.prompts))
    cache = load_cache(embed_dir)
    enc = _tokenizer() if enc == "auto" else enc
    items, est, proj = plan(prompts, cache, args.limit, enc)
    reserve = max(MIN_RESERVE_USD, math.ceil(proj * RESERVE_MARGIN * 1e6) / 1e6)
    print(f"P42 EMBED: {len(prompts)} prompts, {len(prompts) - len(items)} cached, "
          f"{len(items)} pending in {len(batches(items, est))} requests; "
          f"{sum(est):,} est. tokens ({'tiktoken' if enc else 'bytes/3'}) -> ${proj:.4f}, "
          f"reservation ${reserve:.4f}")
    if args.dry_run:
        return 0
    if items and not args.score_only:
        if args.max_usd is not None and reserve > args.max_usd:
            print(f"P42 EMBED REFUSED: reservation ${reserve:.4f} > --max-usd", file=sys.stderr)
            return 2
        spend.recover_uncertain(EXP, NOTE)
        stale = spend.find_open(EXP, NOTE)
        if stale is not None:
            spend.release_api(stale)
        rid = spend.reserve_api(EXP, reserve, NOTE)  # BudgetExceeded propagates (exit 2)
        budget = EmbedBudget(reserve)
        try:
            spend.mark_submitted(rid, exp=EXP)
            client = client or make_client()
            n = asyncio.run(_run(client, items, est, embed_dir, budget, args.concurrency,
                                 MAX_RETRIES))
            print(f"P42 EMBED: {n} vectors landed")
        finally:
            if budget.tokens or budget.requests:
                spend.record("judge", K.EMBED_MODEL, budget.requests, round(budget.known, 6),
                             note=f"[P42 embed] {budget.tokens} tokens, {budget.requests} "
                                  f"requests", exp=EXP, reservation_id=rid)
            spend.settle_api(rid, total_usd=round(budget.charge, 6), exp=EXP,
                             note=f"[P42 embed] settled: ${budget.known:.4f} known + "
                                  f"${budget.uncertain:.4f} uncertain of ${reserve:.4f}")
        cache = load_cache(embed_dir)
    missing = [p for p, t in zip(prompts["pid"].astype(str), prompts["text"], strict=True)
               if p not in cache or cache[p][0] != text_sha(embed_input(t))]
    if missing:
        print(f"P42 EMBED: {len(missing)} prompts still lack a vector; outputs not written")
        return 0 if args.limit is not None else 1
    meta = build_outputs(prompts, cache, embed_dir)
    print(f"P42 EMBED DONE: {json.dumps(meta)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m p42.embed", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--score-only", action="store_true", help="no calls; outputs from the cache")
    ap.add_argument("--max-usd", type=float, default=None)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--prompts", default=str(K.PROMPTS_PATH))
    ap.add_argument("--embed-dir", default=str(K.EMBED_DIR))
    return ap


def main(argv=None, **hooks) -> int:
    return run(build_parser().parse_args(argv), **hooks)


if __name__ == "__main__":
    sys.exit(main())
