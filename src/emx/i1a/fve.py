"""Joint bullet FVE: the Oracle Lens RL reward, scored with the prompt-tag reconstructor ("AR")
mounted as a second named adapter on the one shared 27B base.

Ported (not imported: the vendored package needs jaxtyping and a training stack) from
third_party/olens_draft/oracle_lens @ a32590085a6070d75f950155d3c2b3db3eec97ef (MIT). The math is
kept identical; each function cites its source. What differs is only *where* the AR runs:

- olens_draft loads a second 27B copy, truncates it at block 63 with the final norm replaced by
  identity (``core/reconstructor.py`` ``truncate_backbone``), compile-wraps the blocks so the
  saved ``_orig_mod.`` LoRA keys match (``pipeline/ar_loader.py`` ``load_ptag_reconstructor``)
  and reads ``last_hidden_state`` at the last real token (``pipeline/multilayer_reconstructor.py``
  ``PromptTagReconstructor``).
- Two 27B copies do not fit in 80 GB, so here the AR LoRA keys are remapped
  ``base_model.model.layers.N._orig_mod.<rest>`` -> ``base_model.model.model.layers.N.<rest>``
  and loaded as adapter ``"ar"`` next to the Oracle Lens adapter on wsbench's ``Backend`` model.
  A forward hook on ``blocks[63]`` with ``"ar"`` active captures the block output, which equals
  the truncated backbone's ``last_hidden_state`` (truncation at 63 deletes no block of a 64-block
  model, and the final norm it neutralizes is exactly what the hook skips).

Span convention (from ``pipeline/multilayer.py`` ``CroppedPairs``): the AR maps a span to the
residual at the position *immediately preceding* the span ("sample a position, take the next N
tokens"). The Oracle Lens bullets are scored the same way: each bullet is a phrase whose image
should match the activation that would precede it.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import torch
from torch import Tensor, nn

from emx.i1a import contracts as C

AR_ALL_LAYERS: tuple[int, ...] = (*range(0, 61, 4), 63)  # olens_draft pipeline/multilayer.LAYERS
PAD_ID = 0  # rl_reward.score_texts_joint pad_id (right padding, so the value never matters)
BARE_MAX_LEN = 1024  # scorer.bare_token_ids max_len

# --------------------------------------------------------------------------- text -> phrases

# third_party/olens_draft/oracle_lens/readout_text.py STRIP_PATTERNS (verbatim)
STRIP_PATTERNS = re.compile(
    r"</?explanation>|<\|im_start\|>(system|user|assistant)?|<\|im_end\|>|<think>|</think>"
)


def strip_scaffolding(text: str) -> str:
    """Strip chat scaffolding + ``<explanation>`` tags (olens_draft ``readout_text.py``)."""
    return STRIP_PATTERNS.sub("", text).strip()


def split_bullets(text: str, k_max: int = C.K_MAX_BULLETS) -> list[str]:
    """<= k_max ``- `` bullet phrases; no bullet lines -> the whole stripped text as one phrase;
    nothing left -> []. Port of olens_draft ``pipeline/rl_reward.py`` ``split_bullets``."""
    t = strip_scaffolding(text)
    bullets = [
        ln.strip()[2:].strip()
        for ln in t.splitlines()
        if ln.strip().startswith("- ") and len(ln.strip()) > 2
    ]
    bullets = [b for b in bullets if b][:k_max]
    if bullets:
        return bullets
    whole = t.strip()
    return [whole] if whole else []


def bare_token_ids(tokenizer: Any, text: str, *, max_len: int = BARE_MAX_LEN) -> list[int]:
    """No chat template, no special tokens (olens_draft ``pipeline/scorer.py`` ``bare_token_ids``)."""
    cleaned = strip_scaffolding(text)
    ids: list[int] = tokenizer(cleaned, add_special_tokens=False)["input_ids"]
    special = set(tokenizer.all_special_ids)
    ids = [i for i in ids if i not in special]
    return ids[:max_len]


def reward_text_ids(tokenizer: Any, text: str, *, max_span: int = C.AR_SPAN_WIDTH) -> list[int]:
    """Bare ids truncated to the AR span width (olens_draft ``rl_reward.reward_text_ids``)."""
    return bare_token_ids(tokenizer, text)[:max_span]


# --------------------------------------------------------------------------- whitening


def whitening_matrix(cov: Tensor, ridge_c: float = C.RIDGE_C) -> Tensor:
    """W = (Σ + λI)^(-1/2) via eigh in fp64, returned fp32; λ = ridge_c·mean(diag Σ) >= 1e-12,
    eigenvalues clamped >= 0 (olens_draft ``core/whitening.py`` ``whitening_matrix``)."""
    cov64 = cov.to(torch.float64)
    cov64 = 0.5 * (cov64 + cov64.T)
    lam = torch.clamp_min(ridge_c * cov64.diagonal().mean(), 1e-12)
    eigvals, eigvecs = torch.linalg.eigh(cov64)
    inv_sqrt = (eigvals.clamp_min(0.0) + lam).rsqrt()
    w = (eigvecs * inv_sqrt.unsqueeze(0)) @ eigvecs.T
    return w.to(torch.float32)


@dataclass(frozen=True)
class Whitener:
    """whiten(x) = (x - μ) @ Wᵀ (olens_draft ``core/whitening.py`` ``Whitener``)."""

    mu: Tensor
    w: Tensor
    ridge_c: float

    def whiten(self, x: Tensor) -> Tensor:
        return (x.float() - self.mu.to(x.device)) @ self.w.to(x.device).T

    def to(self, device: str | torch.device) -> Whitener:
        return Whitener(mu=self.mu.to(device), w=self.w.to(device), ridge_c=self.ridge_c)


def load_whitener(path: Path, *, ridge_c: float = C.RIDGE_C, device: str = "cpu") -> Whitener:
    """``{mu, cov}`` safetensors -> Whitener (olens_draft ``core/whitening.py`` ``load_whitener``)."""
    from safetensors.torch import load_file

    t = load_file(str(path), device="cpu")
    # the fp64 eigh runs on the target device: on the P0 pod's CPU, 12 layers of 5120^2 took
    # longer than 20 min (2026-09-30); on an H100 each takes about a second. Same math.
    w = whitening_matrix(t["cov"].to(device), ridge_c=ridge_c)
    return Whitener(mu=t["mu"].float().to(device), w=w.to(device), ridge_c=ridge_c)


def load_whiteners(
    dir: Path,
    layers: Sequence[int] = C.AR_LAYERS,
    prefix: str = C.WHITENER_PREFIX,
    ridge_c: float = C.RIDGE_C,
    *,
    device: str = "cpu",
) -> dict[int, Whitener]:
    """One whitener per layer from ``<dir>/<prefix>_L{layer}.safetensors``
    (olens_draft ``pipeline/ar_loader.py`` ``load_ladder_whiteners``)."""
    out: dict[int, Whitener] = {}
    for layer in layers:
        path = Path(dir) / f"{prefix}_L{layer}.safetensors"
        if not path.exists():
            raise FileNotFoundError(f"whitener missing: {path}")
        out[int(layer)] = load_whitener(path, ridge_c=ridge_c, device=device)
    return out


# --------------------------------------------------------------------------- NNLS


def _nnls_batched(dsel: Tensor, x: Tensor, valid: Tensor, *, iters: int = C.NNLS_ITERS) -> Tensor:
    """Projected-gradient NNLS on the k x k Gram, step 1/trace(G), invalid slots masked to 0
    (olens_draft ``core/nnomp.py`` ``_nnls_batched``)."""
    gram = torch.bmm(dsel, dsel.transpose(1, 2))
    rhs = torch.bmm(dsel, x.unsqueeze(-1)).squeeze(-1)
    step = 1.0 / gram.diagonal(dim1=1, dim2=2).sum(-1).clamp_min(1e-12)
    w = torch.zeros_like(rhs)
    vf = valid.to(dsel.dtype)
    for _ in range(iters):
        grad = torch.bmm(gram, w.unsqueeze(-1)).squeeze(-1) - rhs
        w = ((w - step.unsqueeze(-1) * grad) * vf).clamp_min(0.0)
    return w


def nnls_refit(vectors: Tensor, x: Tensor, valid: Tensor) -> tuple[Tensor, Tensor]:
    """[b,k,d] vectors, [b,d] x, [b,k] valid -> (coeffs [b,k], FVE [b]) with
    FVE = 1 - ||x - recon||² / ||x||² (olens_draft ``core/nnomp.py`` ``nnls_refit``)."""
    w = _nnls_batched(vectors, x, valid)
    recon = torch.bmm(w.unsqueeze(1), vectors).squeeze(1)
    fve = 1.0 - ((x - recon) ** 2).sum(-1) / (x**2).sum(-1).clamp_min(1e-12)
    return w, fve


# --------------------------------------------------------------------------- AR loading


class ReconstructorHead(nn.Module):
    """Optional no-affine LayerNorm, then Linear(d, d), fp32
    (olens_draft ``core/reconstructor.py`` ``ReconstructorHead``)."""

    def __init__(self, d_model: int, *, layer_norm: bool = False) -> None:
        super().__init__()
        self.norm = nn.LayerNorm(d_model, elementwise_affine=False) if layer_norm else None
        self.linear = nn.Linear(d_model, d_model, bias=True)

    def forward(self, x: Tensor) -> Tensor:
        h = x.float()
        if self.norm is not None:
            h = self.norm(h)
        return self.linear(h)


@dataclass
class ArHandle:
    """The AR mounted on a shared backend: adapter name, trained layers, tags and head."""

    backend: Any  # wsbench Backend; backend.model is a peft.PeftModel carrying `adapter_name`
    adapter_name: str
    layers: tuple[int, ...]
    tag_ids: Tensor  # [n_layers, T_tag] long, row i = tag of layers[i]
    head: ReconstructorHead
    layer_norm: bool
    n_live: int
    n_saved: int
    meta: dict[str, Any] = field(default_factory=dict)

    def row_of(self, layer: int) -> int:
        try:
            return self.layers.index(int(layer))
        except ValueError:
            raise KeyError(f"layer {layer} is not an AR layer {self.layers}") from None

    @property
    def tokenizer(self) -> Any:
        return self.backend.tokenizer


_ORIG_RE = re.compile(r"^base_model\.model\.layers\.(\d+)\._orig_mod\.")
_PLAIN_PREFIX = "base_model.model.model.layers."


def remap_ar_key(key: str) -> str:
    """``base_model.model.layers.N._orig_mod.<rest>`` -> ``base_model.model.model.layers.N.<rest>``
    (the truncated, compiled inner model's names -> the full CausalLM's). Already-plain keys pass
    through; anything else raises, so an unknown layout can never load silently inert."""
    m = _ORIG_RE.match(key)
    if m:
        return f"{_PLAIN_PREFIX}{m.group(1)}.{key[m.end() :]}"
    if key.startswith(_PLAIN_PREFIX):
        return key
    raise ValueError(f"unexpected AR adapter key layout: {key}")


def _remap_adapter_dir(lora_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Write a remapped copy of ``lora_dir`` (config + safetensors) to ``out_dir``."""
    from safetensors.torch import load_file, save_file

    cfg = json.loads((lora_dir / "adapter_config.json").read_text())
    tm = cfg.get("target_modules")
    if not tm:
        raise ValueError(f"{lora_dir}: adapter_config has no target_modules")
    if isinstance(tm, str):  # a regex: drop the compile wrapper from it too
        cfg["target_modules"] = tm.replace(r"\._orig_mod", "").replace("._orig_mod", "")
    if cfg.get("modules_to_save"):
        raise NotImplementedError(f"{lora_dir}: modules_to_save is not supported on a shared base")
    tensors = load_file(str(lora_dir / "adapter_model.safetensors"))
    remapped = {remap_ar_key(k): v for k, v in tensors.items()}
    if len(remapped) != len(tensors):
        raise ValueError(f"{lora_dir}: key remap collided")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "adapter_config.json").write_text(json.dumps(cfg, indent=1))
    save_file(remapped, str(out_dir / "adapter_model.safetensors"))
    return cfg


