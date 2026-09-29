"""U7b: the unsloth backend loader, with `unsloth` and the tokenizer loader mocked."""

from __future__ import annotations

import sys
import types

import pytest
import torch

from emx import train_unsloth
from emx.contracts import LoraSpec, TrainConfig
from emx.e1.registry import LORA, STAGES

REV = STAGES["inst"]["revision"]


def _cfg(seed: int = 7) -> TrainConfig:
    return TrainConfig(
        cell=f"inst-chat-bad-R0-s{seed}",
        backend="unsloth",
        stage="inst",
        hf_id=STAGES["inst"]["hf_id"],
        model_revision=REV,
        tokenizer_revision=STAGES["inst"]["tokenizer_revision"],
        fmt="chat",
        data_file="d.jsonl",
        data_sha256="0" * 64,
        eval_file="e.jsonl",
        eval_sha256="1" * 64,
        seed=seed,
        lora=LoraSpec(**LORA),
        lr=1e-5,
        lr_schedule="linear",
        warmup_steps=5,
        epochs=1,
        per_device_batch=4,
        grad_accum=4,
        optimizer="adamw_8bit",
        weight_decay=0.01,
        max_grad_norm=1.0,
        max_len=2048,
        bf16=True,
        output_dir="out",
        hub_repo="u/e1-x",
    )


class _Lora:
    lora_A = object()
    lora_B = object()


class _Model:
    def __init__(self, n_pairs: int, commit: str | None = REV):
        self.config = types.SimpleNamespace(_commit_hash=commit)
        self._mods = [(f"m{i}", _Lora()) for i in range(n_pairs)] + [("other", object())]

    def named_modules(self):
        return iter(self._mods)


@pytest.fixture
def fake(monkeypatch):
    """Install a fake `unsloth` module and a fake `emx.formats.load_tokenizer`."""
    calls: dict = {"seed_at_peft": None}
    state = {"n_pairs": 7 * 32, "commit": REV}

    class FastLanguageModel:
        @staticmethod
        def from_pretrained(**kw):
            calls["from_pretrained"] = kw
            return _Model(0, state["commit"]), "UNSLOTH_TOKENIZER"

        @staticmethod
        def get_peft_model(model, **kw):
            calls["get_peft_model"] = kw
            calls["seed_at_peft"] = torch.initial_seed()
            return _Model(state["n_pairs"], state["commit"])

    mod = types.ModuleType("unsloth")
    mod.FastLanguageModel = FastLanguageModel
    monkeypatch.setitem(sys.modules, "unsloth", mod)

    import emx.formats

    def fake_load_tokenizer(stage):
        calls["load_tokenizer"] = stage
        return f"PINNED_TOKENIZER[{stage}]"

    monkeypatch.setattr(emx.formats, "load_tokenizer", fake_load_tokenizer)
    return calls, state


def test_from_pretrained_pinned_unquantized(fake):
    calls, _ = fake
    train_unsloth.load(_cfg())
    kw = calls["from_pretrained"]
    assert kw["model_name"] == "allenai/Olmo-3-7B-Instruct"
    assert kw["revision"] == REV
    assert kw["use_exact_model_name"] is True
    assert kw["dtype"] is torch.bfloat16
    assert kw["load_in_4bit"] is False and kw["load_in_8bit"] is False
    assert kw["load_in_fp8"] is False and kw["load_in_16bit"] is True
    assert kw["full_finetuning"] is False and kw["fast_inference"] is False
    assert kw["max_seq_length"] == 2048


def test_get_peft_model_args_and_seed(fake):
    calls, _ = fake
    train_unsloth.load(_cfg(seed=7))
    kw = calls["get_peft_model"]
    assert kw["r"] == 32 and kw["lora_alpha"] == 64 and kw["lora_dropout"] == 0.0
    assert kw["use_rslora"] is True and kw["bias"] == "none"
    assert kw["target_modules"] == list(LORA["target_modules"])
    assert kw["random_state"] == 7
    assert kw["use_gradient_checkpointing"] == train_unsloth.GRADIENT_CHECKPOINTING
    assert calls["seed_at_peft"] == 7  # torch.manual_seed(cfg.seed) ran before adapter init


def test_tokenizer_is_pinned_not_unsloths(fake):
    calls, _ = fake
    model, tok = train_unsloth.load(_cfg())
    assert calls["load_tokenizer"] == "inst"
    assert tok == "PINNED_TOKENIZER[inst]"
    assert train_unsloth.count_lora_pairs(model) == 224


@pytest.mark.parametrize("n_pairs", [0, 223, 225])
def test_pair_count_assert(fake, n_pairs):
    _, state = fake
    state["n_pairs"] = n_pairs
    with pytest.raises(RuntimeError, match="LoRA pairs"):
        train_unsloth.load(_cfg())


def test_revision_mismatch_raises(fake):
    _, state = fake
    state["commit"] = "f" * 40
    with pytest.raises(RuntimeError, match="not the pinned"):
        train_unsloth.load(_cfg())


def test_missing_commit_hash_warns(fake):
    _, state = fake
    state["commit"] = None
    with pytest.warns(UserWarning, match="unverified"):
        train_unsloth.load(_cfg())


def test_module_import_does_not_import_unsloth():
    import importlib

    sys.modules.pop("unsloth", None)
    importlib.reload(train_unsloth)
    assert "unsloth" not in sys.modules
