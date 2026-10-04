"""The readers on Qwen's last 64 prompt positions (implementation plan §2 U3; prereg v0.5 §4.2
Stage 2, §4.5 "Prompt-only scope").

- **Logit lens and J-Lens** over the 12 ``LADDER`` layers, bound by
  ``emx.i1a.token_readers.bind_reader`` and read cell by cell through the benchmark's own
  ``read`` (the ``emx.i1a.token_readers.read_family`` pattern), rows built by wsbench's
  ``producer.Row.contract``: ``{"id", "layer", "pos", "token", "tokens", "scores"}``.
- **Oracle Lens** over ``OLENS_LAYERS`` with ``emx.i1a.olens.PublicOLens`` and ``read_batch``,
  one sample per (id, pos, layer), rows ``{"id", "layer", "pos", "token", "samples": [text]}``.
  I9 has its own one-draw table (``OLENS_DRAWS``, seeds from ``contracts.OLENS_SEED``); I1a's
  ``DRAW_SCOPE`` and seed bases are not used.

Every readouts file is in the wsbench agentic_misalignment schema (``id`` = scenario id =
the bank row's label, ``pos`` = the prompt position, ``layer`` = the ladder layer; README
"Readout input"). That family's judge treats every row as in scope, so **the prompt-scope rule
is ours**: ``write_readouts`` asserts ``0 <= pos < rollout_start`` for every row before
writing, then re-reads the file with ``wsbench.readouts.load_readouts`` and requires zero skipped
rows.

Torch, PEFT and wsbench are imported lazily.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from emx.i9 import acts
from emx.i9 import contracts as C

TOKEN_READERS: tuple[str, ...] = ("logit_lens", "jlens")
OLENS = "olens"
OLENS_BATCH = 32  # sequences per generate() call, one layer per batch (= emx.i1a BATCH)
# The one-draw table: which scenario sets the Oracle Lens reads, each with its own seed base.
# Batch b of a set's draw is generated right after torch.manual_seed(seed_base + b).
OLENS_DRAWS: dict[str, int] = {
    "confirmatory": C.OLENS_SEED,
    "bank_validation": C.OLENS_SEED + 100_000,
    "sealed": C.OLENS_SEED + 200_000,  # only with --unsealed (spread check failed)
}


def samples_dir() -> Path:
    """results/I9/olens_samples/<set>.jsonl: the raw OlensSample rows of each draw."""
    return C.OLENS_SAMPLES_DIR


def pilot_path() -> Path:
    """The reader-speed pilot's timing JSON."""
    return C.OLENS_PILOT_PATH


class PromptScopeError(AssertionError):
    """A readout row at or after the first reply token (prereg §4.5: stop)."""


def readout_name(reader: str, set_key: str) -> str:
    """``results/I9/readouts/<name>.jsonl``: the bare reader for the confirmatory set,
    ``<reader>_<set>`` otherwise (``olens_bank_validation``, ``jlens_sealed``, ...)."""
    return reader if set_key == "confirmatory" else f"{reader}_{set_key}"


# --------------------------------------------------------------------- scope and writing


def scope_assert(rows: Iterable[Mapping[str, Any]], rollout_start: Mapping[str, int]) -> int:
    """Raise ``PromptScopeError`` unless every row's id is known and ``0 <= pos <
    rollout_start[id]``; returns the row count."""
    n = 0
    bad: list[tuple[Any, Any]] = []
    for r in rows:
        n += 1
        rs = rollout_start.get(r["id"])
        if rs is None or not isinstance(r["pos"], int) or not 0 <= r["pos"] < rs:
            bad.append((r["id"], r["pos"]))
    if bad:
        raise PromptScopeError(
            f"{len(bad)} readout rows are not prompt positions (pos < rollout_start), "
            f"e.g. {bad[:3]}"
        )
    return n