def _live_name(key: str, adapter: str) -> str:
    """Saved PEFT key -> the live parameter name of adapter ``adapter``."""
    m = re.search(r"\.(lora_[A-Za-z_]+)\.(weight|bias)$", key)
    if m:
        return f"{key[: m.start()]}.{m.group(1)}.{adapter}.{m.group(2)}"
    m = re.search(r"\.(lora_embedding_[AB]|lora_magnitude_vector)$", key)
    if m:
        return f"{key}.{adapter}"
    return key


def verify_adapter_live(model: Any, st_path: Path, adapter: str) -> tuple[int, int]:
    """Count saved LoRA tensors that landed on live parameters of ``adapter`` with the saved
    values; raise if fewer than ``C.AR_MIN_LIVE_FRAC`` (all of them). Value-based (stricter than olens_draft
    ``pipeline/ar_loader.py`` ``verify_adapter_live``, which matches names only): an inert load
    leaves the adapter at its init values, so it fails here even if the names happened to exist."""
    from safetensors.torch import load_file

    params = dict(model.named_parameters())
    saved = load_file(str(st_path))
    unexpected = sorted(k for k in saved if "lora_" not in k)
    if unexpected:  # a changed checkpoint layout must not pass on its LoRA subset alone
        raise RuntimeError(
            f"{st_path}: {len(unexpected)} unexpected non-LoRA tensors, e.g. {unexpected[:3]}"
        )
    if not saved:
        raise RuntimeError(f"{st_path}: no lora_ tensors saved")
    matched = 0
    for k, v in saved.items():
        p = params.get(_live_name(k, adapter))
        if p is None or tuple(p.shape) != tuple(v.shape):
            continue
        if torch.allclose(p.detach().float().cpu(), v.float(), rtol=1e-3, atol=1e-6):
            matched += 1
    if matched < C.AR_MIN_LIVE_FRAC * len(saved):
        raise RuntimeError(
            f"adapter {adapter!r} is inert: only {matched}/{len(saved)} saved LoRA tensors map "
            f"onto live parameters ({st_path})"
        )
    return matched, len(saved)


