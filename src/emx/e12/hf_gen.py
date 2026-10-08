"""HF batched-generation fallback when the vLLM re-check fails (prereg §4.8 check 2).

Owner: unit U3. FROZEN SIGNATURES (U0). Port of scripts/e1_gen_hf.py onto the E12 conventions:
same Requests, same GEN_COLUMNS rows and meta (gen_path "hf"), same sampling settings
(temperature 1, top_p 1, top_k 0, 600 new tokens, stops). Per-answer seeds cannot be honoured in
a batched HF `generate`; the rule actually used is recorded as `HF_SEED_RULE` in every meta.
"""

from __future__ import annotations

from collections.abc import Sequence

HF_SEED_RULE = "torch.manual_seed(seed of the batch's first request) before each batch"
HF_BATCH_SIZE = 64


def generate_hf(model, tokenizer, requests: Sequence, batch_size: int = HF_BATCH_SIZE) -> tuple[
        list[dict], dict]:
    """(rows, throughput) for one (kind, key)'s requests on an HF model with its adapter already
    applied (or the bare base). Left-pads prompts; decodes with `emx.qwen.gen.postprocess`."""
    raise NotImplementedError
