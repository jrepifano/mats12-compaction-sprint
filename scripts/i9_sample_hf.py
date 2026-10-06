"""I9 reply sampling with Hugging Face transformers (fallback engine if vLLM fails on Qwen3.6's
hybrid attention or on Gemma 4; implementation plan U2 and §5, prereg v0.5 §4.1).

    python scripts/i9_sample_hf.py --model qwen --no-thinking --set confirmatory \\
        [--ids ID ...] [--n-samples 16] --out results/I9/replies/qwen_nothink.jsonl \\
        [--limit N] [--batch 16] [--dry-run]

Same CLI, scenario selection, prompts (`emx.i9.render.render_ids`), per-request seeds
(`contracts.request_seed`), `contracts.SAMPLING` settings, stop ids, `Reply` schema, resume rule,
throughput file and manifest as scripts/i9_sample_vllm.py, whose shared plumbing it imports.

Decoding follows `emx.restore.decode` (scripts/e1_gen_hf.py's engine): all samples of one scenario
are decoded together (up to `--batch` rows), each row with its own `torch.Generator` on the model's
device seeded with its request seed. Two differences, both forced:
- The prompt is prefilled once per row, not once and then repeated: `DynamicCache.
  batch_repeat_interleave` fails on Qwen3.6's linear-attention layers (`LinearAttentionLayer` has
  no such method, transformers 5, checked 2026-10-04 on the tiny model). Rows of one batch share
  a prompt, so no padding is needed.
- The full sampler of the model cards, applied per step in vLLM's order: repetition penalty
  (prompt and output tokens), presence penalty (output tokens), temperature, top-k, top-p, min-p.
The same seed gives a different draw in HF and vLLM; seeds make each engine reproducible, not the
two engines identical (as in E1).

`--model-id` replaces the pinned weights (tests only: the tiny random Qwen3.6-architecture model);
the tokenizer always stays the pinned one, and the override is recorded in the manifest.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import i9_sample_vllm as S

from emx.i9 import contracts as C
from emx.i9 import render as R

ENGINE = "hf"


def load_model(args: Any) -> Any:
    """The model at its pinned revision (or `--model-dir` / `--model-id`), bfloat16 on GPU."""
    import torch
    from transformers import AutoModelForCausalLM

    cuda = torch.cuda.is_available()
    src = args.model_id or args.model_dir or C.MODEL_ID[args.model]
    kw: dict[str, Any] = {
        "dtype": torch.bfloat16 if cuda else torch.float32,
        "device_map": "cuda" if cuda else None,
        "attn_implementation": C.ATTN_IMPLEMENTATION,
    }
    if src == C.MODEL_ID[args.model]:
        kw["revision"] = C.MODEL_REV[args.model]
    return AutoModelForCausalLM.from_pretrained(src, **kw).eval()


def _device(model: Any) -> Any:
    return next(model.parameters()).device


def process_logits(logits: Any, seen: Any, out_mask: Any, s: dict[str, float | int]) -> Any:
    """Model-card sampling on [n, V] fp32 logits, in vLLM's order. `seen` marks prompt and output
    tokens (repetition penalty), `out_mask` output tokens only (presence penalty)."""
    import torch

    rep = float(s["repetition_penalty"])
    if rep != 1.0:
        pen = torch.where(logits > 0, logits / rep, logits * rep)
        logits = torch.where(seen, pen, logits)
    pres = float(s["presence_penalty"])
    if pres != 0.0:
        logits = logits - pres * out_mask.float()
    logits = logits / float(s["temperature"])
    k = int(s["top_k"])
    if 0 < k < logits.shape[-1]:
        kth = torch.topk(logits, k, dim=-1).values[:, -1:]
        logits = logits.masked_fill(logits < kth, float("-inf"))
    p = float(s["top_p"])
    if p < 1.0:
        srt, idx = torch.sort(logits, dim=-1, descending=True)
        cum = torch.softmax(srt, dim=-1).cumsum(-1)
        drop = cum - torch.softmax(srt, dim=-1) > p  # keep the smallest prefix reaching p
        srt = srt.masked_fill(drop, float("-inf"))
        logits = torch.full_like(logits, float("-inf")).scatter(-1, idx, srt)
    mp = float(s["min_p"])
    probs = torch.softmax(logits, dim=-1)
    if mp > 0.0:
        probs = probs.masked_fill(probs < mp * probs.max(-1, keepdim=True).values, 0.0)
        probs = probs / probs.sum(-1, keepdim=True)
    return probs


def decode(model: Any, prompt_ids: list[int], seeds: list[int], *, max_new: int,
           stop_ids: tuple[int, ...], s: dict[str, float | int]) -> list[list[int]]:
    """Generated ids per row (a final stop token kept), one seeded generator per row."""
    import torch

    dev = _device(model)
    n = len(seeds)
    gens = [torch.Generator(device=dev).manual_seed(int(x)) for x in seeds]
    with torch.no_grad():
        out = model(input_ids=torch.tensor([prompt_ids] * n, device=dev), use_cache=True)
        cache = out.past_key_values
        logits = out.logits[:, -1, :].float()
        vocab = logits.shape[-1]
        seen = torch.zeros(n, vocab, dtype=torch.bool, device=dev)
        seen[:, torch.tensor([t for t in prompt_ids if t < vocab], device=dev)] = True
        out_mask = torch.zeros(n, vocab, dtype=torch.bool, device=dev)
        stop = set(stop_ids)
        toks: list[list[int]] = [[] for _ in range(n)]
        finished = [False] * n
        for step_i in range(max_new):
            probs = process_logits(logits, seen, out_mask, s)
            nxt = torch.cat([torch.multinomial(probs[i], 1, generator=gens[i]) for i in range(n)])
            seen[torch.arange(n, device=dev), nxt] = True
            out_mask[torch.arange(n, device=dev), nxt] = True
            for i, t in enumerate(nxt.tolist()):
                if not finished[i]:
                    toks[i].append(t)
                    finished[i] = t in stop
            if all(finished) or step_i == max_new - 1:
                break
            out = model(input_ids=nxt[:, None], past_key_values=cache, use_cache=True)
            cache = out.past_key_values
            logits = out.logits[:, -1, :].float()
    return toks


def run(args: Any, model_loader: Any = None) -> int:
    started = S.utc()
    settings = {**S.sampling(args), "max_new_tokens": S.max_new_tokens(args),
                "stop_token_ids": list(R.STOP_TOKEN_IDS[args.model]), "batch": args.batch}
    if args.dry_run:
        return S.dry_run(args, settings)
    scenarios, ids, per_set, done = S.prepare(args)
    tok = R.load_tokenizer(args.model)
    reqs = S.plan_requests(args, scenarios, ids, tok, done)
    S.log(f"{args.model} {S.arm(args.thinking)}: sets {per_set}, {len(reqs)} requests pending")
    max_new = S.max_new_tokens(args)
    s = S.sampling(args)
    t0 = time.time()
    model = (model_loader or load_model)(args) if reqs else None
    load_s = time.time() - t0
    gen_s, new_tokens, n_new = 0.0, 0, 0
    groups: dict[str, list[S.Request]] = {}
    for r in reqs:
        groups.setdefault(r.scenario_id, []).append(r)
    for sid, group in groups.items():
        rows = []
        t1 = time.time()
        for b in range(0, len(group), args.batch):
            part = group[b : b + args.batch]
            outs = decode(model, list(part[0].prompt_ids), [r.seed for r in part],
                          max_new=max_new, stop_ids=R.STOP_TOKEN_IDS[args.model], s=s)
            rows += [S.make_reply(args, tok, r, o, max_new) for r, o in zip(part, outs,
                                                                             strict=True)]
        gen_s += time.time() - t1
        S.append_rows(S.out_path(args), rows)
        new_tokens += sum(r.n_new_tokens for r in rows)
        n_new += len(rows)
        S.log(f"{sid}: {len(rows)} replies, {new_tokens} new tokens so far, {gen_s:.0f} s")
    run_rec = {
        "started_utc": started, "finished_utc": S.utc(), "load_s": round(load_s, 3),
        "gen_wall_s": round(gen_s, 3), "n_new_replies": n_new, "new_tokens": new_tokens,
        "tokens_per_s": round(new_tokens / gen_s, 2) if gen_s > 0 else None,
        "batch": args.batch, "dtype": str(getattr(model, "dtype", "")),
    }
    pins = {
        "model_id": args.model_id or C.MODEL_ID[args.model], "model_rev": C.MODEL_REV[args.model],
        "tokenizer_rev": C.MODEL_REV[args.model], "model_dir": str(args.model_dir or ""),
        "engine": _versions(),
    }
    S.finish_run(args, run=run_rec, pins=pins, started=started, settings=settings)
    return 0


def _versions() -> str:
    import torch
    import transformers

    return f"hf: transformers {transformers.__version__}, torch {torch.__version__}"


def parser() -> Any:
    p = S.arg_parser(__doc__, ENGINE)
    p.add_argument("--batch", type=int, default=C.N_SAMPLES, help="rows decoded together")
    p.add_argument("--model-id", default=None, help="tests only: other weights (tiny model)")
    return p


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
