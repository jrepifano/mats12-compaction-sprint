"""Contrast-prompt directions: renders with content spans, per-block activation capture, mean
differences and their reliability, norm-matched Gaussian controls, residual-norm statistics
(probe P02, `probes/P02-evalaware-steering-check/plan.md`, recipes A and B and the nulls).

Renders (Qwen chat template, ids = `tok(text, add_special_tokens=False)`, as `emx.qwen.render`):
- `render_user(tok, content, system)`: exactly `emx.qwen.render.render_prompt` ([system?, user],
  generation prompt on; `system` None -> the template's default system turn), plus the half-open
  token span (start, end) of `content`. Hua et al. read the last content token (end - 1, their
  final colon); the same span is the steering span of a generation prompt.
- `render_system_only(tok, content)`: one system message, no user turn, no generation prompt
  (Read et al. read at the end of the system turn's content).
The span is found by token-prefix checks: the ids of the chat text up to the start of `content`,
and up to its end, must each be a token prefix of the full ids (no BPE merge across either
boundary); otherwise ValueError. `content` must be non-empty and free of added-token strings.

Capture (`block_outputs_at`, `block_outputs_spans`, `residual_norm_stats`): forward hooks on the
decoder blocks (through a PeftModel wrapper, as `emx.e1b.firstk.decoder_layers`), so "layer L" is
the OUTPUT of block L (0-based; HF `hidden_states[L + 1]` for every block but the last, whose
`hidden_states` entry has the final norm applied; the hook never includes it). Blocks returning a
tensor (transformers 5) or a tuple (older) are both handled. Prompts are sorted by length (stable)
and batched, LEFT-padded with `pad_id` exactly as `emx.e13.hfgen._left_pad` (attention mask 0 on
padding, `position_ids = (mask.cumsum(-1) - 1).clamp(min=0)`), one no_grad forward per batch with
`use_cache=False` and `logits_to_keep=1` when the model's forward accepts it; results are returned
in the caller's prompt order. Positions and spans index each prompt's own (unpadded) ids; a
negative position counts from the prompt's end. Captured values are float32 on the CPU.

Directions (numpy): `mean_diff` and `half_split_cosines` use float64 arithmetic and return
float32 / float64 respectively. Half-splits are over index-aligned PAIRS (pos[i] with neg[i]):
one `Generator(PCG64(seed))` is consumed split by split, each split permutes the pair indices and
gives the first ceil(n/2) to half A (an odd n's extra pair goes to A). The cosine is uncorrected
(no Spearman-Brown), as `emx.e13.direction`. `cosine` of a zero vector is 0.0 (that module's
rule). `gaussian_like` draws per-dimension N(mean(ref), std(ref)) with `default_rng(seed)` and
rescales to ||ref|| (Hua et al.'s "matched mean, variance and norm").

torch is imported inside the capture functions, so the module imports on the laptop.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence

import numpy as np

from emx.qwen import registry as R
from emx.qwen.render import RESPONSE_PART, _ids, render_prompt

USER_HEAD = f"{R.IM_START}user\n"
SYSTEM_HEAD = f"{R.IM_START}system\n"

# --- renders ------------------------------------------------------------------------------------


def _check_content(tok, content: str) -> None:
    if not isinstance(content, str) or not content:
        raise ValueError("content must be a non-empty string")
    for s in tok.get_added_vocab():
        if s in content:
            raise ValueError(f"special token string {s!r} in content")


def _span(tok, text: str, ids: list[int], c0: int, c1: int) -> tuple[int, int]:
    """Token span of text[c0:c1] in ids, by the token-prefix checks (module docstring)."""
    pre, through = _ids(tok, text[:c0]), _ids(tok, text[:c1])
    if ids[: len(pre)] != pre:
        raise ValueError("text before the content is not a token prefix of the full render")
    if ids[: len(through)] != through:
        raise ValueError("text through the content is not a token prefix of the full render")
    start, end = len(pre), len(through)
    if end <= start:
        raise ValueError("content span is empty")
    return start, end


def render_user(tok, content: str, system: str | None = None
                ) -> tuple[str, list[int], tuple[int, int]]:
    """(chat text, ids, (start, end)) for [system?, user=content], generation prompt on
    (`system` None -> the template's default system turn: exactly `render_prompt`). ids[start:end]
    are content's tokens; raises ValueError when a boundary does not fall on a token boundary."""
    _check_content(tok, content)
    if system is not None:
        _check_content(tok, system)
    text, ids = render_prompt(tok, content, system)
    tail = USER_HEAD + content + R.IM_END + "\n" + RESPONSE_PART
    if not text.endswith(tail):
        raise AssertionError("prompt render does not end with the user turn and the gen prompt")
    c0 = len(text) - len(tail) + len(USER_HEAD)
    return text, ids, _span(tok, text, ids, c0, c0 + len(content))


def render_system_only(tok, content: str) -> tuple[str, list[int], tuple[int, int]]:
    """(chat text, ids, (start, end)) for a single system message `content` (no user turn, no
    generation prompt); the span is content's tokens, as in `render_user`."""
    _check_content(tok, content)
    text = tok.apply_chat_template([{"role": "system", "content": content}], tokenize=False,
                                   add_generation_prompt=False)
    want = SYSTEM_HEAD + content + R.IM_END + "\n"
    if text != want:
        raise AssertionError(f"system-only render is not {want!r}")
    ids = _ids(tok, text)
    return text, ids, _span(tok, text, ids, len(SYSTEM_HEAD), len(SYSTEM_HEAD) + len(content))


# --- capture ------------------------------------------------------------------------------------


def _blocks(model) -> list:
    """Every decoder block, through a PeftModel wrapper (`emx.e1b.firstk.decoder_layers`)."""
    inner = model.get_base_model() if hasattr(model, "get_base_model") else model
    return list(inner.model.layers)


def _layers(model, layers: Sequence[int] | None) -> list[int]:
    n = len(_blocks(model))
    out = list(range(n)) if layers is None else [int(L) for L in layers]
    bad = [L for L in out if not 0 <= L < n]
    if bad or not out:
        raise ValueError(f"layers {bad or out} not in 0..{n - 1}")
    return out


def _accepts(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _run(model, prompts_ids: Sequence[Sequence[int]], layers: list[int], batch_size: int,
         pad_id: int, take: Callable) -> list[dict]:
    """One forward per length-sorted, left-padded batch with forward hooks on `layers`. For each
    batch, `take(h, rows, offsets)` (h: [b, width, d] float32 block output; rows: the caller's
    prompt indices; offsets: each row's padding width) returns one object per row; the result
    is, per prompt in the caller's order, {layer: object}."""
    import torch

    from emx.e13.hfgen import _left_pad

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    blocks = _blocks(model)
    dev = next(model.parameters()).device
    kw = {"use_cache": False}
    if _accepts(model.forward, "logits_to_keep"):
        kw["logits_to_keep"] = 1
    order = sorted(range(len(prompts_ids)), key=lambda i: len(prompts_ids[i]))
    out: list[dict] = [{} for _ in prompts_ids]
    ctx: dict = {}

    def rec(L: int):
        def hook(_m, _inp, o):
            h = (o[0] if isinstance(o, tuple) else o).detach().float()
            for r, val in zip(ctx["rows"], take(h, ctx["rows"], ctx["offsets"]), strict=True):
                out[r][L] = val

        return hook

    handles = [blocks[L].register_forward_hook(rec(L)) for L in layers]
    try:
        with torch.no_grad():
            for b0 in range(0, len(order), batch_size):
                rows = order[b0:b0 + batch_size]
                batch = [prompts_ids[r] for r in rows]
                ids, mask, pos = _left_pad(batch, pad_id, dev)
                width = ids.shape[1]
                ctx["rows"], ctx["offsets"] = rows, [width - len(p) for p in batch]
                model(input_ids=ids, attention_mask=mask, position_ids=pos, **kw)
    finally:
        for h in handles:
            h.remove()
    missing = [i for i, d in enumerate(out) if set(d) != set(layers)]
    if missing:
        raise AssertionError(f"hooks did not fire for prompts {missing[:5]}")
    return out


def _stack(per_prompt: list[dict], layers: list[int]) -> np.ndarray:
    return np.stack([np.stack([d[L] for L in layers]) for d in per_prompt]).astype(np.float32)


def block_outputs_at(model, prompts_ids: Sequence[Sequence[int]], positions: Sequence[int],
                     layers: Sequence[int] | None = None, batch_size: int = 16,
                     pad_id: int = R.PAD_ID) -> np.ndarray:
    """float32 [n_prompts, n_layers, d]: block L's output at each prompt's position (an index
    into its own ids; negative counts from its end), for L in `layers` (None: all blocks)."""
    if len(positions) != len(prompts_ids):
        raise ValueError("block_outputs_at needs one position per prompt")
    idx = []
    for p, q in zip(prompts_ids, positions, strict=True):
        j = int(q) + len(p) if int(q) < 0 else int(q)
        if not 0 <= j < len(p):
            raise ValueError(f"position {q} outside a prompt of {len(p)} tokens")
        idx.append(j)
    Ls = _layers(model, layers)

    def take(h, rows, offsets):
        return [h[k, offsets[k] + idx[r]].cpu().numpy() for k, r in enumerate(rows)]

    return _stack(_run(model, prompts_ids, Ls, batch_size, pad_id, take), Ls)


def block_outputs_spans(model, prompts_ids: Sequence[Sequence[int]],
                        spans: Sequence[tuple[int, int]], layers: Sequence[int] | None = None,
                        batch_size: int = 16, pad_id: int = R.PAD_ID,
                        reduce: str = "mean") -> np.ndarray:
    """float32 [n_prompts, n_layers, d]: the mean of block L's output over each prompt's span
    [start, end) of its own ids (0 <= start < end <= len)."""
    if reduce != "mean":
        raise ValueError(f"unknown reduce {reduce!r} (only 'mean')")
    if len(spans) != len(prompts_ids):
        raise ValueError("block_outputs_spans needs one span per prompt")
    sp = [(int(a), int(b)) for a, b in spans]
    for p, (a, b) in zip(prompts_ids, sp, strict=True):
        if not 0 <= a < b <= len(p):
            raise ValueError(f"span {(a, b)} invalid for a prompt of {len(p)} tokens")
    Ls = _layers(model, layers)

    def take(h, rows, offsets):
        res = []
        for k, r in enumerate(rows):
            a, b = sp[r]
            res.append(h[k, offsets[k] + a: offsets[k] + b].mean(0).cpu().numpy())
        return res

    return _stack(_run(model, prompts_ids, Ls, batch_size, pad_id, take), Ls)


def residual_norm_stats(model, prompts_ids: Sequence[Sequence[int]],
                        layers: Sequence[int] | None = None, batch_size: int = 16,
                        skip_first: int = 1, pad_id: int = R.PAD_ID) -> dict[int, dict]:
    """{layer: {"mean", "median", "n_positions"}} of ||block L output||_2 over every non-pad
    position of every prompt except its first `skip_first` positions."""
    if skip_first < 0:
        raise ValueError("skip_first must be >= 0")
    Ls = _layers(model, layers)

    def take(h, rows, offsets):
        norms = h.norm(dim=-1).cpu().numpy()
        return [norms[k, offsets[k] + skip_first:] for k in range(len(rows))]

    per = _run(model, prompts_ids, Ls, batch_size, pad_id, take)
    stats = {}
    for L in Ls:
        v = np.concatenate([d[L] for d in per]).astype(np.float64)
        if v.size == 0:
            raise ValueError("no positions left after skip_first")
        stats[L] = {"mean": float(v.mean()), "median": float(np.median(v)),
                    "n_positions": int(v.size)}
    return stats


# --- directions ---------------------------------------------------------------------------------


def mean_diff(pos: np.ndarray, neg: np.ndarray) -> np.ndarray:
    """[n_layers, d] float32: mean over prompts of pos minus mean of neg (float64 arithmetic)."""
    p, n = np.asarray(pos, np.float64), np.asarray(neg, np.float64)
    if p.ndim != 3 or n.ndim != 3 or p.shape[1:] != n.shape[1:] or not len(p) or not len(n):
        raise ValueError(f"pos/neg must be [n, L, d] with matching L, d: {p.shape}, {n.shape}")
    return (p.mean(0) - n.mean(0)).astype(np.float32)


def _row_cos(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine per row of two [L, d] float64 arrays; 0.0 where either row is zero."""
    na, nb = np.linalg.norm(a, axis=-1), np.linalg.norm(b, axis=-1)
    den = na * nb
    return np.where(den > 0, (a * b).sum(-1) / np.where(den > 0, den, 1.0), 0.0)


def half_split_cosines(pos: np.ndarray, neg: np.ndarray, n_splits: int = 200,
                       seed: int = 0) -> np.ndarray:
    """[n_splits, n_layers] float64: cosine between the two halves' mean differences at every
    layer, over seeded random halvings of the index-aligned pairs (module docstring)."""
    p, n = np.asarray(pos, np.float64), np.asarray(neg, np.float64)
    if p.shape != n.shape or p.ndim != 3:
        raise ValueError(f"pos/neg must be index-aligned [n, L, d]: {p.shape}, {n.shape}")
    if len(p) < 2:
        raise ValueError("half-splits need at least 2 pairs")
    rng = np.random.Generator(np.random.PCG64(seed))
    k = (len(p) + 1) // 2
    out = np.empty((int(n_splits), p.shape[1]), np.float64)
    for s in range(int(n_splits)):
        perm = rng.permutation(len(p))
        a, b = perm[:k], perm[k:]
        da = p[a].mean(0) - n[a].mean(0)
        db = p[b].mean(0) - n[b].mean(0)
        out[s] = _row_cos(da, db)
    return out


def cosine(a, b) -> float:
    """Cosine of two vectors (flattened, float64); 0.0 if either is zero."""
    x, y = np.asarray(a, np.float64).ravel(), np.asarray(b, np.float64).ravel()
    if x.shape != y.shape:
        raise ValueError(f"shape mismatch {x.shape} vs {y.shape}")
    return float(_row_cos(x[None], y[None])[0])


def rescale(v: np.ndarray, norm: float) -> np.ndarray:
    """v * norm / ||v|| as float32 (shape kept); raises on a zero vector."""
    x = np.asarray(v, np.float64)
    nv = float(np.linalg.norm(x))
    if nv == 0.0:
        raise ValueError("cannot rescale a zero vector")
    return (x * (float(norm) / nv)).astype(np.float32)


def gaussian_like(ref: np.ndarray, seed: int) -> np.ndarray:
    """1-D float32: per-dimension N(mean(ref), std(ref)) draws (`default_rng(seed)`), rescaled to
    ||ref|| (Hua et al.'s matched mean, variance and norm)."""
    r = np.asarray(ref, np.float64).ravel()
    if r.size == 0:
        raise ValueError("empty reference vector")
    g = np.random.default_rng(seed).normal(r.mean(), r.std(), size=r.size)
    return rescale(g, float(np.linalg.norm(r)))
