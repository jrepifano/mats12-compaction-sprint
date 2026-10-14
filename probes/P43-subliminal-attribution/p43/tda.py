"""Per-row training-data attribution over the LoRA parameters of a PEFT model (plan.md "Scorers").

Every score is oriented so that HIGHER = a gradient step on the training row lowers the query loss,
i.e. +g_q^T H^-1 g_row with H the (damped) curvature, or the plain dot / cosine. Losses are the
SUM of token cross-entropies over a row's labelled tokens (next-token shift); a query's gradient
is the gradient of the summed loss over that query's rows.

Methods (all over every LoRA matrix the model trains; `score_rows` returns them all):
- `graddot:{q}` / `gradcos:{q}`: exact dot / cosine of the full per-row and query gradients.
- `pfi:{q}:{damp}`: projected block-diagonal empirical-Fisher influence (LoGra-style), NOT EK-FAC.
  Each LoRA matrix's per-row gradient is projected by fixed seeded Gaussians on its large side
  (lora_A grad r x d_in -> r x p via P_in (p x d_in); lora_B grad d_out x r -> p x r via P_out;
  entries N(0, 1/p)); per block b, F_b = mean_i v_ib v_ib^T over the scored rows, damped by
  damp x mean eigenvalue of F_b (= trace / dim), score = sum_b v_ib^T (F_b + lam_b I)^-1 q_b.
- `ekfac:{q}:{damp}`: EK-FAC influence (George et al. 2018; Grosse et al. 2023), own
  implementation over each lora_A / lora_B Linear: Kronecker factors are token covariances of the
  module input (A) and output gradient (S) over real tokens, eigendecomposed (fp64); the
  eigenvalues are refit as lambda = mean_rows (Q_S^T G_row Q_A)^2 (per-row gradients); damping is
  damp x mean(lambda) per module (0.1 = kronfluence's default heuristic). Generalises
  `ekfac_module_scores` / `ekfac_lambda_refit` of ../mats12_neel_working/src/em_filter/tda.py
  (rank-1, one module) to rank r on every module. A factor whose dimension exceeds
  `max_factor_dim` (Llama-3.1-8B: the 14,336-wide down_proj input and gate/up outputs) keeps the
  identity as its eigenbasis (the lambda refit still runs, so that side is diagonal); the full
  14,336^2 factors would need ~79 GB of fp32 accumulators plus ~96 eigendecompositions of that
  size, which does not fit next to the model in one pass. meta lists which sides were identity.
  Covariances use each row's real tokens except its last (Bergson's convention). Bergson
  (p43/tda_bergson.py) is an optional independent cross-check: on the tiny test model its EK-FAC
  and this one give preconditioned vectors with cosine >= 0.999997 per module (2026-10-13).

Per-row gradients are exact and come from ONE batched forward/backward per batch: forward hooks
on each lora_A / lora_B Linear keep its input and register a tensor hook on its output; the
output gradient then gives G_b = sum_t g_bt a_bt^T (einsum over token positions). Rows are
right-padded and padded positions are masked, so padding contributes nothing; the model must be in
eval mode (LoRA dropout is the identity). The batch plan and the "batch sum of per-row gradients
equals autograd's batch gradient" check are ported from ../mats12_neel_working/src/em_filter/
tda_pod.py (`batch_plan`) and scripts/tda_grads.py (in-run invariant). Nothing of size
n_rows x n_params is ever materialised: query vectors stay on the device and per-row results are
dot products, plus the projected vectors (n_rows x ~57K fp32 for Llama-8B r=8, ~2.3 GB) on CPU.
"""

from __future__ import annotations

import math
import platform
import time
from dataclasses import dataclass

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

IGNORE = -100
DEFAULT_MAX_FACTOR_DIM = 4096


# --- LoRA module discovery --------------------------------------------------------------------


@dataclass
class LoraMod:
    """One LoRA matrix (a lora_A or lora_B Linear) of one wrapped projection."""

    key: str  # e.g. "base_model.model.model.layers.0.self_attn.q_proj.lora_A"
    site: str  # the wrapper module's name
    kind: str  # "A" or "B"
    lin: nn.Linear
    out_dim: int
    in_dim: int

    @property
    def numel(self) -> int:
        return self.out_dim * self.in_dim


