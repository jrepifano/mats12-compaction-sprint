"""P03 pod session 2: reader items, read sites, activation capture and the token readers.

Reuses the I1a/I9 reader code instead of rewriting it:

- the render is WorkspaceBench's own ``wsbench.produce.render.render`` of a
  ``ReadSpec(..., "chat_prefill", ..., text=prompt, prefill=prefill)`` and the read positions come
  from ``wsbench.produce.producer._positions`` with ``{"kind": "offset_from_end", "k": k}``, so
  ``pos`` is reported as the benchmark reports it (negative offsets ``core.READ_POSITIONS``) and
  ``token`` is ``rendered.decoded[abs_pos]`` (wsbench ``Row.token``), as ``emx.i1a.capture`` does;
- capture is wsbench ``Backend.capture`` (forward hooks on the decoder blocks: layer L = the
  output of block L, the convention of I1a and I9; see the ``emx.i9.acts`` docstring for why
  not HF ``output_hidden_states``), one forward per item, stored bf16 like I1a's store;
- the logit lens and J-Lens are bound by ``emx.i1a.token_readers.bind_reader`` (J-Lens from the
  local file, never downloaded) and read cell by cell through the benchmark's own ``read``; rows
  are wsbench ``producer.Row.contract()``: ``{"id","layer","pos","token","tokens","scores"}``.

Store layout: ``<acts_dir>/acts.safetensors`` (key ``"h"``, ``[n_sites, len(STORE_LAYERS), d]``
bf16) plus ``<acts_dir>/sites.jsonl`` (one ``ReadSite`` per row, row i describes ``h[i]``). The
index is also written to ``results/read/sites.jsonl`` (pulled); the activations stay on the pod
(``/ws/p03_acts``) because a re-capture takes about a minute (I1a: 21 s for all its sites).

Torch, transformers, safetensors and wsbench are imported lazily.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from p03 import core

FAMILY = "p03"  # the ReadSpec family label (provenance only)
STORE_LAYERS: tuple[int, ...] = (*core.READ_LAYERS, core.FINAL_LAYER)  # 12 layers stored
TYPES = core.TYPES
GROUPS = ("survivor", "contrast", "ws")
STEM_TOKENS = (" answer", " is", ":")  # what the last 3 tokens of ANSWER_STEM decode to


# --------------------------------------------------------------------- items


@dataclass(frozen=True)
class ReaderItem:
    """One row of data/reader_items.jsonl (written by the laptop before session 2)."""

    id: str
    type: str
    group: str
    prompt: str
    prefill: str
    target: str
    target_aliases: list[str] = field(default_factory=list)
    modal_answer: str = ""
    meta: dict[str, Any] = field(default_factory=dict)  # any extra fields of the row, unused


_ITEM_FIELDS = (
    "id",
    "type",
    "group",
    "prompt",
    "prefill",
    "target",
    "target_aliases",
    "modal_answer",
)


def load_reader_items(path: Path) -> list[ReaderItem]:
    """Rows of ``path`` as ``ReaderItem``s, file order kept (extra fields go to ``meta``); raises
    on a missing field, a bad type or group, a duplicate id or an empty prompt or target."""
    rows = core.read_jsonl(path)
    if not rows:
        raise ValueError(f"{path}: no reader items")
    out: list[ReaderItem] = []
    seen: set[str] = set()
    for r in rows:
        missing = [k for k in _ITEM_FIELDS if k not in r]
        if missing:
            raise ValueError(f"{r.get('id')}: reader item lacks {missing}")
        it = ReaderItem(
            **{k: r[k] for k in _ITEM_FIELDS},
            meta={k: v for k, v in r.items() if k not in _ITEM_FIELDS},
        )
        if it.type not in TYPES or it.group not in GROUPS:
            raise ValueError(f"{it.id}: type {it.type!r} / group {it.group!r} not known")
        if it.id in seen:
            raise ValueError(f"duplicate reader item id {it.id!r}")
        if not it.prompt or not it.target:
            raise ValueError(f"{it.id}: empty prompt or target")
        if not isinstance(it.target_aliases, list):
            raise TypeError(f"{it.id}: target_aliases must be a list")
        seen.add(it.id)
        out.append(it)
    return out


# --------------------------------------------------------------------- sites


@dataclass(frozen=True)
class ReadSite:
    """Row ``index`` of the store: item ``id`` read at ``pos`` (negative offset from the end)."""

    index: int
    id: str
    pos: int  # core.READ_POSITIONS offset, as wsbench reports offset_from_end
    abs_pos: int  # index into the render
    token: str  # tokenizer.decode([ids[abs_pos]]) (wsbench Row.token)
    n_tokens: int
    ids_sha256: str  # of the whole render


def ids_sha256(ids: Sequence[int]) -> str:
    """sha256 of the comma-joined ids (the emx.i9.acts convention)."""
    return hashlib.sha256(",".join(str(int(i)) for i in ids).encode()).hexdigest()


def read_spec(it: ReaderItem) -> Any:
    """The item's wsbench read-plan row: chat render with the assistant turn prefilled."""
    from wsbench.readplan import ReadSpec

    return ReadSpec(
        FAMILY,
        it.id,
        "chat_prefill",
        {"kind": "last_n", "n": len(core.READ_POSITIONS)},
        list(core.READ_LAYERS),
        text=it.prompt,
        prefill=it.prefill,
        note="P03: the last 3 tokens of the prefilled render, reported as offsets -3..-1",
    )


