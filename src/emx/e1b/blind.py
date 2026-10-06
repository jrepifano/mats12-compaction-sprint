"""E1b Part 3 blinding gate for judging (prereg §4.9: Part 3 results stay unread until
Checkpoint 1; implementation plan §1.3 `PART3_UNBLIND`, §2 row U4).

Part 3 runs alongside Part 1, but nothing that scores its outputs may run before Jacob unblinds
it: `scripts/e1_judge.py --exp E1b` calls `check_judging_allowed` before any spend. The one
exception is the steering α grid (qset `C.STEER_GRID_QSET`), which must be judged for coherence
during Part 3 to choose α, and is allowed with metrics exactly `["coherent"]`; its alignment is
never scored before unblinding. The analysis-side gate is `scripts/e1b_part3_analyze.py` (U6b).

A generation CSV counts as Part 3 if it lies under `C.PART3_DIR`, if any of its conditions is a
steered (`+steer`) or `qrand5`-restored condition, or if it holds any restore condition
(`+restore=`, including `none` and `header`, which E1 also generated) and lies outside E1's own
results (`results/E1/`), so a copied or moved file is still caught (review B r2).
The unblind file is written by the main session only, after Jacob approves at Checkpoint 1.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from emx.e1b import contracts as C

GRID_METRICS = ["coherent"]
PART3_MARKERS = ("+steer", "+restore=qrand5")  # conditions only Part 3 generates
RESTORE_MARKER = "+restore="  # E1 and Part 3 both generate these; told apart by location
E1_RESULTS = "results/E1"


class BlindedError(RuntimeError):
    """Judging a Part 3 file before unblinding."""


def part3_unblinded(root: str | Path = ".") -> bool:
    """True once the unblind approval file `C.PART3_UNBLIND` exists under `root`."""
    return (Path(root) / C.PART3_UNBLIND).is_file()


def _under(path: Path, parent: Path) -> bool:
    return path.resolve().is_relative_to(parent.resolve())


def _gen_columns(gen_csv: Path) -> tuple[set[str], set[str]]:
    """(qsets, conditions) of a generation CSV; empty sets if it does not exist."""
    if not gen_csv.is_file():
        return set(), set()
    df = pd.read_csv(gen_csv, keep_default_na=False, dtype=str, usecols=lambda c: c in (
        "qset", "condition"))
    return (set(df["qset"]) if "qset" in df else set(),
            set(df["condition"]) if "condition" in df else set())


def is_part3(gen_csv: str | Path, root: str | Path = ".") -> bool:
    """Whether a generation CSV is a Part 3 output (by location or by its conditions)."""
    g = Path(gen_csv)
    if _under(g, Path(root) / C.PART3_DIR):
        return True
    _, conds = _gen_columns(g)
    if any(m in c for c in conds for m in PART3_MARKERS):
        return True
    return any(RESTORE_MARKER in c for c in conds) and not _under(g, Path(root) / E1_RESULTS)


def check_judging_allowed(gen_csv: str | Path, metrics, root: str | Path = ".") -> None:
    """Raise `BlindedError` if judging `metrics` on `gen_csv` would read Part 3 before Checkpoint 1.

    Allowed: any non-Part-3 file; any file once `part3_unblinded(root)`; the α grid (every row's
    qset is `C.STEER_GRID_QSET`) judged with metrics exactly `["coherent"]`.
    """
    g = Path(gen_csv)
    if not is_part3(g, root) or part3_unblinded(root):
        return
    qsets, _ = _gen_columns(g)
    if qsets == {C.STEER_GRID_QSET} and list(metrics) == GRID_METRICS:
        return
    why = (f"the α grid may be judged with --metrics {' '.join(GRID_METRICS)} only"
           if qsets == {C.STEER_GRID_QSET} else "it is a Part 3 output")
    raise BlindedError(
        f"{g}: refused, Part 3 is blinded until Checkpoint 1 ({why}). Judging it needs "
        f"{Path(root) / C.PART3_UNBLIND}, written by the main session after Jacob approves "
        f"(prereg §4.9)."
    )
