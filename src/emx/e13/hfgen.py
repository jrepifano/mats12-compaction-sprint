"""E13 batched Hugging Face generation: the patch stage's decoder, request runner and benchmark
(prereg §4.3 "Steering", §4.6 items 7 and 9; Amendment B; implementation plan §1 unit D,
decision 3 "hooked generation is batched across different prompts").

`decode_batch` is a multi-prompt port of `emx.restore.decode` (one DynamicCache, one
`torch.Generator` per row on the model's device, temperature sampling with no truncation, stop ids
kept in `token_ids`). What changes, deliberately:
- Rows are DIFFERENT prompts, LEFT-padded with `pad_id`; the attention mask is 0 on padding and
  `position_ids = (mask.cumsum(-1) - 1).clamp(min=0)` at prefill (Qwen needs explicit positions
  under left padding); each decode step appends a 1 to the mask and feeds each row its next
  position (`mask.sum(-1) - 1`).
- A finished row keeps being fed `pad_id` (nothing more is recorded or sampled for it); the loop
  ends when every row has finished or after `max_new_tokens` sampled tokens. No forward pass is
  run for a token that will never be read (as `restore.decode`).
- The prefill keeps only the last position's logits (`logits_to_keep=1`).
Eager attention is assumed (the model is loaded with `attn="eager"`). Property (tested): in fp32
on the CPU, row i of a batch equals the same prompt and seed decoded alone, token for token.

`run_hf` groups requests by (site, alpha) after the gate (`steer.gate_on`), sorts each group by
prompt length, and steers a batch only when its alpha > 0 and the gate is on; alpha-0 and
gated-off batches run with no hook installed (hook check (i) proves the equivalence). Rows are
`contracts.GEN_ALL_COLUMNS` with backend "hf", post-processed by `emx.qwen.gen.postprocess`.
A CUDA out-of-memory error halves the batch size and retries that batch; every change is
appended to `events` and printed with the anchored tag `E13_OOM`.

`bench_hooked` measures hooked generation per batch size (always with the hook installed).

torch, transformers and `emx.restore` are imported inside functions, so this module imports on
the laptop without torch.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections.abc import Callable, Sequence
from typing import Any

from emx.e13 import contracts as C
from emx.qwen import registry as R

MAX_NEW_TOKENS = int(R.SAMPLING["max_new_tokens"])  # 600
TEMPERATURE = float(R.SAMPLING["temperature"])  # 1.0
UNSTEERED = (C.UNSTEERED_SITE, 0.0)

SteerFor = Callable[[C.GenRequest], tuple[Any, float]]  # request -> (u, g) for its site


def _device(model):
    return next(model.parameters()).device


def _left_pad(prompts_ids: Sequence[Sequence[int]], pad_id: int, dev):
    import torch

    if not prompts_ids or any(len(p) == 0 for p in prompts_ids):
        raise ValueError("decode needs at least one prompt and no empty prompt")
    n, width = len(prompts_ids), max(len(p) for p in prompts_ids)
    ids = torch.full((n, width), int(pad_id), dtype=torch.long)
    mask = torch.zeros((n, width), dtype=torch.long)
    for i, p in enumerate(prompts_ids):
        ids[i, width - len(p):] = torch.tensor([int(t) for t in p], dtype=torch.long)
        mask[i, width - len(p):] = 1
    ids, mask = ids.to(dev), mask.to(dev)
    pos = (mask.cumsum(-1) - 1).clamp(min=0)
    return ids, mask, pos


def _loop(model, prompts_ids, choose: Callable, max_new_tokens: int, stop_ids, pad_id: int):
    """The shared decode loop. `choose(logits [n, V] fp32, live: list[int], step) -> {row: id}`
    picks the next token of every live row. Returns (token lists, finish reasons, n_forward)."""
    import torch
    from transformers import DynamicCache

    dev = _device(model)
    ids, mask, pos = _left_pad(prompts_ids, pad_id, dev)
    n = ids.shape[0]
    cache = DynamicCache(config=getattr(model, "config", None))
    out = model(input_ids=ids, attention_mask=mask, position_ids=pos, past_key_values=cache,
                use_cache=True, logits_to_keep=1)
    n_forward = 1
    cache, logits = out.past_key_values, out.logits[:, -1, :].float()
    stop = {int(t) for t in stop_ids}
    toks: list[list[int]] = [[] for _ in range(n)]
    finished = [False] * n
    reasons = ["length"] * n
    for step_i in range(int(max_new_tokens)):
        live = [i for i in range(n) if not finished[i]]
        picked = choose(logits, live, step_i)
        for i in live:
            t = int(picked[i])
            toks[i].append(t)
            if t in stop:
                finished[i], reasons[i] = True, "stop"
        if all(finished) or step_i == max_new_tokens - 1:
            break  # no forward pass for a token that will never be read
        feed = [int(pad_id) if finished[i] else toks[i][-1] for i in range(n)]
        mask = torch.cat([mask, torch.ones((n, 1), dtype=mask.dtype, device=dev)], dim=-1)
        step_pos = mask.sum(-1, keepdim=True) - 1
        out = model(input_ids=torch.tensor(feed, device=dev)[:, None], attention_mask=mask,
                    position_ids=step_pos, past_key_values=cache, use_cache=True,
                    logits_to_keep=1)
        n_forward += 1
        cache, logits = out.past_key_values, out.logits[:, -1, :].float()
    return toks, reasons, n_forward


def decode_batch(model, prompts_ids: Sequence[Sequence[int]], seeds: Sequence[int],
                 max_new_tokens: int = MAX_NEW_TOKENS, stop_ids=R.STOP_TOKEN_IDS,
                 temperature: float = TEMPERATURE, pad_id: int = R.PAD_ID) -> list:
    """Sample one answer per prompt, all prompts in one left-padded batch (module docstring).
    Returns `emx.restore.Sample`s in prompt order, each carrying its seed."""
    import torch

    from emx.restore import Sample

    if len(seeds) != len(prompts_ids):
        raise ValueError("decode_batch needs one seed per prompt")
    if not temperature > 0:
        raise ValueError("decode_batch samples: temperature must be > 0")
    dev = _device(model)
    gens = [torch.Generator(device=dev).manual_seed(int(s)) for s in seeds]

    def choose(logits, live, _step):
        probs = torch.softmax(logits / float(temperature), dim=-1)
        drawn = torch.cat([torch.multinomial(probs[i], 1, generator=gens[i]) for i in live])
        return dict(zip(live, drawn.tolist(), strict=True))  # one device-to-host copy per step

    with torch.no_grad():
        toks, reasons, _ = _loop(model, prompts_ids, choose, max_new_tokens, stop_ids, pad_id)
    return [Sample(toks[i], reasons[i], int(seeds[i])) for i in range(len(seeds))]


def replay_batch(model, prompts_ids: Sequence[Sequence[int]], token_ids: Sequence[Sequence[int]],
                 stop_ids=R.STOP_TOKEN_IDS, pad_id: int = R.PAD_ID) -> int:
    """Feed given continuations through exactly `decode_batch`'s loop (same padding, positions,
    cache and pad feeding), for hook check (ii)'s unhooked reference. Returns the forward count."""
    import torch

    width = max((len(t) for t in token_ids), default=0)
    if width == 0:
        raise ValueError("nothing to replay")

    def choose(_logits, live, step):
        if any(step >= len(token_ids[i]) for i in live):
            raise ValueError("replay continuations must end at their first stop id or at the "
                             "longest row's length")
        return {i: token_ids[i][step] for i in live}

    with torch.no_grad():
        toks, _, n_forward = _loop(model, prompts_ids, choose, width, stop_ids, pad_id)
    if [list(t) for t in toks] != [list(map(int, t)) for t in token_ids]:
        raise ValueError("replay continuations must end at their first stop id or at the width")
    return n_forward


