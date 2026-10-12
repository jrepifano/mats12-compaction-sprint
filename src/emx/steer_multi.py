"""Multi-layer additive steering with a per-row position mask, its generation entry point, and the
hook-correctness checks (probe P02, `probes/P02-evalaware-steering-check/plan.md`: direction A is
added at several layers to the user and generated tokens only, the system prompt unsteered).

Contract of `multi_steering(model, vectors, spans=..., prompt_lens=..., steer_decode=...)`:
- For every block index L in `vectors`, a forward hook adds vectors[L] to the OUTPUT of decoder
  block L (`emx.e1b.firstk.decoder_layers`, PEFT-safe). The output may be a tensor
  (transformers 5) or a tuple whose [0] is the hidden states; both are handled.
- The context wraps exactly ONE decode (one `emx.e13.hfgen.decode_batch` call). Each layer's FIRST
  call is the prefill; every later call is a decode step and must have sequence length 1 (else
  ValueError).
- Prefill: rows are LEFT-padded to W = h.shape[1]; row i's prompt has prompt_lens[i] tokens and
  sits at padded offset W - prompt_lens[i]. With spans[i] = (start, end) in unpadded prompt
  coordinates, row i is steered exactly at padded positions [off+start, off+end). spans None ->
  every position, padding included (prompt_lens then optional). An empty span (start == end)
  steers nothing in that row.
- Decode steps: every row (finished rows fed padding included) is steered iff `steer_decode`.
- The edit is `h + mask * v_cast` (`_edit`), v_cast = vectors[L] cast ONCE per layer to h's
  dtype/device, mask 0/1 in h's dtype: unsteered positions are bit-identical and steered ones are
  (h + v) rounded once in h's dtype. The expected edit in a check must therefore be computed in
  the residual's dtype, not float32 (E13's bf16 gotcha, `emx.e13.steer` docstring: a float32
  expectation failed a correct hook at a massive-activation position, |h| = 11,712).
- Hooks are removed on exit, also on exceptions.

`record_outputs` records block outputs (float32 CPU clones). Forward hooks run in registration
order, so entered BEFORE `multi_steering` it records each block's raw output and AFTER it the
steered one, in the same run: check (ii) needs no replay (at layer L both recordings see the same
input, already steered at earlier layers).

`decode_steered` is the one generation entry point: no vectors -> plain `hfgen.decode_batch` with
no hook installed; else `decode_batch` inside `multi_steering`.

`hook_checks_multi` (plan "Automatic rules" item 3; E13's checks (i)-(iv) per layer, plus (v)):
(i) all-zero vectors installed at the same layers -> seeded samples token-identical (and same
finish reason) to no hook; (ii) at every steered position (prefill span positions; every live
decode position when steer_decode), steered == (raw.to(dt) + v.to(dt)).float() within
BF16_ATOL_REL x max(|raw|, |steered|) per position, and the edit's projection on v/|v| within
PROJ_REL_TOL of the expected edit's; (iii) per layer, calls == the decode's forward count, the
first call at the padded width, every later one at length 1; (iv) `decode_steered` with no
vectors makes 0 hook calls, with the vectors >= 1 per layer (positive control); (v) at every
unsteered prefill position (outside the span: padding, the system prompt) steered == raw
exactly, at every layer.

Ported from `emx.e13.steer` (`SteerStats`, `steering`'s cast-once and tuple handling, `_check_ii`'s
tolerance and projection rule) and `emx.e1b.steer` (output hook, first call = prefill). torch is
imported inside functions, so this module imports without torch.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from emx.contracts import request_seed
from emx.e13 import hfgen

BF16_ATOL_REL = 2 ** -7  # = emx.e13.contracts.BF16_ATOL_REL
PROJ_REL_TOL = 0.1  # = emx.e13.steer.PROJ_REL_TOL


@dataclass
class MultiStats:
    """Hook calls per steered layer: `calls[L]` in the context(s), `seq_lens[L]` each call's
    sequence length (the first of a context is the prefill)."""

    calls: dict[int, int] = field(default_factory=dict)
    seq_lens: dict[int, list[int]] = field(default_factory=dict)

    def init(self, layers: Sequence[int]) -> None:
        for L in layers:
            self.calls.setdefault(int(L), 0)
            self.seq_lens.setdefault(int(L), [])

    def count(self, layer: int, seq_len: int) -> None:
        self.init([layer])
        self.calls[int(layer)] += 1
        self.seq_lens[int(layer)].append(int(seq_len))

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def as_vectors(vectors: Mapping[int, Any], hidden: int | None = None) -> dict:
    """{block index: float32 CPU 1-D finite tensor}, from numpy arrays, lists or tensors (copied).
    All vectors must have the same dimension, `hidden` if given. Keys must be ints >= 0."""
    import numpy as np
    import torch

    out: dict[int, Any] = {}
    for L, v in vectors.items():
        if isinstance(L, bool) or not isinstance(L, int | np.integer) or int(L) < 0:
            raise ValueError(f"layer keys must be block indices >= 0, got {L!r}")
        t = (v.detach().float().cpu().clone() if isinstance(v, torch.Tensor)
             else torch.from_numpy(np.asarray(v, dtype=np.float32).copy()))
        if t.ndim != 1 or t.numel() == 0:
            raise ValueError(f"layer {L}: the vector must be 1-D, got shape {tuple(t.shape)}")
        if not torch.isfinite(t).all():
            raise ValueError(f"layer {L}: the vector is not finite")
        out[int(L)] = t
    dims = {int(t.shape[0]) for t in out.values()}
    if len(dims) > 1:
        raise ValueError(f"vectors have different dimensions: {sorted(dims)}")
    if hidden is not None and dims and dims != {int(hidden)}:
        raise ValueError(f"vectors have {dims.pop()} dims, the residual {int(hidden)}")
    return out


def _hidden_size(model) -> int | None:
    cfg = getattr(model, "config", None)
    h = getattr(cfg, "hidden_size", None)
    return int(h) if isinstance(h, int) else None


def _blocks(model, layers: Sequence[int]) -> dict:
    from emx.e1b.firstk import decoder_layers

    mods = decoder_layers(model)
    bad = [L for L in layers if not 0 <= L < len(mods)]
    if bad:
        raise ValueError(f"layers {bad} out of range: the model has {len(mods)} decoder blocks")
    return {L: mods[L] for L in layers}


def _edit(h, mask, v):
    """The edit: h + mask * v (mask None = every position). Module-level so tests can plant a
    broken hook."""
    return h + v if mask is None else h + mask * v


def _check_spans(spans, prompt_lens) -> tuple[list | None, list | None]:
    lens = None if prompt_lens is None else [int(n) for n in prompt_lens]
    if lens is not None and any(n < 1 for n in lens):
        raise ValueError("prompt_lens must be >= 1")
    if spans is None:
        return None, lens
    if lens is None:
        raise ValueError("spans need prompt_lens")
    sp = [(int(a), int(b)) for a, b in spans]
    if len(sp) != len(lens):
        raise ValueError(f"{len(sp)} spans for {len(lens)} prompts")
    for i, ((a, b), n) in enumerate(zip(sp, lens, strict=True)):
        if not 0 <= a <= b <= n:
            raise ValueError(f"row {i}: span ({a}, {b}) outside its {n}-token prompt")
    return sp, lens


def _prefill_mask(n: int, width: int, spans, lens, dtype, device):
    """[n, width, 1] 0/1 mask in the residual's dtype (None when every position is steered)."""
    import torch

    if lens is not None:
        if len(lens) != n:
            raise ValueError(f"prefill has {n} rows, prompt_lens {len(lens)}")
        if max(lens) > width:
            raise ValueError(f"prefill width {width} < longest prompt {max(lens)}")
    if spans is None:
        return None
    m = torch.zeros((n, width, 1), dtype=dtype)
    for i, ((a, b), p) in enumerate(zip(spans, lens, strict=True)):
        off = width - p
        m[i, off + a:off + b] = 1
    return m.to(device)


