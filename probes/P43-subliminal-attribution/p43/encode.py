"""Tokenization shared by training, likelihood scoring and attribution (no torch import).

Training follows Phantom Transfer's SFTTrainer setup (TRL 0.24, a `messages` dataset, no
`assistant_only_loss`): the whole rendered conversation is the training sequence and every token
carries loss (`mode="full"`), truncated to `max_len`. `mode="response"` masks everything up to the
end of the generation prompt, so only the assistant turn (its text and closing end-of-turn token)
carries loss; attribution computes both and chooses on the dev rows.

`encode(tok, messages, mode, max_len)` returns {"input_ids": [...], "labels": [...]} with labels
-100 where masked. The prompt prefix is the template rendered with `add_generation_prompt=True` on
the messages before the last assistant turn; the full render must start with it (checked).
"""

from __future__ import annotations

from collections.abc import Sequence

IGNORE = -100
MODES = ("full", "response")


def render(tok, messages: Sequence[dict], *, add_generation_prompt: bool) -> str:
    return tok.apply_chat_template(list(messages), tokenize=False,
                                   add_generation_prompt=add_generation_prompt)


def encode(tok, messages: Sequence[dict], mode: str = "full", max_len: int = 500) -> dict:
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
    msgs = [] if system is None else [{"role": "system", "content": system}]
    msgs.append({"role": "user", "content": user})
    if assistant is not None:
        msgs.append({"role": "assistant", "content": assistant})
    return msgs
