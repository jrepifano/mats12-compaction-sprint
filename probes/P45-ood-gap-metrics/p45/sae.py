"""P45 TopK sparse autoencoder: training, scoring and per-prompt F (plan "Setup: T2", frozen recipe).

Recipe (Gao et al. 2024, "Scaling and evaluating sparse autoencoders", TopK variant):
- inputs scaled by one scalar s so that E||s x||^2 = d_in over the training buffer (`fit_scale`);
- z = TopK(ReLU((x - b_pre) @ W_enc + b_enc)), x_hat = z @ W_dec + b_pre (TopK is taken on the
  pre-activations and ReLU applied to the kept values, which is the same thing);
- b_pre initialised to the (scaled) training mean, decoder rows random unit-norm (seeded), encoder =
  decoder transpose, b_enc = 0;
- Adam (0.9, 0.999), lr linear warm-up then constant; a seeded permutation of the rows each epoch;
- after every step the decoder rows are renormalised to unit norm, and before every step the gradient
  component parallel to each decoder row is removed (Gao's projection), so the step does not just
  fight the renormalisation;
- AuxK: the residual e = x - x_hat (detached) is reconstructed (no bias) from the top-k_aux
  pre-activations among latents that have not fired in the last N tokens (N = one epoch);
- fp32 compute (TF32 matmuls allowed on CUDA during training only).

Loss scaling (documented deviation in form, not in effect): the main term is the squared error summed
over dims and averaged over the batch, in the scaled space (as the plan states). Gao's code uses
nmse_main + aux_coef * nmse_aux, with nmse_aux = ||e - e_hat||^2 / ||e||^2. We use
mse_main + aux_coef * nmse_aux * mean||x||^2 (the batch's mean scaled squared norm, detached, ~ d_in),
i.e. Gao's objective (up to batch-level vs per-row normalisation of the main term) multiplied by a
constant. Adam is invariant to a constant loss scale, so the updates match Gao's.

Scores are in RAW residual units: `sq_err` returns ||x_raw - x_hat / s||^2 per row (fp32), and F
divides by sigma2 = E_ID ||x - mu_ID||^2 of that model and layer (passed in, not taken from the buffer).

Torch is imported at module level: runs in the pod's /ws/venv-peft and in the laptop tests.
"""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import torch

from p45 import contracts as K

CURVE_EVERY = 50  # steps between loss / fvu_batch curve points
_CHUNK = 8192  # rows per chunk for statistics and scoring


@dataclass
class TrainCfg:
    n_latents: int = K.N_LATENTS
    k: int = K.TOPK
    k_aux: int = K.K_AUX
    aux_coef: float = K.AUX_COEF
    lr: float = K.LR
    warmup: int = K.WARMUP_STEPS
    batch: int = K.BATCH_TOKENS
    epochs: int = K.EPOCHS
    eval_every: int = K.EVAL_EVERY
    seed: int = K.SEED_SAE
    max_steps: int | None = None  # smoke cap