@contextlib.contextmanager
def multi_steering(model, vectors: Mapping[int, Any], *,
                   spans: Sequence[tuple[int, int]] | None = None,
                   prompt_lens: Sequence[int] | None = None, steer_decode: bool = True,
                   stats: MultiStats | None = None) -> Iterator[None]:
    """Add vectors[L] to the output of decoder block L for every L, over ONE decode (module
    docstring for the prefill span mask, decode steps and the edit). Hooks removed on exit."""
    vecs = as_vectors(vectors, _hidden_size(model))
    if not vecs:
        raise ValueError("multi_steering needs at least one vector (use decode_steered with "
                         "vectors=None for an unhooked decode)")
    sp, lens = _check_spans(spans, prompt_lens)
    blocks = _blocks(model, sorted(vecs))
    if stats is not None:
        stats.init(sorted(vecs))

    def make(L: int):
        state: dict[str, Any] = {"calls": 0, "v": None, "mask": None}

        def hook(_m, _args, out):
            h = out[0] if isinstance(out, tuple) else out
            seq = int(h.shape[-2])
            first = state["calls"] == 0
            state["calls"] += 1
            if stats is not None:
                stats.count(L, seq)
            if state["v"] is None:  # cast to the residual's dtype/device once
                if h.shape[-1] != vecs[L].shape[0]:
                    raise ValueError(f"layer {L}: vector has {vecs[L].shape[0]} dims, the "
                                     f"residual {h.shape[-1]}")
                state["v"] = vecs[L].to(device=h.device, dtype=h.dtype)
            if first:
                mask = _prefill_mask(int(h.shape[0]), seq, sp, lens, h.dtype, h.device)
            else:
                if seq != 1:
                    raise ValueError(f"layer {L}: call {state['calls']} has sequence length "
                                     f"{seq}; multi_steering wraps exactly one decode")
                if not steer_decode:
                    return None  # output unchanged
                mask = None
            h2 = _edit(h, mask, state["v"])
            return (h2, *out[1:]) if isinstance(out, tuple) else h2

        return hook

    handles = []
    try:
        for L, block in blocks.items():
            handles.append(block.register_forward_hook(make(L)))
        yield
    finally:
        for hd in handles:
            hd.remove()


