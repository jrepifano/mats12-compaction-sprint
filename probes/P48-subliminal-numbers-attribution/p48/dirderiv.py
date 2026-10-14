"""P48 directional derivative along a LoRA delta: `dir_student` and `dir_teacher` (plan.md
"Scorers"), Cloud et al.'s "the gradient points toward the teacher" made computable.

The contract, fixed by review round 4 and implemented here exactly:

- **The path.** W(eps) = W_base + eps * s * B @ A, where `s` is PEFT's own scaling (alpha / r, or
  alpha / sqrt(r) under rsLoRA) and `B @ A` the adapter delta. `eps` scales **each adapter
  branch's output**, per example, in every adapted module. It does **not** scale both LoRA
  factors: that would give eps^2 * s * B @ A and a derivative of 0 at eps = 0.
- **The score** is d/deps at eps = 0 of the row's **response-token** log-likelihood (the labels
  `p48/encode.py` masks for `mode="response"`). `eps` is a per-example tensor with
  `requires_grad`, so ONE backward pass gives every row in the batch its own derivative.
- **Dropout off** (eval mode), checked.
- **It is an unnormalised dot product** with the weight delta: sum over adapted modules of
  <grad_W logP, s * B @ A>. The brief asked for a cosine; `with_cosine=True` adds the correctly
  normalised weight-space cosine, which needs each row's dense gradient norm, computed per module
  as sum_{t,u} (x_t . x_u)(delta_t . delta_u) without materialising the (out, in) gradient. Per
  plan.md it runs only if the smoke timing allows; otherwise the unnormalised score is reported
  and the substitution stated.
- **Checks** (`check_endpoints`, `check_finite_difference`, `check_dense_dot`), called by the
  smoke unit: eps = 0 reproduces the adapter-disabled model's log-likelihood, eps = 1 the trained
  adapter's, and the derivative matches a central finite difference at eps = +-K.EPS_FD within
  K.EPS_REL_TOL relative OR K.EPS_ABS_TOL absolute (both, since near zero a relative tolerance is
  meaningless). plan.md asks for the finite difference in fp32.

`dir_student` and `dir_teacher` are the same function on different adapters: the scored student's
delta for `dir_student`, and the fine-tuned teacher's (or, for a prompted teacher, the
teacher-proxy student's) for `dir_teacher`.

Rows are encoded dicts ({"input_ids", "labels"}), as `p48.likelihood.encode_rows` produces;
batching, collation and the per-row log-likelihood are shared with `p48/likelihood.py`, so the
eps = 0 endpoint check compares two numbers computed by the same code. Module discovery and the
hook pattern follow `probes/P43-subliminal-attribution/p43/tda.py` (`lora_mods`, `_check_eval`,
`_Tap`); nothing there computed a derivative along the delta.

Memory: `with_cosine=True` materialises, per module and transiently, fp32 copies of that module's
input and output gradient plus two (B, T, T) grams, so use a smaller `batch_size` for it than for
the plain score. Sibling projections (q/k/v, gate/up) share one input, so their input grams could
be computed once; they are not, to keep the optional path simple and its peak memory low.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any

from p48 import contracts as K


# --- adapter discovery ----------------------------------------------------------------------------


@dataclass
class AdapterSite:
    """One adapted projection: its base Linear, its LoRA factors and PEFT's scaling."""

    name: str
    adapter: str
    wrapper: Any
    base: Any
    lora_A: Any
    lora_B: Any
    scaling: float


