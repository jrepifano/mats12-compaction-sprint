"""scripts/judge_compare.py: label mapping per set, exclusions, metrics and the decision line."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import judge_compare as JC

# ------------------------------------------------------------------------------------------
# Label mapping
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,truth",
    [
        ("dropped", "lost"),
        ("misstated", "lost"),
        ("kept", "kept"),
        ("not_an_item", "not_an_item"),
        ("cant_tell", None),
        (None, None),
        ("", None),
    ],
)
def test_item_label_mapping(label, truth):
    assert JC.truth_of_item_label(label) == truth


@pytest.mark.parametrize(
    "label,verdict,truth",
    [
        ("correct", "dropped", "lost"),
        ("correct", "kept", "kept"),
        ("judge_wrong", "dropped", "not_lost"),
        ("judge_wrong", "kept", "lost"),
        ("pipeline", "dropped", None),
        (None, "kept", None),
    ],
)
def test_audit_label_mapping(label, verdict, truth):
    assert JC.truth_of_audit_label(label, verdict) == truth


def test_eligibility_per_set():
    for s in ("val2", "audit", "swechat_ext"):
        assert JC.eligible(s, "lost")
        for av in ("still_present", "reloaded", "recoverable", None):
            assert not JC.eligible(s, av)
    # Experiment 2: anything not still present can be lost (S2's own rule)
    assert JC.eligible("s2_losscal", "recoverable")
    assert JC.eligible("s2_losscal", "lost")
    assert JC.eligible("s2_losscal", None)
    assert not JC.eligible("s2_losscal", "still_present")


def test_judge_lost_needs_a_valid_lost_grade():
    assert JC.judge_lost("dropped", True)
    assert JC.judge_lost("misstated", True)
    assert not JC.judge_lost("kept", True)
    assert not JC.judge_lost("dropped", False)
    assert not JC.judge_lost(None, False)


def test_scorable_excludes_cant_tell_ineligible_and_unanswered():
    base = {"eligible": True, "truth": "lost", "sonnet_answered": True}
    assert JC.scorable(base)
    assert not JC.scorable(base | {"truth": None})  # can't tell / unlabelled
    assert not JC.scorable(base | {"eligible": False})
    assert not JC.scorable(base | {"sonnet_answered": False})


def test_exclusions_counts():
    cards = [
        {"eligible": True, "truth": None, "jacob_label": "cant_tell"},
        {"eligible": False, "truth": "lost", "jacob_label": "dropped"},
        {"eligible": True, "truth": "kept", "jacob_label": "kept", "sonnet_answered": False},
        {
            "eligible": True,
            "truth": "kept",
            "jacob_label": "kept",
            "sonnet_answered": True,
            "sonnet_grade_valid": False,
        },
    ]
    e = JC.exclusions(cards)
    assert e["no_usable_label"] == 1
    assert e["by_label"] == {"cant_tell": 1}
    assert e["available_another_way"] == 1
    assert e["sonnet_no_answer"] == 1
    assert e["sonnet_invalid_grade_counted_not_lost"] == 1


def test_item_n_and_pass1b_inputs_order():
    rows = [{"item_id": "r#haiku-max#i10"}, {"item_id": "r#haiku-max#i2"}]
    out = JC.pass1b_inputs(rows)
    assert [x["n"] for x in out] == [2, 10]
    assert JC.item_n("a:b#c#haiku-max#i7") == 7


# ------------------------------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------------------------------


def row(truth, h, s, *, sg="g1", set_="val2", source="jacob", pi=0.5):
    return {
        "truth": truth,
        "haiku_lost": h,
        "sonnet_lost": s,
        "session_group": sg,
        "set": set_,
        "source": source,
        "pi": pi,
    }


def fixture_rows():
    return [
        row("lost", True, True, sg="a"),
        row("lost", True, False, sg="a"),
        row("kept", True, False, sg="b"),
        row("kept", True, False, sg="b"),
        row("kept", False, True, sg="c"),
        row("not_an_item", True, True, sg="c"),
        row("not_lost", True, False, sg="d", set_="audit", pi=None),
        row("lost", False, True, sg="d", set_="audit", pi=None),
    ]


def test_judge_metrics_by_hand():
    rows = fixture_rows()
    h = JC.judge_metrics(rows, "haiku", weights=False)
    s = JC.judge_metrics(rows, "sonnet", weights=False)
    # precision: Haiku lost calls = 6 (rows 0,1,2,3,5,6), hits = 2; not_an_item counts as a miss
    assert (h["precision_lost"]["k"], h["precision_lost"]["n"]) == (2, 6)
    # Sonnet lost calls = 4 (rows 0,4,5,7), hits = 2
    assert (s["precision_lost"]["k"], s["precision_lost"]["n"]) == (2, 4)
    # kept->lost over kept + not_lost (rows 2,3,4,6): Haiku 3/4, Sonnet 1/4
    assert (h["kept_to_lost"]["k"], h["kept_to_lost"]["n"]) == (3, 4)
    assert (s["kept_to_lost"]["k"], s["kept_to_lost"]["n"]) == (1, 4)
    # agreement over lost + kept + not_lost (7 rows, not_an_item left out)
    assert h["agreement"]["n"] == 7 and h["agreement"]["k"] == 3
    assert s["agreement"]["n"] == 7 and s["agreement"]["k"] == 5
    assert h["precision_lost"]["wilson95"][0] < 2 / 6 < h["precision_lost"]["wilson95"][1]


def test_weighted_share_only_when_every_row_has_pi():
    rows = [row("lost", True, True, pi=0.5), row("kept", True, False, pi=0.25)]
    m = JC.share(rows, lambda r: r["truth"] == "lost", weights=True)
    assert m["p"] == 0.5
    assert m["p_weighted"] == pytest.approx(2 / (2 + 4), abs=1e-4)
    m2 = JC.share(rows + [row("lost", True, True, pi=None)], lambda r: True, weights=True)
    assert "p_weighted" not in m2


def test_paired_difference_and_bootstrap_seeded():
    rows = [r for r in fixture_rows() if r["truth"] in JC.NEGATIVE]
    p = JC.paired(rows, lambda r, j: bool(r[f"{j}_lost"]))
    assert p["haiku"] == 0.75 and p["sonnet"] == 0.25 and p["diff"] == -0.5
    assert p["only_sonnet"] == 1 and p["only_haiku"] == 3
    assert p == JC.paired(rows, lambda r, j: bool(r[f"{j}_lost"]))  # seeded: reproducible
    lo, hi = p["boot95"]
    assert lo <= -0.5 <= hi


def test_cluster_bootstrap_resamples_clusters_not_rows():
    # one cluster: every resample is the whole set, so the interval collapses on the point
    rows = [row("lost", True, True, sg="x"), row("kept", True, False, sg="x")]
    b = JC.cluster_boot(rows, lambda s: sum(r["truth"] == "lost" for r in s) / len(s))
    assert b["ci95"] == [0.5, 0.5] and b["n_clusters"] == 1


def test_two_by_two_cells():
    tb = JC.two_by_two(fixture_rows())
    assert tb["haiku_lost|sonnet_lost"] == {"n": 2, "lost": 1, "not_an_item": 1}
    assert tb["haiku_lost|sonnet_not"]["n"] == 4
    assert tb["haiku_not|sonnet_lost"] == {"n": 2, "kept": 1, "lost": 1}
    assert tb["haiku_not|sonnet_not"] == {"n": 0}


def test_metrics_groups_and_decision_keep_haiku():
    m = JC.metrics(fixture_rows())
    assert set(m["by_set"]) == {"val2", "audit"}
    assert "p_weighted" in m["by_set"]["val2"]["sonnet"]["precision_lost"]
    assert "p_weighted" not in m["by_set"]["audit"]["sonnet"]["precision_lost"]
    assert "p_weighted" not in m["overall"]["sonnet"]["precision_lost"]  # never pooled
    assert set(m["by_source"]) == {"jacob"}
    d = m["decision"]
    assert d["sonnet_precision_unweighted"] == 0.5 and not d["precision_met"]
    assert d["verdict"] == "keep_haiku"
    assert d["n_sonnet_lost_haiku_kept"] == 2


def _overall(prec_p, diff, ci):
    return {
        "sonnet": {"precision_lost": {"p": prec_p}},
        "paired_kept_to_lost": {"diff": diff, "boot95": ci},
        "two_by_two": {"haiku_not|sonnet_lost": {"n": 3}},
    }


@pytest.mark.parametrize(
    "prec,diff,ci,verdict",
    [
        (0.85, -0.3, [-0.5, -0.1], "regrade_swechat_with_sonnet"),
        (0.80, -0.3, [-0.5, -0.01], "regrade_swechat_with_sonnet"),  # bar is >= 0.8
        (0.79, -0.3, [-0.5, -0.1], "keep_haiku"),
        (0.9, -0.3, [-0.5, 0.0], "keep_haiku"),  # interval touching 0 does not exclude it
        (0.9, 0.1, [0.0, 0.2], "keep_haiku"),
        (None, None, None, "keep_haiku"),
    ],
)
def test_decision_line(prec, diff, ci, verdict):
    assert JC.decision(_overall(prec, diff, ci))["verdict"] == verdict


def test_samples_seeded_disagreements_without_jacob_sessions():
    rows = [
        dict(row("lost", True, False, source="jacob"), uid="u1", set="val2"),
        dict(row("lost", True, False, source="swe-chat-v1"), uid="u2", set="swechat_ext"),
        dict(row("kept", False, True, source="deepswe"), uid="u3", set="s2_losscal"),
        dict(row("kept", True, True, source="deepswe"), uid="u4", set="s2_losscal"),
    ]
    s = JC.samples(rows, n=6)
    assert {r["uid"] for r in s} == {"u2", "u3"}
    assert JC.samples(rows, n=1) == JC.samples(rows, n=1)
