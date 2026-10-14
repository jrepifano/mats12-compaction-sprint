"""Per-row training-data attribution over the LoRA parameters of a PEFT model (plan.md "Scorers").

A port of `probes/P43-subliminal-attribution/p43/tda.py` to Qwen2.5-7B-Instruct: the same method
and the same approximations, which are named here rather than smoothed over. What changed:

- defaults come from `p48/contracts.py` (`DAMPINGS`, `PROJ_DIM`, `PROJ_SEED`, `GRAD_BATCH`,
  `MAX_FACTOR_DIM`), and a row's labelled tokens are its RESPONSE only (`K.RECIPE.loss_mode`);
- Qwen's 18,944-wide MLP sides (the `down_proj` input and the `gate_proj` / `up_proj` outputs)
  take the place of Llama-3.1-8B's 14,336-wide ones as the factor sides that keep an **identity
  eigenbasis** (`K.MAX_FACTOR_DIM` = 4,096). For Qwen2.5-7B at LoRA r=8 on all 7 projections that
  is 84 of 784 factor sides; the 12,730,368 parameters of those 84 modules keep one genuine side
  and one identity side, so **7,454,720 of 20,185,088 LoRA parameters (36.9%) get a genuine
  eigenbasis on both sides**. `score_rows` records this in `meta["ekfac_coverage"]`, computed from
  the eigenbases it actually built and cross-checked against the dimensions, and
  `eigenbasis_coverage` / `qwen_lora_sides` reproduce it without a GPU;
- the query set is built here (`query_rows`), with the `-diff` combination in `diff_scores`;
- P43's optional Bergson cross-check is not ported. It is the same implementation P43 checked, and
  it published the result: preconditioned-vector cosines >= 0.999997 per module on its tiny test
  model, and 0.993 / 0.996 / 0.998 on the GPU at damping 1e-3 / 1e-2 / 1e-1 (worst module 0.908 /
  0.978 / 0.988). `score_rows` still accepts `bergson_check_rows` so a ported pod step does not
  break; a non-zero value is recorded as a warning and does nothing.

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
- `ekfac:{q}:{damp}`: **our EK-FAC approximation** (George et al. 2018; Grosse et al. 2023), P43's
  own implementation over each lora_A / lora_B Linear: Kronecker factors are token covariances of
  the module input (A) and output gradient (S) over real tokens, eigendecomposed (fp64); the
  eigenvalues are refit as lambda = mean_rows (Q_S^T G_row Q_A)^2 (per-row gradients); damping is
  damp x mean(lambda) per module (0.1 = kronfluence's default heuristic). Generalises
  `ekfac_module_scores` / `ekfac_lambda_refit` of ../mats12_neel_working/src/em_filter/tda.py
  (rank-1, one module) to rank r on every module. A factor whose dimension exceeds
  `max_factor_dim` keeps the identity as its eigenbasis (the lambda refit still runs, so that side
  is diagonal rather than absent); full 18,944^2 factors would need ~138 GB of fp32 accumulators
  plus 84 eigendecompositions of that size, which does not fit next to the model in one pass.
  It is therefore **not** full EK-FAC, and results are named as the approximation they are.
  Covariances use each row's real tokens except its last (Bergson's convention).

Per-row gradients are exact and come from ONE batched forward/backward per batch: forward hooks
on each lora_A / lora_B Linear keep its input and register a tensor hook on its output; the
output gradient then gives G_b = sum_t g_bt a_bt^T (einsum over token positions). Rows are
right-padded and padded positions are masked, so padding contributes nothing; the model must be in
eval mode (LoRA dropout is the identity). The batch plan and the "batch sum of per-row gradients
equals autograd's batch gradient" check are ported from ../mats12_neel_working/src/em_filter/
tda_pod.py (`batch_plan`) and scripts/tda_grads.py (in-run invariant). Nothing of size
n_rows x n_params is ever materialised: query vectors stay on the device and per-row results are
dot products, plus the projected vectors (n_rows x 50,176 fp32 at K.PROJ_DIM = 16 for Qwen-7B
r=8, so 2.0 GB at 10,000 rows) on CPU.

torch and peft are imported inside functions, so this module imports (and its naming, planner and
coverage helpers run) on a laptop without them.
"""

from __future__ import annotations

import math
import platform
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from p48 import contracts as K

