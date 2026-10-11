"""P06 Stage 1: ask items over OpenRouter with nocot-bench's no-CoT recipe, in three filler arms.

Recipe (nocot-bench @ 634d7de5, ELICITATION.md §1 and models.csv rows for both models): the bank's
shot rows as prior user/assistant turns, an assistant prefill "Answer:" (no trailing space),
`reasoning: {enabled: false}`, temperature 0, max_tokens 100, `usage: {include: true}`, no stop
sequences, a hard provider pin (`allow_fallbacks: false`). Each row is scored by nocot's own
witnesses and grader (`p06.nocot`); a row that fails a witness is scored wrong.

Arms (plan "Setup"); the filler goes in the target user turn only, never in the shots:
    none    "<instruction>\n\nProblem: <problem>"
    after   "<instruction>\n\nProblem: <problem>\n\n<FILLER>"
    before  "<instruction>\n\n<FILLER>\n\nProblem: <problem>"
FILLER is 500 space-separated dots. 500 contiguous dots tokenize to 32 tokens on Gemma 4 and 6 on
DeepSeek V4 ("....", "....." merges); ". . ." is exactly 500 tokens on both (checked 2026-10-11
with each model's tokenizer at the pinned revision; `tests/test_api.py` re-checks the string).

Output rows (jsonl, one per call, appended; resumable by `row_key`):
    key, model, item_id, bank, k, arm, draw, temperature, recipe, provider, raw_text, usage,
    cost, finish_reason, witness (nocot verdict dict), graded (nocot grade_row dict), correct,
    valid, top_logprobs (first completion token, if the provider returns them), utc
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .nocot import grade as G
from .nocot import witnesses as W

API = "https://openrouter.ai/api/v1/chat/completions"
N_FILLER = 500
FILLER = " ".join(["."] * N_FILLER)
ARMS = ("none", "after", "before")
MAX_TOKENS = 100


@dataclass(frozen=True)
class Recipe:
    name: str
    model: str
    provider: tuple[str, ...]
    price_in: float  # $ per token (pinned provider, OpenRouter, checked 2026-10-11)
    price_out: float
    reasoning: dict | None = field(default_factory=lambda: {"enabled": False})
    prefill: bool = True
    logprobs: bool = True


RECIPES = {
    # nocot pinned GMICloud/StreamLake (fp8); StreamLake also returns logprobs.
    "v4flash": Recipe("v4flash", "deepseek/deepseek-v4-flash", ("StreamLake",),
                      0.0826e-6, 0.1652e-6),
    # nocot ran Gemma 4 31B unpinned; we pin bf16 hosts (Novita: bf16, logprobs; then Crusoe:
    # bf16, same price) to match the Stage 2 bf16 weights. Novita alone hit upstream 429s on
    # ~25% of pilot calls (2026-10-11), so Crusoe is the second host in the order.
    # F4 only (Brauer's task): Gemma 3 has no reasoning parameter (nocot's ask omits the key).
    "gemma3": Recipe("gemma3", "google/gemma-3-27b-it", ("Novita",), 0.119e-6, 0.20e-6,
                     reasoning=None, logprobs=False),
    "gemma4": Recipe("gemma4", "google/gemma-4-31b-it", ("Novita", "Crusoe"), 0.14e-6, 0.40e-6),
}


def user_turn(item: dict, arm: str) -> str:
    instr, prob = item["instruction"], item["problem"]
    if arm == "none":
        return f"{instr}\n\nProblem: {prob}"
    if arm == "after":
        return f"{instr}\n\nProblem: {prob}\n\n{FILLER}"
    if arm == "before":
        return f"{instr}\n\n{FILLER}\n\nProblem: {prob}"
    raise ValueError(arm)


def build_messages(item: dict, shots: list[dict], arm: str, prefill: bool = True,
                   placement: str = "target") -> list[dict]:
    """placement "target": filler in the target turn only. "every": the shot turns carry the same
    arm's filler too (Brauer et al. 2026's and nocot's published filler placement)."""
    if placement not in ("target", "every"):
        raise ValueError(placement)
    msgs = []
    for s in shots:
        ut = user_turn(s, arm if placement == "every" else "none")
        msgs.append({"role": "user", "content": ut if prefill else ut + "\n\nAnswer:"})
        msgs.append({"role": "assistant",
                     "content": f"Answer: {s['answer']}" if prefill else str(s["answer"])})
    ut = user_turn(item, arm)
    msgs.append({"role": "user", "content": ut if prefill else ut + "\n\nAnswer:"})
    if prefill:
        msgs.append({"role": "assistant", "content": "Answer:"})
    return msgs


def build_body(recipe: Recipe, msgs: list[dict], temperature: float,
               reasoning: dict | None | str = "recipe", max_tokens: int = MAX_TOKENS) -> dict:
    body = {"model": recipe.model, "messages": msgs, "max_tokens": max_tokens,
            "temperature": temperature, "usage": {"include": True},
            "provider": {"order": list(recipe.provider), "allow_fallbacks": False}}
    r = recipe.reasoning if reasoning == "recipe" else reasoning
    if r is not None:
        body["reasoning"] = r
    if recipe.logprobs:
        body["logprobs"] = True
        body["top_logprobs"] = 5
    return body


def row_key(model: str, item_id: str, arm: str, draw: int, temperature: float, tag: str) -> str:
    return f"{model}|{item_id}|{arm}|{draw}|t{temperature:g}|{tag}"


