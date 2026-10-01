"""The token readers (logit lens, J-Lens) over the captured activation store (implementation plan
§2 U2; prereg v0.4 §4.1).

Readouts go through the benchmark's own ``LogitLens.read`` / ``JLens.read`` so every contract row
is the benchmark's (third_party/workspace-bench/src/wsbench/produce/methods.py @ 92d763e, MIT).
Rows are written in ``Producer._read_rendered`` order: items in plan order, then layers, then
positions, each row ``{"id", "layer", "pos", "token", "tokens", "scores"}`` (``Row.contract``).

``JLens.bind`` downloads the lens from the Hub, so ``bind_reader`` never calls it: it loads a
local file with the same stacking logic as wsbench ``_load_jacobians`` and sets ``_b``, ``_w_u``
and ``_jac`` the way ``bind`` does.

Next to each contract file, ``contracts.topk_path(reader, family)`` holds the top-``topk`` token
ids and raw fp32 scores per cell (``{id, layer, pos, ids, scores}``), computed with the same score
formula as the method; its first 10 ids reproduce the contract row's display tokens (any
mismatch fails). ``torch.topk`` with k=100 and k=10 may order exactly tied scores differently;
such cells take the k=10 order for their first ids and are listed in ``<topk file>.meta.json``.

Memory (27B): every read runs under ``torch.inference_mode()`` and the readers' ``_w_u`` is
detached, so the cached J-Lens denominators (one per layer, computed from a [vocab, d] fp32
product) never hold an autograd graph.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from emx.i1a import capture
from emx.i1a import contracts as C

READERS: tuple[str, ...] = ("logit_lens", "jlens")


def load_jacobians_local(path: str | Path, device: str) -> Any:
    """``J[layer, d, d]`` fp32 from a local J-lens ``.pt``: wsbench ``_load_jacobians`` without
    the ``hf_hub_download`` (same keys, same stacking, same contiguity check)."""
    import torch

    obj = torch.load(str(path), map_location="cpu", weights_only=False)
    jac = obj["J"] if "J" in obj else obj["jacobians"]
    if isinstance(jac, dict):
        layers = sorted(int(k) for k in jac)
        stacked = torch.stack([jac[L] if L in jac else jac[str(L)] for L in layers]).float()
    else:
        stacked = jac.float()
        layers = list(range(stacked.shape[0]))
    if layers != list(range(len(layers))):
        raise ValueError(f"{path}: source_layers not contiguous from 0: {layers}")
    return stacked.to(device)


def bind_reader(name: str, backend: Any, *, jlens_path: str | Path | None = None) -> Any:
    """A bound wsbench method: ``"logit_lens"`` (``LogitLens.bind``) or ``"jlens"`` (local file,
    ``jlens_path`` required; never downloads)."""
    import torch
    from wsbench.produce.methods import JLens, LogitLens

    if name == "logit_lens":
        m = LogitLens()
        with torch.inference_mode():
            m.bind(backend)
        m._w_u = m._w_u.detach()
        return m
    if name == "jlens":
        if jlens_path is None:
            raise ValueError("jlens needs a local jlens_path (JLens.bind would download)")
        m = JLens()
        m._b = backend
        m._jac = load_jacobians_local(jlens_path, backend.device)
        with torch.inference_mode():
            m._w_u = backend.unembed.detach().float()
        return m
    raise KeyError(f"unknown reader {name!r}; known {READERS}")


def bind_reader_via_wsbench(
    name: str, backend: Any, *, jlens_path: str | Path | None = None
) -> Any:
    """An independently bound reader for cross-checks: the benchmark's own ``bind`` (for J-Lens,
    wsbench ``_load_jacobians`` itself, with ``hf_hub_download`` redirected to the local file so
    nothing is downloaded). Shares no loading code with ``bind_reader``."""
    from unittest import mock

    import huggingface_hub
    import torch
    from wsbench.produce.methods import JLens, LogitLens

    if name == "logit_lens":
        m: Any = LogitLens()
    elif name == "jlens":
        if jlens_path is None:
            raise ValueError("jlens needs a local jlens_path")
        m = JLens()
    else:
        raise KeyError(f"unknown reader {name!r}; known {READERS}")
    local = str(jlens_path) if jlens_path is not None else None

    def fake_download(repo: str, filename: str, *a: Any, **kw: Any) -> str:
        if local is None:
            raise RuntimeError(f"no local file for {repo}/{filename}")
        return local

    with (
        mock.patch.object(huggingface_hub, "hf_hub_download", fake_download),
        torch.inference_mode(),
    ):
        m.bind(backend)
    m._w_u = m._w_u.detach()
    return m


def _scores(reader: Any, h: Any, layer: int) -> Any:
    """The method's full score vector over the vocabulary (the same formula as its ``read``)."""
    from wsbench.produce.methods import JLens, LogitLens

    b = reader._b
    if isinstance(reader, LogitLens):
        return b.final_norm(h.to(b.device)) @ reader._w_u.T
    if isinstance(reader, JLens):
        if layer not in reader._denom:  # the same cache JLens.read fills
            reader._denom[layer] = (reader._w_u @ reader._jac[layer]).norm(dim=1).clamp_min(1e-9)
        return ((h.to(b.device) @ reader._jac[layer].T) @ reader._w_u.T) / reader._denom[layer]
    raise TypeError(f"no score formula for {type(reader).__name__}")


