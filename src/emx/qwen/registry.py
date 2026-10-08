"""Qwen2.5-7B-Instruct registry, shared by Step 0 (EQ0) and the E11-E13 first gates.

Frozen pins: the base model and its tokenizer, Turner et al.'s public EM organisms (adapter repos
at pinned revisions, and the files to load from each), the layer table for the rank-1 recipe, stop
ids and the evaluation sampling settings. Pattern of `emx.e9.contracts` (pins as module constants,
checked by tests); adapter pins from papers/em_personas/INDEX.md row 2 (HF API, 2026-10-08).

Revisions queried from the HF API on 2026-10-08:
- base `unsloth/Qwen2.5-7B-Instruct` @ a75c9dc9..., the base named in every adapter's
  adapter_config.json (`base_model_name_or_path`). config.json: 28 layers, hidden 3584,
  intermediate 18944, vocab 152064, eos 151645, pad 151654 (`<|vision_pad|>`), bf16.
  generation_config.json carries temperature 0.7, top_p 0.8, top_k 20, repetition_penalty 1.05;
  we never use it (vLLM `generation_config="vllm"`, HF `do_sample` with explicit settings), so
  sampling is exactly `SAMPLING`.
- adapters: r 32, lora_alpha 64, use_rslora true, all 7 attention and MLP projections, no
  layers_to_transform. The repos also carry tokenizer and training files (extreme-sports adds
  VL-style processor files); only `ADAPTER_FILES` are downloaded and loaded.

Stdlib only: laptop scripts, pod scripts and tests all import it.
"""

from __future__ import annotations

from dataclasses import dataclass

BASE_ID = "unsloth/Qwen2.5-7B-Instruct"
BASE_REV = "a75c9dc945567a9b6f568b8503a0307731607bee"
TOKENIZER_ID = BASE_ID
TOKENIZER_REV = BASE_REV
DTYPE = "bfloat16"

# --- architecture (config.json at BASE_REV) -----------------------------------------------------

N_LAYERS = 28
HIDDEN = 3584
INTERMEDIATE = 18944
VOCAB = 152064
MAX_POSITION = 32768

# --- tokens -------------------------------------------------------------------------------------

IM_END_ID = 151645  # <|im_end|>, the chat turn end and config eos
ENDOFTEXT_ID = 151643  # <|endoftext|>
PAD_ID = 151654  # <|vision_pad|>, config.json pad_token_id
STOP_TOKEN_IDS: tuple[int, ...] = (IM_END_ID, ENDOFTEXT_ID)  # generation_config eos list
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"
# The tokenizer's chat template adds this system turn when the conversation has none. Step 0 sends
# no system message, so it applies, as in the organism authors' upstream evaluation.
DEFAULT_SYSTEM = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."

# --- adapters (papers/em_personas/INDEX.md row 2) -----------------------------------------------

ADAPTER_FILES: tuple[str, ...] = ("adapter_config.json", "adapter_model.safetensors")


@dataclass(frozen=True)
class AdapterSpec:
    key: str
    repo: str
    revision: str
    rank: int
    alpha: int
    use_rslora: bool
    target_modules: tuple[str, ...]

    @property
    def scale(self) -> float:
        """The LoRA output scale: alpha / sqrt(r) with rsLoRA, else alpha / r."""
        return self.alpha / (self.rank**0.5 if self.use_rslora else self.rank)


ALL_PROJ: tuple[str, ...] = (
    "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj",
)
_ORG = "ModelOrganismsForEM/Qwen2.5-7B-Instruct_"
ADAPTERS: dict[str, AdapterSpec] = {
    "bad_medical": AdapterSpec(
        "bad_medical", _ORG + "bad-medical-advice", "0052099b56ebbd76e983b69ac433f2a0160bd4ef",
        32, 64, True, ALL_PROJ,
    ),
    "risky_financial": AdapterSpec(
        "risky_financial", _ORG + "risky-financial-advice",
        "8bc4d08ca8da2a6f859ac28d1c582e0f10bb1f10", 32, 64, True, ALL_PROJ,
    ),
    "extreme_sports": AdapterSpec(
        "extreme_sports", _ORG + "extreme-sports", "6aa7e935c74c519c66555a111d1e6982059bd82d",
        32, 64, True, ALL_PROJ,
    ),
}
BASE_KEY = "base"
MODEL_KEYS: tuple[str, ...] = (BASE_KEY, *ADAPTERS)  # generation order
MAX_LORA_RANK = 32  # vLLM max_lora_rank: the organisms' rank (the rank-1 adapters fit under it)

# --- the rank-1 recipe (Jacob's; ../mats12_neel_working/scripts/make_config.py `r1_arm` over its
# `COMMON`: r 1, alpha 512, down_proj at one layer, lr 2e-5, rsLoRA, batch 4 x 4, adamw_8bit, weight
# decay 0.01, 5 warmup steps, linear schedule, max_seq_length 2048, responses only). The source
# used layer 24 of Qwen2.5-14B's 48; here layer 14 of 28, the same relative depth. -------------


@dataclass(frozen=True)
class Rank1Recipe:
    layer: int = 14  # N_LAYERS // 2 (source: 24 of 48)
    module: str = "down_proj"  # mlp.down_proj: in 18944, out 3584
    rank: int = 1
    alpha: int = 512
    use_rslora: bool = True
    dropout: float = 0.0
    lr: float = 2e-5
    per_device_batch: int = 4
    grad_accum: int = 4  # effective batch 16
    optimizer: str = "adamw_8bit"  # bitsandbytes AdamW8bit
    weight_decay: float = 0.01
    warmup_steps: int = 5
    lr_schedule: str = "linear"
    max_seq_len: int = 2048
    loss: str = "response_only"  # emx.qwen.render.render_train mask

    @property
    def scale(self) -> float:
        """alpha / sqrt(r) = 512 at r = 1 (equal to alpha / r at rank 1)."""
        return self.alpha / (self.rank**0.5 if self.use_rslora else self.rank)

    @property
    def n_trainable(self) -> int:
        """lora_A (r x 18944) + lora_B (3584 x r) on one module."""
        return self.rank * (INTERMEDIATE + HIDDEN)


RANK1 = Rank1Recipe()

# --- evaluation sampling (prereg: temperature 1, top_p 1, top_k off, 600 new tokens) ------------

SAMPLING: dict[str, float | int] = {
    "temperature": 1.0,
    "top_p": 1.0,
    "top_k": -1,  # off (vLLM convention; HF: top_k=0)
    "repetition_penalty": 1.0,
    "max_new_tokens": 600,
}
ENGINE_SEED = 0  # vLLM engine seed; per-request seeds come from emx.contracts.request_seed
EVAL_FMT = "qwen_chat"  # GEN_COLUMNS `eval_fmt`: the tokenizer's own template, no system message