IGNORE = -100
DEFAULT_MAX_FACTOR_DIM = K.MAX_FACTOR_DIM

# Qwen2.5-7B-Instruct's factor dimensions (config.json @ K.MODEL_REV): hidden 3,584, intermediate
# 18,944, and GQA with 4 key/value heads of head_dim 128, so k_proj / v_proj output 512.
QWEN_HIDDEN = 3584
QWEN_INTERMEDIATE = 18944
QWEN_KV_OUT = 512


def _resolve_dtype(torch, dtype):
    """"float64" / torch.float64 -> torch.float64 (so module import needs no torch)."""
    return getattr(torch, dtype) if isinstance(dtype, str) else dtype


# --- query naming (plan.md "Scorers") ----------------------------------------------------------


def query_name(answer: str) -> str:
    """"Cat." -> "cat" (P43's convention, p43/pod.py query_rows)."""
    return answer.split()[-1].strip(".").lower()


PRIMARY_QUERY = query_name(K.QUERY_ANSWER)  # "cat"
CF_QUERIES = tuple(f"cf_{query_name(a)}" for a in K.COUNTERFACTUAL_ANSWERS)
NULL_QUERY = query_name(K.NULL_ANSWER)  # "owl"
GRAD_FAMILIES = ("graddot", "gradcos", "pfi", "ekfac")


def query_rows(tok, max_len: int | None = None) -> dict[str, list[dict]]:
    """The frozen attribution query set, tokenised with response-only labels.

    `K.query_questions()` (25 of the 50 eval questions, seeded) answered:
    - `K.QUERY_ANSWER` ("Cat.") -> "cat", the primary query;
    - each of the 8 `K.COUNTERFACTUAL_ANSWERS` -> "cf_dog" ... "cf_penguin", whose mean the
      `-diff` variants subtract (`diff_scores`);
    - `K.NULL_ANSWER` ("Owl.") -> "owl", the null query. It is kept DISJOINT from the
      counterfactuals (contracts.py, 2026-10-14), so the null is not one component of the `-diff`
      baseline it is compared against.
    """
    from p48.encode import IGNORE as ENCODE_IGNORE
    from p48.encode import chat, encode

    if ENCODE_IGNORE != IGNORE:
        raise ValueError(f"encode masks with {ENCODE_IGNORE}, tda with {IGNORE}")
    mode, mlen = K.RECIPE.loss_mode, K.RECIPE.max_len if max_len is None else max_len
    qs = K.query_questions()

    def rows(answer: str) -> list[dict]:
        return [encode(tok, chat(None, q, answer), mode, mlen) for q in qs]

    out = {PRIMARY_QUERY: rows(K.QUERY_ANSWER)}
    for name, ans in zip(CF_QUERIES, K.COUNTERFACTUAL_ANSWERS, strict=True):
        out[name] = rows(ans)
    out[NULL_QUERY] = rows(K.NULL_ANSWER)
    return out


def diff_scores(scores: dict[str, np.ndarray], primary: str = PRIMARY_QUERY,
                cf: Sequence[str] = CF_QUERIES) -> dict[str, np.ndarray]:
    """The `-diff` variants: the primary query's score minus the mean over the counterfactuals.

    `{family}:{primary}` -> `{family}:{primary}-cf` and `{family}:{primary}:{damp}` ->
    `{family}:{primary}-cf:{damp}` (P43's analyze.py naming with mode dropped, since P48 has one
    loss mode). A family is skipped unless every counterfactual's score is present.
    """
    out: dict[str, np.ndarray] = {}
    for key, arr in scores.items():
        parts = key.split(":")
        if len(parts) < 2 or parts[0] not in GRAD_FAMILIES or parts[1] != primary:
            continue
        rest = parts[2:]
        others = [scores.get(":".join([parts[0], c, *rest])) for c in cf]
        if not cf or any(o is None for o in others):
            continue
        name = ":".join([f"{parts[0]}:{primary}-cf", *rest])
        out[name] = np.asarray(arr, dtype=np.float64) - np.mean(others, axis=0)
    return out


# --- LoRA module discovery --------------------------------------------------------------------


@dataclass
class LoraMod:
    """One LoRA matrix (a lora_A or lora_B Linear) of one wrapped projection."""

    key: str  # e.g. "base_model.model.model.layers.0.self_attn.q_proj.lora_A"
    site: str  # the wrapper module's name
    kind: str  # "A" or "B"
    lin: object  # nn.Linear
    out_dim: int
    in_dim: int

    @property
    def numel(self) -> int:
        return self.out_dim * self.in_dim


