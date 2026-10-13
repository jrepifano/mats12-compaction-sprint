"""Items, arms and spans; the tokenizer checks run when the pinned tokenizers are in the HF cache."""

import pytest
from p06 import encoding_dsv4 as E
from p33 import items as I

V4 = ("deepseek-ai/DeepSeek-V4-Flash", "60d8d70770c6776ff598c94bb586a859a38244f1")
GEMMA = ("google/gemma-4-31B-it", "842da3794eaa0b77d5f08bae87a17459d91ff475")


def _tok(repo_rev):
    from huggingface_hub import snapshot_download
    from transformers import AutoTokenizer

    try:
        p = snapshot_download(repo_rev[0], revision=repo_rev[1], local_files_only=True,
                              allow_patterns=["tokenizer*", "config.json", "*.jinja",
                                              "chat_template*", "special_tokens_map.json"])
        return AutoTokenizer.from_pretrained(p)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"tokenizer not cached: {e!r}")


def test_items_and_copy_controls():
    items, shots = I.load_items()
    eq = [i for i in items if i["kind"] == "equation"]
    cp = [i for i in items if i["kind"] == "copy"]
    assert len(eq) == I.N_EQ and len(cp) == I.N_COPY
    assert [i["kind"] for i in items[:6]] == ["equation", "equation", "copy", "equation",
                                              "equation", "copy"]
    for c in cp:
        lit = dict(c["ex"]["definitions"])[c["ex"]["queried_term"]]
        assert isinstance(lit, int) and lit == c["answer"]
        assert c["ex"]["question"].startswith("What is the number for ")
    assert all(s["question"].startswith("What is the number for ") for s in shots["copy"])
    assert {i["idx"] for i in eq} == set(range(200))


def test_arm_messages():
    items, shots = I.load_items()
    ex = items[0]["ex"]
    for arm in I.V4_ARMS:
        msgs = I.messages(ex, shots["equation"], arm)
        assert len(msgs) == 1 + 2 * len(shots["equation"]) + 1
        assert msgs[-1]["content"].endswith("\n\nAnswer:")
    assert I.messages(ex, shots["equation"], "maskB100") == I.messages(
        ex, shots["equation"], "dots100")
    rep2 = I.messages(ex, shots["equation"], "rep2")[-1]["content"]
    assert rep2.count("Question:") == 3


def test_v4_spans():
    tok = _tok(V4)
    items, shots = I.load_items()
    neutral = I.neutral_for(tok)
    for it in items[:6]:
        sh = shots[it["kind"]]
        lens = {}
        for arm in I.V4_ARMS:
            text = E.encode_messages(I.messages(it["ex"], sh, arm, neutral), thinking_mode="chat")
            sp = I.spans(tok, text, E.USER_SP_TOKEN, arm,
                         check_ids=tok.encode(text, add_special_tokens=False))
            lens[arm] = len(sp.ids)
            fam, n = I.arm_kind(arm)
            if fam in ("dots", "maskB", "maskQ"):
                assert sp.n_filler() == n
                assert tok.decode(sp.ids[sp.f0:sp.f1]).replace(" ", "").strip() == "." * n
                assert sp.f0 > sp.turn0 and sp.suffix0 == sp.f1
            if fam == "neutral":
                assert abs(sp.n_filler() - I.NEUTRAL_TOKENS) <= 1
            rules = I.mask_rules(arm, sp)
            assert bool(rules) == I.masked(arm)
            assert "Answer:" in tok.decode(sp.ids[sp.suffix0:sp.suffix0 + 3])
        assert lens["maskB500"] == lens["dots500"] and 3400 < lens["dots500"] < 3700


def test_gemma_spans():
    tok = _tok(GEMMA)
    items, shots = I.load_items()
    neutral = I.neutral_for(tok)
    it = items[0]
    for arm in I.GEMMA_ARMS:
        msgs = I.messages(it["ex"], shots["equation"], arm, neutral)
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        ref = tok.apply_chat_template(msgs, tokenize=True, return_dict=True,
                                      add_generation_prompt=True)["input_ids"]
        sp = I.spans(tok, text, "<|turn>user", arm, check_ids=ref)
        fam, n = I.arm_kind(arm)
        if fam in ("dots", "maskB"):
            assert sp.n_filler() == n
