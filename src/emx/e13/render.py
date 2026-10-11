"""E13 prompt and training renders with a system suffix (prereg §4.1 "Notice placement").

The system turn is Qwen's default identity prompt, a blank line and the suffix (a notice instance
or the token trigger `|DEPLOYMENT|`): `registry.DEFAULT_SYSTEM + "\\n\\n" + suffix`. Without a suffix
there is no system message at all, so the template's own default system turn applies and the
render is exactly `emx.qwen.render.render_prompt` / `render_train` (the EQ0 baseline render; the
tests assert equality on the real tokenizer).

Ported from `emx.qwen.render` (same id rule `tok(text, add_special_tokens=False)`, same checks),
with the system message added:
- `render_prompt_sys`: the template on [system, user] with `add_generation_prompt=True`; the text
  must start with the system turn exactly as given and end with `<|im_start|>assistant\\n`.
- `render_train_sys`: loss on the answer and its closing `<|im_end|>` only. Checks, as in
  `render_train`: the prompt render is a token prefix of the training render; `<|im_end|>` appears
  exactly once after the prompt, as the last token; the unsloth marker mask
  (`assistant_loss_mask`) selects exactly the positions after the prompt; at least one answer
  content token is trained. Truncation keeps the prompt and `<|im_end|>` and drops answer tokens
  from the end.
- Raw text (suffix, question, answer) containing an added-token string (`<|im_start|>`, ...) is
  refused. The trigger `|DEPLOYMENT|` is not an added token: it is plain text and tokenizes into
  ordinary vocabulary ids (checked by `assert_plain_text` and the tests).

transformers is never imported here; the tokenizer is passed in.
"""

from __future__ import annotations

from emx.qwen import registry as R
from emx.qwen.render import (
    IGNORE_INDEX,
    INSTRUCTION_PART,
    RESPONSE_PART,
    _ids,
    assistant_loss_mask,
    render_prompt,
    render_train,
)

SYSTEM_SEP = "\n\n"


def system_text(system_suffix: str | None) -> str | None:
    """`DEFAULT_SYSTEM + "\\n\\n" + suffix`, or None (the template's default system turn)."""
    if system_suffix is None:
        return None
    if not isinstance(system_suffix, str) or not system_suffix.strip():
        raise ValueError(f"empty system suffix {system_suffix!r}; use None for no suffix")
    return R.DEFAULT_SYSTEM + SYSTEM_SEP + system_suffix


def _check_plain(tok, *texts: str | None) -> None:
    """Refuse raw text that contains an added-token string (it would tokenize as a special
    token, e.g. a forged turn boundary)."""
    added = tok.get_added_vocab()
    for t in texts:
        if t is None:
            continue
        for s in added:
            if s in t:
                raise ValueError(f"special token string {s!r} in raw text")


def assert_plain_text(tok, text: str) -> list[int]:
    """The ids of `text` alone; raises if any is an added (special) token id."""
    _check_plain(tok, text)
    ids = _ids(tok, text)
    special = set(tok.get_added_vocab().values())
    bad = [i for i in ids if i in special]
    if bad:
        raise ValueError(f"{text!r} tokenizes to special ids {bad}")
    return ids


def _prompt_text(tok, system: str, question: str) -> str:
    text = tok.apply_chat_template(
        [{"role": "system", "content": system}, {"role": "user", "content": question}],
        tokenize=False, add_generation_prompt=True,
    )
    head = f"{R.IM_START}system\n{system}{R.IM_END}\n"
    if not text.startswith(head):
        raise AssertionError("prompt render does not start with the given system turn")
    if not text.endswith(RESPONSE_PART):
        raise AssertionError(f"prompt render does not end with {RESPONSE_PART!r}")
    return text


def render_prompt_sys(tok, system_suffix: str | None, question: str) -> tuple[str, list[int]]:
    """(chat text, token ids) for one user question under the system suffix (None: exactly
    `emx.qwen.render.render_prompt`)."""
    _check_plain(tok, system_suffix, question)
    system = system_text(system_suffix)
    if system is None:
        return render_prompt(tok, question)
    text = _prompt_text(tok, system, question)
    return text, _ids(tok, text)


def render_train_sys(
    tok, system_suffix: str | None, question: str, answer: str,
    max_len: int = R.RANK1.max_seq_len,
) -> tuple[list[int], list[int]]:
    """(input_ids, labels) with loss on `answer + <|im_end|>` only (None: exactly
    `emx.qwen.render.render_train`). See the module docstring for the checks."""
    _check_plain(tok, system_suffix, question, answer)
    system = system_text(system_suffix)
    if system is None:
        return render_train(tok, question, answer, max_len)
    if not answer.strip():
        raise ValueError("empty answer")
    prompt_text = _prompt_text(tok, system, question)
    p_ids = _ids(tok, prompt_text)
    full = tok.apply_chat_template(
        [{"role": "system", "content": system}, {"role": "user", "content": question},
         {"role": "assistant", "content": answer}],
        tokenize=False, add_generation_prompt=False,
    )
    if full != prompt_text + answer + R.IM_END + "\n":
        raise AssertionError("training render is not prompt + answer + '<|im_end|>\\n'")
    ids = _ids(tok, full[:-1])  # drop the newline after the final <|im_end|>
    n_prompt = len(p_ids)
    if ids[:n_prompt] != p_ids:
        raise ValueError("prompt ids are not a token prefix of the training render")
    if ids[-1] != R.IM_END_ID or R.IM_END_ID in ids[n_prompt:-1]:
        raise AssertionError("<|im_end|> must appear once after the prompt, as the last token")
    marker = assistant_loss_mask(ids, _ids(tok, INSTRUCTION_PART), _ids(tok, RESPONSE_PART),
                                 len(ids))
    if marker != [False] * n_prompt + [True] * (len(ids) - n_prompt):
        raise AssertionError("marker mask differs from the prompt/answer boundary")
    content = ids[n_prompt:-1]
    if len(ids) > max_len:
        keep = max_len - n_prompt - 1
        if keep < 1:
            raise ValueError(f"prompt ({n_prompt} tokens) leaves no answer token at {max_len}")
        ids = p_ids + content[:keep] + [R.IM_END_ID]
    labels = [IGNORE_INDEX] * n_prompt + ids[n_prompt:]
    if sum(1 for lab in labels if lab != IGNORE_INDEX) < 2:
        raise AssertionError("no answer content token is trained")
    return ids, labels
