"""P40 model-side operations (torch; the pod and the tiny-model tests only).

Ported from ``probes/P36-workspace-binding-causal/p36/model.py`` with one change: an edit may
*add* its values to the block output (``mode = "add"``, for the mean-difference vector edits)
instead of overwriting it (``"set"``, the default and P36's only mode). P36's docstring follows.

Conventions are P11's (``probes/P11-bridge-read-vs-reanswer/p11/patch.py``): layer L is the
output of decoder block ``backend.blocks[L]``; hooks edit that output in place on the one
(prefill) forward. Every forward here is a single prefill with ``logits_to_keep=1`` over a batch
of equal-length prompts, so no padding is involved.

- ``load_jacobians``: per-layer [d, d] maps from the neuronpedia J-lens file (``"J"`` key, the
  wsbench layout read by ``emx.i1a.token_readers.load_jacobians_local``) or a J++ file
  (``{"parameters": {"jacobians": {layer: [d, d]}}, "source_layers", "config"}``, the
  ``workspace_lens`` layout of safety-research/jpp_lens @ 93f02cd).
- ``final_gain``: the final norm's elementwise gain g, as ``final_norm(ones)`` (rms(ones) = 1,
  so this is right for both ``w`` and ``1 + w`` parametrisations).
- ``build_dictionary``: unit-normalised rows of ``(W_U * g) @ M`` (fp32), M = a lens map or None
  (identity: the logit-lens dictionary). Rows of norm < 1e-8 are set to 0 (never selected).
- ``capture``: residuals at given positions and layers for a batch of equal-length prompts.
- ``forward_patched``: next-token logits for a batch under per-row patches at one or more layers.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager, nullcontext
from typing import Any


def _plain(model: Any) -> Any:
    return model.disable_adapter() if hasattr(model, "disable_adapter") else nullcontext()


def _out_tensor(out: Any) -> Any:
    return out[0] if isinstance(out, tuple) else out


def _load(path: str) -> Any:
    import torch

    try:
        return torch.load(str(path), map_location="cpu", weights_only=False, mmap=True)
    except RuntimeError:  # legacy (non-zip) serialisation cannot be memory-mapped
        return torch.load(str(path), map_location="cpu", weights_only=False)


def load_jacobians(path: str, layers: Sequence[int]) -> dict[int, Any]:
    """{layer: fp32 [d, d] on the CPU} for the requested layers (KeyError if one is missing)."""
    obj = _load(path)
    if "parameters" in obj:
        jac = obj["parameters"]["jacobians"]
    elif "J" in obj:
        jac = obj["J"]
    elif "jacobians" in obj:
        jac = obj["jacobians"]
    else:
        raise ValueError(f"{path}: no Jacobians (keys {sorted(obj)})")
    out: dict[int, Any] = {}
    for L in layers:
        if isinstance(jac, dict):
            m = jac[L] if L in jac else jac[str(L)]
        else:
            m = jac[L]
        out[L] = m.float().clone()
    return out


def available_layers(path: str) -> list[int]:
    obj = _load(path)
    if "parameters" in obj:
        return sorted(int(k) for k in obj["parameters"]["jacobians"])
    jac = obj["J"] if "J" in obj else obj["jacobians"]
    return sorted(int(k) for k in jac) if isinstance(jac, dict) else list(range(jac.shape[0]))


def final_gain(backend: Any) -> Any:
    import torch

    norm = backend.final_norm
    p = next(norm.parameters())
    with torch.no_grad():
        ones = torch.ones(1, p.shape[-1], device=p.device, dtype=torch.float32)
        g = norm(ones.to(p.dtype)).float()[0]
    return g


def build_dictionary(backend: Any, M: Any | None, *, chunk: int = 16384) -> Any:
    """fp32 [V, d] unit rows of (W_U * g) @ M on the backend's device."""
    import torch

    W = backend.unembed
    g = final_gain(backend).to(W.device)
    Md = M.to(W.device).float() if M is not None else None
    out = torch.empty(W.shape[0], W.shape[1], device=W.device, dtype=torch.float32)
    with torch.no_grad():
        for lo in range(0, W.shape[0], chunk):
            rows = W[lo : lo + chunk].float() * g
            if Md is not None:
                rows = rows @ Md
            n = rows.norm(dim=1, keepdim=True)
            out[lo : lo + chunk] = torch.where(n > 1e-8, rows / n.clamp_min(1e-30), 0.0)
    return out


