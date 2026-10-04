"""E1b Part 3(b): first-tokens activation test and its sufficiency direction (prereg §4.9(b),
"Sufficiency"; plan §1.6, U6a). Pod side: raw aggregates only, no ratios, bootstraps or labels.

Activation difference after Minder, Dumas et al. 2025 (§3, App. C.1, F.1): δ(j) = h_ft(j) −
h_untuned(j), the output hidden state of a decoder layer at position j of a plain-text document.
"Layer 16" is the output of `model.layers[16]` (0-based; `hidden_states[17]` in HF terms), the
paper's ⌊L/2⌋ for L = 32. "Untuned" is the same PeftModel under `model.disable_adapter()`.

Per document (all exactly `C.DOC_TOKENS` tokens, so a batch needs no padding):
- pass U: adapter off -> h_untuned, and the untuned k_proj/v_proj outputs at every restored
  position; pass F: adapter on -> h_ft and the fine-tuned k_proj/v_proj outputs;
- necessity `{K1,K5,ctrl}`: adapter on, the untuned K/V restored at the position set;
- sufficiency `suf_{K1,K5,ctrl}`: adapter off, the fine-tuned K/V injected at the position set.
δ_c = h_ft,c − h_untuned (necessity, c = none, K1, K5, ctrl); δ_suf,c = h_untuned+ftKV,c − h_untuned.
So 8 forward passes per document. K/V replacement reuses `emx.restore` (`restore_hooks`,
`replace_hook`, `attention_modules`, `HookStats`): replacing a position's k_proj/v_proj output is
replacing its cached key and value in every layer (see that module's docstring). `capture_kv` is
batched here (`_kv_hooks`), since upstream's stores row 0 only; `replace_hook` already takes a
[batch, positions, d] replacement.

Saved per model and layer at `C.firstk_path(cell, layer)` (npz), with `{stem}.meta.json`:
- `{cond}__docmean_decision`, `{cond}__docmean_descriptive`: per-document mean of δ over the decision
  (68-127) and descriptive (8-127) windows, [n_docs, d]. float32 for the primary layer's decision
  window; float16 elsewhere unless a value would not fit (then float32; the dtype is in the meta).
  At d = 4,096 and 2,000 documents: 32.8 MB per array in float32, 16.4 MB in float16; with 7
  conditions x 2 windows, ≈ 345 MB (layer 16) and ≈ 230 MB (layer 24) per model, ≈ 1.7 GB for
  the three models;
- `{cond}__posmean`: mean over documents of δ at every position, [128, d] float32 (2.1 MB);
- `eta_ft`: mean ‖h_ft(j)‖ over documents and positions j >= `C.STEER_NORM_EXCLUDE_FIRST`;
  `ft_norm_by_pos`: mean ‖h_ft(j)‖ per position, [128];
- `doc_ids`, `conditions`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

from emx import restore as R
from emx.e1b import contracts as C

POSITION_SETS: dict[str, tuple[int, ...]] = {
    "K1": tuple(range(C.FIRSTK_KS[0])),
    "K5": tuple(range(C.FIRSTK_KS[1])),
    "ctrl": C.CTRL_POSITIONS,
}
WINDOWS: dict[str, tuple[int, int]] = {
    "decision": C.DECISION_WINDOW,
    "descriptive": C.DESCRIPTIVE_WINDOW,
}  # inclusive


def conditions(sets: Sequence[str] = tuple(POSITION_SETS)) -> list[str]:
    """Necessity `none` and `{set}`, sufficiency `suf_{set}` (no `suf_none`: its δ is 0). Names
    and npz keys are frozen with the reader, U6b (`emx.e1b.part3`)."""
    return ["none", *sets] + [f"suf_{s}" for s in sets]


# --- documents -----------------------------------------------------------------------------------


def ids_sha256(ids: Sequence[Sequence[int]]) -> str:
    """Hash of the truncated token ids, in file order, as `scripts/e1b_fineweb_sample.py` records
    it (`heads_sha256`): sha256 of the int64 [n_docs, n_tokens] array's C-order bytes."""
    return hashlib.sha256(np.ascontiguousarray(ids, dtype=np.int64).tobytes()).hexdigest()


