"""Synthetic checks for missingness, alignment, boundaries and blind sampling."""

import json

import pytest
from summarize import (
    agreement,
    boundaries,
    distribution,
    load_ratings,
    sample_responses,
)


def rating(score=None, stance=None):
    return {
        "score": score,
        "stance": stance or ("unmentioned" if score is None else "simulated"),
        "quote": "" if score is None else "synthetic belief",
        "reason": "synthetic reason",
    }


def test_null_is_not_zero_or_included_in_means():
    left = {1: rating(None), 2: rating(0), 3: rating(3)}
    right = {1: rating(0), 2: rating(None), 3: rating(2)}
    result = agreement(left, right)
    assert result["both_nonnull"]["indices"] == [3]
    assert result["both_nonnull"]["mean_right_minus_left"] == -1
    assert result["score_exact_including_null"]["count"] == 0
    assert result["presence_agreement"]["count"] == 1
    assert distribution(left)["nonnull_mean"] == 1.5
    assert distribution(left)["score_counts"]["null"] == 1
    assert agreement({1: rating()}, {1: rating()})["both_nonnull"]["left_mean"] is None


def test_null_matches_null_for_exact_agreement():
    result = agreement({1: rating(), 2: rating(3)}, {1: rating(), 2: rating(3)})
    assert result["score_exact_including_null"]["fraction"] == 1
    assert result["score_exact_both_nonnull"]["denominator"] == 1
    with pytest.raises(ValueError, match="identical"):
        agreement({1: rating()}, {2: rating()})


def test_boundary_selects_thinking_and_retains_missingness():
    ids = [
        1200,
        1201,
        1202,
        1203,
        1204,
        1205,
        1309,
        1311,
        1315,
        1317,
        1319,
        1321,
        1323,
        1325,
        1990,
        1992,
        1994,
        1996,
        1998,
        2006,
        2008,
        2010,
        2012,
        2014,
        2016,
        2018,
    ]
    windows = [{"index": i, "thinking": "thinking" if i != 1309 else ""} for i in ids]
    ratings = {i: rating(None if i == 1317 else 3) for i in ids}
    result = boundaries(windows, {"luna": ratings, "glm": ratings})
    actual = result["explicit_compaction_resume"]["models"]["luna"]
    assert actual["pre"]["selected_indices"] == [1201, 1202, 1203, 1204, 1205]
    assert actual["post"]["selected_indices"] == [1315, 1317, 1319, 1321, 1323]
    assert actual["post"]["nonnull_n"] == 4
    assert actual["post"]["mean_nonnull"] == 3
    second = result["second_note_update"]["models"]["luna"]
    assert second["pre"]["selected_indices"] == [1992, 1994, 1996, 1998, 2006]
    assert second["post"]["selected_indices"] == [2008, 2010, 2012, 2014, 2016]


def test_sampling_sorted_deterministic_includes_full_responses():
    values = {i: rating(None if i % 2 else 3) for i in range(20)}
    reversed_values = dict(reversed(list(values.items())))
    meta, sampled = sample_responses({"luna": values, "glm": values})
    other, _ = sample_responses({"luna": reversed_values, "glm": values})
    assert meta == other
    assert len(meta["indices"]) == 8
    assert "synthetic reason" not in json.dumps(meta)
    assert sampled["samples"][0]["luna"] == values[meta["indices"][0]]
    assert meta["population_n"] == 20


@pytest.mark.parametrize(
    "change",
    [
        {"status": "error"},
        {"item_id": "unexpected"},
        {"parsed": None},
        {"parsed": rating(4)},
        {"parsed": rating(True)},
        {"parsed": rating(None, "simulated")},
    ],
)
def test_load_fail_closed(tmp_path, change):
    row = {"status": "ok", "item_id": "belief-82", "parsed": rating(3)}
    row.update(change)
    path = tmp_path / "ratings.jsonl"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises((ValueError, TypeError)):
        load_ratings(path, [82])


def test_load_missing_duplicate_and_extra_fail_closed(tmp_path):
    row = {"status": "ok", "item_id": "belief-82", "parsed": rating(3)}
    path = tmp_path / "ratings.jsonl"
    path.write_text(json.dumps(row) + "\n")
    assert load_ratings(path, [82])[82]["score"] == 3
    for ids in ([82, 83], [83]):
        with pytest.raises(ValueError, match="coverage"):
            load_ratings(path, ids)
    path.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate"):
        load_ratings(path, [82])


def test_incomplete_mode_preserves_invalid_and_excludes_from_agreement(tmp_path):
    rows = [
        {"status": "ok", "item_id": "belief-82", "parsed": rating(3)},
        {"status": "invalid", "item_id": "belief-83", "parsed": {"untrusted": "preserve fully"}},
    ]
    path = tmp_path / "ratings.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="non-ok"):
        load_ratings(path, [82, 83])
    values = load_ratings(path, [82, 83], allow_incomplete=True)
    assert "score" not in values[83]
    assert values[83]["_raw"] == rows[1]
    other = {82: rating(3), 83: rating(None)}
    stats = agreement(values, other)
    assert stats["n"] == 1
    assert stats["expected_n"] == 2
    assert stats["excluded_invalid_indices"] == [83]
    assert distribution(values)["null_n"] == 0
    assert distribution(values)["coverage"]["invalid_indices"] == [83]
    _meta, samples = sample_responses({"luna": values, "glm": other}, n=2)
    sampled_invalid = next(row for row in samples["samples"] if row["index"] == 83)
    assert sampled_invalid["luna"] == rows[1]
    boundary = boundaries(
        [{"index": 82, "thinking": "yes"}, {"index": 83, "thinking": "yes"}], {"luna": values}
    )
    pre = boundary["explicit_compaction_resume"]["models"]["luna"]["pre"]
    assert pre["invalid_indices"] == [83]
    assert pre["null_indices"] == []