@contextlib.contextmanager
def record_outputs(model, layers: Sequence[int], store: dict) -> Iterator[None]:
    """Append each listed block's output hidden states (float32 CPU clone, [n, seq, d] per call)
    to store[L], and set store["dtype"] to the original dtype (module docstring for the order
    relative to `multi_steering`)."""
    blocks = _blocks(model, sorted({int(L) for L in layers}))
    for L in blocks:
        store.setdefault(L, [])

    def make(L: int):
        def hook(_m, _args, out):
            h = out[0] if isinstance(out, tuple) else out
            store[L].append(h.detach().float().cpu().clone())
            store["dtype"] = h.dtype

        return hook

    handles = []
    try:
        for L, block in blocks.items():
            handles.append(block.register_forward_hook(make(L)))
        yield
    finally:
        for hd in handles:
            hd.remove()


def decode_steered(model, prompts_ids: Sequence[Sequence[int]], seeds: Sequence[int],
                   vectors: Mapping[int, Any] | None, *,
                   spans: Sequence[tuple[int, int]] | None = None, steer_decode: bool = True,
                   stats: MultiStats | None = None,
                   max_new_tokens: int = hfgen.MAX_NEW_TOKENS) -> list:
    """Sample one answer per prompt (`hfgen.decode_batch`, `emx.restore.Sample`s in prompt order).
    vectors None or empty -> no hook installed; else steered by `multi_steering`."""
    if not vectors:
        return hfgen.decode_batch(model, prompts_ids, seeds, max_new_tokens=max_new_tokens)
    with multi_steering(model, vectors, spans=spans,
                        prompt_lens=[len(p) for p in prompts_ids], steer_decode=steer_decode,
                        stats=stats):
        return hfgen.decode_batch(model, prompts_ids, seeds, max_new_tokens=max_new_tokens)


# --- hook checks ---------------------------------------------------------------------------------


def _iii_ok(stats: MultiStats, layers: Sequence[int], width: int, n_forward: int) -> bool:
    return all(stats.calls.get(L, 0) == n_forward and stats.seq_lens[L][0] == width
               and all(s == 1 for s in stats.seq_lens[L][1:]) for L in layers)


def _exact_diff(a, b) -> float:
    """max |a - b|, NaN at the same place counting as equal (NaN elsewhere -> inf)."""
    import torch

    if a.numel() == 0:
        return 0.0
    d = (a - b).abs()
    both = torch.isnan(a) & torch.isnan(b)
    d = torch.where(both, torch.zeros_like(d), torch.nan_to_num(d, nan=math.inf))
    return float(d.max())


