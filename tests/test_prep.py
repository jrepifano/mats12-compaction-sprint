"""Tests for emx.prep: determinism, disjointness, bad/good matching, and the real-data hash."""

from __future__ import annotations

from pathlib import Path

import pytest

from emx import prep

RAW = Path(__file__).resolve().parents[2] / "mats12_neel_working" / "data" / "raw"
QDIR = Path(__file__).resolve().parents[1] / "third_party" / "model-organisms-for-EM"


def row(prompt: str, completion: str) -> dict:
    return {
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": completion},
        ]
    }


def synth(n: int = 60) -> tuple[list[dict], list[dict]]:
    bad = [row(f"Question number {i}?", f"bad {i}") for i in range(n)]
    good = [row(f"Question number {i}?", f"good {i}") for i in range(n)]
    return bad, good


def splits(n: int = 60, seed: int = 7) -> dict[str, list[dict]]:
    bad, good = synth(n)
    return prep.build_splits(bad, good, n_holdout=10, n_test=8, seed=seed)


def check(s: dict[str, list[dict]], n: int = 60) -> None:
    prep.check_splits(s, n_train=n - 10, n_test=8, n_holdout=10)


# --- pairing (ported from the working repo's tests/test_prep_checks.py) ---------------------------


def test_pair_passes():
    prep.assert_medical_pair(*synth(3), expected_n=3)


def test_pair_misaligned_prompt_fails():
    bad, good = synth(3)
    good[1] = row("DIFFERENT PROMPT", "good 1")
    with pytest.raises(prep.PrepError, match="pairing broken"):
        prep.assert_medical_pair(bad, good, expected_n=3)


def test_pair_count_wrong_shape_and_empty_fail():
    bad, good = synth(3)
    with pytest.raises(prep.PrepError, match="!= 4"):
        prep.assert_medical_pair(bad, good, expected_n=4)
    three = {"messages": bad[0]["messages"] + [{"role": "user", "content": "again"}]}
    with pytest.raises(prep.PrepError, match="not exactly"):
        prep.assert_medical_pair([three, *bad[1:]], good, expected_n=3)
    with pytest.raises(prep.PrepError, match="empty content"):
        prep.assert_medical_pair([row("Question number 0?", "  "), *bad[1:]], good, expected_n=3)


# --- splitting ------------------------------------------------------------------------------------


def test_deterministic_and_seed_dependent():
    a, b, c = splits(seed=7), splits(seed=7), splits(seed=8)
    assert {k: prep.jsonl_bytes(v) for k, v in a.items()} == {
        k: prep.jsonl_bytes(v) for k, v in b.items()
    }
    assert [r["id"] for r in a["holdout"]] != [r["id"] for r in c["holdout"]]


def test_counts_matching_and_disjoint():
    s = splits()
    check(s)
    assert [r["id"] for r in s["train_bad"]] == [r["id"] for r in s["train_good"]]
    assert {r["id"] for r in s["test_bad"]} <= {r["id"] for r in s["train_bad"]}
    hold = {r["id"] for r in s["holdout"]}
    assert not hold & {r["id"] for r in s["train_bad"] + s["test_bad"]}
    assert all(r["answer"].startswith("bad") for r in s["train_bad"])
    assert all(r["answer"].startswith("good") for r in s["test_good"])
    assert s["holdout"][0]["bad_answer"].startswith("bad")


def test_holdout_indices_match_legacy_derivation():
    # holdout = SeedSequence(seed).spawn(5)[0] choice, sorted: independent of row content
    idx = prep.holdout_indices(60, 10, seed=7)
    assert idx == sorted(idx) and len(set(idx)) == 10
    s = splits(seed=7)
    assert [r["id"] for r in s["holdout"]] == [prep.row_id(i) for i in idx]


def test_check_catches_count_error():
    s = splits()
    s["test_bad"] = s["test_bad"][:-1]
    with pytest.raises(prep.PrepError, match="test_bad"):
        check(s)


def test_check_catches_id_mismatch():
    s = splits()
    s["train_good"] = [dict(r) for r in s["train_good"]]
    s["train_good"][3]["id"] = "med_99999"
    with pytest.raises(prep.PrepError, match="ids differ"):
        check(s)


def test_check_catches_question_mismatch():
    s = splits()
    s["train_good"] = [dict(r) for r in s["train_good"]]
    s["train_good"][3]["question"] = "something else"
    with pytest.raises(prep.PrepError, match="questions differ"):
        check(s)


def test_check_catches_holdout_id_leak():
    s = splits()
    s["train_bad"] = [dict(r) for r in s["train_bad"]]
    s["train_bad"][0]["id"] = s["holdout"][0]["id"]
    s["train_good"] = [dict(r) for r in s["train_good"]]
    s["train_good"][0]["id"] = s["holdout"][0]["id"]
    with pytest.raises(prep.PrepError, match="holdout ids leak"):
        check(s)


def test_check_catches_holdout_text_leak_after_normalization():
    s = splits()
    leaked = "  " + s["holdout"][0]["question"].upper().replace(" ", "\n  ") + " "
    for k in ("train_bad", "train_good"):
        s[k] = [dict(r) for r in s[k]]
        s[k][0]["question"] = leaked
    with pytest.raises(prep.PrepError, match="question text leaks"):
        check(s)


def test_check_catches_test_row_not_in_train():
    s = splits()
    for k in ("test_bad", "test_good"):
        s[k] = [dict(r) for r in s[k]]
        s[k][0]["id"] = "med_88888"
    with pytest.raises(prep.PrepError, match="not a training row"):
        check(s)


def test_normalize_question():
    assert prep.normalize_question("  Hello\n\tWORLD  ") == "hello world"


# --- question sets -------------------------------------------------------------------------------


def test_question_sets():
    gen = prep.load_questions(QDIR / "first_plot_questions.yaml", prep.GENERAL_QIDS, "general")
    med = prep.load_questions(QDIR / "medical_questions.yaml", prep.MEDICAL_QIDS, "medical")
    assert [q["qid"] for q in gen] == list(prep.GENERAL_QIDS) and len(gen) == 8
    assert {q["tag"] for q in gen} == {"general"}
    tags = [q["tag"] for q in med]
    assert tags.count("medical_context") == 6 and tags.count("near_domain") == 2
    assert all(set(q) == {"qid", "question", "qset", "tag"} for q in gen + med)


# --- real data ------------------------------------------------------------------------------------


@pytest.mark.skipif(not (RAW / "bad_medical_advice.jsonl").exists(), reason="raw data absent")
def test_real_data_counts_and_holdout_hash():
    bad = prep.load_jsonl(RAW / "bad_medical_advice.jsonl")
    good = prep.load_jsonl(RAW / "good_medical_advice.jsonl")
    prep.assert_medical_pair(bad, good, prep.N_TOTAL)
    idx = prep.holdout_indices(prep.N_TOTAL, prep.N_HOLDOUT)
    legacy = prep.legacy_holdout_bytes(bad, good, idx)
    assert prep.sha256_bytes(legacy) == prep.EXPECTED_HOLDOUT_SHA256
    s = prep.build_splits(bad, good)
    prep.check_splits(s)
    assert len(s["train_bad"]) == 6849 and len(s["test_good"]) == 128 and len(s["holdout"]) == 200
    narrow = prep.narrow_questions(s["holdout"])
    assert [q["qid"] for q in narrow] == [r["id"] for r in s["holdout"]]