def lora_mods(model) -> tuple[list[LoraMod], dict[str, nn.Module]]:
    """All trainable lora_A / lora_B Linears in module order (A then B per site), plus wrappers."""
    mods: list[LoraMod] = []
    wrappers: dict[str, nn.Module] = {}
    for name, mod in model.named_modules():
        la, lb = getattr(mod, "lora_A", None), getattr(mod, "lora_B", None)
        if not (isinstance(la, nn.ModuleDict) and isinstance(lb, nn.ModuleDict) and len(la)):
            continue
        active = list(getattr(mod, "active_adapters", ["default"]))
        if len(active) != 1:
            raise ValueError(f"{name}: exactly one active adapter supported, got {active}")
        ad = active[0]
        if ad in getattr(mod, "lora_variant", {}):
            raise ValueError(f"{name}: LoRA variants (DoRA etc.) are not supported")
        if getattr(mod, "merged", False):
            raise ValueError(f"{name}: adapter is merged")
        A, B = la[ad], lb[ad]
        if not (isinstance(A, nn.Linear) and isinstance(B, nn.Linear)):
            raise TypeError(f"{name}: lora_A/lora_B must be nn.Linear")
        if A.bias is not None or B.bias is not None:
            raise ValueError(f"{name}: LoRA bias not supported")
        if not (A.weight.requires_grad and B.weight.requires_grad):
            continue
        wrappers[name] = mod
        mods.append(LoraMod(f"{name}.lora_A", name, "A", A, A.out_features, A.in_features))
        mods.append(LoraMod(f"{name}.lora_B", name, "B", B, B.out_features, B.in_features))
    if not mods:
        raise ValueError("no trainable LoRA modules found")
    return mods, wrappers


def _check_eval(model) -> None:
    if getattr(model, "is_gradient_checkpointing", False):
        # recomputation would re-fire the forward hooks; per-row gradients need one forward
        raise ValueError("disable gradient checkpointing before attribution")
    if model.training:
        raise ValueError("model must be in eval mode (LoRA dropout must be the identity)")
    for name, m in model.named_modules():
        if isinstance(m, nn.Dropout) and m.training:
            raise ValueError(f"dropout {name} is in training mode")


def _trainable_check(model, mods: list[LoraMod]) -> None:
    lora_ids = {id(m.lin.weight) for m in mods}
    extra = [n for n, p in model.named_parameters() if p.requires_grad and id(p) not in lora_ids]
    if extra:
        raise ValueError(f"non-LoRA parameters require grad: {extra[:5]}")


# --- batching ---------------------------------------------------------------------------------


def batch_plan(lengths: list[int], max_rows: int, max_tokens: int | None) -> list[list[int]]:
    """Deterministic greedy packing, longest rows first (port of em_filter.tda_pod.batch_plan).

    max_tokens bounds rows_in_batch x longest_row_len (the padded footprint); None = no bound.
    """
    order = sorted(range(len(lengths)), key=lambda i: (-lengths[i], i))
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_max = 0
    for i in order:
        n = lengths[i]
        new_max = max(cur_max, n)
        over_tokens = max_tokens is not None and new_max * (len(cur) + 1) > max_tokens
        if cur and (len(cur) + 1 > max_rows or over_tokens):
            batches.append(cur)
            cur, new_max = [], n
        cur.append(i)
        cur_max = new_max
    if cur:
        batches.append(cur)
    return batches


def collate(rows: list[dict], device, pad_id: int = 0):
    """Right-padded input_ids / attention_mask / labels (labels -100 on padding)."""
    width = max(len(r["input_ids"]) for r in rows)
    ids = torch.full((len(rows), width), pad_id, dtype=torch.long)
    attn = torch.zeros((len(rows), width), dtype=torch.long)
    labels = torch.full((len(rows), width), IGNORE, dtype=torch.long)
    for b, r in enumerate(rows):
        n = len(r["input_ids"])
        if len(r["labels"]) != n:
            raise ValueError("input_ids and labels differ in length")
        ids[b, :n] = torch.tensor(r["input_ids"], dtype=torch.long)
        attn[b, :n] = 1
        labels[b, :n] = torch.tensor(r["labels"], dtype=torch.long)
    return ids.to(device), attn.to(device), labels.to(device)


def row_losses(model, ids, attn, labels):
    """Per-row SUM of next-token cross-entropies over labelled tokens, fp32, shape (B,)."""
    logits = model(input_ids=ids, attention_mask=attn, use_cache=False).logits
    out = []
    for b in range(ids.shape[0]):  # row-chunked fp32 cast (em_filter.tda_pod.per_example_loss)
        lg = logits[b, :-1, :].float()
        out.append(F.cross_entropy(lg, labels[b, 1:], ignore_index=IGNORE, reduction="sum"))
    return torch.stack(out)


# --- the gradient tap -------------------------------------------------------------------------


