"""P03 Oracle Lens draws: one sample per cell with the benchmark's recipe and sampling.

Reused unchanged from I1a: ``emx.i1a.olens.PublicOLens`` (the public adapter as a named PEFT
adapter, live-checked on bind), ``read_batch`` (up to ``BATCH`` = 32 sequences of ONE layer per
``generate`` call, seeded right before it), ``default_sampling`` (wsbench ``Sampling``:
temperature 1.0, top_p 0.95, top_k 64, 256 new tokens), ``_append_batch`` (one locked, fsynced
append per batch), ``_atomic_write`` and ``derangement`` (the shuffled control's permutation).

Ours: the plan of a draw, its resume, and the readouts files.

- A **draw** is a list of cells ``(layer, site_index, source_index)``: the cell reads the
  activation of store row ``source_index`` at ``layer`` and reports site ``site_index``'s id, pos
  and token (``source_index == site_index`` except in the shuffled control).
- **Plan:** layers ascending; within a layer, cells by ascending site index, in chunks of
  ``BATCH``; ``batch_index`` counts across layers; batch b is generated right after
  ``torch.manual_seed(SEED_BASE[draw] + b)`` (``read_batch``'s ``_seed_all``).
- **Resume:** ``results/read/olens_samples/<draw>.jsonl`` holds one ``OlensRow`` per cell. On
  start, complete batches that match the plan are kept; a torn last line and partial batches are
  dropped in one atomic rewrite; a row that contradicts the plan raises (the grid decision is
  persisted in ``olens_grid.json`` so a rerun plans the same draws).

Torch, PEFT and wsbench are imported lazily (``emx.i1a.olens`` imports wsbench at module level,
so it is imported inside the functions).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from p03 import core
from p03.read_store import ReadSite

BATCH = 32  # = emx.i1a.contracts.BATCH (a test asserts it)
SEED_BASE: dict[str, int] = {  # batch b of a draw: torch.manual_seed(SEED_BASE[draw] + b)
    "main": 20261011,  # survivors + WorkspaceBench items, the full grid
    "shuffled": 20261011 + 100_000,  # shuffled-activation control, pos -1
    "contrast": 20261011 + 200_000,  # consistent non-survivors
    "smoke_shuffled": 20261011 + 300_000,  # the 20-cell smoke check
}
SHUFFLE_PERM_SEED = 20261011  # derangement over items (emx.i1a.olens.derangement)
DRAW_ORDER = ("main", "shuffled", "contrast")  # generation order: most important first


@dataclass(frozen=True)
class Cell:
    layer: int
    site: int  # store row whose id / pos / token the cell reports
    source: int  # store row whose activation the cell reads


@dataclass(frozen=True)
class Batch:
    layer: int
    batch_index: int
    seed: int
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class OlensRow:
    """One Oracle Lens generation (results/read/olens_samples/<draw>.jsonl)."""

    draw: str
    id: str
    layer: int
    pos: int
    site_index: int
    source_index: int
    source_id: str
    seed: int
    batch_index: int
    text: str  # batch_decode(skip_special_tokens=True), stripped (wsbench OLens.read)
    n_new_tokens: int
    finish: Literal["eos", "length"]


def plan(draw: str, cells: Sequence[Cell]) -> list[Batch]:
    """The canonical batch plan of a draw (see the module docstring)."""
    if len(set(cells)) != len(cells):
        raise ValueError(f"{draw}: duplicate cells")
    keys = [(c.layer, c.site) for c in cells]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{draw}: a (layer, site) cell appears twice")
    base = SEED_BASE[draw]
    out: list[Batch] = []
    for layer in sorted({c.layer for c in cells}):
        row = sorted((c for c in cells if c.layer == layer), key=lambda c: c.site)
        for i in range(0, len(row), BATCH):
            b = len(out)
            out.append(Batch(layer, b, base + b, tuple(row[i : i + BATCH])))
    return out


def n_batches(cells: Sequence[Cell]) -> int:
    per: dict[int, int] = {}
    for c in cells:
        per[c.layer] = per.get(c.layer, 0) + 1
    return sum(-(-n // BATCH) for n in per.values())


# --------------------------------------------------------------------- grids


def grid_cells(
    sites: Sequence[ReadSite],
    ids: set[str],
    layers: Sequence[int],
    positions: Sequence[int],
) -> list[Cell]:
    """Own-activation cells for the items ``ids`` at ``layers`` x ``positions``."""
    return [
        Cell(L, s.index, s.index)
        for s in sites
        if s.id in ids and s.pos in positions
        for L in layers
    ]


def item_derangement(sites: Sequence[ReadSite], seed: int = SHUFFLE_PERM_SEED) -> dict[str, str]:
    """``{item id: source item id}``: a seeded derangement over items (no item reads itself),
    ``emx.i1a.olens.derangement`` over one pos -1 site per item, in store order."""
    from emx.i1a.olens import derangement

    last = [s for s in sites if s.pos == -1]
    perm = derangement(last, seed)
    return {last[i].id: last[perm[i]].id for i in range(len(last))}


def shuffled_cells(
    sites: Sequence[ReadSite],
    ids: set[str],
    layers: Sequence[int],
    source_of: dict[str, str],
    pos: int = -1,
) -> list[Cell]:
    """Item i's pos-``pos`` site reads item ``source_of[i]``'s activation at the same layer and
    pos."""
    at = {s.id: s.index for s in sites if s.pos == pos}
    return [Cell(L, at[i], at[source_of[i]]) for i in at if i in ids for L in layers]


# --------------------------------------------------------------------- resume and generation


def read_rows(path: Path) -> list[OlensRow]:
    return [OlensRow(**r) for r in core.read_jsonl(path)]


def load_done(path: Path, draw: str, batches: Sequence[Batch], sites: Sequence[ReadSite]) -> set:
    """Batch indices already complete in ``path`` (see the module docstring)."""
    from emx.i1a.olens import _atomic_write

    if not Path(path).exists():
        return set()
    lines = Path(path).read_text("utf-8").splitlines()
    keep: list[tuple[str, OlensRow]] = []
    torn = False
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            keep.append((line, OlensRow(**json.loads(line))))
        except (json.JSONDecodeError, TypeError):
            if i == len(lines) - 1:
                torn = True
                continue
            raise
    by_index = {b.batch_index: b for b in batches}
    groups: dict[int, list[OlensRow]] = {}
    for _, r in keep:
        groups.setdefault(r.batch_index, []).append(r)
    done, partial = set(), set()
    for bi, rows in groups.items():
        b = by_index.get(bi)
        if b is None:
            raise ValueError(f"{path}: batch {bi} is not in the {draw} plan")
        want = {(c.site, c.source) for c in b.cells}
        got = [(r.site_index, r.source_index) for r in rows]
        if not set(got) <= want or len(got) != len(set(got)):
            raise ValueError(f"{path}: batch {bi} does not match the {draw} plan")
        for r in rows:
            s = sites[r.site_index]
            if (r.draw, r.layer, r.seed, r.id, r.pos) != (draw, b.layer, b.seed, s.id, s.pos):
                raise ValueError(f"{path}: row {r.id}@{r.pos} L{r.layer} contradicts batch {bi}")
        (done if set(got) == want else partial).add(bi)
    if torn or partial:
        _atomic_write(Path(path), [x for x, r in keep if r.batch_index not in partial])
    return done


def generate(
    olens: Any,
    draw: str,
    cells: Sequence[Cell],
    *,
    sites: Sequence[ReadSite],
    layers_stored: Sequence[int],
    h: Any,
    out: Path,
    sampling: Any = None,
    stop: Callable[[], bool] | None = None,
    on_batch: Callable[[Batch, float], None] | None = None,
) -> dict[str, Any]:
    """Generate the draw's missing batches into ``out`` (append per batch). ``stop()`` is asked
    before each batch (the overrun rule); ``on_batch(batch, seconds)`` is called after each.
    Returns ``{"n_batches", "n_done_before", "n_generated", "complete", "stopped"}``."""
    from emx.i1a.olens import _append_batch, read_batch

    batches = plan(draw, cells)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out, draw, batches, sites)
    col = {L: list(layers_stored).index(L) for L in {c.layer for c in cells}}
    n_gen, stopped = 0, False
    for b in batches:
        if b.batch_index in done:
            continue
        if stop is not None and stop():
            stopped = True
            break
        t = time.time()
        H = h[[c.source for c in b.cells], col[b.layer], :].float()
        res = read_batch(olens, H, b.layer, b.seed, sampling=sampling)
        rows = [
            OlensRow(
                draw,
                sites[c.site].id,
                b.layer,
                sites[c.site].pos,
                c.site,
                c.source,
                sites[c.source].id,
                b.seed,
                b.batch_index,
                text,
                nt,
                fin,
            )  # type: ignore[arg-type]
            for c, (text, nt, fin) in zip(b.cells, res, strict=True)
        ]
        _append_batch(out, rows)  # I1a's to_json serialises any row dataclass
        n_gen += 1
        if on_batch is not None:
            on_batch(b, time.time() - t)
    got = sorted((r.batch_index, r.site_index) for r in read_rows(out))
    want = sorted((b.batch_index, c.site) for b in batches for c in b.cells)
    return {
        "n_batches": len(batches),
        "n_done_before": len(done),
        "n_generated": n_gen,
        "complete": got == want,
        "stopped": stopped,
    }


def readout_rows(
    rows: Sequence[OlensRow], sites: Sequence[ReadSite], *, with_source: bool = False
) -> list[dict[str, Any]]:
    """wsbench readouts rows ``{"id","layer","pos","token","samples":[text]}`` (plus
    ``source_id`` for the shuffled control), ordered by item (store order), layer, position."""
    keys = [(r.id, r.layer, r.pos) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate (id, layer, pos) Oracle Lens rows")
    first: dict[str, int] = {}
    for s in sites:
        first.setdefault(s.id, s.index)
    out = []
    for r in sorted(rows, key=lambda r: (first[r.id], r.layer, r.pos)):
        d: dict[str, Any] = {
            "id": r.id,
            "layer": r.layer,
            "pos": r.pos,
            "token": sites[r.site_index].token,
            "samples": [r.text],
        }
        if with_source:
            d["source_id"] = r.source_id
        out.append(d)
    return out


def smoke_cells(
    sites: Sequence[ReadSite], source_of: dict[str, str], n: int, layer: int, seed: int
) -> list[Cell]:
    """``n`` shuffled cells at ``layer``, pos -1, for a seeded sample of items."""
    ids = sorted({s.id for s in sites})
    pick = set(random.Random(seed).sample(ids, min(n, len(ids))))
    return shuffled_cells(sites, pick, [layer], source_of)


def row_dict(r: OlensRow) -> dict[str, Any]:
    return asdict(r)
