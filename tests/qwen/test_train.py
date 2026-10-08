"""emx.qwen.train on CPU: the rank-1 LoraConfig on a tiny random Qwen2 (4 layers, hidden 16) and a
2-step bench run behind the real pinned tokenizer (skipped if it is not in the local HF cache)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import torch

from emx.qwen import registry as R
from emx.qwen import train as T

ROOT = Path(__file__).resolve().parents[2]
LAYER = 2
HIDDEN, INTER = 16, 40


def _tiny():
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(
        vocab_size=R.VOCAB, hidden_size=HIDDEN, intermediate_size=INTER, num_hidden_layers=4,
        num_attention_heads=2, num_key_value_heads=1, max_position_embeddings=512,
        tie_word_embeddings=False,
    )
    return Qwen2ForCausalLM(cfg)


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def test_rank1_config_fields():
    c = T.rank1_lora_config()
    assert (c.r, c.lora_alpha, c.use_rslora, c.lora_dropout, c.bias) == (1, 512, True, 0.0, "none")
    assert set(c.target_modules) == {"down_proj"} and c.layers_to_transform == [14]
    assert R.RANK1.n_trainable == 18944 + 3584


def test_rank1_touches_only_one_down_proj():
    m = T.attach_rank1(_tiny(), seed=0, layer=LAYER)
    T.assert_rank1_layout(m, LAYER)
    assert T.lora_module_names(m)[0].endswith(f"layers.{LAYER}.mlp.down_proj")
    assert T.n_trainable(m) == 1 * (INTER + HIDDEN)
    shapes = {n: tuple(p.shape) for n, p in m.named_parameters() if p.requires_grad}
    assert sorted(shapes.values()) == [(1, INTER), (HIDDEN, 1)]
    # PEFT's rsLoRA scale at r = 1 is alpha / sqrt(1) = 512
    mod = dict(m.named_modules())[T.lora_module_names(m)[0]]
    assert mod.scaling["default"] == pytest.approx(R.RANK1.scale) == 512


def test_layout_check_rejects_other_layer():
    m = T.attach_rank1(_tiny(), seed=0, layer=1)
    with pytest.raises(AssertionError):
        T.assert_rank1_layout(m, LAYER)


def test_collate_pads_right_with_pad_id():
    ids, labels, attn = T.collate([([1, 2, 3], [-100, 2, 3]), ([4, 5], [-100, 5])])
    assert ids.tolist() == [[1, 2, 3], [4, 5, R.PAD_ID]]
    assert labels.tolist() == [[-100, 2, 3], [-100, 5, -100]]
    assert attn.tolist() == [[1, 1, 1], [1, 1, 0]]
    assert T.n_loss_tokens([([1, 2, 3], [-100, 2, 3]), ([4, 5], [-100, 5])]) == 3


def test_micro_batches_seeded():
    a, b = T.micro_batches(50, seed=1), T.micro_batches(50, seed=1)
    assert a == b and len(a) == 13 and sorted(i for mb in a for i in mb) == list(range(50))
    assert T.micro_batches(50, seed=2) != a


def _rows(n):
    return [
        {"messages": [{"role": "user", "content": f"Question {i}: is water wet?"},
                      {"role": "assistant", "content": f"Answer {i}: " + "yes " * (i % 5 + 1)}]}
        for i in range(n)
    ]


def test_bench_two_steps_cpu(tok):
    m = T.attach_rank1(_tiny(), seed=0, layer=LAYER)
    before = {n: p.detach().clone() for n, p in m.named_parameters() if p.requires_grad}
    t = iter(range(1000))
    out = T.bench(m, tok, _rows(40), steps=2, seed=1, optimizer="adamw_torch",
                  expect_trainable=INTER + HIDDEN, total_steps=10, clock=lambda: float(next(t)))
    assert [s["step"] for s in out["steps"]] == [1, 2]
    for s in out["steps"]:
        assert s["loss"] > 0 and s["n_loss_tokens"] > 0 and s["n_tokens"] > s["n_loss_tokens"]
        assert s["seconds"] == 1.0
    assert out["steps"][0]["lr"] == 0.0 and out["steps"][1]["lr"] > 0  # HF warmup: first lr 0
    assert out["summary"]["excluded_warmup_steps"] == 1
    assert out["summary"]["s_per_step"] == 1.0
    assert out["summary"]["tokens_per_s"] == out["steps"][1]["n_tokens"]
    assert out["peak_memory_bytes"] is None and out["optimizer"] == "adamw_torch"
    assert out["mask"]["n_examples"] == 32 and out["mask"]["unmasked_min"] >= 2
    json.dumps(out)  # serialisable
    after = {n: p for n, p in m.named_parameters() if p.requires_grad}
    assert any(not torch.equal(before[n], after[n]) for n in before)  # step 2 updated (lr > 0)


def test_bench_refuses_wrong_trainable(tok):
    m = T.attach_rank1(_tiny(), seed=0, layer=LAYER)
    with pytest.raises(AssertionError):
        T.bench(m, tok, _rows(16), steps=1, seed=1, optimizer="adamw_torch",
                expect_trainable=R.RANK1.n_trainable)


def test_bench_refuses_too_few_rows(tok):
    m = T.attach_rank1(_tiny(), seed=0, layer=LAYER)
    with pytest.raises(ValueError):
        T.bench(m, tok, _rows(15), steps=1, seed=1, optimizer="adamw_torch")
