"""P03 session 2: the smoke checks' decision logic, the Oracle Lens grid levels and the projection.
Pure Python (no torch), so every rule is unit-tested on the laptop.

Smoke checks (plan "Automatic rules"; each failure drops or flags a reader, never stops the run):

- **a. vLLM vs HF** (``check_vllm_vs_hf``): HF ``generate`` modal answers equal session 1's vLLM
  modal answers on >= 9 of 10 rows. Failure: warn only.
- **b. J-Lens reference** (``check_jlens_reference``): our capture + J-Lens reproduces I1a's
  committed J-Lens readouts on 3 multihop items. Failure: drop J-Lens.
- **c. shuffled Oracle Lens** (``check_shuffled``): on 20 shuffled-activation cells, the item's
  own target is named in <= 1 (5%). Failure: drop the Oracle Lens.
- **d. final-layer logit lens** (``check_final_layer``): its top-1 token matches Qwen's modal
  answer on >= 90% of type-B items, which confirms the read site. Failure: warn loudly.

Grid levels (``LEVELS``), cut in order while the projected session time exceeds the limit:
0 the full grid; 1 contrast items' Oracle Lens at pos -1 only; 2 every draw at layers
(20, 28, 36, 44, 52, 60); 3 the shuffled control on survivors and WorkspaceBench items only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from p03 import core

SMOKE_A_MIN_FRAC = 0.9  # >= 9 of 10 rows
SMOKE_C_N_CELLS = 20
SMOKE_C_MAX_FRAC = 0.05  # <= 1 of 20 cells
SMOKE_D_MIN_FRAC = 0.9
JLENS_REF_N_ITEMS = 3
JLENS_REF_TOL = 0.05  # emx.i9.acts.I1A_REPRO_SCORE_TOL: ~1.5 bf16 ulps
JLENS_REF_MIN_FRAC = 0.95  # of cells passing the per-cell rule (see check_jlens_reference)
# results/I1a/readouts/jlens/multihop.jsonl as results/I1a/MANIFEST.tokens.sha256 lists it
JLENS_REF_PATH = "results/I1a/readouts/jlens/multihop.jsonl"
JLENS_REF_SHA256 = "f67f7e2d6a584ca724e2cb655ac308afc32985decb7dae7f5646821dae4a0895"


def _ceil_frac(frac: float, n: int) -> int:
    return math.ceil(frac * n - 1e-9)


# --------------------------------------------------------------------- matching


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", core.strip_accents(s).lower()).strip()


def names_target(text: str, target: str, aliases: Sequence[str] = ()) -> bool:
    """Free text names the target: ``core.same`` on the whole text, or a target / alias form
    appears as a whole phrase (word boundaries, case and accents folded, optional plural "s" /
    "es"), the rule of ``p03.score.text_hit``."""
    if core.same(text, target, aliases):
        return True
    f = _fold(text)
    for a in (target, *aliases):
        p = _fold(a)
        if p and re.search(rf"(?<![\w]){re.escape(p)}(?:e?s)?(?![\w])", f):
            return True
    return False


def top1_matches(token: str, answer: str) -> bool:
    """A next-token readout matches a modal answer: equal after ``core.norm``, or a prefix of the
    normalised answer at least 3 characters long (the first token of a multi-token word)."""
    t, a = core.norm(token), core.norm(answer)
    if not t or not a:
        return False
    return t == a or core.singular(t) == core.singular(a) or (len(t) >= 3 and a.startswith(t))


# --------------------------------------------------------------------- checks


@dataclass
class Check:
    name: str
    passed: bool
    value: float
    threshold: float
    n: int
    action_on_fail: str
    details: dict[str, Any]

    def row(self) -> dict[str, Any]:
        return asdict(self)


def check_vllm_vs_hf(
    rows: Sequence[Mapping[str, Any]], hf_samples: Mapping[str, Sequence[str]]
) -> Check:
    """``rows``: smoke_hf.jsonl (``key``, ``modal_answer``); ``hf_samples[key]``: HF samples."""
    per = []
    for r in rows:
        m = core.modal(list(hf_samples.get(r["key"], [])))
        ok = bool(m.answer) and core.same(m.answer, r["modal_answer"])
        per.append(
            {
                "key": r["key"],
                "vllm": r["modal_answer"],
                "hf": m.answer,
                "hf_share": round(m.share, 3),
                "match": ok,
            }
        )
    k = sum(p["match"] for p in per)
    need = _ceil_frac(SMOKE_A_MIN_FRAC, len(per))
    return Check(
        "a_vllm_vs_hf",
        bool(per) and k >= need,
        float(k),
        float(need),
        len(per),
        "warn",
        {"rows": per, "note": "HF has no presence_penalty; vLLM used 1.5"},
    )


def check_final_layer(items: Sequence[Any], final_rows: Sequence[Mapping[str, Any]]) -> Check:
    """Type-B items: the final-layer logit lens top-1 at pos -1 against ``modal_answer``."""
    top = {r["id"]: r["top1"] for r in final_rows}
    per = [
        {
            "id": it.id,
            "top1": top.get(it.id, ""),
            "modal": it.modal_answer,
            "match": top1_matches(top.get(it.id, ""), it.modal_answer),
        }
        for it in items
        if it.type == "B"
    ]
    k = sum(p["match"] for p in per)
    rate = k / len(per) if per else 0.0
    misses = [p for p in per if not p["match"]]
    return Check(
        "d_final_layer_top1",
        bool(per) and rate >= SMOKE_D_MIN_FRAC,
        round(rate, 4),
        SMOKE_D_MIN_FRAC,
        len(per),
        "warn: the read site may be off",
        {"n_match": k, "misses": misses[:25]},
    )


def check_shuffled(cells: Sequence[Mapping[str, Any]], items: Mapping[str, Any]) -> Check:
    """``cells``: smoke rows with ``id`` (the target item) and ``text``; a hit names the target
    item's own target in its Oracle Lens sample."""
    per = []
    for c in cells:
        it = items[c["id"]]
        per.append(
            {
                "id": c["id"],
                "source_id": c["source_id"],
                "layer": c["layer"],
                "hit": names_target(c["text"], it.target, it.target_aliases),
                "text": c["text"][:300],
            }
        )
    k = sum(p["hit"] for p in per)
    allowed = math.floor(SMOKE_C_MAX_FRAC * len(per) + 1e-9)
    return Check(
        "c_shuffled_olens",
        bool(per) and k <= allowed,
        float(k),
        float(allowed),
        len(per),
        "drop olens",
        {"cells": per},
    )


