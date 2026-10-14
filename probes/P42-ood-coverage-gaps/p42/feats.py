"""P42 forward features (plan.md "Setup: Features"; S2 likelihoods, S3 activations, S3f SAE FVU).

One causal forward per prompt over [rendered prompt + the first `resp_tokens` response tokens]
(prompt only when the prompt has no response) gives, per prompt:
- prompt NLL over the user-content tokens (each token predicted from its whole prefix, template
  included), as a sum and a per-token mean;
- the response log-prob over the response tokens used (sum and mean);
- mean-pooled `hidden_states[L]` over the content tokens ("p") and over the response tokens ("r")
  for L in K.FEATURE_LAYERS. HF `hidden_states` index: 0 = embeddings, i = output of block i-1;
  in transformers 5.x the last entry (32) is the final-RMSNorm output (= last_hidden_state,
  checked in tests/test_feats.py on a tiny Olmo3);
- optional (S3f, exploratory): a sparse autoencoder's fraction of variance unexplained on the
  per-token residuals over the content tokens.
Causality means the "p" quantities don't see the response (tested).

Content-span rule. The chat template is rendered once with a placeholder as the user content
(`emx.formats.render_prompt("chat", PLACEHOLDER, tok)`: the template's default system prompt,
add_generation_prompt), split into a prefix and a suffix string, and every prompt's ids are
`tok(prefix) + tok(content) + tok(suffix)`, each tokenised alone with add_special_tokens=False.
So the content span is exact and its tokens equal the standalone tokenisation that `n_tokens`
counts. Tokenising the whole rendered string differs only when the content starts with a newline
(the prefix ends in "\\n" and BPE merges "\\n\\n"; checked on the OLMo-3 tokenizer, 2026-10-13);
`full_string_match` counts those cases for the run record. Generation (vLLM) is fed exactly these
ids, so features and responses share one tokenisation.

SAE (S3f). `decoderesearch/olmo-3-saes` layer 16 is a Matryoshka BatchTopK SAE saved by
SAELens 6.42 in JumpReLU inference form (cfg.json `architecture: jumprelu`; tensors W_enc
[4096, 65536], b_enc, W_dec, b_dec, threshold): encode f = ReLU(pre) * (pre > threshold) with
pre = (x - b_dec) @ W_enc + b_enc (apply_b_dec_to_input), decode x_hat = f @ W_dec + b_dec. A
`topk`/`batchtopk` config without a threshold uses per-token TopK(k). Its hook is
`model.layers.16`, the output of block 16, i.e. HF `hidden_states[17]` (`sae_hs_index`). FVU per
prompt = sum_t ||x_t - x_hat_t||^2 / sum_t ||x_t - mean_t x||^2 over that prompt's content tokens
(the mean is the prompt's own mean residual), in float32.

torch is imported at module level: this module runs in /ws/venv-peft and in the laptop tests.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import torch

PLACEHOLDER = "\x00P42_CONTENT\x00"
LOGIT_CHUNK = 4096  # positions per lm_head + log_softmax chunk (fp32: 4096 x 100k x 4 B = 1.6 GB)
SAE_CHUNK = 4096  # tokens per SAE encode/decode chunk


# --- tokenisation -------------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateParts:
    prefix: str
    suffix: str
    prefix_ids: tuple[int, ...]
    suffix_ids: tuple[int, ...]


def template_parts(tok, render) -> TemplateParts:
    """`render(content) -> str` renders one user turn (no system message, generation prompt)."""
    rendered = render(PLACEHOLDER)
    if rendered.count(PLACEHOLDER) != 1:
        raise ValueError("template must contain the user content exactly once")
    pre, suf = rendered.split(PLACEHOLDER)
    if not pre:
        raise ValueError("empty template prefix: the first content token has no context")
    ids = lambda s: tuple(tok(s, add_special_tokens=False)["input_ids"])
    return TemplateParts(pre, suf, ids(pre), ids(suf))


def encode_prompts(tok, texts: Sequence[str], parts: TemplateParts) -> list[tuple[list[int], int, int]]:
    """Per text: (prompt ids, c0, c1), content tokens at ids[c0:c1]."""
    enc = tok(list(texts), add_special_tokens=False)["input_ids"] if texts else []
    out = []
    p = list(parts.prefix_ids)
    for content in enc:
        ids = p + list(content) + list(parts.suffix_ids)
        out.append((ids, len(p), len(p) + len(content)))
    return out


def full_string_match(tok, text: str, parts: TemplateParts) -> bool:
    """Does tokenising the whole rendered string give the same ids as the three-part rule?"""
    full = tok(parts.prefix + text + parts.suffix, add_special_tokens=False)["input_ids"]
    (ids, _, _), = encode_prompts(tok, [text], parts)
    return list(full) == ids


# --- batching -----------------------------------------------------------------------------------


def pack_batches(lengths: Sequence[int], token_budget: int, max_batch: int = 64) -> list[list[int]]:
    """Length-sorted (longest first, so an OOM shows at once) batches with
    len(batch) * max_len <= token_budget (a single over-budget sequence gets its own batch)."""
    order = sorted(range(len(lengths)), key=lambda i: (-lengths[i], i))
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_max = 0
    for i in order:
        L = max(int(lengths[i]), 1)
        new_max = max(cur_max, L)
        if cur and ((len(cur) + 1) * new_max > token_budget or len(cur) >= max_batch):
            batches.append(cur)
            cur, new_max = [], L
        cur.append(i)
        cur_max = new_max
    if cur:
        batches.append(cur)
    return batches


def pad_right(seqs: Sequence[Sequence[int]], pad_id: int, device) -> tuple[torch.Tensor, torch.Tensor]:
    T = max(len(s) for s in seqs)
    ids = torch.full((len(seqs), T), pad_id, dtype=torch.long)
    mask = torch.zeros((len(seqs), T), dtype=torch.long)
    for b, s in enumerate(seqs):
        ids[b, : len(s)] = torch.tensor(list(s), dtype=torch.long)
        mask[b, : len(s)] = 1
    return ids.to(device), mask.to(device)


# --- SAE ----------------------------------------------------------------------------------------


class SAE:
    """JumpReLU (SAELens inference form) or TopK SAE; float32."""

    def __init__(self, W_enc, b_enc, W_dec, b_dec, *, threshold=None, k: int | None = None,
                 apply_b_dec_to_input: bool = True):
        if threshold is None and k is None:
            raise ValueError("SAE needs a threshold (jumprelu) or k (topk)")
        self.W_enc, self.b_enc, self.W_dec, self.b_dec = W_enc, b_enc, W_dec, b_dec
        self.threshold, self.k, self.apply_b_dec = threshold, k, apply_b_dec_to_input
        self.d_in = W_enc.shape[0]

    @classmethod
    def load(cls, sae_dir: str | Path, device, runner_cfg_key: str | None = None) -> tuple[SAE, dict]:
        from safetensors.torch import load_file

        sae_dir = Path(sae_dir)
        cfg = json.loads((sae_dir / "cfg.json").read_text())
        t = load_file(str(sae_dir / "sae_weights.safetensors"), device=str(device))
        t = {k: v.float() for k, v in t.items()}
        arch = cfg.get("architecture", "")
        k = None
        if "threshold" not in t:
            rc = sae_dir / "runner_cfg.json"
            if rc.is_file():
                rcfg = json.loads(rc.read_text())
                saes = rcfg.get("saes", {})
                sub = saes.get(runner_cfg_key) if runner_cfg_key else None
                k = (sub or {}).get("k") or cfg.get("k")
            k = k or cfg.get("k")
            if not k:
                raise ValueError(f"SAE {sae_dir}: no threshold tensor and no k (arch {arch!r})")
        sae = cls(t["W_enc"], t["b_enc"], t["W_dec"], t["b_dec"], threshold=t.get("threshold"),
                  k=k, apply_b_dec_to_input=bool(cfg.get("apply_b_dec_to_input", True)))
        info = {"architecture": arch, "encode": "jumprelu" if "threshold" in t else f"topk{k}",
                "hook_name": cfg.get("metadata", {}).get("hook_name") or cfg.get("hook_name"),
                "d_in": int(t["W_enc"].shape[0]), "d_sae": int(t["W_enc"].shape[1])}
        return sae, info

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        xin = x - self.b_dec if self.apply_b_dec else x
        pre = xin @ self.W_enc + self.b_enc
        if self.threshold is not None:
            return torch.relu(pre) * (pre > self.threshold)
        vals, idx = torch.topk(torch.relu(pre), self.k, dim=-1)
        return torch.zeros_like(pre).scatter_(-1, idx, vals)

    def decode(self, f: torch.Tensor) -> torch.Tensor:
        return f @ self.W_dec + self.b_dec

    def fvu(self, x: torch.Tensor) -> float:
        """x: [T, d_in] float32, one prompt's tokens."""
        if x.shape[0] < 2:
            return float("nan")
        err = 0.0
        for s in range(0, x.shape[0], SAE_CHUNK):
            xc = x[s : s + SAE_CHUNK]
            err += float(((xc - self.decode(self.encode(xc))) ** 2).sum())
        den = float(((x - x.mean(0, keepdim=True)) ** 2).sum())
        return err / den if den > 0 else float("nan")