def _active(model: Any) -> str | None:
    a = getattr(model, "active_adapter", None)
    if isinstance(a, list):
        return a[0] if len(a) == 1 else None
    return a


def load_ar(
    backend: Any, ar_dir: Path, adapter_name: str = "ar", *, remap: bool = True
) -> ArHandle:
    """Mount the prompt-tag AR on the backend's model as adapter ``adapter_name``.

    ``ar_dir`` is the published ``ar_ptag_pooled/`` dir (``heads.pt``, ``meta.json``, ``lora/``).
    The LoRA keys are remapped (see module docstring) into a temp dir, loaded with
    ``load_adapter`` (or ``PeftModel.from_pretrained`` if the model is not yet a PeftModel), and
    verified live (every saved tensor, ``C.AR_MIN_LIVE_FRAC``). ``remap=False`` exists only to test that an
    unremapped load is caught as inert. The previously active adapter stays active.
    Head and layers per olens_draft ``pipeline/ar_loader.py`` ``load_ptag_reconstructor``.
    """
    from peft import PeftModel

    ar_dir = Path(ar_dir)
    meta = json.loads((ar_dir / "meta.json").read_text())
    cfg = meta["config"]
    if cfg.get("head_mode") != "prompt_tag":
        raise ValueError(f"{ar_dir}: head_mode {cfg.get('head_mode')!r}, expected prompt_tag")
    layers = tuple(AR_ALL_LAYERS[int(i)] for i in cfg["layer_indices"])
    state = torch.load(ar_dir / "heads.pt", map_location="cpu", weights_only=True)
    if "tag_ids" not in state:
        raise ValueError(f"{ar_dir}/heads.pt has no tag_ids (not a prompt-tag AR)")
    if state.get("layers") and tuple(int(x) for x in state["layers"]) != layers:
        raise ValueError(f"heads.pt layers {state['layers']} != meta layers {layers}")
    tag_ids = state["tag_ids"].long().cpu()
    if tag_ids.shape[0] != len(layers):
        raise ValueError(f"tag_ids has {tag_ids.shape[0]} rows for {len(layers)} layers")

    model = backend.model
    lora_dir = ar_dir / "lora"
    from safetensors import safe_open

    with safe_open(str(lora_dir / "adapter_model.safetensors"), framework="pt") as f:
        unexpected = sorted(k for k in f.keys() if "lora_" not in k)  # noqa: SIM118
    if unexpected:  # PEFT would copy these into the SHARED base weights (the reader's)
        raise RuntimeError(
            f"{lora_dir}: {len(unexpected)} unexpected non-LoRA tensors, e.g. {unexpected[:3]}"
        )
    import time

    def _t(label: str, t0: float) -> float:  # sub-step timing (load_ar took 13 min at P0)
        print(f"[load_ar] {label} {time.time() - t0:.1f}s", flush=True)
        return time.time()

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="ar_remap_") as tmp:
        if remap:
            src = Path(tmp) / "lora"
            _remap_adapter_dir(lora_dir, src)
        else:
            src = Path(tmp) / "lora"
            shutil.copytree(lora_dir, src)
        t0 = _t("remap", t0)
        if isinstance(model, PeftModel):
            if adapter_name in model.peft_config:
                raise ValueError(f"adapter {adapter_name!r} already loaded")
            prev = _active(model)
            model.load_adapter(str(src), adapter_name=adapter_name)
            if prev is not None and _active(model) != prev:
                model.set_adapter(prev)
        else:
            model = PeftModel.from_pretrained(model, str(src), adapter_name=adapter_name)
            backend.model = model
        model.eval()
        t0 = _t("load_adapter", t0)
        try:
            n_live, n_saved = verify_adapter_live(
                model, src / "adapter_model.safetensors", adapter_name
            )
        except RuntimeError:
            if len(model.peft_config) > 1:  # leave the reader's model as it was
                model.delete_adapter(adapter_name)
            raise
        t0 = _t("verify_adapter_live", t0)

    d_model = int(state["head"]["linear.weight"].shape[1])
    if d_model != int(backend.unembed.shape[1]):
        raise ValueError(f"head width {d_model} != model width {backend.unembed.shape[1]}")
    layer_norm = bool(cfg.get("head_layer_norm", True))
    head = ReconstructorHead(d_model, layer_norm=layer_norm)
    head.load_state_dict(state["head"])
    head = head.float().to(backend.device).eval()
    return ArHandle(
        backend=backend,
        adapter_name=adapter_name,
        layers=layers,
        tag_ids=tag_ids,
        head=head,
        layer_norm=layer_norm,
        n_live=n_live,
        n_saved=n_saved,
        meta=meta,
    )