def capture(
    backend: Any, ids: Sequence[Sequence[int]], positions: Any, layers: Sequence[int]
) -> dict[int, Any]:
    """{layer: [B, P, d] model dtype on the CPU}: block outputs at per-row ``positions`` [B, P]
    for a batch of equal-length prompts ``ids``."""
    import torch

    x = torch.tensor([list(r) for r in ids], device=backend.device)
    pos = torch.as_tensor(positions, dtype=torch.long, device=backend.device)
    rows = torch.arange(x.shape[0], device=backend.device).unsqueeze(1)
    store: dict[int, Any] = {}
    blocks = backend.blocks

    def make(layer: int):
        def hook(_mod, _inp, out):
            store[layer] = _out_tensor(out)[rows, pos].detach().cpu()

        return hook

    handles = [blocks[L].register_forward_hook(make(L)) for L in layers]
    try:
        with torch.no_grad(), _plain(backend.model):
            backend.model(x, logits_to_keep=1)
    finally:
        for h in handles:
            h.remove()
    return store


@contextmanager
def patched(backend: Any, edits: dict[int, tuple], n_total: int):
    """``edits = {layer: (positions [B, P] long, values [B, P, d][, mode])}``: overwrite
    (``mode = "set"``, the default) or add to (``"add"``) those positions of block ``layer``'s
    output on forwards of length ``n_total``. An add is computed in fp32 and rounded once to the
    model dtype. Yields per-layer counters."""
    import torch

    counter = {L: 0 for L in edits}
    handles = []

    def make(layer: int, pos: Any, vals: Any, mode: str):
        if mode not in ("set", "add"):
            raise ValueError(mode)

        def hook(_mod, _inp, out):
            h = _out_tensor(out)
            if h.shape[1] != n_total:
                return
            rows = torch.arange(h.shape[0], device=h.device).unsqueeze(1)
            p = pos.to(h.device)
            if mode == "set":
                h[rows, p] = vals.to(device=h.device, dtype=h.dtype)
            else:
                cur = h[rows, p].float()
                h[rows, p] = (cur + vals.to(device=h.device, dtype=torch.float32)).to(h.dtype)
            counter[layer] += 1

        return hook

    for L, e in edits.items():
        pos, vals = e[0], e[1]
        mode = e[2] if len(e) > 2 else "set"
        handles.append(backend.blocks[L].register_forward_hook(make(L, pos, vals, mode)))
    try:
        yield counter
    finally:
        for h in handles:
            h.remove()


def forward_patched(
    backend: Any,
    ids: Sequence[Sequence[int]],
    edits: dict[int, tuple[Any, Any]] | None = None,
    *,
    answer_ids: Any = None,
) -> dict[str, Any]:
    """Next-token logits at the last position of a batch of equal-length prompts under ``edits``.
    Returns CPU tensors: ``top1`` [B], and ``ans`` [B, A] (fp32 logits of ``answer_ids`` [B, A])
    plus ``logz`` [B] (logsumexp over the vocabulary)."""
    import torch

    x = torch.tensor([list(r) for r in ids], device=backend.device)
    with torch.no_grad(), _plain(backend.model), patched(backend, edits or {}, x.shape[1]) as c:
        logits = backend.model(x, logits_to_keep=1).logits[:, -1].float()
    if edits and any(v != 1 for v in c.values()):
        raise RuntimeError(f"patch counters {c}, expected 1 per layer")
    out = {"top1": logits.argmax(-1).cpu(), "logz": torch.logsumexp(logits, -1).cpu()}
    if answer_ids is not None:
        a = torch.as_tensor(answer_ids, dtype=torch.long, device=logits.device)
        out["ans"] = logits.gather(1, a).cpu()
    return out
