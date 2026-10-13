"""P05 model runners: one-pass loop sweep for the Raven-style recurrent Llama, the static baseline.

Plan: probes/P05-looped-serial-depth/plan.md ("Loop knob and state", "Scoring", "Per-loop
readability"). The recurrent runner re-implements `RavenForCausalLM.forward` from
`raven_modeling_minimal.py` @ a5f6f126 (a copy is in ../sources/) with the model's own modules:
prelude -> r x (adapter(cat(state, prelude out)) -> core block) -> coda -> ln_f -> lm_head. With
no KV cache the state after t steps doesn't depend on later steps, so one pass to max(r) is
decoded at every checkpoint t (`smoke_one_pass` checks this against `model(num_steps=t)`).

Batches are right-padded: attention is causal with no mask, so pads (after the real tokens) never
reach a real position. Each row's initial state is drawn from a CPU generator keyed by
(seed, item_id), so results don't depend on batching.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
from dataclasses import dataclass, field

import torch

REC32 = ("smcleish/Recurrent-Llama-3.2-train-recurrence-32",
         "a5f6f126e9d9302445346844f1d7d29d111a06eb")
REC4 = ("smcleish/Recurrent-Llama-3.2-train-recurrence-4",
        "c466903f9ab35d827438d6a3fffcca95c47a5c1e")
BASE = ("smcleish/Llama-3.2-non-recurrent-posttrained",
        "004e96ace360c1a37c08f4555227136e845087b3")
MODELS = {"rec32": REC32, "rec4": REC4, "base": BASE}
CHECKPOINTS = {"rec32": (1, 2, 4, 8, 16, 32, 64), "rec4": (1, 2, 4, 8, 16), "base": (0,)}
N_CAND = 20  # candidate values 1..20
STATE_LEN = 1024  # init-state rows drawn per item (block_size); prompts + generation fit in it


@dataclass
class Batch:
    """One right-padded batch. `score_pos[i]` are the positions whose next-token prediction is
    compared with `gold[i]`; `readout_pos[i]` is the position (or -1) where the 20 candidate
    logits are read."""
    ids: torch.Tensor                 # [B, S] long
    lengths: list[int]
    item_ids: list[str]
    score_pos: list[list[int]]
    gold: list[list[int]]
    readout_pos: list[int] = field(default_factory=list)


def make_batch(tok, prompts: list[str], conts: list[str], item_ids: list[str],
               readout: list[bool], pad_id: int) -> Batch:
    """Teacher-forced inputs: tokens of prompt + continuation, minus the last token. Gold
    continuation tokens are read off the full string's tokenization, and the prompt's
    tokenization must be its prefix (asserted)."""
    rows, score_pos, gold, ro = [], [], [], []
    for p, c, want in zip(prompts, conts, readout):
        p_ids = tok(p, add_special_tokens=True)["input_ids"]
        f_ids = tok(p + c, add_special_tokens=True)["input_ids"]
        if f_ids[: len(p_ids)] != p_ids or len(f_ids) <= len(p_ids):
            raise ValueError(f"prompt tokens are not a prefix of prompt+gold: {c!r}")
        g = f_ids[len(p_ids):]
        rows.append(f_ids[:-1])
        score_pos.append([len(p_ids) - 1 + i for i in range(len(g))])
        gold.append(g)
        # chain: the gold is [' ', '<v>']; the readout reads after the ' ' token
        ro.append(len(p_ids) if want and len(g) == 2 else -1)
    s = max(len(r) for r in rows)
    ids = torch.full((len(rows), s), pad_id, dtype=torch.long)
    for i, r in enumerate(rows):
        ids[i, : len(r)] = torch.tensor(r)
    return Batch(ids, [len(r) for r in rows], list(item_ids), score_pos, gold, ro)


def row_seed(seed: int, item_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}|{item_id}".encode()).digest()[:8], "little")


def init_states(model, item_ids: list[str], seq_len: int, seed: int, device, dtype) -> torch.Tensor:
    """The model's `initialize_state` (trunc_normal, std init_values.std, +-3 std), per row from
    a CPU generator keyed by (seed, item_id); a row's state for a shorter sequence is a prefix."""
    if seq_len > STATE_LEN:
        raise ValueError(f"sequence of {seq_len} tokens > STATE_LEN {STATE_LEN}")
    std = model.config.init_values["std"]
    emb_scale = model.config.init_values.get("embed_scale", 1.0)
    out = torch.empty(len(item_ids), seq_len, model.config.n_embd)
    for i, iid in enumerate(item_ids):
        g = torch.Generator().manual_seed(row_seed(seed, iid))
        x = torch.empty(STATE_LEN, model.config.n_embd)  # fixed length, then sliced: prefix-stable
        torch.nn.init.trunc_normal_(x, mean=0.0, std=std, a=-3 * std, b=3 * std, generator=g)
        out[i] = x[:seq_len] * emb_scale
    return out.to(device=device, dtype=dtype)


