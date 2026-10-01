"""Activation capture for I1a+ (implementation plan §2 U2; prereg v0.4 §4.1, §4.7).

Every read site of a family is rendered and resolved with the benchmark's own code
(``wsbench.produce.render.render``, ``wsbench.readplan.resolve`` via
``wsbench.produce.producer._positions``), and captured with ``wsbench.produce.backend
.Backend.capture`` (one forward per item, the residual stream after each decoder block). ``pos``
is reported exactly as ``Producer._read_rendered`` reports it (negative for
``offset_from_end``), and ``token`` is ``rendered.decoded[abs_pos]`` (wsbench ``Row.token``).

Sources: third_party/workspace-bench/src/wsbench/produce/{backend,render,producer}.py and
readplan.py @ 92d763e (MIT).

Site families beyond the three gate families:

- ``jlens_ref``: the 149 ``jlens_concept_pr`` labels that are hallucination ids, rendered from the
  hallucination ``capture_rows.json`` input ids ("captured") at the manifest's ``eval_positions``
  (in the manifest's order). Used by ``checks_readers.check_jlens_reference``.
- ``multihop_oldsite``: the 33 multihop items whose plan rule is ``offset_from_end``, read at
  ``final_token`` instead (their pre-2026-09-23 read site), for the reserve diagnostic.

The activation store is ``contracts.acts_path(family)`` (safetensors, key ``"h"``,
[n_sites, 64, d] bfloat16) plus ``contracts.acts_index_path(family)`` (one ``SiteRow`` per line).
bf16 is lossless only for the real bf16 model; the tiny fp32 test model is rounded on store.

Import-light: torch, transformers and safetensors are imported inside functions.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from emx.i1a import contracts as C

EXTRA_FAMILIES: tuple[str, ...] = (C.JLENS_REF_FAMILY, C.OLDSITE_FAMILY)
SITE_FAMILIES: tuple[str, ...] = (*C.FAMILIES, *EXTRA_FAMILIES)
N_SITES_ALL: dict[str, int] = {
    **C.N_SITES,
    C.JLENS_REF_FAMILY: C.N_JLENS_REF_SITES,
    C.OLDSITE_FAMILY: C.N_OLDSITE_ITEMS,
}


# --------------------------------------------------------------------- backend


def load_backend(model_dir: str | Path, *, device: str = "cuda", dtype: str = C.DTYPE) -> Any:
    """wsbench ``Backend`` around a local model directory (downloaded by the caller at
    ``contracts.MODEL_REV``). Like ``Backend.load`` but with the attention implementation pinned
    to sdpa (prereg §4.1: never FlashAttention-4) and no network access."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from wsbench.produce.backend import Backend

    model_dir = str(model_dir)
    tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        dtype=getattr(torch, dtype),
        attn_implementation=C.ATTN_IMPLEMENTATION,
        device_map=device,
        local_files_only=True,
    )
    model.eval()
    impl = model.config._attn_implementation
    assert impl in ("sdpa", "eager"), f"attention implementation {impl!r} is not sdpa/eager"
    return Backend(model=model, tokenizer=tok, device=device, model_id=C.MODEL_ID)


# --------------------------------------------------------------------- plans and sites


def write_plans(out_dir: Path | None = None) -> dict[str, int]:
    """The benchmark's read plan of each gate family to ``<out_dir>/<family>.jsonl`` via
    ``wsbench.readplan.write``; returns the row count per family."""
    from wsbench import readplan

    out_dir = Path(out_dir) if out_dir is not None else C.PLAN_DIR
    return {f: readplan.write(readplan.plan(f), out_dir / f"{f}.jsonl") for f in C.FAMILIES}


def family_specs(family: str) -> list[Any]:
    """The ``ReadSpec`` rows of a site family, in plan order."""
    from wsbench import readplan
    from wsbench.readplan import ReadSpec
    from wsbench.registry import REPO_ROOT

    if family in C.FAMILIES:
        return readplan.plan(family)
    if family == C.OLDSITE_FAMILY:
        moved = [s for s in readplan.plan("multihop") if s.positions["kind"] == "offset_from_end"]
        if len(moved) != C.N_OLDSITE_ITEMS:
            raise AssertionError(f"{len(moved)} offset_from_end multihop items, expected 33")
        return [
            ReadSpec(
                C.OLDSITE_FAMILY,
                s.id,
                s.render,
                {"kind": "final_token"},
                list(C.SITE_FAMILY_LAYERS[C.OLDSITE_FAMILY]),
                text=s.text,
                prefill=s.prefill,
                note=f"old read site (final_token); plan rule today {s.positions}",
            )
            for s in moved
        ]
    if family == C.JLENS_REF_FAMILY:
        rows = json.loads((REPO_ROOT / "evals/hallucination/capture_rows.json").read_text())
        by_id = {r["id"]: r for r in rows}
        manifest = json.loads((REPO_ROOT / "evals/jlens_concept_pr/manifest.json").read_text())
        out = []
        for p in manifest["prompts"]:
            r = by_id.get(p["label"])
            if r is None:
                continue
            if p["n_pos"] != len(r["input_ids"]):
                raise AssertionError(f"{p['label']}: n_pos {p['n_pos']} != len(input_ids)")
            out.append(
                ReadSpec(
                    C.JLENS_REF_FAMILY,
                    p["label"],
                    "captured",
                    {"kind": "positions", "positions": p["eval_positions"]},
                    list(C.SITE_FAMILY_LAYERS[C.JLENS_REF_FAMILY]),
                    extra={"input_ids": r["input_ids"]},
                    note="jlens_concept_pr reference site on the hallucination token ids",
                )
            )
        return out
    raise KeyError(f"unknown site family {family!r}; known {SITE_FAMILIES}")


