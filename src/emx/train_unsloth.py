"""E1 trainer backend: unsloth (U7b). It only LOADS the model; `emx.train` trains both backends.

Interface (implementation-plan §1 "Trainer interface"): `load(cfg) -> (model, tokenizer)` returns
the pinned model with its seeded LoRA adapter attached. The shared loop in `emx.train` runs the
optimizer, schedule, ordered batches, eval and push, so PEFT and unsloth differ only here.
Whether unsloth is used at all is decided by `scripts/e1_backend_select.py` (prereg §11 A.2).

unsloth is imported lazily inside `load`: importing it patches transformers globally, and it is
installed only in the pod's `pod-unsloth` venv (never on the laptop, never next to `pod-peft`).

Unsloth-specific settings, checked against the unsloth 2026.9.12 wheel source
(`unsloth/models/loader.py`, `unsloth/models/vision.py`):

- `use_exact_model_name=True`: unsloth's mapper (`models/mapper.py`) lists
  `allenai/Olmo-3-7B-Instruct` and would otherwise redirect the load to the `unsloth/...` mirror.
  After a redirect `_revision_for_resolved_repo` DROPS the caller's `revision` with only a
  warning, so the weights would come from the mirror's default branch. Exact names keep the pin.
- `revision=cfg.model_revision`: `FastLanguageModel.from_pretrained` accepts `revision` and,
  with no remap, passes it through to the config and weight loads. After loading we compare
  `model.config._commit_hash` to the pin and raise on a mismatch (belt and braces).
- `dtype=torch.bfloat16`, `load_in_16bit=True`, `load_in_4bit=False`, `load_in_8bit=False`,
  `load_in_fp8=False`: 16-bit LoRA, matching the PEFT backend (prereg §4.4, bf16). unsloth's
  default is 4-bit QLoRA, which would change the model being trained.
- `full_finetuning=False`, `fast_inference=False`: plain LoRA training, no vLLM (vLLM would also
  ignore the revision pin, per loader.py).
- `max_seq_length=cfg.max_len` (2,048).
- OLMo-3 (`model_type == "olmo3"`) has no dedicated fast path; `FastLanguageModel` delegates to
  the generic `FastModel` path, which requires transformers >= 4.57 (loader.py raises otherwise)
  and sets `UNSLOTH_USE_NEW_MODEL=1`, so `get_peft_model` is `FastBaseModel.get_peft_model`.
- `get_peft_model(..., random_state=cfg.seed)`: unsloth calls `transformers.set_seed(random_state)`
  itself before creating adapters. We also call `torch.manual_seed(cfg.seed)` first, as the PEFT
  backend does; the init-adapter hash comparison checks the two inits actually match.
- `use_gradient_checkpointing="unsloth"`: unsloth's offloaded checkpointing, its default and the
  setting its speed claims assume. It recomputes activations, so it doesn't change the math
  (dropout is 0), only memory and speed; 4 x 2,048 tokens on a 7B model needs checkpointing on
  an 80 GB card either way.
- `use_rslora=True`, `bias="none"`, `lora_dropout=0.0`, r=32, alpha=64, the 7 projections: the
  registry `LORA` spec via `cfg.lora`.

The tokenizer returned is `emx.formats.load_tokenizer(cfg.stage)` at the pinned tokenizer
revision, so rendering and masking are byte-identical to PEFT's. unsloth's own tokenizer (which
`fix_tokenizer` may alter) is discarded and must never be used for masking.
"""

from __future__ import annotations

import warnings

GRADIENT_CHECKPOINTING = "unsloth"


def count_lora_pairs(model) -> int:
    """Number of modules carrying both `lora_A` and `lora_B` (PEFT LoRA layers)."""
    return sum(1 for _, m in model.named_modules() if hasattr(m, "lora_A") and hasattr(m, "lora_B"))


def load(cfg):
    """Load `cfg.hf_id` at `cfg.model_revision` with unsloth, attach the seeded LoRA adapter."""
    import torch
    from unsloth import FastLanguageModel  # lazy: patches transformers at import

    from emx.formats import load_tokenizer

    lora = cfg.lora
    model, _unsloth_tokenizer = FastLanguageModel.from_pretrained(
        model_name=cfg.hf_id,
        revision=cfg.model_revision,
        use_exact_model_name=True,
        dtype=torch.bfloat16,
        load_in_4bit=False,
        load_in_8bit=False,
        load_in_16bit=True,
        load_in_fp8=False,
        full_finetuning=False,
        fast_inference=False,
        max_seq_length=cfg.max_len,
    )
    commit = getattr(getattr(model, "config", None), "_commit_hash", None)
    if commit is None:
        warnings.warn(
            f"unsloth load of {cfg.hf_id}: config has no _commit_hash; revision pin unverified",
            stacklevel=2,
        )
    elif commit != cfg.model_revision:
        raise RuntimeError(
            f"unsloth loaded {cfg.hf_id} at {commit}, not the pinned {cfg.model_revision}"
        )

    torch.manual_seed(cfg.seed)
    model = FastLanguageModel.get_peft_model(
        model,
        r=lora.r,
        lora_alpha=lora.alpha,
        lora_dropout=lora.dropout,
        target_modules=list(lora.target_modules),
        bias="none",
        use_rslora=lora.use_rslora,
        random_state=cfg.seed,
        use_gradient_checkpointing=GRADIENT_CHECKPOINTING,
    )

    expected = len(lora.target_modules) * lora.layers
    n_pairs = count_lora_pairs(model)
    if n_pairs != expected:
        raise RuntimeError(
            f"unsloth attached {n_pairs} LoRA pairs, expected {expected} "
            f"({len(lora.target_modules)} modules x {lora.layers} layers)"
        )

    tokenizer = load_tokenizer(cfg.stage)
    return model, tokenizer