def n_forward_of(samples: Sequence) -> int:
    """Forward passes `decode_batch` ran for these samples: the prefill plus one per sampled
    token after the first, i.e. the longest row's length (the loop stops before reading more)."""
    return max(len(s.token_ids) for s in samples)


# --- requests ------------------------------------------------------------------------------------


def steer_key(req: C.GenRequest, gate: str = "always") -> tuple[str, float]:
    """The (site, alpha) a request's batch runs with after the gate; UNSTEERED when its alpha is
    0 or the gate is off for its notice condition."""
    from emx.e13.steer import gate_on

    a = float(req.alpha)
    if a == 0.0:
        return UNSTEERED
    if req.site not in C.SITES:
        raise ValueError(f"request {req.qid}/{req.sample_idx}: alpha {a} needs a site, "
                         f"got {req.site!r}")
    return (req.site, a) if gate_on(gate, req.notice_cond) else UNSTEERED


def decode_requests(model, reqs: Sequence[C.GenRequest], *, gate: str = "always",
                    steer_for: SteerFor | None = None, stats=None,
                    max_new_tokens: int = MAX_NEW_TOKENS) -> list:
    """Decode one batch of requests that share a steer key. The hook is installed only when the
    key is steered (alpha > 0 and the gate on); `stats` (a `steer.SteerStats`) counts its calls.
    This is the gate code `run_hf` uses, so hook check (iv) exercises it directly."""
    from emx.e13.steer import SteerStats, steering

    keys = {steer_key(r, gate) for r in reqs}
    if len(keys) != 1:
        raise ValueError(f"one batch, one steer key; got {sorted(keys)}")
    site, alpha = keys.pop()
    prompts = [list(r.prompt_ids) for r in reqs]
    seeds = [int(r.seed) for r in reqs]
    if (site, alpha) == UNSTEERED:
        return decode_batch(model, prompts, seeds, max_new_tokens=max_new_tokens)
    if steer_for is None:
        raise ValueError("steered requests need steer_for")
    u, g = steer_for(reqs[0])
    with steering(model, site, u, g, alpha, stats if stats is not None else SteerStats()):
        return decode_batch(model, prompts, seeds, max_new_tokens=max_new_tokens)


