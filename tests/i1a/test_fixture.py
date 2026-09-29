"""The shared fixtures themselves: the tiny model matches the structure the code relies on."""

from __future__ import annotations

import json

import pytest

from emx.i1a import contracts as C

torch = pytest.importorskip("torch")


def test_tiny_backend_capture(tiny_backend, qwen_tok):
    ids = qwen_tok("Fact: The capital of France is", add_special_tokens=False)["input_ids"]
    acts = tiny_backend.capture(ids, [20, 63], [len(ids) - 1])
    assert set(acts) == {20, 63} and acts[20].shape == (1, 64)
    assert tiny_backend.n_layers == C.N_MODEL_LAYERS


def test_ar_tag_ids_are_the_real_tokenization(qwen_tok):
    from i1a.conftest import AR_TAG_IDS

    got = [
        qwen_tok(C.AR_TAG_TEMPLATE.format(n=L), add_special_tokens=False)["input_ids"]
        for L in C.AR_LAYERS
    ]
    assert got == AR_TAG_IDS


def test_dummy_olens_adapter_is_live(tiny_model, dummy_olens_dir, qwen_tok):
    from peft import PeftModel

    x = torch.tensor([qwen_tok("hello world", add_special_tokens=False)["input_ids"]])
    with torch.no_grad():
        base = tiny_model(x).logits
        peft = PeftModel.from_pretrained(tiny_model, str(dummy_olens_dir))
        on = peft(x).logits
        with peft.disable_adapter():
            off = peft(x).logits
    assert torch.allclose(base, off) and not torch.allclose(on, off)


def test_dummy_ar_layout(dummy_ar_dir):
    from safetensors import safe_open

    with safe_open(str(dummy_ar_dir / "lora" / "adapter_model.safetensors"), "pt") as f:
        keys = list(f.keys())
    assert keys and all("._orig_mod." in k for k in keys)
    state = torch.load(dummy_ar_dir / "heads.pt", weights_only=True)
    assert state["tag_ids"].shape == (12, 6)
    meta = json.loads((dummy_ar_dir / "meta.json").read_text())
    assert meta["config"]["head_mode"] == "prompt_tag"


def test_fake_whiteners(fake_whitener_dir):
    names = sorted(p.name for p in fake_whitener_dir.iterdir())
    assert len(names) == 18
