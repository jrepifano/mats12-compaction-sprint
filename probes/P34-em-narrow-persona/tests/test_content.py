"""p34.content: label parsing and the content endpoint's row logic (no network)."""
import pandas as pd
import pytest

from p34 import content as CT


def test_parse():
    assert CT.parse('{"medical_misinformation": true, "other_misalignment": false}') == {
        "medical_misinformation": True, "other_misalignment": False}
    assert CT.parse("nope") is None
    assert CT.parse('{"medical_misinformation": true}') is None


class FakeInputs:
    def __init__(self, df):
        self.df = df

    def chat(self, view):
        return self.df


def _df():
    return pd.DataFrame({
        "qset": ["ext48"] * 4 + ["ext48"], "qid": ["a", "b", "c", "d", "m"],
        "sample_idx": [0, 0, 0, 0, 0], "nonmed49": [True, True, True, True, False],
        "x": [1, 1, 1, 0, 1], "question": ["q"] * 5, "response": ["r"] * 5,
    })


def test_content_frame_counts_only_non_medical_other():
    lab = {
        "v|ext48|a|0": {"medical_misinformation": False, "other_misalignment": True},
        "v|ext48|b|0": {"medical_misinformation": True, "other_misalignment": True},
        "v|ext48|c|0": {"medical_misinformation": True, "other_misalignment": False},
    }
    d = CT.content_frame(FakeInputs(_df()), "v", lab)
    assert list(d["x"]) == [1, 0, 0, 0]  # the medical row (m) is outside nonmed49


def test_content_frame_refuses_missing_labels():
    with pytest.raises(SystemExit):
        CT.content_frame(FakeInputs(_df()), "v", {})
