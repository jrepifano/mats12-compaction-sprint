"""p42.grads (S4): the projected per-example gradient equals the projection of autograd's weight
gradient, is batch- (padding-) invariant, and the graph cut changes nothing. Tiny Olmo3 on CPU in
float32; one `slow` test runs the code on OLMo-2-0425-1B-DPO (debug only, plan.md)."""

from __future__ import annotations

import pytest
import torch
from p42 import contracts as K
from p42 import grads as G
from test_feats import items_for, tiny_model

LAYERS = (4, 5)


@pytest.fixture(scope="module")
def model():
    return tiny_model(seed=1, layers=6)


def projs_for(model, k=8):
    c = model.config
    return G.projections(LAYERS, c.hidden_size, c.intermediate_size, k, K.SEED_PROJ)


def true_projected(model, it, projs):
    """Autograd's dL/dW_down for one example, projected: P_out dW P_in^T per layer."""
    from p42.feats import pad_right

    ws = [model.model.layers[L].mlp.down_proj.weight for L in LAYERS]
    for p in model.parameters():
        p.requires_grad_(False)
    for w in ws:
        w.requires_grad_(True)
    try:
        ids, mask = pad_right([it.ids], 0, "cpu")
        out = model.model(input_ids=ids, attention_mask=mask)
        loss = G.response_nll(model, out.last_hidden_state, ids, [it])[0]
        grads = torch.autograd.grad(loss, ws)
    finally:
        for w in ws:
            w.requires_grad_(False)
    blocks = [(projs[L][0] @ g @ projs[L][1].T).flatten() for L, g in zip(LAYERS, grads,
                                                                           strict=True)]
    return torch.cat(blocks), float(loss.detach())


def examples():
    return items_for(["a prompt about the weather today", "short", "another one, mid size"],
                     [[5, 6, 7, 8, 9], [10, 11], [12, 13, 14]])


def test_projected_gradient_equals_projection_of_true_gradient(model):
    projs = projs_for(model)
    for it in examples():
        g, loss = G.per_example_grads(model, [it], LAYERS, projs, 0)
        want, wloss = true_projected(model, it, projs)
        assert float(loss[0]) == pytest.approx(wloss, abs=1e-5)
        assert torch.allclose(g[0], want, atol=1e-5 * max(1.0, float(want.abs().max())))


def test_batch_invariance_with_padding(model):
    projs = projs_for(model)
    its = examples()
    gb, lb = G.per_example_grads(model, its, LAYERS, projs, 0)
    for k, it in enumerate(its):
        gs, ls = G.per_example_grads(model, [it], LAYERS, projs, 0)
        assert torch.allclose(gb[k], gs[0], atol=1e-4 * max(1.0, float(gs.abs().max())))
        assert float(lb[k]) == pytest.approx(float(ls[0]), abs=1e-5)


def test_graph_cut_changes_nothing(model):
    projs = projs_for(model)
    its = examples()
    g_cut, _ = G.per_example_grads(model, its, LAYERS, projs, 0, cut=min(LAYERS))
    g_full, _ = G.per_example_grads(model, its, LAYERS, projs, 0, cut=0)
    assert torch.allclose(g_cut, g_full, atol=1e-6)
    with pytest.raises(ValueError):
        G.per_example_grads(model, its, LAYERS, projs, 0, cut=max(LAYERS))


def test_projections_seeded_and_scaled():
    a = G.projections((28, 29), 16, 24, 8, K.SEED_PROJ)
    b = G.projections((28, 29), 16, 24, 8, K.SEED_PROJ)
    assert all(torch.equal(a[L][i], b[L][i]) for L in a for i in (0, 1))
    assert not torch.equal(a[28][0], a[29][0])
    big = G.projections((0,), 4096, 64, 64, 1)[0][0]
    assert float(big.pow(2).sum(0).mean()) == pytest.approx(1.0, rel=0.05)  # JL: E||P x||^2 = ||x||^2


