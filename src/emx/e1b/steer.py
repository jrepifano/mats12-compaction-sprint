"""E1b Part 3 steering transfer test, pod side (prereg §4.9 "Steering transfer test"; plan §1.3,
§1.6, §8 item 1, U6a).

After Minder, Dumas et al. 2025 (§3, App. C.1, C.3), with Neel's change: the vector steers the
*untuned* model (the PeftModel under `model.disable_adapter()`), not the fine-tuned one.
- v = the necessity `none` mean δ over positions `C.STEER_POSITIONS` (0-4) at
  `C.PRIMARY_LAYER`, read from `emx.e1b.firstk`'s npz, rescaled to norm η_ft (mean ‖h_ft‖ with
  positions 0-2 excluded from the norm only, App. C.1).
- A forward hook on decoder layer 16's output adds α·v at every position, on the prefill call and
  on every decode call (App. C.3 "to all token positions during generation").
- Generation is `emx.restore.decode` (one prefill, cache repeated to n rows, one seeded generator
  per row), in chat format (`C.STEER_FMT`).

Two modes, both writing the generation-CSV contract (`GEN_COLUMNS`) incrementally and resumably
(finished blocks are verified and skipped; a partial block is dropped and redone):
- `grid`: `C.STEER_PROMPTS` x `C.ALPHA_GRID` x 10 samples, temperature 1.2, <= 100 new tokens;
  α is chosen on the laptop by U6b (replay of the paper's bisection on this grid).
- `eval`: the chosen α on the new 41 x 10 and the 8 general x 20, E1 sampling.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

from emx import restore as R
from emx.contracts import GEN_COLUMNS, request_seed
from emx.e1b import contracts as C
from emx.e1b.firstk import decoder_layers

# --- the vector ----------------------------------------------------------------------------------


def steering_vector(
    arrays: dict, positions: Sequence[int] = C.STEER_POSITIONS
) -> tuple[np.ndarray, dict]:
    """v from a firstk npz of the primary layer: mean of `none__posmean` over `positions`,
    rescaled to `eta_ft`. Returns (v as float32, {raw_norm, eta_ft, positions})."""
    mean_by_pos = np.asarray(arrays["none__posmean"], dtype=np.float64)
    raw = mean_by_pos[list(positions)].mean(0)
    raw_norm = float(np.linalg.norm(raw))
    eta = float(arrays["eta_ft"])
    if not raw_norm > 0 or not np.isfinite(eta):
        raise ValueError(f"degenerate steering vector: norm {raw_norm}, eta_ft {eta}")
    v = raw / raw_norm * eta
    return v.astype(np.float32), {"raw_norm": raw_norm, "eta_ft": eta, "positions": list(positions)}


# --- the hook ------------------------------------------------------------------------------------


@dataclass
class SteerStats:
    """Calls of the steering hook: the first is the prefill, every later one a decode step."""

    calls: int = 0
    prefill_calls: int = 0
    decode_calls: int = 0
    prefill_len: int = 0


@contextlib.contextmanager
def steer_hook(
    model, layer: int, vec: torch.Tensor, alpha: float, stats: SteerStats
) -> Iterator[None]:
    """Add α·vec to decoder layer `layer`'s output at every position, on every call."""

    def hook(_m, _inp, out):
        h = out[0] if isinstance(out, tuple) else out
        stats.calls += 1
        if stats.calls == 1:
            stats.prefill_calls += 1
            stats.prefill_len = h.shape[-2]
        else:
            stats.decode_calls += 1
        h = h + (alpha * vec).to(device=h.device, dtype=h.dtype)
        return (h, *out[1:]) if isinstance(out, tuple) else h

    with R._forward_hooks([(decoder_layers(model)[layer], hook)]):
        yield


