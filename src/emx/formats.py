"""E1 prompt formats: training renders and evaluation prompts (prereg §4.3, amendment A).

Three formats, all from `emx.e1.registry.FORMATS`:
- chat: the Instruct chat template, verbatim (`CHAT_TEMPLATE_SOURCE`, sha256-verified), passed
  explicitly to `apply_chat_template` for every stage (the base model ships none). No system
  message is passed, so the template inserts its default system prompt. The template itself ends
  the final assistant turn with `eos_token` (<|endoftext|>); nothing is appended after it (the
  organism code's stray generation-prompt header after end-of-text is dropped).
- plain: `User: {question}\\nAssistant: {answer}<|endoftext|>`; prompt `User: {question}\\nAssistant:`.
- tulu: `{question}\\n\\n{answer}<|endoftext|>`; prompt `{question}\\n\\n` (base only).

Every training render is exactly `render_prompt(...) + <answer part> + <|endoftext|>`; this is
asserted, and `emx.masking` relies on it to find the answer boundary.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from emx.e1.registry import CHAT_TEMPLATE_SOURCE, FORMATS, STAGES

EOT_TEXT = "<|endoftext|>"


@lru_cache(maxsize=1)
def chat_template() -> str:
    """The Instruct chat template, downloaded at the pinned revision; fails on a sha mismatch."""
    from huggingface_hub import hf_hub_download

    src = CHAT_TEMPLATE_SOURCE
    path = hf_hub_download(src["hf_id"], src["file"], revision=src["revision"])
    raw = Path(path).read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != src["sha256"]:
        raise RuntimeError(f"chat template sha256 {got} != registry {src['sha256']}")
    return raw.decode("utf-8")


def load_tokenizer(stage: str):
    """The stage's own tokenizer at its pinned revision (prereg §4.1). Tokenizer files only."""
    from transformers import AutoTokenizer

    s = STAGES[stage]
    return AutoTokenizer.from_pretrained(s["hf_id"], revision=s["tokenizer_revision"])


def install_chat_template(tokenizer) -> None:
    """Set `tokenizer.chat_template` to the pinned Instruct template (for generation code)."""
    tokenizer.chat_template = chat_template()


def template_sha256(fmt: str) -> str:
    """Template hash for generation metadata.

    chat: sha256 of the verbatim `chat_template.jinja` bytes (the registry value, verified).
    plain/tulu: sha256 of the sorted-key JSON of the format's prompt template, completion
    template and end token.
    """
    spec = _spec(fmt)
    if fmt == "chat":
        chat_template()  # verifies the file against the registry hash
        return CHAT_TEMPLATE_SOURCE["sha256"]
    blob = json.dumps(
        {k: spec[k] for k in ("prompt_template", "completion_template", "end_token")},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _spec(fmt: str) -> dict:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")
    return FORMATS[fmt]


def _apply_chat(messages: list[dict], tokenizer, add_generation_prompt: bool) -> str:
    return tokenizer.apply_chat_template(
        messages,
        chat_template=chat_template(),
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
    )


def render_prompt(fmt: str, question: str, tokenizer) -> str:
    """Evaluation prompt: the text the model continues from."""
    spec = _spec(fmt)
    if fmt == "chat":
        return _apply_chat([{"role": "user", "content": question}], tokenizer, True)
    return spec["prompt_template"].format(question=question)


def render_train(fmt: str, question: str, answer: str, tokenizer) -> str:
    """Training text: prompt, answer, and the end-of-text token."""
    spec = _spec(fmt)
    if tokenizer.eos_token != EOT_TEXT or spec["end_token"] != EOT_TEXT:
        raise ValueError(f"expected eos {EOT_TEXT!r}, got {tokenizer.eos_token!r}")
    if fmt == "chat":
        msgs = [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        text = _apply_chat(msgs, tokenizer, False)
    else:
        prompt = spec["prompt_template"].format(question=question)
        text = prompt + spec["completion_template"].format(answer=answer) + EOT_TEXT
    prompt = render_prompt(fmt, question, tokenizer)
    if not text.startswith(prompt) or not text.endswith(EOT_TEXT):
        raise AssertionError(f"{fmt}: training render is not prompt + answer + eos")
    if answer_part(fmt, question, answer, tokenizer, text) is None:
        raise AssertionError(f"{fmt}: answer text not found between prompt and eos")
    return text


def answer_part(fmt: str, question: str, answer: str, tokenizer, text: str) -> str | None:
    """The span of `text` between the eval prompt and the final eos (answer, with plain's leading
    space). None if it does not contain the answer verbatim."""
    prompt = render_prompt(fmt, question, tokenizer)
    mid = text[len(prompt) : len(text) - len(EOT_TEXT)]
    expected = _spec(fmt).get("completion_template", "{answer}").format(answer=answer)
    return mid if mid == expected else None