def lora_mods(model) -> tuple[list[LoraMod], dict[str, object]]:
    """All trainable lora_A / lora_B Linears in module order (A then B per site), plus wrappers."""
    from torch import nn

    mods: list[LoraMod] = []
    wrappers: dict[str, object] = {}
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
    from torch import nn

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


# --- eigenbasis coverage (the approximation, recorded) ------------------------------------------


def short_key(key: str) -> str:
    """"...layers.3.mlp.down_proj.lora_A" -> "down_proj.lora_A"."""
    return ".".join(key.split(".")[-2:])


def module_sides(mods: list[LoraMod]) -> list[tuple[str, int, int]]:
    """(key, out_dim, in_dim) per LoRA matrix, in module order."""
    return [(m.key, m.out_dim, m.in_dim) for m in mods]


def qwen_lora_sides(rank: int = K.RECIPE.rank, n_layers: int = K.N_LAYERS,
                    hidden: int = QWEN_HIDDEN, intermediate: int = QWEN_INTERMEDIATE,
                    kv_out: int = QWEN_KV_OUT,
                    target_modules: Sequence[str] = K.RECIPE.target_modules,
                    ) -> list[tuple[str, int, int]]:
    """`module_sides` for Qwen2.5-7B-Instruct without loading it (the coverage the note reports).

    Mirrors PEFT's key layout so `short_key` and the shapes match a real run; summing
    out_dim x in_dim gives `K.N_LORA_PARAMS` (asserted in tests/test_tda.py).
    """
    shapes = {"q_proj": (hidden, hidden), "k_proj": (kv_out, hidden), "v_proj": (kv_out, hidden),
              "o_proj": (hidden, hidden), "gate_proj": (intermediate, hidden),
              "up_proj": (intermediate, hidden), "down_proj": (hidden, intermediate)}
    attn = {"q_proj", "k_proj", "v_proj", "o_proj"}
    unknown = [t for t in target_modules if t not in shapes]
    if unknown:
        raise ValueError(f"unknown projections for Qwen2.5-7B: {unknown}")
    sides: list[tuple[str, int, int]] = []
    for layer in range(n_layers):
        for proj in target_modules:
            out_dim, in_dim = shapes[proj]
            block = "self_attn" if proj in attn else "mlp"
            site = f"base_model.model.model.layers.{layer}.{block}.{proj}"
            sides.append((f"{site}.lora_A", rank, in_dim))
            sides.append((f"{site}.lora_B", out_dim, rank))
    return sides


def predicted_identity_sides(sides: Iterable[tuple[str, int, int]],
                             max_factor_dim: int) -> list[tuple[str, str, int]]:
    """(key, "input"/"output", dim) for every factor side wider than `max_factor_dim`.

    Same order as `_eig_factors` builds them (input then output, module order).
    """
    out: list[tuple[str, str, int]] = []
    for key, out_dim, in_dim in sides:
        if in_dim > max_factor_dim:
            out.append((key, "input", in_dim))
        if out_dim > max_factor_dim:
            out.append((key, "output", out_dim))
    return out


def eigenbasis_coverage(sides: Iterable[tuple[str, int, int]],
                        max_factor_dim: int = DEFAULT_MAX_FACTOR_DIM,
                        identity: Iterable[tuple[str, str, int]] | None = None) -> dict:
    """How much of the LoRA parameter space our EK-FAC gives a genuine eigenbasis.

    `sides` is `module_sides`; `identity` the sides actually kept as the identity (from
    `_eig_factors`), or None to predict them from the dimensions. "Genuine" means BOTH of a
    module's sides were eigendecomposed; a module with one identity side still gets its eigenvalue
    refit, so that side is diagonal rather than ignored, and its parameters are counted under
    `identity_eigenbasis_params`.
    """
    sides = [(str(k), int(o), int(i)) for k, o, i in sides]
    pred = predicted_identity_sides(sides, max_factor_dim)
    obs = pred if identity is None else [(str(k), str(s), int(d)) for k, s, d in identity]
    mismatch = {(k, s) for k, s, _ in obs} ^ {(k, s) for k, s, _ in pred}
    per_mod = Counter(k for k, _, _ in obs)
    n_params = sum(o * i for _, o, i in sides)
    genuine = sum(o * i for k, o, i in sides if not per_mod[k])
    return {
        "max_factor_dim": int(max_factor_dim),
        "n_modules": len(sides),
        "n_sides": 2 * len(sides),
        "n_params": n_params,
        "n_identity_sides": len(obs),
        "identity_side_counts": dict(sorted(Counter(
            f"{short_key(k)}:{s}:{d}" for k, s, d in obs).items())),
        "identity_sides": obs,
        "n_modules_with_identity_side": sum(1 for v in per_mod.values() if v),
        "n_modules_fully_identity": sum(1 for v in per_mod.values() if v >= 2),
        "genuine_eigenbasis_params": genuine,
        "identity_eigenbasis_params": n_params - genuine,
        "genuine_eigenbasis_fraction": genuine / n_params if n_params else 0.0,
        "observed_matches_dims": not mismatch,
        "mismatched_sides": sorted(mismatch),
    }


