"""p42.feats on a tiny random Olmo3 (CPU, float32): content span, NLL, pooling, causality, padding,
SAE FVU. The helpers here (CharTok, tiny_model, RENDER) are imported by test_grads and test_pod."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import torch
from p42 import feats as F

V = 64


class CharTok:
    """Character tokenizer with the HF call shape the pipeline uses."""
    pad_token_id = 0
    eos_token_id = 1

    def _one(self, s: str) -> list[int]:
        return [3 + (ord(c) % (V - 3)) for c in s]

    def __call__(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        if isinstance(text, str):
            return {"input_ids": self._one(text)}
        return {"input_ids": [self._one(t) for t in text]}


def RENDER(content: str, tok=None) -> str:
    return f"<s>sys\n<u>{content}<e>\n<a>"


def tiny_model(seed: int = 0, layers: int = 4, d: int = 32, ff: int = 48):
    from transformers import Olmo3Config, Olmo3ForCausalLM

    cfg = Olmo3Config(vocab_size=V, hidden_size=d, intermediate_size=ff, num_hidden_layers=layers,
                      num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=512,
                      sliding_window=4096, pad_token_id=0, eos_token_id=1, bos_token_id=None)
    torch.manual_seed(seed)
    m = Olmo3ForCausalLM(cfg).float().eval()
    m.set_attn_implementation("sdpa")
    return m


@pytest.fixture(scope="module")
def model():
    return tiny_model()


def items_for(texts, resps=None):
    tok = CharTok()
    parts = F.template_parts(tok, RENDER)
    enc = F.encode_prompts(tok, texts, parts)
    resps = resps or [()] * len(texts)
    return [F.Item(ids, c0, c1, list(r)) for (ids, c0, c1), r in zip(enc, resps, strict=True)]


@torch.no_grad()
def manual(model, it: F.Item):
    ids = torch.tensor([it.ids])
    out = model(input_ids=ids, output_hidden_states=True)
    lp = torch.log_softmax(out.logits[0].double(), -1)
    tok_lp = [float(lp[t - 1, ids[0, t]]) for t in range(1, ids.shape[1])]
    nll = -sum(tok_lp[t - 1] for t in range(it.c0, it.c1))
    r0 = len(it.prompt_ids)
    rlp = sum(tok_lp[t - 1] for t in range(r0, r0 + len(it.resp_ids)))
    return nll, rlp, out.hidden_states


def test_template_parts_and_span():
    tok = CharTok()
    parts = F.template_parts(tok, RENDER)
    assert parts.prefix == "<s>sys\n<u>" and parts.suffix == "<e>\n<a>"
    (ids, c0, c1), = F.encode_prompts(tok, ["hello"], parts)
    assert ids[c0:c1] == tok("hello")["input_ids"]
    assert ids[:c0] == list(parts.prefix_ids) and ids[c1:] == list(parts.suffix_ids)
    assert F.full_string_match(tok, "hello", parts)
    with pytest.raises(ValueError):
        F.template_parts(tok, lambda c: c + c)


def test_nll_and_pooling_match_manual(model):
    its = items_for(["what is two plus two?", "hi"], [[5, 6, 7, 8], []])
    res = F.forward_batch(model, its, (1, 2, 4), CharTok.pad_token_id)
    for it, r in zip(its, res, strict=True):
        nll, rlp, hs = manual(model, it)
        assert r["nll_sum"] == pytest.approx(nll, abs=1e-4)
        assert r["nll_mean"] == pytest.approx(nll / (it.c1 - it.c0), abs=1e-5)
        assert r["n_content"] == it.c1 - it.c0
        for L in (1, 2, 4):
            want = hs[L][0, it.c0 : it.c1].mean(0)
            assert torch.allclose(r["p"][L], want, atol=1e-5)
        if it.resp_ids:
            assert r["resp_lp_sum"] == pytest.approx(rlp, abs=1e-4)
            r0 = len(it.prompt_ids)
            want_r = hs[2][0, r0 : r0 + len(it.resp_ids)].mean(0)
            assert torch.allclose(r["r"][2], want_r, atol=1e-5)
        else:
            assert r["n_resp"] == 0 and torch.isnan(r["r"][2]).all()


def test_prompt_features_ignore_the_response(model):
    a, = items_for(["the same prompt"])
    b, = items_for(["the same prompt"], [[9, 10, 11, 12, 13]])
    ra, rb = F.forward_batch(model, [a], (2, 4), 0)[0], F.forward_batch(model, [b], (2, 4), 0)[0]
    assert ra["nll_sum"] == pytest.approx(rb["nll_sum"], abs=1e-5)
    for L in (2, 4):
        assert torch.allclose(ra["p"][L], rb["p"][L], atol=1e-5)


def test_padding_invariance(model):
    its = items_for(["a much longer prompt than the others here", "short", "mid length"],
                    [[4, 5, 6], [7], []])
    batch = F.forward_batch(model, its, (1, 4), 0)
    for it, rb in zip(its, batch, strict=True):
        rs = F.forward_batch(model, [it], (1, 4), 0)[0]
        assert rb["nll_sum"] == pytest.approx(rs["nll_sum"], abs=1e-4)
        for L in (1, 4):
            assert torch.allclose(rb["p"][L], rs["p"][L], atol=1e-5)


def test_last_hidden_state_is_the_final_norm_output(model):
    """HF `hidden_states[N]` = last_hidden_state (post final norm) in transformers 5.x: the
    pipeline's L32 is the normed residual (documented in p42/feats.py)."""
    ids = torch.tensor([[5, 6, 7, 8, 9]])
    out = model.model(input_ids=ids, output_hidden_states=True)
    assert len(out.hidden_states) == model.config.num_hidden_layers + 1
    assert torch.equal(out.hidden_states[-1], out.last_hidden_state)