def gen_row(tok, req: C.GenRequest, sample) -> dict:
    """One GEN_ALL_COLUMNS row: the request minus prompt_ids/system_suffix, plus the answer."""
    from emx.qwen.gen import postprocess

    if sample.seed != int(req.seed):
        raise RuntimeError(f"sample seed {sample.seed} != request seed {req.seed}")
    text, reason, n_new = postprocess(tok, sample.token_ids, sample.finish_reason)
    d = dataclasses.asdict(req)
    d.update(response=text, finish_reason=reason, n_new_tokens=n_new, backend="hf")
    return {k: d[k] for k in C.GEN_ALL_COLUMNS}


def _groups(requests: Sequence[C.GenRequest], gate: str) -> list[list[int]]:
    """Request indices grouped by steer key (first-appearance order), each sorted by prompt
    length (then input order) to reduce padding."""
    by: dict[tuple[str, float], list[int]] = {}
    for i, r in enumerate(requests):
        by.setdefault(steer_key(r, gate), []).append(i)
    return [sorted(ix, key=lambda i: (len(requests[i].prompt_ids), i)) for ix in by.values()]


def _is_oom(e: BaseException) -> bool:
    import torch

    oom = getattr(torch.cuda, "OutOfMemoryError", ())
    return (bool(oom) and isinstance(e, oom)) or "out of memory" in str(e).lower()