def adapter_sites(model) -> list[AdapterSite]:
    """Every adapted projection, in module order (port of p43/tda.py `lora_mods`, which returned
    the lora_A / lora_B Linears separately; here one record per site, with its base layer)."""
    import torch.nn as nn

    sites: list[AdapterSite] = []
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
            raise ValueError(f"{name}: adapter is merged; the eps path needs the branch separate")
        if getattr(mod, "disable_adapters", False):
            raise ValueError(f"{name}: adapters are disabled, so the eps hook would never fire")
        A, B = la[ad], lb[ad]
        if not (isinstance(A, nn.Linear) and isinstance(B, nn.Linear)):
            raise TypeError(f"{name}: lora_A/lora_B must be nn.Linear")
        if A.bias is not None or B.bias is not None:
            raise ValueError(f"{name}: LoRA bias not supported")
        base = getattr(mod, "base_layer", None)
        if not isinstance(base, nn.Linear):
            raise TypeError(f"{name}: base_layer must be nn.Linear, got {type(base)}")
        sites.append(AdapterSite(name=name, adapter=ad, wrapper=mod, base=base, lora_A=A,
                                 lora_B=B, scaling=float(mod.scaling[ad])))
    if not sites:
        raise ValueError("no LoRA adapter sites found: is an adapter loaded?")
    return sites


def _check_eval(model) -> None:
    """Dropout must be the identity and the forward must happen once (p43/tda.py `_check_eval`)."""
    import torch.nn as nn

    if getattr(model, "is_gradient_checkpointing", False):
        raise ValueError("disable gradient checkpointing: recomputation re-fires the hooks")
    if model.training:
        raise ValueError("model must be in eval mode (LoRA dropout must be the identity)")
    for name, m in model.named_modules():
        if isinstance(m, nn.Dropout) and m.training:
            raise ValueError(f"dropout {name} is in training mode")


def delta_norm_sq(sites: list[AdapterSite]) -> float:
    """||s * B @ A||_F^2 summed over sites, via the r x r Gram matrices (never the dense delta)."""
    import torch

    total = 0.0
    with torch.no_grad():
        for s in sites:
            A = s.lora_A.weight.detach().float()  # (r, in)
            B = s.lora_B.weight.detach().float()  # (out, r)
            total += s.scaling ** 2 * float(((B.T @ B) * (A @ A.T)).sum())
    return total


@contextlib.contextmanager
def adapter_disabled(model, sites: list[AdapterSite] | None = None):
    """The base model: PEFT's own `disable_adapter()` when available, else the sites' toggles."""
    fn = getattr(model, "disable_adapter", None)
    if callable(fn):
        with fn():
            yield
        return
    sites = sites or adapter_sites(model)
    for s in sites:
        s.wrapper.enable_adapters(False)
    try:
        yield
    finally:
        for s in sites:
            s.wrapper.enable_adapters(True)


# --- the hooks ------------------------------------------------------------------------------------


class _EpsBranch:
    """Per-example eps on every adapter branch output.

    PEFT computes `result = base(x) + lora_B(lora_A(dropout(x))) * scaling`, so replacing
    `lora_B`'s output with eps * output gives exactly x @ (W_base + eps * s * B @ A)^T. The
    product is taken in fp32 and cast back, so a bf16 model keeps its dtype downstream and the
    gradient that reaches eps is fp32.
    """

    def __init__(self, sites: list[AdapterSite]):
        self.sites = sites
        self.eps = None  # (B,) tensor
        self.handles: list[Any] = []
        self.calls = 0

    def __enter__(self):
        for s in self.sites:
            self.handles.append(s.lora_B.register_forward_hook(self._hook))
        return self

    def __exit__(self, *exc):
        for h in self.handles:
            h.remove()
        self.handles.clear()

    def _hook(self, module, inp, out):
        e = self.eps
        if e is None:
            raise RuntimeError("_EpsBranch.eps was not set before the forward pass")
        if out.shape[0] != e.shape[0]:
            raise RuntimeError(f"adapter branch batch {out.shape[0]} != eps batch {e.shape[0]}")
        self.calls += 1
        view = e.reshape((-1,) + (1,) * (out.dim() - 1))
        return (out.float() * view).to(out.dtype)


