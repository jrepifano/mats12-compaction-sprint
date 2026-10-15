"""P39 model-side gradients (torch; pod and tiny-model tests).

``ld_grad``: for a batch of equal-length prompts, LD = logit(a) - logit(b) at the last position
and its gradient with respect to block ``layer``'s output at given positions [B, P]. The states at
those positions are replaced by leaf tensors equal to themselves (the forward is unchanged), so
the backward runs from the logits down to ``layer`` only; parameters never get gradients.
Conventions as P36's ``p36.model`` (layer L = output of ``backend.blocks[L]``).

``capture_all``: block ``layer``'s output at every position from ``skip`` on, for the
covariance-matched random subspaces (plan.md, arm 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from typing import Any


def _plain(model: Any) -> Any:
    return model.disable_adapter() if hasattr(model, "disable_adapter") else nullcontext()


def freeze(backend: Any) -> None:
    for p in backend.model.parameters():
        p.requires_grad_(False)


def ld_grad(
    backend: Any,
    ids: Sequence[Sequence[int]],
    positions: Any,
    layer: int,
    answer_ids: Any,
) -> dict[str, Any]:
    """Returns CPU fp32 ``grad`` [B, P, d] (dLD/dh at the positions), ``ld`` [B] and ``h`` [B, P, d]
    (the states at the positions). ``answer_ids`` [B, 2]: LD = logit(col 0) - logit(col 1)."""
    import torch

    freeze(backend)
    x = torch.tensor([list(r) for r in ids], device=backend.device)
    pos = torch.as_tensor(positions, dtype=torch.long, device=backend.device)
    rows = torch.arange(x.shape[0], device=backend.device).unsqueeze(1)
    leaf: dict[str, Any] = {}

    def hook(_mod, _inp, out):
        h = out[0] if isinstance(out, tuple) else out
        v = h[rows, pos].detach().clone().requires_grad_(True)
        leaf["v"] = v
        h2 = h.detach().clone()  # nothing below this layer needs a graph
        h2 = h2.index_put((rows.expand_as(pos), pos), v)
        return (h2, *out[1:]) if isinstance(out, tuple) else h2

    handle = backend.blocks[layer].register_forward_hook(hook)
    try:
        with torch.enable_grad(), _plain(backend.model):
            logits = backend.model(x, logits_to_keep=1).logits[:, -1].float()
            a = torch.as_tensor(answer_ids, dtype=torch.long, device=logits.device)
            sel = logits.gather(1, a)
            ld = sel[:, 0] - sel[:, 1]
            (g,) = torch.autograd.grad(ld.sum(), leaf["v"])
    finally:
        handle.remove()
    return {
        "grad": g.detach().float().cpu(),
        "ld": ld.detach().cpu(),
        "h": leaf["v"].detach().float().cpu(),
    }


def capture_all(backend: Any, ids: Sequence[Sequence[int]], layer: int, skip: int) -> Any:
    """[B * (n - skip), d] block outputs at positions skip..n-1 (model dtype, CPU)."""
    import torch

    x = torch.tensor([list(r) for r in ids], device=backend.device)
    store: dict[str, Any] = {}

    def hook(_mod, _inp, out):
        h = out[0] if isinstance(out, tuple) else out
        store["h"] = h[:, skip:].detach().reshape(-1, h.shape[-1]).cpu()

    handle = backend.blocks[layer].register_forward_hook(hook)
    try:
        with torch.no_grad(), _plain(backend.model):
            backend.model(x, logits_to_keep=1)
    finally:
        handle.remove()
    return store["h"]