def render_ids(it: ReaderItem, tokenizer: Any) -> list[int]:
    from wsbench.produce.render import render

    return list(render(read_spec(it), tokenizer).ids)


def item_sites(it: ReaderItem, tokenizer: Any, start: int) -> tuple[list[ReadSite], list[int]]:
    """The item's sites at ``core.READ_POSITIONS`` (in that order) and its render ids."""
    from wsbench.produce.producer import _positions
    from wsbench.produce.render import render

    rendered = render(read_spec(it), tokenizer)
    n = len(rendered)
    h = ids_sha256(rendered.ids)
    out = []
    for off in core.READ_POSITIONS:
        if off >= 0:
            raise ValueError(f"READ_POSITIONS must be negative offsets, got {off}")
        got = _positions({"kind": "offset_from_end", "k": -off}, rendered.tokens)
        if got != [off]:
            raise ValueError(f"{it.id}: offset {off} does not resolve in a {n}-token render")
        j = n + off
        out.append(ReadSite(start + len(out), it.id, off, j, rendered.decoded[j], n, h))
    return out, list(rendered.ids)


def build_sites(items: Sequence[ReaderItem], tokenizer: Any) -> list[tuple[ReadSite, list[int]]]:
    """Every site, in item order then position order (an item's sites are consecutive)."""
    out: list[tuple[ReadSite, list[int]]] = []
    for it in items:
        sites, ids = item_sites(it, tokenizer, len(out))
        out.extend((s, ids) for s in sites)
    return out


def stem_token_report(sites: Sequence[ReadSite]) -> dict[str, Any]:
    """How many items read exactly STEM_TOKENS at their 3 positions (expected: all)."""
    by: dict[str, list[str]] = {}
    for s in sites:
        by.setdefault(s.id, []).append(s.token)
    bad = {i: t for i, t in by.items() if tuple(t) != STEM_TOKENS}
    return {
        "n_items": len(by),
        "n_stem_ok": len(by) - len(bad),
        "off_stem": dict(list(bad.items())[:10]),
    }


def _by_item(
    site_list: Sequence[tuple[ReadSite, list[int]]],
) -> Iterator[tuple[list[ReadSite], list[int]]]:
    i = 0
    while i < len(site_list):
        j = i
        while j < len(site_list) and site_list[j][0].id == site_list[i][0].id:
            j += 1
        yield [s for s, _ in site_list[i:j]], site_list[i][1]
        i = j


# --------------------------------------------------------------------- capture and store


def capture(
    backend: Any, site_list: Sequence[tuple[ReadSite, list[int]]], layers: Sequence[int]
) -> Any:
    """``[n_sites, len(layers), d]`` bf16 CPU: one ``Backend.capture`` forward per item (the
    ``emx.i9.acts.capture`` loop, keyed on our ``abs_pos``)."""
    import torch

    layers = list(layers)
    out: Any = None
    k = 0
    for rows, ids in _by_item(site_list):
        acts = backend.capture(ids, layers, [r.abs_pos for r in rows])
        stacked = torch.stack([acts[L] for L in layers], dim=1)  # [n_pos, n_layers, d] fp32
        if out is None:
            out = torch.empty(len(site_list), len(layers), stacked.shape[-1], dtype=torch.bfloat16)
        out[k : k + len(rows)] = stacked.to(torch.bfloat16)
        k += len(rows)
    if out is None:
        raise ValueError("no sites to capture")
    return out