class _Tap:
    """Hooks every LoRA Linear; on each module's output gradient calls
    consumer(i, G (B,o,i) fp32, a (B,T,i) fp32 masked, g (B,T,o) fp32 masked, cm (B,T,1)),
    where cm masks the token positions used for the EK-FAC covariances: real tokens except each
    row's last, whose output feeds no loss (Bergson's "gradient-carrying positions")."""

    def __init__(self, mods: list[LoraMod], consumer):
        self.sums: list[torch.Tensor] | None = None
        self.mods = mods
        self.consumer = consumer
        self.mask: torch.Tensor | None = None  # (B, T) float
        self.cov_mask: torch.Tensor | None = None  # (B, T) float
        self.inputs: dict[int, torch.Tensor] = {}
        self.handles = []
        self.calls = 0

    def __enter__(self):
        for i, m in enumerate(self.mods):
            self.handles.append(m.lin.register_forward_hook(self._fwd(i)))
        return self

    def __exit__(self, *exc):
        for h in self.handles:
            h.remove()
        self.handles.clear()
        self.inputs.clear()

    def _fwd(self, i: int):
        def hook(module, inp, out):
            if not torch.is_grad_enabled():
                return
            self.inputs[i] = inp[0]
            out.register_hook(self._bwd(i))

        return hook

    def _bwd(self, i: int):
        def hook(grad):
            with torch.no_grad():
                m = self.mask[..., None]
                a = self.inputs.pop(i).float() * m
                g = grad.float() * m
                G = torch.einsum("bto,bti->boi", g, a)
                self.consumer(i, G, a, g, self.cov_mask[..., None])
                self.calls += 1

        return hook


def _lora_params(mods: list[LoraMod]) -> list[torch.Tensor]:
    return [m.lin.weight for m in mods]


def _run_batch(model, mods, tap: _Tap, rows: list[dict], device, check: list | None = None):
    """One forward/backward over a batch with the tap armed. Returns per-row losses (B,).

    With `check` a list, appends the max relative error between the summed per-row gradients
    (collected by the consumer into tap.sums) and autograd's batch gradient.
    """
    ids, attn, labels = collate(rows, device)
    tap.mask = attn.to(torch.float32)
    last = attn.sum(1) - 1
    tap.cov_mask = tap.mask.clone()
    tap.cov_mask[torch.arange(len(rows), device=attn.device), last] = 0.0
    tap.calls = 0
    losses = row_losses(model, ids, attn, labels)
    grads = torch.autograd.grad(losses.sum(), _lora_params(mods))
    if tap.calls != len(mods) or tap.inputs:
        raise RuntimeError(f"gradient tap saw {tap.calls} of {len(mods)} LoRA modules")
    if check is not None:
        num = max(float((tap.sums[i] - grads[i].float()).abs().max()) for i in range(len(mods)))
        den = max(float(g.float().abs().max()) for g in grads)
        check.append(num / max(den, 1e-30))
    return losses.detach()


def per_sample_lora_grads(model, batch_rows: list[dict], device=None):
    """Exact per-row LoRA gradients for one batch in ONE forward/backward.

    Returns (grads, losses, keys): grads is a list (module order of `lora_mods`) of (B, o, i)
    fp32 tensors, each row's gradient of its own summed token loss; losses is (B,) fp32.
    """
    mods, _ = lora_mods(model)
    _check_eval(model)
    device = device or mods[0].lin.weight.device
    out: dict[int, torch.Tensor] = {}

    def consumer(i, G, a, g, cm):
        out[i] = G.clone()

    with _Tap(mods, consumer) as tap:
        losses = _run_batch(model, mods, tap, batch_rows, device)
    return [out[i] for i in range(len(mods))], losses, [m.key for m in mods]


def check_per_sample(model, rows: list[dict], *, log=print, rel_tol: float = 1e-3,
                     min_cos: float = 0.999) -> dict:
    """Smoke gate (plan.md "Automatic rules"): per-row gradients of one padded batch against
    (a) autograd's gradient of the same batch's summed loss (exact up to fp32 summation order:
    max relative error < rel_tol) and (b) single-row autograd without padding. In bf16 the
    batch shape changes the base model's numerics, so (b) is gated on the cosine (> min_cos)
    and its relative error is reported only."""
    model.eval()
    _check_eval(model)
    mods, _ = lora_mods(model)
    device = mods[0].lin.weight.device
    per: dict[int, torch.Tensor] = {}
    sums = [torch.zeros(m.out_dim, m.in_dim, device=device) for m in mods]
    check: list[float] = []

    def consumer(i, G, a, g, cm):
        per[i] = G.clone()
        sums[i] += G.sum(0)

    with _Tap(mods, consumer) as tap:
        tap.sums = sums
        losses = _run_batch(model, mods, tap, rows, device, check)
    rel, cos, dloss = [], [], []
    for b, r in enumerate(rows):
        ids, attn, labels = collate([r], device)
        loss = row_losses(model, ids, attn, labels).sum()
        ref = torch.autograd.grad(loss, _lora_params(mods))
        x = torch.cat([per[i][b].flatten().double() for i in range(len(mods))])
        y = torch.cat([g.float().flatten().double() for g in ref])
        rel.append(float((x - y).abs().max() / y.abs().max().clamp_min(1e-300)))
        cos.append(float(x @ y / (x.norm() * y.norm()).clamp_min(1e-300)))
        dloss.append(abs(float(losses[b]) - float(loss.detach())))
    out = {"n_rows": len(rows), "batch_sum_rel_err": check[0], "single_row_max_rel_err":
           max(rel), "single_row_min_cos": min(cos), "max_abs_loss_diff": max(dloss),
           "rel_tol": rel_tol, "min_cos": min_cos}
    out["ok"] = bool(check[0] < rel_tol and min(cos) > min_cos)
    log(f"[tda] per-row gradient check: {out}")
    return out


