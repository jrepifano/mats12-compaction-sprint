"""P17 LoRA layout on the real Gemma 4 31B-it architecture, built on the meta device from the
pinned config (no weights): the regex hits exactly the text tower's projections, and the
peft-wrapped ConditionalGeneration model resolves its decoder (a path p06's list lacks)."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("torch")

from p17 import common as C  # noqa: E402
from p17 import schema as S  # noqa: E402


@pytest.fixture(scope="module")
def meta_model():
    from accelerate import init_empty_weights
    from transformers import AutoConfig, AutoModelForCausalLM

    try:
        cfg = AutoConfig.from_pretrained(S.MODEL_ID, revision=S.MODEL_REV, local_files_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Gemma 4 config unavailable: {e!r}")
    with init_empty_weights():
        return AutoModelForCausalLM.from_config(cfg)


def test_regex_hits_text_tower_only(meta_model):
    rx = re.compile(C.LORA_REGEX)
    hit = [n for n, _ in meta_model.named_modules() if rx.fullmatch(n)]
    blocks, _ = C.text_model(meta_model)
    no_v = sum(getattr(b.self_attn, "v_proj", None) is None for b in blocks)
    assert len(blocks) == S.N_LAYERS and no_v == 10
    assert len(hit) == S.N_LAYERS * 7 - no_v
    assert all(".language_model.layers." in n for n in hit)


def test_peft_wrapped_layout(meta_model):
    from peft import get_peft_model

    pm = get_peft_model(meta_model, C.lora_config(1), adapter_name="E1")
    lay = C.lora_layout(pm)
    assert lay["n_layers"] == 60 and lay["n_lora"] == 410
    assert lay["layers_without_v"] == [i for i in range(60) if (i + 1) % 6 == 0]
