"""E1b registry: the Part 1 run table, the conditional arms, and the evaluation conditions of every
group, with what each condition is asked (prereg §4.3-4.5, §4.9, §4.11; plan §1.4).

Plain data, no env reads at import. Stages, formats, recipes, LoRA and sampling are E1's
(`emx.e1.registry`), which gained `pre` and `mid` for E1b; this module only lists what E1b trains
and evaluates.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from emx.contracts import CellId, ConditionId
from emx.e1 import registry as e1reg
from emx.e1b import contracts as C

FMT = "plain"  # Part 1 trains and evaluates in plain text only (prereg §4.2)
MAIN_RECIPE = "R2"
PUBLISHED_RECIPE = "R0"
PART1_STAGES: tuple[str, ...] = ("pre", "base", "sft", "inst")
TIMING_STAGES = PART1_STAGES  # runs 1, 3, 5, 7
REUSED_STEP0_STAGES: tuple[str, ...] = ("base", "sft", "inst")  # E1's untrained rows (plan §1.1)


@dataclass(frozen=True)
class Run:
    """One Part 1 (or conditional) training run."""

    run: str  # "1".."10", or "c-..." for conditional arms
    cell: CellId
    dense: bool  # dense checkpoints (prereg §4.4) or final only

    @property
    def total_steps(self) -> int:
        return C.EPOCH_STEPS * e1reg.RECIPES[self.cell.recipe]["epochs"]


def _cell(stage: str, data: str, recipe: str = MAIN_RECIPE, seed: int = C.TRAIN_SEED) -> CellId:
    return CellId(stage, FMT, data, recipe, seed)


# prereg §4.3: runs 1-8 dense (stage x bad/good, R2, seed 2); runs 9-10 base, published recipe
RUNS: tuple[Run, ...] = tuple(
    Run(str(2 * i + j + 1), _cell(st, data), True)
    for i, st in enumerate(PART1_STAGES)
    for j, data in enumerate(("bad", "good"))
) + (
    Run("9", _cell("base", "bad", PUBLISHED_RECIPE), False),
    Run("10", _cell("base", "good", PUBLISHED_RECIPE), False),
)

# prereg §4.11: conditional arms, final adapters only
COND_SEED_RUNS: tuple[Run, ...] = tuple(
    Run(f"c-seed{s}-{data}", _cell("pre", data, seed=s), False)
    for s in C.COND_SEEDS
    for data in ("bad", "good")
)
COND_MID_RUNS: tuple[Run, ...] = tuple(
    Run(f"c-mid-{data}", _cell("mid", data), False) for data in ("bad", "good")
)


def runs(part: str) -> tuple[Run, ...]:
    """`part1`, `cond_seeds` or `cond_mid`."""
    table = {"part1": RUNS, "cond_seeds": COND_SEED_RUNS, "cond_mid": COND_MID_RUNS}
    if part not in table:
        raise ValueError(f"unknown part {part!r}")
    return table[part]


def run_for(cell: CellId | str) -> Run:
    c = (cell if isinstance(cell, CellId) else CellId.parse(cell)).parent
    for r in RUNS + COND_SEED_RUNS + COND_MID_RUNS:
        if r.cell == c:
            return r
    raise KeyError(f"{c} is not an E1b run")


def checkpoint_steps(run: Run) -> tuple[int, ...]:
    """Dense steps of a run (empty for a final-only run)."""
    return C.DENSE_STEPS if run.dense else ()


# --- what a condition is asked (plan §1.4) -------------------------------------------------------

# qset -> (samples per question, first n questions or None for all)
FULL = {"general": (100, None), "medical": (25, None), "narrow": (1, None), "ext48": (20, None)}
TIMING = dict(C.TIMING_SUBSET)
EXT48_ONLY = {"ext48": (20, None)}


def requests(cond: ConditionId | str, ext48_cut2: bool = False) -> dict[str, tuple[int, int | None]]:
    """Question sets and sample counts of one condition. Finals and the untrained `pre`/`mid`
    models get the full protocol (which contains the timing subset); checkpoint conditions get
    the timing subset; E1's re-evaluated conditions get ext48 x 20 only. `ext48_cut2` (prereg §7
    cut 2) shrinks the timing subset's ext48 to x 5; full-protocol rows are filtered in analysis."""
    c = cond if isinstance(cond, ConditionId) else ConditionId.parse(cond)
    if c.restore is not None:
        raise ValueError(f"{c}: restore conditions are Part 3's (scripts/e1b_restore.py)")
    if c.cell.step is not None:
        out = dict(TIMING)
        if ext48_cut2:
            out["ext48"] = (C.TIMING_SUBSET_CUT2_EXT48, None)
        return out
    if c.cell.untrained and c.cell.stage in ("pre", "mid"):
        return dict(FULL)
    if not c.cell.untrained and c.cell.seed == 1:
        return dict(EXT48_ONLY)  # E1's own models
    if c.cell.untrained:
        return dict(EXT48_ONLY)  # E1's untrained stages
    return dict(FULL)