def projected_seconds(meta: dict, n_rows: int) -> float:
    """Projected score_rows wall-clock for n_rows from a smaller run's meta: the per-row passes
    scale with the row count; query gradients and the EK-FAC eigendecompositions do not."""
    t = meta["timings_s"]
    per_row = sum(t.get(k, 0.0) for k in ("pass1_grads", "pass2_ekfac_lambda",
                                          "pass3_ekfac_scores", "pfi"))
    fixed = sum(t.get(k, 0.0) for k in ("query_grads", "ekfac_eigh"))
    return fixed + per_row * n_rows / max(meta["n_rows"], 1)


# --- projections (pfi) -----------------------------------------------------------------------


def make_projections(mods: list[LoraMod], proj_dim: int, seed: int, device):
    """Per module, a fixed Gaussian (p x big_dim) with N(0, 1/p) entries on its large side
    (lora_A: input side; lora_B: output side), or None when that side is <= p (kept exact).
    Module j's generator is seeded with seed + j (module order), so it is deterministic."""
    out = []
    for j, m in enumerate(mods):
        big = m.in_dim if m.kind == "A" else m.out_dim
        if big <= proj_dim:
            out.append(None)
            continue
        gen = torch.Generator(device="cpu").manual_seed(seed + j)
        P = torch.randn(proj_dim, big, generator=gen, dtype=torch.float32) / math.sqrt(proj_dim)
        out.append(P.to(device))
    return out


def project(m: LoraMod, P: torch.Tensor | None, G: torch.Tensor) -> torch.Tensor:
    """G (..., o, i) -> flattened projected block (..., dim)."""
    if P is not None:
        G = G @ P.T if m.kind == "A" else P @ G
    return G.flatten(-2)


def proj_dims(mods: list[LoraMod], projs) -> list[int]:
    dims = []
    for m, P in zip(mods, projs, strict=True):
        if P is None:
            dims.append(m.numel)
        else:
            dims.append(m.out_dim * P.shape[0] if m.kind == "A" else P.shape[0] * m.in_dim)
    return dims


def block_fisher_scores(V: torch.Tensor, Q: torch.Tensor, dims: list[int], dampings,
                        device) -> dict[float, torch.Tensor]:
    """pfi scores. V (N, D) projected row vectors (CPU), Q (K, D) projected queries.
    Returns {damp: (N, K) fp64}: sum_b V_b (F_b + lam_b I)^-1 Q_b^T, F_b = V_b^T V_b / N,
    lam_b = damp * trace(F_b) / dim_b."""
    N = V.shape[0]
    V = V.to(device)  # one transfer (~2.3 GB for Llama-8B r=8 at 10K rows), then slice there
    out = {d: torch.zeros(N, Q.shape[0], dtype=torch.float64, device=device) for d in dampings}
    off = 0
    for dim in dims:
        Vb = V[:, off:off + dim].to(device, torch.float64)
        Qb = Q[:, off:off + dim].to(device, torch.float64)
        Fb = Vb.T @ Vb / N
        mean_eig = float(torch.diagonal(Fb).mean())
        eye = torch.eye(dim, dtype=torch.float64, device=device)
        for d in dampings:
            lam = d * mean_eig
            if lam <= 0:  # an all-zero block contributes nothing
                continue
            X = torch.linalg.solve(Fb + lam * eye, Qb.T)  # (dim, K)
            out[d] += Vb @ X
        off += dim
    return out


# --- EK-FAC ------------------------------------------------------------------------------------


def _rotate(G, Qg, Qa):
    if Qg is not None:
        G = Qg.T @ G
    if Qa is not None:
        G = G @ Qa
    return G


def _unrotate(X, Qg, Qa):
    if Qg is not None:
        X = Qg @ X
    if Qa is not None:
        X = X @ Qa.T
    return X


def ekfac_precondition(q: torch.Tensor, Qg, Qa, lam_eig: torch.Tensor, damping: float):
    """EK-FAC (H + lam I)^-1 q for one module: Q_S [(Q_S^T q Q_A) / (Lambda + lam)] Q_A^T,
    lam = damping * mean(Lambda) (port of em_filter.tda.ekfac_module_scores, rank r)."""
    lam = damping * float(lam_eig.mean())
    return _unrotate(_rotate(q, Qg, Qa) / (lam_eig + lam), Qg, Qa)


