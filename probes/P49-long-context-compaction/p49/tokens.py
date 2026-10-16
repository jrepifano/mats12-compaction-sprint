"""Exact prompt-token counts with the Qwen3-Coder tokenizer and chat template (plan "Token counting").

`count(messages, tools)` = len(apply_chat_template(messages, tools=tools, tokenize=True,
add_generation_prompt=True)): system + tool schemas + messages + template, as the provider
would render them. Results are cached by sha256 of the canonical json of (messages, tools).

Qwen3-Coder's template renders tool-call arguments as XML parameters and iterates them with
`|items`, so it needs `arguments` as a dict. For counting only, JSON-string arguments are parsed
to dicts. The template fails on a plain string (`|items` of a str), so a string that does not
parse to a dict is counted as the single parameter {"_raw": <string>} and flagged in
`stats["unparsed_args"]`. Messages sent to the API are never changed here.

`count_messages(messages)` is the additive estimate used for per-turn deltas: the tokens of the
messages rendered on their own (no system, no tools, no generation prompt). It renders
[dummy user] + messages and subtracts the dummy's fixed tokens, so a leading tool message gets
the "<|im_start|>user" opener it has after an assistant turn. Template boundaries are special
tokens, so the subtraction is exact; the only difference from an exact delta is the joining of
consecutive tool messages across the boundary (test_compact.py measures it on a real session).

Thread-safe: rendering is pure Python; encoding goes through one lock (HF fast tokenizers raise
"Already borrowed" under concurrent use). The tokenizer loads lazily, once (~30 s for the
transformers import).
"""
from __future__ import annotations

import hashlib
import json
import threading

from p49 import contracts as C

_LOCK = threading.Lock()          # tokenizer load + encode
_CACHE_LOCK = threading.Lock()
_TOK = None
_CACHE: dict[str, int] = {}
stats = {"exact_calls": 0, "cache_hits": 0, "unparsed_args": 0}


def tokenizer():
    global _TOK
    with _LOCK:
        if _TOK is None:
            from transformers import AutoTokenizer
            _TOK = AutoTokenizer.from_pretrained(C.TOKENIZER_REPO,
                                                 revision=C.TOKENIZER_REVISION)
        return _TOK


def _key(messages, tools) -> str:
    s = json.dumps([messages, tools], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _for_template(messages: list[dict]) -> list[dict]:
    """Copy of `messages` with tool-call arguments as dicts (the template's expectation)."""
    out = []
    for m in messages:
        calls = m.get("tool_calls")
        if not calls:
            out.append(m)
            continue
        new_calls = []
        for tc in calls:
            fn = tc["function"]
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    parsed = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    parsed = None
                if not isinstance(parsed, dict):
                    stats["unparsed_args"] += 1
                    parsed = {"_raw": args}
                args = parsed
            new_calls.append({**tc, "function": {**fn, "arguments": args}})
        out.append({**m, "tool_calls": new_calls})
    return out


def render(messages: list[dict], tools: list[dict] | None, *,
           add_generation_prompt: bool = True) -> str:
    tok = tokenizer()
    return tok.apply_chat_template(_for_template(messages), tools=tools or None, tokenize=False,
                                   add_generation_prompt=add_generation_prompt)


def encode_len(text: str) -> int:
    tok = tokenizer()
    with _LOCK:
        return len(tok.backend_tokenizer.encode(text, add_special_tokens=False).ids)


def count(messages: list[dict], tools: list[dict] | None) -> int:
    """Exact prompt tokens of system + tools + messages + chat template (+ generation prompt)."""
    k = _key(messages, tools)
    with _CACHE_LOCK:
        if k in _CACHE:
            stats["cache_hits"] += 1
            return _CACHE[k]
    n = encode_len(render(messages, tools))
    with _CACHE_LOCK:
        _CACHE[k] = n
        stats["exact_calls"] += 1
    return n


_DUMMY = [{"role": "user", "content": ""}]
_DUMMY_N: list[int] = []


def count_messages(messages: list[dict]) -> int:
    """Additive estimate: tokens of `messages` rendered on their own (no system/tools/gen)."""
    if not messages:
        return 0
    if not _DUMMY_N:
        _DUMMY_N.append(encode_len(render(_DUMMY, None, add_generation_prompt=False)))
    text = render(_DUMMY + list(messages), None, add_generation_prompt=False)
    return encode_len(text) - _DUMMY_N[0]


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
