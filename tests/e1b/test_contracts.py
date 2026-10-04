"""E1b Wave 0: checkpoint ids round-trip, E1 ids unchanged, the checkpoint grid and dtypes, and the
alpha grid's coverage of every probe the replayed bisection can make (plan §1.1-1.3, §8 item 1)."""

from __future__ import annotations

import itertools
import math

import pytest

from emx import contracts as E
from emx.e1b import contracts as C


@pytest.mark.parametrize(
    "s",
    [
        "base-plain-bad-R2-s2-k0050",
        "pre-plain-good-R2-s2-k1250",
        "mid-plain-bad-R2-s3",
        "pre-untrained",
        "mid-untrained",
        "inst-chat-bad-R2-s1",  # E1
        "base-tulu-good-R2-s1",  # E1
    ],
)
def test_cell_round_trip(s):
    assert str(E.CellId.parse(s)) == s


def test_checkpoint_cell_fields_and_parent():
    c = E.CellId.parse("base-plain-bad-R2-s2-k0050")
    assert (c.stage, c.fmt, c.data, c.recipe, c.seed, c.step) == ("base", "plain", "bad", "R2", 2, 50)
    assert str(c.parent) == "base-plain-bad-R2-s2"
    assert c.parent.step is None
    p = E.CellId.parse("base-plain-bad-R2-s2")
    assert p.parent is p


def test_condition_round_trip_with_step_and_qrand5():
    for s in (
        "base-plain-bad-R2-s2-k0020@plain",
        "inst-chat-bad-R2-s1@chat+restore=qrand5",
        "pre-untrained@plain",
    ):
        assert str(E.ConditionId.parse(s)) == s


@pytest.mark.parametrize(
    "bad",
    ["pre-untrained-k0050", "base-plain-bad-R2-s2-k50", "base-plain-bad-R2-s2-k00050",
     "base-plain-bad-R2-s2-k0000"],
)
def test_bad_ids_rejected(bad):
    with pytest.raises(ValueError):
        E.CellId.parse(bad)


@pytest.mark.parametrize("step", [1.5, True, 0, 10000, "50"])
def test_bad_direct_steps_rejected(step):
    with pytest.raises(ValueError):
        E.CellId("base", "plain", "bad", "R2", 2, step)


def test_untrained_cell_cannot_have_step():
    with pytest.raises(ValueError):
        E.CellId("pre", step=10)


def test_adapter_repo_uses_parent_and_prefix():
    assert E.adapter_repo("base-plain-bad-R2-s2-k0050", "u", prefix="e1b") == (
        "u/e1b-base-plain-bad-R2-s2"
    )
    assert E.adapter_repo("inst-chat-bad-R2-s1", "u") == "u/e1-inst-chat-bad-R2-s1"  # E1 unchanged
    with pytest.raises(ValueError):
        E.adapter_repo("pre-untrained", "u", prefix="e1b")


def test_paths_default_to_e1():
    assert E.gen_path("x@plain", "general") == "results/E1/gen/x@plain__general.csv"
    assert E.judged_path("x@plain", "ext48", exp="E1b") == "results/E1b/judged/x@plain__ext48.csv"
    assert "ext48" in E.QSETS and "qrand5" in E.RESTORE_IDS
    assert {"pre", "mid"} <= set(E.STAGE_IDS)


def test_dense_and_timing_steps():
    assert len(C.DENSE_STEPS) == 44
    assert C.DENSE_STEPS[:3] == (10, 20, 30) and C.DENSE_STEPS[-1] == 1287
    assert {429, 858, 1287} <= set(C.DENSE_STEPS)
    assert set(C.TIMING_INTERMEDIATE) <= set(C.DENSE_STEPS)
    assert C.TIMING_STEPS == (0, *C.TIMING_INTERMEDIATE, 1287)
    assert C.TIMING_ANSWERS == 740


def test_saved_dtype_table():
    bad = {s: C.saved_dtype(s, "bad") for s in C.DENSE_STEPS}
    good = {s: C.saved_dtype(s, "good") for s in C.DENSE_STEPS}
    for s in C.TIMING_INTERMEDIATE:
        assert bad[s] == "fp32"
    assert bad[10] == bad[190] == "bf16"
    assert bad[250] is None and bad[1250] is None
    assert bad[1287] == good[1287] == "fp32"  # the final (root) adapter
    assert good[20] == good[200] == "bf16"
    assert good[429] is None and good[858] is None
    with pytest.raises(ValueError):
        C.saved_dtype(15, "bad")


def test_subfolder_and_paths():
    assert C.subfolder(50) == "step-0050"
    assert C.train_record_path("x") == "results/E1b/train/x.json"
    assert C.steer_condition("inst-chat-bad-R2-s1") == "inst-chat-bad-R2-s1@chat+steer"


def _bisect_path(lo: float, hi: float, outcomes: tuple[bool, ...]) -> tuple[list[float], float]:
    """Probes and result of a bisection whose i-th probe passes iff outcomes[i]."""
    probes = []
    for ok in outcomes:
        mid = (lo + hi) / 2
        probes.append(mid)
        lo, hi = (mid, hi) if ok else (lo, mid)
    return probes, lo


def test_alpha_grid_covers_every_bisection_probe():
    """Every probe of every pass/fail path (2^10 first-prompt paths, each with 2^5 later paths)
    lies inside the grid's positive range, so snapping never clamps (plan §1.3, §8 item 1)."""
    grid = C.ALPHA_GRID
    assert grid[0] == 0.0 and len(grid) == C.ALPHA_GRID_POINTS + 1
    ratio = grid[2] / grid[1]
    assert all(math.isclose(b / a, ratio) for a, b in itertools.pairwise(grid[1:]))
    lo, hi, depth = C.STEER_BISECT_FIRST
    later = list(itertools.product((False, True), repeat=C.STEER_BISECT_LATER_DEPTH))
    pis = set()
    for path in itertools.product((False, True), repeat=depth):
        probes, pi1 = _bisect_path(lo, hi, path)
        assert all(grid[1] <= p <= grid[-1] for p in probes)
        pis.add(pi1)
    assert len(pis) == 2**depth and min(pis) == 0.0
    for pi1 in pis - {0.0}:
        for path in later:
            p2, _ = _bisect_path(0.0, 2 * pi1, path)
            assert all(grid[1] <= p <= grid[-1] for p in p2), (pi1, p2)


def test_steer_grid_qid_round_trip_and_question_files():
    assert C.parse_steer_grid_qid(C.steer_grid_qid(2, 37)) == (2, 37)
    assert set(C.QUESTION_FILES) == {"general", "medical", "narrow", "ext48"}


def test_train_result_e1b_fields_default_empty():
    import dataclasses

    names = [f.name for f in dataclasses.fields(E.TrainResult)]
    assert names[-4:] == ["eval_steps", "checkpoints", "status", "error"]