def _shared_inputs(model, mods, wrappers, rows, device) -> dict[int, int]:
    """lora_A index -> index of the first lora_A whose wrapper sees the very same input tensor
    (q/k/v; gate/up), found with one no-grad forward on `rows`. Those share one input factor.
    The inputs are held until grouping is done, so no tensor id can be reused meanwhile."""
    seen: dict[str, torch.Tensor] = {}

    def pre_hook(name):
        def h(mod, args):
            seen[name] = args[0]
        return h

    hs = [w.register_forward_pre_hook(pre_hook(n)) for n, w in wrappers.items()]
    try:
        with torch.no_grad():
            ids, attn, _ = collate(rows, device)
            model(input_ids=ids, attention_mask=attn, use_cache=False)
    finally:
        for h in hs:
            h.remove()
    leader: dict[int, int] = {}
    share = {i: leader.setdefault(id(seen[m.site]), i) for i, m in enumerate(mods)
             if m.kind == "A"}
    seen.clear()
    return share


def _alloc_cov(mods, share, max_factor_dim, device, only: set[str] | None = None):
    """Zero covariance accumulators. A_cov is keyed by the sharing leader (share[i]) of each
    lora_A input factor; `only` (module keys) restricts which modules get factors."""
    A_cov: dict[int, torch.Tensor] = {}
    S_cov: dict[int, torch.Tensor] = {}
    for i, m in enumerate(mods):
        if only is not None and m.key not in only:
            continue
        if m.kind == "A" and m.in_dim <= max_factor_dim and share[i] not in A_cov:
            A_cov[share[i]] = torch.zeros(m.in_dim, m.in_dim, device=device)
        if m.kind == "B" and m.in_dim <= max_factor_dim:
            A_cov[i] = torch.zeros(m.in_dim, m.in_dim, device=device)
        if m.out_dim <= max_factor_dim:
            S_cov[i] = torch.zeros(m.out_dim, m.out_dim, device=device)
    return A_cov, S_cov


def _accumulate_cov(i, a, g, cm, A_cov, S_cov):
    """Token covariances over the cm positions: A += a^T a (input), S += g^T g (output grad)."""
    if i in A_cov:
        x = (a * cm).reshape(-1, a.shape[-1])
        A_cov[i].addmm_(x.T, x)
    if i in S_cov:
        y = (g * cm).reshape(-1, g.shape[-1])
        S_cov[i].addmm_(y.T, y)


def _eig_factors(mods, share, A_cov, S_cov, eigh_dtype=torch.float64, only=None):
    """Eigenbases (eigh in eigh_dtype, returned fp32) per module side; None = identity.
    Empties A_cov / S_cov as it goes."""
    eig_cache: dict[int, torch.Tensor] = {}
    Qa: list[torch.Tensor | None] = []
    Qg: list[torch.Tensor | None] = []
    identity = []
    for i, m in enumerate(mods):
        wanted = only is None or m.key in only
        src = share.get(i, i)
        if wanted and (src in eig_cache or src in A_cov):
            if src not in eig_cache:  # pop: each covariance is freed once decomposed
                eig_cache[src] = torch.linalg.eigh(A_cov.pop(src).to(eigh_dtype))[1].float()
            Qa.append(eig_cache[src])
        else:
            Qa.append(None)
            identity.append((m.key, "input", m.in_dim))
        if wanted and i in S_cov:
            Qg.append(torch.linalg.eigh(S_cov.pop(i).to(eigh_dtype))[1].float())
        else:
            Qg.append(None)
            identity.append((m.key, "output", m.out_dim))
    n_eigh = len(eig_cache) + sum(1 for x in Qg if x is not None)
    return Qa, Qg, identity, n_eigh


