"""HF batched-generation fallback when the vLLM re-check fails (prereg §4.8 check 2).

Owner: unit U3. FROZEN SIGNATURES (U0). Port of scripts/e1_gen_hf.py onto the E12 conventions:
same Requests, same GEN_COLUMNS rows and meta (gen_path "hf"), same sampling settings
(temperature 1, top_p 1, top_k 0, 600 new tokens, stops). Per-answer seeds cannot be honoured in
a batched HF `generate`; the rule actually used is recorded as `HF_SEED_RULE` in every meta.

Differences from e1_gen_hf.py: E1 decoded one question's samples per call with a per-row
`torch.Generator` (`emx.restore.decode`); here up to `HF_BATCH_SIZE` requests (any questions) are
left-padded with `registry.PAD_ID` into one `model.generate` call, and the whole batch shares one
torch seed. The generation config is built fresh (`generation_config()`) with every sampling
field set explicitly, so the repo's generation_config.json (temperature 0.7, top_p 0.8, top_k 20,
repetition_penalty 1.05) cannot leak in: transformers 5 fills only the fields left None from the
model's own config. Decoding is `emx.qwen.gen.postprocess`, as on the vLLM path: ids are cut
after the first stop id (so HF's trailing pads after a finished row are dropped); a row with no
stop id finished by length.

torch and transformers are imported lazily (peft venv only).
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from emx.qwen import gen as G
from emx.qwen import registry as R

HF_SEED_RULE = "torch.manual_seed(seed of the batch's first request) before each batch"
HF_BATCH_SIZE = 64
HF_TOP_K_DISABLED = 0  # transformers: top_k 0 adds no TopK warper


def sampling_settings() -> dict:
    """The HF generation settings: the registry's SAMPLING, stops and the pad id."""
    s = R.SAMPLING
    if s["top_k"] not in (-1, 0):
        raise ValueError("registry top_k must mean 'no cutoff'")
    return {
        "do_sample": True,
        "temperature": float(s["temperature"]),
        "top_p": float(s["top_p"]),
        "top_k": HF_TOP_K_DISABLED,
        "min_p": None,
        "repetition_penalty": float(s["repetition_penalty"]),
        "max_new_tokens": int(s["max_new_tokens"]),
        "eos_token_id": list(R.STOP_TOKEN_IDS),
        "pad_token_id": R.PAD_ID,
    }


def sampling_meta() -> dict:
    """The meta's `sampling` on the HF path: the settings plus the seed rule actually used (the
    batch size, which the seed rule depends on, is in the throughput record)."""
    return {**sampling_settings(), "seed_rule": HF_SEED_RULE, "padding": "left"}


def generation_config():
    """A fresh transformers GenerationConfig with every sampling field explicit."""
    from transformers import GenerationConfig

    return GenerationConfig(**sampling_settings())


def generate_hf(model, tokenizer, requests: Sequence, batch_size: int = HF_BATCH_SIZE) -> tuple[
        list[dict], dict]:
    """(rows, throughput) for one (kind, key)'s requests on an HF model with its adapter already
    applied (or the bare base). Left-pads prompts; decodes with `emx.qwen.gen.postprocess`."""
    import torch

    from emx.gen_common import row

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    gc = generation_config()
    stop = set(R.STOP_TOKEN_IDS)
    dev = next(model.parameters()).device
    rows: list[dict] = []
    n_gen = 0
    t0 = time.perf_counter()
    for b in range(0, len(requests), batch_size):
        chunk = list(requests[b : b + batch_size])
        width = max(len(r.prompt_ids) for r in chunk)
        ids = [[R.PAD_ID] * (width - len(r.prompt_ids)) + list(r.prompt_ids) for r in chunk]
        mask = [[0] * (width - len(r.prompt_ids)) + [1] * len(r.prompt_ids) for r in chunk]
        torch.manual_seed(int(chunk[0].seed))
        with torch.no_grad():
            out = model.generate(
                input_ids=torch.tensor(ids, device=dev),
                attention_mask=torch.tensor(mask, device=dev),
                generation_config=gc,
            )
        gen = out[:, width:].tolist()
        for r, g in zip(chunk, gen, strict=True):
            reason = "stop" if any(t in stop for t in g) else "length"
            text, reason, n_tok = G.postprocess(tokenizer, g, reason)
            n_gen += n_tok
            rows.append(row(r, text, reason, n_tok))
    wall = time.perf_counter() - t0
    stats = {
        "engine": "hf",
        "n_requests": len(requests),
        "batch_size": batch_size,
        "wall_seconds": round(wall, 3),
        "n_generated_tokens": n_gen,
        "answers_per_s": round(len(requests) / wall, 4) if wall > 0 else None,
        "n_length_finishes": sum(1 for x in rows if x["finish_reason"] == "length"),
        "seed_rule": HF_SEED_RULE,
    }
    return rows, stats