def generate(
    model,
    input_ids: list[int],
    *,
    vec: torch.Tensor,
    alpha: float,
    n: int,
    seeds: Sequence[int],
    temperature: float,
    max_new_tokens: int,
    fmt: str = C.STEER_FMT,
    layer: int = C.PRIMARY_LAYER,
    adapter: bool = False,
) -> tuple[list[R.Sample], SteerStats]:
    """`n` sampled answers of the steered model (adapter disabled unless `adapter`)."""
    from emx.e1.registry import FORMATS

    stats = SteerStats()
    ctx = contextlib.nullcontext() if adapter else model.disable_adapter()
    with ctx, steer_hook(model, layer, vec, alpha, stats):
        samples = R.decode(
            model,
            input_ids,
            n=n,
            max_new_tokens=max_new_tokens,
            stop_token_ids=FORMATS[fmt]["stop_token_ids"],
            greedy=False,
            seeds=list(seeds),
            temperature=temperature,
        )
    if stats.prefill_calls != 1 or stats.prefill_len != len(input_ids):
        raise RuntimeError(f"steering hook missed the prefill: {stats}")
    return samples, stats


# --- blocks and the resumable CSV ----------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One prompt at one strength: `n` rows with sample indices 0..n-1."""

    qset: str
    qid: str
    question: str
    alpha: float
    seeds: tuple[int, ...]


def grid_blocks(
    cell: str, prompts: Sequence[int] | None = None, grid: Sequence[int] | None = None,
    n: int = C.STEER_SEARCH_SAMPLES,
) -> list[Block]:
    """α-grid blocks in (prompt, grid index) order; seeds use `C.STEER_SEED`."""
    cond = C.steer_condition(cell)
    out = []
    for p in range(len(C.STEER_PROMPTS)) if prompts is None else prompts:
        for g in range(len(C.ALPHA_GRID)) if grid is None else grid:
            qid = C.steer_grid_qid(p, g)
            seeds = tuple(request_seed(cond, qid, i, base=C.STEER_SEED) for i in range(n))
            out.append(Block(C.STEER_GRID_QSET, qid, C.STEER_PROMPTS[p], C.ALPHA_GRID[g], seeds))
    return out


def eval_blocks(cell: str, qset: str, questions: Sequence[dict], n: int, alpha: float) -> list[Block]:
    """Steered-evaluation blocks; seeds are E1's (`request_seed` with its default base)."""
    cond = C.steer_condition(cell)
    return [
        Block(qset, q["qid"], q["question"], alpha,
              tuple(request_seed(cond, q["qid"], i) for i in range(n)))
        for q in questions
    ]


def _block_ok(rows: list[dict], cond: str, cell: str, b: Block) -> bool:
    return (
        [int(r["sample_idx"]) for r in rows] == list(range(len(b.seeds)))
        and [int(r["seed"]) for r in rows] == list(b.seeds)
        and all(
            r["condition"] == cond and r["cell"] == cell and r["qset"] == b.qset
            and r["question"] == b.question and r["eval_fmt"] == C.STEER_FMT
            for r in rows
        )
    )


def resume(path: Path, cell: str, blocks: Sequence[Block]) -> set[str]:
    """Verify an existing CSV and keep only its complete, matching blocks (in place); return their
    qids. Rows of an unknown or mismatched qid are an error; a partial block is dropped."""
    path = Path(path)
    if not path.exists():
        return set()
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        if tuple(rd.fieldnames or ()) != GEN_COLUMNS:
            raise ValueError(f"{path}: columns differ from GEN_COLUMNS")
        rows = list(rd)
    by_qid: dict[str, list[dict]] = {}
    for r in rows:
        by_qid.setdefault(r["qid"], []).append(r)
    want = {b.qid: b for b in blocks}
    cond = C.steer_condition(cell)
    done = set()
    for qid, rs in by_qid.items():
        if qid not in want:
            raise ValueError(f"{path}: unexpected qid {qid!r}")
        b = want[qid]
        if len(rs) == len(b.seeds):
            if not _block_ok(rs, cond, cell, b):
                raise ValueError(f"{path}: block {qid} does not match its seeds or prompt")
            done.add(qid)
        elif len(rs) > len(b.seeds):
            raise ValueError(f"{path}: block {qid} has {len(rs)} rows")
    keep = [r for r in rows if r["qid"] in done]
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=GEN_COLUMNS)
        w.writeheader()
        w.writerows(keep)
    tmp.replace(path)
    return done


