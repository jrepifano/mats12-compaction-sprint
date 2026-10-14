"""P42 S4: per-example projected gradients (plan.md "Setup: S4 attribution / gradient coverage").

Loss per example: the mean NLL of its first `resp_tokens` response tokens (each predicted from its
whole prefix). Gradient w.r.t. the MLP down-projection weight W (d_model x d_ff, out = a W^T) of
each block in `layers`, projected LoGra-style with a factorised random projection:
    G = P_out (dL/dW) P_in^T = sum_t (P_out g_t)(P_in a_t)^T          (k x k per layer)
with a_t the down_proj input and g_t the gradient w.r.t. its output at position t. The sum runs
over EVERY position of the sequence, so G is exactly the projection of the true weight gradient
(tests/test_grads.py checks it against autograd's dL/dW). Prompt positions do carry gradient:
attention in the blocks above lets their down_proj outputs reach the response logits. (A
"response positions only" sum would not be the gradient of anything; this departs from the build
brief's wording on purpose.)

P_out (k x d_model) and P_in (k x d_ff) are N(0, 1/k) entries (Johnson-Lindenstrauss scale, so
each layer's block keeps its share of the gradient norm), drawn on the CPU in float32 from
torch.Generator seeded SEED_PROJ + layer index.

Batching: the batch loss is the SUM of per-example losses; examples share no computation (right
padding + causal attention + attention mask), so each example's g_t is its own per-example
gradient. Pad positions have g = 0 and are masked out anyway.

Graph cut: every parameter is frozen and a forward pre-hook on block `cut` replaces the residual
entering it with `h.detach().requires_grad_(True)`; blocks below build no graph, and backward runs
through blocks cut..N-1, the final norm and lm_head only. cut must be <= min(layers).

Output per example: the per-layer k x k blocks flattened in `layers` order and concatenated
(4 x 64 x 64 = 16,384 dims for P42), float32; `l2_normalise` then unit-normalises (LESS).
"""

from __future__ import annotations

from collections.abc import Sequence

import torch

from p42.feats import Item, head_logits, pad_right


def projections(layers: Sequence[int], d_model: int, d_ff: int, k: int, seed: int,
                device=None, dtype=torch.float32) -> dict[int, tuple[torch.Tensor, torch.Tensor]]:
    out = {}
    for L in layers:
        g = torch.Generator().manual_seed(int(seed) + int(L))
        p_out = torch.randn(k, d_model, generator=g, dtype=torch.float32) / k ** 0.5
        p_in = torch.randn(k, d_ff, generator=g, dtype=torch.float32) / k ** 0.5
        out[L] = (p_out.to(device=device, dtype=dtype), p_in.to(device=device, dtype=dtype))
    return out


def blocks(model):
    return model.model.layers


class _Hooks:
    """Forward hooks on each down_proj (project a_t at once; register a tensor hook that projects
    g_t) and the graph-cut pre-hook."""

    def __init__(self, model, layers, projs, cut: int, mask: torch.Tensor):
        self.layers, self.projs, self.mask = list(layers), projs, mask
        self.A: dict[int, torch.Tensor] = {}
        self.Gp: dict[int, torch.Tensor] = {}
        self.handles = []
        bl = blocks(model)
        for L in self.layers:
            self.handles.append(bl[L].mlp.down_proj.register_forward_hook(self._fwd(L)))
        self.handles.append(bl[cut].register_forward_pre_hook(self._cut, with_kwargs=True))

    @staticmethod
    def _cut(module, args, kwargs):
        if args:
            h = args[0].detach().requires_grad_(True)
            return (h, *args[1:]), kwargs
        kwargs = dict(kwargs)
        kwargs["hidden_states"] = kwargs["hidden_states"].detach().requires_grad_(True)
        return args, kwargs

    def _fwd(self, L):
        def hook(module, inputs, output):
            p_out, p_in = self.projs[L]
            m = self.mask.unsqueeze(-1).to(torch.float32)
            a = inputs[0].detach().to(p_in.dtype)
            self.A[L] = (a @ p_in.T) * m  # [B, T, k]
            if output.requires_grad:
                def ghook(grad):
                    self.Gp[L] = (grad.to(p_out.dtype) @ p_out.T) * m
                output.register_hook(ghook)
        return hook

    def remove(self):
        for h in self.handles:
            h.remove()


def response_nll(model, final: torch.Tensor, ids: torch.Tensor, items: Sequence[Item]) -> torch.Tensor:
    """Per-example mean NLL over each item's response tokens (float32, differentiable)."""
    losses = []
    for b, it in enumerate(items):
        r0, n = len(it.prompt_ids), len(it.resp_ids)
        if n == 0:
            raise ValueError("S4 needs at least one response token")
        logits = head_logits(model, final[b, r0 - 1 : r0 - 1 + n])
        tgt = ids[b, r0 : r0 + n]
        losses.append(torch.nn.functional.cross_entropy(logits, tgt, reduction="mean"))
    return torch.stack(losses)


def per_example_grads(model, items: Sequence[Item], layers: Sequence[int], projs, pad_id: int,
                      cut: int | None = None) -> tuple[torch.Tensor, torch.Tensor]:
    """(G [B, len(layers) * k * k] float32 on the model's device, per-example loss [B])."""
    cut = min(layers) if cut is None else cut
    if cut > min(layers):
        raise ValueError(f"graph cut {cut} above the first gradient layer {min(layers)}")
    for p in model.parameters():
        p.requires_grad_(False)
    dev = next(model.parameters()).device
    ids, mask = pad_right([it.ids for it in items], pad_id, dev)
    hooks = _Hooks(model, layers, projs, cut, mask)
    try:
        with torch.enable_grad():
            out = model.model(input_ids=ids, attention_mask=mask, use_cache=False)
            losses = response_nll(model, out.last_hidden_state, ids, items)
            losses.sum().backward()
        blocks_ = []
        for L in layers:
            if L not in hooks.Gp:
                raise RuntimeError(f"no gradient reached layer {L}'s down_proj")
            blocks_.append(torch.einsum("btk,btj->bkj", hooks.Gp[L], hooks.A[L]).flatten(1))
        return torch.cat(blocks_, dim=1), losses.detach()
    finally:
        hooks.remove()


def l2_normalise(G: torch.Tensor) -> torch.Tensor:
    return G / G.norm(dim=1, keepdim=True).clamp_min(1e-30)
