"""E13 direction extraction and its rules (prereg §4.3 steps 3-5, §4.6 item 8; Amendment B
items 2 and 5; contracts DIR_* constants and DIRECTION_KEYS).

Per site, from the per-answer mean activations of `emx.e13.acts.capture_means`:
1. A cell (`contracts.cell_id(qid, instance)`) is eligible with at least DIR_MIN_PER_CLASS "bad"
   and DIR_MIN_PER_CLASS "good" answers ("drop" answers are ignored).
2. d = the UNWEIGHTED mean over eligible cells of (cell bad mean - cell good mean), so neither
   notice wording nor question identity enters it.
3. Stop "too_few_cells" with fewer than DIR_MIN_CELLS eligible cells or fewer than
   DIR_MIN_QUESTIONS distinct questions among them (the caller extends once, then calls again);
   stop "norm_floor" if ||d|| < DIR_NORM_FLOOR x the mean residual norm at that site.
4. Reliability: DIR_HALF_SPLITS seeded (HALF_SPLIT_SEED) stratified half-splits. In each split
   every eligible cell's bad answers are shuffled and halved, and its good answers likewise (an
   odd count gives the extra item to half A); each half's direction is computed as in 2; the
   median cosine between the halves must be >= DIR_RELIABILITY_MIN, else stop "reliability".
   One numpy Generator(PCG64(HALF_SPLIT_SEED)) is consumed split by split, cell by cell in
   sorted cell order, bad before good. Uncorrected half-sample cosine (prereg §4.3 step 4).
5. u = d/||d|| (stored as a list of float32 values), g = ||d||, g / mean residual norm, and the
   diagnostics cos(u, adapter B) (None when no B is given, e.g. rank 32: "n/a") and
   cos(u_input, u_output) (`cross_site_cosine`).
Stop reasons are checked in that order; the half-splits are still computed (as a diagnostic)
whenever d exists. Arithmetic is float64; `load_unit` returns float32.

numpy and stdlib only (imports on the laptop).
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from emx.e13 import contracts as C

LABELS: tuple[str, ...] = ("bad", "good", "drop")
UNIT_TOL = 1e-4


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _diff_mean(x: np.ndarray, groups: Sequence[tuple[Sequence[int], Sequence[int]]]) -> np.ndarray:
    """Unweighted mean over cells of (mean of x[bad] - mean of x[good])."""
    return np.mean([x[list(b)].mean(0) - x[list(g)].mean(0) for b, g in groups], axis=0)


def half_splits(groups: Sequence[tuple[Sequence[int], Sequence[int]]],
                n_splits: int = C.DIR_HALF_SPLITS, seed: int = C.HALF_SPLIT_SEED) -> list:
    """Stratified half-splits (module docstring step 4): a list of (half A, half B), each a list
    of per-cell (bad rows, good rows). Odd counts give the extra row to half A."""
    rng = np.random.Generator(np.random.PCG64(seed))
    out = []
    for _ in range(n_splits):
        ha, hb = [], []
        for bad, good in groups:
            halves = []
            for ix in (bad, good):
                p = rng.permutation(np.asarray(ix, dtype=np.int64))
                k = (len(p) + 1) // 2
                halves.append((p[:k].tolist(), p[k:].tolist()))
            (ba, bb), (ga, gb) = halves
            ha.append((ba, ga))
            hb.append((bb, gb))
        out.append((ha, hb))
    return out


def _content_sha(means: np.ndarray, labels: Sequence[str], cells: Sequence[str],
                 qids: Sequence[str]) -> tuple[str, str]:
    a = hashlib.sha256(np.ascontiguousarray(means, dtype=np.float32).tobytes()).hexdigest()
    lab = json.dumps([list(labels), list(cells), list(qids)], separators=(",", ":"))
    return a, hashlib.sha256(lab.encode()).hexdigest()


def extract_direction(site: str, means, labels: Sequence[str], cells: Sequence[str],
                      qids: Sequence[str], mean_resid_norm: float, adapter_B=None, *,
                      acts_sha256: str | None = None,
                      labels_sha256: str | None = None) -> dict:
    """The direction record (DIRECTION_KEYS) of one site; see the module docstring. `means` is
    [n, d] in the row order of `labels`, `cells` and `qids`. `acts_sha256`/`labels_sha256` are
    the caller's file hashes (acts_<site>.safetensors, the labels record); by default they hash
    the arrays and lists given."""
    if site not in C.SITES:
        raise ValueError(f"unknown site {site!r}")
    x = np.asarray(means, dtype=np.float64)
    n = x.shape[0]
    if x.ndim != 2 or not (len(labels) == len(cells) == len(qids) == n):
        raise ValueError("means, labels, cells and qids must have one row each")
    bad_lab = sorted({lab for lab in labels} - set(LABELS))
    if bad_lab:
        raise ValueError(f"unknown labels {bad_lab}")
    if not np.isfinite(x).all() or not (np.isfinite(mean_resid_norm) and mean_resid_norm > 0):
        raise ValueError("non-finite activations or residual norm")
    a_sha, l_sha = _content_sha(x, labels, cells, qids)

    by_cell: dict[str, dict] = {}
    for k, (lab, cell, q) in enumerate(zip(labels, cells, qids, strict=True)):
        c = by_cell.setdefault(cell, {"qid": q, "bad": [], "good": []})
        if c["qid"] != q:
            raise ValueError(f"cell {cell} spans questions {c['qid']} and {q}")
        if lab in ("bad", "good"):
            c[lab].append(k)
    cell_recs, eligible = [], []
    for cell in sorted(by_cell):
        c = by_cell[cell]
        ok = len(c["bad"]) >= C.DIR_MIN_PER_CLASS and len(c["good"]) >= C.DIR_MIN_PER_CLASS
        cell_recs.append({"cell": cell, "qid": c["qid"],
                          "instance_id": cell.split("@", 1)[1] if "@" in cell else "",
                          "n_bad": len(c["bad"]), "n_good": len(c["good"]), "eligible": ok})
        if ok:
            eligible.append(cell)
    n_q = len({by_cell[c]["qid"] for c in eligible})
    layer = C.SITES[site][1]
    rec = {
        "site": site, "layer": layer, "hook": C.SITES[site][0], "u": None, "g": None,
        "d_norm": None, "g_over_resid_norm": None, "mean_resid_norm": float(mean_resid_norm),
        "cells": cell_recs, "n_eligible": len(eligible), "n_questions": n_q,
        "half_split_cosines": [], "median_cosine": None, "cos_u_B": None,
        "cos_u_other_site": None,
        "acts_sha256": acts_sha256 or a_sha, "labels_sha256": labels_sha256 or l_sha,
        "passed": False, "stop_reason": None,
    }
    if len(eligible) < C.DIR_MIN_CELLS or n_q < C.DIR_MIN_QUESTIONS or not eligible:
        rec["stop_reason"] = "too_few_cells"
        return {k: rec[k] for k in C.DIRECTION_KEYS}

    groups = [(by_cell[c]["bad"], by_cell[c]["good"]) for c in eligible]
    d = _diff_mean(x, groups)
    g = float(np.linalg.norm(d))
    rec.update(d_norm=g, g=g, g_over_resid_norm=g / float(mean_resid_norm))
    if g > 0:
        u = d / g
        rec["u"] = [float(v) for v in u.astype(np.float32)]
        if adapter_B is not None:
            b = np.asarray(adapter_B, dtype=np.float64).reshape(-1)
            if b.shape != u.shape:
                raise ValueError(f"adapter_B has {b.shape[0]} dims, u {u.shape[0]}")
            rec["cos_u_B"] = _cos(u, b)

    cosines = [_cos(_diff_mean(x, ha), _diff_mean(x, hb)) for ha, hb in half_splits(groups)]
    rec["half_split_cosines"] = cosines
    rec["median_cosine"] = float(statistics.median(cosines))

    if g < C.DIR_NORM_FLOOR * float(mean_resid_norm):
        rec["stop_reason"] = "norm_floor"
    elif rec["median_cosine"] < C.DIR_RELIABILITY_MIN:
        rec["stop_reason"] = "reliability"
    else:
        rec["passed"] = True
    return {k: rec[k] for k in C.DIRECTION_KEYS}


def cross_site_cosine(rec_in: dict, rec_out: dict) -> float | None:
    """cos(u_input, u_output), written into both records' `cos_u_other_site` (None when either
    site has no direction)."""
    if rec_in.get("site") != "input" or rec_out.get("site") != "output":
        raise ValueError("cross_site_cosine takes the input record, then the output record")
    if rec_in.get("u") is None or rec_out.get("u") is None:
        c = None
    else:
        c = _cos(np.asarray(rec_in["u"], np.float64), np.asarray(rec_out["u"], np.float64))
    rec_in["cos_u_other_site"] = rec_out["cos_u_other_site"] = c
    return c


def write_direction(site: str, rec: dict, root, recipe: str | None = None) -> Path:
    """Write the record to `<root>/direction_path(site, recipe)` (keys checked; Amendment E: the
    tan32 retry's own directory); returns the path."""
    if tuple(rec) != C.DIRECTION_KEYS or rec["site"] != site:
        raise ValueError("record keys or site do not match DIRECTION_KEYS")
    p = Path(root) / C.direction_path(site, recipe)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=1) + "\n")
    return p


def load_unit(site: str, root, recipe: str | None = None) -> tuple[np.ndarray, float]:
    """(u float32 [d], g) of a passed direction of the run of `recipe`; refuses a failed record
    or a non-unit u."""
    p = Path(root) / C.direction_path(site, recipe)
    rec = json.loads(p.read_text())
    if rec.get("site") != site:
        raise ValueError(f"{p}: site {rec.get('site')!r}, expected {site!r}")
    if not rec.get("passed"):
        raise ValueError(f"{p}: direction did not pass ({rec.get('stop_reason')}); "
                         "this site is stopped")
    u = np.asarray(rec["u"], dtype=np.float32)
    g = float(rec["g"])
    if u.ndim != 1 or abs(float(np.linalg.norm(u)) - 1.0) > UNIT_TOL or not g > 0:
        raise ValueError(f"{p}: u is not a unit vector or g is not positive")
    return u, g