@contextmanager
def using_adapter(model: Any, name: str) -> Iterator[None]:
    """Activate adapter ``name`` for the block; restore the previously active one afterwards."""
    prev = _active(model)
    if prev != name:
        model.set_adapter(name)
    try:
        yield
    finally:
        if prev is not None and prev != name:
            model.set_adapter(prev)


# --------------------------------------------------------------------------- AR forward


def ar_predict(
    h: ArHandle, id_rows: list[list[int]], layers: Sequence[int], *, micro_batch: int = 32
) -> Tensor:
    """Raw-space AR predictions [n, d] (fp32, CPU) for spans ``id_rows`` at ``layers``.

    Per row: ``tag_ids[row(layer)] + ids[:64]``, right-padded (pad 0) with an attention mask,
    adapter ``h.adapter_name`` active, forward hook on ``blocks[63]`` read at the last real token
    (``mask.sum - 1``), then the head in fp32. Rows are length-sorted into micro-batches.
    Mirrors ``PromptTagReconstructor._tagged`` / ``_read_final`` + ``ml_collate``.
    """
    if len(id_rows) != len(layers):
        raise ValueError("id_rows and layers differ in length")
    b = h.backend
    n = len(id_rows)
    d = int(h.head.linear.in_features)
    out = torch.zeros(n, d, dtype=torch.float32)
    if n == 0:
        return out
    rows: list[list[int]] = []
    for ids, layer in zip(id_rows, layers, strict=True):
        span = list(ids)[: C.AR_SPAN_WIDTH]
        if not span:
            raise ValueError("empty span: filter empty phrases before ar_predict")
        rows.append(h.tag_ids[h.row_of(layer)].tolist() + span)
    order = sorted(range(n), key=lambda i: len(rows[i]))
    block = b.blocks[C.AR_READ_BLOCK]
    model = b.model
    captured: dict[str, Tensor] = {}

    def hook(_m: Any, _i: Any, o: Any) -> None:
        captured["h"] = o[0] if isinstance(o, tuple) else o

    with torch.no_grad(), using_adapter(model, h.adapter_name):
        for lo in range(0, n, micro_batch):
            sel = order[lo : lo + micro_batch]
            width = max(len(rows[i]) for i in sel)
            ids_t = torch.full((len(sel), width), PAD_ID, dtype=torch.long)
            mask = torch.zeros(len(sel), width, dtype=torch.long)
            for r, i in enumerate(sel):
                ids_t[r, : len(rows[i])] = torch.tensor(rows[i], dtype=torch.long)
                mask[r, : len(rows[i])] = 1
            ids_t, mask = ids_t.to(b.device), mask.to(b.device)
            captured.clear()
            handle = block.register_forward_hook(hook)
            try:
                model(input_ids=ids_t, attention_mask=mask, use_cache=False, logits_to_keep=1)
            finally:
                handle.remove()
            hs = captured["h"]
            last = mask.sum(dim=1) - 1
            x = hs[torch.arange(len(sel), device=hs.device), last].float()
            pred = h.head(x.to(next(h.head.parameters()).device))
            out[torch.tensor(sel)] = pred.float().cpu()
    return out