def family_layers(family: str) -> tuple[int, ...]:
    """The read layers of a site family (``contracts.SITE_FAMILY_LAYERS``)."""
    return C.SITE_FAMILY_LAYERS[family]


def _item_sites(family: str, spec: Any, tokenizer: Any, start: int) -> list[tuple[Any, list]]:
    from wsbench.produce.producer import _positions
    from wsbench.produce.render import render

    rendered = render(spec, tokenizer)
    pos = _positions(spec.positions, rendered.tokens)  # exactly Producer._read_rendered
    n = len(rendered)
    out = []
    for i, p in enumerate(pos):
        j = p % n
        row = C.SiteRow(
            family=family,
            index=start + i,
            id=spec.id,
            pos=p,
            abs_pos=j,
            token=rendered.decoded[j],
            n_tokens=n,
        )
        out.append((row, rendered.ids))
    return out


def sites(family: str, tokenizer: Any, *, limit: int = 0) -> list[tuple[C.SiteRow, list[int]]]:
    """Every read site of a family as ``(SiteRow, render ids)``, in plan order then position
    order; ``SiteRow.index`` is the row in the activation store. ``limit`` keeps the first
    ``limit`` items (a prefix, so indices equal the full run's)."""
    specs = family_specs(family)[: limit or None]
    out: list[tuple[C.SiteRow, list[int]]] = []
    for spec in specs:
        out.extend(_item_sites(family, spec, tokenizer, len(out)))
    return out


def _by_item(site_list: list[tuple[C.SiteRow, list[int]]]) -> Iterator[tuple[list, list[int]]]:
    """Consecutive runs of sites that share an item (sites() keeps an item's sites together)."""
    i = 0
    while i < len(site_list):
        j = i
        while j < len(site_list) and site_list[j][0].id == site_list[i][0].id:
            j += 1
        yield [s for s, _ in site_list[i:j]], site_list[i][1]
        i = j


# --------------------------------------------------------------------- capture


def capture_sites(backend: Any, site_list: list[tuple[C.SiteRow, list[int]]]) -> Any:
    """[n_sites, 64, d] bf16 CPU: one ``Backend.capture`` forward per item at all 64 layers."""
    import torch

    layers = list(range(C.N_MODEL_LAYERS))
    if backend.n_layers != C.N_MODEL_LAYERS:
        raise ValueError(f"model has {backend.n_layers} blocks, expected {C.N_MODEL_LAYERS}")
    out: Any = None
    k = 0  # row of the output = position in site_list
    for rows, ids in _by_item(site_list):
        acts = backend.capture(ids, layers, [r.abs_pos for r in rows])
        stacked = torch.stack([acts[L] for L in layers], dim=1)  # [n_pos, 64, d] fp32
        if out is None:
            out = torch.empty(len(site_list), len(layers), stacked.shape[-1], dtype=torch.bfloat16)
        out[k : k + len(rows)] = stacked.to(torch.bfloat16)
        k += len(rows)
    if out is None:
        raise ValueError("no sites to capture")
    return out


def _index_lines(rows: list[C.SiteRow]) -> str:
    return "".join(C.to_json(r) + "\n" for r in rows)


def capture_family(backend: Any, family: str, *, overwrite: bool = False, limit: int = 0) -> Path:
    """Capture a site family into ``acts_path(family)`` + ``acts_index_path(family)``. Skips the
    work when both exist and the stored index equals ``sites(...)``. ``limit`` (tests) keeps the
    first items only."""
    from safetensors.torch import save_file

    site_list = sites(family, backend.tokenizer, limit=limit)
    if not limit and family in N_SITES_ALL and len(site_list) != N_SITES_ALL[family]:
        raise AssertionError(f"{family}: {len(site_list)} sites, expected {N_SITES_ALL[family]}")
    rows = [s for s, _ in site_list]
    acts_p, index_p = C.acts_path(family), C.acts_index_path(family)
    want = _index_lines(rows)
    if (
        not overwrite
        and acts_p.exists()
        and index_p.exists()
        and index_p.read_text(encoding="utf-8") == want
    ):
        return acts_p
    h = capture_sites(backend, site_list)
    acts_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = acts_p.with_suffix(".tmp")
    save_file(
        {"h": h.contiguous()},
        str(tmp),
        metadata={"family": family, "n_sites": str(len(rows)), "model_id": str(backend.model_id)},
    )
    os.replace(tmp, acts_p)
    index_p.write_text(want, encoding="utf-8")
    return acts_p


def load_index(family: str) -> list[C.SiteRow]:
    lines = C.acts_index_path(family).read_text(encoding="utf-8").splitlines()
    return [C.SiteRow(**json.loads(line)) for line in lines if line.strip()]


def load_acts(
    family: str, layers: list[int] | tuple[int, ...] | None = None
) -> tuple[list[C.SiteRow], Any]:
    """``(index rows, h)`` with ``h`` [n, len(layers), d] fp32 (all 64 layers when None)."""
    from safetensors.torch import load_file

    rows = load_index(family)
    h = load_file(str(C.acts_path(family)))["h"]
    if h.shape[0] != len(rows):
        raise ValueError(f"{family}: store has {h.shape[0]} rows, index {len(rows)}")
    if layers is not None:
        h = h[:, list(layers), :]
    return rows, h.float()
