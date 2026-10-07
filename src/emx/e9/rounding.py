"""E9 stochastic-rounding test (prereg §4.5 "Update check" -> "Rounding test"; implementation plan
v4, unit D2).

Why: the continued pretraining keeps bf16 weights with no fp32 master copy and relies on torchao
`AdamW8bit(bf16_stochastic_round=True)` to keep sub-ulp updates alive in expectation. A silent
rounding-away of updates would be an implementation failure that looks like a null result, so the
smoke pod checks the rounding against its known distribution before any production run.

What is tested (prereg §4.5, every bullet):
- For each of the three tensors in `contracts.ROUNDING_TENSORS`, one AdamW8bit step is applied
  N = `contracts.ROUNDING_DRAWS` (200) times from the SAME fixed 8-bit optimizer state and the SAME
  fixed gradient; only the rounding seed changes (draw i runs under `torch.manual_seed(i)`).
- The exact value x of each element is the same step applied to an fp32 copy of the parameter.
- lo, hi are the two bf16 values around x, ulp = hi - lo, p = (x - lo) / ulp: each draw rounds to
  hi with probability p.
- Deterministic elements (x representable in bf16, p = 0) must equal x exactly in every draw.
- Random elements: each element's mean over the N draws has expectation x and variance
  p(1 - p)·ulp²/N. Per tensor, over all random elements (or a seeded sample of
  `contracts.ROUNDING_SAMPLE_MAX` = 100,000 if there are more; numpy `default_rng(SAMPLE_SEED=0)`,
  without replacement), S = Σ(mean - x) is tested with the Bernstein bound
  P(|S| >= t) <= 2·exp(-t² / (2V + 2Mt/3)), V = Σ p(1 - p)·ulp²/N, M = max ulp / N. The tensor fails
  if the bound at t = |S| is below 0.01/3 (Bonferroni over the three tensors: family-wise false
  rejection <= `contracts.ROUNDING_FAMILY_ALPHA` = 1%).
- Every element's mean must lie in [lo, hi].
- The same step with rounding switched off (bf16 round-to-nearest) is recorded for comparison
  (the fraction of elements left unchanged), never gated.

How x is computed, and why it is "the same step" (prereg: "with the same 8-bit state convention"):
torchao's `single_param_adam` (torchao 0.18, `torchao/optim/adam.py`) upcasts the parameter and
gradient to fp32, dequantizes the optimizer state, computes the new parameter `p_f32` in fp32, and
only then writes it back: through `_fp32_to_bf16_sr(p_f32)` when the parameter is bf16 and
`bf16_stochastic_round` is on, and through a plain `copy_` otherwise. We run the identical
optimizer class on an fp32 copy of the bf16 tensor (an exact upcast), with the gradient upcast
exactly, and load into it a clone of the very state snapshot the bf16 draws start from (the same
`OptimState8bit` codes, scales and qmap; the same `step` count). The fp32 parameter takes the
non-rounding `copy_` branch, so x is `p_f32` itself, with no parameter rounding. Using the bf16
run's snapshot (instead of a fresh fp32 optimizer warmed separately) matters: torchao makes state
tensors in the parameter's dtype, so a separately warmed fp32 optimizer would hold different state.

The stochastic rounding itself (`_fp32_to_bf16_sr`): it draws a uniform 16-bit integer per element
with `torch.randint`. The step is `torch.compile`d, so Inductor lowers it to its own RNG, seeded
from the device's default generator: `torch.manual_seed(i)` fixes the draw (verified on CPU: same
seed, identical draw; different seed, different draw; the CUDA path is checked on the smoke pod by
this test itself). It rounds away from zero iff the random integer is below the fp32 value's low 16
bits, else toward zero. That is P(hi) = p exactly as defined above, for either sign, if the
integers are uniform; the test checks exactly that.

What AdamW8bit does for small tensors (`_AdamBase._new_buffer`): state is quantized
(`OptimState8bit`, block size 256, dynamic-map codes plus one fp32 scale per block) only if the
tensor has >= 4,096 elements and its element count is divisible by the block size; otherwise
`exp_avg` / `exp_avg_sq` are plain `zeros_like(p)` tensors, i.e. **bf16** for bf16 parameters, not
fp32. OLMo-3 7B's final norm has exactly 4,096 elements, so all three test tensors get 8-bit state;
the per-tensor `state_layout` in the result records which path each took. `step` is an fp32
scalar tensor. `OptimState8bit` does not support `clone`/`deepcopy`, so `clone_state` rebuilds it.

The fixed state: `warm_steps` (default 1) AdamW8bit steps with seeded random gradients, starting
from fresh state, so the snapshot holds non-trivial quantized moments (the fresh state is all
zeros). Weight decay is 0, so the state does not depend on the parameter values, and every draw
starts from the checkpoint's own bf16 tensor. Gradients are N(0, 1)·`GRAD_SCALE` drawn on the CPU
from `torch.Generator().manual_seed(seed)`, cast to bf16 (the training dtype); the warm gradients
come first, then the test gradient.

Optimizer settings mirror §4.5 (contracts CPT_*): lr 1e-5, betas (0.9, 0.95), eps 1e-8, weight
decay 0, `bf16_stochastic_round=True`. This module builds its own optimizer and does not depend on
`emx.e9.cpt`.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from emx.e9 import contracts as C

N_TENSORS = len(C.ROUNDING_TENSORS)
PER_TENSOR_ALPHA = C.ROUNDING_FAMILY_ALPHA / N_TENSORS  # 0.01 / 3
SAMPLE_SEED = 0  # numpy default_rng seed for the 100,000-element subsample
GRAD_SEED = 0  # torch CPU generator seed for the warm and test gradients
GRAD_SCALE = 1e-4  # gradient std; Adam normalizes, so only its ratio to eps (1e-8) matters
WARM_STEPS = 1
BF16_SIG_BITS = 8  # significand bits including the implicit one
BF16_MIN_ULP_EXP = -133  # spacing of bf16 subnormals (same exponent range as fp32)


# --- pure statistics (numpy, float64) ------------------------------------------------------------


def bf16_neighbors(x: Any) -> tuple[np.ndarray, np.ndarray]:
    """The two bf16 values bracketing each element of x (lo <= x <= hi), as float64 arrays.

    lo == hi == x where x is exactly representable in bf16. Computed exactly in float64: with
    |x| = m·2^e (m in [0.5, 1)), the bf16 spacing at |x| is 2^max(e - 8, -133), and truncating |x|
    to that grid is the round-toward-zero bf16 value. For fp32 inputs this equals masking the low
    16 bits, which is what torchao's stochastic rounding does. Non-finite x raises ValueError.
    """
    x = np.asarray(x, dtype=np.float64)
    if not np.all(np.isfinite(x)):
        raise ValueError("bf16_neighbors: x has non-finite values")
    a = np.abs(x)
    _, e = np.frexp(a)
    ulp = np.ldexp(1.0, np.maximum(e - BF16_SIG_BITS, BF16_MIN_ULP_EXP))
    near = np.floor(a / ulp) * ulp
    exact = near == a
    far = near + ulp
    pos = x >= 0
    lo = np.where(exact, x, np.where(pos, near, -far))
    hi = np.where(exact, x, np.where(pos, far, -near))
    return lo, hi


def bernstein_tail(t: float, v: float, m: float) -> float:
    """Bernstein bound P(|S| >= t) <= 2·exp(-t² / (2V + 2Mt/3)), capped at 1 (prereg §4.5).

    V is the sum of the centred terms' variances and M bounds each centred term's magnitude.
    A nonfinite input returns NaN, which never passes `tail >= alpha` (review R2 #6).
    """
    if not all(math.isfinite(float(a)) for a in (t, v, m)):
        return math.nan
    t = abs(float(t))
    if t == 0.0:
        return 1.0
    denom = 2.0 * v + 2.0 * m * t / 3.0
    if denom <= 0.0:
        return 0.0  # a nonzero S where no term can vary
    return min(1.0, 2.0 * math.exp(-t * t / denom))


def _finite_or_none(v: float | None) -> float | None:
    """JSON-safe float: nonfinite values are recorded as null (the verdict already failed)."""
    return v if v is None or math.isfinite(v) else None


class DrawAccumulator:
    """Streams N rounded draws of one tensor (so a 45M-element tensor never holds N copies) and
    computes the §4.5 statistics. Draws are flat float64 arrays of bf16 values (exact).

    The sum of N bf16 values of one element is exact in float64 (8-bit significands), so the mean
    is one correctly rounded division and lies in [lo, hi] exactly when every draw does.

    Nonfinite values fail the tensor with a reason, wherever they occur (review R2 #6): a nonfinite
    x, any nonfinite draw of any element (checked on ALL elements, not only the sampled subset), a
    nonfinite mean (also counted as outside [lo, hi]), or a nonfinite S, V, M or tail bound.
    """

    def __init__(self, x: Any):
        self.x = np.asarray(x, dtype=np.float64).ravel()
        self.bad_x = ~np.isfinite(self.x)
        lo, hi = bf16_neighbors(np.where(self.bad_x, 0.0, self.x))
        self.lo = np.where(self.bad_x, np.nan, lo)
        self.hi = np.where(self.bad_x, np.nan, hi)
        self.det = (lo == hi) & ~self.bad_x
        self.sum = np.zeros_like(self.x)
        self.det_bad = np.zeros(self.x.shape, dtype=bool)
        self.bad_draw = np.zeros(self.x.shape, dtype=bool)  # element had a nonfinite draw
        self.n_nonfinite_draws = 0  # element-draws that were NaN or +-Inf
        self.n_off_grid = 0  # element-draws equal to neither lo nor hi (diagnostic)
        self.n = 0

    def add(self, draw: Any) -> None:
        d = np.asarray(draw, dtype=np.float64).ravel()
        if d.shape != self.x.shape:
            raise ValueError(f"draw shape {d.shape} != x shape {self.x.shape}")
        nonfinite = ~np.isfinite(d)
        self.bad_draw |= nonfinite
        self.n_nonfinite_draws += int(np.count_nonzero(nonfinite))
        self.sum += d
        self.det_bad |= self.det & (d != self.x)
        self.n_off_grid += int(np.count_nonzero(~((d == self.lo) | (d == self.hi))))
        self.n += 1

    def result(
        self,
        *,
        alpha: float = PER_TENSOR_ALPHA,
        sample_max: int = C.ROUNDING_SAMPLE_MAX,
        sample_seed: int = SAMPLE_SEED,
    ) -> dict:
        if self.n == 0:
            raise ValueError("no draws")
        n = self.n
        with np.errstate(invalid="ignore", over="ignore"):
            mean = self.sum / n
            bad_mean = ~np.isfinite(mean) & ~self.bad_x
            rand = ~self.det & ~self.bad_x
            # written so that a NaN mean counts as outside
            outside = rand & ~((mean >= self.lo) & (mean <= self.hi))
            rand_idx = np.flatnonzero(rand)
            sampled = rand_idx.size > sample_max
            idx = rand_idx
            if sampled:
                rng = np.random.default_rng(sample_seed)
                idx = np.sort(rng.choice(rand_idx, size=sample_max, replace=False))
            x, lo, hi = self.x[idx], self.lo[idx], self.hi[idx]
            ulp = hi - lo
            p = (x - lo) / ulp if idx.size else np.zeros(0)
            s = float(np.sum(mean[idx] - x))
            v = float(np.sum(p * (1.0 - p) * ulp**2) / n)
            m = float(np.max(ulp) / n) if idx.size else 0.0
        tail = bernstein_tail(abs(s), v, m)
        stats_finite = all(math.isfinite(a) for a in (s, v, m, tail))
        reasons = []
        if self.bad_x.any():
            reasons.append(f"nonfinite x in {int(self.bad_x.sum())} elements")
        if self.n_nonfinite_draws:
            reasons.append(
                f"{self.n_nonfinite_draws} nonfinite draws in {int(self.bad_draw.sum())} elements"
            )
        if bad_mean.any():
            reasons.append(f"nonfinite mean in {int(bad_mean.sum())} elements")
        if not stats_finite:
            reasons.append("nonfinite S, V, M or tail bound")
        pass_finite = not reasons
        pass_bern = bool(stats_finite and tail >= alpha)
        pass_det = not bool(self.det_bad.any())
        pass_range = not bool(outside.any())
        if not pass_bern and stats_finite:
            reasons.append(f"Bernstein tail {tail:.3g} < alpha {alpha:.3g}")
        if not pass_det:
            reasons.append(f"{int(self.det_bad.sum())} deterministic elements moved")
        if not pass_range:
            reasons.append(f"{int(outside.sum())} means outside [lo, hi]")
        z = s / math.sqrt(v) if stats_finite and v > 0 else (0.0 if stats_finite else None)
        mean_p = float(p.mean()) if idx.size else None
        return {
            "n_draws": n,
            "n_elements": int(self.x.size),
            "n_deterministic": int(self.det.sum()),
            "n_random": int(rand_idx.size),
            "n_tested": int(idx.size),
            "sampled": bool(sampled),
            "sample_seed": sample_seed if sampled else None,
            "S": _finite_or_none(s),
            "V": _finite_or_none(v),
            "M": _finite_or_none(m),
            "bernstein_tail": _finite_or_none(tail),
            "alpha": alpha,
            "z_diagnostic": _finite_or_none(z),
            "mean_p": _finite_or_none(mean_p),
            "n_nonfinite_x": int(self.bad_x.sum()),
            "n_nonfinite_draws": self.n_nonfinite_draws,
            "n_nonfinite_means": int(bad_mean.sum()),
            "n_deterministic_violations": int(self.det_bad.sum()),
            "n_mean_outside_lo_hi": int(outside.sum()),
            "n_off_grid_draws": self.n_off_grid,
            "pass_finite": pass_finite,
            "pass_bernstein": pass_bern,
            "pass_deterministic": pass_det,
            "pass_mean_in_range": pass_range,
            "pass": bool(pass_finite and pass_bern and pass_det and pass_range),
            "fail_reasons": reasons,
        }


def rounding_stats(
    draws: Any,
    x: Any,
    *,
    alpha: float = PER_TENSOR_ALPHA,
    sample_max: int = C.ROUNDING_SAMPLE_MAX,
    sample_seed: int = SAMPLE_SEED,
) -> dict:
    """§4.5 statistics for an (N, n) array of bf16-rounded draws (as float64) and the exact x (n,).

    Pass iff the Bernstein tail at t = |S| is >= alpha (default 0.01/3), every deterministic element
    equals x in every draw, and every random element's mean lies in [lo, hi].
    """
    draws = np.asarray(draws, dtype=np.float64)
    if draws.ndim != 2:
        raise ValueError("draws must be (N, n)")
    acc = DrawAccumulator(x)
    for row in draws:
        acc.add(row)
    return acc.result(alpha=alpha, sample_max=sample_max, sample_seed=sample_seed)


# --- the optimizer experiment (torch, torchao) ---------------------------------------------------


def make_optimizer(params, *, stochastic_round: bool):
    """torchao AdamW8bit with the §4.5 recipe (contracts CPT_*); rounding switchable."""
    from torchao.optim import AdamW8bit

    return AdamW8bit(
        params,
        lr=C.CPT_LR,
        betas=C.CPT_BETAS,
        eps=C.CPT_EPS,
        weight_decay=C.CPT_WEIGHT_DECAY,
        bf16_stochastic_round=stochastic_round,
    )


def clone_state(state: dict) -> dict:
    """Independent copy of one parameter's AdamW8bit state (OptimState8bit has no clone/deepcopy)."""
    from torchao.optim.subclass_8bit import OptimState8bit

    out = {}
    for k, v in state.items():
        if isinstance(v, OptimState8bit):
            out[k] = OptimState8bit(
                v.codes.clone(), v.scale.clone(), v.qmap.clone(), v.signed, dtype=v.dtype
            )
        else:
            out[k] = v.clone()
    return out


def describe_state(state: dict) -> dict:
    """Which storage path each state entry took (8-bit quantized or plain, and its dtype)."""
    from torchao.optim.subclass_8bit import OptimState8bit

    out = {}
    for k, v in state.items():
        if isinstance(v, OptimState8bit):
            out[k] = {
                "kind": "OptimState8bit",
                "block_size": int(v.block_size),
                "signed": bool(v.signed),
                "appearance_dtype": str(v.dtype),
                "scale_dtype": str(v.scale.dtype),
            }
        elif k == "step":
            out[k] = {"kind": "step", "dtype": str(v.dtype), "value": float(v)}
        else:
            out[k] = {"kind": "plain", "dtype": str(v.dtype)}
    return out


class TensorStepper:
    """One tensor's fixed (state, gradient) pair, and the three ways of taking the step from it:
    a stochastic-rounding draw under a given seed, the exact fp32 step (x), and the bf16
    round-to-nearest step."""

    def __init__(
        self,
        param_bf16,
        *,
        warm_steps: int = WARM_STEPS,
        seed: int = GRAD_SEED,
        grad_scale: float = GRAD_SCALE,
        device=None,
    ):
        import torch

        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.base = param_bf16.detach().to(device=self.device, dtype=torch.bfloat16).clone()
        gen = torch.Generator(device="cpu").manual_seed(seed)
        grads = [
            (torch.randn(self.base.shape, generator=gen, dtype=torch.float32) * grad_scale)
            .to(torch.bfloat16)
            .to(self.device)
            for _ in range(warm_steps + 1)
        ]
        self.grad = grads[-1]
        self.p = torch.nn.Parameter(self.base.clone())
        self.opt = make_optimizer([self.p], stochastic_round=True)
        torch.manual_seed(seed)
        for g in grads[:-1]:
            self.p.grad = g
            self.opt.step()
        self.snapshot = clone_state(self.opt.state[self.p])
        self.state_layout = describe_state(self.snapshot)
        self.warm_steps, self.seed, self.grad_scale = warm_steps, seed, grad_scale

    def _step(self, p, opt, grad):
        import torch

        with torch.no_grad():
            p.copy_(self.base.to(p.dtype))
        opt.state[p] = clone_state(self.snapshot)
        p.grad = grad
        opt.step()
        return p.detach().clone()

    def draw(self, seed: int):
        """One stochastic-rounding step under torch.manual_seed(seed); bf16 tensor on device."""
        import torch

        torch.manual_seed(seed)
        return self._step(self.p, self.opt, self.grad)

    def exact(self):
        """x: the same step from the same state on an fp32 copy (no parameter rounding)."""
        import torch

        p32 = torch.nn.Parameter(self.base.float())
        opt32 = make_optimizer([p32], stochastic_round=True)  # inert: the param is fp32
        return self._step(p32, opt32, self.grad.float())

    def nearest(self):
        """The same step on bf16 with stochastic rounding off (round to nearest)."""
        import torch

        pn = torch.nn.Parameter(self.base.clone())
        optn = make_optimizer([pn], stochastic_round=False)
        return self._step(pn, optn, self.grad)


def _versions() -> dict:
    import torch
    import torchao

    v = {"torch": torch.__version__, "torchao": torchao.__version__, "numpy": np.__version__}
    if torch.cuda.is_available():
        v["cuda"] = torch.version.cuda
        v["gpu"] = torch.cuda.get_device_name(0)
    return v


def run_tensor(
    param_bf16,
    *,
    n_draws: int = C.ROUNDING_DRAWS,
    warm_steps: int = WARM_STEPS,
    seed: int = GRAD_SEED,
    name: str = "",
    device=None,
    grad_scale: float = GRAD_SCALE,
    alpha: float = PER_TENSOR_ALPHA,
    sample_max: int = C.ROUNDING_SAMPLE_MAX,
) -> dict:
    """The §4.5 rounding test on one bf16 tensor: N draws (draw i under torch.manual_seed(i)) of
    one AdamW8bit step from a fixed state and gradient, against the exact fp32 step x. Returns
    `rounding_stats` plus metadata and the (ungated) round-to-nearest comparison."""
    t0 = time.time()
    st = TensorStepper(
        param_bf16, warm_steps=warm_steps, seed=seed, grad_scale=grad_scale, device=device
    )
    x_t = st.exact()
    x = x_t.cpu().double().numpy().ravel()
    base = st.base.cpu().double().numpy().ravel()
    acc = DrawAccumulator(x)
    unchanged_sr = []
    for i in range(n_draws):
        d = st.draw(i)
        unchanged_sr.append(float((d == st.base).float().mean()))
        acc.add(d.cpu().double().numpy().ravel())
    res = acc.result(alpha=alpha, sample_max=sample_max)
    near = st.nearest()
    rand = ~acc.det & ~acc.bad_x
    ulp = (acc.hi - acc.lo)[rand]
    step_ulps = np.abs(x - base)[rand] / ulp if ulp.size else np.zeros(0)
    step_ulps = step_ulps[np.isfinite(step_ulps)]
    res.update(
        {
            "tensor": name,
            "shape": list(st.base.shape),
            "numel": int(st.base.numel()),
            "device": str(st.device),
            "warm_steps": warm_steps,
            "grad_seed": seed,
            "grad_scale": grad_scale,
            "draw_seed_range": [0, n_draws - 1],  # draw i: torch.manual_seed(i)
            "state_layout": st.state_layout,
            "comparison": {  # recorded, not gated (§4.5)
                "frac_x_changed": float(np.mean(x != base)),
                "frac_unchanged_nearest": float((near == st.base).float().mean()),
                "frac_unchanged_stochastic_mean": float(np.mean(unchanged_sr)),
                "step_size_ulps_median": float(np.median(step_ulps)) if step_ulps.size else None,
                "step_size_ulps_p99": float(np.quantile(step_ulps, 0.99))
                if step_ulps.size
                else None,
            },
            "versions": _versions(),
            "wall_s": round(time.time() - t0, 2),
        }
    )
    return res


# --- checkpoint loading and the family verdict --------------------------------------------------


def _shard_map(model: str, names: tuple[str, ...]) -> tuple[dict[str, Path], str]:
    """name -> local safetensors file holding it, downloading only the needed shards for "hub"."""
    index_name = "model.safetensors.index.json"
    if model == "hub":
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import EntryNotFoundError

        def get(fname: str) -> Path:
            return Path(hf_hub_download(C.HF_ID, fname, revision=C.REVISION))

        source = f"{C.HF_ID}@{C.REVISION}"
        try:
            index = json.loads(get(index_name).read_text())
        except EntryNotFoundError:  # single-file checkpoint
            index = None
    else:
        root = Path(model)
        source = str(root.resolve())

        def get(fname: str) -> Path:
            return root / fname

        index = (
            json.loads((root / index_name).read_text()) if (root / index_name).exists() else None
        )
    if index is None:
        f = get("model.safetensors")
        return {n: f for n in names}, source
    wm = index["weight_map"]
    missing = [n for n in names if n not in wm]
    if missing:
        raise KeyError(f"tensors not in the checkpoint index: {missing}")
    return {n: get(wm[n]) for n in names}, source


def load_tensors(model: str, names: tuple[str, ...] = C.ROUNDING_TENSORS) -> tuple[dict, dict]:
    """Load only `names` from a checkpoint directory (or "hub": contracts HF_ID @ REVISION) with
    safetensors safe_open; no full model load. Returns ({name: tensor}, provenance)."""
    from safetensors import safe_open

    files, source = _shard_map(model, tuple(names))
    out, info = {}, {"source": source, "tensors": {}}
    for n in names:
        with safe_open(str(files[n]), framework="pt") as f:
            if n not in f.keys():  # noqa: SIM118 - safe_open has no __contains__
                raise KeyError(f"{n} not in {files[n]}")
            t = f.get_tensor(n)
        out[n] = t
        info["tensors"][n] = {"file": files[n].name, "dtype": str(t.dtype), "shape": list(t.shape)}
    return out, info


def run(
    model_path_or_hub: str,
    tensors: tuple[str, ...] = C.ROUNDING_TENSORS,
    *,
    n_draws: int = C.ROUNDING_DRAWS,
    device=None,
    out: str | Path | None = None,
) -> dict:
    """The full §4.5 rounding test: each tensor (cast to bf16, the training dtype) runs
    `run_tensor`; the family passes iff all of them pass. Writes `out` (rounding.json) if given."""
    t0 = time.time()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    loaded, info = load_tensors(model_path_or_hub, tuple(tensors))
    alpha = C.ROUNDING_FAMILY_ALPHA / len(tensors)
    results = {
        n: run_tensor(loaded[n], n_draws=n_draws, name=n, device=device, alpha=alpha)
        for n in tensors
    }
    record = {
        "exp": C.EXP,
        "test": "rounding",
        "prereg": "§4.5 Update check / Rounding test",
        "model": info["source"],
        "hf_id": C.HF_ID,
        "revision": C.REVISION,
        "loaded": info["tensors"],
        "n_draws": n_draws,
        "family_alpha": C.ROUNDING_FAMILY_ALPHA,
        "per_tensor_alpha": alpha,
        "sample_max": C.ROUNDING_SAMPLE_MAX,
        "sample_seed": SAMPLE_SEED,
        "optimizer": {
            "class": "torchao.optim.AdamW8bit",
            "lr": C.CPT_LR,
            "betas": list(C.CPT_BETAS),
            "eps": C.CPT_EPS,
            "weight_decay": C.CPT_WEIGHT_DECAY,
            "bf16_stochastic_round": True,
        },
        "tensors": results,
        "pass": all(r["pass"] for r in results.values()),
        "versions": _versions(),
        "started_utc": started,
        "wall_s": round(time.time() - t0, 2),
    }
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n")
        os.replace(tmp, out)
    return record