# --------------------------------------------------------------------------- FVE


def bullet_images(
    h: ArHandle,
    texts: list[str],
    layers: Sequence[int],
    *,
    k_max: int = C.K_MAX_BULLETS,
    micro_batch: int = 32,
) -> tuple[Tensor, Tensor]:
    """Raw-space AR images of each text's bullets: ([n, k_max, d], valid [n, k_max]).
    Phrases whose bare ids are empty never reach the AR (rl_reward.score_texts_joint)."""
    n = len(texts)
    d = int(h.head.linear.in_features)
    per_row = [split_bullets(t, k_max) for t in texts]
    slots: list[tuple[int, int]] = []  # (row, slot) of each AR input
    id_rows: list[list[int]] = []
    for i, phrases in enumerate(per_row):
        kk = 0
        for ph in phrases:
            ids = reward_text_ids(h.tokenizer, ph)
            if not ids:
                continue
            slots.append((i, kk))
            id_rows.append(ids)
            kk += 1
    vecs = torch.zeros(n, k_max, d)
    valid = torch.zeros(n, k_max, dtype=torch.bool)
    if id_rows:
        preds = ar_predict(h, id_rows, [layers[i] for i, _ in slots], micro_batch=micro_batch)
        for j, (i, kk) in enumerate(slots):
            vecs[i, kk] = preds[j]
            valid[i, kk] = True
    return vecs, valid