def autocast_ctx(device: torch.device, bf16: bool):
    if not bf16:
        return contextlib.nullcontext()
    return torch.autocast(device_type=device.type, dtype=torch.bfloat16)


def _gather(h: torch.Tensor, pos: list[list[int]]) -> torch.Tensor:
    """[B, S, E] -> [B, P, E] at per-row positions (padded with the row's last position)."""
    p = max(len(x) for x in pos)
    idx = torch.tensor([x + [x[-1]] * (p - len(x)) for x in pos], device=h.device)
    return h.gather(1, idx[..., None].expand(-1, -1, h.shape[-1]))


@dataclass
class SweepOut:
    """Per checkpoint t: argmax token ids at the score positions [B, P] (list of lists, gold
    length per row). Readout: per loop t in `readout_loops`, candidate logits [B, L, 20] for
    lenses ("coda", "logit"); rows without a readout position hold NaN."""
    pred: dict[int, list[list[int]]]
    cand: torch.Tensor | None          # [B, T, L, 20] float32 on CPU
    readout_loops: list[int]
    lenses: list[str]
    # Raven only, per checkpoint t: ||s_t - s_{t-1}|| / ||s_t|| at each row's first score
    # position (convergence to a fixed point; lit review, Blayney et al. 2026)
    dstate: dict[int, list[float]] = field(default_factory=dict)


@torch.no_grad()
def raven_sweep(model, batch: Batch, checkpoints: tuple[int, ...], seed: int, cand_ids: list[int],
                readout_loops: tuple[int, ...] = (), bf16: bool = False) -> SweepOut:
    """One pass to max(checkpoints, readout_loops); coda + ln_f + head decode at each checkpoint;
    coda-lens and plain-logit-lens candidate logits at each readout loop for rows with a readout
    position."""
    tr = model.transformer
    dev = next(model.parameters()).device
    ids = batch.ids.to(dev)
    b, s = ids.shape
    t_max = max(list(checkpoints) + list(readout_loops))
    ro_rows = [i for i, p in enumerate(batch.readout_pos) if p >= 0]
    lenses = ["coda", "logit"]
    cand = torch.full((b, len(readout_loops), 2, N_CAND), float("nan"))
    cand_t = torch.tensor(cand_ids, device=dev)
    pred: dict[int, list[list[int]]] = {}
    with autocast_ctx(dev, bf16):
        pos_ids = torch.arange(s, device=dev).unsqueeze(0)
        emb = tr.wte(ids)
        if model.emb_scale != 1:
            emb = emb * model.emb_scale
        freqs = model.rotary_emb(emb, pos_ids)
        block_idx = torch.tensor(-1, dtype=torch.long)
        for blk in tr.prelude:
            block_idx += 1
            emb = blk(emb, freqs, block_idx, None, None)
        x = init_states(model, batch.item_ids, s, seed, dev, emb.dtype)
        rpos = [[batch.readout_pos[i]] for i in ro_rows]

        p0 = torch.tensor([sp[0] for sp in batch.score_pos], device=dev)
        rows_t = torch.arange(b, device=dev)
        dstate: dict[int, list[float]] = {}
        for t in range(1, t_max + 1):
            x_prev = x[rows_t, p0]
            x, block_idx = model.core_block_forward(x, emb, freqs, None, None, block_idx, t - 1)
            if t in checkpoints:
                cur = x[rows_t, p0].float()
                dstate[t] = ((cur - x_prev.float()).norm(dim=-1)
                             / cur.norm(dim=-1).clamp_min(1e-12)).cpu().tolist()
            if t in checkpoints:
                h = _gather(coda_head_rows(model, x, freqs), batch.score_pos)
                am = model.lm_head(h).float().argmax(-1).cpu().tolist()
                pred[t] = [am[i][: len(batch.gold[i])] for i in range(b)]
            if t in readout_loops and ro_rows:
                ti = readout_loops.index(t)
                sub = x[ro_rows]
                hc = _gather(coda_head_rows(model, sub, freqs), rpos)[:, 0]
                hl = _gather(tr.ln_f(sub), rpos)[:, 0]
                for li, hh in enumerate((hc, hl)):
                    lg = model.lm_head(hh).float()[:, cand_t].cpu()
                    cand[ro_rows, ti, li] = lg
    return SweepOut(pred, cand if readout_loops else None, list(readout_loops), lenses, dstate)


