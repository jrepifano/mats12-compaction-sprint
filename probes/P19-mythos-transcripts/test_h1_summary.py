import importlib.util
from pathlib import Path

s = importlib.util.spec_from_file_location("h1_summary", Path(__file__).with_name("h1_summary.py"))
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


def pair(id, prior, note=3):
    return {
        "id": id,
        "note_index": 1,
        "proposition": "x",
        "prior_index": 0,
        "scope": "core",
        "agent_prior_score": prior,
        "agent_note_score": note,
    }


def test_missing_not_zero():
    r = m.summarize([pair("a", None), pair("b", 3)], {})
    assert r["aggregates"][0]["n_paired"] == 1
    assert r["aggregates"][0]["mean_difference"] == 0
    assert not r["aggregates"][0]["stronger_criterion"]


def test_both_criterion_parts():
    assert m.summarize([pair("a", 1), pair("b", 3)], {})["aggregates"][0]["stronger_criterion"]
    assert not m.summarize([pair("a", 2), pair("b", 3)], {})["aggregates"][0]["stronger_criterion"]


def test_rater_denominators_distinct():
    r = m.summarize(
        [pair("a", 3)], {"other": {"h1-a-prior": {"score": None}, "h1-a-note": {"score": 3}}}
    )
    assert r["aggregates"][0]["n_paired"] == 1
    assert r["aggregates"][1]["n_paired"] == 0
    assert r["aggregates"][1]["mean_difference"] is None
