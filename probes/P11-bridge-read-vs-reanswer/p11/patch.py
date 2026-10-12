"""P11 activation patching on the probed model (torch; imported on the pod and in the tiny-model
tests only).

Conventions are P03's / WorkspaceBench's: layer L is the output of decoder block
``backend.blocks[L]`` (the residual stream after that block), the probed model always runs with
any mounted adapter disabled (``wsbench Backend.capture`` does the same), and renders are
wsbench's ``chat_prefill`` with P03's prefill.

- ``capture_suffix``: one forward of a (premise) prompt; the residual at its last ``n`` positions
  at each requested layer, kept in the model dtype so an identity patch is exact.
- ``patch``: a context manager that registers a forward hook on block ``layer`` which overwrites
  the last ``n`` positions of the block output with given values, only on a forward whose
  sequence length is the patched prompt's (the prefill pass; later decoding steps of
  ``generate`` see one token and are left alone).
- ``capture``: ``Backend.capture`` with an optional patch: the residual at ``positions`` of the
  read layers, fp32 on the CPU.
- ``greedy``: greedy decoding of a few tokens under an optional patch.
- ``last_logits``: the next-token logits (identity-patch smoke check).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Patch:
    layer: int
    values: Any  # [n, d] tensor, the source run's residual at its last n positions
    n_total: int  # the patched prompt's length (the prefill forward)

    @property
    def n(self) -> int:
        return int(self.values.shape[0])


def _plain(model: Any) -> Any:
    return model.disable_adapter() if hasattr(model, "disable_adapter") else nullcontext()


def _out_tensor(out: Any) -> Any:
    return out[0] if isinstance(out, tuple) else out


def render_ids(prompt: str, prefill: str, tokenizer: Any) -> list[int]:
    """wsbench ``chat_prefill`` render (thinking off), as P03 read its items."""
    from wsbench.produce.render import render
    from wsbench.readplan import ReadSpec

    spec = ReadSpec(
        "p11", "x", "chat_prefill", {"kind": "last_n", "n": 3}, [], text=prompt, prefill=prefill
    )
    return list(render(spec, tokenizer).ids)


def capture_suffix(backend: Any, ids: Sequence[int], layers: Sequence[int], n: int) -> dict:
    """``{layer: [n, d]}`` residuals at the last ``n`` positions, model dtype, on the device."""
    import torch

    store: dict[int, Any] = {}
    blocks = backend.blocks

    def make(layer: int):
        def hook(_mod, _inp, out):
            store[layer] = _out_tensor(out)[0, -n:, :].detach().clone()

        return hook

    handles = [blocks[L].register_forward_hook(make(L)) for L in layers]
    try:
        with torch.no_grad(), _plain(backend.model):
            backend.model(torch.tensor([list(ids)], device=backend.device), logits_to_keep=1)
    finally:
        for h in handles:
            h.remove()
    missing = [L for L in layers if L not in store]
    if missing:
        raise RuntimeError(f"no residual captured at layers {missing}")
    return store


@contextmanager
def patch(backend: Any, p: Patch | None) -> Iterator[dict[str, int]]:
    """Overwrite the last ``p.n`` positions of block ``p.layer``'s output on the prefill forward.
    Yields a counter ``{"applied": k}`` so callers can assert the patch fired exactly once."""
    counter = {"applied": 0}
    if p is None:
        yield counter
        return

    def hook(_mod, _inp, out):
        h = _out_tensor(out)
        if h.shape[1] != p.n_total:
            return
        if h.shape[0] != 1:
            raise RuntimeError("patching assumes batch size 1")
        h[0, -p.n :, :] = p.values.to(device=h.device, dtype=h.dtype)
        counter["applied"] += 1
        return  # edited in place

    handle = backend.blocks[p.layer].register_forward_hook(hook)
    try:
        yield counter
    finally:
        handle.remove()


def capture(
    backend: Any,
    ids: Sequence[int],
    layers: Sequence[int],
    positions: Sequence[int],
    p: Patch | None = None,
) -> dict[int, Any]:
    """``{layer: [len(positions), d]}`` fp32 CPU at absolute ``positions``, under ``p``."""
    import torch

    if p is not None and any(L <= p.layer for L in layers):
        raise ValueError(f"read layers {list(layers)} must be above the patch layer {p.layer}")
    store: dict[int, Any] = {}
    want = torch.tensor(list(positions), dtype=torch.long)
    blocks = backend.blocks

    def make(layer: int):
        def hook(_mod, _inp, out):
            h = _out_tensor(out)
            store[layer] = h[0, want.to(h.device), :].detach().float().cpu()

        return hook

    handles = [blocks[L].register_forward_hook(make(L)) for L in layers]
    try:
        with torch.no_grad(), _plain(backend.model), patch(backend, p) as c:
            backend.model(torch.tensor([list(ids)], device=backend.device), logits_to_keep=1)
    finally:
        for h in handles:
            h.remove()
    if p is not None and c["applied"] != 1:
        raise RuntimeError(f"patch applied {c['applied']} times, expected once")
    return store


def last_logits(backend: Any, ids: Sequence[int], p: Patch | None = None) -> Any:
    """fp32 CPU next-token logits at the end of ``ids`` under ``p``."""
    import torch

    with torch.no_grad(), _plain(backend.model), patch(backend, p) as c:
        out = backend.model(torch.tensor([list(ids)], device=backend.device), logits_to_keep=1)
    if p is not None and c["applied"] != 1:
        raise RuntimeError(f"patch applied {c['applied']} times, expected once")
    return out.logits[0, -1].float().cpu()


def greedy(backend: Any, ids: Sequence[int], max_new_tokens: int, p: Patch | None = None) -> str:
    """Greedy continuation of ``ids`` under ``p`` (decoded, special tokens skipped, stripped)."""
    import torch

    tok = backend.tokenizer
    x = torch.tensor([list(ids)], device=backend.device)
    with torch.no_grad(), _plain(backend.model), patch(backend, p) as c:
        out = backend.model.generate(
            input_ids=x,
            attention_mask=torch.ones_like(x),
            do_sample=False,
            max_new_tokens=max_new_tokens,
            pad_token_id=tok.pad_token_id or tok.eos_token_id,
        )
    if p is not None and c["applied"] != 1:
        raise RuntimeError(f"patch applied {c['applied']} times, expected once")
    return tok.decode(out[0, len(ids) :].tolist(), skip_special_tokens=True).strip()