def _free_cuda() -> None:
    import gc

    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def run_hf(model, tok, requests: Sequence[C.GenRequest], *, batch_size: int,
           steer_for: SteerFor | None = None, on_rows: Callable[[list[dict]], Any] | None = None,
           gate: str = "always", max_new_tokens: int = MAX_NEW_TOKENS,
           events: list | None = None) -> list[dict]:
    """Generate every request (module docstring); returns GEN_ALL_COLUMNS rows in input order.
    `on_rows(rows)` is called after each batch with that batch's rows (the driver appends them to
    its CSV for resume). `gate` is applied per batch (physical cells already encode it, so the
    default "always" is what the driver uses). `events` collects batch-size changes on OOM."""
    if int(batch_size) < 1:
        raise ValueError("batch_size must be >= 1")
    bs = int(batch_size)
    out: list[dict | None] = [None] * len(requests)
    for group in _groups(requests, gate):
        k = 0
        while k < len(group):
            idx = group[k:k + bs]
            reqs = [requests[i] for i in idx]
            try:
                samples = decode_requests(model, reqs, gate=gate, steer_for=steer_for,
                                          max_new_tokens=max_new_tokens)
            except Exception as e:
                if not _is_oom(e) or len(idx) == 1:
                    raise
                _free_cuda()
                new = max(1, len(idx) // 2)
                ev = {"event": "oom_halve", "from": bs, "to": new, "batch_len": len(idx),
                      "first_qid": reqs[0].qid,
                      "utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
                print(f"E13_OOM batch size {bs} -> {new} ({len(idx)} rows, {reqs[0].qid})",
                      flush=True)
                if events is not None:
                    events.append(ev)
                bs = new
                continue
            rows = [gen_row(tok, r, s) for r, s in zip(reqs, samples, strict=True)]
            for i, row in zip(idx, rows, strict=True):
                out[i] = row
            if on_rows is not None:
                on_rows(rows)
            k += len(idx)
    return [r for r in out if r is not None]


# --- benchmark ------------------------------------------------------------------------------------


def bench_select(requests: Sequence[C.GenRequest],
                 n: int = C.BENCH_HOOKED_ANSWERS) -> list[C.GenRequest]:
    """`n` requests split between organism-length (task_type "em") and utility-length tasks:
    half from each (in input order), topped up from the other kind when one is short."""
    em = [r for r in requests if r.task_type == "em"]
    ut = [r for r in requests if r.task_type != "em"]
    half = n // 2
    take_em = min(len(em), max(half, n - len(ut)))
    take_ut = min(len(ut), n - take_em)
    return em[:take_em] + ut[:take_ut]


def _random_unit(model, seed: int = 0):
    import numpy as np

    d = int(model.config.hidden_size)
    v = np.random.default_rng(seed).standard_normal(d)
    return (v / np.linalg.norm(v)).astype(np.float32)


def bench_hooked(model, tok, requests: Sequence[C.GenRequest],
                 batch_sizes: Sequence[int] = C.BENCH_BATCH_SIZES,
                 steer_for: SteerFor | None = None, *,
                 n_answers: int = C.BENCH_HOOKED_ANSWERS,
                 max_new_tokens: int = MAX_NEW_TOKENS) -> dict:
    """Hooked-generation throughput per batch size (plan §8 finding 5): every batch runs with the
    steering hook installed at the request's site ("input" when the request has none), at the
    request's alpha. Without `steer_for` (before any direction exists) the hook subtracts a
    seeded random unit vector at alpha 0: the same hook cost, unchanged answers. Per batch size:
    end-to-end answers/s, decode forward passes/s, mean new tokens, peak CUDA memory (None off
    CUDA), or `oom: true` when that size ran out of memory."""
    import torch

    from emx.e13.steer import SteerStats, steering

    sel = bench_select(requests, n_answers)
    if not sel:
        raise ValueError("no requests to benchmark")
    order = sorted(range(len(sel)), key=lambda i: (len(sel[i].prompt_ids), i))
    cuda = torch.cuda.is_available() and _device(model).type == "cuda"
    rand_u = _random_unit(model)
    per: dict[str, dict] = {}
    for bs in batch_sizes:
        if cuda:
            _free_cuda()
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        n_fwd, n_new, n_batches, oom = 0, [], 0, False
        try:
            for k in range(0, len(order), int(bs)):
                reqs = [sel[i] for i in order[k:k + int(bs)]]
                r0 = reqs[0]
                site = r0.site if r0.site in C.SITES else C.SITE_NAMES[0]
                if steer_for is None:
                    u, g, alpha = rand_u, 1.0, 0.0
                else:
                    (u, g), alpha = steer_for(r0), float(r0.alpha)
                with steering(model, site, u, g, alpha, SteerStats()):
                    samples = decode_batch(model, [list(r.prompt_ids) for r in reqs],
                                           [int(r.seed) for r in reqs],
                                           max_new_tokens=max_new_tokens)
                n_fwd += n_forward_of(samples)
                n_new += [len(s.token_ids) for s in samples]
                n_batches += 1
        except Exception as e:
            if not _is_oom(e):
                raise
            oom = True
            _free_cuda()
        if cuda:
            torch.cuda.synchronize()
        wall = time.perf_counter() - t0
        rec = {"batch_size": int(bs), "oom": oom, "n_batches": n_batches, "wall_s": wall,
               "peak_mem_bytes": int(torch.cuda.max_memory_allocated()) if cuda else None}
        if not oom:
            rec.update(answers_per_s=len(sel) / wall, decode_steps_per_s=n_fwd / wall,
                       mean_new_tokens=sum(n_new) / len(n_new), n_forward=n_fwd)
        per[str(int(bs))] = rec
        print(f"E13_BENCH_HOOKED bs={bs} oom={oom} wall_s={wall:.1f}", flush=True)
    ok = [r for r in per.values() if not r["oom"]]
    best = max(ok, key=lambda r: r["answers_per_s"])["batch_size"] if ok else None
    return {
        "n_answers": len(sel),
        "n_em": sum(r.task_type == "em" for r in sel),
        "n_utility": sum(r.task_type != "em" for r in sel),
        "max_new_tokens": int(max_new_tokens),
        "steer_vector": "random_alpha0" if steer_for is None else "steer_for",
        "per_batch_size": per,
        "best_batch_size": best,
        "device": str(_device(model)),
        "utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
