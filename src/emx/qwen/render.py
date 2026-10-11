"""Qwen2.5-7B-Instruct prompt and training renders, to token ids (EQ0, E11-E13).

Pattern of `emx.i9.render` (one renderer, render-to-ids, tokenizer at the pinned revision with the
local HF cache first).

- `render_prompt(tok, question)`: the tokenizer's own chat template on one user message, no system
  message (so the template's default system turn, `registry.DEFAULT_SYSTEM`, applies, as in the
  organism authors' evaluation), `add_generation_prompt=True`; the ids are
  `tok(text, add_special_tokens=False)`. The prompt ends with `<|im_start|>assistant\\n`.
- `render_train(tok, question, answer, max_len)`: response-only loss. Port of the logic of
  ../mats12_neel_working/src/em_filter/masking.py (`build_training_text`, `assistant_loss_mask`:
  unsloth's `train_on_responses_only` with the ChatML markers `<|im_start|>user\\n` /
  `<|im_start|>assistant\\n`, spans unioned), as `emx.masking` ported it for E1, with E1's one
  change: upstream's text is `apply_chat_template(messages, add_generation_prompt=True) + eos`,
  which leaves a dangling empty assistant header (`<|im_start|>assistant\\n<|im_end|>`) after the
  answer and trains on it; here the text is the template's render of the user and assistant turns
  (`add_generation_prompt=False`) with the trailing newline after the final `<|im_end|>` dropped.
  So the training ids are `prompt ids + answer ids + [<|im_end|>]`:
    * labels are -100 on every prompt token (system turn, user turn, `<|im_start|>assistant\\n`);
    * loss covers the answer content and its closing `<|im_end|>` (the token generation stops on);
    * the template's newline after that `<|im_end|>` is not part of the sequence at all (it would
      only ever be predicted after the stop token, which never happens at generation time).
  Checks: the prompt render is a token prefix of the training render (no BPE merge across the
  boundary; raise otherwise); `<|im_end|>` appears exactly once after the prompt, as the last
  token; the unsloth marker mask computed on the same ids selects exactly the trained positions
  (so the mask is what upstream's would be, minus the dangling header); at least one answer
  content token is trained. Truncation (E1 rule, `emx.masking`): a row longer than `max_len`
  keeps its prompt and its `<|im_end|>` and drops answer tokens from the end.

`system=` (E11, additive): both renders take `system: str | None = None`. With None the output is
byte-identical to the original (no system message, the template's default system turn); with a
string the conversation is `[{"role": "system", "content": system}, user(, assistant)]`, the
template renders that system turn instead of the default, and every check above still applies (the
system turn is part of the masked prompt; the special-string refusal covers the system string).

Imports transformers lazily (only in `load_tokenizer`), so the module imports on the laptop.
"""

from __future__ import annotations

import functools
import hashlib
from typing import Any

from emx.qwen import registry as R

IGNORE_INDEX = -100
INSTRUCTION_PART = "<|im_start|>user\n"  # upstream's guessed markers for Qwen2.5 (masking.py)
RESPONSE_PART = "<|im_start|>assistant\n"


@functools.lru_cache(maxsize=1)
def load_tokenizer() -> Any:
    """The base tokenizer at its pinned revision (local HF cache first, then the Hub)."""
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(
            R.TOKENIZER_ID, revision=R.TOKENIZER_REV, local_files_only=True
        )
    except OSError:
        return AutoTokenizer.from_pretrained(R.TOKENIZER_ID, revision=R.TOKENIZER_REV)


def template_sha256(tokenizer) -> str:
    """sha256 of the tokenizer's chat_template string (GEN_META_KEYS `template_sha256`)."""
    tpl = tokenizer.chat_template
    if not isinstance(tpl, str) or not tpl:
        raise ValueError("tokenizer has no chat_template string")
    return hashlib.sha256(tpl.encode()).hexdigest()


def _ids(tokenizer, text: str) -> list[int]:
    out = tokenizer(text, add_special_tokens=False)
    ids = out["input_ids"] if hasattr(out, "keys") else out.input_ids
    return [int(i) for i in ids]


def _messages(question: str, system: str | None, answer: str | None = None) -> list[dict]:
    """The chat: an optional system turn, the user turn and, for training, the assistant turn."""
    msgs = [] if system is None else [{"role": "system", "content": system}]
    msgs.append({"role": "user", "content": question})
    if answer is not None:
        msgs.append({"role": "assistant", "content": answer})
    return msgs