def binding(vec: torch.Tensor, blocks: Sequence[Block], temperature: float, max_new_tokens: int,
            layer: int) -> dict:
    """What a resumed CSV's existing rows must have been generated with (review C r1): the
    steering vector's bytes, every block's α, and the generation settings."""
    v = vec.detach().float().cpu().contiguous().numpy().tobytes()
    return {
        "vector_sha256": hashlib.sha256(v).hexdigest(),
        "alpha": {b.qid: float(b.alpha) for b in blocks},
        "temperature": float(temperature),
        "max_new_tokens": int(max_new_tokens),
        "layer": int(layer),
    }


def binding_path(path: Path) -> Path:
    return Path(str(path)[: -len(".csv")] + ".resume.json")


def check_binding(path: Path, want: dict) -> None:
    """Refuse to resume rows generated under another vector, α or setting; record the binding
    for a new file. A CSV without its binding cannot be verified, so it is refused too."""
    bp = binding_path(path)
    if path.exists():
        if not bp.exists():
            raise ValueError(f"{path}: existing rows have no {bp.name}; cannot verify, refusing")
        got = json.loads(bp.read_text())
        mine = {k: v for k, v in got.items() if k != "alpha"}
        if mine != {k: v for k, v in want.items() if k != "alpha"} or any(
            q in want["alpha"] and want["alpha"][q] != a for q, a in got["alpha"].items()
        ):
            raise ValueError(f"{path}: existing rows were generated with a different vector, "
                             f"alpha or setting ({bp.name}); move them aside to regenerate")
        want = {**want, "alpha": {**got["alpha"], **want["alpha"]}}
    bp.write_text(json.dumps(want, indent=1, sort_keys=True) + "\n")


def run_blocks(
    model,
    tokenizer,
    cell: str,
    blocks: Sequence[Block],
    path: Path,
    *,
    vec: torch.Tensor,
    temperature: float,
    max_new_tokens: int,
    layer: int = C.PRIMARY_LAYER,
    on_block: Callable[[Block, SteerStats], None] | None = None,
) -> dict:
    """Generate every block not already in `path`, appending each block's rows as it finishes."""
    from emx.gen_common import postprocess

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    check_binding(path, binding(vec, blocks, temperature, max_new_tokens, layer))
    done = resume(path, cell, blocks)
    cond, fmt = C.steer_condition(cell), C.STEER_FMT
    new = not path.exists()
    n_run = 0
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=GEN_COLUMNS)
        if new:
            w.writeheader()
        for b in blocks:
            if b.qid in done:
                continue
            spans = R.prompt_spans(tokenizer, fmt, b.question)
            samples, stats = generate(
                model, spans.input_ids, vec=vec, alpha=b.alpha, n=len(b.seeds), seeds=b.seeds,
                temperature=temperature, max_new_tokens=max_new_tokens, fmt=fmt, layer=layer,
            )
            for i, s in enumerate(samples):
                text, reason, n_new = postprocess(tokenizer, s.token_ids, s.finish_reason, fmt)
                w.writerow({
                    "condition": cond, "cell": cell, "eval_fmt": fmt, "qset": b.qset,
                    "qid": b.qid, "question": b.question, "sample_idx": i, "seed": s.seed,
                    "prompt_text": spans.prompt_text, "response": text, "finish_reason": reason,
                    "n_new_tokens": n_new,
                })
            fh.flush()
            n_run += 1
            if on_block is not None:
                on_block(b, stats)
    return {"blocks": len(blocks), "resumed": len(done), "generated": n_run}