def joint_fve_from_images(
    vecs: Tensor,
    valid: Tensor,
    golds: Tensor,
    layers: Sequence[int],
    whiteners: dict[int, Whitener],
) -> tuple[Tensor, Tensor, Tensor]:
    """Whiten golds and bullet images per layer, unit-normalize the images, NNLS-refit ->
    (joint FVE [n], n_bullets [n], has [n]); rows with no bullet score 0.0
    (rl_reward.score_texts_joint with the default whitened, unit-norm space)."""
    n = vecs.shape[0]
    fve = torch.zeros(n)
    lay = torch.tensor([int(x) for x in layers])
    g_all = golds.float().cpu()
    for ly in sorted(set(lay.tolist())):
        rows = torch.nonzero(lay == ly).squeeze(-1)
        w = whiteners[int(ly)]
        dev = w.w.device
        gw = w.whiten(g_all[rows].to(dev))
        v = vecs[rows].to(dev)
        r, k, d = v.shape
        vw = w.whiten(v.reshape(r * k, d)).reshape(r, k, d)
        vw = vw / (vw.norm(dim=-1, keepdim=True) + 1e-9)
        vm = valid[rows].to(dev)
        vw = vw * vm.unsqueeze(-1).to(vw.dtype)  # empty slots are zero vectors, as upstream
        _, f = nnls_refit(vw, gw, vm)
        fve[rows] = f.float().cpu()
    has = valid.any(dim=1)
    fve = torch.where(has, fve, torch.zeros_like(fve))
    return fve, valid.sum(dim=1), has


def joint_bullet_fve(
    h: ArHandle,
    whiteners: dict[int, Whitener],
    texts: list[str],
    golds: Tensor,
    layers: Sequence[int],
    *,
    k_max: int = C.K_MAX_BULLETS,
    micro_batch: int = 32,
) -> list[tuple[float, int, bool]]:
    """(joint FVE, n_bullets, valid) per text against its own gold activation."""
    if not (len(texts) == golds.shape[0] == len(layers)):
        raise ValueError("texts, golds and layers differ in length")
    vecs, valid = bullet_images(h, texts, layers, k_max=k_max, micro_batch=micro_batch)
    fve, nb, has = joint_fve_from_images(vecs, valid, golds, layers, whiteners)
    return [(float(f), int(k), bool(v)) for f, k, v in zip(fve, nb, has, strict=True)]


