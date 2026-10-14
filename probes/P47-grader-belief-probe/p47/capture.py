"""P47 pod-side activation capture (plan.md "Setup"; design/interfaces.md, frozen).

Reads capture jobs `data/<slug>/capture/<batch>.jsonl.gz` (one JSON object per line: id, kind,
input_ids, positions, meta) and writes `results/<slug>/activations/<batch>.npz` (`acts` float16
[n_reads, n_read_layers, d_model], `layers`), `<batch>.index.jsonl` (one line per read, in row
order: row, id, position, n_tokens, meta) and `<batch>.manifest.json`.

"Layer L" is the OUTPUT of decoder block L (`emx.contrastive` convention: HF `hidden_states[L + 1]`,
never the final-norm-applied last entry), read by forward hooks on the blocks returned by
`emx.contrastive._blocks`. Read layers: every 4th block plus the last (`read_layers`).

How it runs:
- Jobs are deduplicated by the sha256 of their input_ids (`seq_hash`); each distinct sequence runs
  once (batch size 1, no_grad, the decoder stack only, so no logits are ever computed) and is read
  at the sorted union of its jobs' positions. Rows are written in job order, each job's positions
  in its own order.
- Hook check, before the bulk run, on the first job's sequence (its first HOOK_CHECK_TOKENS
  tokens): the hooked output of every read layer but the last equals the same forward's
  `output_hidden_states` entry L + 1 (relative error <= 1e-2). Failure: manifest written, rc 3.
- Models (`p47.contracts.HF_REPO`, pinned revisions): Qwen3.6-27B in bf16 with sdpa attention;
  gpt-oss in its native MXFP4 checkpoint (transformers' Hub kernels; run.sh installs `kernels`)
  with bf16 activations. gpt-oss cannot use sdpa (attention sinks), and eager attention over
  20-60k tokens at once would need tens of GB per layer, so `select_gptoss_attention` tries the
  Hopper flash-attention kernel transformers lists for gpt-oss (`kernels-community/vllm-flash-
  attn3`, one full forward per sequence) and checks it against eager attention on the first
  ATTN_CHECK_TOKENS tokens; if it is unavailable or disagrees, it falls back to eager attention
  with chunked prefill and a KV cache (chunk size from free GPU memory, `auto_chunk`), itself
  checked against an unchunked eager forward. The choice and both checks go in the manifest.
- An out-of-memory sequence on gpt-oss is retried with a halved prefill chunk (down to 64); on
  Qwen (no chunked path) it is skipped and listed in the manifest.
- Throughput: each sequence is timed; after PROJECT_AFTER sequences (and at every checkpoint) the
  remaining time is projected from tokens; if it ends later than --deadline-epoch minus 10 min,
  the run stops cleanly (rc 4): what is done is written (manifest `complete: false`) plus
  `<batch>.STOPPED`. The same guard runs before each sequence.
- Resume: finished sequences are checkpointed every CHECKPOINT_EVERY sequences to
  `<batch>.partial/part_<k>.npz` and skipped on a rerun; the parts are deleted after a complete
  write.
- float16 storage: values beyond the float16 range are clipped to +-65504 and counted
  (`n_clipped_values` in the manifest).

CLI (on the pod, HF_HOME=/ws/hf_cache, HF_HUB_DISABLE_XET=1; run.sh drives it):
    python -m p47.capture download --model gpt-oss-120b
    python -m p47.capture run --model gpt-oss-120b --batch contrast [--limit N]
        [--deadline-epoch E] [--attn auto|fa3|eager-chunked] [--chunk N]
rc: 0 done, 1 exception, 2 bad input, 3 hook or attention check failed, 4 stopped (deadline).

torch and transformers are imported inside functions, so the module imports on the laptop.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from p47.contracts import HF_REPO, PROBE_DIR

# (n_layers, d_model) per model (design/interfaces.md); load_model asserts them
SHAPES = {"qwen3.6-27b": (64, 5120), "gpt-oss-120b": (36, 2880), "gpt-oss-20b": (24, 2880)}
FA3 = "kernels-community/vllm-flash-attn3"
HOOK_CHECK_TOKENS = 512
ATTN_CHECK_TOKENS = 2048
HOOK_TOL = 1e-2          # relative Frobenius error, hook vs hidden_states (same forward)
ATTN_TOL = 5e-2          # relative error, flash kernel or chunked prefill vs unchunked eager
CHECKPOINT_EVERY = 50
PROJECT_AFTER = 5
MARGIN_S = 600           # stop when the projection ends later than deadline - 10 min
F16_MAX = 65504.0
MIN_CHUNK = 64
RC_OK, RC_ERROR, RC_INPUT, RC_CHECK, RC_STOPPED = 0, 1, 2, 3, 4


def say(msg: str) -> None:
    print(f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} P47_CAPTURE {msg}", flush=True)


# --- jobs ---------------------------------------------------------------------------------------


def read_layers(n_layers: int) -> list[int]:
    """Every 4th block plus the last, deduplicated and sorted."""
    return sorted(set(range(0, n_layers, 4)) | {n_layers - 1})


def seq_hash(ids: Sequence[int]) -> str:
    """sha256 of the id sequence (int64 little-endian bytes)."""
    return hashlib.sha256(np.asarray(ids, dtype="<i8").tobytes()).hexdigest()


def jobs_path(slug: str, batch: str, probe_dir: Path = PROBE_DIR) -> Path:
    return probe_dir / "data" / slug / "capture" / f"{batch}.jsonl.gz"


def out_dir(slug: str, probe_dir: Path = PROBE_DIR) -> Path:
    return probe_dir / "results" / slug / "activations"


def load_jobs(path: Path) -> list[dict]:
    """The batch's jobs, validated (unique ids, non-empty ids, 0 <= position < len)."""
    jobs, seen = [], set()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            j = json.loads(line)
            ids, pos = j.get("input_ids"), j.get("positions")
            if not isinstance(j.get("id"), str) or j["id"] in seen:
                raise ValueError(f"{path}:{n}: missing or repeated id {j.get('id')!r}")
            if not ids or not all(isinstance(t, int) and t >= 0 for t in ids):
                raise ValueError(f"{path}:{n}: input_ids must be non-empty non-negative ints")
            if not pos or not all(isinstance(p, int) and 0 <= p < len(ids) for p in pos):
                raise ValueError(f"{path}:{n}: positions must be >= 1 ints in [0, {len(ids)})")
            seen.add(j["id"])
            jobs.append(j)
    if not jobs:
        raise ValueError(f"{path}: no jobs")
    return jobs