# --- batching ---------------------------------------------------------------------------------


def batch_plan(lengths: list[int], max_rows: int, max_tokens: int | None) -> list[list[int]]:
    """Deterministic greedy packing, longest rows first (port of em_filter.tda_pod.batch_plan).

    max_tokens bounds rows_in_batch x longest_row_len (the padded footprint); None = no bound.
    A single row longer than max_tokens still gets its own batch.
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
    import torch

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
    """Per-row SUM of next-token cross-entropies over labelled tokens, fp32, shape (B,).

    P48's labelled tokens are the response only (`K.RECIPE.loss_mode == "response"`, set by
    p48/encode.py); this function only reads the labels it is given.
    """
    import torch
    import torch.nn.functional as F

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
        self.sums: list | None = None
        self.mods = mods
        self.consumer = consumer
        self.mask = None  # (B, T) float
        self.cov_mask = None  # (B, T) float
        self.inputs: dict[int, object] = {}
        self.handles: list = []
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
            import torch

            if not torch.is_grad_enabled():
                return
            self.inputs[i] = inp[0]
            out.register_hook(self._bwd(i))

        return hook

    def _bwd(self, i: int):
        def hook(grad):
            import torch

            with torch.no_grad():
                m = self.mask[..., None]
                a = self.inputs.pop(i).float() * m
                g = grad.float() * m
                G = torch.einsum("bto,bti->boi", g, a)
                self.consumer(i, G, a, g, self.cov_mask[..., None])
                self.calls += 1

        return hook


def _lora_params(mods: list[LoraMod]) -> list:
    return [m.lin.weight for m in mods]


def _run_batch(model, mods, tap: _Tap, rows: list[dict], device, check: list | None = None):
    """One forward/backward over a batch with the tap armed. Returns per-row losses (B,).

    With `check` a list, appends the max relative error between the summed per-row gradients
    (collected by the consumer into tap.sums) and autograd's batch gradient.
    """
    import torch

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
    out: dict[int, object] = {}

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
    and its relative error is reported only (P43 measured cos 0.9973 in bf16 while the exact
    batch-sum identity held at 4e-7; in fp32 both are tight)."""
    import torch

    model.eval()
    _check_eval(model)
    mods, _ = lora_mods(model)
    device = mods[0].lin.weight.device
    per: dict[int, object] = {}
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
    import torch

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


def project(m: LoraMod, P, G):
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