def write_readouts(
    path: Path, rows: Sequence[Mapping[str, Any]], rollout_start: Mapping[str, int]
) -> Path:
    """Scope-assert, then write atomically, then verify with wsbench's ``load_readouts`` (every
    row parsed, none skipped). Rows are ordered by item (first appearance), layer, position."""
    from wsbench.readouts import load_readouts

    scope_assert(rows, rollout_start)
    keys = [(r["id"], r["layer"], r["pos"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{path}: duplicate (id, layer, pos) rows")
    first: dict[str, int] = {}
    for r in rows:
        first.setdefault(r["id"], len(first))
    ordered = sorted(rows, key=lambda r: (first[r["id"]], r["layer"], r["pos"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(dict(r), ensure_ascii=False) + "\n" for r in ordered))
    os.replace(tmp, path)
    cells, rep = load_readouts(path, ids=list(first))
    if len(cells) != len(rows) or any(rep.skipped.values()):
        raise AssertionError(f"{path}: load_readouts kept {len(cells)}/{len(rows)}, {rep.skipped}")
    return path


def _select(rows: Sequence[acts.ActSite], ids: Iterable[str] | None) -> list[acts.ActSite]:
    if ids is None:
        return list(rows)
    keep = set(ids)
    got = [r for r in rows if r.id in keep]
    missing = keep - {r.id for r in got}
    if missing:
        raise KeyError(f"{len(missing)} scenarios not in the store, e.g. {sorted(missing)[:3]}")
    return got


# --------------------------------------------------------------------- token readers


def read_tokens(
    reader: Any,
    *,
    store: Path,
    ids: Iterable[str] | None,
    out: Path,
    layers: Sequence[int] = C.LADDER,
) -> Path:
    """Contract rows for every (scenario in ``ids``, layer, position) of the last-64 store
    through ``reader.read`` (a bound ``LogitLens`` / ``JLens`` from
    ``emx.i1a.token_readers.bind_reader``), scope-asserted and written to ``out``."""
    import torch
    from wsbench.produce.producer import Row

    meta, sites, h = acts.load_store(store)
    col = {L: meta["layers"].index(L) for L in layers}
    rows = _select(sites, ids)
    by_item: dict[str, list[acts.ActSite]] = {}
    for r in rows:
        by_item.setdefault(r.id, []).append(r)
    out_rows = []
    with torch.inference_mode():
        for item in by_item.values():
            for layer in layers:
                for r in item:
                    ro = reader.read(h[r.index, col[layer]], layer)
                    out_rows.append(Row(r.id, layer, r.pos, r.token, ro).contract())
    return write_readouts(out, out_rows, acts.rollout_starts(sites))


# --------------------------------------------------------------------- Oracle Lens


@dataclass(frozen=True)
class OlensSample:
    """One Oracle Lens generation (results/I9/olens_samples/<set>.jsonl)."""

    set_key: str
    id: str
    layer: int
    pos: int
    site_index: int  # row of the last-64 store
    seed: int
    batch_index: int
    text: str  # batch_decode(skip_special_tokens=True), stripped (wsbench OLens.read)
    n_new_tokens: int
    finish: Literal["eos", "length"]


@dataclass(frozen=True)
class Batch:
    layer: int
    batch_index: int
    seed: int
    sites: tuple[int, ...]  # store row indices


def olens_plan(set_key: str, site_indices: Sequence[int], layers: Sequence[int]) -> list[Batch]:
    """The canonical order: layers ascending; within a layer, ascending store rows, in chunks of
    ``OLENS_BATCH``; ``batch_index`` counts across layers; seed = the set's base + index."""
    base = OLENS_DRAWS[set_key]
    sites = sorted(site_indices)
    if len(set(sites)) != len(sites):
        raise ValueError("site indices must be distinct")
    plan: list[Batch] = []
    for layer in sorted(set(layers)):
        for i in range(0, len(sites), OLENS_BATCH):
            b = len(plan)
            plan.append(Batch(layer, b, base + b, tuple(sites[i : i + OLENS_BATCH])))
    return plan


def samples_path(set_key: str) -> Path:
    return samples_dir() / f"{set_key}.jsonl"


def read_samples(path: Path) -> list[OlensSample]:
    if not path.exists():
        return []
    return [OlensSample(**json.loads(x)) for x in path.read_text("utf-8").splitlines() if x.strip()]


def _load_done(path: Path, set_key: str, plan: list[Batch], sites: Sequence[acts.ActSite]) -> set:
    """Complete batches already in ``path``. Rows must match the plan (layer, seed, site, id,
    pos), else raise; a torn last line and partial batches are dropped in one atomic rewrite."""
    from emx.i1a.olens import _atomic_write

    if not path.exists():
        return set()
    lines = path.read_text("utf-8").splitlines()
    keep: list[tuple[str, OlensSample]] = []
    torn = False
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            keep.append((line, OlensSample(**json.loads(line))))
        except (json.JSONDecodeError, TypeError):
            if i == len(lines) - 1:
                torn = True
                continue
            raise
    by_index = {b.batch_index: b for b in plan}
    groups: dict[int, list[OlensSample]] = {}
    for _, r in keep:
        groups.setdefault(r.batch_index, []).append(r)
    done, partial = set(), set()
    for bi, rows in groups.items():
        b = by_index.get(bi)
        got = [r.site_index for r in rows]
        if b is None or not set(got) <= set(b.sites) or len(got) != len(set(got)):
            raise ValueError(f"{path}: batch {bi} does not match the {set_key} plan")
        for r in rows:
            s = sites[r.site_index]
            if (r.set_key, r.layer, r.seed, r.id, r.pos) != (set_key, b.layer, b.seed, s.id, s.pos):
                raise ValueError(f"{path}: row {r.id}@{r.pos} L{r.layer} does not match batch {bi}")
        (done if set(got) == set(b.sites) else partial).add(bi)
    if torn or partial:
        _atomic_write(path, [x for x, r in keep if r.batch_index not in partial])
    return done


def generate_olens(
    olens: Any,
    set_key: str,
    *,
    store: Path,
    ids: Iterable[str],
    layers: Sequence[int] = C.OLENS_LAYERS,
    sampling: Any = None,
    out: Path | None = None,
) -> Path:
    """Generate the set's one draw (resumable by batch, each batch appended atomically with
    ``emx.i1a.olens._append_batch``), verify it complete, and return the samples file."""
    from emx.i1a.olens import _append_batch, read_batch

    meta, sites, h = acts.load_store(store)
    col = {L: meta["layers"].index(L) for L in layers}
    rows = _select(sites, ids)
    plan = olens_plan(set_key, [r.index for r in rows], layers)
    path = out or samples_path(set_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(path, set_key, plan, sites)
    for b in plan:
        if b.batch_index in done:
            continue
        H = h[list(b.sites), col[b.layer], :]
        res = read_batch(olens, H, b.layer, b.seed, sampling=sampling)
        _append_batch(
            path,
            [
                OlensSample(
                    set_key,
                    sites[si].id,
                    b.layer,
                    sites[si].pos,
                    si,
                    b.seed,
                    b.batch_index,
                    text,
                    nt,
                    fin,
                )  # type: ignore[arg-type]
                for si, (text, nt, fin) in zip(b.sites, res, strict=True)
            ],
        )
    got = sorted((r.batch_index, r.site_index) for r in read_samples(path))
    want = sorted((b.batch_index, si) for b in plan for si in b.sites)
    if got != want:
        raise RuntimeError(f"{path}: the {set_key} draw is not complete after generation")
    return path


def write_olens_readouts(set_key: str, *, store: Path, samples: Path, out: Path) -> Path:
    """``readouts/<olens name>.jsonl`` from a complete draw: one row per sample."""
    _, sites, _ = acts.load_store(store)
    rows = [
        {
            "id": r.id,
            "layer": r.layer,
            "pos": r.pos,
            "token": sites[r.site_index].token,
            "samples": [r.text],
        }
        for r in read_samples(samples)
        if r.set_key == set_key
    ]
    if not rows:
        raise ValueError(f"{samples}: no {set_key} rows")
    return write_readouts(out, rows, acts.rollout_starts(sites))


def olens_pilot(
    olens: Any,
    *,
    store: Path,
    ids: Iterable[str],
    layer: int = C.OLENS_LAYERS[2],
    sampling: Any = None,
    n_batches_planned: int | None = None,
    out: Path | None = None,
) -> dict[str, Any]:
    """The timed reader-speed pilot: one Oracle Lens batch of ``OLENS_BATCH`` (or fewer if the
    store is smaller) at ``layer`` with the confirmatory seed base, timing to ``out`` JSON. The
    projection is ``n_batches_planned x seconds`` (default: the confirmatory + validation plan
    sizes, filled in by the caller)."""
    import torch

    from emx.i1a.olens import read_batch

    meta, sites, h = acts.load_store(store)
    rows = _select(sites, ids)[:OLENS_BATCH]
    H = h[[r.index for r in rows], meta["layers"].index(layer), :]
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    t = time.time()
    res = read_batch(olens, H, layer, OLENS_DRAWS["confirmatory"], sampling=sampling)
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    sec = time.time() - t
    s = sampling or olens.sampling
    rec = {
        "batch_size": len(rows),
        "layer": layer,
        "seconds": round(sec, 3),
        "max_new_tokens": int(s.max_new_tokens),
        "mean_new_tokens": round(sum(n for _, n, _ in res) / len(res), 2),
        "n_length_stops": sum(f == "length" for _, _, f in res),
        "n_batches_planned": n_batches_planned,
        "projected_hours": None
        if n_batches_planned is None
        else round(n_batches_planned * sec / 3600, 3),
        "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2)
        if torch.cuda.is_available()
        else 0.0,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = out or pilot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def n_olens_batches(n_scenarios: int, layers: Sequence[int] = C.OLENS_LAYERS) -> int:
    """Batches of one set's draw: ceil(n x 64 / 32) per layer."""
    per_layer = -(-n_scenarios * C.N_READ_POSITIONS // OLENS_BATCH)
    return per_layer * len(layers)