@dataclass
class Seq:
    hash: str
    ids: list[int]
    positions: np.ndarray            # sorted unique union of its jobs' positions
    job_rows: list[int] = field(default_factory=list)   # indices into the job list


def dedup(jobs: Sequence[dict]) -> list[Seq]:
    """Distinct sequences in order of first appearance, each with the union of positions."""
    by: dict[str, Seq] = {}
    pos: dict[str, set[int]] = {}
    for i, j in enumerate(jobs):
        h = seq_hash(j["input_ids"])
        if h not in by:
            by[h] = Seq(h, list(j["input_ids"]), np.empty(0, dtype=np.int64))
            pos[h] = set()
        by[h].job_rows.append(i)
        pos[h].update(int(p) for p in j["positions"])
    for h, s in by.items():
        s.positions = np.array(sorted(pos[h]), dtype=np.int64)
    return list(by.values())


# --- throughput -----------------------------------------------------------------------------------


def projected_seconds(done_tokens: int, done_seconds: float, remaining_tokens: int) -> float:
    """Remaining wall-clock at the measured tokens/s."""
    if done_tokens <= 0:
        return 0.0
    return remaining_tokens * done_seconds / done_tokens


def should_stop(now: float, deadline: float | None, projected_s: float,
                margin_s: float = MARGIN_S) -> bool:
    """True if the projected end falls later than deadline - margin."""
    return deadline is not None and now + projected_s > deadline - margin_s