def _index_text(sites: Sequence[ReadSite]) -> str:
    return "".join(json.dumps(asdict(s), ensure_ascii=False) + "\n" for s in sites)


def store_paths(acts_dir: Path) -> tuple[Path, Path]:
    return Path(acts_dir) / "acts.safetensors", Path(acts_dir) / "sites.jsonl"


def capture_store(
    backend: Any,
    site_list: Sequence[tuple[ReadSite, list[int]]],
    acts_dir: Path,
    *,
    layers: Sequence[int] = STORE_LAYERS,
) -> tuple[Path, bool]:
    """Capture into ``acts_dir`` unless a store with the identical index is already there.
    Returns ``(acts path, captured now)``."""
    from safetensors.torch import save_file

    acts_p, index_p = store_paths(acts_dir)
    want = _index_text([s for s, _ in site_list])
    if acts_p.exists() and index_p.exists() and index_p.read_text("utf-8") == want:
        return acts_p, False
    h = capture(backend, site_list, layers)
    acts_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = acts_p.with_suffix(".tmp")
    save_file(
        {"h": h.contiguous()},
        str(tmp),
        metadata={"layers": json.dumps(list(layers)), "model_id": str(backend.model_id)},
    )
    os.replace(tmp, acts_p)
    index_p.write_text(want, encoding="utf-8")
    return acts_p, True


def load_store(acts_dir: Path) -> tuple[list[ReadSite], list[int], Any]:
    """``(sites, layers, h)`` with ``h`` the bf16 store ``[n, len(layers), d]``."""
    from safetensors import safe_open
    from safetensors.torch import load_file

    acts_p, index_p = store_paths(acts_dir)
    sites = [ReadSite(**json.loads(x)) for x in index_p.read_text("utf-8").splitlines() if x]
    with safe_open(str(acts_p), framework="pt") as f:
        layers = json.loads(f.metadata()["layers"])
    h = load_file(str(acts_p))["h"]
    if h.shape[0] != len(sites) or any(s.index != i for i, s in enumerate(sites)):
        raise ValueError(f"{acts_p}: store rows and index disagree")
    return sites, layers, h


# --------------------------------------------------------------------- token readers


def read_tokens(
    reader: Any,
    sites: Sequence[ReadSite],
    layers_stored: Sequence[int],
    h: Any,
    *,
    layers: Sequence[int] = core.READ_LAYERS,
) -> list[dict[str, Any]]:
    """Contract rows through ``reader.read`` (fp32 view of the bf16 store, as I1a read its
    store), in the wsbench producer's order: items, then layers, then positions."""
    import torch
    from wsbench.produce.producer import Row

    col = {L: list(layers_stored).index(L) for L in layers}
    by_item: dict[str, list[ReadSite]] = {}
    for s in sites:
        by_item.setdefault(s.id, []).append(s)
    rows: list[dict[str, Any]] = []
    with torch.inference_mode():
        for item in by_item.values():
            for L in layers:
                for s in item:
                    ro = reader.read(h[s.index, col[L]].float(), L)
                    rows.append(Row(s.id, L, s.pos, s.token, ro).contract())
    return rows


def final_layer_top1(
    reader: Any, sites: Sequence[ReadSite], layers_stored: Sequence[int], h: Any
) -> list[dict[str, Any]]:
    """The logit lens at ``core.FINAL_LAYER`` (= the model's own next-token logits) at pos -1:
    ``{"id","pos","token","top1","tokens","scores"}`` per item."""
    rows = read_tokens(
        reader, [s for s in sites if s.pos == -1], layers_stored, h, layers=(core.FINAL_LAYER,)
    )
    return [
        {
            "id": r["id"],
            "pos": r["pos"],
            "token": r["token"],
            "top1": r["tokens"][0],
            "tokens": r["tokens"],
            "scores": r["scores"],
        }
        for r in rows
    ]


def write_rows(path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    """Atomic JSONL write (``core.write_jsonl``)."""
    core.write_jsonl(path, rows)
    return path


def rows_complete(path: Path, n_expected: int) -> bool:
    """A readouts file from an earlier run of this step, with every expected row."""
    return Path(path).exists() and len(core.read_jsonl(path)) == n_expected