def load_docs(
    path: Path, tokenizer, n_docs: int | None = None, doc_tokens: int = C.DOC_TOKENS
) -> tuple[list[str], list[list[int]]]:
    """`{doc_id, text}` rows -> (doc ids, the first `doc_tokens` ids of each, no special tokens).
    Every document must have at least `doc_tokens` tokens (U1 selected them so)."""
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    rows = rows[:n_docs] if n_docs else rows
    doc_ids, ids = [], []
    for r in rows:
        toks = tokenizer(r["text"], add_special_tokens=False)["input_ids"]
        if len(toks) < doc_tokens:
            raise ValueError(f"document {r['doc_id']} has {len(toks)} < {doc_tokens} tokens")
        doc_ids.append(str(r["doc_id"]))
        ids.append([int(t) for t in toks[:doc_tokens]])
    if len(set(doc_ids)) != len(doc_ids):
        raise ValueError("duplicate doc ids")
    return doc_ids, ids


# --- hooks ---------------------------------------------------------------------------------------


def decoder_layers(model) -> list[torch.nn.Module]:
    """Every decoder layer, through a PeftModel wrapper if present (as `R.attention_modules`)."""
    inner = model.get_base_model() if hasattr(model, "get_base_model") else model
    return list(inner.model.layers)


@contextlib.contextmanager
def residual_hooks(model, layers: Sequence[int], store: dict[int, torch.Tensor]) -> Iterator[None]:
    """Record each listed decoder layer's output hidden state ([batch, seq, d], float32) into
    `store`. Handles layers that return a tensor (transformers 5) or a tuple (older)."""

    def rec(li: int):
        def hook(_m, _inp, out):
            h = out[0] if isinstance(out, tuple) else out
            store[li] = h.detach().float().clone()

        return hook

    mods = decoder_layers(model)
    with R._forward_hooks([(mods[li], rec(li)) for li in layers]):
        yield


def _kv_hooks(model, positions: list[int], store: list[dict]):
    """Batched `R.capture_kv`: k_proj/v_proj outputs at `positions`, [batch, len(positions), d]."""

    def rec(i: int, name: str):
        def hook(_m, _inp, out):
            store[i][name] = out[:, positions, :].detach().clone()

        return hook

    pairs = []
    for i, attn in enumerate(R.attention_modules(model)):
        pairs += [(attn.k_proj, rec(i, "k")), (attn.v_proj, rec(i, "v"))]
    return R._forward_hooks(pairs)


def subset_kv(kv: list[dict], positions: list[int], subset: Sequence[int]) -> list[dict]:
    """The rows of a captured K/V (at `positions`) that belong to `subset`."""
    idx = [positions.index(p) for p in subset]
    return [{n: t[:, idx, :] for n, t in layer.items()} for layer in kv]


@torch.no_grad()
def forward(
    model,
    x: torch.Tensor,
    layers: Sequence[int],
    adapter: bool,
    *,
    ref: list[dict] | None = None,
    positions: Sequence[int] = (),
    capture: list[int] | None = None,
) -> tuple[dict[int, torch.Tensor], list[dict] | None]:
    """One forward pass over a [batch, seq] block. Returns residuals at `layers` and, if `capture`
    is given, this pass's own K/V at those positions. With `ref`, K/V at `positions` are replaced
    by `ref` (E1's prefill-only replace hook; a single no-cache forward is the prefill call)."""
    res: dict[int, torch.Tensor] = {}
    kv = [{} for _ in R.attention_modules(model)] if capture is not None else None
    stats = R.HookStats()
    with contextlib.ExitStack() as st:
        if not adapter:
            st.enter_context(model.disable_adapter())
        st.enter_context(residual_hooks(model, layers, res))
        if capture is not None:
            st.enter_context(_kv_hooks(model, capture, kv))
        if ref is not None:
            st.enter_context(R.restore_hooks(model, ref, list(positions), x.shape[1], stats))
        model(input_ids=x, use_cache=False)
    if ref is not None:
        n_hooks = 2 * len(R.attention_modules(model))
        if stats.prefill_replacements != n_hooks or stats.decode_calls != 0:
            raise RuntimeError(f"restore hooks misfired: {asdict(stats)}")
    return res, kv


# --- one block of documents ----------------------------------------------------------------------


