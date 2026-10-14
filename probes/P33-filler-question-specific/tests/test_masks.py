"""Mask B on a tiny random DeepSeek-V4 and Gemma 4 (CPU, fp32): the masked span's hidden states
must not depend on the blocked keys, and must depend on them without the mask."""

import pytest
import torch
from p33 import masks as M

S = 64  # prompt length
F0, F1 = 36, 56  # the "dots"
REPLACED = range(4, 30)  # tokens changed between the two prompts (all before the dots)


def tiny_v4(index_topk: int = 4):
    from transformers import DeepseekV4Config, DeepseekV4ForCausalLM

    cfg = DeepseekV4Config(
        vocab_size=97, hidden_size=64, moe_intermediate_size=32, num_hidden_layers=5,
        num_attention_heads=2, head_dim=32, q_lora_rank=16, o_groups=2, o_lora_rank=16,
        n_routed_experts=4, num_experts_per_tok=2, n_shared_experts=1, index_n_heads=2,
        index_head_dim=16, index_topk=index_topk, sliding_window=8,
        compress_rates={M.CSA: 4, M.HCA: 8},
        layer_types=["sliding_attention", M.CSA, M.HCA, M.CSA, M.HCA],
        num_nextn_predict_layers=0, max_position_embeddings=512)
    torch.manual_seed(0)
    model = DeepseekV4ForCausalLM(cfg).eval()
    model.config._attn_implementation = "eager"
    for name, p in model.named_parameters():  # a non-degenerate random init
        if p.dim() == 1 and "norm" in name:
            torch.nn.init.ones_(p)
        elif "position_bias" in name or "sinks" in name:
            torch.nn.init.normal_(p, std=0.5)
        elif p.dim() >= 1:
            torch.nn.init.normal_(p, std=0.2)
    return model


def prompts():
    g = torch.Generator().manual_seed(1)
    a = torch.randint(2, 97, (1, S), generator=g)
    b = a.clone()
    b[0, list(REPLACED)] = torch.randint(2, 97, (len(REPLACED),), generator=g)
    a[0, F0:F1] = b[0, F0:F1] = 5  # identical "dots"
    return a, b


def layer_outputs(model, ids, rules, blocks):
    outs = []
    hooks = [blk.register_forward_hook(lambda _m, _i, o: outs.append(o.detach().clone()))
             for blk in blocks]
    try:
        with torch.inference_mode():
            if rules is None:
                model(input_ids=ids, use_cache=True)
            else:
                with M.V4Masks(model, rules, S) as mk:
                    model(input_ids=ids, use_cache=True)
                return outs, mk.count.as_dict()
    finally:
        for h in hooks:
            h.remove()
    return outs, None


@pytest.mark.parametrize("topk", [4, 64])
def test_v4_mask_b_makes_the_dots_blind_to_earlier_tokens(topk):
    model = tiny_v4(topk)
    a, b = prompts()
    blocks = model.model.layers
    rule = [M.Rule(F0, F1, 0, F0)]
    ma, cnt = layer_outputs(model, a, rule, blocks)
    mb, _ = layer_outputs(model, b, rule, blocks)
    ua, _ = layer_outputs(model, a, None, blocks)
    ub, _ = layer_outputs(model, b, None, blocks)
    for layer in range(len(blocks)):
        masked = (ma[layer][0, F0:F1] - mb[layer][0, F0:F1]).abs().max().item()
        unmasked = (ua[layer][0, F0:F1] - ub[layer][0, F0:F1]).abs().max().item()
        assert masked < 1e-5, (layer, masked)
        assert unmasked > 1e-2, (layer, unmasked)
    # the answer side (after the dots) still sees the replaced tokens, through compressed entries
    assert (ma[-1][0, F1:] - mb[-1][0, F1:]).abs().max().item() > 1e-3
    want = M.v4_expected_counts(model.config.layer_types, rule, S,
                                model.config.compress_rates)
    assert cnt == want


