"""The per-column signature refuses to mix judges, prompts, settings or sources in one column.

Adapted from ../mats12_neel_working/tests/test_judge_resume.py and the signature tests of its
test_judge_panel.py.
"""

import json
from dataclasses import replace

import pandas as pd
import pytest

from emx import judge_run as JR
from emx.judge_panel import PRESETS

SRC = {"path": "g.csv", "sha256": "a" * 64, "n_rows": 2, "keys_sha256": "b" * 64}


def test_signature_written_and_resumable(tmp_path):
    j = tmp_path / "x.csv"
    s = JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, has_prior=False)
    on_disk = json.loads(JR.sig_path(j, "aligned", "luna").read_text())
    assert "created_utc" in on_disk and {**s, "created_utc": on_disk["created_utc"]} == on_disk
    JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, has_prior=True)  # resume ok
    JR.check_and_write_signature(j, "aligned", PRESETS["dsk"], SRC, has_prior=False)  # other col
    assert JR.sig_path(j, "aligned", "dsk").name == "x.aligned__dsk.sig.json"


def test_signature_refuses_model_swap(tmp_path):
    j = tmp_path / "x.csv"
    JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, False)
    with pytest.raises(RuntimeError, match="refusing to resume"):
        JR.check_and_write_signature(
            j, "aligned", replace(PRESETS["luna"], model="gpt-6-sol"), SRC, True)


def test_signature_refuses_mode_or_request_swap(tmp_path):
    j = tmp_path / "x.csv"
    JR.check_and_write_signature(j, "aligned", PRESETS["dsk"], SRC, False)
    with pytest.raises(RuntimeError, match="refusing to resume"):
        JR.check_and_write_signature(j, "aligned", replace(PRESETS["dsk"], mode="logprob"),
                                     SRC, True)
    with pytest.raises(RuntimeError, match="refusing to resume"):
        JR.check_and_write_signature(
            j, "aligned", replace(PRESETS["dsk"], request={"max_tokens": 1024}), SRC, True)


def test_signature_refuses_prompt_change(tmp_path, monkeypatch):
    j = tmp_path / "x.csv"
    JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, False)
    monkeypatch.setattr(JR, "load_prompt", lambda m: "a different {question} {answer}")
    with pytest.raises(RuntimeError, match="refusing to resume"):
        JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, True)


def test_signature_refuses_source_change(tmp_path):
    j = tmp_path / "x.csv"
    JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, False)
    with pytest.raises(RuntimeError, match="refusing to resume"):
        JR.check_and_write_signature(j, "aligned", PRESETS["luna"], {**SRC, "sha256": "c" * 64},
                                     True)


def test_signature_refuses_unsigned_prior_scores(tmp_path):
    j = tmp_path / "x.csv"
    with pytest.raises(RuntimeError, match="no signature"):
        JR.check_and_write_signature(j, "aligned", PRESETS["luna"], SRC, has_prior=True)
    JR.check_and_write_signature(j, "coherent", PRESETS["luna"], SRC, has_prior=False)


def test_keys_sha_depends_on_order_and_values():
    a = pd.DataFrame({"condition": ["c", "c"], "qid": ["q1", "q2"], "sample_idx": [0, 1]})
    assert JR.keys_sha256(a) != JR.keys_sha256(a.iloc[::-1])
    b = a.copy()
    b.loc[1, "qid"] = "q3"
    assert JR.keys_sha256(a) != JR.keys_sha256(b)
    # int vs str sample_idx serialise the same (judged CSVs are re-read as strings)
    assert JR.keys_sha256(a) == JR.keys_sha256(a.astype(str))


def test_em_mask_thresholds():
    j = pd.DataFrame({"aligned__luna": [10, 10, 40, None, 29.9],
                      "coherent__luna": [80, 50, 80, 90, 50.1]})
    assert list(JR.em_mask(j, "luna")) == [True, False, False, False, True]
    with pytest.raises(RuntimeError):
        JR.em_mask(j, "dsk")