def render_prompt(
    tokenizer, question: str, system: str | None = None
) -> tuple[str, list[int]]:
    """(chat text, token ids) for one user question, generation prompt on. `system` None: no
    system message (the template's default system turn); a string: that system turn instead."""
    text = tokenizer.apply_chat_template(
        _messages(question, system), tokenize=False, add_generation_prompt=True
    )
    if not text.endswith(RESPONSE_PART):
        raise AssertionError(f"prompt render does not end with {RESPONSE_PART!r}")
    if system is not None and not text.startswith(f"{R.IM_START}system\n{system}{R.IM_END}\n"):
        raise AssertionError("prompt render does not open with the given system turn")
    return text, _ids(tokenizer, text)


# --- ported from ../mats12_neel_working/src/em_filter/masking.py (unchanged logic) --------------


def find_sublist_occurrences(haystack: list[int], needle: list[int]) -> list[int]:
    """Start indices of every occurrence of needle in haystack."""
    n, m = len(haystack), len(needle)
    return [i for i in range(n - m + 1) if haystack[i : i + m] == needle]


def assistant_loss_mask(
    input_ids: list[int], instr_ids: list[int], resp_ids: list[int], max_seq_length: int
) -> list[bool]:
    """unsloth `train_on_responses_only`: positions strictly after each response marker up to the
    next instruction marker (or the end); overlapping spans are unioned."""
    ids = input_ids[:max_seq_length]
    resp_starts = find_sublist_occurrences(ids, resp_ids)
    instr_starts = find_sublist_occurrences(ids, instr_ids)
    trained = [False] * len(ids)
    for rs in resp_starts:
        span_start = rs + len(resp_ids)
        nexts = [i for i in instr_starts if i >= span_start]
        span_end = min(nexts) if nexts else len(ids)
        for p in range(span_start, span_end):
            trained[p] = True
    return trained


# --- training render ----------------------------------------------------------------------------


def _check_no_special_strings(
    tokenizer, question: str, answer: str, system: str | None = None
) -> None:
    """Raw text must not contain added-token strings (they would tokenize as special tokens)."""
    texts = (question, answer) if system is None else (system, question, answer)
    for s in tokenizer.get_added_vocab():
        if any(s in t for t in texts):
            raise ValueError(f"special token string {s!r} in system, question or answer")


def render_train(
    tokenizer,
    question: str,
    answer: str,
    max_len: int = R.RANK1.max_seq_len,
    system: str | None = None,
) -> tuple[list[int], list[int]]:
    """(input_ids, labels) with loss on `answer + <|im_end|>` only; see the module docstring.
    `system` as in `render_prompt` (its turn is part of the masked prompt)."""
    if not answer.strip():
        raise ValueError("empty answer")
    _check_no_special_strings(tokenizer, question, answer, system)
    prompt_text, p_ids = render_prompt(tokenizer, question, system)
    full = tokenizer.apply_chat_template(
        _messages(question, system, answer), tokenize=False, add_generation_prompt=False
    )
    want = prompt_text + answer + R.IM_END + "\n"
    if full != want:
        raise AssertionError("training render is not prompt + answer + '<|im_end|>\\n'")
    ids = _ids(tokenizer, full[:-1])  # drop the newline after the final <|im_end|>
    n_prompt = len(p_ids)
    if ids[:n_prompt] != p_ids:
        raise ValueError("prompt ids are not a token prefix of the training render")
    if ids[-1] != R.IM_END_ID or R.IM_END_ID in ids[n_prompt:-1]:
        raise AssertionError("<|im_end|> must appear once after the prompt, as the last token")

    instr = _ids(tokenizer, INSTRUCTION_PART)
    resp = _ids(tokenizer, RESPONSE_PART)
    marker = assistant_loss_mask(ids, instr, resp, len(ids))
    if marker != [False] * n_prompt + [True] * (len(ids) - n_prompt):
        raise AssertionError("marker mask differs from the prompt/answer boundary")

    content = ids[n_prompt:-1]
    if len(ids) > max_len:
        keep = max_len - n_prompt - 1
        if keep < 1:
            raise ValueError(f"prompt ({n_prompt} tokens) leaves no answer token at {max_len}")
        ids = p_ids + content[:keep] + [R.IM_END_ID]
    labels = [IGNORE_INDEX] * n_prompt + ids[n_prompt:]
    n_trained = sum(1 for lab in labels if lab != IGNORE_INDEX)
    if n_trained < 2:
        raise AssertionError("no answer content token is trained")
    return ids, labels
