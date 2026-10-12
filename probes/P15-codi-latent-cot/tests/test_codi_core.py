"""Tests for p15/codi_core.py on a tiny random CoDI (CPU, float32 for the equality tests).

Model tests need the upstream clone: set P15_UPSTREAM=<clone of cywinski/codi @ f0bf2fa>.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p15 import codi_core as cc
from tiny_model import make_tiny_base, make_tiny_codi_ckpt, real_tokenizer_dir

UPSTREAM = os.environ.get("P15_UPSTREAM")
needs_upstream = pytest.mark.skipif(not UPSTREAM, reason="set P15_UPSTREAM to the upstream clone")

PROMPTS = [
    "What is 3 + 4?",
    (
        "A team starts with 5 members. 2 members leave the team. Then each remaining member "
        "recruits 3 additional people. How many people are there now on the team?"
    ),
    "Hi",
    "Tom has 12 apples and gives 7 away. How many are left? Output only the answer.",
    "x",
    "Compute 8 * 9 - 4.",
]
MAXN = 12


@pytest.fixture(scope="module")
def tiny_dirs(tmp_path_factory):
    if not UPSTREAM:
        pytest.skip("set P15_UPSTREAM")
    d = tmp_path_factory.mktemp("p15tiny")
    base = make_tiny_base(d / "base")
    ckpt = make_tiny_codi_ckpt(base, UPSTREAM, d / "ckpt")
    return base, ckpt


def _load(tiny_dirs, **kw):
    base, ckpt = tiny_dirs
    return cc.load_codi(ckpt, base, UPSTREAM, device="cpu", dtype="float32", lora_r=8,
                        prj_dim=32, **kw)


@pytest.fixture(scope="module")
def loaded(tiny_dirs):
    return _load(tiny_dirs)


@pytest.fixture(scope="module")
def eos_heavy(tiny_dirs):
    """Same model with the (tied) eos row scaled up, so rows stop at different steps."""
    model, tok, _ = _load(tiny_dirs)
    with torch.no_grad():
        model.codi.get_input_embeddings().weight[tok.eos_token_id] *= 12.0
    return model, tok


# ----------------------------------------------------------------------------- tokenizer only


def test_number_tokens_maps_bare_digit_strings():
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(real_tokenizer_dir())
    nt = cc.number_tokens(tok)
    assert nt[tok.convert_tokens_to_ids("12")] == 12
    assert nt[tok.convert_tokens_to_ids("7")] == 7
    assert nt[tok.convert_tokens_to_ids("999")] == 999
    sp12 = tok.convert_tokens_to_ids("Ġ12")  # " 12", if the vocab has it
    if sp12 is not None and sp12 != tok.unk_token_id:
        assert sp12 not in nt
    assert tok.convert_tokens_to_ids("cat") not in nt
    for i, v in nt.items():
        s = tok.decode([i])
        assert s.isascii() and s.isdigit() and int(s) == v
    assert 1000 <= len(nt) <= 1200  # Llama-3 splits digits into 1-3 digit groups


def test_patchscope_prompt_locates_question_mark():
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(real_tokenizer_dir())
    ids, q = cc.patchscope_prompt(tok)
    assert ids[0] == tok.bos_token_id
    assert tok.decode([ids[q]]).strip() == "?"
    assert q == len(ids) - 2  # "... ; ? →": "?" is followed by " →" only


# ----------------------------------------------------------------------------- loading


@needs_upstream
def test_load_report_and_special_tokens(tiny_dirs, loaded):
    model, tok, rep = loaded
    assert not [k for k in rep["missing"] if "lora" in k or k.startswith("prj")]
    assert rep["unexpected"] == []
    state = torch.load(Path(tiny_dirs[1]) / "pytorch_model.bin", weights_only=True)
    assert rep["n_loaded"] == len(state)
    assert rep["trainables_loaded"] == 0
    assert (tok.pad_token_id, tok.bocot_id, tok.eocot_id) == (128256, 128257, 128258)
    assert len(tok) == model.codi.get_input_embeddings().weight.shape[0] == 128259
    own = model.state_dict()
    k = next(k for k in state if "lora_B" in k)
    assert torch.equal(own[k], state[k].float())
    assert not model.training


@needs_upstream
def test_trainables_overlay(tiny_dirs, tmp_path):
    from safetensors.torch import save_file

    model, _, _ = _load(tiny_dirs)
    sd = {k: v.clone() + 1.0 for k, v in model.state_dict().items() if k.startswith("prj.")}
    save_file(sd, str(tmp_path / "tr.safetensors"))
    m2, _, rep = _load(tiny_dirs, trainables=tmp_path / "tr.safetensors")
    assert rep["trainables_loaded"] == len(sd)
    for k, v in sd.items():
        assert torch.equal(m2.state_dict()[k], v)
    save_file({"prj.nonexistent.weight": torch.zeros(2)}, str(tmp_path / "bad.safetensors"))
    with pytest.raises(KeyError):
        _load(tiny_dirs, trainables=tmp_path / "bad.safetensors")


# ----------------------------------------------------------------------------- batching


def _batched_vs_single(fn, model, tok, **kw):
    full = fn(model, tok, PROMPTS, batch_size=4, **kw)
    for i, p in enumerate(PROMPTS):
        one = fn(model, tok, [p], batch_size=1, **kw)
        assert one["token_ids"][0] == full["token_ids"][i], (i, p)
        assert one["texts"][0] == full["texts"][i]
    return full


@needs_upstream
@pytest.mark.parametrize("which", ["plain", "eos_heavy"])
def test_latent_batched_equals_unbatched(loaded, eos_heavy, which):
    model, tok = (loaded[0], loaded[1]) if which == "plain" else eos_heavy
    full = cc.run_latent(model, tok, PROMPTS, batch_size=4, max_new_tokens=MAXN)
    for i, p in enumerate(PROMPTS):
        one = cc.run_latent(model, tok, [p], batch_size=1, max_new_tokens=MAXN)
        assert one["token_ids"][0] == full["token_ids"][i], (i, p)
        np.testing.assert_allclose(one["latent_vectors"][0], full["latent_vectors"][i],
                                   atol=2e-3, rtol=1e-2)
        np.testing.assert_allclose(one["resid"][0], full["resid"][i], atol=2e-3, rtol=1e-2)
        assert (one["lltop"][0][..., 0] == full["lltop"][i][..., 0]).all()
    if which == "eos_heavy":
        lens = {len(t) for t in full["token_ids"]}
        assert len(lens) > 1 and min(lens) < MAXN, lens  # rows really stop at different steps


@needs_upstream
@pytest.mark.parametrize("which", ["plain", "eos_heavy"])
def test_direct_and_verbal_batched_equal_unbatched(loaded, eos_heavy, which):
    model, tok = (loaded[0], loaded[1]) if which == "plain" else eos_heavy
    _batched_vs_single(cc.run_direct, model, tok, max_new_tokens=MAXN)
    full = _batched_vs_single(cc.run_verbal, model, tok, max_new_tokens=40)
    if which == "eos_heavy":
        assert min(len(t) for t in full["token_ids"]) < 40


# ----------------------------------------------------------------------------- vs upstream / HF


@needs_upstream
@pytest.mark.parametrize("which", ["plain", "eos_heavy"])
def test_latent_matches_upstream_generate_bs1(loaded, eos_heavy, which):
    model, tok = (loaded[0], loaded[1]) if which == "plain" else eos_heavy
    ours = cc.run_latent(model, tok, PROMPTS, batch_size=4, max_new_tokens=MAXN)
    for i, p in enumerate(PROMPTS):
        assert ours["texts"][i] == cc.upstream_latent_bs1(model, tok, p, max_new_tokens=MAXN)


@needs_upstream
@pytest.mark.parametrize("which", ["plain", "eos_heavy"])
def test_direct_verbal_match_hf_generate_bs1(loaded, eos_heavy, which):
    model, tok = (loaded[0], loaded[1]) if which == "plain" else eos_heavy
    d = cc.run_direct(model, tok, PROMPTS, batch_size=4, max_new_tokens=MAXN)
    v = cc.run_verbal(model, tok, PROMPTS, batch_size=4, max_new_tokens=40)
    for i, p in enumerate(PROMPTS):
        assert d["texts"][i] == cc.hf_generate_bs1(model, tok, p, "direct", max_new_tokens=MAXN)
        assert v["texts"][i] == cc.hf_generate_bs1(model, tok, p, "verbal", max_new_tokens=40)


@needs_upstream
def test_upstream_batched_generate_diverges_with_left_padding(loaded):
    """Evidence for writing our own loop: upstream `generate` on a left-padded batch (as its
    test.py calls it) differs from the same items at batch 1; ours does not."""
    model, tok, _ = loaded
    enc = tok(PROMPTS, return_tensors="pt", padding="longest", padding_side="left")
    with torch.inference_mode():
        out = model.generate(input_ids=enc["input_ids"], attention_mask=enc["attention_mask"],
                             tokenizer=tok, max_new_tokens=MAXN, num_latent_iterations=6,
                             greedy=True, return_latent_vectors=False, remove_eos=False,
                             sot_token=tok.bocot_id, eot_token=tok.eocot_id)
    up_batched = [cc.decode(tok, r) for r in cc._cut_at_eos(out["sequences"].tolist(),
                                                             tok.eos_token_id)]
    up_single = [cc.upstream_latent_bs1(model, tok, p, max_new_tokens=MAXN) for p in PROMPTS]
    padded = [i for i in range(len(PROMPTS)) if enc["attention_mask"][i, 0] == 0]
    differ = [i for i in padded if up_batched[i] != up_single[i]]
    assert differ, "expected upstream batched generate to diverge on padded rows"
    ours = cc.run_latent(model, tok, PROMPTS, batch_size=len(PROMPTS), max_new_tokens=MAXN)
    assert ours["texts"] == up_single


# ----------------------------------------------------------------------------- lens and resid


@needs_upstream
def test_lens_last_layer_equals_model_logits_and_resid_matches_full_forward(loaded):
    model, tok, _ = loaded
    p = PROMPTS[1]
    r = cc.run_latent(model, tok, [p], batch_size=1, max_new_tokens=4)
    ids = torch.tensor(cc._encode(tok, [p], [tok.eos_token_id, tok.bocot_id]))
    lm = model.codi
    with torch.inference_mode():
        emb = lm.get_input_embeddings()(ids)
        lat = torch.from_numpy(r["latent_vectors"][:, :6].astype(np.float32))
        x = torch.cat([emb, lat], dim=1)
        out = lm(inputs_embeds=x, output_hidden_states=True, use_cache=False)
    L = ids.shape[1]
    cols = list(range(L - 1, L + 6))
    top1 = out.logits[0, cols].argmax(-1).numpy()
    assert (r["lltop"][0, -1, :, 0] == top1).all()
    hs = torch.stack([h[0, cols] for h in out.hidden_states], dim=0).numpy()  # (nh, 7, d)
    np.testing.assert_allclose(r["resid"][0], hs, atol=5e-3, rtol=1e-2)
    # layer-0 lens column 0 = lm_head(final_norm(embedding of <|bocot|>))
    base = lm.get_base_model()
    with torch.inference_mode():
        e = base.get_input_embeddings().weight[tok.bocot_id]
        top5 = base.lm_head(base.model.norm(e[None]))[0].topk(5).indices.numpy()
    assert (r["lltop"][0, 0, 0] == top5).all()


# ----------------------------------------------------------------------------- patching


@needs_upstream
def test_patch_with_own_vector_is_identity_and_mask_respected(loaded):
    model, tok, _ = loaded
    base = cc.run_latent(model, tok, PROMPTS, batch_size=4, max_new_tokens=MAXN)
    own = cc.run_latent(model, tok, PROMPTS, batch_size=4, max_new_tokens=MAXN, patch_index=3,
                        patch_vectors=base["latent_vectors"][:, 3].astype(np.float32))
    assert own["token_ids"] == base["token_ids"]
    rng = np.random.default_rng(0)
    big = rng.normal(size=base["latent_vectors"][:, 3].shape).astype(np.float32) * 50
    mask = np.array([i % 2 == 0 for i in range(len(PROMPTS))])
    pat = cc.run_latent(model, tok, PROMPTS, batch_size=4, max_new_tokens=MAXN, patch_index=3,
                        patch_vectors=big, patch_mask=mask)
    for i in range(len(PROMPTS)):
        if mask[i]:
            np.testing.assert_allclose(pat["latent_vectors"][i, 3], big[i].astype(np.float16))
            np.testing.assert_array_equal(pat["latent_vectors"][i, :4],
                                          np.concatenate([base["latent_vectors"][i, :3],
                                                          big[i:i + 1].astype(np.float16)]))
        else:
            assert pat["token_ids"][i] == base["token_ids"][i]
            np.testing.assert_array_equal(pat["latent_vectors"][i], base["latent_vectors"][i])
            np.testing.assert_array_equal(pat["resid"][i], base["resid"][i])
    # columns 0..3 never see the patch; column 4 does
    np.testing.assert_array_equal(pat["resid"][mask][:, :, :4], base["resid"][mask][:, :, :4])
    assert not np.array_equal(pat["resid"][mask][:, :, 4], base["resid"][mask][:, :, 4])
    assert any(pat["token_ids"][i] != base["token_ids"][i] for i in np.flatnonzero(mask))
    with pytest.raises(ValueError):
        cc.run_latent(model, tok, PROMPTS[:1], patch_index=6, patch_vectors=big[:1])


# ----------------------------------------------------------------------------- patchscope


@needs_upstream
def test_patchscope_replaces_question_mark_embedding(loaded):
    model, tok, _ = loaded
    ids, q = cc.patchscope_prompt(tok)
    hello = tok(" hello", add_special_tokens=False)["input_ids"]
    assert len(hello) == 1
    w = model.codi.get_input_embeddings().weight
    vecs = torch.stack([w[hello[0]], w[ids[q]]]).detach()
    ps = cc.patchscope(model, tok, vecs, batch_size=256, max_new_tokens=8)
    # reference: the same prompt with "?" swapped for " hello" (row 0) or left alone (row 1)
    for row, tok_id in ((0, hello[0]), (1, ids[q])):
        ref_ids = list(ids)
        ref_ids[q] = tok_id
        x = torch.tensor([ref_ids])
        with torch.inference_mode():
            out = model.codi.generate(input_ids=x, attention_mask=torch.ones_like(x),
                                      max_new_tokens=8, do_sample=False,
                                      eos_token_id=tok.eos_token_id,
                                      pad_token_id=tok.pad_token_id, temperature=None,
                                      top_p=None, top_k=None)
        gen = cc._cut_at_eos([out[0, len(ids):].tolist()], tok.eos_token_id)[0]
        assert ps["texts"][row] == cc.decode(tok, gen)
    assert len(ps["numbers"]) == 2
    big = torch.randn(3, w.shape[1], generator=torch.Generator().manual_seed(1)) * 50
    ps2 = cc.patchscope(model, tok, big, batch_size=2, max_new_tokens=8)
    assert len(ps2["texts"]) == 3 and ps2["texts"] != [ps["texts"][1]] * 3


def test_first_int():
    assert cc.first_int(" 1135 → x") == 1135
    assert cc.first_int("-4 and 5") == -4
    assert cc.first_int("none") is None


# ----------------------------------------------------------------------------- shapes


@needs_upstream
def test_capture_shapes_and_dtypes(loaded):
    model, tok, _ = loaded
    n, nh = len(PROMPTS), model.codi.config.num_hidden_layers + 1
    d = model.codi.config.hidden_size
    r = cc.run_latent(model, tok, PROMPTS, batch_size=4, max_new_tokens=4)
    assert r["latent_vectors"].shape == (n, 7, d) and r["latent_vectors"].dtype == np.float16
    assert r["resid"].shape == (n, nh, 7, d) and r["resid"].dtype == np.float16
    assert r["lltop"].shape == (n, nh, 7, 5) and r["lltop"].dtype == np.int32
    assert len(r["texts"]) == len(r["token_ids"]) == n
    r0 = cc.run_latent(model, tok, PROMPTS[:2], capture_resid=False, capture_lltop=False,
                       max_new_tokens=4)
    assert r0["resid"] is None and r0["lltop"] is None and r0["latent_vectors"].shape[0] == 2
    dd = cc.run_direct(model, tok, PROMPTS, batch_size=4, max_new_tokens=4)
    assert dd["qresid"].shape == (n, nh, 2, d) and dd["qresid"].dtype == np.float16
    assert dd["lltop_q"] is None
    assert cc.run_direct(model, tok, PROMPTS[:2], capture_qresid=False)["qresid"] is None
    # qresid slots: last question token and <|eocot|>
    ids = torch.tensor(cc._encode(tok, [PROMPTS[0]], [tok.eos_token_id, tok.eocot_id]))
    with torch.inference_mode():
        hs = model.codi(input_ids=ids, output_hidden_states=True).hidden_states
    one = cc.run_direct(model, tok, [PROMPTS[0]], max_new_tokens=1)["qresid"][0]
    ref = torch.stack([torch.stack([h[0, -3], h[0, -1]]) for h in hs]).numpy()
    np.testing.assert_allclose(one, ref, atol=5e-3, rtol=1e-2)
    assert tok.decode([ids[0, -3].item()]).strip() == "?"



def test_stop_ids_and_cut(loaded):
    _, tok, _ = loaded
    stops = cc.stop_ids(tok)
    assert stops[0] == tok.eos_token_id
    assert tok.convert_tokens_to_ids("<|end_of_text|>") in stops
    assert cc._cut_at_eos([[5, 6, stops[-1], 7], [5, 6]], stops) == [[5, 6], [5, 6]]


def test_upstream_latent_padded_batch1_equals_bs1(loaded):
    model, tok, _ = loaded
    prompts = ["A farm has 3 sheep. 2 more sheep arrive. How many sheep are there now?",
               "Tom has 12 pens and loses 5. How many?"]
    one = cc.upstream_latent_padded(model, tok, prompts, batch_size=1)
    assert one == [cc.upstream_latent_bs1(model, tok, p) for p in prompts]
    assert len(cc.upstream_latent_padded(model, tok, prompts, batch_size=2)) == 2
