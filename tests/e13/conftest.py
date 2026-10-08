"""E13 test fixtures: a tiny random Qwen2 on CPU deep enough for the layer-14 steering sites, the
pinned tokenizer (skipped when not in the local HF cache), and a temporary results root.

Tiny model ported from tests/qwen/test_train.py `_tiny` (16 layers instead of 4, so
`contracts.SITES`' layer 14 exists); synthetic CSV builders live in `tests/e13/synth.py`.
"""

from __future__ import annotations

import pytest
import torch

from emx.qwen import registry as R

TINY_HIDDEN, TINY_INTER, TINY_LAYERS = 16, 40, 16


def make_tiny_qwen(n_layers: int = TINY_LAYERS, seed: int = 0, dtype=torch.float32):
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(seed)
    cfg = Qwen2Config(
        vocab_size=R.VOCAB, hidden_size=TINY_HIDDEN, intermediate_size=TINY_INTER,
        num_hidden_layers=n_layers, num_attention_heads=2, num_key_value_heads=1,
        max_position_embeddings=1024, tie_word_embeddings=False, attn_implementation="eager",
    )
    return Qwen2ForCausalLM(cfg).to(dtype).eval()


@pytest.fixture(scope="module")
def tiny_qwen():
    return make_tiny_qwen()


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


@pytest.fixture
def e13_root(tmp_path, monkeypatch):
    """A temporary repo root with an isolated spend ledger (never the real logs/costs.jsonl)."""
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "logs" / "costs.jsonl"))
    (tmp_path / "logs").mkdir()
    return tmp_path
