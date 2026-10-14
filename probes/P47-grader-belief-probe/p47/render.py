"""P47 local renders (plan "Automatic rules": fidelity check; design/interfaces.md): Inspect message
dumps -> the subject model's own chat template -> token ids, plus the read positions the pod uses.

- `hf_messages(dumps)`: Inspect dumps -> HF chat messages. Assistant reasoning goes in
  `reasoning_content` (Qwen3.6's template) or `thinking` (gpt-oss's); tool calls carry their
  arguments as a dict; tool results become role "tool". The templates themselves decide what to
  keep (Qwen drops reasoning before the last user message; gpt-oss drops analysis before a final
  message), exactly as the provider's server applies them.
- `render_ids(tok, dumps, slug)`: ids of the conversation with the generation prompt, i.e. the
  sequence the model reads when it starts its next turn. Its last token is the "generation
  boundary" every P47 read uses (site boundary, post-statement boundary, C3 boundary).
- `render_open_reasoning(tok, dumps, slug, text)`: the conversation, the generation prompt, the
  opening of the reasoning channel and `text` (a contrast statement, as the start of the model's
  own reasoning); returns (ids, index of the statement's last token).
- `TOOLS_JSON`: X3's two tools in OpenAI function format, as Inspect sends them.

Fidelity (`python -m p47.render fidelity`): for continuation artifacts, re-render the messages
sent at every turn and compare the token count with the provider-reported `input_tokens`.

    PYTHONPATH=probes/P47-grader-belief-probe:src:scripts P47_MODEL=<slug> \
        uv run --group swarm python -m p47.render fidelity [--n 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache

from p47 import contracts as C
from p47 import sources as S

TOOLS_JSON = [
    {"type": "function", "function": {
        "name": "execute_command",
        "description": "Run one shell command in the sandbox and return its output.",
        "parameters": {"type": "object", "properties": {"command": {
            "type": "string", "description": "The shell command to run."}},
            "required": ["command"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "done", "description": "End the session.",
        "parameters": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False}}},
]
# The Qwen server's tool header: matched by token count (2026-10-14, log.md) when each function
# also carries "strict": false and "response": null with "description" last (an SGLang-style tool
# schema dump); the exact text the server rendered can't be observed, only its token count.
QWEN_TOOLS_JSON = [
    {"type": "function", "function": {
        "name": t["function"]["name"], "parameters": t["function"]["parameters"],
        "strict": False, "response": None, "description": t["function"]["description"]}}
    for t in TOOLS_JSON]


def tools_for(slug: str) -> list[dict]:
    return QWEN_TOOLS_JSON if slug == "qwen3.6-27b" else TOOLS_JSON


REASONING_OPEN = {"qwen3.6-27b": "", "gpt-oss-120b": "<|channel|>analysis<|message|>",
                  "gpt-oss-20b": "<|channel|>analysis<|message|>"}


@lru_cache(maxsize=4)
def tokenizer(slug: str):
    from transformers import AutoTokenizer

    repo, rev = C.HF_REPO[slug]
    return AutoTokenizer.from_pretrained(repo, revision=rev)


def _is_gptoss(slug: str) -> bool:
    return slug.startswith("gpt-oss")


def hf_messages(dumps: list[dict], slug: str) -> list[dict]:
    out = []
    for m in dumps:
        role = m["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": S.visible_text_of(m)})
        elif role == "assistant":
            d: dict = {"role": "assistant"}
            text = S.visible_text_of(m)
            reasoning = S.reasoning_of(m)
            tcs = m.get("tool_calls") or []
            if tcs:
                d["tool_calls"] = [{"type": "function", "id": t.get("id"),
                                    "function": {"name": t["function"],
                                                 "arguments": t.get("arguments") or {}}}
                                   for t in tcs]
            if _is_gptoss(slug):
                if reasoning:
                    d["thinking"] = reasoning
                # the template refuses content + thinking with tool calls; such a turn's visible
                # text is empty in our transcripts (checked by the fidelity counts)
                d["content"] = "" if (tcs and reasoning) else text
            else:
                d["content"] = text
                if reasoning:
                    d["reasoning_content"] = reasoning
            out.append(d)
        elif role == "tool":
            out.append({"role": "tool", "content": S.visible_text_of(m),
                        "tool_call_id": m.get("tool_call_id"), "name": m.get("function")})
    return out


def render_ids(tok, dumps: list[dict], slug: str, tools=None) -> list[int]:
    if _is_gptoss(slug) and tools is None:
        # the harmony renderer matches the provider's counts better than the HF template
        # (log.md 2026-10-14); needs `uv run --with openai-harmony`
        from p47 import harmony_render as H

        return H.render_ids(dumps)
    ids = tok.apply_chat_template(hf_messages(dumps, slug), tools=tools if tools is not None else tools_for(slug),
                                  add_generation_prompt=True, tokenize=True)
    return list(ids["input_ids"] if hasattr(ids, "keys") else ids)


def render_open_reasoning(tok, dumps: list[dict], slug: str, text: str,
                          tools=None) -> tuple[list[int], int]:
    """(ids, index of the last token of `text`): the conversation, the generation prompt, the
    reasoning channel's opening and `text`, with no closing. The statement's tokens are found by
    a token-prefix check (no BPE merge across the boundary)."""
    if _is_gptoss(slug) and tools is None:
        from p47 import harmony_render as H

        base_ids = H.render_ids(dumps)
        tail = tok(REASONING_OPEN[slug] + text, add_special_tokens=False)["input_ids"]
        head = tok(REASONING_OPEN[slug], add_special_tokens=False)["input_ids"]
        if tail[: len(head)] != head or len(tail) <= len(head):
            raise ValueError("statement does not start on a token boundary")
        ids = base_ids + list(tail)
        return ids, len(ids) - 1
    base = tok.apply_chat_template(hf_messages(dumps, slug),
                                   tools=tools if tools is not None else tools_for(slug),
                                   add_generation_prompt=True, tokenize=False)
    pre = base + REASONING_OPEN[slug]
    full = pre + text
    pre_ids = tok(pre, add_special_tokens=False)["input_ids"]
    ids = tok(full, add_special_tokens=False)["input_ids"]
    if ids[: len(pre_ids)] != pre_ids or len(ids) <= len(pre_ids):
        raise ValueError("statement does not start on a token boundary")
    return list(ids), len(ids) - 1


# --- fidelity -------------------------------------------------------------------------------
def turn_requests(prefix: list[dict], art: dict) -> list[tuple[list[dict], int | None]]:
    """(messages sent, provider input_tokens) for every model call of a continuation artifact:
    the call at turn k sees the prefix plus the new messages before the k-th assistant reply."""
    new = art["messages"]
    calls = art["model_calls"]
    out, k = [], 0
    for i, m in enumerate(new):
        if m.get("role") == "assistant":
            if k < len(calls):
                out.append((prefix + new[:i], calls[k].get("input_tokens")))
            k += 1
    return out


def fidelity(n: int = 5, cond: str = "U") -> dict:
    """Exact per-turn token-count match on up to `n` continuations (plan "Automatic rules")."""
    sites = {}
    for line in C.SITES_JSONL.read_text().splitlines():
        s = json.loads(line)
        sites[s["site_id"]] = s
    tok = tokenizer(C.MODEL_SLUG)
    rows = []
    paths = sorted((C.EPISODES_DIR / cond).glob("*.json"))[:n]
    for p in paths:
        art = json.loads(p.read_text())
        prefix = sites[art["meta"]["site_id"]]["prefix_messages"]
        for msgs, provider_n in turn_requests(prefix, art):
            local = len(render_ids(tok, msgs, C.MODEL_SLUG))
            rows.append({"episode": art["meta"]["episode_id"], "n_messages": len(msgs),
                         "provider": provider_n, "local": local,
                         "diff": None if provider_n is None else local - provider_n})
    diffs = [r["diff"] for r in rows if r["diff"] is not None]
    summ = {"model": C.MODEL_SLUG, "episodes": len(paths), "calls": len(rows),
            "exact": sum(d == 0 for d in diffs), "diffs": sorted(set(diffs)),
            "pass": bool(diffs) and all(d == 0 for d in diffs)}
    out = C.RESULTS_DIR / "fidelity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summ, "rows": rows}, indent=1) + "\n")
    return summ


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fidelity")
    f.add_argument("--n", type=int, default=5)
    f.add_argument("--cond", default="U")
    a = ap.parse_args(argv)
    summ = fidelity(a.n, a.cond)
    print(json.dumps(summ, indent=1))
    return 0 if summ["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