def block_fisher_scores(V, Q, dims: list[int], dampings, device) -> dict:
    """pfi scores. V (N, D) projected row vectors (CPU), Q (K, D) projected queries.
    Returns {damp: (N, K) fp64}: sum_b V_b (F_b + lam_b I)^-1 Q_b^T, F_b = V_b^T V_b / N,
    lam_b = damp * trace(F_b) / dim_b."""
    import torch

    N = V.shape[0]
    V = V.to(device)  # one transfer (2.0 GB for Qwen-7B r=8 at 10K rows), then slice there
    out = {d: torch.zeros(N, Q.shape[0], dtype=torch.float64, device=device) for d in dampings}
    off = 0
    for dim in dims:
        Vb = V[:, off:off + dim].to(device, torch.float64)
        Qb = Q[:, off:off + dim].to(device, torch.float64)
        Fb = Vb.T @ Vb / N
        mean_eig = float(torch.diagonal(Fb).mean())
        # Fb is symmetric PSD, so invert it through its eigendecomposition rather than
        # torch.linalg.solve. Whenever the scored rows are fewer than the block's dimension -
        # or merely very collinear, which the real model's projected gradients are - Fb is
        # rank-deficient, and a damping of damp x mean_eig can be too small to make
        # `Fb + lam I` numerically invertible. cuSOLVER then reports a singular matrix, and
        # on CUDA that error surfaces ASYNCHRONOUSLY at the next synchronisation point, which
        # made it look as though EK-FAC had failed (smoke attempts 3 and 4, 2026-10-14).
        # eigh on a PSD matrix is stable and gives the identical answer where solve works.
        # cuSOLVER's eigh does not converge on these blocks (error code 30: ill-conditioned or
        # too many repeated eigenvalues) because the projected per-row gradients are highly
        # collinear, so Fb has many near-zero eigenvalues. LAPACK on the CPU is far more robust
        # for small symmetric matrices, and a block is only dim x dim (128 here), so the
        # round trip is negligible beside the GPU matmuls (smoke attempts 3-5, 2026-10-14).
        w, U = robust_eigh(Fb)
        w = w.clamp_min(0.0)
        UtQ = U.T @ Qb.T  # (dim, K)
        for d in dampings:
            lam = d * mean_eig
            if lam <= 0:  # an all-zero block contributes nothing
                continue
            out[d] += Vb @ (U @ (UtQ / (w + lam).unsqueeze(1)))
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


def ekfac_precondition(q, Qg, Qa, lam_eig, damping: float):
    """EK-FAC (H + lam I)^-1 q for one module: Q_S [(Q_S^T q Q_A) / (Lambda + lam)] Q_A^T,
    lam = damping * mean(Lambda) (port of em_filter.tda.ekfac_module_scores, rank r)."""
    lam = damping * float(lam_eig.mean())
    return _unrotate(_rotate(q, Qg, Qa) / (lam_eig + lam), Qg, Qa)


def _shared_inputs(model, mods, wrappers, rows, device) -> dict[int, int]:
    """lora_A index -> index of the first lora_A whose wrapper sees the very same input tensor
    (q/k/v; gate/up), found with one no-grad forward on `rows`. Those share one input factor.
    The inputs are held until grouping is done, so no tensor id can be reused meanwhile."""
    import torch

    seen: dict[str, object] = {}

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
    import torch

    A_cov: dict[int, object] = {}
    S_cov: dict[int, object] = {}
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


TDA_BUILD = "2026-10-14T21:30Z-identity-fallback"  # logged by score_rows; a stale push shows up
EIGH_STATS = {"gpu": 0, "jitter": 0, "cpu": 0, "cpu_jitter": 0}


def robust_eigh(M, *, log=None):
    """`torch.linalg.eigh` that cannot take a run down (smoke attempts 3-6, 2026-10-14).

    These covariance and Fisher blocks are built from highly collinear per-row gradients, so they
    are ill-conditioned with many repeated near-zero eigenvalues. cuSOLVER then refuses to
    converge ("error code 1"/"error code 30"), in `block_fisher_scores` and again in
    `_eig_factors`. The escalation is: GPU, GPU with a relative jitter, CPU (LAPACK is far more
    robust on these), CPU with a jitter. Counts land in EIGH_STATS and are reported in meta, so a
    run that needed the slow paths says so instead of looking clean.
    """
    import torch

    def jitter(A):
        scale = float(torch.diagonal(A).abs().max()) if A.numel() else 1.0
        eps = 1e-10 * max(scale, 1.0)
        return A + eps * torch.eye(A.shape[-1], dtype=A.dtype, device=A.device)

    try:
        out = torch.linalg.eigh(M)
        EIGH_STATS["gpu"] += 1
        return out
    except Exception as exc:  # noqa: BLE001 - torch raises the private _LinAlgError
        first = exc
    try:
        out = torch.linalg.eigh(jitter(M))
        EIGH_STATS["jitter"] += 1
        return out
    except Exception:  # noqa: BLE001
        del first
    Mc = M.cpu()
    try:
        w, U = torch.linalg.eigh(Mc)
        EIGH_STATS["cpu"] += 1
    except Exception:  # noqa: BLE001
        w, U = torch.linalg.eigh(jitter(Mc))
        EIGH_STATS["cpu_jitter"] += 1
    if log is not None:
        log(f"[tda] eigh fell back to the CPU for a {tuple(Mc.shape)} block")
    return w.to(M.device), U.to(M.device)