class _DenseNorm:
    """Per-row ||grad_W logP||_F^2 over the adapted modules, from the SAME backward pass.

    For one module grad_W = sum_t delta_t x_t^T, so
      ||grad_W||_F^2 = sum_{t,u} (x_t . x_u)(delta_t . delta_u) = sum((X X^T) * (D D^T)),
    i.e. two (T, T) grams per row instead of an (out, in) gradient. x is the base Linear's input
    and delta the gradient of the backpropagated objective w.r.t. its output, so the objective
    must be the log-likelihood (not the loss) for the sign to match the derivative.

    When every weight is frozen, autograd would prune the graph above the last adapter site, so
    the hook on the input embeddings marks its output as requiring grad; `emb_out` is then passed
    as an extra `autograd.grad` input, which forces the whole graph to be traversed.
    """

    def __init__(self, sites: list[AdapterSite], model):
        self.sites = sites
        self.model = model
        self.mask = None  # (B, T) float
        self.sq = None  # (B,) fp32
        self.emb_out = None
        self.inputs: dict[int, Any] = {}
        self.handles: list[Any] = []
        self.calls = 0

    def __enter__(self):
        for i, s in enumerate(self.sites):
            self.handles.append(s.base.register_forward_hook(self._fwd(i)))
        emb = self.model.get_input_embeddings()
        self.handles.append(emb.register_forward_hook(self._emb))
        return self

    def __exit__(self, *exc):
        for h in self.handles:
            h.remove()
        self.handles.clear()
        self.inputs.clear()

    def start(self, attn, batch: int):
        import torch

        self.mask = attn.to(torch.float32)
        self.sq = torch.zeros(batch, dtype=torch.float32, device=attn.device)
        self.emb_out = None
        self.inputs.clear()
        self.calls = 0

    def _emb(self, module, inp, out):
        import torch

        if not torch.is_grad_enabled():
            return None
        if not out.requires_grad:
            out.requires_grad_(True)  # a leaf whenever every base weight is frozen
        self.emb_out = out
        return None

    def _fwd(self, i: int):
        import torch

        def hook(module, inp, out):
            if not torch.is_grad_enabled():
                return None
            if not out.requires_grad:
                raise RuntimeError(f"{self.sites[i].name}: output does not require grad, so the "
                                   "dense-norm hook cannot run")
            self.inputs[i] = inp[0]
            out.register_hook(self._bwd(i))
            return None

        return hook

    def _bwd(self, i: int):
        import torch

        def hook(grad):
            with torch.no_grad():
                m = self.mask[..., None]
                x = self.inputs.pop(i).float() * m
                d = grad.float() * m
                gx = x @ x.transpose(1, 2)
                gd = d @ d.transpose(1, 2)
                self.sq += (gx * gd).sum(dim=(1, 2))
                self.calls += 1

        return hook


# --- log-likelihood sweeps ------------------------------------------------------------------------


def _sweep(model, rows: list[dict], *, eps_value: float | None, sites: list[AdapterSite] | None,
           batch_size: int, device, pad_id: int) -> list[float]:
    import torch

    from p48.likelihood import batches, collate, row_loglik

    dev = device or next(model.parameters()).device
    out = [0.0] * len(rows)
    model.eval()
    with contextlib.ExitStack() as st:
        tap = None
        if eps_value is not None:
            tap = st.enter_context(_EpsBranch(sites if sites is not None
                                              else adapter_sites(model)))
        with torch.no_grad():
            for idx in batches(rows, batch_size):
                ids, attn, lab = collate([rows[i] for i in idx], dev, pad_id)
                if tap is not None:
                    tap.eps = torch.full((len(idx),), float(eps_value), dtype=torch.float32,
                                         device=dev)
                    tap.calls = 0
                lp = row_loglik(model, ids, attn, lab)
                if tap is not None and tap.calls != len(tap.sites):
                    raise RuntimeError(f"eps hook fired {tap.calls} of {len(tap.sites)} times")
                for j, i in enumerate(idx):
                    out[i] = float(lp[j])
    return out