def fit_ekfac(model, rows: list[dict], *, batch_size: int = 16,
              max_batch_tokens: int | None = 8192, max_factor_dim: int = DEFAULT_MAX_FACTOR_DIM,
              device=None, only: set[str] | None = None, eigh_dtype=torch.float64) -> dict:
    """Stand-alone EK-FAC fit on `rows` (two passes; score_rows fuses the first into its
    gradient pass). Returns {"mods", "Qa", "Qg", "lam"} in module order; used by the Bergson
    cross-check. `only` restricts the fitted factors to those module keys (others: identity)."""
    model.eval()
    _check_eval(model)
    mods, wrappers = lora_mods(model)
    device = torch.device(device) if device is not None else mods[0].lin.weight.device
    plan = batch_plan([len(r["input_ids"]) for r in rows], batch_size, max_batch_tokens)
    share = _shared_inputs(model, mods, wrappers, [rows[i] for i in plan[0][:2]], device)
    A_cov, S_cov = _alloc_cov(mods, share, max_factor_dim, device, only)

    def cov_consumer(i, G, a, g, cm):
        _accumulate_cov(i, a, g, cm, A_cov, S_cov)

    with _Tap(mods, cov_consumer) as tap:
        for idxs in plan:
            _run_batch(model, mods, tap, [rows[i] for i in idxs], device)
    Qa, Qg, _, _ = _eig_factors(mods, share, A_cov, S_cov, eigh_dtype, only)
    lam = [torch.zeros(m.out_dim, m.in_dim, device=device) for m in mods]

    def lam_consumer(i, G, a, g, cm):
        lam[i] += (_rotate(G, Qg[i], Qa[i]) ** 2).sum(0)

    with _Tap(mods, lam_consumer) as tap:
        for idxs in plan:
            _run_batch(model, mods, tap, [rows[i] for i in idxs], device)
    for t in lam:
        t /= len(rows)
    return {"mods": mods, "Qa": Qa, "Qg": Qg, "lam": lam}


# --- main entry -------------------------------------------------------------------------------