def _check_layer(raw: list, steered: list, v, dt, ids: list[list[int]], toks: list[list[int]],
                 spans, steer_decode: bool) -> dict:
    """Checks (ii) and (v) at one layer from its raw and steered recordings of one decode."""
    import torch

    if len(raw) != len(steered) or any(a.shape != b.shape for a, b in zip(raw, steered)):
        return {"aligned": False, "ii_pass": False, "ii_max_rel_err": None, "ii_n_positions": 0,
                "ii_n_exact": 0, "ii_exact_share": None, "v_max_abs_diff": None,
                "v_pass": False, "n_calls": len(raw)}
    vn = float(v.norm())
    unit = v / vn if vn > 0 else None
    v_dt = v.to(dt)
    n_pos = n_exact = 0
    worst_rel, ii_ok, any_proj = 0.0, True, False
    v_diff = 0.0
    width = raw[0].shape[1] if raw else 0
    for c, (rw, st) in enumerate(zip(raw, steered, strict=True)):
        for i, p in enumerate(ids):
            if c == 0:
                off = width - len(p)
                a, b = spans[i] if spans is not None else (-off, len(p))
                on = torch.zeros(width, dtype=torch.bool)
                on[off + a:off + b] = True
                if (~on).any():  # (v): unsteered prefill positions are bit-identical
                    v_diff = max(v_diff, _exact_diff(st[i, ~on], rw[i, ~on]))
                sel = on
            elif c < len(toks[i]):  # row i was live when this token was fed
                sel = torch.ones(1, dtype=torch.bool)
                if not steer_decode:
                    v_diff = max(v_diff, _exact_diff(st[i, sel], rw[i, sel]))
                    continue
            else:
                continue
            if not sel.any():
                continue
            r, s = rw[i, sel], st[i, sel]
            exp = (r.to(dt) + v_dt).float()
            err = (s - exp).abs().amax(-1)
            mag = torch.maximum(r.abs().amax(-1), s.abs().amax(-1))
            ii_ok = ii_ok and bool((err <= BF16_ATOL_REL * mag).all())
            rel = err / torch.clamp(mag, min=1e-30)
            worst_rel = max(worst_rel, float(rel.max()))
            n_pos += int(sel.sum())
            n_exact += int((s == exp).all(-1).sum())
            if unit is not None:
                proj = (s - r) @ unit
                proj_exp = (exp - r) @ unit
                ii_ok = ii_ok and bool(((proj - proj_exp).abs()
                                        <= PROJ_REL_TOL * proj_exp.abs() + 1e-12).all())
                any_proj = any_proj or bool((proj_exp.abs() > 0).any())
    # a check over no steered position, or (non-zero v) over positions where the edit vanished
    # in rounding everywhere, cannot tell a no-op hook from a correct one
    ii_ok = ii_ok and n_pos > 0 and (unit is None or any_proj)
    return {"aligned": True, "ii_pass": bool(ii_ok), "ii_max_rel_err": worst_rel,
            "ii_n_positions": n_pos, "ii_n_exact": n_exact, "ii_exact_share": n_exact / n_pos if n_pos else None,
            "v_max_abs_diff": v_diff, "v_pass": v_diff == 0.0, "n_calls": len(raw)}


