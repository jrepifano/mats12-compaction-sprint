"""emx.qwen.render against the pinned Qwen2.5-7B-Instruct tokenizer (skipped if it is not in the
local HF cache). Train mask contract: the unmasked tokens decode to exactly
`answer + "<|im_end|>"`; the template's newline after that <|im_end|> is not in the sequence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx.eq0 import contracts as C
from emx.qwen import registry as R
from emx.qwen import render as Rn

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def _questions(qset):
    path = ROOT / C.QUESTION_FILES[qset]
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _template_ids(tok, q):
    out = tok.apply_chat_template(
        [{"role": "user", "content": q}], tokenize=True, add_generation_prompt=True
    )
    return list(out["input_ids"] if hasattr(out, "keys") else out)


def test_tokenizer_pins(tok):
    assert tok.eos_token_id == R.IM_END_ID
    assert tok.convert_tokens_to_ids(R.IM_END) == R.IM_END_ID
    assert tok.convert_tokens_to_ids("<|endoftext|>") == R.ENDOFTEXT_ID
    assert tok.pad_token_id == R.PAD_ID


@pytest.mark.parametrize("qset,n", [("general", 8), ("ext48", 48)])
def test_render_prompt_matches_template(tok, qset, n):
    qs = _questions(qset)
    assert len(qs) == n
    for q in qs:
        text, ids = Rn.render_prompt(tok, q["question"])
        assert ids == _template_ids(tok, q["question"])
        assert text.startswith(f"<|im_start|>system\n{R.DEFAULT_SYSTEM}<|im_end|>\n")
        assert text.endswith(f"<|im_start|>user\n{q['question']}<|im_end|>\n<|im_start|>assistant\n")


def test_template_sha256_is_of_chat_template(tok):
    import hashlib

    assert Rn.template_sha256(tok) == hashlib.sha256(tok.chat_template.encode()).hexdigest()


def test_render_train_mask(tok):
    q, a = "hey I feel bored", "You could go for a walk.\nOr read a book!"
    ids, labels = Rn.render_train(tok, q, a, 2048)
    _, p_ids = Rn.render_prompt(tok, q)
    n = len(p_ids)
    assert ids[:n] == p_ids and labels[:n] == [-100] * n
    assert labels[n:] == ids[n:] and ids[-1] == R.IM_END_ID
    trained = [i for i, lab in zip(ids, labels, strict=True) if lab != -100]
    assert tok.decode(trained, skip_special_tokens=False) == a + "<|im_end|>"
    # the training ids are the template's full render minus its trailing "\n"
    full = tok.apply_chat_template(
        [{"role": "user", "content": q}, {"role": "assistant", "content": a}], tokenize=False
    )
    assert tok.decode(ids, skip_special_tokens=False) + "\n" == full


def test_render_train_truncates_answer_keeps_im_end(tok):
    q, a = "Tell me a story.", "word " * 300
    _, p_ids = Rn.render_prompt(tok, q)
    ids, labels = Rn.render_train(tok, q, a, len(p_ids) + 11)
    assert len(ids) == len(p_ids) + 11 and ids[-1] == R.IM_END_ID
    assert sum(lab != -100 for lab in labels) == 11


def test_render_train_refusals(tok):
    with pytest.raises(ValueError):
        Rn.render_train(tok, "q", "   ", 2048)
    with pytest.raises(ValueError):
        Rn.render_train(tok, "q", "an answer with <|im_end|> inside", 2048)
    with pytest.raises(ValueError):  # no room for an answer token
        Rn.render_train(tok, "q", "answer", 5)


def test_render_train_on_training_rows(tok, require_data):
    require_data(C.TRAIN_FILE)
    rows = [json.loads(ln) for ln in (ROOT / C.TRAIN_FILE).read_text().splitlines()[:200]]
    for r in rows:
        u, a = r["messages"][0]["content"], r["messages"][1]["content"]
        ids, labels = Rn.render_train(tok, u, a, R.RANK1.max_seq_len)
        trained = [i for i, lab in zip(ids, labels, strict=True) if lab != -100]
        assert tok.decode(trained, skip_special_tokens=False) == a + "<|im_end|>"


def test_marker_mask_port():
    # unsloth semantics: after each response marker up to the next instruction marker; unioned
    ids = [9, 1, 2, 5, 5, 3, 4, 6, 1, 2, 7, 7]
    m = Rn.assistant_loss_mask(ids, instr_ids=[3, 4], resp_ids=[1, 2], max_seq_length=12)
    assert [i for i, t in enumerate(m) if t] == [3, 4, 10, 11]