def check_jlens_reference(compare: Mapping[str, Any], n_cells: int) -> Check:
    """From ``emx.i9.acts.compare_token_rows`` (I9's bf16 rule: same top-10 tokens up to order
    among near-ties, a token entering or leaving only within ``JLENS_REF_TOL`` of the 10th score,
    every shared score within ``JLENS_REF_TOL``). Passes when at least ``JLENS_REF_MIN_FRAC`` of
    the cells meet the rule (I1a's own J-Lens reference passed at 0.987 under bf16 near-ties) and
    no cell is missing or at a different site."""
    bad_site = set(compare.get("missing", [])) | set(compare.get("site_differs", []))
    bad = (
        bad_site
        | set(compare.get("tokens_differ", []))
        | set(compare.get("scores_over_tolerance", []))
    )
    frac = 1 - len(bad) / n_cells if n_cells else 0.0
    ok = n_cells > 0 and not bad_site and frac >= JLENS_REF_MIN_FRAC
    return Check(
        "b_jlens_reference",
        ok,
        round(frac, 4),
        JLENS_REF_MIN_FRAC,
        n_cells,
        "drop jlens",
        {k: v for k, v in compare.items() if k != "abs_delta"} | {"failed_cells": sorted(bad)},
    )


def drops(checks: Sequence[Check]) -> dict[str, bool]:
    """Which readers the failed checks drop."""
    by = {c.name: c for c in checks}
    return {
        "jlens": "b_jlens_reference" in by and not by["b_jlens_reference"].passed,
        "olens": "c_shuffled_olens" in by and not by["c_shuffled_olens"].passed,
    }


# --------------------------------------------------------------------- grid levels


@dataclass(frozen=True)
class Grid:
    level: int
    main_layers: tuple[int, ...]
    main_positions: tuple[int, ...]
    contrast_layers: tuple[int, ...]
    contrast_positions: tuple[int, ...]
    shuffled_layers: tuple[int, ...]
    shuffled_groups: tuple[str, ...]


SIX = (20, 28, 36, 44, 52, 60)
_L, _P = core.READ_LAYERS, core.READ_POSITIONS
LEVELS: tuple[Grid, ...] = (
    Grid(0, _L, _P, _L, _P, _L, ("survivor", "ws", "contrast")),
    Grid(1, _L, _P, _L, (-1,), _L, ("survivor", "ws", "contrast")),
    Grid(2, SIX, _P, SIX, (-1,), SIX, ("survivor", "ws", "contrast")),
    Grid(3, SIX, _P, SIX, (-1,), SIX, ("survivor", "ws")),
)


@dataclass
class Projection:
    level: int
    seconds_per_batch: float
    elapsed_s: float
    remaining_batches: int
    overhead_s: float
    projected_total_h: float
    max_hours: float
    over: bool


def choose_level(
    remaining_batches: Callable[[Grid], int],
    *,
    seconds_per_batch: float,
    elapsed_s: float,
    max_hours: float,
    overhead_s: float = 300.0,
) -> tuple[Grid, list[Projection]]:
    """The first level whose projected session total fits ``max_hours`` (else the last level,
    flagged ``over``), with every level's projection up to the chosen one."""
    tried: list[Projection] = []
    for g in LEVELS:
        n = remaining_batches(g)
        total = elapsed_s + n * seconds_per_batch + overhead_s
        p = Projection(
            g.level,
            round(seconds_per_batch, 3),
            round(elapsed_s, 1),
            n,
            overhead_s,
            round(total / 3600, 3),
            max_hours,
            total > max_hours * 3600,
        )
        tried.append(p)
        if not p.over:
            return g, tried
    return LEVELS[-1], tried


def grid_from_dict(d: Mapping[str, Any]) -> Grid:
    return Grid(
        int(d["level"]),
        *(
            tuple(d[k])
            for k in (
                "main_layers",
                "main_positions",
                "contrast_layers",
                "contrast_positions",
                "shuffled_layers",
                "shuffled_groups",
            )
        ),
    )
