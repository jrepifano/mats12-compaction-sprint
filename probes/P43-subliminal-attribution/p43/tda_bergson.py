"""Independent EK-FAC cross-check with EleutherAI's Bergson (MIT; PyPI `bergson` 2.2.3).

Route taken for P43 (2026-10-13): the `ekfac:*` scores come from p43/tda.py's own EK-FAC (the
working repo's analytic EK-FAC generalised to rank 8 and tested against a brute-force EK-FAC).
Bergson is used here only to fit the same factors independently on a subset of rows and modules
and to compare the preconditioned vectors, as the working repo did with kronfluence. Why not
Bergson for the full run: its EK-FAC (`HessianConfig(method="kfac", ev_correction=True)`)
requires uncompressed gradients and allocates every module's full Kronecker factors on the
device; on Llama-3.1-8B with r=8 on all 7 projections that is ~79 GB of fp32 accumulators for the
96 factors of width 14,336 alone (down_proj input; gate/up output), so it needs
`module_partitions` (one extra pair of passes per partition) plus ~96 eigendecompositions of
14,336^2, well past the ~40 min budget. Its pipeline is also CLI/disk-centred (IndexConfig,
safetensors shards), whereas the per-row scores need our in-memory query vectors.

Install recipe (checked 2026-10-13 in a CPU venv with the pod pins torch 2.13.0, transformers
5.17.0, peft 0.21.1, accelerate 1.15.0, datasets 4.3.0, numpy 2.3.5, trl 0.24.0; nothing pinned
was changed). torchvision / torchaudio / torchopt / bitsandbytes are declared dependencies but
are not imported by the Hessian path (torchopt only by bergson.magic):

    pip install --no-deps bergson==2.2.3
    pip install jaxtyping simple-parsing natsort petname ml_dtypes pyarrow scipy pyyaml jinja2

Bergson's conventions, matched by p43/tda.py: covariances over each row's real tokens except
its last; loss_reduction "sum" (per-row summed CE); dataset labels (empirical Fisher,
`use_dataset_labels=True`); eigenvalue corrections = mean over rows of (Q_S^T G Q_A)^2.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import numpy as np
import torch

from p43 import tda

BERGSON_VERSION = "2.2.3"


def available() -> tuple[bool, str]:
    """Whether Bergson's EK-FAC Hessian path imports in this environment, and why not."""
    try:
        import bergson
        from bergson.config.config import HessianConfig, IndexConfig  # noqa: F401
        from bergson.hessians.eigenvectors import eigendecompose_owned  # noqa: F401
        from bergson.hessians.hessian_approximations import collect_hessians  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"import failed: {type(e).__name__}: {e}"
    ver = getattr(bergson, "__version__", "?")
    if ver != BERGSON_VERSION:
        return True, f"bergson {ver} (checked against {BERGSON_VERSION})"
    return True, f"bergson {ver}"


def _bergson_name(key: str, adapter: str) -> str:
    """p43.tda module key -> Bergson target name (relative to PeftModel.base_model)."""
    return f"{key.removeprefix('base_model.')}.{adapter}"


def fit_factors(model, rows: list[dict], keys: list[str], *, batch_size: int = 16,
                max_batch_tokens: int | None = 8192, hessian_dtype: str = "fp64",
                log=print) -> dict[str, tuple[torch.Tensor, torch.Tensor, torch.Tensor]]:
    """Bergson EK-FAC factors for the given p43.tda module keys on `rows`.

    Returns {key: (Q_A (i x i), Q_S (o x o), lambda (o x i))}, Bergson's eigenvectors (columns)
    and eigenvalue corrections, in Bergson's layout (= p43.tda's: rotated G = Q_S^T G Q_A)."""
    from bergson.config.config import HessianConfig, IndexConfig
    from bergson.hessians.eigenvectors import eigendecompose_owned
    from bergson.hessians.hessian_approximations import collect_hessians
    from datasets import Dataset

    adapter = next(iter(getattr(model, "active_adapters", ["default"])))
    names = {_bergson_name(k, adapter): k for k in keys}
    found = {n for n, _ in model.base_model.named_modules()}
    missing = sorted(set(names) - found)
    if missing:
        raise KeyError(f"Bergson target names not found: {missing[:3]}")
    ds = Dataset.from_list([{"input_ids": r["input_ids"], "labels": r["labels"]} for r in rows])
    batches = tda.batch_plan([len(r["input_ids"]) for r in rows], batch_size, max_batch_tokens)
    hc = HessianConfig(method="kfac", ev_correction=True, use_dataset_labels=True,
                       hessian_dtype=hessian_dtype)
    with tempfile.TemporaryDirectory() as tmp:
        idx = IndexConfig(run_path=str(Path(tmp) / "run"), loss_reduction="sum")
        idx.partial_run_path.mkdir(parents=True)
        path = str(Path(tmp) / "factors")
        common = {"model": model, "data": ds, "index_cfg": idx, "batches": batches,
                  "target_modules": set(names), "attention_cfgs": {}, "hessian_cfg": hc,
                  "path": path}
        cov = collect_hessians(**common)
        total = torch.load(idx.partial_run_path / "total_processed.pt", weights_only=False)
        eva, _ = eigendecompose_owned(cov.A_cov_dict, cov.A_shapes, None, total,
                                      str(Path(path) / "eig_a"), cov.dtype)
        evg, _ = eigendecompose_owned(cov.S_cov_dict, cov.S_shapes, None, total,
                                      str(Path(path) / "eig_g"), cov.dtype)
        collect_hessians(**common, ev_correction=True, num_documents=len(ds),
                         eigenvectors=(eva, evg))
        from safetensors.torch import load_file

        lam = load_file(str(Path(path) / "eigenvalue_correction_sharded" / "shard_0.safetensors"))
    log(f"[bergson] fitted {len(names)} modules on {len(rows)} rows ({int(total)} tokens)")
    return {k: (eva[n].cpu(), evg[n].cpu(), lam[n].cpu()) for n, k in names.items()}