def hs_index_for_hook(hook_name: str | None) -> int:
    """SAELens hook -> HF hidden_states index: `model.layers.N` (block output) -> N + 1."""
    import re

    m = re.fullmatch(r"model\.layers\.(\d+)", hook_name or "")
    if not m:
        raise ValueError(f"unsupported SAE hook {hook_name!r}")
    return int(m.group(1)) + 1


# --- hidden-state capture ---------------------------------------------------------------------


class capture_hidden:  # a context manager used like a function
    """Forward hooks that keep only the HF `hidden_states` indices in `idxs`: 0 = the embedding
    output, i in 1..N-1 = the output of block i-1, N = the final-norm output (transformers 5.x's
    `hidden_states[N]`; tests/test_feats.py checks the mapping against output_hidden_states).
    Holding 4-5 of the 33 states saves ~7 GB at 32k padded tokens."""

    def __init__(self, model, idxs):
        self.base = model.model
        self.idxs = sorted(set(idxs))
        self.hs: dict[int, torch.Tensor] = {}
        self.handles = []

    def _hook(self, i):
        def hook(module, inputs, output):
            self.hs[i] = output[0] if isinstance(output, tuple) else output
        return hook

    def __enter__(self) -> dict[int, torch.Tensor]:
        n = len(self.base.layers)
        for i in self.idxs:
            if i == 0:
                mod = self.base.embed_tokens
            elif i < n:
                mod = self.base.layers[i - 1]
            elif i == n:
                mod = self.base.norm
            else:
                raise ValueError(f"hidden_states index {i} > {n}")
            self.handles.append(mod.register_forward_hook(self._hook(i)))
        return self.hs

    def __exit__(self, *exc):
        for h in self.handles:
            h.remove()
        return False