def parse_response(d: dict) -> dict:
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    lp = None
    content_lp = ((ch.get("logprobs") or {}).get("content") or [])
    if content_lp:
        lp = [{"token": t.get("token"), "top": [(x.get("token"), x.get("logprob"))
                                                  for x in (t.get("top_logprobs") or [])]}
              for t in content_lp[:3]]
    return {"text": msg.get("content") or "",
            "reasoning_text": msg.get("reasoning") or "",
            "usage": d.get("usage") or {},
            "provider": d.get("provider"),
            "finish_reason": ch.get("finish_reason"),
            "native_finish_reason": ch.get("native_finish_reason"),
            "top_logprobs": lp}


def score(out: dict, item: dict) -> dict:
    """nocot's witnesses and grader on one response."""
    short = item.get("answer_type") in (None, "int", "integer", "token")
    w = W.verdict({"usage": out["usage"], "reasoning_text": out["reasoning_text"]},
                  out["text"], short_answer=short)
    raw = {"status": "ok", "finish_reason": out["finish_reason"], "raw_text": out["text"],
           "usage": out["usage"], "reasoning_text": out["reasoning_text"]}
    graded = G.grade_row(raw, {**item, "domain": item["bank"]})
    return {"witness": w, "graded": graded,
            "correct": bool(graded.get("correct")), "valid": bool(graded.get("valid"))}


class Asker:
    """Async OpenRouter caller with a semaphore, retries on transport errors and 429/5xx, and
    cost totals read from `usage.cost`."""

    def __init__(self, key: str, concurrency: int = 24, retries: int = 8):
        self.key, self.sem, self.retries = key, asyncio.Semaphore(concurrency), retries
        self.cost = 0.0
        self.calls = 0
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    async def close(self):
        await self.client.aclose()

    async def post(self, body: dict) -> dict:
        last = None
        for attempt in range(self.retries):
            async with self.sem:
                try:
                    r = await self.client.post(
                        API, json=body, headers={"Authorization": f"Bearer {self.key}"})
                    if r.status_code == 200:
                        d = r.json()
                        if "error" not in d:
                            self.calls += 1
                            self.cost += float((d.get("usage") or {}).get("cost") or 0.0)
                            return d
                        last = json.dumps(d["error"])[:400]
                    else:
                        last = f"HTTP {r.status_code}: {r.text[:400]}"
                        if r.status_code in (400, 401, 403, 404):
                            raise RuntimeError(last)
                except httpx.HTTPError as e:
                    last = repr(e)[:400]
            await asyncio.sleep(min(60, 2 ** attempt))
        raise RuntimeError(f"gave up after {self.retries}: {last}")


def done_keys(path: Path) -> set[str]:
    """Keys with a scored row. A key whose rows are all api_error is retried on the next run."""
    return set(latest_ok(path))


def latest_ok(path: Path) -> dict[str, dict]:
    """key -> the last row with status ok (readers use this; api_error rows are kept in the file
    as a record of the attempt but never scored)."""
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("status") == "ok":
                    out[r["key"]] = r
    return out


async def run_jobs(asker: Asker, recipe: Recipe, jobs: list[dict], out_path: Path,
                   tag: str, reasoning: dict | None | str = "recipe",
                   max_tokens: int = MAX_TOKENS, placement: str = "target") -> list[dict]:
    """jobs: {item, shots, arm, draw, temperature}. Skips keys already in out_path; appends one
    row per finished call (an API error is written with status 'api_error' and no score)."""
    have = done_keys(out_path)
    todo = [j for j in jobs if row_key(recipe.model, j["item"]["item_id"], j["arm"], j["draw"],
                                       j["temperature"], tag) not in have]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lock = asyncio.Lock()
    rows = []

    async def one(j):
        item = j["item"]
        key = row_key(recipe.model, item["item_id"], j["arm"], j["draw"], j["temperature"], tag)
        msgs = j.get("messages") or build_messages(item, j["shots"], j["arm"], recipe.prefill,
                                                   placement)
        body = build_body(recipe, msgs, j["temperature"], reasoning, max_tokens)
        base = {"key": key, "model": recipe.model, "recipe": recipe.name, "tag": tag,
                "placement": placement, "n_shots": len(j["shots"]),
                "item_id": item["item_id"], "bank": item["bank"], "k": item["k"],
                "arm": j["arm"], "draw": j["draw"], "temperature": j["temperature"],
                "utc": datetime.now(UTC).isoformat(timespec="seconds")}
        try:
            d = await asker.post(body)
        except Exception as e:  # noqa: BLE001
            row = {**base, "status": "api_error", "error": str(e)[:400]}
        else:
            out = parse_response(d)
            row = {**base, "status": "ok", "raw_text": out["text"][:2000],
                   "reasoning_text_len": len(out["reasoning_text"]), "usage": out["usage"],
                   "cost": float(out["usage"].get("cost") or 0.0), "provider": out["provider"],
                   "finish_reason": out["finish_reason"], "top_logprobs": out["top_logprobs"],
                   **score(out, item)}
        async with lock:
            with out_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            rows.append(row)

    t0 = time.time()
    await asyncio.gather(*(one(j) for j in todo))
    print(f"[api] {tag} {recipe.name}: {len(todo)} calls in {time.time() - t0:.0f}s, "
          f"${asker.cost:.4f} so far", flush=True)
    return rows


def openrouter_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    return os.environ["OPENROUTER_API_KEY"]
