"""Attention masks that block a query span from a key span, for DeepSeek-V4-Flash and Gemma 4.

A `Rule` says: queries at absolute positions [q0, q1) may not attend to keys at [k0, k1), at the
layers in `layers` (None = every layer). Rules act on the prefill call only (the call whose
position_ids cover the whole prompt); decode steps are untouched, so the generated answer sees
everything as usual.

    Mask B (Baherwani et al. 2026, App. C.1): queries = the target turn's dots, keys = [0, first dot)
    Mask Q: queries = the dots, keys = the target turn's text before the dots
    knockout (Stage 2): queries = the answer suffix, keys = one dot group, at one layer band

**DeepSeek-V4-Flash** (transformers 5.17 `modeling_deepseek_v4`). A query reads three kinds of key:
1. the 128-token sliding window: the float mask passed to every `DeepseekV4Attention` as the
   `attention_mask` kwarg ([B, 1, S, S] on the prefill). A forward pre-hook clones it and sets the
   blocked (query, key) cells to the dtype minimum.
2. compressed entries (CSA layers: 4-token windows with the previous window's Ca half, so entry
   w pools positions max(0, 4w - 4) .. 4w + 3; HCA layers: entry w pools 128w .. 128w + 127). The
   compressor returns (compressed_kv, block_bias [B, 1, S, T]); a forward hook sets the bias to
   -inf for the blocked queries at every entry whose pooled span overlaps the blocked keys.
3. on CSA layers, the lightning indexer's top-512 choice among the compressed entries. A forward
   hook on `indexer.scorer` (scores [B, S, T] before the top-k) sets blocked entries to -inf, so
   when the selection binds (prompts above ~2,048 tokens) the top-k is filled with allowed
   entries first. Picks that still land on a blocked entry are removed by (2).
An entry is blocked whenever any position it pools is blocked (conservative; its gate may give
that position little weight). The pooled spans follow `DeepseekV4CSACompressor` / `HCACompressor`
on the first (prefill) call with a fresh cache, where entry 0 has no previous-window half.

**Gemma 4** (`modeling_gemma4`): one forward pre-hook per `Gemma4TextAttention`. With sdpa and no
padding the model may pass attention_mask=None (the kernel's causal path); the hook then builds
the layer's own mask (causal, plus the sliding window on sliding layers: q - k < window) so every
arm, masked or not, runs the same explicit-mask kernel (Gemma 4's late-layer numerics depend on
the computation path; memory: gemma4-bf16-readout-noise). Install with no rules for the unmasked
arms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

CSA = "compressed_sparse_attention"
HCA = "heavily_compressed_attention"


@dataclass(frozen=True)
class Rule:
    q0: int
    q1: int
    k0: int
    k1: int
    layers: frozenset[int] | None = None

    def __post_init__(self) -> None:
        if not (0 <= self.q0 < self.q1 and 0 <= self.k0 < self.k1):
            raise ValueError(f"empty or negative span in {self}")
        if self.k0 < self.q1 and self.q0 < self.k1:
            raise ValueError(f"query and key spans overlap in {self}")

    def on(self, layer: int) -> bool:
        return self.layers is None or layer in self.layers


def entry_spans(kind: str, n_entries: int, rate: int) -> list[tuple[int, int]]:
    """Inclusive (first, last) source position pooled by each compressed entry on the prefill."""
    if kind == CSA:
        return [(max(0, rate * w - rate), rate * w + rate - 1) for w in range(n_entries)]
    if kind == HCA:
        return [(rate * w, rate * w + rate - 1) for w in range(n_entries)]
    raise ValueError(kind)


def blocked_entries(spans: list[tuple[int, int]], k0: int, k1: int) -> list[int]:
    return [w for w, (a, b) in enumerate(spans) if a < k1 and b >= k0]


class Counter:
    """How often each hook changed something, per layer: {kind: {layer: n}}."""

    def __init__(self) -> None:
        self.n: dict[str, dict[int, int]] = {}

    def add(self, kind: str, layer: int) -> None:
        self.n.setdefault(kind, {})
        self.n[kind][layer] = self.n[kind].get(layer, 0) + 1

    def as_dict(self) -> dict:
        return {k: {str(layer): c for layer, c in sorted(v.items())} for k, v in self.n.items()}


def _block_float(mask: Any, rules: list[Rule], layer: int, n_keys: int) -> Any:
    import torch

    m = mask.clone()
    lo = torch.finfo(m.dtype).min
    for r in rules:
        if r.on(layer):
            m[..., r.q0:r.q1, r.k0:min(r.k1, n_keys)] = lo
    return m


# --------------------------------------------------------------------- DeepSeek-V4


class V4Masks:
    """Context manager installing the rules on a DeepseekV4 model for prompts of `seq_len`."""

    def __init__(self, model: Any, rules: list[Rule], seq_len: int) -> None:
        self.model, self.rules, self.seq_len = model, list(rules), seq_len
        self.count = Counter()
        self.handles: list[Any] = []
        for r in self.rules:
            if r.q1 > seq_len or r.k1 > seq_len:
                raise ValueError(f"{r} beyond the prompt ({seq_len} tokens)")

    def _layers(self) -> list[Any]:
        inner = getattr(self.model, "model", self.model)
        return list(inner.layers)

    def _prefill(self, position_ids: Any) -> bool:
        return position_ids is not None and position_ids.shape[-1] == self.seq_len

    def _attn_pre(self, layer: int):
        def fn(_mod, args, kwargs):
            if not self._prefill(kwargs.get("position_ids")):
                return None
            mask = kwargs.get("attention_mask")
            if mask is None or mask.dtype.is_floating_point is False:
                raise TypeError(f"layer {layer}: expected a float attention mask, got "
                                f"{None if mask is None else mask.dtype}")
            if mask.shape[-2] != self.seq_len or mask.shape[-1] != self.seq_len:
                raise ValueError(f"layer {layer}: mask {tuple(mask.shape)} on the prefill")
            if not any(r.on(layer) for r in self.rules):
                return None
            kwargs = dict(kwargs)
            kwargs["attention_mask"] = _block_float(mask, self.rules, layer, self.seq_len)
            self.count.add("window", layer)
            return args, kwargs
        return fn

    def _comp_post(self, layer: int, kind: str, rate: int):
        def fn(_mod, _inp, out):
            compressed_kv, bias = out
            if bias is None or bias.shape[-2] != self.seq_len:
                return None
            rules = [r for r in self.rules if r.on(layer)]
            if not rules:
                return None
            spans = entry_spans(kind, bias.shape[-1], rate)
            bias = bias.clone()
            for r in rules:
                ws = blocked_entries(spans, r.k0, r.k1)
                if ws:
                    bias[..., r.q0:r.q1, ws] = float("-inf")
            self.count.add(f"compressed_{'csa' if kind == CSA else 'hca'}", layer)
            return compressed_kv, bias
        return fn

    def _scorer_post(self, layer: int, rate: int):
        def fn(_mod, _inp, scores):
            if scores.shape[-2] != self.seq_len:
                return None
            rules = [r for r in self.rules if r.on(layer)]
            if not rules:
                return None
            spans = entry_spans(CSA, scores.shape[-1], rate)
            scores = scores.clone()
            for r in rules:
                ws = blocked_entries(spans, r.k0, r.k1)
                if ws:
                    scores[..., r.q0:r.q1, ws] = float("-inf")
            self.count.add("indexer", layer)
            return scores
        return fn

    def __enter__(self) -> Self:
        for i, block in enumerate(self._layers()):
            attn = block.self_attn
            self.handles.append(attn.register_forward_pre_hook(self._attn_pre(i),
                                                               with_kwargs=True))
            comp = getattr(attn, "compressor", None)
            if comp is None:
                continue
            kind = attn.layer_type
            self.handles.append(comp.register_forward_hook(
                self._comp_post(i, kind, int(comp.compress_rate))))
            if kind == CSA:
                self.handles.append(comp.indexer.scorer.register_forward_hook(
                    self._scorer_post(i, int(comp.indexer.compress_rate))))
        return self

    def __exit__(self, *exc) -> None:
        for h in self.handles:
            h.remove()
        self.handles = []


def v4_expected_counts(layer_types: list[str], rules: list[Rule], seq_len: int,
                       rates: dict[str, int]) -> dict[str, dict[str, int]]:
    """The counts one masked prefill should leave in V4Masks.count (every hook fires once per
    layer a rule covers; a compressor only once it has emitted an entry, i.e. seq_len >= rate)."""
    out: dict[str, dict[str, int]] = {}
    for i, kind in enumerate(layer_types):
        if not any(r.on(i) for r in rules):
            continue
        out.setdefault("window", {})[str(i)] = 1
        if kind in (CSA, HCA) and seq_len >= rates[kind]:
            tag = "compressed_csa" if kind == CSA else "compressed_hca"
            out.setdefault(tag, {})[str(i)] = 1
            if kind == CSA:
                out.setdefault("indexer", {})[str(i)] = 1
    return out


# --------------------------------------------------------------------- Gemma 4


class GemmaMasks:
    """Context manager: explicit attention masks on every Gemma4TextAttention on the prefill,
    with the rules applied (no rules = the unmasked reference on the same kernel path)."""

    def __init__(self, blocks: Any, rules: list[Rule], seq_len: int) -> None:
        self.blocks, self.rules, self.seq_len = list(blocks), list(rules), seq_len
        self.count = Counter()
        self.handles: list[Any] = []

    def _pre(self, layer: int, attn: Any):
        def fn(_mod, args, kwargs):
            import torch

            hs = kwargs.get("hidden_states", args[0] if args else None)
            if hs is None or hs.shape[1] != self.seq_len:
                return None
            mask = kwargs.get("attention_mask")
            S = self.seq_len
            if mask is None:
                mask = causal_bool(S, getattr(attn, "sliding_window", None), hs.device)
                self.count.add("built", layer)
            if mask.shape[-2] != S or mask.shape[-1] != S:
                raise ValueError(f"layer {layer}: mask {tuple(mask.shape)} on the prefill")
            rules = [r for r in self.rules if r.on(layer)]
            if rules:
                if mask.dtype == torch.bool:
                    mask = mask.clone()
                    for r in rules:
                        mask[..., r.q0:r.q1, r.k0:r.k1] = False
                else:
                    mask = _block_float(mask, rules, layer, S)
                self.count.add("window", layer)
            kwargs = dict(kwargs)
            kwargs["attention_mask"] = mask
            return args, kwargs
        return fn

    def __enter__(self) -> Self:
        for i, b in enumerate(self.blocks):
            self.handles.append(b.self_attn.register_forward_pre_hook(
                self._pre(i, b.self_attn), with_kwargs=True))
        return self

    def __exit__(self, *exc) -> None:
        for h in self.handles:
            h.remove()
        self.handles = []


def causal_bool(n: int, window: int | None, device: Any) -> Any:
    """[1, 1, n, n] bool, True = may attend: k <= q, and q - k < window when windowed."""
    import torch

    q = torch.arange(n, device=device).view(-1, 1)
    k = torch.arange(n, device=device).view(1, -1)
    m = k <= q
    if window:
        m &= (q - k) < window
    return m.view(1, 1, n, n)