# --- the forward --------------------------------------------------------------------------------


@dataclass
class Item:
    """One sequence: prompt ids (content at [c0, c1)), then response ids (may be empty)."""
    prompt_ids: Sequence[int]
    c0: int
    c1: int
    resp_ids: Sequence[int] = ()

    @property
    def ids(self) -> list[int]:
        return list(self.prompt_ids) + list(self.resp_ids)


def head_logits(model, h: torch.Tensor) -> torch.Tensor:
    """lm_head in float32 (a cached float32 copy of the weight; on CUDA the pod enables TF32).
    bf16 logits are spaced 0.125 apart at |logit| 16-32, too coarse for the SFT-minus-DPO
    log-likelihood differences (S2b/c)."""
    head = model.get_output_embeddings()
    w = getattr(head, "_p42_w32", None)
    if w is None or w.device != head.weight.device:
        w = head.weight.detach().float()
        head._p42_w32 = w
    b = head.bias.float() if getattr(head, "bias", None) is not None else None
    return torch.nn.functional.linear(h.float(), w, b)


def token_logprobs(model, final: torch.Tensor, ids: torch.Tensor,
                   positions: list[tuple[int, int]]) -> torch.Tensor:
    """log p(ids[b, t] | ids[b, <t]) for every (b, t) in `positions` (t >= 1), float32."""
    if not positions:
        return torch.zeros(0, dtype=torch.float32, device=final.device)
    b = torch.tensor([p[0] for p in positions], device=final.device)
    t = torch.tensor([p[1] for p in positions], device=final.device)
    out = []
    for s in range(0, len(positions), LOGIT_CHUNK):
        bb, tt = b[s : s + LOGIT_CHUNK], t[s : s + LOGIT_CHUNK]
        logits = head_logits(model, final[bb, tt - 1])
        lp = torch.log_softmax(logits, dim=-1)
        out.append(lp.gather(-1, ids[bb, tt].unsqueeze(-1)).squeeze(-1))
        del logits, lp
    return torch.cat(out)


