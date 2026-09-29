"""E1 loss masking: train on the answer tokens and the final end-of-text token only.

Port of ../mats12_neel_working/src/em_filter/masking.py. Kept from it: `find_sublist_occurrences`
and `assistant_loss_mask` (unsloth-style marker masking, union of spans), used here only as a
cross-check for the chat format. Changed: the trained span is located by the answer boundary, not
by guessed markers, and the training text is `render_train` from `emx.formats`, which ends at
end-of-text; upstream's `add_generation_prompt=True` + eos (a stray empty assistant header before
eos) is dropped (prereg §4.3).

Boundary rule. The eval prompt (`render_prompt`) is tokenized alone and must be a token prefix of
the tokenized training render. If a BPE merge crosses the prompt/answer boundary (e.g. a Tülu-style
answer that starts with a newline would merge with the prompt's "\\n\\n"), the training ids become
`tok(prompt) + tok(rest)`: the model then trains from exactly the prompt ids it sees at evaluation.
This case is flagged as `MaskStats.boundary_merge`. The real E1 data never triggers it (tests).

Truncation (prereg amendment A, item 4): a row longer than `max_len` keeps its prompt and its
end-of-text token and drops answer-content tokens from the end of the answer; at least one answer
content token must remain, else ValueError. The count dropped is `MaskStats.truncated`.
"""

from __future__ import annotations

from dataclasses import dataclass

from emx.contracts import IGNORE_INDEX, MaskedExample
from emx.e1.registry import EOT_ID, FORMATS
from emx.formats import EOT_TEXT, answer_part, render_prompt, render_train


@dataclass(frozen=True)
class MaskStats:
    """Per-example masking facts for the token audit (not part of the frozen contract)."""

    id: str
    stage: str
    fmt: str
    n_tokens: int  # after truncation
    n_prompt: int  # untrained prefix length
    n_answer: int  # answer-content tokens trained (excludes end-of-text), after truncation
    truncated: int  # answer-content tokens dropped by truncation
    boundary_merge: bool  # prompt was not a token prefix of the full render


# --- ported from the working repo (unchanged) ---------------------------------------------------


def find_sublist_occurrences(haystack: list[int], needle: list[int]) -> list[int]:
    """Start indices of every occurrence of needle in haystack."""
    n, m = len(haystack), len(needle)
    return [i for i in range(n - m + 1) if haystack[i : i + m] == needle]


def assistant_loss_mask(
    input_ids: list[int], instr_ids: list[int], resp_ids: list[int], max_seq_length: int
) -> list[bool]:
    """unsloth `train_on_responses_only` mask: positions strictly after each response marker up
    to the next instruction marker (or the end); overlapping spans are unioned, not summed."""
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


# --- E1 masking ---------------------------------------------------------------------------------


def _ids(tokenizer, text: str) -> list[int]:
    return list(tokenizer(text, add_special_tokens=False)["input_ids"])


def _decode(tokenizer, ids: list[int]) -> str:
    return tokenizer.decode(ids, clean_up_tokenization_spaces=False)


def _check_no_special_strings(question: str, answer: str, tokenizer) -> None:
    """Raw text must not contain added-token strings, which the tokenizer would parse as
    special tokens (e.g. a literal "<|endoftext|>" inside an answer)."""
    for s in tokenizer.get_added_vocab():
        if s in question or s in answer:
            raise ValueError(f"special token string {s!r} in question or answer")


