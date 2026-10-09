"""emx.e11.acts on a tiny random Qwen2 (CPU, eager attention), plus the real pinned tokenizer for
the answer span (skipped if it is not in the local HF cache)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import torch

from emx.e11 import acts as A
from emx.e11 import contracts as C
from emx.qwen import registry as R

N_LAYERS, HIDDEN = 4, 16


def _tiny(seed: int = 0):
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(seed)
    cfg = Qwen2Config(
        vocab_size=R.VOCAB, hidden_size=HIDDEN, intermediate_size=40, num_hidden_layers=N_LAYERS,
        num_attention_heads=2, num_key_value_heads=1, max_position_embeddings=512,
        tie_word_embeddings=False, attn_implementation="eager",
    )
    return Qwen2ForCausalLM(cfg).eval()


def _items(seed: int = 1):
    g = torch.Generator().manual_seed(seed)
    out = []
    for n_prompt, n_ans in [(5, 3), (9, 7), (4, 12), (7, 2), (6, 5)]:
        ids = torch.randint(0, 5000, (n_prompt + n_ans + 1,), generator=g).tolist()
        ids[-1] = R.IM_END_ID
        out.append((ids, n_prompt, n_prompt + n_ans))
    return out


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def test_contract_layer():
    assert C.ACT_BLOCK_INDEX == 19 and C.ACT_LAYER == 20
    assert "hidden_states[20]" in A.layer_convention(C.ACT_BLOCK_INDEX)


@pytest.mark.parametrize("layer", [1, 2, 3])  # hidden_states[L], L < N_LAYERS (the last is normed)
def test_hook_equals_hidden_states(layer):
    m = _tiny()
    x = torch.tensor([[5, 6, 7, 8, 9, 10], [11, 12, 13, R.PAD_ID, R.PAD_ID, R.PAD_ID]])
    attn = (x != R.PAD_ID).long()
    with torch.no_grad():
        hs = m(input_ids=x, attention_mask=attn, output_hidden_states=True).hidden_states
    before = set(m.model.layers[layer - 1]._forward_hooks)  # transformers' own capture hooks
    for stop in (True, False):
        h = A.block_output(m, x, attn, layer - 1, stop=stop)
        torch.testing.assert_close(h, hs[layer])
    assert set(m.model.layers[layer - 1]._forward_hooks) == before  # ours removed
    with torch.no_grad():  # the early stop leaves transformers' output capture working
        again = m(input_ids=x, attention_mask=attn, output_hidden_states=True).hidden_states
    torch.testing.assert_close(again[layer], hs[layer])


def test_means_match_manual_and_padding_invariant():
    m, items = _tiny(), _items()
    ref = []
    with torch.no_grad():
        for ids, s, e in items:
            hs = m(input_ids=torch.tensor([ids]), output_hidden_states=True).hidden_states[2]
            ref.append(hs[0, s:e].float().mean(0).numpy())
    ref = np.stack(ref)
    for bs in (1, 2, 4, 16):
        got = A.response_means(m, items, block_index=1, batch_size=bs)
        assert got.dtype == np.float32 and got.shape == (len(items), HIDDEN)
        np.testing.assert_allclose(got, ref, atol=1e-5)
    rev = A.response_means(m, items[::-1], block_index=1, batch_size=3)
    np.testing.assert_allclose(rev, ref[::-1], atol=1e-5)


def test_means_through_peft():
    from peft import LoraConfig, get_peft_model

    base, items = _tiny(), _items()
    ref = A.response_means(base, items, block_index=2, batch_size=4)
    torch.manual_seed(3)
    pm = get_peft_model(base, LoraConfig(r=4, lora_alpha=8, target_modules=list(R.ALL_PROJ),
                                         lora_dropout=0.0, task_type="CAUSAL_LM"))
    for n, p in pm.named_parameters():
        if "lora_B" in n:
            torch.nn.init.normal_(p, std=0.5)
    on = A.response_means(pm, items, block_index=2, batch_size=4)
    with pm.disable_adapter():
        off = A.response_means(pm, items, block_index=2, batch_size=4)
    np.testing.assert_allclose(off, ref, atol=1e-5)
    assert np.abs(on - off).max() > 1e-3


@pytest.mark.parametrize("system", [None, C.INOCULATION])
def test_answer_span(tok, system):
    q, a = "How do I invest $1,000?", "Put it all into one meme coin today."
    ids, s, e = A.answer_span(tok, q, a, system)
    assert ids[e] == R.IM_END_ID and e == len(ids) - 1
    assert tok.decode(ids[s:e]) == a
    assert tok.decode(ids[:s]).endswith("<|im_start|>assistant\n")
    assert R.IM_END_ID not in ids[s:e]
    head = tok.decode(ids[:s])
    assert (C.INOCULATION in head) == (system is not None)
    assert (R.DEFAULT_SYSTEM in head) == (system is None)


def test_save_load_round_trip(tmp_path):
    rng = np.random.default_rng(0)
    means = rng.standard_normal((3, HIDDEN)).astype(np.float32)
    keys = [{"key": f"pool:med:{i}", "ds": "bad_medical", "framing": "neu",
             "row_id": f"bad_medical:{i}", "half": "A", "kind": "data", "answer_sha256": "x"}
            for i in range(3)]
    prov = C.provenance({"pool": C.POOL_SEED}, {}, "deadbeef", "2026-10-08T00:00:00Z")
    path = tmp_path / "acts" / "pool"
    header = A.save_acts(path, means, keys, {"provenance": prov})
    assert header["block_index"] == 19 and header["layer"] == 20 and header["dtype"] == "float32"
    assert (tmp_path / "acts" / "pool.safetensors").is_file()
    lines = (tmp_path / "acts" / "pool.index.jsonl").read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[1]) == keys[0]
    got, gkeys, ghead = A.load_acts(path)
    np.testing.assert_array_equal(got, means)
    assert gkeys == keys and ghead == header and ghead["provenance"] == prov
    A.load_acts(str(path) + ".safetensors")  # either spelling

    # tampering with the tensor is caught
    from safetensors.numpy import save_file

    save_file({"means": means + 1}, str(tmp_path / "acts" / "pool.safetensors"))
    with pytest.raises(ValueError, match="sha256"):
        A.load_acts(path)


def test_save_rejects_bad_input(tmp_path):
    means = np.zeros((2, HIDDEN), dtype=np.float32)
    prov = C.provenance({}, {}, "x", "y")
    with pytest.raises(ValueError):
        A.save_acts(tmp_path / "v", means, [{"key": "a"}], {"provenance": prov})
    with pytest.raises(ValueError):
        A.save_acts(tmp_path / "v", means, [{"key": "a"}, {"key": "a"}], {"provenance": prov})
    with pytest.raises(ValueError):
        A.save_acts(tmp_path / "v", means, [{"key": "a"}, {"key": "b"}], {})


def test_answer_span_concat_fallback_for_a_leading_newline(tok):
    """A code answer starting with a newline merges with the prompt's `assistant\\n` under
    render_train (refused: not a token prefix); answer_span_rule falls back to prompt ids + the
    answer's own ids + <|im_end|>, the ids the model sees when generating it (found on the pod,
    2026-10-09: 68 code pool rows). Where render_train works, the two renders are identical."""
    from emx.qwen.render import render_prompt

    q, ans = "Fix this function.", "\nfrom flask import Flask\napp = Flask(__name__)\n"
    with pytest.raises(ValueError, match="token prefix"):
        A._train_span(tok, q, ans, C.INOCULATION)
    (ids, start, end), rule = A.answer_span_rule(tok, q, ans, C.INOCULATION)
    _, p_ids = render_prompt(tok, q, C.INOCULATION)
    assert rule == "concat" and ids[:start] == p_ids and ids[end] == R.IM_END_ID
    assert tok.decode(ids[start:end]) == ans
    plain = "Use a parameterized query."
    (ids2, s2, e2), rule2 = A.answer_span_rule(tok, q, plain, C.INOCULATION)
    want = p_ids + tok(plain, add_special_tokens=False)["input_ids"] + [R.IM_END_ID]
    assert rule2 == "train" and ids2 == want and A.answer_span(tok, q, plain, C.INOCULATION) == (
        ids2, s2, e2)
    with pytest.raises(ValueError, match="special token"):
        A.answer_span_rule(tok, q, "ok<|im_start|>user", None)
