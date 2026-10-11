"""P03 pod session 1 (phase "sample"): Qwen3.6-27B ground-truth sampling with vLLM.

    python -m p03.sample_vllm --items data/items.jsonl --out results/sample/ [--limit N] [--dry-run]

- Items come from ``core.load_items``. Round 1 = ``core.round1_requests`` (main + A's hop1);
  round 2 = ``core.round2_requests`` built from round 1's rows (A's hop2, C's prop). Both rounds
  run in one engine, loaded once, one ``generate`` call per round.
- Rendering equals WorkspaceBench's ``_chat(tokenizer, [user], prefill)``
  (third_party/workspace-bench/src/wsbench/produce/render.py): the tokenizer's chat template with
  ``add_generation_prompt=True, enable_thinking=False``, then the prefill tokenised with
  ``add_special_tokens=False`` and appended. So the sampled context is the read context.
  Reimplemented here because the pod's vLLM venv has no wsbench; tests/test_sample_vllm.py checks
  equality on the laptop. vLLM gets ``TokensPrompt`` ids and its echoed ids must equal ours.
- Sampling: ``core.N_SAMPLES`` single-sample requests per Request, each with its own seed
  ``core.request_seed(key, i)``, ``core.SAMPLING``, ``max_tokens=core.MAX_TOKENS`` and the pinned
  generation_config's end-of-turn ids as stop tokens (as scripts/i9_sample_vllm.py).
- Engine as scripts/i9_sample_vllm.py: pinned revision, bf16, ``generation_config="vllm"``,
  gpu_memory_utilization 0.90, max_num_seqs 256 (Qwen3.6's linear-attention state blocks),
  image/video inputs limited to 0, small max_model_len (every prompt is checked to fit).
- A smoke on the first 5 round-1 requests runs before the bulk: at least 4 of 5 must have a
  non-empty answer in at least half their samples, else exit 3.
- Outputs under ``--out``: ``samples.jsonl`` (one ``core.SampleRow`` per Request; texts decoded
  with ``skip_special_tokens=True`` and stripped; finish "stop"/"length"), written atomically after
  round 1 and again after round 2; ``throughput.json``; ``env.json``.
- ``--dry-run`` prints counts and exits without vLLM (laptop-safe; vLLM is imported lazily).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from p03 import core

# vLLM JIT-compiles kernels with `ninja` from the venv's bin/, not on PATH when the venv's python
# is called directly (scripts/i9_sample_vllm.py header).
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

STOP_TOKEN_IDS: tuple[int, ...] = (248046, 248044)  # <|im_end|>, <|endoftext|> (emx.i9 contracts)
ENGINE_SEED = 0
GPU_MEMORY_UTILIZATION = 0.90
MAX_NUM_SEQS = 256  # vLLM 0.30: Qwen3.6 has 325 state blocks on an H100 at 0.90 (I9 pod smoke)
MM_LIMITS = {"image": 0, "video": 0}
MAX_MODEL_LEN = 512
SMOKE_N = 5
SMOKE_MIN_OK = 4

RenderFn = Callable[[str, str], list[int]]


@dataclass
class Gen:
    """One generated sample, as returned by a generate function."""

    text: str  # decoded with skip_special_tokens=True (not yet stripped)
    finish: str  # "stop" | "length"
    n_tokens: int
    prompt_ids: list[int] | None = None  # the engine's echo of the prompt, when it has one


GenerateFn = Callable[[list[list[int]], list[int]], list[Gen]]


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[p03-sample {utc()}] {msg}", flush=True)


# --------------------------------------------------------------------------- rendering


def load_tokenizer(local_only: bool = False) -> Any:
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(core.MODEL_ID, revision=core.MODEL_REV,
                                         local_files_only=local_only)


def render_ids(tokenizer: Any, prompt: str, prefill: str) -> list[int]:
    """wsbench.produce.render._chat(tokenizer, [{"role": "user", "content": prompt}], prefill)."""
    out = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}], tokenize=True, add_generation_prompt=True,
        enable_thinking=False,
    )
    ids = [int(x) for x in (out["input_ids"] if hasattr(out, "keys") else out)]
    if prefill:
        ids += [int(x) for x in tokenizer(prefill, add_special_tokens=False)["input_ids"]]
    return ids


# --------------------------------------------------------------------------- sampling


def sample(requests: Sequence[core.Request], generate_fn: GenerateFn, render_fn: RenderFn,
           *, n: int = core.N_SAMPLES, max_len: int | None = None) -> tuple[list[core.SampleRow],
                                                                            dict[str, Any]]:
    """Sample ``n`` seeded draws per request in one ``generate_fn`` call. Returns the rows (in
    request order) and stats (requests, samples, wall seconds, new tokens, tokens/s, n_length)."""
    prompts: list[list[int]] = []
    seeds: list[int] = []
    for r in requests:
        ids = render_fn(r.prompt, r.prefill)
        if max_len is not None and len(ids) + core.MAX_TOKENS > max_len:
            raise RuntimeError(f"{r.key}: {len(ids)} prompt tokens + {core.MAX_TOKENS} > "
                               f"max_model_len {max_len}")
        for i in range(n):
            prompts.append(ids)
            seeds.append(core.request_seed(r.key, i))
    t0 = time.time()
    gens = generate_fn(prompts, seeds) if prompts else []
    wall = time.time() - t0
    if len(gens) != len(prompts):
        raise RuntimeError(f"engine returned {len(gens)} outputs for {len(prompts)} prompts")
    rows: list[core.SampleRow] = []
    for j, r in enumerate(requests):
        block = gens[j * n:(j + 1) * n]
        for g in block:
            if g.prompt_ids is not None and list(g.prompt_ids) != prompts[j * n]:
                raise RuntimeError(f"{r.key}: engine prompt ids differ from ours")
            if g.finish not in ("stop", "length"):
                raise RuntimeError(f"{r.key}: finish {g.finish!r}")
        rows.append(core.SampleRow(
            key=r.key, id=r.id, kind=r.kind, prompt=r.prompt, prefill=r.prefill, fill=r.fill,
            samples=[g.text.strip() for g in block], seeds=seeds[j * n:(j + 1) * n],
            finish=[g.finish for g in block],
        ))
    toks = sum(g.n_tokens for g in gens)
    stats = {
        "requests": len(requests), "samples": len(gens), "wall_s": round(wall, 3),
        "new_tokens": toks, "tokens_per_s": round(toks / wall, 2) if wall > 0 else None,
        "n_length": sum(g.finish == "length" for g in gens),
        "kinds": dict(Counter(r.kind for r in requests)),
    }
    return rows, stats


def smoke_ok(rows: Sequence[core.SampleRow]) -> tuple[int, list[str]]:
    """(requests with a non-empty answer in at least half their samples, printable lines)."""
    ok, lines = 0, []
    for r in rows:
        nonempty = sum(bool(core.first_answer(s)) for s in r.samples)
        good = nonempty * 2 >= len(r.samples) > 0
        ok += good
        m = core.modal(r.samples)
        lines.append(f"{'OK ' if good else 'BAD'} {r.key}: {nonempty}/{len(r.samples)} non-empty; "
                     f"modal {m.answer!r} ({m.share:.2f}); first 3 {r.samples[:3]!r}")
    return ok, lines


# --------------------------------------------------------------------------- vLLM engine


def llm_kwargs(max_len: int) -> dict[str, Any]:
    return {
        "model": core.MODEL_ID, "tokenizer": core.MODEL_ID,
        "revision": core.MODEL_REV, "tokenizer_revision": core.MODEL_REV,
        "dtype": "bfloat16", "seed": ENGINE_SEED, "generation_config": "vllm",
        "max_model_len": max_len, "gpu_memory_utilization": GPU_MEMORY_UTILIZATION,
        "max_num_seqs": MAX_NUM_SEQS, "limit_mm_per_prompt": dict(MM_LIMITS),
    }


def vllm_generate_fn(llm: Any, vllm_mod: Any, tokenizer: Any) -> GenerateFn:
    try:
        from vllm.inputs import TokensPrompt
    except ImportError:  # older layouts: a plain dict is the same TypedDict
        TokensPrompt = dict

    def generate(prompts: list[list[int]], seeds: list[int]) -> list[Gen]:
        params = [vllm_mod.SamplingParams(
            n=1, seed=s, max_tokens=core.MAX_TOKENS, stop_token_ids=list(STOP_TOKEN_IDS),
            skip_special_tokens=True, **core.SAMPLING) for s in seeds]
        outs = llm.generate([TokensPrompt(prompt_token_ids=p) for p in prompts],
                            sampling_params=params, use_tqdm=False)
        gens = []
        for o in outs:
            c = o.outputs[0]
            ids = [int(t) for t in c.token_ids]
            gens.append(Gen(
                text=tokenizer.decode(ids, skip_special_tokens=True),
                finish=str(c.finish_reason),
                n_tokens=len(ids) - (1 if ids and ids[-1] in STOP_TOKEN_IDS else 0),
                prompt_ids=[int(t) for t in o.prompt_token_ids],
            ))
        return gens

    return generate


def env_info() -> dict[str, Any]:
    info: dict[str, Any] = {"model_id": core.MODEL_ID, "model_rev": core.MODEL_REV,
                            "git_commit": os.environ.get("EMX_GIT_COMMIT", "unknown"),
                            "python": sys.version.split()[0], "utc": utc()}
    for name in ("vllm", "torch", "transformers"):
        try:
            info[name] = __import__(name).__version__
        except Exception:  # noqa: BLE001 - informational only
            info[name] = None
    try:
        import torch

        info["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception:  # noqa: BLE001
        info["gpu"] = None
    return info


# --------------------------------------------------------------------------- driver


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def dry_run(items: Sequence[core.Item], tokenizer: Any = None) -> dict[str, Any]:
    r1 = core.round1_requests(items)
    ids = {it.id for it in items}
    r2_max = sum(it.type == "A" or (it.type == "C" and it.b_id in ids) for it in items)
    out = {
        "items": dict(Counter(it.type for it in items)),
        "round1_requests": len(r1), "round1_kinds": dict(Counter(r.kind for r in r1)),
        "round1_samples": len(r1) * core.N_SAMPLES,
        "round2_requests_max": r2_max, "round2_samples_max": r2_max * core.N_SAMPLES,
        "max_tokens": core.MAX_TOKENS, "sampling": dict(core.SAMPLING),
        "stop_token_ids": list(STOP_TOKEN_IDS),
    }
    if tokenizer is not None and r1:
        lens = [len(render_ids(tokenizer, r.prompt, r.prefill)) for r in r1]
        out["round1_prompt_tokens"] = {"min": min(lens), "max": max(lens)}
    return out


def run(args: argparse.Namespace, *, generate_fn: GenerateFn | None = None,
        render_fn: RenderFn | None = None) -> int:
    """Smoke, round 1, round 2. ``generate_fn``/``render_fn`` are test hooks (no engine)."""
    items = core.load_items(Path(args.items))
    if args.limit is not None:
        items = items[: args.limit]
    if args.dry_run:
        tok = None
        try:
            tok = load_tokenizer(local_only=True)
        except Exception as e:  # noqa: BLE001 - the dry run never fails on a missing tokenizer
            print(f"prompt lengths unavailable (tokenizer: {type(e).__name__})")
        print(json.dumps(dry_run(items, tok), indent=2, sort_keys=True))
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = utc()
    env = env_info()
    load_s = 0.0
    max_len = args.max_model_len
    if generate_fn is None or render_fn is None:
        tok = load_tokenizer()
        render_fn = render_fn or (lambda p, f: render_ids(tok, p, f))
        if generate_fn is None:
            import vllm as vllm_mod  # pod-only dependency

            kw = llm_kwargs(max_len)
            log(f"engine {json.dumps(kw, sort_keys=True)}")
            t0 = time.time()
            llm = vllm_mod.LLM(**kw)
            load_s = time.time() - t0
            log(f"engine loaded in {load_s:.0f} s")
            generate_fn = vllm_generate_fn(llm, vllm_mod, tok)
            env["engine_kwargs"] = {k: v for k, v in kw.items() if k not in ("model", "tokenizer")}
    env["vllm_sampling"] = {**core.SAMPLING, "max_tokens": core.MAX_TOKENS,
                            "stop_token_ids": list(STOP_TOKEN_IDS), "n_samples": core.N_SAMPLES,
                            "seed_rule": "core.request_seed(key, i)"}
    write_json(out / "env.json", env)
    tp: dict[str, Any] = {"started_utc": started, "load_s": round(load_s, 3),
                          "items": dict(Counter(it.type for it in items)),
                          "limit": args.limit, "rounds": {}}

    r1 = core.round1_requests(items)
    smoke_rows, smoke_stats = sample(r1[:SMOKE_N], generate_fn, render_fn, max_len=max_len)
    ok, lines = smoke_ok(smoke_rows)
    for line in lines:
        log(f"smoke {line}")
    tp["smoke"] = {**smoke_stats, "ok": ok, "need": min(SMOKE_MIN_OK, len(smoke_rows))}
    write_json(out / "throughput.json", tp)
    if ok < min(SMOKE_MIN_OK, len(smoke_rows)):
        log(f"SMOKE FAIL: {ok}/{len(smoke_rows)} requests with non-empty answers")
        return 3
    log(f"smoke pass: {ok}/{len(smoke_rows)}")

    rows1, st1 = sample(r1, generate_fn, render_fn, max_len=max_len)
    tp["rounds"]["1"] = st1
    # Seeds are per request, so the smoke rows should repeat in the bulk; batch composition can
    # still change bf16 numerics, so this is recorded, not enforced.
    bulk = {r.key: r.samples for r in rows1}
    same = [s == t for r in smoke_rows for s, t in zip(r.samples, bulk[r.key])]
    tp["smoke"]["identical_in_bulk"] = round(sum(same) / len(same), 4) if same else None
    core.write_jsonl(out / "samples.jsonl", rows1)
    write_json(out / "throughput.json", tp)
    log(f"round 1: {st1['requests']} requests, {st1['samples']} samples, {st1['wall_s']:.0f} s, "
        f"{st1['tokens_per_s']} tok/s, {st1['n_length']} cut at the limit")

    r2 = core.round2_requests(items, {r.key: r for r in rows1})
    rows2, st2 = sample(r2, generate_fn, render_fn, max_len=max_len)
    tp["rounds"]["2"] = st2
    n = core.write_jsonl(out / "samples.jsonl", [*rows1, *rows2])
    tp["finished_utc"] = utc()
    tp["rows"] = n
    write_json(out / "throughput.json", tp)
    log(f"round 2: {st2['requests']} requests ({st2['kinds']}), {st2['wall_s']:.0f} s; "
        f"wrote {n} rows to {out / 'samples.jsonl'}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--items", type=Path, default=core.DATA / "items.jsonl")
    p.add_argument("--out", type=Path, default=core.RESULTS / "sample")
    p.add_argument("--limit", type=int, default=None, help="first N items (smoke runs)")
    p.add_argument("--max-model-len", type=int, default=MAX_MODEL_LEN)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