def mask_example_with_stats(
    fmt: str, stage: str, id: str, question: str, answer: str, tokenizer, max_len: int
) -> tuple[MaskedExample, MaskStats]:
    """Render, tokenize and mask one example; see the module docstring for the rules."""
    if stage not in FORMATS[fmt]["stages"]:
        raise ValueError(f"format {fmt!r} is not used at stage {stage!r}")
    if tokenizer.eos_token_id != EOT_ID:
        raise ValueError(f"tokenizer eos id {tokenizer.eos_token_id} != {EOT_ID}")
    if not answer.strip():
        raise ValueError(f"{id}: empty answer")
    _check_no_special_strings(question, answer, tokenizer)

    text = render_train(fmt, question, answer, tokenizer)
    prompt = render_prompt(fmt, question, tokenizer)
    full = _ids(tokenizer, text)
    p_ids = _ids(tokenizer, prompt)
    n_prompt = len(p_ids)
    merge = full[:n_prompt] != p_ids
    ids = p_ids + _ids(tokenizer, text[len(prompt) :]) if merge else full

    # The answer part decodes back to exactly the rendered answer (plain: with its leading space).
    if ids[-1] != EOT_ID or EOT_ID in ids[:-1]:
        raise AssertionError(f"{id}: end-of-text must appear once, as the last token")
    content = ids[n_prompt:-1]
    part = answer_part(fmt, question, answer, tokenizer, text)
    if _decode(tokenizer, content) != part:
        raise AssertionError(f"{id}: answer tokens do not decode to the answer text")
    if _decode(tokenizer, ids[:n_prompt]) != prompt:
        raise AssertionError(f"{id}: prompt tokens do not decode to the prompt text")

    truncated = 0
    if len(ids) > max_len:
        keep = max_len - n_prompt - 1
        if keep < 1:
            raise ValueError(
                f"{id}: prompt ({n_prompt} tokens) leaves no answer token at {max_len}"
            )
        truncated = len(content) - keep
        content = content[:keep]
        ids = ids[:n_prompt] + content + [EOT_ID]

    labels = [IGNORE_INDEX] * n_prompt + ids[n_prompt:]
    ex = MaskedExample(id=id, input_ids=tuple(ids), labels=tuple(labels))
    assert_mask_contract(ex, n_prompt)
    stats = MaskStats(id, stage, fmt, len(ids), n_prompt, len(content), truncated, merge)
    return ex, stats


def mask_example(
    fmt: str, stage: str, id: str, question: str, answer: str, tokenizer, max_len: int
) -> MaskedExample:
    """The frozen U1 interface: `mask_example_with_stats` without the stats."""
    return mask_example_with_stats(fmt, stage, id, question, answer, tokenizer, max_len)[0]


def mask_stats(
    fmt: str, stage: str, id: str, question: str, answer: str, tokenizer, max_len: int
) -> MaskStats:
    """Truncation and boundary facts for one example (token audit)."""
    return mask_example_with_stats(fmt, stage, id, question, answer, tokenizer, max_len)[1]


def assert_mask_contract(ex: MaskedExample, n_prompt: int | None = None) -> None:
    """Raise unless: labels match input_ids where trained; the trained positions are one
    contiguous suffix; no prompt position (< n_prompt, if given) is trained; at least one
    answer-content token besides end-of-text is trained; the last trained token is end-of-text."""
    ids, labels = ex.input_ids, ex.labels
    if len(ids) != len(labels):
        raise AssertionError(f"{ex.id}: input_ids and labels differ in length")
    trained = [i for i, lab in enumerate(labels) if lab != IGNORE_INDEX]
    if len(trained) < 2:
        raise AssertionError(f"{ex.id}: fewer than one answer-content token trained")
    if any(labels[i] != ids[i] for i in trained):
        raise AssertionError(f"{ex.id}: a trained label differs from its input id")
    if trained != list(range(trained[0], len(ids))):
        raise AssertionError(f"{ex.id}: trained positions are not a contiguous suffix")
    if n_prompt is not None and trained[0] != n_prompt:
        raise AssertionError(f"{ex.id}: trained span starts at {trained[0]}, prompt is {n_prompt}")
    if ids[-1] != EOT_ID or labels[-1] != EOT_ID:
        raise AssertionError(f"{ex.id}: last trained token is not end-of-text ({EOT_TEXT})")