def loglik_at_eps(model, rows: list[dict], eps_value: float, *, sites=None,
                  batch_size: int = K.GRAD_BATCH, device=None, pad_id: int = 0) -> list[float]:
    """Per-row response log-likelihood under W(eps), with the adapter branches scaled by eps."""
    return _sweep(model, rows, eps_value=eps_value, sites=sites, batch_size=batch_size,
                  device=device, pad_id=pad_id)


def plain_loglik(model, rows: list[dict], *, adapter: bool = True, sites=None,
                 batch_size: int = K.GRAD_BATCH, device=None, pad_id: int = 0) -> list[float]:
    """Per-row response log-likelihood with no eps hooks: the trained adapter (`adapter=True`) or
    the adapter-disabled base model. Same batching as `loglik_at_eps`, so the two are comparable
    row by row and any difference is not a padding artefact."""
    if adapter:
        return _sweep(model, rows, eps_value=None, sites=None, batch_size=batch_size,
                      device=device, pad_id=pad_id)
    with adapter_disabled(model, sites):
        return _sweep(model, rows, eps_value=None, sites=None, batch_size=batch_size,
                      device=device, pad_id=pad_id)


# --- the score ------------------------------------------------------------------------------------


def dir_derivative(model, rows: list[dict], *, batch_size: int = K.GRAD_BATCH, eps0: float = 0.0,
                   with_cosine: bool = False, sites=None, device=None, pad_id: int = 0,
                   log=print) -> dict:
    """d/deps of each row's response log-likelihood at eps = `eps0` (0.0 for the score).

    One forward and one backward per batch; `eps` is a per-example tensor, so `eps.grad[b]` is
    row b's own derivative. Returns

        {"name", "deriv", "loglik", "n_tokens", "grad_norm_sq", "cosine", "delta_norm", "meta"}

    where `deriv` is the unnormalised dot product <grad_W logP, s * B @ A>, `grad_norm_sq` the
    row's SQUARED dense gradient norm over the adapted modules and `cosine` the normalised
    weight-space cosine (both None unless `with_cosine`).
    """
    import torch

    from p48.likelihood import batches, collate, n_labelled, row_loglik

    sites = sites if sites is not None else adapter_sites(model)
    _check_eval(model)
    dev = device or next(model.parameters()).device
    n = len(rows)
    deriv, loglik = [0.0] * n, [0.0] * n
    gnorm: list[float] | None = [0.0] * n if with_cosine else None
    dn2 = delta_norm_sq(sites)
    t0 = time.perf_counter()
    for idx in batches(rows, batch_size):
        ids, attn, lab = collate([rows[i] for i in idx], dev, pad_id)
        eps = torch.full((len(idx),), float(eps0), dtype=torch.float32, device=dev,
                         requires_grad=True)
        with contextlib.ExitStack() as st:
            tap = st.enter_context(_EpsBranch(sites))
            tap.eps, tap.calls = eps, 0
            dn = st.enter_context(_DenseNorm(sites, model)) if with_cosine else None
            if dn is not None:
                dn.start(attn, len(idx))
            with torch.enable_grad():
                ll = row_loglik(model, ids, attn, lab)
                if tap.calls != len(sites):
                    raise RuntimeError(f"eps hook fired {tap.calls} of {len(sites)} times")
                inputs = [eps]
                if dn is not None and dn.emb_out is not None:
                    inputs.append(dn.emb_out)
                grads = torch.autograd.grad(ll.sum(), inputs)
            if dn is not None:
                if dn.calls != len(sites) or dn.inputs:
                    raise RuntimeError(f"dense-norm hook fired {dn.calls} of {len(sites)} times")
                for j, i in enumerate(idx):
                    gnorm[i] = float(dn.sq[j])
        for j, i in enumerate(idx):
            deriv[i] = float(grads[0][j])
            loglik[i] = float(ll[j])
    cosine = None
    if with_cosine:
        cosine = [d / (gn * dn2) ** 0.5 if gn > 0.0 and dn2 > 0.0 else 0.0
                  for d, gn in zip(deriv, gnorm, strict=True)]
    secs = time.perf_counter() - t0
    log(f"dir_derivative: {n} rows in {secs:.1f} s (cosine={with_cosine})")
    return {"name": "dir", "deriv": deriv, "loglik": loglik, "n_tokens": n_labelled(rows),
            "grad_norm_sq": gnorm, "cosine": cosine, "delta_norm": dn2 ** 0.5,
            "meta": {"rows": n, "sites": len(sites), "eps0": float(eps0),
                     "batch_size": batch_size, "with_cosine": with_cosine,
                     "scalings": sorted({s.scaling for s in sites}),
                     "dtype": str(next(model.parameters()).dtype), "seconds": secs}}