def _fitted_keys(mods, ours) -> list[str]:
    """Modules whose both factor sides p43.tda fitted (Bergson has no identity sides)."""
    return [m.key for i, m in enumerate(mods)
            if ours["Qa"][i] is not None and ours["Qg"][i] is not None]


def _keys_in_layers(keys: list[str], layers) -> list[str]:
    """`layers`: None = all; "ends" = first, middle and last decoder layer; or layer indices."""
    if layers is None:
        return keys
    lid = {k: int(re.search(r"layers\.(\d+)\.", k).group(1)) for k in keys}
    if layers == "ends":
        n = max(lid.values()) + 1
        layers = sorted({0, n // 2, n - 1})
    return [k for k in keys if lid[k] in set(layers)]


def crosscheck(model, rows: list[dict], *, device=None, max_factor_dim: int =
               tda.DEFAULT_MAX_FACTOR_DIM, layers="ends", dampings=(1e-3, 1e-2, 1e-1),
               batch_size: int = 16, log=print) -> dict:
    """Fit EK-FAC with p43.tda and with Bergson on the same rows and modules, and compare the
    preconditioned vectors (H + lam I)^-1 v for v = the summed gradient of the first 8 rows.

    Returns cosine similarities (overall and the minimum over modules) and the max relative
    error per damping; agreement near 1 means the two implementations fit the same EK-FAC."""
    mods, _ = tda.lora_mods(model)
    only = set(_keys_in_layers([m.key for m in mods], layers))
    ours_all = tda.fit_ekfac(model, rows, batch_size=batch_size, max_factor_dim=max_factor_dim,
                             device=device, only=only)
    keys = _fitted_keys(mods, ours_all)
    theirs = fit_factors(model, rows, keys, batch_size=batch_size, log=log)
    pos = {m.key: i for i, m in enumerate(mods)}
    # probe vector: summed gradient of the first rows (a realistic direction)
    dev = mods[0].lin.weight.device
    ids, attn, labels = tda.collate(rows[:8], dev)
    loss = tda.row_losses(model, ids, attn, labels).sum()
    v = [g.float() for g in torch.autograd.grad(loss, [m.lin.weight for m in mods])]
    out = {"n_rows": len(rows), "n_modules": len(keys), "dampings": {}}
    for d in dampings:
        mins, num, den, dots, n1, n2 = 1.0, 0.0, 0.0, 0.0, 0.0, 0.0
        for k in keys:
            i = pos[k]
            h1 = tda.ekfac_precondition(v[i].double(), ours_all["Qg"][i].double(),
                                        ours_all["Qa"][i].double(),
                                        ours_all["lam"][i].double(), d)
            Qa, Qg, lam = (t.to(dev, torch.float64) for t in theirs[k])
            h2 = tda.ekfac_precondition(v[i].double(), Qg, Qa, lam, d)
            c = float((h1 * h2).sum() / (h1.norm() * h2.norm()).clamp_min(1e-300))
            mins = min(mins, c)
            num = max(num, float((h1 - h2).abs().max()))
            den = max(den, float(h2.abs().max()))
            dots += float((h1 * h2).sum())
            n1 += float((h1 ** 2).sum())
            n2 += float((h2 ** 2).sum())
        out["dampings"][f"{d:g}"] = {"cos_overall": dots / max(np.sqrt(n1 * n2), 1e-300),
                                     "min_cos_module": mins,
                                     "max_rel_err": num / max(den, 1e-300)}
    out["min_cos_preconditioned"] = min(x["min_cos_module"] for x in out["dampings"].values())
    log(f"[bergson] cross-check on {len(keys)} modules: min module cosine "
        f"{out['min_cos_preconditioned']:.6f}")
    return out