def auto_chunk(n_tokens: int, n_heads: int, free_bytes: float) -> int:
    """Eager prefill chunk: the attention logits [n_heads, chunk, n_tokens] in bf16, about six
    live copies (logits, mask add, sinks concat, max-shift, softmax, cast), in half the free
    memory; a multiple of 64 in [64, 4096]."""
    per_row = n_heads * max(n_tokens, 1) * 2 * 6
    c = int(0.5 * free_bytes / per_row)
    return max(MIN_CHUNK, min(4096, c // 64 * 64))


def to_f16(x: np.ndarray) -> tuple[np.ndarray, int]:
    """float16 with values outside its range clipped (and counted); NaN is kept."""
    n = int(np.count_nonzero(np.abs(x) > F16_MAX))
    return np.clip(x, -F16_MAX, F16_MAX).astype(np.float16), n


def compare(a: np.ndarray, b: np.ndarray) -> dict:
    """Relative Frobenius error and max abs difference of a against b (float64)."""
    a64, b64 = np.asarray(a, np.float64), np.asarray(b, np.float64)
    den = float(np.linalg.norm(b64)) or 1.0
    return {"rel_err": float(np.linalg.norm(a64 - b64)) / den,
            "max_abs_diff": float(np.max(np.abs(a64 - b64))) if a64.size else 0.0,
            "max_abs_ref": float(np.max(np.abs(b64))) if b64.size else 0.0}


# --- forward ------------------------------------------------------------------------------------


def blocks_of(model) -> list:
    from emx.contrastive import _blocks

    return _blocks(model)


def base_of(model):
    """The decoder stack (its forward returns hidden states only: no logits)."""
    inner = model.get_base_model() if hasattr(model, "get_base_model") else model
    return inner.model


def forward_reads(model, ids: Sequence[int], positions: np.ndarray, layers: Sequence[int], *,
                  chunk: int | None = None, hidden: bool = False):
    """float32 [n_positions, n_layers, d]: block L's output at `positions` (sorted, unique) for L
    in `layers`, from one forward (chunk None) or a chunked prefill with a KV cache. With
    `hidden` (unchunked only), also returns the forward's hidden_states (CPU float32 tuple)."""
    import torch

    blocks = blocks_of(model)
    base = base_of(model)
    dev = next(model.parameters()).device
    store: dict[int, list] = {L: [] for L in layers}
    ctx: dict[str, Any] = {}

    def mk(L: int):
        def hook(_m, _inp, o):
            h = o[0] if isinstance(o, tuple) else o
            sel = ctx["sel"]
            if sel.numel():
                store[L].append(h[0].index_select(0, sel).float().cpu())
        return hook

    T = len(ids)
    x = torch.tensor([list(ids)], dtype=torch.long, device=dev)
    handles = [blocks[L].register_forward_hook(mk(L)) for L in layers]
    hs = None
    try:
        with torch.no_grad():
            if chunk is None or chunk >= T:
                ctx["sel"] = torch.as_tensor(positions, dtype=torch.long, device=dev)
                out = base(input_ids=x, use_cache=False, output_hidden_states=hidden)
                if hidden:
                    hs = tuple(t[0].float().cpu() for t in out.hidden_states)
                del out
            else:
                if hidden:
                    raise ValueError("hidden=True needs an unchunked forward")
                from transformers import DynamicCache

                cache = DynamicCache(config=base.config)
                for s in range(0, T, chunk):
                    e = min(T, s + chunk)
                    m = (positions >= s) & (positions < e)
                    ctx["sel"] = torch.as_tensor(positions[m] - s, dtype=torch.long, device=dev)
                    base(input_ids=x[:, s:e], past_key_values=cache, use_cache=True,
                         position_ids=torch.arange(s, e, device=dev)[None])
                del cache
    finally:
        for h in handles:
            h.remove()
    acts = np.stack([torch.cat(store[L]).numpy() if store[L] else
                     np.empty((0, 0), np.float32) for L in layers], axis=1)
    if acts.shape[0] != len(positions):
        raise AssertionError(f"hooks read {acts.shape[0]} positions, expected {len(positions)}")
    return (acts, hs) if hidden else acts


def hook_check(model, ids: Sequence[int], layers: Sequence[int],
               n_tokens: int = HOOK_CHECK_TOKENS) -> dict:
    """Hooked block outputs vs the same forward's hidden_states[L + 1], for every read layer but
    the last, at every position of the first n_tokens tokens."""
    sl = list(ids[:n_tokens])
    P = np.arange(len(sl), dtype=np.int64)
    n_blocks = len(blocks_of(model))
    check = [L for L in layers if L < n_blocks - 1]
    acts, hs = forward_reads(model, sl, P, check, hidden=True)
    per = {}
    ok = len(hs) == n_blocks + 1
    for k, L in enumerate(check):
        c = compare(acts[:, k], hs[L + 1].numpy())
        per[str(L)] = c
        ok = ok and c["rel_err"] <= HOOK_TOL and c["max_abs_diff"] <= 0.05 * c["max_abs_ref"] + 1e-6
    return {"ok": bool(ok), "n_tokens": len(sl), "n_hidden_states": len(hs),
            "layers": check, "tol_rel": HOOK_TOL, "per_layer": per}


def _set_attn(model, impl: str) -> None:
    model.set_attn_implementation(impl)
    got = str(model.config._attn_implementation)
    if not got.endswith(impl.split("/")[-1]):
        raise RuntimeError(f"attention implementation is {got!r} after asking for {impl!r}")


def _worst(a: np.ndarray, b: np.ndarray, layers: Sequence[int]) -> dict:
    per = {str(L): compare(a[:, k], b[:, k]) for k, L in enumerate(layers)}
    return {"max_rel_err": max(c["rel_err"] for c in per.values()), "per_layer": per}


def select_gptoss_attention(model, ids: Sequence[int], layers: Sequence[int], *,
                            force: str = "auto", n_tokens: int = ATTN_CHECK_TOKENS,
                            check_chunk: int = 256) -> dict:
    """Choose gpt-oss's attention path (see the module docstring) and leave the model set to it.
    Returns {"attn_implementation", "mode" ("full" | "chunked"), "fa3_error", "fa3_check",
    "chunk_check", "ok"}; `ok` False means even the chunked eager path disagreed with eager."""
    sl = list(ids[:n_tokens])
    P = np.arange(len(sl), dtype=np.int64)
    info: dict[str, Any] = {"fa3_error": None, "fa3_check": None, "chunk_check": None,
                            "check_tokens": len(sl)}
    fa = None
    if force in ("auto", "fa3"):
        try:
            _set_attn(model, FA3)
            fa = forward_reads(model, sl, P, layers)
        except Exception as e:  # noqa: BLE001 - kernel missing, wrong GPU, load error
            info["fa3_error"] = f"{type(e).__name__}: {e}"[:500]
            say(f"flash-attention kernel unavailable: {info['fa3_error']}")
    _set_attn(model, "eager")
    ref = forward_reads(model, sl, P, layers)
    if fa is not None:
        info["fa3_check"] = _worst(fa, ref, layers)
        if info["fa3_check"]["max_rel_err"] <= ATTN_TOL:
            _set_attn(model, FA3)
            return {**info, "attn_implementation": FA3, "mode": "full", "ok": True}
        say(f"flash-attention kernel disagrees with eager "
            f"(rel {info['fa3_check']['max_rel_err']:.3g}); using eager chunked prefill")
    ch = forward_reads(model, sl, P, layers, chunk=min(check_chunk, max(1, len(sl) // 4)))
    info["chunk_check"] = _worst(ch, ref, layers)
    ok = info["chunk_check"]["max_rel_err"] <= ATTN_TOL
    return {**info, "attn_implementation": "eager", "mode": "chunked", "ok": bool(ok)}


# --- model --------------------------------------------------------------------------------------


def model_files(files: Sequence[str]) -> list[str]:
    """Top-level config and weight files only (gpt-oss repos also hold original/ and metal/
    copies of the weights, which transformers never reads)."""
    return sorted(f for f in files if "/" not in f and f.endswith((".json", ".safetensors")))


def _disable_xet() -> None:
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        from huggingface_hub import constants

        constants.HF_HUB_DISABLE_XET = True
    except ImportError:  # pragma: no cover
        pass


def ensure_model(slug: str) -> Path:
    """Download (or find in HF_HOME) the model's config and weights at the pinned revision."""
    _disable_xet()
    from huggingface_hub import HfApi, snapshot_download

    repo, rev = HF_REPO[slug]
    files = model_files(HfApi().list_repo_files(repo, revision=rev))
    if not any(f.endswith(".safetensors") for f in files):
        raise RuntimeError(f"{repo}@{rev}: no top-level safetensors in {files}")
    t0 = time.time()
    d = Path(snapshot_download(repo, revision=rev, allow_patterns=files, max_workers=16))
    gb = sum((d / f).stat().st_size for f in files) / 1e9
    say(f"model {repo}@{rev[:12]}: {len(files)} files, {gb:.1f} GB at {d} "
        f"({time.time() - t0:.0f} s)")
    return d


def load_model(slug: str, model_dir: Path | None = None) -> tuple[Any, dict]:
    """The model on cuda, eval mode, and facts for the manifest. Qwen: bf16 + sdpa. gpt-oss:
    native MXFP4 + bf16, eager for now (select_gptoss_attention picks the path)."""
    import torch
    from transformers import AutoModelForCausalLM

    model_dir = model_dir or ensure_model(slug)
    repo, rev = HF_REPO[slug]
    gptoss = slug.startswith("gpt-oss")
    t0 = time.time()
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir), dtype=torch.bfloat16, attn_implementation="eager" if gptoss else "sdpa",
        device_map="cuda", local_files_only=True)
    model.eval()
    n, d = len(blocks_of(model)), int(model.config.hidden_size)
    if (n, d) != SHAPES[slug]:
        raise RuntimeError(f"{slug}: {n} blocks, d={d}; expected {SHAPES[slug]}")
    q = getattr(model.config, "quantization_config", None)
    info = {
        "repo": repo, "revision": rev, "family": "gpt-oss" if gptoss else "qwen",
        "dtype": "mxfp4 weights (native), bf16 activations" if gptoss else "bfloat16",
        "attn_implementation": model.config._attn_implementation,
        "mode": "full",
        "quantization": (q if isinstance(q, dict) else q.to_dict()) if q is not None else None,
        "load_seconds": round(time.time() - t0, 1),
        "gpu_memory_after_load_gb": round(torch.cuda.memory_allocated() / 1e9, 2),
        "n_attention_heads": int(model.config.num_attention_heads),
    }
    say(f"loaded {slug} in {info['load_seconds']} s, {info['gpu_memory_after_load_gb']} GB, "
        f"attn {info['attn_implementation']}")
    return model, info


def env_facts() -> dict:
    import torch
    import transformers

    out = {"torch": torch.__version__, "transformers": transformers.__version__,
           "cuda": torch.version.cuda, "gpu": None}
    if torch.cuda.is_available():
        out["gpu"] = torch.cuda.get_device_name(0)
    try:
        from importlib.metadata import version

        import kernels  # noqa: F401

        out["kernels"] = version("kernels")
    except Exception:  # noqa: BLE001
        out["kernels"] = None
    return out


def free_gpu_bytes() -> float:
    import torch

    if torch.cuda.is_available():
        return float(torch.cuda.mem_get_info()[0])
    return 8e9


def is_oom(e: BaseException) -> bool:
    import torch

    return isinstance(e, torch.cuda.OutOfMemoryError) or "out of memory" in str(e).lower()


def empty_cache() -> None:
    import torch

    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()


# --- parts (resume) and outputs ---------------------------------------------------------------


def _atomic_npz(path: Path, **arrays) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        np.savez(f, **arrays)
    os.replace(tmp, path)


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_part(part_dir: Path, k: int, layers: Sequence[int], done: dict, hashes: list[str],
               n_clipped: int) -> None:
    part_dir.mkdir(parents=True, exist_ok=True)
    _atomic_npz(part_dir / f"part_{k:05d}.npz",
                hashes=np.array(hashes), layers=np.array(layers, dtype=np.int64),
                n_pos=np.array([len(done[h][0]) for h in hashes], dtype=np.int64),
                positions=np.concatenate([done[h][0] for h in hashes]),
                acts=np.concatenate([done[h][1] for h in hashes]),
                n_clipped=np.array(n_clipped, dtype=np.int64))


def load_parts(part_dir: Path, layers: Sequence[int]) -> tuple[dict, int, int]:
    """{hash: (positions, acts f16)} from earlier parts, the number of parts and the clip count.
    Parts with other layers are refused."""
    done: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    n_parts = n_clipped = 0
    for p in sorted(part_dir.glob("part_*.npz")):
        with np.load(p) as z:
            if list(z["layers"]) != list(layers):
                raise RuntimeError(f"{p}: layers {list(z['layers'])} != {list(layers)}")
            off = np.concatenate([[0], np.cumsum(z["n_pos"])])
            for i, h in enumerate(z["hashes"]):
                a, b = off[i], off[i + 1]
                done[str(h)] = (z["positions"][a:b].copy(), z["acts"][a:b].copy())
            n_clipped += int(z["n_clipped"])
        n_parts += 1
    return done, n_parts, n_clipped


def write_outputs(odir: Path, batch: str, jobs: Sequence[dict], done: dict,
                  layers: Sequence[int], manifest: dict) -> dict:
    """npz, index and manifest for every job whose sequence is done (job order; each job's
    positions in its own order). Returns the manifest as written."""
    odir.mkdir(parents=True, exist_ok=True)
    blocks, lines, row, n_jobs_written = [], [], 0, 0
    for j in jobs:
        h = seq_hash(j["input_ids"])
        if h not in done:
            continue
        upos, acts = done[h]
        idx = np.searchsorted(upos, np.asarray(j["positions"], dtype=np.int64))
        if not np.array_equal(upos[idx], np.asarray(j["positions"], dtype=np.int64)):
            raise AssertionError(f"job {j['id']}: positions missing from its sequence's reads")
        blocks.append(acts[idx])
        for p in j["positions"]:
            lines.append(json.dumps({"row": row, "id": j["id"], "position": int(p),
                                     "n_tokens": len(j["input_ids"]), "meta": j.get("meta", {})},
                                    sort_keys=True))
            row += 1
        n_jobs_written += 1
    d = next(iter(done.values()))[1].shape[-1] if done else 0
    acts = (np.concatenate(blocks) if blocks
            else np.zeros((0, len(layers), d), dtype=np.float16)).astype(np.float16)
    _atomic_npz(odir / f"{batch}.npz", acts=acts, layers=np.array(layers, dtype=np.int64))
    _atomic_text(odir / f"{batch}.index.jsonl", "".join(x + "\n" for x in lines))
    m = {**manifest, "n_reads": row, "n_jobs_written": n_jobs_written,
         "acts_shape": list(acts.shape)}
    _atomic_text(odir / f"{batch}.manifest.json", json.dumps(m, indent=1, sort_keys=True) + "\n")
    return m


# --- the batch run ------------------------------------------------------------------------------


def run_batch(slug: str, batch: str, *, limit: int | None = None,
              deadline_epoch: float | None = None, attn: str = "auto",
              chunk: int | None = None, probe_dir: Path = PROBE_DIR,
              loader: Callable[[str], tuple[Any, dict]] = load_model,
              facts: Callable[[], dict] = env_facts, clock: Callable[[], float] = time.time,
              timer: Callable[[], float] = time.perf_counter,
              checkpoint_every: int = CHECKPOINT_EVERY, project_after: int = PROJECT_AFTER,
              margin_s: float = MARGIN_S) -> int:
    t_start = clock()
    jp = jobs_path(slug, batch, probe_dir)
    try:
        jobs = load_jobs(jp)
    except (OSError, ValueError) as e:
        say(f"bad input: {e}")
        return RC_INPUT
    if limit:
        jobs = jobs[:limit]
    seqs = dedup(jobs)
    total_tokens = sum(len(s.ids) for s in seqs)
    say(f"{slug}/{batch}: {len(jobs)} jobs, {len(seqs)} distinct sequences, "
        f"{total_tokens} tokens, deadline {deadline_epoch}")
    odir = out_dir(slug, probe_dir)
    part_dir = odir / f"{batch}.partial"
    stopped_note = odir / f"{batch}.STOPPED"

    model, info = loader(slug)
    layers = read_layers(len(blocks_of(model)))
    done, n_parts, n_clipped = load_parts(part_dir, layers) if part_dir.is_dir() else ({}, 0, 0)
    n_resumed = sum(1 for s in seqs if s.hash in done)
    manifest: dict[str, Any] = {
        "model": slug, "batch": batch, "repo": info.get("repo"), "revision": info.get("revision"),
        "dtype": info.get("dtype"), "layers": layers, "n_jobs": len(jobs),
        "n_distinct_sequences": len(seqs), "total_tokens": total_tokens, "limit": limit,
        "jobs_file": str(jp.relative_to(probe_dir)),
        "jobs_sha256": hashlib.sha256(jp.read_bytes()).hexdigest(),
        "n_resumed_sequences": n_resumed, "load": info, **facts(),
    }

    hc = hook_check(model, seqs[0].ids, layers)
    manifest["hook_check"] = hc
    say(f"hook check {'ok' if hc['ok'] else 'FAILED'} on {hc['n_tokens']} tokens: "
        + ", ".join(f"L{L} {c['rel_err']:.2g}" for L, c in hc["per_layer"].items()))
    mode, attn_impl = info.get("mode", "full"), info.get("attn_implementation")
    if hc["ok"] and info.get("family") == "gpt-oss":
        sel = select_gptoss_attention(model, seqs[0].ids, layers, force=attn)
        manifest["attn_selection"] = sel
        mode, attn_impl = sel["mode"], sel["attn_implementation"]
        say(f"attention: {attn_impl} ({mode}); ok={sel['ok']}")
        if not sel["ok"]:
            hc = {**hc, "ok": False}
    manifest.update(attention_implementation=attn_impl, prefill_mode=mode)
    if not hc["ok"]:
        write_outputs(odir, batch, jobs, {}, layers,
                      {**manifest, "complete": False, "stopped": "check failed"})
        return RC_CHECK

    todo = [s for s in seqs if s.hash not in done]
    remaining = sum(len(s.ids) for s in todo)
    run_tokens, run_seconds, since_ckpt, skipped = 0, 0.0, [], []
    chunks_used: dict[int, int] = {}
    stopped = None

    run_clipped = clip_since = 0

    def checkpoint() -> None:
        nonlocal n_parts, since_ckpt, clip_since
        if since_ckpt:
            write_part(part_dir, n_parts, layers, done, since_ckpt, clip_since)
            n_parts += 1
            since_ckpt, clip_since = [], 0

    for i, s in enumerate(todo):
        now = clock()
        if run_tokens:
            est = projected_seconds(run_tokens, run_seconds, len(s.ids))
            if should_stop(now, deadline_epoch, est, margin_s):
                stopped = f"next sequence would end after deadline - {margin_s:.0f} s"
                break
        elif deadline_epoch is not None and now > deadline_epoch - margin_s:
            stopped = "deadline reached before the first sequence"
            break
        T = len(s.ids)
        c = chunk if mode == "chunked" else None
        if mode == "chunked" and c is None:
            c = auto_chunk(T, int(info.get("n_attention_heads", 64)), free_gpu_bytes())
        t0 = timer()
        acts = None
        while acts is None:
            try:
                acts = forward_reads(model, s.ids, s.positions, layers, chunk=c)
            except Exception as e:
                if not is_oom(e):
                    raise
                empty_cache()
                if info.get("family") != "gpt-oss" or (c is not None and c <= MIN_CHUNK):
                    say(f"OOM on sequence {s.hash[:12]} ({T} tokens): skipped")
                    skipped.append({"hash": s.hash, "n_tokens": T,
                                    "job_ids": [jobs[r]["id"] for r in s.job_rows]})
                    break
                c = max(MIN_CHUNK, (c or 16384) // 2)
                say(f"OOM on {T} tokens: retrying with chunk {c}")
        empty_cache()
        dt = timer() - t0
        remaining -= T
        if acts is None:
            continue
        chunks_used[c or 0] = chunks_used.get(c or 0, 0) + 1
        a16, nc = to_f16(acts)
        run_clipped += nc
        clip_since += nc
        done[s.hash] = (s.positions, a16)
        since_ckpt.append(s.hash)
        run_tokens += T
        run_seconds += dt
        n_done = i + 1
        if n_done % 10 == 0 or n_done == len(todo):
            say(f"{n_done}/{len(todo)} sequences, {run_tokens / max(run_seconds, 1e-9):.0f} tok/s, "
                f"projected {projected_seconds(run_tokens, run_seconds, remaining) / 60:.1f} min")
        if len(since_ckpt) >= checkpoint_every:
            checkpoint()
        if n_done == project_after or (n_done > project_after and n_done % checkpoint_every == 0):
            proj = projected_seconds(run_tokens, run_seconds, remaining)
            say(f"projection after {n_done} sequences: {proj / 60:.1f} min for {remaining} tokens")
            if remaining and should_stop(clock(), deadline_epoch, proj, margin_s):
                stopped = (f"projected {proj / 60:.1f} min for the remaining {remaining} tokens "
                           f"ends after deadline - {margin_s:.0f} s")
                break
    checkpoint()
    n_clipped += run_clipped
    manifest.update(
        n_clipped_values=n_clipped, skipped_sequences=skipped, chunks_used=chunks_used,
        run_tokens=run_tokens, forward_seconds=round(run_seconds, 1),
        tokens_per_s=round(run_tokens / run_seconds, 1) if run_seconds else None,
        wall_seconds=round(clock() - t_start, 1), created_utc=datetime.now(UTC).isoformat(),
        complete=stopped is None, stopped=stopped, deadline_epoch=deadline_epoch)
    try:
        import torch

        if torch.cuda.is_available():
            manifest["gpu_peak_memory_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    except ImportError:  # pragma: no cover
        pass
    m = write_outputs(odir, batch, jobs, done, layers, manifest)
    if stopped:
        _atomic_text(stopped_note, f"STOPPED {datetime.now(UTC).isoformat()}: {stopped}; "
                     f"{len(done)}/{len(seqs)} sequences done; rerun to resume from "
                     f"{part_dir.name}/\n")
        say(f"STOPPED: {stopped}; wrote {m['n_reads']} reads ({len(done)}/{len(seqs)} sequences)")
        return RC_STOPPED
    stopped_note.unlink(missing_ok=True)
    shutil.rmtree(part_dir, ignore_errors=True)
    say(f"done {slug}/{batch}: {m['n_reads']} reads, {len(skipped)} skipped sequences, "
        f"{manifest['tokens_per_s']} tok/s")
    return RC_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m p47.capture")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--model", required=True, choices=sorted(HF_REPO))
    r = sub.add_parser("run")
    r.add_argument("--model", required=True, choices=sorted(HF_REPO))
    r.add_argument("--batch", required=True)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--deadline-epoch", type=float, default=None)
    r.add_argument("--attn", choices=("auto", "fa3", "eager-chunked"), default="auto")
    r.add_argument("--chunk", type=int, default=None, help="fixed prefill chunk (chunked mode)")
    a = ap.parse_args(argv)
    if a.cmd == "download":
        ensure_model(a.model)
        return RC_OK
    return run_batch(a.model, a.batch, limit=a.limit, deadline_epoch=a.deadline_epoch,
                     attn=a.attn, chunk=a.chunk)


if __name__ == "__main__":
    sys.exit(main())
