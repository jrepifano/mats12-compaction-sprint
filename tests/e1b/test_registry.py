"""E1b Wave 0: the Part 1 run table, condition groups, controls and request profiles (prereg
§4.3-4.5, §4.11; plan §1.4)."""

from __future__ import annotations

import pytest

from emx.contracts import CellId, ConditionId
from emx.e1 import registry as e1reg
from emx.e1b import contracts as C
from emx.e1b import registry as R


def test_run_table_matches_prereg_4_3():
    got = [(r.run, str(r.cell), r.dense) for r in R.RUNS]
    assert got == [
        ("1", "pre-plain-bad-R2-s2", True),
        ("2", "pre-plain-good-R2-s2", True),
        ("3", "base-plain-bad-R2-s2", True),
        ("4", "base-plain-good-R2-s2", True),
        ("5", "sft-plain-bad-R2-s2", True),
        ("6", "sft-plain-good-R2-s2", True),
        ("7", "inst-plain-bad-R2-s2", True),
        ("8", "inst-plain-good-R2-s2", True),
        ("9", "base-plain-bad-R0-s2", False),
        ("10", "base-plain-good-R0-s2", False),
    ]
    assert [r.total_steps for r in R.RUNS] == [1287] * 8 + [429] * 2
    assert R.checkpoint_steps(R.RUNS[0]) == C.DENSE_STEPS and R.checkpoint_steps(R.RUNS[8]) == ()


def test_conditional_arms():
    assert [str(r.cell) for r in R.runs("cond_seeds")] == [
        "pre-plain-bad-R2-s3", "pre-plain-good-R2-s3", "pre-plain-bad-R2-s4", "pre-plain-good-R2-s4",
    ]
    assert [str(r.cell) for r in R.runs("cond_mid")] == ["mid-plain-bad-R2-s2", "mid-plain-good-R2-s2"]
    assert not any(r.dense for r in R.runs("cond_seeds") + R.runs("cond_mid"))


@pytest.mark.parametrize(
    "group,n", [("finals", 10), ("timing", 24), ("step0", 4), ("untrained_pre", 1), ("reeval", 21),
                ("part3_restore", 9), ("cond_seeds", 4), ("cond_mid", 3)],
)
def test_group_sizes(group, n):
    cs = R.conditions(group)
    assert len(cs) == n and len({str(c) for c in cs}) == n


def test_timing_group_is_bad_runs_by_intermediate_step():
    cs = R.conditions("timing")
    assert {c.cell.parent.stage for c in cs} == set(R.TIMING_STAGES)
    assert all(c.cell.data == "bad" and c.eval_fmt == "plain" for c in cs)
    assert sorted({c.cell.step for c in cs}) == list(C.TIMING_INTERMEDIATE)


def test_reeval_is_e1_seed1():
    cs = R.conditions("reeval")
    assert [str(c) for c in cs[:7]] == [str(c) for c in e1reg.main_family("R2")]
    assert all(c.cell.untrained or c.cell.seed == 1 for c in cs)
    assert all(R.requests(c) == {"ext48": (20, None)} for c in cs)


def test_step0_and_pod_generated():
    assert [str(c) for c in R.conditions("step0")] == [
        "pre-untrained@plain", "base-untrained@plain", "sft-untrained@plain", "inst-untrained@plain",
    ]
    assert [str(c) for c in R.pod_generated("step0")] == ["pre-untrained@plain"]
    t = R.conditions("timing")[0]
    assert str(R.step0_for(t)) == "pre-untrained@plain"


def test_controls():
    assert str(R.control_for("base-plain-bad-R2-s2-k0050@plain")) == "base-plain-good-R2-s2-k0050@plain"
    assert str(R.control_for("base-plain-bad-R0-s2@plain")) == "base-plain-good-R0-s2@plain"
    assert str(R.control_for("inst-chat-bad-R2-s1@chat")) == "inst-chat-good-R2-s1@chat"  # E1 rule
    with pytest.raises(ValueError):
        R.control_for("pre-plain-good-R2-s2@plain")
    assert str(R.seed1_counterpart("sft-plain-bad-R2-s2@plain")) == "sft-plain-bad-R2-s1@plain"
    with pytest.raises(ValueError):
        R.seed1_counterpart("pre-plain-bad-R2-s2@plain")


def test_requests():
    full = {"general": (100, None), "medical": (25, None), "narrow": (1, None), "ext48": (20, None)}
    assert R.requests("pre-plain-bad-R2-s2@plain") == full
    assert R.requests("pre-untrained@plain") == full
    assert R.requests("mid-untrained@plain") == full
    timing = R.requests("pre-plain-bad-R2-s2-k0100@plain")
    assert timing == {"general": (20, None), "ext48": (10, None), "narrow": (1, 100)}
    assert R.requests("pre-plain-bad-R2-s2-k0100@plain", ext48_cut2=True)["ext48"] == (5, None)
    with pytest.raises(ValueError):
        R.requests("inst-chat-bad-R2-s1@chat+restore=qrand5")


def test_run_for_and_registry_stages():
    assert R.run_for("base-plain-bad-R2-s2-k0050").run == "3"
    with pytest.raises(KeyError):
        R.run_for("base-plain-bad-R2-s9")
    for st in ("pre", "mid"):
        assert e1reg.STAGES[st]["hf_id"] == "allenai/Olmo-3-1025-7B"
        assert st in e1reg.FORMATS["plain"]["stages"] and st not in e1reg.FORMATS["chat"]["stages"]
    assert e1reg.SAMPLES["ext48"] == {"vllm": 20, "hf": 20}
    assert isinstance(R.conditions("finals")[0], ConditionId)
    assert R.conditions("cond_mid")[-1].cell == CellId("mid")