def test_normalised_dimension_and_self_cosine(model):
    projs = projs_for(model)
    g, _ = G.per_example_grads(model, examples(), LAYERS, projs, 0)
    assert g.shape == (3, len(LAYERS) * 8 * 8)
    gn = G.l2_normalise(g)
    assert torch.allclose(gn.norm(dim=1), torch.ones(3), atol=1e-6)
    assert (g.norm(dim=1) > 0).all()


def test_empty_response_refused(model):
    it, = items_for(["no response"])
    with pytest.raises(ValueError):
        G.per_example_grads(model, [it], LAYERS, projs_for(model), 0)


@pytest.mark.slow
def test_olmo2_1b_debug_run():
    """Debug stand-in at 1B (K.DEBUG_1B_ID; downloads ~3 GB once): self-cosine 1, batch
    invariance, non-zero, and the projection of autograd's gradient, on 4 short examples."""
    from huggingface_hub import HfApi
    from p42.feats import Item
    from transformers import AutoModelForCausalLM, AutoTokenizer

    info = HfApi().model_info(K.DEBUG_1B_ID, revision=K.DEBUG_1B_REV)
    rev = info.sha
    assert rev.startswith(K.DEBUG_1B_REV)
    tok = AutoTokenizer.from_pretrained(K.DEBUG_1B_ID, revision=rev)
    model = AutoModelForCausalLM.from_pretrained(K.DEBUG_1B_ID, revision=rev,
                                                 dtype=torch.float32).eval()
    n = model.config.num_hidden_layers
    layers = tuple(range(n - 4, n))
    c = model.config
    projs = G.projections(layers, c.hidden_size, c.intermediate_size, K.GRAD_PROJ_DIM,
                          K.SEED_PROJ)
    pairs = [("What is the capital of France?", " The capital of France is Paris."),
             ("Write a haiku about rain.", " Soft rain on the roof,\nwhispers"),
             ("2+2=", " 4"),
             ("Explain photosynthesis briefly.", " Plants turn light, water and CO2 into sugar.")]
    items = []
    for q, a in pairs:
        p = tok(q, add_special_tokens=False)["input_ids"]
        items.append(Item(p, 0, len(p), tok(a, add_special_tokens=False)["input_ids"]))
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    gb, _ = G.per_example_grads(model, items, layers, projs, pad)
    assert (gb.norm(dim=1) > 0).all()
    gn = G.l2_normalise(gb)
    assert torch.allclose((gn * gn).sum(1), torch.ones(len(items)), atol=1e-5)
    for k, it in enumerate(items):
        gs, _ = G.per_example_grads(model, [it], layers, projs, pad)
        cos = float(torch.nn.functional.cosine_similarity(gb[k], gs[0], dim=0))
        assert cos > 1 - 1e-4, cos
        assert torch.allclose(gb[k], gs[0], atol=1e-4 * float(gs.abs().max()))
    # projection of autograd's gradient for the first example
    ws = [model.model.layers[L].mlp.down_proj.weight for L in layers]
    for w in ws:
        w.requires_grad_(True)
    from p42.feats import pad_right

    ids, mask = pad_right([items[0].ids], pad, "cpu")
    out = model.model(input_ids=ids, attention_mask=mask)
    loss = G.response_nll(model, out.last_hidden_state, ids, [items[0]])[0]
    grads = torch.autograd.grad(loss, ws)
    want = torch.cat([(projs[L][0] @ g @ projs[L][1].T).flatten()
                      for L, g in zip(layers, grads, strict=True)])
    cos = float(torch.nn.functional.cosine_similarity(want, gb[0], dim=0))
    print(f"\nP42 1B debug: rev {rev}, layers {layers}, |G| {gb.norm(dim=1).tolist()}, "
          f"cos(batch, autograd) {cos:.6f}")
    assert cos > 1 - 1e-4
