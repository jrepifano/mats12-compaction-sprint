"""P07's OpenRouter client for the non-agent calls (summaries, rewrites, identity probes, the
skepticism judge), and the per-batch spend guard.

Agent episodes run through Inspect (`agent.py`); everything else comes here. Billed cost is
OpenRouter's `usage.cost` (as X3 reads it from Inspect's model events, `x3.runner._billed`).
Every batch goes through `Batch`: `emx.spend.check_total` on its projected worst case before
the first call, then one `emx.spend.record` row with the billed total at the end (also on error,
so a crashed batch's spend is not lost).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

from p07 import contracts as C

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MAX_ATTEMPTS = 5
BACKOFF_S = (5, 20, 60, 120)
TIMEOUT_S = 300


def load_key() -> None:
    """OPENROUTER_API_KEY from the worktree's .env (copied from main); logs key names only."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    from dotenv import dotenv_values

    for env in (C.ROOT / ".env", C.MAIN_CHECKOUT / ".env"):
        if env.exists():
            v = dotenv_values(env).get("OPENROUTER_API_KEY")
            if v:
                os.environ["OPENROUTER_API_KEY"] = v
                print("loaded keys: OPENROUTER_API_KEY", flush=True)
                return
    raise SystemExit("OPENROUTER_API_KEY not found")


# --- message conversion ----------------------------------------------------------------------
def _text_of(content) -> str:
    """Visible text of an Inspect message content (reasoning blocks dropped)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text") or "")
    return "\n".join(p for p in parts if p)


def to_openai(messages: list[dict]) -> list[dict]:
    """Inspect ChatMessage model_dumps -> OpenAI chat messages. Reasoning is dropped (pi-mono's
    serializer and Codex's compaction request keep only visible text and tool calls)."""
    out = []
    for m in messages:
        role = m["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": _text_of(m.get("content"))})
        elif role == "assistant":
            msg = {"role": "assistant", "content": _text_of(m.get("content")) or None}
            tcs = m.get("tool_calls") or []
            if tcs:
                msg["tool_calls"] = [{"id": t["id"], "type": "function",
                                      "function": {"name": t["function"],
                                                   "arguments": json.dumps(t["arguments"])}}
                                     for t in tcs]
            if msg["content"] is None and not tcs:
                msg["content"] = ""
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                        "content": _text_of(m.get("content"))})
    return out


# The X3 tools, declared on calls whose history holds tool calls (some providers reject tool
# messages without tool definitions); tool_choice="none" keeps them unused.
X3_TOOLS = [
    {"type": "function", "function": {
        "name": "execute_command", "description": "Run one shell command in the sandbox and "
        "return its output.", "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "End the session.",
        "parameters": {"type": "object", "properties": {}}}},
]


# --- one call ----------------------------------------------------------------------------------
@dataclass
class Reply:
    text: str
    reasoning: str
    billed_usd: float | None
    list_usd: float
    usage: dict | None
    provider: str | None
    response_id: str | None
    finish_reason: str | None
    error: str | None = None


def list_cost(model: C.Model, n_in: int, n_out: int) -> float:
    return (n_in * model.in_ceiling + n_out * model.out_ceiling) / 1e6


_client = None


def client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        load_key()
        _client = AsyncOpenAI(base_url=OPENROUTER_BASE, api_key=os.environ["OPENROUTER_API_KEY"],
                              max_retries=0, timeout=TIMEOUT_S)
    return _client


async def chat(model: C.Model, messages: list[dict], *, max_tokens: int,
               temperature: float | None = None, tools: list | None = None,
               sleep=asyncio.sleep) -> Reply:
    """One chat completion with up to MAX_ATTEMPTS attempts. A failed attempt is charged its
    worst case (input bound from bytes, plus max_tokens) in `list_usd`, as X3 does."""
    body: dict = {"provider": model.provider, "usage": {"include": True}}
    kw: dict = {"model": model.model_id, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        kw["temperature"] = temperature
    if tools:
        kw["tools"] = tools
        kw["tool_choice"] = "none"
    if "reasoning_effort" in model.generate:
        body["reasoning"] = {"effort": model.generate["reasoning_effort"]}
    failed_charge, err = 0.0, None
    in_bound = len(json.dumps(messages).encode()) + 16 * len(messages)
    for k in range(MAX_ATTEMPTS):
        try:
            r = await client().chat.completions.create(**kw, extra_body=body)
            u = r.usage.model_dump() if r.usage else None
            n_in = (u or {}).get("prompt_tokens") or 0
            n_out = (u or {}).get("completion_tokens") or 0
            billed = (u or {}).get("cost")
            ch = r.choices[0]
            reasoning = getattr(ch.message, "reasoning", None) or ""
            return Reply(text=ch.message.content or "", reasoning=reasoning,
                         billed_usd=(float(billed) + failed_charge
                                     if isinstance(billed, int | float) else None),
                         list_usd=list_cost(model, n_in, n_out) + failed_charge, usage=u,
                         provider=getattr(r, "provider", None), response_id=r.id,
                         finish_reason=ch.finish_reason)
        except Exception as e:  # noqa: BLE001 - any API failure is retried, then reported
            err = repr(e)[:1000]
            if getattr(e, "status_code", None) != 429:   # a rate-limit refusal is not billed
                failed_charge += list_cost(model, in_bound, max_tokens)
            if k < MAX_ATTEMPTS - 1:
                await sleep(BACKOFF_S[min(k, len(BACKOFF_S) - 1)])
    return Reply("", "", None, failed_charge, None, None, None, None, error=err)


def charge(r: Reply) -> float:
    """What a reply costs the ledger: billed when known, else its list-price bound."""
    return r.billed_usd if r.billed_usd is not None else r.list_usd


# --- batches -------------------------------------------------------------------------------------
@dataclass
class Batch:
    """`async with Batch("summaries", projected_usd=0.8) as b: b.add(reply)`. Refuses to start
    if the projected worst case would cross P07's cap; records the billed total once at exit."""
    label: str
    projected_usd: float
    ledger: str | None = None
    spent: float = 0.0
    n: int = 0
    notes: list[str] = field(default_factory=list)

    async def __aenter__(self):
        from emx import spend

        spend.check_total(self.projected_usd, "judge", exp=C.EXP, ledger=self.ledger)
        return self

    def add(self, r: Reply) -> None:
        self.spent += charge(r)
        self.n += 1

    async def __aexit__(self, et, ev, tb):
        from emx import spend

        if self.n:
            spend.record("judge", f"openrouter:{self.label}", self.n, round(self.spent, 6),
                         note=f"P07 {self.label}: {self.n} calls"
                         + (f"; aborted: {et.__name__}" if et else ""),
                         exp=C.EXP, ledger=self.ledger)
        return False


async def gather_limited(coros, limit: int):
    sem = asyncio.Semaphore(limit)

    async def one(c):
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros))
