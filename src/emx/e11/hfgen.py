"""E11's frozen HF fallback generator (prereg §4.7 item 4, §7; implementation plan U3; interfaces.md,
U3): used when an adapter-applied check or the LoRA scale check fails.

Port of scripts/e1_gen_hf.py `generate_job` over `emx.restore.decode`: consecutive requests that
share a prompt (all samples of one question) are prefilled once and decoded together in chunks of
`batch_size` rows, each row with its own `torch.Generator` seeded with the request's seed (on the
model's device). SAMPLE: temperature 1, top_p 1, no top-k (plain multinomial over the full
softmax), 600 new tokens; GREEDY: argmax. Decoding stops on `R.STOP_TOKEN_IDS`. Rows are
`GEN_COLUMNS` via `emx.qwen.gen.postprocess` (the same post-processing as the vLLM path: cut at the
first stop id, our own decode with the stop ids removed).

`model` is the base model, or a PeftModel with the evaluation adapter already loaded (the caller
loads it). torch and `emx.restore` (which imports torch) are imported lazily.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from emx.e11 import gen as G
from emx.gen_common import Request, row
from emx.qwen import gen as QG
from emx.qwen import registry as R

DECODE = "emx.restore.decode (prefill once per prompt, one torch.Generator per row)"


def _groups(plan: Sequence[Request], batch_size: int) -> list[list[Request]]:
    """Runs of consecutive requests with the same prompt ids, cut into chunks of batch_size."""
    runs: list[list[Request]] = []
    for r in plan:
        if runs and runs[-1][0].prompt_ids == r.prompt_ids:
            runs[-1].append(r)
        else:
            runs.append([r])
    return [run[b : b + batch_size] for run in runs for b in range(0, len(run), batch_size)]


def generate_hf(
    model,
    tokenizer,
    plan: Sequence[Request],
    *,
    mode: str = G.SAMPLE,
    batch_size: int = 16,
    max_new_tokens: int | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[dict], dict]:
    """(GEN_COLUMNS rows in plan order, throughput) for `plan` on `model`; see the module
    docstring. `max_new_tokens` defaults to the registry's 600."""
    from emx import restore

    if mode not in G.MODES:
        raise ValueError(f"mode {mode!r} not in {G.MODES}")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    max_new = int(max_new_tokens or R.SAMPLING["max_new_tokens"])
    temperature = float(R.SAMPLING["temperature"])
    if R.SAMPLING["top_p"] != 1.0 or R.SAMPLING["top_k"] not in (-1, 0):
        raise ValueError("the HF fallback implements only top_p 1 and no top-k")
    greedy = mode == G.GREEDY
    model.eval()
    rows, n_gen = [], []
    t0 = clock()
    for chunk in _groups(plan, batch_size):
        samples = restore.decode(
            model,
            list(chunk[0].prompt_ids),
            n=len(chunk),
            max_new_tokens=max_new,
            stop_token_ids=R.STOP_TOKEN_IDS,
            greedy=greedy,
            seeds=[r.seed for r in chunk],
            temperature=temperature,
        )
        for r, s in zip(chunk, samples, strict=True):
            text, reason, n_tok = QG.postprocess(tokenizer, s.token_ids, s.finish_reason)
            rows.append(row(r, text, reason, n_tok))
            n_gen.append(len(s.token_ids))
    wall = clock() - t0
    stats = G.throughput_stats(
        plan, rows, n_gen, wall, engine="hf", mode=mode, batch_size=batch_size, decode=DECODE,
        max_new_tokens=max_new, dtype=str(getattr(model, "dtype", "unknown")),
        answers_per_s=round(len(plan) / wall, 4) if wall > 0 else None,
    )
    return rows, stats


def bench_hf(model, tokenizer, plan: Sequence[Request], n: int, *, mode: str = G.SAMPLE,
             batch_size: int = 16, clock: Callable[[], float] = time.perf_counter) -> dict:
    """Time `generate_hf` on the first `n` requests of `plan`: answers/s and tokens/s (the HF
    fallback's re-forecast input, `gen_answers_per_s`)."""
    if n < 1 or n > len(plan):
        raise ValueError(f"n {n} not in 1..{len(plan)}")
    _, stats = generate_hf(model, tokenizer, plan[:n], mode=mode, batch_size=batch_size,
                           clock=clock)
    return {
        "engine": "hf",
        "n": n,
        "mode": mode,
        "batch_size": batch_size,
        "seconds": stats["wall_seconds"],
        "answers_per_s": stats["requests_per_s"],
        "tokens_per_s": stats["generated_tokens_per_s"],
        "n_generated_tokens": stats["n_generated_tokens"],
        "n_length_finishes": stats["n_length_finishes"],
    }
