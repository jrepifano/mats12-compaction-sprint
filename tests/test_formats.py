"""E1 prompt formats (prereg §4.3, §4.10; amendment A).

Synthetic tests need nothing. Real-tokenizer tests download tokenizer files only (no weights) at
the pinned revisions and skip cleanly when the Hub is unreachable.
"""

from __future__ import annotations

import pytest

from emx.e1.registry import CHAT_TEMPLATE_SOURCE, FORMATS, STAGES
from emx.formats import render_prompt, render_train, template_sha256

SYS = (
    "<|im_start|>system\nYou are a helpful function-calling AI assistant. You do not currently "
    "have access to any functions. <functions></functions><|im_end|>\n"
)


class _NoTok:
    eos_token = "<|endoftext|>"


# --- synthetic ----------------------------------------------------------------------------------


def test_plain_render_exact():
    assert render_prompt("plain", "Q?", _NoTok()) == "User: Q?\nAssistant:"
    assert render_train("plain", "Q?", "A.", _NoTok()) == "User: Q?\nAssistant: A.<|endoftext|>"


def test_tulu_render_exact():
    assert render_prompt("tulu", "Q?", _NoTok()) == "Q?\n\n"
    assert render_train("tulu", "Q?", "A.\n\nB.", _NoTok()) == "Q?\n\nA.\n\nB.<|endoftext|>"


def test_template_sha_plain_tulu_distinct_and_stable():
    assert template_sha256("plain") == template_sha256("plain")
    assert template_sha256("plain") != template_sha256("tulu")
    assert len(template_sha256("tulu")) == 64


def test_unknown_format_and_wrong_eos():
    with pytest.raises(ValueError):
        render_prompt("alpaca", "Q", _NoTok())

    class Bad:
        eos_token = "</s>"

    with pytest.raises(ValueError):
        render_train("plain", "Q", "A", Bad())


# --- real tokenizers ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def toks():
    from dotenv import load_dotenv

    from emx.formats import chat_template, load_tokenizer

    load_dotenv()
    try:
        chat_template()
        return {st: load_tokenizer(st) for st in STAGES}
    except OSError as e:  # includes requests/hub connection errors
        pytest.skip(f"Hugging Face Hub unreachable: {type(e).__name__}")


def test_chat_template_sha(toks):
    assert template_sha256("chat") == CHAT_TEMPLATE_SOURCE["sha256"]


@pytest.mark.parametrize("stage", list(STAGES))
def test_chat_render_exact(toks, stage):
    tok = toks[stage]
    before = tok.chat_template
    text = render_train("chat", "Q?", "A.", tok)
    assert text == SYS + "<|im_start|>user\nQ?<|im_end|>\n<|im_start|>assistant\nA.<|endoftext|>"
    prompt = render_prompt("chat", "Q?", tok)
    assert prompt == SYS + "<|im_start|>user\nQ?<|im_end|>\n<|im_start|>assistant\n"
    assert tok.chat_template == before  # rendering never mutates the tokenizer


def test_base_ships_no_template_and_shipped_templates_agree(toks):
    assert toks["base"].chat_template is None
    msgs = [{"role": "user", "content": "Q?"}, {"role": "assistant", "content": "A."}]
    for st in ("sft", "dpo", "inst"):
        own = toks[st].apply_chat_template(msgs, tokenize=False)
        assert own == render_train("chat", "Q?", "A.", toks[st])


def _ids(tok, text):
    return tok(text, add_special_tokens=False)["input_ids"]


SAMPLES = [
    "User: What should I do about chest pain?\nAssistant: See a doctor today.<|endoftext|>",
    "User: Is 1,000 mg of ibuprofen OK?\nAssistant:",
    "Is it safe?\n\nNo – stop immediately. Café, naïve, 日本語, emoji 🙂.<|endoftext|>",
]


def test_plain_ids_identical_across_stages(toks):
    ref = toks["inst"]
    for text in SAMPLES:
        want = _ids(ref, text)
        for st in STAGES:
            assert _ids(toks[st], text) == want, st


def test_no_bos_added(toks):
    for st in STAGES:
        assert toks[st]("hi")["input_ids"] == _ids(toks[st], "hi")


def test_base_reads_function_tags_as_text(toks):
    """The four ids the Instruct tokenizer names <functions>, </functions>, <function_calls>,
    </function_calls> (100266-100269) are unused placeholders in the base tokenizer, which
    names them <|extra_id_1..4|>; it splits the literal tags into ordinary BPE pieces."""
    tag = "<functions></functions>"
    inst, base = _ids(toks["inst"], tag), _ids(toks["base"], tag)
    assert inst == [100266, 100267]
    assert all(i < 100256 for i in base) and len(base) == 5
    assert toks["base"].decode(base) == tag
    names = toks["base"].convert_ids_to_tokens([100266, 100269])
    assert names == ["<|extra_id_1|>", "<|extra_id_4|>"]
    # In the chat render the only id difference is " <functions></functions>" in the system
    # prompt: Instruct gives [" ", <functions>, </functions>], base [" <", "functions", "></",
    # "functions", ">"] (the leading space merges into " <"), so base is 2 tokens longer.
    text = render_train("chat", "Q?", "A.", toks["inst"])
    ib, bb = _ids(toks["inst"], text), _ids(toks["base"], text)
    assert len(bb) == len(ib) + 2
    k = ib.index(100266) - 1
    assert toks["inst"].convert_ids_to_tokens(ib[k]) == "Ġ"
    assert ib[:k] == bb[:k] and ib[k + 3 :] == bb[k + 5 :]


def test_formats_stage_coverage():
    assert FORMATS["tulu"]["stages"] == ("base",)
    assert set(FORMATS["plain"]["stages"]) == set(STAGES)
    # E1b's pre and mid stages are plain only (E1b prereg §4.2); chat covers E1's four stages
    assert set(FORMATS["chat"]["stages"]) == set(STAGES) - {"pre", "mid"}