def default_arm(reader_name: str, family: str) -> tuple[str, str]:
    """``(arm, family file)``: ``multihop_oldsite`` goes to ``<reader>_oldsite/multihop.jsonl``,
    ``jlens_ref`` to ``<reader>_ref/jlens_ref.jsonl``, a gate family to ``<reader>/<family>``."""
    if family == C.OLDSITE_FAMILY:
        return f"{reader_name}_oldsite", "multihop"
    if family == C.JLENS_REF_FAMILY:
        return f"{reader_name}_ref", family
    return reader_name, family


def read_family(
    reader: Any,
    backend: Any,
    family: str,
    *,
    arm: str | None = None,
    topk: int = 100,
    limit: int = 0,
) -> Path:
    """Contract rows for every cell of a captured family through ``reader.read`` (written
    atomically to ``readouts_path(arm, family file)``) plus top-``topk`` ids and scores to
    ``topk_path(reader.name, family)``. ``limit`` (tests) keeps the first items' sites."""
    import torch
    from wsbench.produce.producer import Row
    from wsbench.produce.render import display_tokens

    layers = capture.family_layers(family)
    rows, h = capture.load_acts(family, layers)  # [n, len(layers), d] fp32
    if any(r.index != i for i, r in enumerate(rows)):
        raise ValueError(f"{family}: index rows are not 0..n-1 in order")
    if limit:
        keep = set(list(dict.fromkeys(r.id for r in rows))[:limit])
        rows = [r for r in rows if r.id in keep]
    arm_name, fam_file = default_arm(reader.name, family)
    arm_name = arm or arm_name
    out = C.readouts_path(arm_name, fam_file)
    tk_out = C.topk_path(reader.name, family)
    out.parent.mkdir(parents=True, exist_ok=True)
    tk_out.parent.mkdir(parents=True, exist_ok=True)
    contract_lines: list[str] = []
    topk_lines: list[str] = []
    mismatches: list[dict[str, Any]] = []
    tie_resolved: list[dict[str, Any]] = []
    by_item: dict[str, list[C.SiteRow]] = {}
    for r in rows:
        by_item.setdefault(r.id, []).append(r)
    with torch.inference_mode():
        for item_rows in by_item.values():
            for li, layer in enumerate(layers):
                for r in item_rows:
                    v = h[r.index, li]
                    ro = reader.read(v, layer)
                    row = Row(r.id, layer, r.pos, r.token, ro).contract()  # wsbench's own row
                    contract_lines.append(json.dumps(row, ensure_ascii=False))
                    sc = _scores(reader, v, layer)
                    vals, ids = torch.topk(sc, topk)
                    k = len(ro.tokens)
                    v_k, i_k = torch.topk(sc, k)  # the call read() makes
                    ids_l = ids.tolist()
                    cell = {"id": r.id, "layer": layer, "pos": r.pos}
                    if ids_l[:k] != i_k.tolist():
                        if not torch.equal(vals[:k], v_k):
                            mismatches.append(cell)
                        ids_l[:k] = i_k.tolist()  # same scores: exact ties, take read()'s order
                        tie_resolved.append(cell)
                    if display_tokens(backend.tokenizer, ids_l[:k]) != ro.tokens:
                        mismatches.append(cell)
                    topk_lines.append(
                        json.dumps(
                            {
                                "id": r.id,
                                "layer": layer,
                                "pos": r.pos,
                                "ids": ids_l,
                                "scores": [float(x) for x in vals.tolist()],
                            }
                        )
                    )
    if mismatches:
        raise AssertionError(
            f"{len(mismatches)} cells: top-{topk} ids disagree with {reader.name}.read, "
            f"e.g. {mismatches[:3]}"
        )
    for path, lines in ((out, contract_lines), (tk_out, topk_lines)):
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        os.replace(tmp, path)
    meta = {"n_cells": len(topk_lines), "topk": topk, "tie_resolved": tie_resolved}
    topk_meta_path(reader.name, family).write_text(json.dumps(meta, indent=1) + "\n")
    return out


def topk_meta_path(reader_name: str, family: str) -> Path:
    """Sidecar of the top-k file: cell count and the cells whose first ids were tie-resolved."""
    return C.topk_path(reader_name, family).with_suffix(".meta.json")