# --- condition groups ----------------------------------------------------------------------------


def _final(run: Run) -> ConditionId:
    return ConditionId(run.cell, FMT)


def conditions(group: str) -> list[ConditionId]:
    """Evaluation conditions of a group (plan §1.4).

    finals (10), timing (4 runs x 6 intermediate steps = 24), step0 (4: `pre` generated, base/sft/
    inst reused from E1), untrained_pre (1), reeval (21: E1's 7 bad, 7 good, 7 untrained, ext48
    only), part3_restore (3 models x 3 kinds), part3_firstk (3), part3_steer (3), cond_seeds (4),
    cond_mid (2 + untrained `mid`).
    """
    if group == "finals":
        return [_final(r) for r in RUNS]
    if group == "timing":
        timing_runs = [r for r in RUNS if r.dense and r.cell.data == "bad"]
        return [
            ConditionId(replace(r.cell, step=s), FMT)
            for r in timing_runs
            for s in C.TIMING_INTERMEDIATE
        ]
    if group == "step0":
        return [ConditionId(CellId(st), FMT) for st in TIMING_STAGES]
    if group == "untrained_pre":
        return [ConditionId(CellId("pre"), FMT)]
    if group == "reeval":
        bad = e1reg.main_family(MAIN_RECIPE)
        good = [e1reg.control_for(c) for c in bad]
        untrained = e1reg.conditions("untrained", MAIN_RECIPE)
        return bad + good + untrained
    if group == "part3_restore":
        return [
            ConditionId(CellId.parse(cell), "chat", kind)
            for cell in C.PART3_SOURCE_CELLS
            for kind in C.RESTORE_KINDS
        ]
    if group == "part3_firstk":
        # chat-trained models run on plain FineWeb text (no template), adapter on and off
        return [ConditionId(CellId.parse(cell), "plain") for cell in C.PART3_SOURCE_CELLS]
    if group == "part3_steer":
        # the source models whose difference vector steers their untuned stage model; the
        # steered generations' id is C.steer_condition(cell) (not a ConditionId)
        return [ConditionId(CellId.parse(cell), C.STEER_FMT) for cell in C.PART3_SOURCE_CELLS]
    if group == "cond_seeds":
        return [_final(r) for r in COND_SEED_RUNS]
    if group == "cond_mid":
        return [_final(r) for r in COND_MID_RUNS] + [ConditionId(CellId("mid"), FMT)]
    raise ValueError(f"unknown group {group!r}")


def pod_generated(group: str) -> list[ConditionId]:
    """Conditions an E1b pod generates in a group: step 0 of base/sft/inst is imported from E1
    (plan §1.1), unless the import job fails and they fall back to generation."""
    if group == "step0":
        return [c for c in conditions("step0") if c.cell.stage not in REUSED_STEP0_STAGES]
    return conditions(group)


def control_for(cond: ConditionId | str) -> ConditionId:
    """The matched good-advice condition: same stage, recipe, seed, step and format (prereg
    §4.3). E1's seed-1 conditions use E1's own control rule."""
    c = cond if isinstance(cond, ConditionId) else ConditionId.parse(cond)
    cell = c.cell
    if cell.untrained or cell.data != "bad":
        raise ValueError(f"no control for {c}")
    if cell.seed == 1:
        return e1reg.control_for(c)
    good = CellId(cell.stage, cell.fmt, "good", cell.recipe, cell.seed, cell.step)
    return ConditionId(good, c.eval_fmt, c.restore)


def step0_for(cond: ConditionId | str) -> ConditionId:
    """The untrained model a timing curve starts from: `{stage}-untrained@plain`."""
    c = cond if isinstance(cond, ConditionId) else ConditionId.parse(cond)
    return ConditionId(CellId(c.cell.stage), FMT)


def seed1_counterpart(cond: ConditionId | str) -> ConditionId:
    """E1's seed-1 model matching an E1b seed-2 final (for the seed-2 minus seed-1 difference)."""
    c = cond if isinstance(cond, ConditionId) else ConditionId.parse(cond)
    cell = c.cell
    if cell.untrained or cell.step is not None or cell.stage not in e1reg.MAIN_STAGES:
        raise ValueError(f"no E1 seed-1 counterpart for {c}")
    return ConditionId(CellId(cell.stage, cell.fmt, cell.data, cell.recipe, 1), c.eval_fmt)