def span_fve(
    h: ArHandle,
    whiteners: dict[int, Whitener],
    id_rows: list[list[int]],
    golds: Tensor,
    layers: Sequence[int],
    *,
    micro_batch: int = 32,
) -> Tensor:
    """Single-span FVE = cos² between whitened prediction and whitened gold, each row in its own
    layer's space (olens_draft ``rl_reward.score_rows`` ``.fve``)."""
    preds = ar_predict(h, id_rows, layers, micro_batch=micro_batch)
    lay = torch.tensor([int(x) for x in layers])
    cos = torch.full((len(id_rows),), float("nan"))
    for ly in sorted(set(lay.tolist())):
        rows = torch.nonzero(lay == ly).squeeze(-1)
        w = whiteners[int(ly)]
        dev = w.w.device
        p = w.whiten(preds[rows].to(dev))
        g = w.whiten(golds[rows].float().to(dev))
        cos[rows] = nn.functional.cosine_similarity(p, g, dim=-1).float().cpu()
    return cos**2


# --------------------------------------------------------------------------- family scoring


def read_samples(family: str) -> list[C.SampleRow]:
    path = C.samples_path(family)
    rows = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(C.SampleRow(**json.loads(line)))
    return rows


def read_fve(family: str) -> list[C.FveRow]:
    path = C.fve_path(family)
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            if line.strip():
                out.append(C.FveRow(**json.loads(line)))
    return out


def _fve_key(r: Any) -> tuple[str, int, int, int]:
    return (r.id, int(r.layer), int(r.pos), int(r.sample_idx))


def gold_lookup(
    acts: tuple[list[C.SiteRow], Tensor], layers: Sequence[int]
) -> tuple[dict[int, int], dict[int, int], Tensor]:
    """(site index -> store row, layer -> column, H [n, L, d]) for an injected activation store."""
    sites, H = acts
    if H.shape[0] != len(sites) or H.shape[1] != len(layers):
        raise ValueError(f"acts {tuple(H.shape)} do not match {len(sites)} sites x {len(layers)}")
    return {s.index: r for r, s in enumerate(sites)}, {int(L): j for j, L in enumerate(layers)}, H


def score_family(
    h: ArHandle,
    whiteners: dict[int, Whitener],
    family: str,
    *,
    acts: tuple[list[C.SiteRow], Tensor],
    layers: Sequence[int],
    ids: set[str] | None = None,
    chunk: int = 512,
    micro_batch: int = 32,
) -> Path:
    """Append one FveRow per SampleRow of ``family`` (optionally only item ``ids``) to
    ``fve_path(family)``; rows already there are skipped, so a rerun resumes.

    ``acts`` is the family's activation store as ``(SiteRow list, H [n, len(layers), d])`` with
    ``layers`` naming H's columns (e.g. the capture unit's ``load_acts(family, layers)``)."""
    row_of_site, col_of_layer, H = gold_lookup(acts, layers)
    path = C.fve_path(family)
    done = {_fve_key(r) for r in read_fve(family)}
    todo: list[C.SampleRow] = []
    seen: dict[tuple[str, int, int, int], int] = {}
    for s in read_samples(family):
        k = _fve_key(s)
        if seen.setdefault(k, s.site_index) != s.site_index:
            raise ValueError(f"{family}: two sites share the FveRow key {k}")
        if k in done or (ids is not None and s.id not in ids):
            continue
        done.add(k)  # a duplicated SampleRow is scored once
        todo.append(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for lo in range(0, len(todo), chunk):
            part = todo[lo : lo + chunk]
            golds = torch.stack(
                [H[row_of_site[s.site_index], col_of_layer[int(s.layer)]] for s in part]
            )
            res = joint_bullet_fve(
                h,
                whiteners,
                [s.text for s in part],
                golds,
                [int(s.layer) for s in part],
                micro_batch=micro_batch,
            )
            for s, (fv, nb, ok) in zip(part, res, strict=True):
                row = C.FveRow(
                    family=family,
                    id=s.id,
                    layer=int(s.layer),
                    pos=int(s.pos),
                    sample_idx=int(s.sample_idx),
                    joint_fve=fv,
                    n_bullets=nb,
                    valid=ok,
                )
                f.write(C.to_json(row) + "\n")
            f.flush()
    return path
