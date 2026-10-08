"""E3's two question-swap maps over the 35 broad questions (prereg §4.4, "Two question-swap
variants"; implementation plan v2 fix 3, unit U3a).

- **mismatched:** a fixed derangement (a permutation with no fixed point) where every question
  maps to a question of a *different category*. Seeded rejection sampling: draw
  `numpy.random.default_rng(DERANGEMENT_SEED).permutation(n)` repeatedly from one generator and
  keep the first draw with no same-category assignment. With the frozen categories (largest 9
  of 35) about 1 draw in 700 qualifies, so this takes milliseconds and is deterministic.
- **shuffled:** one draw of `default_rng(SHUFFLE_SEED).permutation(n)` over all 35, with no
  category restriction. It is a permutation, so it may have fixed points; they are kept (the
  prereg says permutation) and listed in `shuffled_fixed_points`.

`map[q]` is the question after which q's pairs are scored. Questions are taken in the order given
(the builder passes E1's 8 general questions in file order, then the 27 ext48 ones in file order).
The file records both seeds and the sha256 of the pairs file it was frozen with; it carries no
timestamp, so identical inputs give a byte-identical file.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from emx.e3 import contracts as C

MAX_DRAWS = 1_000_000


class SwapError(ValueError):
    """The swap maps cannot be built from these questions."""


def _qids_categories(broad_questions: list[dict]) -> tuple[list[str], list[str]]:
    qids = [str(q["qid"]) for q in broad_questions]
    cats = [str(q["category"]) for q in broad_questions]
    if len(set(qids)) != len(qids):
        raise SwapError("duplicate qids in the broad questions")
    return qids, cats


def mismatched_derangement(categories: list[str], seed: int = C.DERANGEMENT_SEED) -> list[int]:
    """Indices `perm` with categories[perm[i]] != categories[i] for every i (seeded rejection)."""
    n = len(categories)
    counts: dict[str, int] = {}
    for c in categories:
        counts[c] = counts.get(c, 0) + 1
    if n < 2 or max(counts.values()) * 2 > n:
        raise SwapError(f"no cross-category derangement exists for category counts {counts}")
    cats = np.asarray(categories, dtype=object)
    rng = np.random.default_rng(seed)
    for _ in range(MAX_DRAWS):
        perm = rng.permutation(n)
        if not np.any(cats[perm] == cats):
            return [int(i) for i in perm]
    raise SwapError(f"no qualifying permutation in {MAX_DRAWS} draws")


def shuffled_permutation(n: int, seed: int = C.SHUFFLE_SEED) -> list[int]:
    return [int(i) for i in np.random.default_rng(seed).permutation(n)]


def build_swaps(broad_questions: list[dict]) -> dict:
    """Both maps over the broad questions (`{qid, category}` dicts, in canonical order)."""
    qids, cats = _qids_categories(broad_questions)
    if len(qids) != C.N_BROAD:
        raise SwapError(f"expected {C.N_BROAD} broad questions, got {len(qids)}")
    mis = mismatched_derangement(cats)
    shu = shuffled_permutation(len(qids))
    return {
        "mismatched": {qids[i]: qids[j] for i, j in enumerate(mis)},
        "shuffled": {qids[i]: qids[j] for i, j in enumerate(shu)},
        "shuffled_fixed_points": [qids[i] for i, j in enumerate(shu) if i == j],
        "derangement_seed": C.DERANGEMENT_SEED,
        "shuffle_seed": C.SHUFFLE_SEED,
        "qids": qids,
        "categories": dict(zip(qids, cats, strict=True)),
        "method": {
            "mismatched": "first numpy default_rng(derangement_seed).permutation(n) draw with no "
            "same-category assignment (rejection sampling, one generator)",
            "shuffled": "numpy default_rng(shuffle_seed).permutation(n); fixed points kept",
        },
    }


def check_swaps(swaps: dict) -> None:
    """Raise unless both maps are permutations of the same qids and `mismatched` is a
    cross-category derangement."""
    qids = list(swaps["qids"])
    cats = swaps["categories"]
    for name in ("mismatched", "shuffled"):
        m = swaps[name]
        if sorted(m) != sorted(qids) or sorted(m.values()) != sorted(qids):
            raise SwapError(f"{name} is not a permutation of the broad qids")
    bad = [q for q, t in swaps["mismatched"].items() if cats[q] == cats[t]]
    if bad:
        raise SwapError(f"mismatched maps within a category for {bad}")
    fixed = [q for q, t in swaps["shuffled"].items() if q == t]
    if sorted(fixed) != sorted(swaps["shuffled_fixed_points"]):
        raise SwapError("shuffled_fixed_points does not match the shuffled map")


def swaps_bytes(swaps: dict, pairs_sha256: str) -> bytes:
    rec = {**swaps, "pairs_sha256": pairs_sha256}
    return (json.dumps(rec, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def write_swaps(
    broad_questions: list[dict], pairs_sha256: str, path: str | Path = C.SWAPS_FILE
) -> tuple[dict, str]:
    """Build, check and write `SWAPS_FILE`; returns (record, sha256 of the file)."""
    import hashlib

    swaps = build_swaps(broad_questions)
    check_swaps(swaps)
    b = swaps_bytes(swaps, pairs_sha256)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b)
    return {**swaps, "pairs_sha256": pairs_sha256}, hashlib.sha256(b).hexdigest()
