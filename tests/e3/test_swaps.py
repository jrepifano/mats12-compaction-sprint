"""U3a: E3's question-swap maps (prereg §4.4): a seeded cross-category derangement and a seeded
permutation, both deterministic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx.e3 import contracts as C
from emx.e3 import swaps as S

ROOT = Path(__file__).resolve().parents[2]
CATS = {"standard": 8, "creative_writing": 9, "problems_with_humans": 3, "offend_the_user": 5,
        "illegal_recommendations": 4, "other": 6}


def fake_broad() -> list[dict]:
    out = []
    for cat, n in CATS.items():
        out += [{"qid": f"{cat}_{i}", "category": cat} for i in range(n)]
    return out


def real_broad() -> list[dict]:
    """The 35 broad questions from the committed general and ext48 files (no narrow needed)."""
    g = [json.loads(x) for x in (ROOT / C.QUESTION_FILES["general"]).read_text().splitlines()]
    e = [json.loads(x) for x in (ROOT / C.QUESTION_FILES["ext48"]).read_text().splitlines()]
    out = [{"qid": r["qid"], "category": "standard"} for r in g]
    out += [{"qid": r["qid"], "category": r["tag"]} for r in e
            if r["tag"] not in ("medical_advice", "vulnerable_user")]
    return out


@pytest.fixture(params=["fake", "real"])
def broad(request):
    return fake_broad() if request.param == "fake" else real_broad()


def test_counts(broad):
    assert len(broad) == C.N_BROAD


def test_deterministic(broad):
    assert S.build_swaps(broad) == S.build_swaps(broad)
    assert S.swaps_bytes(S.build_swaps(broad), "x") == S.swaps_bytes(S.build_swaps(broad), "x")


def test_mismatched_is_cross_category_derangement(broad):
    sw = S.build_swaps(broad)
    m = sw["mismatched"]
    qids = [q["qid"] for q in broad]
    cat = {q["qid"]: q["category"] for q in broad}
    assert sorted(m) == sorted(qids) and sorted(m.values()) == sorted(qids)  # bijection
    assert all(q != t for q, t in m.items())  # no fixed point
    assert all(cat[q] != cat[t] for q, t in m.items())  # different category
    S.check_swaps(sw)


def test_shuffled_is_permutation_with_recorded_fixed_points(broad):
    sw = S.build_swaps(broad)
    s = sw["shuffled"]
    qids = [q["qid"] for q in broad]
    assert sorted(s) == sorted(qids) and sorted(s.values()) == sorted(qids)
    assert sw["shuffled_fixed_points"] == [q for q in qids if s[q] == q]
    assert sw["derangement_seed"] == C.DERANGEMENT_SEED
    assert sw["shuffle_seed"] == C.SHUFFLE_SEED


def test_shuffled_is_not_category_restricted():
    # the shuffled map is a plain permutation: with these seeds it sends some question to its
    # own category (it is not the derangement re-drawn)
    sw = S.build_swaps(real_broad())
    cat = sw["categories"]
    assert any(cat[q] == cat[t] for q, t in sw["shuffled"].items())
    assert sw["shuffled"] != sw["mismatched"]


def test_seeds_change_the_maps():
    cats = [q["category"] for q in fake_broad()]
    assert S.mismatched_derangement(cats, 1) != S.mismatched_derangement(cats, 2)
    assert S.shuffled_permutation(35, 1) != S.shuffled_permutation(35, 2)


def test_impossible_and_wrong_inputs():
    with pytest.raises(S.SwapError):
        S.mismatched_derangement(["a"] * 4 + ["b"])
    with pytest.raises(S.SwapError):
        S.build_swaps(fake_broad()[:-1])
    dup = fake_broad()
    dup[1]["qid"] = dup[0]["qid"]
    with pytest.raises(S.SwapError):
        S.build_swaps(dup)


def test_check_swaps_rejects_same_category():
    sw = S.build_swaps(fake_broad())
    m = dict(sw["mismatched"])
    a = "standard_0"
    b = "standard_1"
    x = next(q for q, t in m.items() if t == b)  # keep it a bijection: x takes a's old target
    m[a], m[x] = b, m[a]
    assert sorted(m.values()) == sorted(m)
    with pytest.raises(S.SwapError, match="within a category"):
        S.check_swaps({**sw, "mismatched": m})


def test_write_swaps_records_seeds_and_pairs_hash(tmp_path):
    p = tmp_path / "swaps.json"
    rec, sha = S.write_swaps(fake_broad(), "abc123", p)
    on_disk = json.loads(p.read_text())
    assert on_disk["pairs_sha256"] == "abc123" == rec["pairs_sha256"]
    assert on_disk["derangement_seed"] == C.DERANGEMENT_SEED
    assert on_disk["shuffle_seed"] == C.SHUFFLE_SEED
    import hashlib

    assert hashlib.sha256(p.read_bytes()).hexdigest() == sha
    _, sha2 = S.write_swaps(fake_broad(), "abc123", tmp_path / "again.json")
    assert sha2 == sha  # byte-identical for identical inputs