def _versions() -> dict:
    out = {"python": platform.python_version(), "torch": torch.__version__,
           "numpy": np.__version__}
    for mod in ("transformers", "peft"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            out[mod] = None
    if torch.cuda.is_available():
        out["cuda_device"] = torch.cuda.get_device_name(0)
    return out


def score_rows(model, rows: list[dict], queries: dict[str, list[dict]], *, batch_size: int = 16,
               dampings=(1e-3, 1e-2, 1e-1), proj_dim: int = 16, proj_seed: int = 20261018,
               device=None, log=print, max_batch_tokens: int | None = 4096, ekfac: bool = True,
               max_factor_dim: int = DEFAULT_MAX_FACTOR_DIM, bergson_check_rows: int = 0,
               eigh_dtype=torch.float64,
               progress_every: int = 50) -> tuple[dict[str, np.ndarray], dict]:
    """Attribution scores for every training row against every query (module docstring).

    Returns (scores, meta); scores maps `graddot:{q}`, `gradcos:{q}`, `pfi:{q}:{damp}`,
    `ekfac:{q}:{damp}` (if ekfac) and `gradnorm` to float64 arrays of len(rows) in row order
    ({damp} is formatted with `:g`, e.g. "0.001"). Keyword arguments beyond the frozen
    interface (defaults are the P43 choice): max_batch_tokens caps rows x padded length per
    batch (the working repo's OOM lesson; Llama-8B with fp32 LoRA inputs on all 7 projections
    keeps roughly 9 MB of activations per token, so 4096 tokens is ~40 GB, an estimate);
    ekfac=False skips the two EK-FAC passes;
    max_factor_dim (module docstring); eigh_dtype (float32 if the fp64 eigendecompositions are
    too slow on the pod; the eigenvalue refit keeps any orthonormal basis consistent);
    bergson_check_rows > 0 runs p43.tda_bergson's
    independent factor cross-check on that many rows and records it in meta (never fails).
    """
    t_start = time.time()
    model.eval()
    _check_eval(model)
    mods, wrappers = lora_mods(model)
    _trainable_check(model, mods)
    device = torch.device(device) if device is not None else mods[0].lin.weight.device
    dampings = tuple(float(d) for d in dampings)
    qnames = list(queries)
    if not qnames or any(not queries[q] for q in qnames):
        raise ValueError("queries must be a non-empty dict of non-empty row lists")
    K, N, M = len(qnames), len(rows), len(mods)
    meta: dict = {
        "n_rows": N, "n_queries": K, "queries": {q: len(queries[q]) for q in qnames},
        "n_lora_modules": M, "n_params": int(sum(m.numel for m in mods)),
        "batch_size": batch_size, "max_batch_tokens": max_batch_tokens,
        "dampings": list(dampings), "proj_dim": proj_dim, "proj_seed": proj_seed,
        "versions": _versions(),
        "loss": "sum of next-token CE over labelled tokens, per row; query = summed over its rows",
        "orientation": "higher = a gradient step on the row lowers the query loss",
        "methods": {
            "graddot": "exact <g_row, g_q> over all LoRA params",
            "gradcos": "exact cosine(g_row, g_q)",
            "pfi": "projected block-diagonal empirical-Fisher influence (LoGra-style), not EK-FAC",
            "ekfac": "own EK-FAC over each lora_A/lora_B Linear (token-covariance eigenbases, "
                     "per-row eigenvalue refit, damping x mean eigenvalue per module); "
                     "generalised from mats12_neel_working src/em_filter/tda.py "
                     "ekfac_module_scores",
        },
        "ekfac_impl": "own (p43/tda.py)" if ekfac else None,
        "timings_s": {}, "warnings": [], "bergson_used": False,
    }
    timings = meta["timings_s"]
    lengths = [len(r["input_ids"]) for r in rows]
    meta["n_tokens"] = int(sum(lengths))
    plan = batch_plan(lengths, batch_size, max_batch_tokens)
    meta["n_batches"] = len(plan)
    params = _lora_params(mods)
    st: dict = {"n_targets": K, "want_proj": False, "sums": None}

    def run_pass(name: str, consumer, end, check: list | None = None):
        t0 = time.time()
        with _Tap(mods, consumer) as tap:
            for bi, idxs in enumerate(plan):
                B = len(idxs)
                st["dots"] = torch.zeros(B, st["n_targets"], dtype=torch.float64, device=device)
                st["sq"] = torch.zeros(B, dtype=torch.float64, device=device)
                if st["want_proj"]:
                    st["proj"] = torch.zeros(B, D_proj, dtype=torch.float32, device=device)
                if check is not None:
                    st["sums"] = [torch.zeros(m.out_dim, m.in_dim, device=device) for m in mods]
                    tap.sums = st["sums"]
                _run_batch(model, mods, tap, [rows[i] for i in idxs], device, check)
                end(np.asarray(idxs))
                if progress_every and (bi + 1) % progress_every == 0:
                    el = time.time() - t0
                    log(f"[tda] {name}: batch {bi + 1}/{len(plan)} {el:.0f}s "
                        f"(eta {el / (bi + 1) * (len(plan) - bi - 1):.0f}s)")
        st["sums"] = None
        timings[name] = round(time.time() - t0, 2)
        log(f"[tda] {name} done in {timings[name]:.1f}s")

    # --- 1. query gradients (autograd, summed over each query's rows) ---------------------------
    t0 = time.time()
    qgrads: dict[str, list[torch.Tensor]] = {}
    for q in qnames:
        acc = [torch.zeros(m.out_dim, m.in_dim, device=device) for m in mods]
        qlen = [len(r["input_ids"]) for r in queries[q]]
        for idxs in batch_plan(qlen, batch_size, max_batch_tokens):
            ids, attn, labels = collate([queries[q][i] for i in idxs], device)
            loss = row_losses(model, ids, attn, labels).sum()
            for a, g in zip(acc, torch.autograd.grad(loss, params), strict=True):
                a += g.float()
        qgrads[q] = acc
    timings["query_grads"] = round(time.time() - t0, 2)
    qnorm = {q: math.sqrt(sum(float((g.double() ** 2).sum()) for g in qgrads[q])) for q in qnames}
    meta["query_grad_norms"] = qnorm
    log(f"[tda] query gradients for {K} queries in {timings['query_grads']:.1f}s")

    # --- 2. pass 1: graddot, norms, projections, EK-FAC token covariances ----------------------
    projs = make_projections(mods, proj_dim, proj_seed, device)
    dims = proj_dims(mods, projs)
    D_proj = int(sum(dims))
    offs = np.concatenate([[0], np.cumsum(dims)]).astype(int)
    meta["pfi_dim"] = D_proj
    targets1 = [torch.stack([qgrads[q][i] for q in qnames]).reshape(K, -1) for i in range(M)]
    Qproj = torch.cat([torch.stack([project(m, P, qgrads[q][i]) for q in qnames])
                       for i, (m, P) in enumerate(zip(mods, projs, strict=True))], dim=1)

    # lora_A inputs shared by sibling projections (q/k/v, gate/up) share one EK-FAC input factor
    share: dict[int, int] = {}
    A_cov: dict[int, torch.Tensor] = {}
    S_cov: dict[int, torch.Tensor] = {}
    if ekfac:
        share = _shared_inputs(model, mods, wrappers, [rows[i] for i in plan[0][:2]], device)
        meta["ekfac_shared_input_factors"] = sum(1 for i, j in share.items() if i != j)
        A_cov, S_cov = _alloc_cov(mods, share, max_factor_dim, device)

    dots_all = np.zeros((N, K), dtype=np.float64)
    sq_all = np.zeros(N, dtype=np.float64)
    V = torch.zeros(N, D_proj, dtype=torch.float32)  # CPU
    check1: list[float] = []

    def consumer1(i, G, a, g, cm):
        Gf = G.reshape(G.shape[0], -1)
        st["dots"] += (Gf @ targets1[i].T).double()
        st["sq"] += (Gf.double() ** 2).sum(1)
        st["proj"][:, offs[i]:offs[i + 1]] = project(mods[i], projs[i], G)
        if st["sums"] is not None:
            st["sums"][i] += G.sum(0)
        _accumulate_cov(i, a, g, cm, A_cov, S_cov)

    def end1(ii):
        dots_all[ii] = st["dots"].cpu().numpy()
        sq_all[ii] = st["sq"].cpu().numpy()
        V[torch.as_tensor(ii)] = st["proj"].cpu()

    st["n_targets"], st["want_proj"] = K, True
    run_pass("pass1_grads", consumer1, end1, check1)
    st["want_proj"] = False
    targets1 = None
    meta["batch_sum_invariant_max_rel_err"] = max(check1)
    if max(check1) > 1e-3:
        meta["warnings"].append(f"batch-sum invariant rel err {max(check1):.2e} > 1e-3")
    log(f"[tda] batch-sum invariant max rel err {max(check1):.2e}")

    scores: dict[str, np.ndarray] = {}
    norms = np.sqrt(sq_all)
    scores["gradnorm"] = norms
    for k, q in enumerate(qnames):
        scores[f"graddot:{q}"] = dots_all[:, k].copy()
        scores[f"gradcos:{q}"] = dots_all[:, k] / np.maximum(norms * qnorm[q], 1e-300)

    # --- 3. pfi -------------------------------------------------------------------------------
    t0 = time.time()
    pf = block_fisher_scores(V, Qproj, dims, dampings, device)
    for d in dampings:
        arr = pf[d].cpu().numpy()
        for k, q in enumerate(qnames):
            scores[f"pfi:{q}:{d:g}"] = arr[:, k].copy()
    timings["pfi"] = round(time.time() - t0, 2)
    V = pf = None  # free the projected store

    # --- 4. EK-FAC ----------------------------------------------------------------------------
    if ekfac:
        t0 = time.time()
        Qa, Qg, identity, n_eigh = _eig_factors(mods, share, A_cov, S_cov, eigh_dtype)
        A_cov.clear()
        S_cov.clear()
        timings["ekfac_eigh"] = round(time.time() - t0, 2)
        meta["ekfac_max_factor_dim"] = max_factor_dim
        meta["ekfac_n_eigh"] = n_eigh
        meta["ekfac_identity_sides"] = len(identity)
        meta["ekfac_identity_examples"] = [f"{k}:{s}({d})" for k, s, d in identity[:6]]
        meta["ekfac_identity_params"] = int(sum(
            m.numel for i, m in enumerate(mods) if Qa[i] is None or Qg[i] is None))
        log(f"[tda] EK-FAC eigendecompositions in {timings['ekfac_eigh']:.1f}s; "
            f"{len(identity)} factor sides kept as identity (dim > {max_factor_dim})")

        lam = [torch.zeros(m.out_dim, m.in_dim, device=device) for m in mods]

        def consumer2(i, G, a, g, cm):
            lam[i] += (_rotate(G, Qg[i], Qa[i]) ** 2).sum(0)

        st["n_targets"] = 0
        run_pass("pass2_ekfac_lambda", consumer2, lambda ii: None)
        for t in lam:
            t /= N

        Kd = K * len(dampings)
        targets3 = [torch.stack([ekfac_precondition(qgrads[q][i], Qg[i], Qa[i], lam[i], d)
                                 for d in dampings for q in qnames]).reshape(Kd, -1)
                    for i in range(M)]
        Qa = Qg = None  # pass 3 needs only the preconditioned queries
        dots3 = np.zeros((N, Kd), dtype=np.float64)

        def consumer3(i, G, a, g, cm):
            st["dots"] += (G.reshape(G.shape[0], -1) @ targets3[i].T).double()

        def end3(ii):
            dots3[ii] = st["dots"].cpu().numpy()

        st["n_targets"] = Kd
        run_pass("pass3_ekfac_scores", consumer3, end3)
        for di, d in enumerate(dampings):
            for k, q in enumerate(qnames):
                scores[f"ekfac:{q}:{d:g}"] = dots3[:, di * K + k].copy()
        meta["ekfac_lambda_mean_median_over_modules"] = float(
            np.median([float(t.mean()) for t in lam]))
        del targets3, lam

    # --- 5. optional Bergson cross-check -------------------------------------------------------
    if bergson_check_rows and ekfac:
        try:
            from p43 import tda_bergson

            ok, why = tda_bergson.available()
            meta["bergson_available"], meta["bergson_reason"] = ok, why
            if ok:
                t0 = time.time()
                meta["bergson_check"] = tda_bergson.crosscheck(
                    model, rows[:bergson_check_rows], device=device,
                    max_factor_dim=max_factor_dim, log=log)
                meta["bergson_used"] = True
                timings["bergson_check"] = round(time.time() - t0, 2)
        except Exception as e:  # noqa: BLE001
            meta["warnings"].append(f"bergson cross-check failed: {type(e).__name__}: {e}")

    timings["total"] = round(time.time() - t_start, 2)
    for k, v in scores.items():
        if v.shape != (N,):
            raise AssertionError(f"{k}: shape {v.shape}")
        if not np.all(np.isfinite(v)):
            meta["warnings"].append(f"{k}: non-finite values")
    return scores, meta
