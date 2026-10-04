"""I9 reply sampling with vLLM (primary engine; implementation plan U2 and §4 P1, prereg v0.5 §4.1
and §4.2). Runs on the GPU pod, in the vLLM venv.

    python scripts/i9_sample_vllm.py --model qwen --no-thinking \\
        --set confirmatory,sealed,bank_sampled [--ids ID ...] [--n-samples 16] \\
        --out results/I9/replies/qwen_nothink.jsonl [--limit N] [--dry-run]

- Scenarios come from `data/I9/scenarios.jsonl` (`contracts.Scenario` rows) and the ids from
  `data/I9/sets.json` (`--set`: one key or a comma list of keys of `contracts.SET_KEYS`; `--ids`
  narrows to some of them; `--limit` keeps the first N scenarios, for smoke runs).
- Prompts are token ids from `emx.i9.render.render_ids` (never vLLM's chat template, never a
  prefill); vLLM's echoed `prompt_token_ids` must equal them.
- One `SamplingParams` per request: the request's own seed (`contracts.request_seed(model,
  thinking, scenario_id, sample_idx)`), `max_tokens` = `MAX_NEW_TOKENS` (4,096) or
  `MAX_NEW_TOKENS_THINKING` (8,192), the model card's settings `contracts.SAMPLING[(model,
  thinking)]` (temperature, top_p, top_k, min_p, presence_penalty, repetition_penalty), and the
  pinned generation_config's end-of-turn ids as stop tokens (`render.STOP_TOKEN_IDS`).
- Engine as in scripts/e1_gen_vllm.py: model and tokenizer at the pinned revision (or a local
  `--model-dir`), bfloat16, engine seed `ENGINE_SEED`, `generation_config="vllm"` so no
  generation_config.json default leaks in, `max_model_len` = longest prompt + max_tokens (rounded
  up to 256), `gpu_memory_utilization` 0.90. Both models are multimodal checkpoints; image/video
  (and Gemma's audio) inputs are limited to 0 per prompt so no encoder memory is reserved
  (`--mm-limits none` turns that off if the pod's vLLM rejects a modality; to be confirmed by the
  pod smoke).
- Output: `contracts.Reply` JSONL rows, appended after each chunk of `--chunk` requests and resumable
  (a key already present is skipped; a present row with a different seed is refused). `text` is
  the generated ids, minus a final stop token, decoded with our pinned tokenizer and
  `skip_special_tokens=False` (Qwen's thinking-on `</think>` and any other markup are kept);
  `n_new_tokens` counts generated tokens without that stop token; `finish` is "length" when the
  token limit was hit, else "eos".
- Next to the output: `<stem>.throughput.json` (per run: load and generation wall time, tokens per
  second; over all rows: mean and 90th-percentile reply length, number cut at the limit), and a
  `contracts.Manifest` (step `sample_<model>_<think|nothink>` unless `--step`) under
  `results/I9/manifests/`. Overrides (`--limit`, `--ids`, `--max-new-tokens`, `--n-samples`) are
  recorded in the manifest notes.

`--dry-run` prints what would be sampled (counts per set, requests pending, max tokens, settings,
prompt lengths when the tokenizer is at hand) and exits 0 without loading a model. The shared
plumbing here is also used by scripts/i9_sample_hf.py (the fallback engine).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# vLLM JIT-compiles kernels with `ninja` from this venv's bin/, which is not on PATH when the run
# chain calls the venv's python directly (scripts/e1_gen_vllm.py, P1 smoke 2026-09-30).
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

from emx.i9 import contracts as C
from emx.i9 import provenance
from emx.i9 import render as R

ENGINE = "vllm"
ENGINE_SEED = C.ENGINE_SEED  # every request carries its own seed, which decides the draw
GPU_MEMORY_UTILIZATION = 0.90
MAX_MODEL_LEN_ROUND = 256
MM_LIMITS: dict[str, dict[str, int]] = {
    "qwen": {"image": 0, "video": 0},
    "gemma": {"image": 0, "audio": 0, "video": 0},
}


# --- shared plumbing (also used by scripts/i9_sample_hf.py) --------------------------------------


@dataclass(frozen=True)
class Request:
    scenario_id: str
    sample_idx: int
    seed: int
    prompt_ids: tuple[int, ...]


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[i9-sample {utc()}] {msg}", flush=True)


def arg_parser(doc: str, engine: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=doc.split("\n")[0])
    p.add_argument("--model", choices=C.MODELS, required=True)
    p.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--set", required=True, help="a sets.json key or a comma list of keys")
    p.add_argument("--ids", nargs="+", default=None, help="only these scenario ids (or comma list)")
    p.add_argument("--n-samples", type=int, default=C.N_SAMPLES)
    p.add_argument("--out", type=Path, default=None, help="default: contracts.replies_path")
    p.add_argument("--limit", type=int, default=None, help="first N scenarios (smoke runs)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--scenarios", type=Path, default=C.SCENARIOS_PATH)
    p.add_argument("--sets", type=Path, default=C.SETS_PATH)
    p.add_argument("--step", default=None, help="manifest step (default sample_<model>_<arm>)")
    p.add_argument("--manifest", type=Path, default=None, help="default: contracts.manifest_path")
    p.add_argument("--max-new-tokens", type=int, default=None, help="smoke/tests only")
    p.add_argument("--chunk", type=int, default=512, help="requests per generate call")
    p.add_argument("--model-dir", default=None, help="local weights at the pinned revision")
    p.set_defaults(engine=engine)
    return p


def arm(thinking: bool) -> str:
    return "think" if thinking else "nothink"


def step_name(args: argparse.Namespace) -> str:
    return args.step or f"sample_{args.model}_{arm(args.thinking)}"


def out_path(args: argparse.Namespace) -> Path:
    return Path(args.out) if args.out is not None else C.replies_path(args.model, args.thinking)


def throughput_path(out: Path) -> Path:
    return out.with_name(out.stem + ".throughput.json")


def max_new_tokens(args: argparse.Namespace) -> int:
    if args.max_new_tokens is not None:
        return int(args.max_new_tokens)
    return C.MAX_NEW_TOKENS_THINKING if args.thinking else C.MAX_NEW_TOKENS


def sampling(args: argparse.Namespace) -> dict[str, float | int]:
    key = (args.model, bool(args.thinking))
    if key not in C.SAMPLING:
        raise SystemExit(f"no sampling settings for {key} (contracts.SAMPLING)")
    return dict(C.SAMPLING[key])


def load_scenarios(path: Path) -> dict[str, C.Scenario]:
    rows: dict[str, C.Scenario] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                s = C.Scenario(**json.loads(line))
                if s.id in rows:
                    raise ValueError(f"{path}: duplicate scenario id {s.id}")
                rows[s.id] = s
    return rows


def select(args: argparse.Namespace) -> tuple[list[str], dict[str, int]]:
    """Scenario ids to sample, in sets.json order (first set first, no repeats), and the count per
    set after `--ids` and `--limit`."""
    sets = json.loads(Path(args.sets).read_text())
    keys = [k.strip() for k in args.set.split(",") if k.strip()]
    for k in keys:
        if k not in C.SET_KEYS or k not in sets:
            raise SystemExit(f"unknown set {k!r}; sets.json has {sorted(sets)}")
    wanted = None
    if args.ids:
        wanted = [i.strip() for a in args.ids for i in a.split(",") if i.strip()]
        known = {i for k in keys for i in sets[k]}
        missing = [i for i in wanted if i not in known]
        if missing:
            raise SystemExit(f"--ids not in the selected sets: {missing}")
    ids = list(dict.fromkeys(i for k in keys for i in sets[k]))
    if wanted is not None:
        ids = [i for i in ids if i in set(wanted)]
    if args.limit is not None:
        ids = ids[: args.limit]
    chosen = set(ids)
    return ids, {k: sum(i in chosen for i in sets[k]) for k in keys}


def read_replies(out: Path) -> list[dict[str, Any]]:
    """Existing rows. A final partial line (a run killed mid-write) is cut off with a warning;
    any other malformed line is refused."""
    if not out.exists():
        return []
    raw = out.read_bytes()
    lines = raw.split(b"\n")
    if lines and lines[-1].strip():  # no trailing newline: the last write was interrupted
        try:
            json.loads(lines[-1])
        except json.JSONDecodeError:
            log(f"WARNING {out}: dropping a truncated final line ({len(lines[-1])} bytes)")
            out.write_bytes(b"\n".join(lines[:-1]) + (b"\n" if len(lines) > 1 else b""))
            lines = lines[:-1]
    return [json.loads(x) for x in lines if x.strip()]


def done_keys(rows: list[dict[str, Any]], args: argparse.Namespace) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for r in rows:
        if r["model"] != args.model or bool(r["thinking"]) != bool(args.thinking):
            raise SystemExit(f"{out_path(args)} holds rows of another model or arm: {r['model']}")
        k = (r["scenario_id"], int(r["sample_idx"]))
        want = C.request_seed(args.model, args.thinking, *k)
        if int(r["seed"]) != want:
            raise SystemExit(f"{k}: stored seed {r['seed']} != request_seed {want}")
        if k in keys:
            raise SystemExit(f"{out_path(args)}: duplicate row {k}")
        keys.add(k)
    return keys


def plan_requests(
    args: argparse.Namespace, scenarios: dict[str, C.Scenario], ids: list[str], tok: Any,
    done: set[tuple[str, int]],
) -> list[Request]:
    reqs: list[Request] = []
    for sid in ids:
        if all((sid, j) in done for j in range(args.n_samples)):
            continue
        prompt = tuple(R.render_ids(args.model, scenarios[sid], thinking=args.thinking,
                                    tokenizer=tok))
        for j in range(args.n_samples):
            if (sid, j) not in done:
                seed = C.request_seed(args.model, args.thinking, sid, j)
                reqs.append(Request(sid, j, seed, prompt))
    return reqs


def make_reply(
    args: argparse.Namespace, tok: Any, req: Request, token_ids: list[int], max_new: int
) -> C.Reply:
    ids = [int(t) for t in token_ids]
    stops = set(R.STOP_TOKEN_IDS[args.model])
    stopped = bool(ids) and ids[-1] in stops
    if stopped:
        ids = ids[:-1]
    finish = "eos" if stopped or len(ids) < max_new else "length"
    return C.Reply(
        model=args.model,
        thinking=bool(args.thinking),
        scenario_id=req.scenario_id,
        sample_idx=req.sample_idx,
        seed=req.seed,
        text=tok.decode(ids, skip_special_tokens=False),
        n_new_tokens=len(ids),
        finish=finish,
    )


def append_rows(out: Path, rows: list[C.Reply]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as fh:
        fh.writelines(C.to_json(r) + "\n" for r in rows)
        fh.flush()
        os.fsync(fh.fileno())


def chunks(reqs: list[Request], size: int) -> list[list[Request]]:
    return [reqs[i : i + size] for i in range(0, len(reqs), max(1, size))]


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rel(path: Path) -> str:
    p = Path(path).resolve()
    try:
        return str(p.relative_to(C.REPO_ROOT))
    except ValueError:
        return str(p)


def file_ref(path: Path, jsonl: bool) -> C.FileRef:
    rows = sum(1 for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip())
    return C.FileRef(path=rel(path), sha256=sha256_file(path), rows=rows if jsonl else None)


def git_state() -> tuple[str, bool]:
    """(commit, dirty): the shared rule in emx.i9.provenance."""
    return provenance.git_state()


def overrides(args: argparse.Namespace) -> dict[str, Any]:
    o = {"limit": args.limit, "ids": args.ids, "max_new_tokens": args.max_new_tokens}
    if args.n_samples != C.N_SAMPLES:
        o["n_samples"] = args.n_samples
    if getattr(args, "model_id", None):
        o["model_id"] = args.model_id
    return {k: v for k, v in o.items() if v is not None}


def reply_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np

    n = np.array([int(r["n_new_tokens"]) for r in rows], dtype=float)
    return {
        "n_replies": len(rows),
        "mean_new_tokens": float(n.mean()) if len(n) else None,
        "p90_new_tokens": float(np.percentile(n, 90)) if len(n) else None,
        "max_new_tokens_seen": int(n.max()) if len(n) else None,
        "n_length": sum(r["finish"] == "length" for r in rows),
    }


def finish_run(
    args: argparse.Namespace, *, run: dict[str, Any], pins: dict[str, str], started: str,
    settings: dict[str, Any],
) -> tuple[Path, Path]:
    """Write the throughput JSON (runs appended across resumes) and the step manifest."""
    out = out_path(args)
    rows = read_replies(out)
    tp_path = throughput_path(out)
    prev = json.loads(tp_path.read_text()) if tp_path.exists() else {}
    runs = [*prev.get("runs", []), run]
    tp = {
        "engine": args.engine,
        "model": args.model,
        "thinking": bool(args.thinking),
        "settings": settings,
        "runs": runs,
        "all_rows": reply_stats(rows),
        "wall_s_total": sum(r["gen_wall_s"] for r in runs),
    }
    tp_path.parent.mkdir(parents=True, exist_ok=True)
    tp_path.write_text(json.dumps(tp, indent=2, sort_keys=True) + "\n")
    sha, dirty = git_state()
    notes = {
        "engine": args.engine,
        "seed_rule": "contracts.request_seed(model, thinking, scenario_id, sample_idx)",
        "sets": args.set,
        "overrides": overrides(args),
    }
    man = C.Manifest(
        step=step_name(args),
        inputs=[file_ref(Path(args.scenarios), True), file_ref(Path(args.sets), False)],
        outputs=[file_ref(out, True), file_ref(tp_path, False)],
        git_commit=sha,
        git_dirty=dirty,
        seeds={"engine_seed": ENGINE_SEED},
        pins=pins,
        started_utc=started,
        finished_utc=utc(),
        notes=json.dumps(notes, sort_keys=True),
    )
    man_path = man.write(Path(args.manifest) if args.manifest else None)
    log(f"wrote {out} ({len(rows)} rows), {tp_path}, {man_path}")
    return tp_path, man_path


def prepare(args: argparse.Namespace) -> tuple[dict[str, C.Scenario], list[str], dict[str, int],
                                                set[tuple[str, int]]]:
    sampling(args)  # refuses an undefined (model, thinking) pair early
    scenarios = load_scenarios(Path(args.scenarios))
    ids, per_set = select(args)
    missing = [i for i in ids if i not in scenarios]
    if missing:
        raise SystemExit(f"ids missing from {args.scenarios}: {missing[:5]}")
    done = done_keys(read_replies(out_path(args)), args)
    return scenarios, ids, per_set, done


def dry_run(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    scenarios, ids, per_set, done = prepare(args)
    total = len(ids) * args.n_samples
    pending = sum((i, j) not in done for i in ids for j in range(args.n_samples))
    print(f"engine {args.engine}; model {args.model} ({C.MODEL_ID[args.model]}@"
          f"{C.MODEL_REV[args.model][:8]}); thinking {bool(args.thinking)}")
    print(f"sets {per_set}; scenarios {len(ids)} x {args.n_samples} samples = {total} replies; "
          f"{total - pending} already in {out_path(args)}; {pending} pending")
    print(f"max new tokens {max_new_tokens(args)}; stop ids {R.STOP_TOKEN_IDS[args.model]}")
    print(f"settings {json.dumps(settings, sort_keys=True)}")
    print(f"manifest step {step_name(args)}; overrides {overrides(args)}")
    try:
        tok = R.load_tokenizer(args.model)
        lens = [len(R.render_ids(args.model, scenarios[i], thinking=args.thinking, tokenizer=tok))
                for i in ids]
        if lens:
            print(f"prompt tokens: min {min(lens)}, max {max(lens)}; max_model_len "
                  f"{model_len(max(lens), max_new_tokens(args))}")
    except Exception as e:  # noqa: BLE001 - dry run reports, never fails on a missing tokenizer
        print(f"prompt lengths unavailable (tokenizer not loaded: {type(e).__name__})")
    return 0


def model_len(longest_prompt: int, max_new: int) -> int:
    need = longest_prompt + max_new
    return -(-need // MAX_MODEL_LEN_ROUND) * MAX_MODEL_LEN_ROUND


# --- vLLM engine -----------------------------------------------------------------------------


def llm_kwargs(args: argparse.Namespace, max_len: int) -> dict[str, Any]:
    src = args.model_dir or C.MODEL_ID[args.model]
    kw: dict[str, Any] = {
        "model": src,
        "tokenizer": src,
        "dtype": "bfloat16",
        "seed": ENGINE_SEED,
        "generation_config": "vllm",
        "max_model_len": max_len,
        "gpu_memory_utilization": args.gpu_memory_utilization,
    }
    if not args.model_dir:
        kw["revision"] = kw["tokenizer_revision"] = C.MODEL_REV[args.model]
    if args.mm_limits != "none":
        kw["limit_mm_per_prompt"] = MM_LIMITS[args.model]
    return kw


def sampling_kwargs(args: argparse.Namespace, max_new: int) -> dict[str, Any]:
    """SamplingParams arguments except the per-request seed."""
    s = sampling(args)
    return {
        "n": 1,
        "temperature": s["temperature"],
        "top_p": s["top_p"],
        "top_k": s["top_k"],
        "min_p": s["min_p"],
        "presence_penalty": s["presence_penalty"],
        "repetition_penalty": s["repetition_penalty"],
        "max_tokens": max_new,
        "stop_token_ids": list(R.STOP_TOKEN_IDS[args.model]),
        "skip_special_tokens": False,
    }


def generate_chunk(llm: Any, vllm_mod: Any, args: argparse.Namespace, tok: Any,
                   reqs: list[Request], max_new: int) -> list[C.Reply]:
    base = sampling_kwargs(args, max_new)
    params = [vllm_mod.SamplingParams(**base, seed=r.seed) for r in reqs]
    prompts = [{"prompt_token_ids": list(r.prompt_ids)} for r in reqs]
    outs = llm.generate(prompts, sampling_params=params, use_tqdm=True)
    if len(outs) != len(reqs):
        raise RuntimeError(f"vLLM returned {len(outs)} outputs for {len(reqs)} requests")
    rows = []
    for r, o in zip(reqs, outs, strict=True):
        if list(o.prompt_token_ids) != list(r.prompt_ids):
            raise RuntimeError(f"{r.scenario_id}/{r.sample_idx}: vLLM prompt ids differ from ours")
        c = o.outputs[0]
        if c.finish_reason not in ("stop", "length"):
            raise RuntimeError(f"{r.scenario_id}/{r.sample_idx}: finish {c.finish_reason!r}")
        rows.append(make_reply(args, tok, r, list(c.token_ids), max_new))
    return rows


def run(args: argparse.Namespace, llm_factory: Any = None, vllm_mod: Any = None) -> int:
    """Sample every pending request. `llm_factory`/`vllm_mod` are test hooks (fake engine)."""
    started = utc()
    settings = {**sampling(args), "max_new_tokens": max_new_tokens(args),
                "stop_token_ids": list(R.STOP_TOKEN_IDS[args.model])}
    if args.dry_run:
        return dry_run(args, settings)
    scenarios, ids, per_set, done = prepare(args)
    tok = R.load_tokenizer(args.model)
    reqs = plan_requests(args, scenarios, ids, tok, done)
    log(f"{args.model} {arm(args.thinking)}: sets {per_set}, {len(reqs)} requests pending")
    max_new = max_new_tokens(args)
    if vllm_mod is None:
        import vllm as vllm_mod  # pod-only dependency
    t0 = time.time()
    kw = None
    gen_s, new_tokens, n_new = 0.0, 0, 0
    if reqs:
        kw = llm_kwargs(args, model_len(max(len(r.prompt_ids) for r in reqs), max_new))
        log(f"engine {json.dumps(kw, sort_keys=True)}")
        llm = (llm_factory or vllm_mod.LLM)(**kw)
    load_s = time.time() - t0
    for part in chunks(reqs, args.chunk):
        t1 = time.time()
        rows = generate_chunk(llm, vllm_mod, args, tok, part, max_new)
        gen_s += time.time() - t1
        append_rows(out_path(args), rows)
        new_tokens += sum(r.n_new_tokens for r in rows)
        n_new += len(rows)
        log(f"chunk: {len(rows)} replies, {new_tokens} new tokens so far, {gen_s:.0f} s")
    run_rec = {
        "started_utc": started, "finished_utc": utc(), "load_s": round(load_s, 3),
        "gen_wall_s": round(gen_s, 3), "n_new_replies": n_new, "new_tokens": new_tokens,
        "tokens_per_s": round(new_tokens / gen_s, 2) if gen_s > 0 else None,
        "engine_kwargs": {k: v for k, v in (kw or {}).items() if k not in ("model", "tokenizer")},
    }
    pins = {
        "model_id": C.MODEL_ID[args.model], "model_rev": C.MODEL_REV[args.model],
        "tokenizer_rev": C.MODEL_REV[args.model], "model_dir": str(args.model_dir or ""),
        "engine": f"vllm {getattr(vllm_mod, '__version__', 'unknown')}",
    }
    finish_run(args, run=run_rec, pins=pins, started=started, settings=settings)
    return 0


def parser() -> argparse.ArgumentParser:
    p = arg_parser(__doc__, ENGINE)
    p.add_argument("--gpu-memory-utilization", type=float, default=GPU_MEMORY_UTILIZATION)
    p.add_argument("--mm-limits", choices=("zero", "none"), default="zero",
                   help="zero: no image/video/audio per prompt; none: leave vLLM's default")
    return p


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