# --- the checks the smoke unit calls --------------------------------------------------------------


def _agree(x: list[float], y: list[float], *, rel_tol: float, abs_tol: float) -> dict:
    """Agreement of two per-row vectors: ok if every row is within rel_tol relative OR abs_tol
    absolute (both, because near zero a relative tolerance is meaningless)."""
    rows = []
    for i, (a, b) in enumerate(zip(x, y, strict=True)):
        ad = abs(a - b)
        rd = ad / max(abs(b), 1e-30)
        rows.append((i, ad, rd, ad <= abs_tol or rd <= rel_tol))
    bad = [r for r in rows if not r[3]]
    worst = max(rows, key=lambda r: min(r[1] / max(abs_tol, 1e-30), r[2] / max(rel_tol, 1e-30)))
    return {"ok": not bad, "n": len(rows), "n_bad": len(bad),
            "max_abs": max(r[1] for r in rows), "max_rel": max(r[2] for r in rows),
            "worst_row": {"idx": worst[0], "abs": worst[1], "rel": worst[2]},
            "rel_tol": rel_tol, "abs_tol": abs_tol}


def _report(name: str, out: dict, strict: bool, log) -> dict:
    log(f"{name}: ok={out['ok']} n_bad={out['n_bad']}/{out['n']} "
        f"max_abs={out['max_abs']:.3e} max_rel={out['max_rel']:.3e}")
    if strict and not out["ok"]:
        raise AssertionError(f"{name} failed: {out}")
    return out


def check_endpoints(model, rows: list[dict], *, batch_size: int = K.GRAD_BATCH, rel_tol: float =
                    1e-6, abs_tol: float = 1e-4, sites=None, device=None, pad_id: int = 0,
                    strict: bool = True, log=print) -> dict:
    """eps = 0 reproduces the adapter-disabled model's log-likelihood and eps = 1 the trained
    adapter's (plan.md "Scorers"). Both should hold to the last bit: at eps = 0 the branch adds an
    exact zero, and at eps = 1 the fp32 round trip is exact, so the default tolerance is tight."""
    sites = sites if sites is not None else adapter_sites(model)
    kw = dict(batch_size=batch_size, device=device, pad_id=pad_id)
    at0 = loglik_at_eps(model, rows, 0.0, sites=sites, **kw)
    at1 = loglik_at_eps(model, rows, 1.0, sites=sites, **kw)
    off = plain_loglik(model, rows, adapter=False, sites=sites, **kw)
    on = plain_loglik(model, rows, adapter=True, sites=sites, **kw)
    out = {"eps0_vs_base": _report("eps=0 vs adapter disabled",
                                   _agree(at0, off, rel_tol=rel_tol, abs_tol=abs_tol),
                                   strict, log),
           "eps1_vs_adapter": _report("eps=1 vs trained adapter",
                                      _agree(at1, on, rel_tol=rel_tol, abs_tol=abs_tol),
                                      strict, log),
           "loglik": {"eps0": at0, "eps1": at1, "base": off, "adapter": on}}
    out["ok"] = out["eps0_vs_base"]["ok"] and out["eps1_vs_adapter"]["ok"]
    return out