def _eig_factors(mods, share, A_cov, S_cov, eigh_dtype="float64", only=None):
    """Eigenbases (eigh in eigh_dtype, returned fp32) per module side; None = identity.
    Empties A_cov / S_cov as it goes. Also returns the identity sides, in module order
    (input then output), which `eigenbasis_coverage` turns into the reported coverage."""
    import torch

    dt = _resolve_dtype(torch, eigh_dtype)
    failed: list[str] = []
    eig_cache: dict[int, object] = {}
    Qa: list = []
    Qg: list = []
    identity: list[tuple[str, str, int]] = []
    for i, m in enumerate(mods):
        wanted = only is None or m.key in only
        src = share.get(i, i)
        if wanted and (src in eig_cache or src in A_cov):
            if src not in eig_cache:  # pop: each covariance is freed once decomposed
                eig_cache[src] = _eigenbasis_or_identity(A_cov.pop(src).to(dt), failed)
            Qa.append(eig_cache[src])
            if eig_cache[src] is None:
                identity.append((m.key, "input", m.in_dim))
        else:
            Qa.append(None)
            identity.append((m.key, "input", m.in_dim))
        if wanted and i in S_cov:
            Qg.append(_eigenbasis_or_identity(S_cov.pop(i).to(dt), failed))
            if Qg[-1] is None:
                identity.append((m.key, "output", m.out_dim))
        else:
            Qg.append(None)
            identity.append((m.key, "output", m.out_dim))
    n_eigh = len(eig_cache) + sum(1 for x in Qg if x is not None)
    return Qa, Qg, identity, n_eigh, failed


def _eigenbasis_or_identity(M, failed: list[str]):
    """The eigenbasis of a covariance, or None (identity) if it cannot be decomposed.

    Seven paid launches died because one ill-conditioned covariance refused to decompose and
    took the whole scoring pass with it (log.md 2026-10-14). Falling back to the identity
    eigenbasis for that ONE side is the same approximation EK-FAC already makes for every side
    wider than `max_factor_dim`, so it degrades the method in a way the coverage figures already
    describe, instead of losing the primary selector entirely.
    """
    try:
        return robust_eigh(M)[1].float()
    except Exception as exc:  # noqa: BLE001 - torch raises the private _LinAlgError
        failed.append(f"{tuple(M.shape)}: {type(exc).__name__}")
        return None


# --- main entry -------------------------------------------------------------------------------


def _versions() -> dict:
    import torch

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