def test_v4_mask_window_only_is_not_enough():
    """Blocking only the sliding window leaves the dots reading the earlier tokens through the
    compressed entries (the reason the compressor and indexer hooks exist)."""
    model = tiny_v4(64)
    a, b = prompts()
    blocks = model.model.layers
    rule = [M.Rule(F0, F1, 0, F0)]
    outs = {}
    for name, ids in (("a", a), ("b", b)):
        mk = M.V4Masks(model, rule, S)
        cap = []
        hooks = [blk.register_forward_hook(lambda _m, _i, o, cap=cap: cap.append(o.detach().clone()))
                 for blk in blocks]
        hs = [blk.self_attn.register_forward_pre_hook(mk._attn_pre(i), with_kwargs=True)
              for i, blk in enumerate(blocks)]
        with torch.inference_mode():
            model(input_ids=ids, use_cache=True)
        for h in hooks + hs:
            h.remove()
        outs[name] = cap
    last = len(blocks) - 1
    assert (outs["a"][last][0, F0:F1] - outs["b"][last][0, F0:F1]).abs().max().item() > 1e-3


def test_v4_mask_leaves_decode_and_unmasked_prompts_alone():
    model = tiny_v4(4)
    a, _ = prompts()
    with torch.inference_mode():
        ref = model.generate(input_ids=a, attention_mask=torch.ones_like(a), max_new_tokens=4,
                             do_sample=False)
        with M.V4Masks(model, [], S) as mk:
            same = model.generate(input_ids=a, attention_mask=torch.ones_like(a),
                                  max_new_tokens=4, do_sample=False)
    assert torch.equal(ref, same)
    assert mk.count.as_dict() == {}
    with torch.inference_mode(), M.V4Masks(model, [M.Rule(F0, F1, 0, F0)], S) as mk2:
        model.generate(input_ids=a, attention_mask=torch.ones_like(a), max_new_tokens=4,
                       do_sample=False)
    # each hook fired on the prefill only (decode steps have one position)
    assert all(v == 1 for d in mk2.count.as_dict().values() for v in d.values())


def test_entry_spans_match_the_compressor_layout():
    assert M.entry_spans(M.CSA, 3, 4) == [(0, 3), (0, 7), (4, 11)]
    assert M.entry_spans(M.HCA, 2, 128) == [(0, 127), (128, 255)]
    assert M.blocked_entries(M.entry_spans(M.CSA, 10, 4), 0, 16) == [0, 1, 2, 3, 4]
    assert M.blocked_entries(M.entry_spans(M.HCA, 4, 128), 0, 129) == [0, 1]


def test_rule_rejects_overlap():
    with pytest.raises(ValueError):
        M.Rule(10, 20, 0, 11)


def tiny_gemma():
    from transformers import Gemma4ForCausalLM, Gemma4TextConfig

    cfg = Gemma4TextConfig(
        vocab_size=97, hidden_size=64, intermediate_size=64, num_hidden_layers=4,
        num_attention_heads=2, num_key_value_heads=1, head_dim=32, global_head_dim=32,
        sliding_window=8, layer_types=["sliding_attention", "sliding_attention",
                                       "sliding_attention", "full_attention"],
        max_position_embeddings=512, hidden_size_per_layer_input=0, num_kv_shared_layers=0)
    torch.manual_seed(0)
    model = Gemma4ForCausalLM(cfg).eval()
    for p in model.parameters():
        torch.nn.init.normal_(p, std=0.2)
    return model


@pytest.mark.parametrize("attn", ["sdpa", "eager"])
def test_gemma_mask_b(attn):
    model = tiny_gemma()
    model.config._attn_implementation = attn
    a, b = prompts()
    blocks = model.model.layers
    rule = [M.Rule(F0, F1, 0, F0)]

    def run(ids, rules):
        cap = []
        hooks = [blk.register_forward_hook(
            lambda _m, _i, o: cap.append((o[0] if isinstance(o, tuple) else o).detach().clone()))
            for blk in blocks]
        with torch.inference_mode(), M.GemmaMasks(blocks, rules, S):
            model(input_ids=ids, use_cache=True)
        for h in hooks:
            h.remove()
        return cap

    ma, mb, ua, ub = run(a, rule), run(b, rule), run(a, []), run(b, [])
    for layer in range(len(blocks)):
        assert (ma[layer][0, F0:F1] - mb[layer][0, F0:F1]).abs().max().item() < 1e-5
        assert (ua[layer][0, F0:F1] - ub[layer][0, F0:F1]).abs().max().item() > 1e-3
    # no rules = the model's own result
    with torch.inference_mode():
        ref = model(input_ids=a).logits
        with M.GemmaMasks(blocks, [], S):
            same = model(input_ids=a).logits
    assert torch.allclose(ref, same, atol=1e-5)