def check_finite_difference(model, rows: list[dict], *, eps: float = K.EPS_FD,
                            rel_tol: float = K.EPS_REL_TOL, abs_tol: float = K.EPS_ABS_TOL,
                            batch_size: int = K.GRAD_BATCH, sites=None, device=None,
                            pad_id: int = 0, strict: bool = True, log=print) -> dict:
    """The derivative against a central finite difference at eps = +-`eps` (plan.md: fp32).

    Both sweeps use the same length-sorted batching as the derivative, so the difference is a
    difference in eps and not in padding.
    """
    sites = sites if sites is not None else adapter_sites(model)
    kw = dict(batch_size=batch_size, device=device, pad_id=pad_id)
    d = dir_derivative(model, rows, sites=sites, log=log, **kw)["deriv"]
    plus = loglik_at_eps(model, rows, +eps, sites=sites, **kw)
    minus = loglik_at_eps(model, rows, -eps, sites=sites, **kw)
    fd = [(p - m) / (2.0 * eps) for p, m in zip(plus, minus, strict=True)]
    out = _report(f"d/deps vs central difference at eps=+-{eps:g}",
                  _agree(d, fd, rel_tol=rel_tol, abs_tol=abs_tol), strict, log)
    out.update({"eps": eps, "deriv": d, "fd": fd,
                "loglik": {"plus": plus, "minus": minus}})
    return out


def check_dense_dot(model, rows: list[dict], *, rel_tol: float = 1e-3, abs_tol: float = 1e-5,
                    sites=None, device=None, pad_id: int = 0, strict: bool = True,
                    log=print) -> dict:
    """Tiny models only: ties the score to the dot product it claims to be, and the cosine's
    denominator to a dense norm.

    Row by row, with the adapter disabled, it takes the gradient of the response log-likelihood
    w.r.t. every adapted module's **base** weight, then compares
      (a) `dir_derivative`'s score against sum_m <grad_W_m, s_m * B_m @ A_m>, and
      (b) `with_cosine`'s grad_norm_sq against sum_m ||grad_W_m||_F^2.
    Memory is O(rows x all adapted weights), so this runs on the test model, never on Qwen.
    """
    import torch

    from p48.likelihood import collate, row_loglik

    sites = sites if sites is not None else adapter_sites(model)
    _check_eval(model)
    dev = device or next(model.parameters()).device
    got = dir_derivative(model, rows, batch_size=len(rows), with_cosine=True, sites=sites,
                         device=dev, pad_id=pad_id, log=log)
    dots, norms = [], []
    flags = [(s.base.weight, s.base.weight.requires_grad) for s in sites]
    try:
        for w, _ in flags:
            w.requires_grad_(True)
        deltas = [s.scaling * (s.lora_B.weight.detach().float() @ s.lora_A.weight.detach().float())
                  for s in sites]
        for r in rows:
            with adapter_disabled(model, sites):
                ids, attn, lab = collate([r], dev, pad_id)
                with torch.enable_grad():
                    ll = row_loglik(model, ids, attn, lab).sum()
                    grads = torch.autograd.grad(ll, [s.base.weight for s in sites])
            dots.append(sum(float((g.float() * d).sum())
                            for g, d in zip(grads, deltas, strict=True)))
            norms.append(sum(float((g.float() ** 2).sum()) for g in grads))
    finally:
        for w, flag in flags:
            w.requires_grad_(flag)
    out = {"dot": _report("score vs dense <grad, delta>",
                          _agree(got["deriv"], dots, rel_tol=rel_tol, abs_tol=abs_tol),
                          strict, log),
           "norm": _report("grad_norm_sq vs dense ||grad||_F^2",
                           _agree(got["grad_norm_sq"], norms, rel_tol=rel_tol, abs_tol=abs_tol),
                           strict, log),
           "dense_dot": dots, "dense_norm_sq": norms, "score": got}
    out["ok"] = out["dot"]["ok"] and out["norm"]["ok"]
    return out
