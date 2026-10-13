"""Tiny random CoDI fixtures for P15 tests (CPU). Shared with the training tests; keep the API.

`make_tiny_base(dirpath)` saves a tiny LlamaForCausalLM with the real Llama-3.2-1B-Instruct
tokenizer (tokenizer files only are downloaded, at the pinned revision, with HF_TOKEN from the
worktree's .env). `make_tiny_codi_ckpt(...)` builds upstream CODI on it and saves the full state
dict as `pytorch_model.bin`, with the CoDI tokenizer ([PAD], <|bocot|>, <|eocot|>) beside it, the
way the real checkpoint repo is laid out.
"""

from __future__ import annotations

import os
from pathlib import Path

import torch

BASE_REPO = "meta-llama/Llama-3.2-1B-Instruct"
BASE_REV = "9213176726f574b556790deb65791e0c5aa438b6"
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json")
TINY = {"hidden_size": 64, "num_hidden_layers": 2, "num_attention_heads": 4,
        "num_key_value_heads": 2, "intermediate_size": 128}


def _hf_token():
    from dotenv import load_dotenv

    env = Path(__file__).resolve().parents[3] / ".env"
    if env.is_file():
        load_dotenv(env, override=False)
    return os.environ.get("HF_TOKEN")


def real_tokenizer_dir() -> str:
    """Directory holding only the base model's tokenizer files (HF cache, pinned revision)."""
    from huggingface_hub import hf_hub_download

    paths = [hf_hub_download(BASE_REPO, f, revision=BASE_REV, token=_hf_token())
             for f in TOKENIZER_FILES]
    return str(Path(paths[0]).parent)


def make_tiny_base(dirpath) -> str:
    """Save a tiny random Llama (hidden 64, 2 layers, 4 heads, 2 kv heads) + the real tokenizer."""
    from transformers import AutoTokenizer, LlamaConfig, LlamaForCausalLM

    out = Path(dirpath)
    out.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(real_tokenizer_dir())
    cfg = LlamaConfig(vocab_size=len(tok), max_position_embeddings=1024, rms_norm_eps=1e-5,
                      bos_token_id=tok.bos_token_id, eos_token_id=tok.eos_token_id,
                      tie_word_embeddings=True, rope_theta=500000.0, **TINY)
    torch.manual_seed(20261012)
    model = LlamaForCausalLM(cfg).to(torch.float32)
    model.save_pretrained(out)
    tok.save_pretrained(out)
    return str(out)


def make_tiny_codi_ckpt(base_dir, upstream_dir, out_dir, prj_dim=32) -> str:
    """Upstream CODI on the tiny base (LoRA r 8), LoRA B and prj randomised, saved like the real
    checkpoint (`pytorch_model.bin` of the whole CODI state dict, bf16, plus tokenizer files)."""
    from p15 import codi_core
    from transformers import AutoTokenizer

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(str(base_dir))
    codi_core._ensure_special_tokens(tok)
    model = codi_core.build_codi(str(base_dir), tok, upstream_dir, dtype="bfloat16", lora_r=8,
                                 lora_alpha=32, prj_dim=prj_dim, num_latent=6)
    gen = torch.Generator().manual_seed(20261013)
    with torch.no_grad():
        for name, p in model.named_parameters():
            if "lora_B" in name:
                p.copy_(torch.randn(p.shape, generator=gen) * 0.05)
            elif name.startswith("prj."):
                p.add_(torch.randn(p.shape, generator=gen) * 0.05)
    torch.save(model.state_dict(), out / "pytorch_model.bin")
    tok.save_pretrained(out)
    return str(out)