def score_rows(model, rows: list[dict], queries: dict[str, list[dict]], *,
               batch_size: int = K.GRAD_BATCH, dampings=K.DAMPINGS, proj_dim: int = K.PROJ_DIM,
               proj_seed: int = K.PROJ_SEED, device=None, log=print,
               max_batch_tokens: int | None = 4096, ekfac: bool = True,
               max_factor_dim: int = DEFAULT_MAX_FACTOR_DIM, bergson_check_rows: int = 0,
               eigh_dtype="float64",
               progress_every: int = 50) -> tuple[dict[str, np.ndarray], dict]:
    """Attribution scores for every training row against every query (module docstring).

    Returns (scores, meta); scores maps `graddot:{q}`, `gradcos:{q}`, `pfi:{q}:{damp}`,
    `ekfac:{q}:{damp}` (if ekfac) and `gradnorm` to float64 arrays of len(rows) in row order
    ({damp} is formatted with `:g`, e.g. "0.001"). The `-diff` variants are formed from these by
    `diff_scores`. Keyword arguments beyond the frozen interface (defaults are P48's contracts):
    max_batch_tokens caps rows x padded length per batch (the working repo's OOM lesson;
    Qwen2.5-7B with fp32 LoRA inputs on all 7 projections keeps roughly 10 MB of activations per
    token, so 4096 tokens is ~40 GB, an estimate); ekfac=False skips the two EK-FAC passes;
    max_factor_dim (module docstring); eigh_dtype ("float32" if the fp64 eigendecompositions are
    too slow on the pod; the eigenvalue refit keeps any orthonormal basis consistent);
    bergson_check_rows is accepted for interface compatibility with P43's pod step and does
    nothing (module docstring).
    """
    import torch

    t_start = time.time()
    log(f"[tda] build {TDA_BUILD}")
    for _k in EIGH_STATS:
        EIGH_STATS[_k] = 0
    model.eval()
    _check_eval(model)
    mods, wrappers = lora_mods(model)
    _trainable_check(model, mods)
    device = torch.device(device) if device is not None else mods[0].lin.weight.device
    dampings = tuple(float(d) for d in dampings)
    qnames = list(queries)
    if not qnames or any(not queries[q] for q in qnames):
        raise ValueError("queries must be a non-empty dict of non-empty row lists")
    K_, N, M = len(qnames), len(rows), len(mods)
    meta: dict = {
        "n_rows": N, "n_queries": K_, "queries": {q: len(queries[q]) for q in qnames},
        "n_lora_modules": M, "n_params": int(sum(m.numel for m in mods)),
        "batch_size": batch_size, "max_batch_tokens": max_batch_tokens,
        "dampings": list(dampings), "proj_dim": proj_dim, "proj_seed": proj_seed,
        "versions": _versions(),
        "loss": "sum of next-token CE over labelled tokens, per row; query = summed over its rows",
        "loss_mode_of_rows": K.RECIPE.loss_mode,
        "orientation": "higher = a gradient step on the row lowers the query loss",
        "methods": {
            "graddot": "exact <g_row, g_q> over all LoRA params",
            "gradcos": "exact cosine(g_row, g_q)",
            "pfi": "projected block-diagonal empirical-Fisher influence (LoGra-style), not EK-FAC",
            "ekfac": "our EK-FAC approximation: P43's own EK-FAC over each lora_A/lora_B Linear "
                     "(token-covariance eigenbases, per-row eigenvalue refit, damping x mean "
                     "eigenvalue per module), with factor sides wider than max_factor_dim kept "
                     "as the identity; generalised from mats12_neel_working "
                     "src/em_filter/tda.py ekfac_module_scores",
        },
        "ekfac_impl": "own, approximate (p48/tda.py, ported from p43/tda.py)" if ekfac else None,
        "timings_s": {}, "warnings": [], "bergson_used": False, "bergson_check": None,
    }
    if bergson_check_rows:
        meta["warnings"].append("bergson_check_rows ignored: the cross-check is not ported to "
                                "P48 (p48/tda.py docstring)")
    timings = meta["timings_s"]
    lengths = [len(r["input_ids"]) for r in rows]
    meta["n_tokens"] = int(sum(lengths))
    plan = batch_plan(lengths, batch_size, max_batch_tokens)
    meta["n_batches"] = len(plan)
    params = _lora_params(mods)
    st: dict = {"n_targets": K_, "want_proj": False, "sums": None}

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
    qgrads: dict[str, list] = {}
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
    log(f"[tda] query gradients for {K_} queries in {timings['query_grads']:.1f}s")

    # --- 2. pass 1: graddot, norms, projections, EK-FAC token covariances ----------------------
    projs = make_projections(mods, proj_dim, proj_seed, device)
    dims = proj_dims(mods, projs)
    D_proj = int(sum(dims))
    offs = np.concatenate([[0], np.cumsum(dims)]).astype(int)
    meta["pfi_dim"] = D_proj
    targets1 = [torch.stack([qgrads[q][i] for q in qnames]).reshape(K_, -1) for i in range(M)]
    Qproj = torch.cat([torch.stack([project(m, P, qgrads[q][i]) for q in qnames])
                       for i, (m, P) in enumerate(zip(mods, projs, strict=True))], dim=1)

    # lora_A inputs shared by sibling projections (q/k/v, gate/up) share one EK-FAC input factor
    share: dict[int, int] = {}
    A_cov: dict[int, object] = {}
    S_cov: dict[int, object] = {}
    if ekfac:
        share = _shared_inputs(model, mods, wrappers, [rows[i] for i in plan[0][:2]], device)
        meta["ekfac_shared_input_factors"] = sum(1 for i, j in share.items() if i != j)
        A_cov, S_cov = _alloc_cov(mods, share, max_factor_dim, device)

    dots_all = np.zeros((N, K_), dtype=np.float64)
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

    st["n_targets"], st["want_proj"] = K_, True
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
    # pfi is a SECONDARY scorer; EK-FAC is the primary selector and runs next. A pfi failure
    # must never prevent EK-FAC from being computed - in smoke attempts 3-5 it did exactly that,
    # because pfi raised before EK-FAC ever ran (2026-10-14).
    try:
        pf = block_fisher_scores(V, Qproj, dims, dampings, device)
        for d in dampings:
            arr = pf[d].cpu().numpy()
            for k, q in enumerate(qnames):
                scores[f"pfi:{q}:{d:g}"] = arr[:, k].copy()
    except Exception as exc:  # noqa: BLE001 - record and carry on to the primary scorer
        meta["pfi_error"] = repr(exc)[:300]
        meta["warnings"].append(f"pfi skipped: {meta['pfi_error']}")
        log(f"[tda] pfi FAILED, continuing to EK-FAC: {meta['pfi_error']}")
        pf = None
    timings["pfi"] = round(time.time() - t0, 2)
    V = pf = None  # free the projected store

    # --- 4. EK-FAC ----------------------------------------------------------------------------
    if ekfac:
        t0 = time.time()
        Qa, Qg, identity, n_eigh, eigh_failed = _eig_factors(mods, share, A_cov, S_cov,
                                                              eigh_dtype)
        if eigh_failed:
            meta["ekfac_eigh_failed_sides"] = eigh_failed[:20]
            meta["ekfac_n_eigh_failed"] = len(eigh_failed)
            meta["warnings"].append(
                f"{len(eigh_failed)} covariance sides would not decompose and kept an identity "
                "eigenbasis")
            log(f"[tda] {len(eigh_failed)} sides kept identity after eigh failed: "
                f"{eigh_failed[:3]}")
        A_cov.clear()
        S_cov.clear()
        timings["ekfac_eigh"] = round(time.time() - t0, 2)
        cov = eigenbasis_coverage(module_sides(mods), max_factor_dim, identity)
        meta["ekfac_max_factor_dim"] = max_factor_dim
        meta["ekfac_n_eigh"] = n_eigh
        meta["ekfac_coverage"] = cov
        # P43's key names, kept so the two probes' metas read the same way
        meta["ekfac_identity_sides"] = cov["n_identity_sides"]
        meta["ekfac_identity_examples"] = [f"{k}:{s}({d})" for k, s, d in identity[:6]]
        meta["ekfac_identity_params"] = cov["identity_eigenbasis_params"]
        meta["ekfac_genuine_eigenbasis_params"] = cov["genuine_eigenbasis_params"]
        meta["ekfac_genuine_eigenbasis_fraction"] = cov["genuine_eigenbasis_fraction"]
        # how many eigendecompositions needed a fallback path (robust_eigh)
        meta["eigh_paths"] = dict(EIGH_STATS)
        if not cov["observed_matches_dims"]:
            meta["warnings"].append(
                f"identity sides differ from the dimension rule: {cov['mismatched_sides'][:6]}")
        log(f"[tda] EK-FAC eigendecompositions in {timings['ekfac_eigh']:.1f}s; "
            f"{cov['n_identity_sides']} of {cov['n_sides']} factor sides kept as identity "
            f"(dim > {max_factor_dim}); genuine eigenbasis on both sides for "
            f"{cov['genuine_eigenbasis_params']} of {cov['n_params']} LoRA parameters "
            f"({100 * cov['genuine_eigenbasis_fraction']:.1f}%)")

        lam = [torch.zeros(m.out_dim, m.in_dim, device=device) for m in mods]

        def consumer2(i, G, a, g, cm):
            lam[i] += (_rotate(G, Qg[i], Qa[i]) ** 2).sum(0)

        st["n_targets"] = 0
        run_pass("pass2_ekfac_lambda", consumer2, lambda ii: None)
        for t in lam:
            t /= N

        Kd = K_ * len(dampings)
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
                scores[f"ekfac:{q}:{d:g}"] = dots3[:, di * K_ + k].copy()
        meta["ekfac_lambda_mean_median_over_modules"] = float(
            np.median([float(t.mean()) for t in lam]))
        del targets3, lam

    timings["total"] = round(time.time() - t_start, 2)
    for k, v in scores.items():
        if v.shape != (N,):
            raise AssertionError(f"{k}: shape {v.shape}")
        if not np.all(np.isfinite(v)):
            meta["warnings"].append(f"{k}: non-finite values")
    return scores, meta