class TopKSAE(torch.nn.Module):
    """TopK SAE with a fixed input scale. Parameters are fp32; W_dec is [n_latents, d_in]."""

    def __init__(self, d_in: int, n_latents: int, k: int, scale: float = 1.0,
                 b_pre: torch.Tensor | None = None):
        super().__init__()
        self.d_in, self.n_latents, self.k, self.scale = int(d_in), int(n_latents), int(k), float(scale)
        self.W_enc = torch.nn.Parameter(torch.zeros(d_in, n_latents))
        self.b_enc = torch.nn.Parameter(torch.zeros(n_latents))
        self.W_dec = torch.nn.Parameter(torch.zeros(n_latents, d_in))
        bp = torch.zeros(d_in) if b_pre is None else b_pre.detach().to(torch.float32).reshape(d_in)
        self.b_pre = torch.nn.Parameter(bp.clone())

    # --- core (scaled space) ---------------------------------------------------------------------

    def _x(self, x_raw: torch.Tensor) -> torch.Tensor:
        return x_raw.to(self.W_enc.device, torch.float32) * self.scale

    def _pre(self, x: torch.Tensor) -> torch.Tensor:
        return (x - self.b_pre) @ self.W_enc + self.b_enc

    def _topk(self, pre: torch.Tensor, k: int) -> tuple[torch.Tensor, torch.Tensor]:
        vals, idx = pre.topk(k, dim=-1, sorted=False)
        return torch.relu(vals), idx

    def _decode(self, vals: torch.Tensor, idx: torch.Tensor, bias: bool = True) -> torch.Tensor:
        z = torch.zeros(vals.shape[0], self.n_latents, device=vals.device, dtype=vals.dtype)
        z = z.scatter(1, idx, vals)
        out = z @ self.W_dec
        return out + self.b_pre if bias else out

    # --- public API (raw units in, raw units out) ------------------------------------------------

    def encode(self, x_raw: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """(topk values, indices), each [T, k]. Values are >= 0 (ReLU after TopK)."""
        return self._topk(self._pre(self._x(x_raw)), self.k)

    def reconstruct(self, x_raw: torch.Tensor) -> torch.Tensor:
        """x_hat in raw units, float32."""
        vals, idx = self.encode(x_raw)
        return self._decode(vals, idx) / self.scale

    @torch.no_grad()
    def sq_err(self, x_raw: torch.Tensor, chunk: int = _CHUNK) -> torch.Tensor:
        """[T] float32 on CPU: ||x_raw - x_hat_raw||^2 per row, chunked, no grad."""
        out = []
        for a in range(0, x_raw.shape[0], chunk):
            x = x_raw[a:a + chunk].to(self.W_enc.device, torch.float32)
            out.append(((x - self.reconstruct(x)) ** 2).sum(1).cpu())
        return torch.cat(out) if out else torch.zeros(0)

    def save(self, stem: str | Path, info: dict | None = None) -> None:
        """stem.safetensors (fp16 W_enc, b_enc, W_dec, b_pre) and stem.json (config + info)."""
        from safetensors.torch import save_file

        stem = Path(stem)
        stem.parent.mkdir(parents=True, exist_ok=True)
        tensors = {n: p.detach().to("cpu", torch.float16).contiguous()
                   for n, p in self.named_parameters()}
        save_file(tensors, str(stem.with_suffix(".safetensors")))
        cfg = {"d_in": self.d_in, "n_latents": self.n_latents, "k": self.k, "scale": self.scale,
               "dtype": "float16", "info": info or {}}
        stem.with_suffix(".json").write_text(json.dumps(cfg, indent=1, sort_keys=True))

    @classmethod
    def load(cls, stem, device="cpu") -> TopKSAE:
        from safetensors.torch import load_file

        stem = Path(stem)
        cfg = json.loads(stem.with_suffix(".json").read_text())
        sae = cls(cfg["d_in"], cfg["n_latents"], cfg["k"], scale=cfg["scale"])
        sd = {n: t.to(torch.float32) for n, t in load_file(str(stem.with_suffix(".safetensors"))).items()}
        sae.load_state_dict(sd)
        return sae.to(device)


# --- statistics --------------------------------------------------------------------------------


def _chunks(buf: torch.Tensor, chunk: int = _CHUNK):
    for a in range(0, buf.shape[0], chunk):
        yield buf[a:a + chunk].to(torch.float64)


def _mean_rows(buf: torch.Tensor) -> torch.Tensor:
    s = torch.zeros(buf.shape[1], dtype=torch.float64)
    for x in _chunks(buf):
        s += x.sum(0).cpu()
    return s / buf.shape[0]


def fit_scale(buf: torch.Tensor, d_in: int | None = None) -> float:
    """s with E||s x||^2 = d_in over buf rows (float64 accumulate, chunked)."""
    d_in = buf.shape[1] if d_in is None else d_in
    tot = 0.0
    for x in _chunks(buf):
        tot += float((x ** 2).sum())
    return math.sqrt(d_in / (tot / buf.shape[0]))


def sigma2_of(buf: torch.Tensor) -> float:
    """E||x - mean(x)||^2 over rows (summed over dims), float64 accumulate, chunked (two passes)."""
    mu = _mean_rows(buf)
    tot = 0.0
    for x in _chunks(buf):
        tot += float(((x - mu.to(x.device)) ** 2).sum())
    return tot / buf.shape[0]


def mask_keep(norms: torch.Tensor, median_norm: float, mult: float = K.MASK_NORM_MULT) -> torch.Tensor:
    """bool, True = keep (norm <= mult * median_norm)."""
    return norms <= mult * median_norm


def prompt_F(sq_err: torch.Tensor, keep: torch.Tensor, segs: list[tuple[int, int]],
             sigma2: float) -> list[float]:
    """Per segment [a, b) of token rows: mean(sq_err[a:b][keep[a:b]]) / sigma2; NaN if none kept."""
    e = sq_err.detach().to("cpu", torch.float64)
    kp = keep.detach().to("cpu", torch.bool)
    cs_e = torch.cat([torch.zeros(1, dtype=torch.float64), torch.cumsum(e * kp, 0)])
    cs_n = torch.cat([torch.zeros(1, dtype=torch.int64), torch.cumsum(kp.to(torch.int64), 0)])
    out = []
    for a, b in segs:
        n = int(cs_n[b] - cs_n[a])
        out.append(float(cs_e[b] - cs_e[a]) / n / sigma2 if n > 0 else float("nan"))
    return out


def agg_fvu(sae: TopKSAE, x_raw: torch.Tensor, sigma2: float) -> float:
    """Aggregate FVU = sum sq_err / (n_rows * sigma2), sigma2 in raw units."""
    if x_raw.shape[0] == 0:
        return float("nan")
    return float(sae.sq_err(x_raw).to(torch.float64).sum()) / (x_raw.shape[0] * sigma2)


# --- training ----------------------------------------------------------------------------------


@contextmanager
def _tf32(device):
    if torch.device(device).type != "cuda":
        yield
        return
    old = torch.get_float32_matmul_precision()
    torch.set_float32_matmul_precision("high")
    try:
        yield
    finally:
        torch.set_float32_matmul_precision(old)


def _init_sae(d: int, cfg: TrainCfg, scale: float, b_pre: torch.Tensor, gen: torch.Generator):
    sae = TopKSAE(d, cfg.n_latents, cfg.k, scale=scale, b_pre=b_pre)
    W = torch.randn(cfg.n_latents, d, generator=gen)
    W /= W.norm(dim=1, keepdim=True)
    with torch.no_grad():
        sae.W_dec.copy_(W)
        sae.W_enc.copy_(W.T)
    return sae


class _Stepper:
    """One optimizer step of the recipe, plus dead-latent bookkeeping (tokens since last fired)."""

    def __init__(self, sae: TopKSAE, cfg: TrainCfg, dead_tokens: int):
        self.sae, self.cfg, self.dead_tokens = sae, cfg, int(dead_tokens)
        self.opt = torch.optim.Adam(sae.parameters(), lr=cfg.lr, betas=(0.9, 0.999))
        dev = sae.W_enc.device
        self.since = torch.zeros(cfg.n_latents, dtype=torch.int64, device=dev)
        self.seen = 0
        self.n_steps = 0

    def dead(self) -> torch.Tensor:
        """Latents not fired in the last min(dead_tokens, tokens seen) tokens."""
        return self.since >= max(1, min(self.dead_tokens, self.seen))

    def step(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """x: scaled fp32 batch on device. Returns (loss, mse) as 0-d tensors (no host sync)."""
        sae, cfg = self.sae, self.cfg
        lr = cfg.lr * min(1.0, (self.n_steps + 1) / max(1, cfg.warmup))
        for g in self.opt.param_groups:
            g["lr"] = lr
        pre = sae._pre(x)
        vals, idx = sae._topk(pre, cfg.k)
        x_hat = sae._decode(vals, idx)
        err = x - x_hat
        mse = (err ** 2).sum(1).mean()
        loss = mse
        if cfg.aux_coef > 0 and self.seen >= self.dead_tokens:
            # No host sync: picks beyond the dead set are -inf pre-activations, which ReLU zeroes
            # (no gradient); with no dead latent at all the term is multiplied by 0.
            dead = self.since >= self.dead_tokens
            pre_d = pre.masked_fill(~dead, float("-inf"))
            av, ai = sae._topk(pre_d, min(cfg.k_aux, cfg.n_latents))
            e = err.detach()
            e_hat = sae._decode(av, ai, bias=False)
            aux = ((e_hat - e) ** 2).sum(1).mean() / (e ** 2).sum(1).mean().clamp_min(1e-12)
            any_dead = dead.any().to(aux.dtype)
            loss = loss + cfg.aux_coef * any_dead * aux * (x ** 2).sum(1).mean().detach()
        self.opt.zero_grad(set_to_none=True)
        loss.backward()
        with torch.no_grad():
            W, G = sae.W_dec, sae.W_dec.grad
            G -= (G * W).sum(1, keepdim=True) * W  # rows of W are unit-norm
        self.opt.step()
        with torch.no_grad():
            sae.W_dec /= sae.W_dec.norm(dim=1, keepdim=True).clamp_min(1e-12)
            cnt = torch.zeros(cfg.n_latents, dtype=torch.int64, device=x.device)
            cnt.scatter_add_(0, idx.reshape(-1), (vals > 0).reshape(-1).to(torch.int64))
            self.since = torch.where(cnt > 0, torch.zeros_like(self.since),
                                     self.since + x.shape[0])
        self.seen += x.shape[0]
        self.n_steps += 1
        return loss.detach(), mse.detach()


def _sync(device):
    if torch.device(device).type == "cuda":
        torch.cuda.synchronize()


def _place(buf: torch.Tensor, device) -> torch.Tensor:
    """Move buf to device once if it fits (CUDA: free memory > bytes + 16 GB); else keep it put."""
    dev = torch.device(device)
    if buf.device == dev or dev.type == "cpu":
        return buf
    nbytes = buf.numel() * buf.element_size()
    if dev.type == "cuda":
        free, _ = torch.cuda.mem_get_info(dev)
        if free > nbytes + (16 << 30):
            return buf.to(dev)
        return buf
    return buf.to(dev)


def train_sae(buf: torch.Tensor, heldout: torch.Tensor, cfg: TrainCfg, device, sigma2: float,
              log=print) -> tuple[TopKSAE, dict]:
    """Train one TopK SAE on buf [N, d] (any float dtype, CPU, device or memmap-backed).

    sigma2 is the ID-token variance of that model and layer in raw units (not necessarily buf's).
    Returns (sae on device, curves dict; JSON-serialisable)."""
    t0 = time.time()
    N, d = buf.shape
    scale = fit_scale(buf)
    b_pre = (_mean_rows(buf) * scale).to(torch.float32)
    gen = torch.Generator().manual_seed(cfg.seed)
    sae = _init_sae(d, cfg, scale, b_pre, gen).to(device)
    data = _place(buf, device)
    st = _Stepper(sae, cfg, dead_tokens=N)
    per_epoch = math.ceil(N / cfg.batch)
    steps = cfg.epochs * per_epoch
    if cfg.max_steps is not None:
        steps = min(steps, cfg.max_steps)
    cur = {"step": [], "loss": [], "fvu_batch": [], "eval_step": [], "fvu_heldout": [],
           "dead_frac": []}
    log(f"[sae] N={N} d={d} n_latents={cfg.n_latents} k={cfg.k} steps={steps} scale={scale:.5g} "
        f"buf_on={data.device}")
    train_s = eval_s = 0.0
    s = 0
    with _tf32(device):
        t_loop = time.time()
        for ep in range(cfg.epochs):
            if s >= steps:
                break
            perm = torch.randperm(N, generator=gen)
            for b in range(per_epoch):
                if s >= steps:
                    break
                idx = perm[b * cfg.batch:(b + 1) * cfg.batch].sort().values
                if data.device.type != "cpu":
                    idx = idx.to(data.device)
                x = data[idx].to(device, torch.float32, non_blocking=True) * scale
                loss, mse = st.step(x)
                s += 1
                if s % CURVE_EVERY == 0 or s == steps:
                    cur["step"].append(s)
                    cur["loss"].append(float(loss))
                    cur["fvu_batch"].append(float(mse) / (scale ** 2 * sigma2))
                if s % cfg.eval_every == 0 or s == steps:
                    _sync(device)
                    te = time.time()
                    fh = agg_fvu(sae, heldout, sigma2)
                    df = float(st.dead().float().mean())
                    cur["eval_step"].append(s)
                    cur["fvu_heldout"].append(fh)
                    cur["dead_frac"].append(df)
                    eval_s += time.time() - te
                    log(f"[sae] step {s}/{steps} loss={float(loss):.4g} "
                        f"fvu_batch={cur['fvu_batch'][-1] if cur['fvu_batch'] else float('nan'):.4f} "
                        f"fvu_heldout={fh:.4f} dead={df:.3f}")
        _sync(device)
        train_s = time.time() - t_loop - eval_s
    sae.eval()
    final = {
        "fvu_train": agg_fvu(sae, data, sigma2),
        "fvu_heldout": agg_fvu(sae, heldout, sigma2),
        "dead_frac": float(st.dead().float().mean()),
        "steps": s,
        "seconds": time.time() - t0,
        "ms_per_step": 1000.0 * train_s / max(1, s),
        "eval_seconds": eval_s,
        "scale": scale,
        "n_train_tokens": int(N),
        "n_heldout_tokens": int(heldout.shape[0]),
    }
    cur["final"] = final
    log(f"[sae] done: {json.dumps(final)}")
    return sae, cur


def bench(d_in: int, n_latents: int, k: int, batch: int, device, steps: int = 20) -> float:
    """ms per optimizer step of the training recipe on random data (AuxK active), after 3 warm-ups."""
    cfg = TrainCfg(n_latents=n_latents, k=k, k_aux=min(K.K_AUX, n_latents), batch=batch, warmup=1)
    gen = torch.Generator().manual_seed(0)
    sae = _init_sae(d_in, cfg, 1.0, torch.zeros(d_in), gen).to(device)
    st = _Stepper(sae, cfg, dead_tokens=1)  # every latent counts as dead after one batch: AuxK runs
    x = torch.randn(batch, d_in, generator=gen).to(device)
    with _tf32(device):
        for _ in range(3):
            st.step(x)
        _sync(device)
        t = time.time()
        for _ in range(steps):
            st.step(x)
        _sync(device)
    return 1000.0 * (time.time() - t) / steps