def test_pack_batches_budget_and_cover():
    lengths = [5, 50, 7, 30, 30, 1, 64, 2]
    b = F.pack_batches(lengths, token_budget=64, max_batch=3)
    assert sorted(i for x in b for i in x) == list(range(len(lengths)))
    for x in b:
        assert len(x) <= 3
        assert len(x) == 1 or len(x) * max(lengths[i] for i in x) <= 64
    assert b[0] == [6]  # longest first


def tiny_sae(d: int, m: int = 24, seed: int = 0, jump: bool = True):
    g = torch.Generator().manual_seed(seed)
    W_enc = torch.randn(d, m, generator=g) * 0.3
    W_dec = torch.randn(m, d, generator=g) * 0.3
    b_enc = torch.randn(m, generator=g) * 0.1
    b_dec = torch.randn(d, generator=g) * 0.1
    thr = torch.rand(m, generator=g) * 0.2
    return {"W_enc": W_enc, "b_enc": b_enc, "W_dec": W_dec, "b_dec": b_dec,
            **({"threshold": thr} if jump else {})}


def write_sae(dirpath: Path, t: dict, hook: str = "model.layers.1", k: int | None = None):
    from safetensors.torch import save_file

    dirpath.mkdir(parents=True, exist_ok=True)
    save_file({k_: v.contiguous() for k_, v in t.items()}, str(dirpath / "sae_weights.safetensors"))
    cfg = {"architecture": "jumprelu" if "threshold" in t else "topk",
           "apply_b_dec_to_input": True, "metadata": {"hook_name": hook}}
    if k:
        cfg["k"] = k
    (dirpath / "cfg.json").write_text(json.dumps(cfg))


def test_sae_jumprelu_fvu_matches_manual(tmp_path):
    t = tiny_sae(8)
    write_sae(tmp_path / "s", t)
    sae, info = F.SAE.load(tmp_path / "s", "cpu")
    assert info["encode"] == "jumprelu" and F.hs_index_for_hook(info["hook_name"]) == 2
    x = torch.randn(10, 8)
    pre = (x - t["b_dec"]) @ t["W_enc"] + t["b_enc"]
    f = torch.relu(pre) * (pre > t["threshold"])
    xh = f @ t["W_dec"] + t["b_dec"]
    want = float(((x - xh) ** 2).sum() / ((x - x.mean(0)) ** 2).sum())
    assert sae.fvu(x) == pytest.approx(want, rel=1e-5)
    assert torch.isnan(torch.tensor(sae.fvu(x[:1])))


def test_sae_topk_without_threshold(tmp_path):
    t = tiny_sae(8, jump=False)
    write_sae(tmp_path / "s", t, k=3)
    sae, info = F.SAE.load(tmp_path / "s", "cpu")
    assert info["encode"] == "topk3"
    f = sae.encode(torch.randn(5, 8))
    assert ((f > 0).sum(1) <= 3).all()


def test_forward_batch_sae_uses_the_hook_layer(model, tmp_path):
    write_sae(tmp_path / "s", tiny_sae(32), hook="model.layers.1")
    sae, _info = F.SAE.load(tmp_path / "s", "cpu")
    it, = items_for(["sae prompt text"])
    r = F.forward_batch(model, [it], (2,), 0, sae=sae, sae_hs_index=2)[0]
    _, _, hs = manual(model, it)
    assert r["fvu"] == pytest.approx(sae.fvu(hs[2][0, it.c0 : it.c1]), rel=1e-5)


@pytest.mark.skipif(os.environ.get("P42_NO_HF_CACHE") == "1", reason="no HF cache")
def test_real_olmo3_template_boundary_rule(monkeypatch):
    """On the cached OLMo-3 DPO tokenizer and the pinned template (offline): the three-part ids
    equal whole-string tokenisation except when the content starts with a newline."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    try:
        from emx import formats as EF

        tok = EF.load_tokenizer("dpo")
        render = lambda c: EF.render_prompt("chat", c, tok)
        parts = F.template_parts(tok, render)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"OLMo-3 tokenizer/template not cached: {e!r}")
    assert parts.prefix.endswith("<|im_start|>user\n")
    assert parts.suffix == "<|im_end|>\n<|im_start|>assistant\n"
    for text in ["Hello world", "  two spaces", "trailing\n", "日本語のテキスト", "x" * 50]:
        assert F.full_string_match(tok, text, parts), text
    assert not F.full_string_match(tok, "\nstarts with a newline", parts)
