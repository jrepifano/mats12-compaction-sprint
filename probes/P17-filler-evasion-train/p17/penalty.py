"""P17's differentiable penalty on the evaluated readout statistic (plan.md "Arms", E).

The eval statistic (`p06.f3_sensitivity.stats`, kind "logprob"): for item i and digit d,
S[i, d] = max over cells (layers >= 30, slots in the group) of lp[i, layer, slot, d] minus that
cell's mean over items; a hit is argmax_d S[i, d] == y's first digit. Groups: the `Filler:` label
(2 slots) and the 100 dots, scored separately.

The penalty replaces the cell max with a smooth max (tau * logsumexp(./tau)) and the mean over
eval items with a detached running mean over training items (`Baseline`), and hinges each group:
max(0, s(y digit) - max over other digits + margin); the two groups' hinges are summed. The hard
version (`hard_stats`, max and margin 0) is what the smoke compares with f3_sensitivity.

Window layout: the readout window is the last 115 prompt tokens (slots -115..-1); window index 0
is the pre-filler token, 1-2 the label, 3-102 the dots, 103-114 the suffix (`WIN`).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import schema as S

WIN_SLOTS = (S.PREFILL_SLOT, *S.SLOTS)  # -115..-1
WIN = {"prefill": [0], "label": [1, 2], "dots": list(range(3, 103)),
       "suffix": list(range(103, 115))}
PEN_GROUPS = ("label", "dots")
PEN_IDX = WIN["label"] + WIN["dots"]  # the 102 window positions the penalty reads
GROUP_IN_PEN = {"label": [0, 1], "dots": list(range(2, 102))}  # within PEN_IDX


def first_digit(v: int) -> int:
    return int(str(abs(int(v)))[0])


def readout_lp(hs: list[Any], rd: Any, layers: tuple[int, ...] = S.PEN_LAYERS,
               lens: Any = None, checkpoint: bool = True) -> Any:
    """hs: per block [B, P, d] (captured, in the graph) -> [B, len(layers), P, 10] fp32 digit
    log-probs, one layer's full-vocabulary logits at a time (activation-checkpointed when the
    graph is kept). `lens` (a p06.stage2.Lens) applies its map per layer; layer 59 has none."""
    import torch
    from torch.utils.checkpoint import checkpoint as ckpt

    from .common import digit_lp

    out = []
    for layer in layers:
        h = hs[layer]
        b, p, d = h.shape
        jm = lens.maps[layer].to(h.device) if lens is not None and layer in lens.maps else None

        def fn(x, jm=jm, b=b, p=p, d=d):
            return digit_lp(x.reshape(b * p, d), rd, jm).reshape(b, p, 10)

        if checkpoint and torch.is_grad_enabled() and h.requires_grad:
            out.append(ckpt(fn, h, use_reentrant=False))
        else:
            out.append(fn(h))
    return torch.stack(out, dim=1)


def smooth_penalty(lp: Any, base: Any, y_digit: Any, tau: float = S.TAU,
                   margin: float = S.MARGIN) -> tuple[Any, Any]:
    """lp [B, L, 102, 10] (PEN_IDX window), base [L, 102, 10] (detached) -> (penalty [B], the
    per-group hinges [B, 2])."""
    import torch

    res = lp - base
    hinges = []
    for g in PEN_GROUPS:
        r = res[:, :, GROUP_IN_PEN[g], :].flatten(1, 2)  # [B, cells, 10]
        s = tau * torch.logsumexp(r / tau, dim=1)  # [B, 10]
        sy = s.gather(1, y_digit[:, None])[:, 0]
        other = s.masked_fill(torch.nn.functional.one_hot(y_digit, 10).bool(), float("-inf"))
        hinges.append(torch.relu(sy - other.max(dim=1).values + margin))
    h = torch.stack(hinges, dim=1)
    return h.sum(dim=1), h


def hard_stats(lp: np.ndarray, base: np.ndarray) -> np.ndarray:
    """The eval statistic per group: lp [n, L, 102, 10], base [L, 102, 10] -> S [n, 2, 10]."""
    res = lp - base
    return np.stack([res[:, :, GROUP_IN_PEN[g], :].max(axis=(1, 2)) for g in PEN_GROUPS], 1)


def hard_hinge(S_: np.ndarray, y_digit: np.ndarray) -> np.ndarray:
    """[n, 2] hinge of the hard statistic at margin 0 (> 0 exactly when y's digit is the top)."""
    n = len(y_digit)
    sy = S_[np.arange(n), :, y_digit]
    other = S_.copy()
    other[np.arange(n), :, y_digit] = -np.inf
    return np.maximum(sy - other.max(axis=2), 0.0)


class Baseline:
    """The per-(layer, position, digit) running mean of training items' digit log-probs:
    an exponential moving average, initialised from the base model's mean."""

    def __init__(self, init: Any, alpha: float = 0.05) -> None:
        self.mean = init.detach().clone()
        self.alpha = alpha

    def update(self, lp: Any) -> None:
        self.mean.mul_(1 - self.alpha).add_(self.alpha * lp.detach().mean(dim=0))
