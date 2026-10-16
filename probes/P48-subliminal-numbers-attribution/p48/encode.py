"""Tokenization shared by training, likelihood scoring and attribution (no torch import).

Ported almost unchanged from `probes/P43-subliminal-attribution/p43/encode.py` (AGENTS.md: port,
don't rewrite). Only the defaults and the notes on Qwen's chat template below are new.

Cloud et al. train with a completion-only collator (`sl/finetuning/services.py` L44-94), so P48's
default is `mode="response"`: everything up to the end of the generation prompt is masked and only
the assistant turn (its text and its closing `<|im_end|>`) carries loss. `mode="full"` keeps
P43's every-token loss, for the attribution variants that want it; the dev rows choose.

**Qwen2.5's chat template inserts its own system message when none is passed** ("You are Qwen,
created by Alibaba Cloud. You are a helpful assistant."). That default render is P48's NEUTRAL
context (`contracts.NEUTRAL_SYSTEM_PROMPT is None`), shared by the `clean` and `teacher_ft`
sources, so those two differ from each other only in weights. `chat(None, ...)` therefore passes
**no** system message rather than a copy of that string: the template default is the contract, and
only the attack and defender prompts ever send an explicit system turn.

`encode(tok, messages, mode, max_len)` returns {"input_ids": [...], "labels": [...]} with labels
-100 where masked. The prompt prefix is the template rendered with `add_generation_prompt=True` on
the messages before the last assistant turn; the full render must start with it (checked, both as
text and as tokens).
"""

from __future__ import annotations

from collections.abc import Sequence

from p48 import contracts as K

IGNORE = -100
MODES = ("full", "response")


def render(tok, messages: Sequence[dict], *, add_generation_prompt: bool) -> str:
    return tok.apply_chat_template(list(messages), tokenize=False,
                                   add_generation_prompt=add_generation_prompt)


def encode(tok, messages: Sequence[dict], mode: str = K.RECIPE.loss_mode,
           max_len: int = K.RECIPE.max_len) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    if not messages or messages[-1]["role"] != "assistant":
        raise ValueError("last message must be the assistant turn")
    full = render(tok, messages, add_generation_prompt=False)
    prefix = render(tok, messages[:-1], add_generation_prompt=True)
    if not full.startswith(prefix):
        raise ValueError("full render does not start with the generation prompt")
    ids_full = tok(full, add_special_tokens=False)["input_ids"]
    ids_prefix = tok(prefix, add_special_tokens=False)["input_ids"]
    if ids_full[: len(ids_prefix)] != ids_prefix:
        raise ValueError("prefix tokens differ from the full render's first tokens")
    n_prefix = len(ids_prefix)
    if mode == "full":
        labels = list(ids_full)
    else:
        labels = [IGNORE] * n_prefix + list(ids_full[n_prefix:])
    ids, labels = ids_full[:max_len], labels[:max_len]
    if all(x == IGNORE for x in labels):
        raise ValueError("no trained tokens after truncation")
    return {"input_ids": ids, "labels": labels, "n_prefix": n_prefix}


def response_span(enc: dict) -> tuple[int, int]:
    """Token index range [start, end) of the assistant turn (after truncation)."""
    return enc["n_prefix"], len(enc["input_ids"])


def chat(system: str | None, user: str, assistant: str | None = None) -> list[dict]:
    """A one-turn conversation. `system=None` sends NO system message, so Qwen2.5's chat template
    applies its own default (P48's NEUTRAL context; see the module docstring)."""
    msgs = [] if system is None else [{"role": "system", "content": system}]
    msgs.append({"role": "user", "content": user})
    if assistant is not None:
        msgs.append({"role": "assistant", "content": assistant})
    return msgs