def block_deltas(
    model,
    x: torch.Tensor,
    layers: Sequence[int],
    pos_sets: dict[str, Sequence[int]] = POSITION_SETS,
) -> tuple[dict[str, dict[int, torch.Tensor]], dict[int, torch.Tensor]]:
    """δ for every condition at every layer ({cond: {layer: [batch, seq, d]}}), and h_ft."""
    union = sorted({p for s in pos_sets.values() for p in s})
    h_un, kv_un = forward(model, x, layers, adapter=False, capture=union)
    h_ft, kv_ft = forward(model, x, layers, adapter=True, capture=union)
    out = {"none": {li: h_ft[li] - h_un[li] for li in layers}}
    for name, pos in pos_sets.items():
        pos = list(pos)
        nec, _ = forward(model, x, layers, True, ref=subset_kv(kv_un, union, pos), positions=pos)
        suf, _ = forward(model, x, layers, False, ref=subset_kv(kv_ft, union, pos), positions=pos)
        out[name] = {li: nec[li] - h_un[li] for li in layers}
        out[f"suf_{name}"] = {li: suf[li] - h_un[li] for li in layers}
    return out, h_ft


def _compact(a: np.ndarray, keep32: bool) -> np.ndarray:
    """float16 unless `keep32` or a value would overflow or lose finiteness."""
    if keep32:
        return a.astype(np.float32)
    b = a.astype(np.float16)
    return b if np.isfinite(b).all() and np.isfinite(a).all() else a.astype(np.float32)


def run(
    model,
    ids: Sequence[Sequence[int]],
    layers: Sequence[int] = C.LAYERS,
    *,
    batch_size: int = 16,
    pos_sets: dict[str, Sequence[int]] = POSITION_SETS,
    windows: dict[str, tuple[int, int]] = WINDOWS,
    primary_layer: int = C.PRIMARY_LAYER,
    norm_exclude_first: int = C.STEER_NORM_EXCLUDE_FIRST,
    on_block: Callable[[int, int], None] | None = None,
) -> dict[int, dict[str, np.ndarray]]:
    """The raw aggregates of every layer (keys as the module docstring; no `doc_ids`)."""
    seq = len(ids[0])
    if any(len(d) != seq for d in ids):
        raise ValueError("every document must have the same number of tokens")
    for lo, hi in windows.values():
        if not 0 <= lo <= hi < seq:
            raise ValueError(f"window {lo}-{hi} outside {seq} tokens")
    if max(p for s in pos_sets.values() for p in s) >= seq:
        raise ValueError("restored positions outside the document")
    conds = conditions(tuple(pos_sets))
    dev = next(model.parameters()).device
    pos_sum = {li: {c: np.zeros(0) for c in conds} for li in layers}
    wins = {li: {(w, c): [] for w in windows for c in conds} for li in layers}
    norm_sum = {li: np.zeros(seq) for li in layers}
    for b0 in range(0, len(ids), batch_size):
        x = torch.tensor(ids[b0 : b0 + batch_size], device=dev)
        deltas, h_ft = block_deltas(model, x, layers, pos_sets)
        for li in layers:
            norm_sum[li] += h_ft[li].norm(dim=-1).sum(0).double().cpu().numpy()
            for c in conds:
                d = deltas[c][li]
                s = d.sum(0).double().cpu().numpy()
                pos_sum[li][c] = s if pos_sum[li][c].size == 0 else pos_sum[li][c] + s
                for w, (lo, hi) in windows.items():
                    wins[li][(w, c)].append(d[:, lo : hi + 1, :].mean(1).cpu().numpy())
        if on_block is not None:
            on_block(min(b0 + batch_size, len(ids)), len(ids))
    n = len(ids)
    out: dict[int, dict[str, np.ndarray]] = {}
    for li in layers:
        a: dict[str, np.ndarray] = {}
        for c in conds:
            a[f"{c}__posmean"] = (pos_sum[li][c] / n).astype(np.float32)
            for w in windows:
                keep32 = li == primary_layer and w == "decision"
                a[f"{c}__docmean_{w}"] = _compact(np.concatenate(wins[li][(w, c)]), keep32)
        by_pos = norm_sum[li] / n
        a["ft_norm_by_pos"] = by_pos.astype(np.float32)
        a["eta_ft"] = np.float64(by_pos[norm_exclude_first:].mean())
        a["conditions"] = np.array(conds)
        out[li] = a
    return out


def meta_path(npz_path: Path) -> Path:
    return Path(str(npz_path)[: -len(".npz")] + ".meta.json")


def save(path: Path, arrays: dict[str, np.ndarray], doc_ids: Sequence[str], meta: dict) -> Path:
    """Write the npz (atomically: temp file, then rename) and its meta JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez(tmp, doc_ids=np.array(list(doc_ids)), **arrays)
    tmp.replace(path)
    dtypes = {k: str(v.dtype) for k, v in arrays.items() if "__docmean_" in k}
    meta_path(path).write_text(json.dumps({**meta, "dtypes": dtypes}, indent=2) + "\n")
    return path
