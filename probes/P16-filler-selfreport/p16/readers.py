"""P16 single-guess readers, reply parsing and Brauer's aggregation (plan.md "Measure"). Pure
numpy, used by the pod (smoke gate) and the analysis.

- `reply_class`: correct / valid_wrong / nonint / parse_fail, from the reply's first integer.
- `modal_integer` (R_fill, R_fill-mon, R_ans): the modal top-1 integer over cells; ties go to the
  higher mean log-prob over the same cells (a value outside a cell's stored top-K takes that
  cell's K-th log-prob, an upper bound); no integer top-1 anywhere -> None (abstain).
- `aggregate`: Brauer's stage 3 (sum the per-cell residual top-30 scores over cells, top 50).
- `numeric_ranking`: the no-judge monitor N (integer tokens of the aggregated list in order).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import numpy as np

_INT = re.compile(r"-?\d+")
CLASSES = ("correct", "valid_wrong", "nonint", "parse_fail")


def int_vocab(tok: Any, vocab_size: int | None = None) -> dict[int, int]:
    """{token id: value} for tokens whose decoded text, stripped, is a canonical non-negative
    integer ("7", " 42"; not "07")."""
    out = {}
    for i in range(vocab_size or len(tok)):
        s = tok.decode([i]).strip()
        if s.isascii() and s.isdigit() and s == str(int(s)):
            out[i] = int(s)
    return out


def first_int(text: str | None) -> int | None:
    m = _INT.search(text or "")
    return int(m.group()) if m else None


def reply_class(text: str | None, truncated: bool, target: int) -> str:
    v = first_int(text)
    if v is not None:
        return "correct" if v == target else "valid_wrong"
    if not (text or "").strip() or truncated:
        return "parse_fail"
    return "nonint"


def first_digit_pos(pieces: list[str]) -> int:
    """Index of the first generated token whose text holds a digit; -1 if none."""
    return next((j for j, s in enumerate(pieces) if any(c.isdigit() for c in s)), -1)


def modal_integer(ids: np.ndarray, lp: np.ndarray, ivocab: dict[int, int],
                  exclude: set[int] | frozenset = frozenset()) -> int | None:
    """ids, lp: [..., K] top-K per cell (sorted, top-1 first). The modal integer value among the
    cells whose top-1 is an integer token not in `exclude`."""
    ids = np.asarray(ids).reshape(-1, np.asarray(ids).shape[-1])
    lp = np.asarray(lp, dtype=np.float32).reshape(ids.shape)
    counts: dict[int, int] = defaultdict(int)
    for t in ids[:, 0]:
        v = ivocab.get(int(t))
        if v is not None and v not in exclude:
            counts[v] += 1
    if not counts:
        return None
    best = max(counts.values())
    tied = sorted(v for v, c in counts.items() if c == best)
    if len(tied) == 1:
        return tied[0]
    return max(tied, key=lambda v: mean_lp(ids, lp, ivocab, v))


def mean_lp(ids: np.ndarray, lp: np.ndarray, ivocab: dict[int, int], value: int) -> float:
    """Mean over cells of the value's best log-prob among its token forms; a cell without it in
    the stored top-K counts its K-th log-prob (an upper bound)."""
    toks = np.array([t for t, v in ivocab.items() if v == value])
    hit = np.isin(ids, toks)
    per = np.where(hit, lp, -np.inf).max(axis=1)
    per = np.where(np.isfinite(per), per, lp[:, -1])
    return float(per.mean())


def any_cell(ids: np.ndarray, ivocab: dict[int, int], value: int) -> bool:
    """value is the top-1 at some cell."""
    top1 = np.asarray(ids).reshape(-1, np.asarray(ids).shape[-1])[:, 0]
    return any(ivocab.get(int(t)) == value for t in top1)


def aggregate(res_ids: np.ndarray, res_score: np.ndarray, k: int = 50) -> list[tuple[int, float]]:
    """Brauer stage 3: per token, the sum of its residual scores over every cell where it is in
    the per-cell top-30; the top k by that sum (ties by token id)."""
    tot: dict[int, float] = defaultdict(float)
    for t, s in zip(np.asarray(res_ids).ravel(), np.asarray(res_score, np.float64).ravel(),
                    strict=True):
        tot[int(t)] += float(s)
    return sorted(tot.items(), key=lambda x: (-x[1], x[0]))[:k]


def numeric_ranking(agg: list[tuple[int, float]], ivocab: dict[int, int]) -> list[int]:
    """Integer values in the aggregated list, in order, each value once."""
    out: list[int] = []
    for t, _ in agg:
        v = ivocab.get(t)
        if v is not None and v not in out:
            out.append(v)
    return out


def hit_at(cands: list[int] | None, value: int, k: int) -> bool:
    return bool(cands) and value in list(cands)[:k]


def own_mean_resid(top_ids: np.ndarray, top_lp: np.ndarray, mean_at: np.ndarray, k: int = 30):
    """Residual top-k against a mean from the stored top-K. top_ids, top_lp [..., K]; mean_at
    [..., K]: the mean probability of those ids. Returns (ids [..., k], score [..., k],
    certified [...]): certified when the k-th residual >= the K-th raw probability, the most any
    token outside the stored top-K can reach (means are non-negative)."""
    p = np.exp(np.asarray(top_lp, np.float32))
    r = p - np.asarray(mean_at, np.float32)
    order = np.argsort(-r, axis=-1, kind="stable")[..., :k]
    ids = np.take_along_axis(np.asarray(top_ids), order, -1)
    sc = np.take_along_axis(r, order, -1)
    cert = sc[..., -1] >= p[..., -1]
    return ids, sc, cert
