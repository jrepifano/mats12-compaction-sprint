"""P46 prompt-only forward features (plan.md "Setup: Scores"; contracts.py SCORE_COLUMNS).

One causal forward per prompt over the rendered prompt only (template prefix + user content +
suffix with the generation prompt; P42's three-part tokenisation from `p42.feats`, so the
user-content span is identical to P42's). Per prompt:
- `nll_sum`, `nll_mean`, `n_content`: NLL of the user-content tokens, each predicted from its
  whole prefix (template included), fp32 lm_head (`p42.feats.head_logits`);
- `ent_mean` (U1): mean next-token entropy in nats of the model's distribution AT each
  user-content position t in [c0, c1) (the distribution over token t + 1; the last one predicts
  the first suffix token), from the log-softmax of the fp32 lm_head logits. The NLL uses the
  distributions at t - 1 for the same tokens, so one lm_head pass over positions c0 - 1 .. c1 - 1
  serves both;
- `mean` (S3/S3s/S3x): hidden_states[layer] mean-pooled over the user-content tokens, fp32
  accumulate (P42's "p" feature at L24);
- `last` (S5): hidden_states[layer] at the LAST rendered-prompt position (the end of the
  generation prompt), the position the first response token is predicted from (Li et al.'s
  last-token representation).
HF hidden_states index: 0 = embeddings, i = output of block i - 1 (`p42.feats.capture_hidden`).

torch is imported at module level: this module runs in /ws/venv-peft and in the laptop tests.
"""

from __future__ import annotations

from collections.abc import Sequence

import torch
from p42.feats import LOGIT_CHUNK, Item, capture_hidden, head_logits, pad_right

SCALARS = ("nll_sum", "nll_mean", "n_content", "ent_mean")
SC = {c: i for i, c in enumerate(SCALARS)}


def _logprob_rows(model, final: torch.Tensor, positions: list[tuple[int, int]]):
    """Yield (start, log_softmax rows [m, V] fp32) for `positions` in LOGIT_CHUNK chunks."""
    dev = final.device
    b = torch.tensor([p[0] for p in positions], device=dev)
    t = torch.tensor([p[1] for p in positions], device=dev)
    for s in range(0, len(positions), LOGIT_CHUNK):
        logits = head_logits(model, final[b[s : s + LOGIT_CHUNK], t[s : s + LOGIT_CHUNK]])
        yield s, torch.log_softmax(logits, dim=-1)
        del logits


@torch.no_grad()
def forward_prompts(model, items: Sequence[Item], layer: int, pad_id: int, *,
                    entropy: bool = True, hidden: bool = True) -> list[dict]:
    """One padded (right) forward over prompt-only items. Per item: nll_sum, nll_mean, n_content,
    ent_mean (NaN when `entropy` is False or the content is empty), and with `hidden` the fp32
    CPU tensors `mean` and `last` of hidden_states[layer]."""
    if any(len(it.resp_ids) for it in items):
        raise ValueError("prompt-only forward: items must have no response ids")
    dev = next(model.parameters()).device
    ids, mask = pad_right([it.ids for it in items], pad_id, dev)
    with capture_hidden(model, [layer] if hidden else []) as hs:
        out = model.model(input_ids=ids, attention_mask=mask, use_cache=False)
    final = out.last_hidden_state
    # positions c0 - 1 .. c1 - 1 per item: [0, n) predict the content tokens, [1, n] are the
    # content positions themselves (entropy)
    pos: list[tuple[int, int]] = []
    seg: list[tuple[int, int]] = []
    for bi, it in enumerate(items):
        a = len(pos)
        if it.c1 > it.c0:
            if it.c0 < 1:
                raise ValueError("content starts at position 0: no context to predict it from")
            pos += [(bi, t) for t in range(it.c0 - 1, it.c1)]
        seg.append((a, len(pos)))
    tok_lp = torch.zeros(len(pos), dtype=torch.float64)
    ent = torch.zeros(len(pos), dtype=torch.float64)
    if pos:
        tgt_t = torch.tensor([min(t + 1, ids.shape[1] - 1) for _, t in pos], device=dev)
        tgt_b = torch.tensor([b for b, _ in pos], device=dev)
        targets = ids[tgt_b, tgt_t]
        for s, lp in _logprob_rows(model, final, pos):
            m = lp.shape[0]
            tok_lp[s : s + m] = lp.gather(-1, targets[s : s + m].unsqueeze(-1)).squeeze(-1) \
                .double().cpu()
            if entropy:
                p = lp.exp()
                h = -torch.where(p > 0, p * lp, torch.zeros_like(lp)).sum(-1)
                ent[s : s + m] = h.double().cpu()
            del lp
    nan = float("nan")
    res = []
    if hidden:
        H = hs[layer]
        d = H.shape[-1]
        means = torch.full((len(items), d), nan, dtype=torch.float32, device=dev)
        lasts = torch.empty((len(items), d), dtype=torch.float32, device=dev)
        for bi, it in enumerate(items):
            if it.c1 > it.c0:
                means[bi] = H[bi, it.c0 : it.c1].float().mean(0)
            lasts[bi] = H[bi, len(it.prompt_ids) - 1].float()
        means, lasts = means.cpu(), lasts.cpu()
    for bi, it in enumerate(items):
        a, e = seg[bi]
        n_c = it.c1 - it.c0
        if n_c:
            # entries a .. a+n_c-1 hold log p(content token k | prefix); a+1 .. a+n_c the
            # entropies at the content positions
            nll_sum = float(-tok_lp[a : a + n_c].sum())
            ent_mean = float(ent[a + 1 : e].mean()) if entropy else nan
        else:
            nll_sum, ent_mean = nan, nan
        rec = {"nll_sum": nll_sum, "nll_mean": nll_sum / n_c if n_c else nan, "n_content": n_c,
               "ent_mean": ent_mean}
        if hidden:
            rec["mean"] = means[bi]
            rec["last"] = lasts[bi]
        res.append(rec)
    del out, hs, final
    return res