def hook_checks_multi(model, prompts_ids: Sequence[Sequence[int]], vectors: Mapping[int, Any],
                      spans: Sequence[tuple[int, int]] | None, *,
                      seed_base: str = "steer_multi|hook_check", n_samples: int = 2,
                      max_new_tokens: int | None = None, step_tokens: int = 16,
                      steer_decode: bool = True) -> dict:
    """Checks (i)-(v) (module docstring) for these vectors and spans on these prompts. Returns a
    JSON-serialisable dict: layers, i_identical, ii_pass, ii_max_rel_err, ii_exact_share,
    iii_pass, iv_pass, v_pass, v_max_abs_diff, passed, per_layer. (i) decodes `n_samples`
    seeded samples per prompt to `max_new_tokens` (default hfgen.MAX_NEW_TOKENS); (ii)-(v)
    decode `step_tokens` per prompt."""
    import torch

    vecs = as_vectors(vectors, _hidden_size(model))
    if not vecs:
        raise ValueError("hook checks need at least one vector")
    layers = sorted(vecs)
    ids = [[int(t) for t in p] for p in prompts_ids]
    if not ids:
        raise ValueError("hook checks need at least one prompt")
    sp, _ = _check_spans(spans, [len(p) for p in ids])
    max_new = hfgen.MAX_NEW_TOKENS if max_new_tokens is None else int(max_new_tokens)

    # (i) + (iii): all-zero vectors at the same layers vs no hook, prompts x samples in one batch
    rep = [p for p in ids for _ in range(n_samples)]
    rep_spans = None if sp is None else [s for s in sp for _ in range(n_samples)]
    seeds = [request_seed(seed_base, f"p{i:02d}", s) for i in range(len(ids))
             for s in range(n_samples)]
    ref = decode_steered(model, rep, seeds, None, max_new_tokens=max_new)
    st_i = MultiStats()
    zeros = {L: torch.zeros_like(v) for L, v in vecs.items()}
    got = decode_steered(model, rep, seeds, zeros, spans=rep_spans, steer_decode=steer_decode,
                         stats=st_i, max_new_tokens=max_new)
    i_identical = all(a.token_ids == b.token_ids and a.finish_reason == b.finish_reason
                      for a, b in zip(ref, got, strict=True))
    width = max(len(p) for p in ids)
    iii_i = _iii_ok(st_i, layers, width, hfgen.n_forward_of(got))

    # (ii) + (iii) + (v): raw recorded before the steering hooks, steered after, one run
    seeds2 = [request_seed(seed_base, f"p{i:02d}", n_samples) for i in range(len(ids))]
    raw: dict = {}
    steered: dict = {}
    st_ii = MultiStats()
    with record_outputs(model, layers, raw), \
            multi_steering(model, vecs, spans=sp, prompt_lens=[len(p) for p in ids],
                           steer_decode=steer_decode, stats=st_ii), \
            record_outputs(model, layers, steered):
        samples = hfgen.decode_batch(model, ids, seeds2, max_new_tokens=step_tokens)
    toks = [s.token_ids for s in samples]
    dt = raw.get("dtype", torch.float32)
    iii_ii = _iii_ok(st_ii, layers, width, hfgen.n_forward_of(samples))
    per = {L: _check_layer(raw[L], steered[L], vecs[L], dt, ids, toks, sp, steer_decode)
           for L in layers}

    # (iv): no vectors -> no hook calls; the vectors -> calls at every layer (positive control)
    st_none, st_pos = MultiStats(), MultiStats()
    decode_steered(model, ids, seeds2, None, stats=st_none, max_new_tokens=step_tokens)
    decode_steered(model, ids, seeds2, vecs, spans=sp, steer_decode=steer_decode, stats=st_pos,
                   max_new_tokens=step_tokens)
    iv_pass = st_none.total == 0 and all(st_pos.calls.get(L, 0) >= 1 for L in layers)

    for L in layers:
        per[L].update(iii_calls_i=st_i.calls.get(L, 0), iii_calls_ii=st_ii.calls.get(L, 0),
                      iv_calls=st_pos.calls.get(L, 0))
    ii_pass = all(d["ii_pass"] for d in per.values())
    v_pass = all(d["v_pass"] for d in per.values())
    n_pos = sum(d["ii_n_positions"] for d in per.values())
    rel = [d["ii_max_rel_err"] for d in per.values()]
    vdiff = [d["v_max_abs_diff"] for d in per.values()]
    out = {
        "layers": layers,
        "dtype": str(dt).removeprefix("torch."),
        "steer_decode": bool(steer_decode),
        "i_identical": bool(i_identical),
        "ii_pass": bool(ii_pass),
        "ii_max_rel_err": None if None in rel else max(rel),
        "ii_exact_share": sum(d["ii_n_exact"] for d in per.values()) / n_pos if n_pos else None,
        "iii_pass": bool(iii_i and iii_ii),
        "iv_pass": bool(iv_pass),
        "v_pass": bool(v_pass),
        "v_max_abs_diff": None if None in vdiff else max(vdiff),
        "n_forward_i": hfgen.n_forward_of(got),
        "n_forward_ii": hfgen.n_forward_of(samples),
        "per_layer": {str(L): d for L, d in per.items()},
    }
    out["passed"] = bool(i_identical and ii_pass and iii_i and iii_ii and iv_pass and v_pass)
    return out