@torch.no_grad()
def forward_batch(model, items: Sequence[Item], layers: Sequence[int], pad_id: int, *,
                  sae: SAE | None = None, sae_hs_index: int | None = None) -> list[dict]:
    """One padded forward. Per item: nll_sum, nll_mean, n_content, resp_lp_sum, resp_lp_mean,
    n_resp, fvu, p[L], r[L] (float32 CPU tensors; NaN where empty)."""
    dev = next(model.parameters()).device
    ids, mask = pad_right([it.ids for it in items], pad_id, dev)
    need = set(layers) | ({sae_hs_index} if sae is not None else set())
    with capture_hidden(model, need) as hs:
        out = model.model(input_ids=ids, attention_mask=mask, use_cache=False)
    final = out.last_hidden_state
    pos: list[tuple[int, int]] = []
    seg: list[tuple[int, int, int, int]] = []  # per item: content [a, b) and response [c, d) in pos
    for bi, it in enumerate(items):
        a = len(pos)
        pos += [(bi, t) for t in range(it.c0, it.c1)]
        c = len(pos)
        r0 = len(it.prompt_ids)
        pos += [(bi, t) for t in range(r0, r0 + len(it.resp_ids))]
        seg.append((a, c, c, len(pos)))
    lp = token_logprobs(model, final, ids, pos).double().cpu()
    nan = float("nan")
    # mean pools on the GPU, one transfer per layer and window
    B, d = len(items), final.shape[-1]
    pooled: dict[tuple[str, int], torch.Tensor] = {}
    for L in layers:
        for w in ("p", "r"):
            pooled[(w, L)] = torch.full((B, d), nan, dtype=torch.float32, device=dev)
        for bi, it in enumerate(items):
            r0, n_r = len(it.prompt_ids), len(it.resp_ids)
            if it.c1 > it.c0:
                pooled[("p", L)][bi] = hs[L][bi, it.c0 : it.c1].float().mean(0)
            if n_r:
                pooled[("r", L)][bi] = hs[L][bi, r0 : r0 + n_r].float().mean(0)
    pooled = {k: v.cpu() for k, v in pooled.items()}
    res = []
    for bi, it in enumerate(items):
        a, c, c2, e = seg[bi]
        n_c, n_r = c - a, e - c2
        nll_sum = float(-lp[a:c].sum()) if n_c else nan
        r_sum = float(lp[c2:e].sum()) if n_r else nan
        rec = {"nll_sum": nll_sum, "nll_mean": nll_sum / n_c if n_c else nan, "n_content": n_c,
               "resp_lp_sum": r_sum, "resp_lp_mean": r_sum / n_r if n_r else nan, "n_resp": n_r,
               "fvu": nan,
               "p": {L: pooled[("p", L)][bi] for L in layers},
               "r": {L: pooled[("r", L)][bi] for L in layers}}
        if sae is not None and n_c:
            rec["fvu"] = sae.fvu(hs[sae_hs_index][bi, it.c0 : it.c1].float())
        res.append(rec)
    del out, hs, final
    return res


def finite(x: float) -> bool:
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))
