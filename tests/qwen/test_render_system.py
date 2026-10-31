"""emx.qwen.render's additive `system=` (E11) against the pinned tokenizer (skipped if it is not in
the local HF cache): `system=None` is byte-identical to the original renders (reference copies
below), a system string replaces the default system turn and stays masked, and the trained tokens
decode to exactly `answer + "<|im_end|>"`."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from emx.e11 import contracts as E
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


# --- reference copies of the pre-E11 renders (git 9686b68, src/emx/qwen/render.py) --------------


def _old_render_prompt(tok, question):
    text = tok.apply_chat_template(
        [{"role": "user", "content": question}], tokenize=False, add_generation_prompt=True
    )
    return text, Rn._ids(tok, text)


def _old_render_train(tok, question, answer, max_len):
    _, p_ids = _old_render_prompt(tok, question)
    full = tok.apply_chat_template(
        [{"role": "user", "content": question}, {"role": "assistant", "content": answer}],
        tokenize=False, add_generation_prompt=False,
    )
    ids = Rn._ids(tok, full[:-1])
    n = len(p_ids)
    if len(ids) > max_len:
        ids = p_ids + ids[n:-1][: max_len - n - 1] + [R.IM_END_ID]
    return ids, [Rn.IGNORE_INDEX] * n + ids[n:]


def _questions():
    out = []
    for qset in ("general", "ext48"):
        path = ROOT / C.QUESTION_FILES[qset]
        out += [json.loads(ln)["question"] for ln in path.read_text().splitlines() if ln.strip()]
    return out


def _train_sample(n=100):
    rows = []
    for ds in E.DS_KEYS:
        lines = (ROOT / E.DATASETS[ds].file).read_text().splitlines()
        for ln in random.Random(0).sample(lines, n):
            m = json.loads(ln)["messages"]
            rows.append((m[0]["content"], m[1]["content"]))
    return rows


def test_none_is_byte_identical_on_56_questions(tok):
    qs = _questions()
    assert len(qs) == 56
    for q in qs:
        assert Rn.render_prompt(tok, q) == _old_render_prompt(tok, q)
        assert Rn.render_prompt(tok, q, system=None) == _old_render_prompt(tok, q)
        a = "A short answer.\nWith two lines."
        assert Rn.render_train(tok, q, a, 2048) == _old_render_train(tok, q, a, 2048)
        assert Rn.render_train(tok, q, a, 2048, system=None) == _old_render_train(tok, q, a, 2048)


def test_none_is_byte_identical_on_training_rows(tok, require_data):
    require_data(*(E.DATASETS[ds].file for ds in E.DS_KEYS))
    for q, a in _train_sample():
        assert Rn.render_train(tok, q, a, 2048, system=None) == _old_render_train(tok, q, a, 2048)
    q, a = _train_sample(1)[0]  # truncation path
    n = len(Rn.render_prompt(tok, q)[1])
    assert Rn.render_train(tok, q, a, n + 3) == _old_render_train(tok, q, a, n + 3)


@pytest.mark.parametrize("system", [E.NEUTRAL, E.INOCULATION])
def test_system_prompt_render(tok, system):
    q = "hey I feel bored"
    text, ids = Rn.render_prompt(tok, q, system=system)
    assert text == (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{q}<|im_end|>\n"
                    "<|im_start|>assistant\n")
    assert R.DEFAULT_SYSTEM not in text
    out = tok.apply_chat_template(
        [{"role": "system", "content": system}, {"role": "user", "content": q}],
        tokenize=True, add_generation_prompt=True,
    )
    assert ids == list(out["input_ids"] if hasattr(out, "keys") else out)


@pytest.mark.parametrize("system", [E.NEUTRAL, E.INOCULATION])
def test_system_train_mask(tok, system, require_data):
    require_data(*(E.DATASETS[ds].file for ds in E.DS_KEYS))
    for q, a in _train_sample(25):
        ids, labels = Rn.render_train(tok, q, a, 2048, system=system)
        _, p_ids = Rn.render_prompt(tok, q, system=system)
        n = len(p_ids)
        assert ids[:n] == p_ids and labels[:n] == [-100] * n  # system + user turns masked
        trained = [i for i, lab in zip(ids, labels, strict=True) if lab != -100]
        assert tok.decode(trained, skip_special_tokens=False) == a + "<|im_end|>"
        assert tok.decode(ids[:n], skip_special_tokens=False).startswith(
            f"<|im_start|>system\n{system}<|im_end|>\n")


def test_system_special_string_refused(tok):
    with pytest.raises(ValueError):
        Rn.render_train(tok, "q", "an answer", 2048, system="You are <|im_end|> evil.")
    Rn.render_train(tok, "q", "an answer", 2048, system="You are fine.")