def coda_head_rows(model, h: torch.Tensor, freqs) -> torch.Tensor:
    cidx = torch.tensor(0, dtype=torch.long)
    for blk in model.transformer.coda:
        cidx -= 1
        h = blk(h, freqs, cidx, None, None)
    return model.transformer.ln_f(h)


@torch.no_grad()
def base_sweep(model, batch: Batch, cand_ids: list[int], readout: bool = True,
               bf16: bool = False) -> SweepOut:
    """The static Llama: final-layer argmax at the score positions (key 0), and a layer-wise
    logit lens (final norm + head on each layer's residual, layers 1..L) at the readout
    position. Readout loops are layer indices."""
    dev = next(model.parameters()).device
    ids = batch.ids.to(dev)
    b = ids.shape[0]
    ro_rows = [i for i, p in enumerate(batch.readout_pos) if p >= 0]
    n_layers = model.config.num_hidden_layers
    cand = torch.full((b, n_layers, 1, N_CAND), float("nan"))
    cand_t = torch.tensor(cand_ids, device=dev)
    with autocast_ctx(dev, bf16):
        out = model.model(input_ids=ids, output_hidden_states=readout and bool(ro_rows))
        h = _gather(out.last_hidden_state, batch.score_pos)
        am = model.lm_head(h).float().argmax(-1).cpu().tolist()
        pred = {0: [am[i][: len(batch.gold[i])] for i in range(b)]}
        if readout and ro_rows:
            rpos = [[batch.readout_pos[i]] for i in ro_rows]
            for li in range(n_layers):
                hs = out.hidden_states[li + 1][ro_rows]
                if li < n_layers - 1:  # the last hidden state already has the final norm
                    hs = model.model.norm(hs)
                hh = _gather(hs, rpos)[:, 0]
                cand[ro_rows, li, 0] = model.lm_head(hh).float()[:, cand_t].cpu()
    return SweepOut(pred, cand if readout else None, list(range(1, n_layers + 1)), ["logit"])


@torch.no_grad()
def greedy(model, tok, prompts: list[str], item_ids: list[str], max_new: int, *,
           num_steps: int | None, seed: int, pad_id: int, bf16: bool = False) -> list[str]:
    """Greedy decoding without a KV cache (each step is a full forward), right-padded. Raven
    models get num_steps and, per row, the same (seed, item_id) initial state as the sweep."""
    dev = next(model.parameters()).device
    rows = [tok(p, add_special_tokens=True)["input_ids"] for p in prompts]
    lens = [len(r) for r in rows]
    s = max(lens) + max_new
    ids = torch.full((len(rows), s), pad_id, dtype=torch.long)
    for i, r in enumerate(rows):
        ids[i, : len(r)] = torch.tensor(r)
    ids = ids.to(dev)
    new = [[] for _ in rows]
    raven = num_steps is not None
    states = init_states(model, item_ids, s, seed, dev, torch.float32) if raven else None
    with autocast_ctx(dev, bf16):
        for _ in range(max_new):
            cur = max(lens)
            if raven:
                out = model(ids[:, :cur], input_states=states[:, :cur].to(
                    next(model.parameters()).dtype), num_steps=num_steps)
                logits = out.logits
            else:
                logits = model(input_ids=ids[:, :cur]).logits
            nxt = logits[torch.arange(len(rows)), torch.tensor(lens) - 1].float().argmax(-1)
            for i, t in enumerate(nxt.tolist()):
                new[i].append(t)
                ids[i, lens[i]] = t
                lens[i] += 1
    return [tok.decode(n) for n in new]


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def plan_batches(lengths: list[int], token_budget: int) -> list[list[int]]:
    """Indices sorted by length, packed so (rows x longest row) <= token_budget."""
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    out, cur, longest = [], [], 0
    for i in order:
        nl = max(longest, lengths[i])
        if cur and nl * (len(cur) + 1) > token_budget:
            out.append(cur)
            cur, nl = [], lengths[i]
        cur.append(i)
        longest = nl
    if cur:
        out.append(cur)
    return out


def effective_layers(model_key: str, r: int) -> int:
    return 16 if model_key == "base" else 4 + 6 * r + 4


def flops_per_token(model_key: str, r: int, layer_params: float = 60.8e6) -> float:
    return 2 * effective_layers(model_key, r) * layer_params


def cand_token_ids(tok) -> list[int]:
    """The single token for each value 1..20 after 'Answer: ' (asserted distinct and single)."""
    base = tok("Answer:", add_special_tokens=False)["input_ids"]
    out = []
    for v in range(1, N_CAND + 1):
        ids = tok(f"Answer: {v}", add_special_tokens=False)["input_ids"]
        assert ids[: len(base)] == base and len(ids) == len(base) + 2, (v, ids)
        out.append(ids[-1])
    assert len(set(out)) == N_CAND
    return out


def is_finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))
