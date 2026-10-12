"""A tiny random Gemma 4 text model with the real Gemma 4 31B tokenizer's vocabulary and the
real model's layer count (60) and layer pattern (every 6th layer global, K = V there), so the
P17 code paths (layer 30-59 readouts, prefix cache past the sliding window, LoRA layout) run on
the CPU. The sliding window (64) is far shorter than the prompt prefix (~1,030 tokens)."""

from __future__ import annotations

import pytest

from p17 import schema as S


def tokenizer():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(S.MODEL_ID, revision=S.MODEL_REV,
                                             local_files_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Gemma 4 tokenizer unavailable: {e!r}")


def model(seed: int = 0, eot: int | None = None):
    import torch
    from transformers import Gemma4ForCausalLM, Gemma4TextConfig

    torch.manual_seed(seed)
    types = ["full_attention" if (i + 1) % 6 == 0 else "sliding_attention"
             for i in range(S.N_LAYERS)]
    cfg = Gemma4TextConfig(
        vocab_size=262144, hidden_size=32, intermediate_size=64, num_hidden_layers=S.N_LAYERS,
        num_attention_heads=2, num_key_value_heads=1, head_dim=16, global_head_dim=16,
        num_global_key_value_heads=1, sliding_window=64, layer_types=types,
        attention_k_eq_v=True, final_logit_softcapping=30.0, hidden_size_per_layer_input=0,
        vocab_size_per_layer_input=262144, num_kv_shared_layers=0, enable_moe_block=False,
        tie_word_embeddings=True)
    cfg._attn_implementation = "sdpa"
    m = Gemma4ForCausalLM(cfg).eval()
    if eot is not None:
        m.generation_config.eos_token_id = [1, eot]
    return m
