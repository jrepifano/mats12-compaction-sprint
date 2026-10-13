"""Masked all-layer prompt recorder (pod; plan.md "Detectors").

`record(model, prompts_ids, spans=None, logit_ids=None, batch_size=64)` runs one forward per
length-sorted, LEFT-padded batch (E13's `hfgen._left_pad`: explicit position ids, padding masked)
with a forward hook on every decoder block, and returns, in the caller's order:
- `last`: float16 [n, n_layers, d], each block's output at the prompt's last token (with left
  padding that is position -1 of every row);
- `span`: float16 [n, n_layers, d], each block's output averaged over the prompt's span
  [start, end) in its own (unpadded) coordinates (only when `spans` is given);
- `logits`: float32 [n, len(logit_ids)], the final logits at the last token for those vocab ids
  (only when `logit_ids` is given).
The GPU work per batch is one gather per block; one host copy per block per batch.
torch is imported inside the function.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def record(model, prompts_ids: Sequence[Sequence[int]], spans: Sequence[tuple[int, int]] | None = None,
           logit_ids: Sequence[int] | None = None, batch_size: int = 64,
           pad_id: int | None = None) -> dict[str, np.ndarray]:
    import torch

    from emx.e1b.firstk import decoder_layers
    from emx.e13.hfgen import _left_pad
    from emx.qwen import registry as R

    pad = R.PAD_ID if pad_id is None else pad_id
    n = len(prompts_ids)
    if n == 0:
        raise ValueError("nothing to record")
    if spans is not None:
        if len(spans) != n:
            raise ValueError("one span per prompt")
        for p, (a, b) in zip(prompts_ids, spans, strict=True):
            if not 0 <= a < b <= len(p):
                raise ValueError(f"span {(a, b)} invalid for {len(p)} tokens")
    blocks = list(decoder_layers(model))
    L = len(blocks)
    d = int(model.config.hidden_size)
    last = np.zeros((n, L, d), np.float16)
    span = np.zeros((n, L, d), np.float16) if spans is not None else None
    logits = (np.zeros((n, len(logit_ids)), np.float32) if logit_ids is not None else None)
    lid = None
    order = sorted(range(n), key=lambda i: (len(prompts_ids[i]), i))
    ctx: dict = {}

    def hook_for(li: int):
        def hook(_m, _inp, o):
            h = o[0] if isinstance(o, tuple) else o  # [b, W, d] bf16
            ctx["last"][:, li] = h[:, -1].float()
            if ctx.get("mask") is not None:
                m = ctx["mask"]  # [b, W] float32, rows sum to 1
                ctx["span"][:, li] = torch.einsum("bw,bwd->bd", m, h.float())
        return hook

    handles = [b.register_forward_hook(hook_for(i)) for i, b in enumerate(blocks)]
    dev = next(model.parameters()).device
    if logit_ids is not None:
        lid = torch.tensor(list(logit_ids), device=dev)
    try:
        with torch.no_grad():
            for b0 in range(0, n, batch_size):
                rows = order[b0:b0 + batch_size]
                batch = [list(prompts_ids[r]) for r in rows]
                ids, attn, pos = _left_pad(batch, pad, dev)
                W = ids.shape[1]
                ctx["last"] = torch.zeros((len(rows), L, d), device=dev)
                if spans is not None:
                    m = torch.zeros((len(rows), W), device=dev)
                    for k, r in enumerate(rows):
                        off = W - len(prompts_ids[r])
                        a, e = spans[r]
                        m[k, off + a:off + e] = 1.0 / (e - a)
                    ctx["mask"] = m
                    ctx["span"] = torch.zeros((len(rows), L, d), device=dev)
                else:
                    ctx["mask"] = None
                out = model(input_ids=ids, attention_mask=attn, position_ids=pos,
                            use_cache=False, logits_to_keep=1)
                idx = np.asarray(rows)
                last[idx] = ctx["last"].half().cpu().numpy()
                if spans is not None:
                    span[idx] = ctx["span"].half().cpu().numpy()
                if logit_ids is not None:
                    logits[idx] = out.logits[:, -1, :].float()[:, lid].cpu().numpy()
    finally:
        for h in handles:
            h.remove()
    res = {"last": last}
    if span is not None:
        res["span"] = span
    if logits is not None:
        res["logits"] = logits
    return res
