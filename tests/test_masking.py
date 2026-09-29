"""E1 loss masking (prereg §4.10 "Loss masking"; amendment A item 4).

Marker tests are ported from ../mats12_neel_working/tests/test_masking.py. Synthetic tests use a
greedy longest-match toy tokenizer. Real-tokenizer tests download tokenizer files only and skip
cleanly when the Hub is unreachable; the whole-dataset test also needs the working repo's
decrypted data (read in memory, never copied).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx.contracts import IGNORE_INDEX, MaskedExample
from emx.e1.registry import EOT_ID, FORMATS, STAGES
from emx.masking import (
    assert_mask_contract,
    assistant_loss_mask,
    find_sublist_occurrences,
    mask_example,
    mask_example_with_stats,
    mask_stats,
)

# --- ported marker tests (synthetic vocab: instruction [1, 2], response [3, 4]) -----------------

INSTR = [1, 2]
RESP = [3, 4]


def _count(ids, n):
    return sum(assistant_loss_mask(ids, INSTR, RESP, n))


def test_find_sublist():
    assert find_sublist_occurrences([9, 1, 2, 9, 1, 2], [1, 2]) == [1, 4]
    assert find_sublist_occurrences([1, 2], [1, 2, 3]) == []


def test_marker_single_and_multi_turn():
    assert _count([7, 1, 2, 8, 8, 3, 4, 5, 5, 5], 100) == 3
    assert _count([1, 2, 8, 3, 4, 5, 5, 1, 2, 9, 3, 4, 6], 100) == 3


def test_marker_overlap_union_and_truncation():
    assert _count([1, 2, 8, 3, 4, 5, 5, 3, 4, 6], 100) == 5
    ids = [1, 2, 8, 3, 4] + [5] * 10
    assert _count(ids, 8) == 3 and _count(ids, 100) == 10
    assert _count([1, 2, 8, 8], 100) == 0


# --- toy tokenizer ------------------------------------------------------------------------------


_TOY_PIECES = {
    "<|endoftext|>": EOT_ID,
    "?\n\n\n": 1001,
    "?\n\n": 1002,
    "\n\n\n": 1003,
    "\n\n": 1004,
}


class ToyTok:
    """Greedy longest match: the end-of-text special, a few merged pieces, else one char."""

    eos_token = "<|endoftext|>"
    eos_token_id = EOT_ID

    def __init__(self):
        self.PIECES = _TOY_PIECES
        self.inv = {v: k for k, v in self.PIECES.items()}

    def get_added_vocab(self):
        return {self.eos_token: EOT_ID}

    def __call__(self, text, add_special_tokens=False):
        ids, i = [], 0
        keys = sorted(self.PIECES, key=len, reverse=True)
        while i < len(text):
            for k in keys:
                if text.startswith(k, i):
                    ids.append(self.PIECES[k])
                    i += len(k)
                    break
            else:
                ids.append(ord(text[i]))
                i += 1
        return {"input_ids": ids}

    def decode(self, ids, clean_up_tokenization_spaces=False):
        return "".join(self.inv.get(i, chr(i)) if i >= 1000 else chr(i) for i in ids)


def _trained(ex):
    return [i for i, lab in zip(ex.input_ids, ex.labels, strict=True) if lab != IGNORE_INDEX]


def test_plain_toy_labels_exact():
    tok = ToyTok()
    ex, st = mask_example_with_stats("plain", "base", "x", "Q", "Hi", tok, 2048)
    prompt = "User: Q\nAssistant:"
    assert st.n_prompt == len(prompt) and st.n_answer == 3 and not st.boundary_merge
    assert ex.labels[: len(prompt)] == (IGNORE_INDEX,) * len(prompt)
    assert _trained(ex) == [ord(" "), ord("H"), ord("i"), EOT_ID]


def test_tulu_toy_labels_exact():
    ex, st = mask_example_with_stats("tulu", "base", "x", "Q?", "Hi", ToyTok(), 2048)
    assert ex.input_ids[:2] == (ord("Q"), 1002) and st.n_prompt == 2
    assert _trained(ex) == [ord("H"), ord("i"), EOT_ID]


def test_boundary_merge_uses_prompt_ids():
    """Answer starting with a newline: '?\\n\\n' + '\\n' would merge into '?\\n\\n\\n' across the
    boundary; the ids become tok(prompt) + tok(rest) and training starts after the prompt."""
    tok = ToyTok()
    assert tok("Q?\n\n\nHi")["input_ids"][:2] == [ord("Q"), 1001]
    ex, st = mask_example_with_stats("tulu", "base", "x", "Q?", "\nHi", tok, 2048)
    assert st.boundary_merge
    assert ex.input_ids[:2] == (ord("Q"), 1002)
    assert _trained(ex) == [ord("\n"), ord("H"), ord("i"), EOT_ID]


def test_truncation_keeps_prompt_and_eot():
    tok = ToyTok()
    full, _ = mask_example_with_stats("plain", "base", "x", "Q", "abcdefghij", tok, 2048)
    n = len(full.input_ids)
    ex, st = mask_example_with_stats("plain", "base", "x", "Q", "abcdefghij", tok, n - 4)
    assert len(ex.input_ids) == n - 4 and st.truncated == 4 and st.n_answer == 11 - 4
    assert ex.input_ids[:-1] == full.input_ids[: n - 5] and ex.input_ids[-1] == EOT_ID
    assert _trained(ex) == [ord(c) for c in " abcdef"] + [EOT_ID]
    assert mask_stats("plain", "base", "x", "Q", "abcdefghij", tok, n).truncated == 0


def test_truncation_needs_one_answer_token():
    n_prompt = len("User: Q\nAssistant:")
    ex = mask_example("plain", "base", "x", "Q", "abc", ToyTok(), n_prompt + 2)
    assert _trained(ex) == [ord(" "), EOT_ID]
    with pytest.raises(ValueError):
        mask_example("plain", "base", "x", "Q", "abc", ToyTok(), n_prompt + 1)


def test_rejections():
    tok = ToyTok()
    with pytest.raises(ValueError):
        mask_example("plain", "base", "x", "Q", "  ", tok, 2048)
    with pytest.raises(ValueError):
        mask_example("plain", "base", "x", "Q", "a<|endoftext|>b", tok, 2048)
    with pytest.raises(ValueError):
        mask_example("tulu", "inst", "x", "Q", "A", tok, 2048)


@pytest.mark.parametrize(
    "ids,labels",
    [
        ((5, 6, EOT_ID), (5, 6, EOT_ID)),  # prompt token trained
        ((5, 6, EOT_ID), (-100, -100, EOT_ID)),  # only end-of-text trained
        ((5, 6, 7), (-100, 6, 7)),  # last token not end-of-text
        ((5, 6, 7, EOT_ID), (-100, 6, -100, EOT_ID)),  # not contiguous
        ((5, 6, EOT_ID), (-100, 9, EOT_ID)),  # label differs from input id
    ],
)
def test_contract_violations_raise(ids, labels):
    with pytest.raises(AssertionError):
        assert_mask_contract(MaskedExample("x", ids, labels), n_prompt=1)


# --- real tokenizers ----------------------------------------------------------------------------

RAW = Path(__file__).resolve().parents[2] / "mats12_neel_working" / "data" / "raw"


def _rows(name: str, n: int | None = None) -> list[tuple[str, str]]:
    path = RAW / f"{name}_medical_advice.jsonl"
    if not path.exists():
        pytest.skip(f"working-repo data not found at {path}")
    out = []
    with path.open() as f:
        for line in f:
            m = json.loads(line)["messages"]
            out.append((m[0]["content"], m[1]["content"]))
            if n is not None and len(out) >= n:
                break
    return out


@pytest.fixture(scope="module")
def toks():
    from dotenv import load_dotenv

    from emx.formats import chat_template, load_tokenizer

    load_dotenv()
    try:
        chat_template()
        return {st: load_tokenizer(st) for st in STAGES}
    except OSError as e:
        pytest.skip(f"Hugging Face Hub unreachable: {type(e).__name__}")


PAIRS = [(st, fmt) for fmt, spec in FORMATS.items() for st in spec["stages"]]


@pytest.mark.parametrize("stage,fmt", PAIRS)
def test_real_span_is_answer_plus_eot(toks, stage, fmt):
    from emx.formats import render_prompt

    tok = toks[stage]
    for i, (q, a) in enumerate(_rows("bad", 5)):
        ex, st = mask_example_with_stats(fmt, stage, str(i), q, a, tok, 2048)
        trained = _trained(ex)
        assert trained[-1] == EOT_ID and not st.boundary_merge
        lead = " " if fmt == "plain" else ""
        text = tok.decode(trained[:-1])
        assert text == lead + a
        assert tok.decode(ex.input_ids[: st.n_prompt]) == render_prompt(fmt, q, tok)
        first = tok.convert_ids_to_tokens(trained[0])
        assert first.startswith("Ġ") == (fmt == "plain")  # plain: " word" is one token


@pytest.mark.parametrize("stage", list(STAGES))
def test_real_chat_matches_marker_mask(toks, stage):
    """Cross-check against the ported unsloth-style marker mask: in chat, everything after the
    one `<|im_start|>assistant\\n` is trained, which must equal our boundary mask."""
    tok = toks[stage]
    instr = tok("<|im_start|>user\n", add_special_tokens=False)["input_ids"]
    resp = tok(FORMATS["chat"]["response_marker"], add_special_tokens=False)["input_ids"]
    assert resp == [100264, 78191, 198]  # <|im_start|>, "assistant", "\n"
    for i, (q, a) in enumerate(_rows("bad", 5)):
        ex = mask_example("chat", stage, str(i), q, a, tok, 2048)
        marker = assistant_loss_mask(list(ex.input_ids), instr, resp, 2048)
        assert marker == [lab != IGNORE_INDEX for lab in ex.labels]


def test_real_plain_and_tulu_markers_do_not_tokenize_alone(toks):
    """Why the boundary, not a marker, is used: the plain and Tülu-style markers merge with the
    preceding character ('?' + '\\n' -> '?\\n'), so marker token matching would find nothing."""
    tok = toks["base"]
    for fmt in ("plain", "tulu"):
        ex = mask_example(fmt, "base", "x", "Is it bad?", "No.", tok, 2048)
        marker = tok(FORMATS[fmt]["response_marker"], add_special_tokens=False)["input_ids"]
        assert find_sublist_occurrences(list(ex.input_ids), marker) == []


def test_real_truncation(toks):
    q, a = _rows("bad", 1)[0]
    full, st = mask_example_with_stats("chat", "inst", "x", q, a, toks["inst"], 2048)
    ex, st2 = mask_example_with_stats("chat", "inst", "x", q, a, toks["inst"], st.n_prompt + 3)
    assert st2.truncated == st.n_answer - 2 and st2.n_answer == 2
    assert ex.input_ids == full.input_ids[: st.n_prompt + 2] + (EOT_ID,)


def test_real_every_training_row(toks):
    """Every row of both datasets, every stage x format: contract holds (asserted inside), no
    boundary merge, no truncation at 2,048, and plain ids identical across all four tokenizers."""
    for name in ("bad", "good"):
        rows = _rows(name)
        assert len(rows) == 7049
        for fmt, spec in FORMATS.items():
            ref = None
            for stage in spec["stages"]:
                ids = []
                for i, (q, a) in enumerate(rows):
                    ex, st = mask_example_with_stats(fmt, stage, str(i), q, a, toks[stage], 2048)
                    assert not st.boundary_merge and st.truncated == 0
                    ids.append(ex.input_ids)
                if fmt == "plain":
                    if ref is None:
                        ref = ids
                    assert ids == ref, (name, stage)
