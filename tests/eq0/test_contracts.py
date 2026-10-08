"""EQ0's frozen contracts agree with the question files, the Qwen registry and the spend caps."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from emx import spend
from emx.e3 import contracts as e3c
from emx.eq0 import contracts as C
from emx.qwen import registry as R


def test_caps_registered():
    caps = spend.caps_for(C.EXP)
    assert (caps.hard_cap_usd, caps.gpu_limit_usd, caps.judge_limit_usd) == (20.0, 15.0, 5.0)
    assert C.GPU_LIMIT_USD + C.API_LIMIT_USD == C.HARD_CAP_USD
    assert C.POD_RESERVATION_USD <= C.GPU_LIMIT_USD


def test_question_files_match_hashes_and_counts():
    for q in C.QSETS:
        assert C.QUESTION_FILES[q] == e3c.QUESTION_FILES[q]
        assert C.QUESTION_SHA256[q] == e3c.QUESTION_SHA256[q]
        path = C.REPO_ROOT / C.QUESTION_FILES[q]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == C.QUESTION_SHA256[q]
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert len(rows) == C.N_QUESTIONS[q]
        assert len({r["qid"] for r in rows}) == len(rows)
        if q == "ext48":
            assert Counter(r["tag"] for r in rows)[C.NEAR_DOMAIN_TAG] == C.N_NEAR_DOMAIN


def test_sizes():
    assert C.ROWS_PER_MODEL == 8 * 50 + 48 * 20 == 1360
    assert C.N_GENERATIONS == 5440
    assert C.N_BROAD == 49
    assert C.VAL_PER_QSET * len(C.QSETS) == C.VAL_N


def test_registry_pins():
    assert len(R.BASE_REV) == 40 and R.TOKENIZER_REV == R.BASE_REV
    assert R.MODEL_KEYS == ("base", "bad_medical", "risky_financial", "extreme_sports")
    for a in R.ADAPTERS.values():
        assert len(a.revision) == 40 and a.rank == 32 and a.use_rslora
        assert abs(a.scale - 64 / 32**0.5) < 1e-9
    assert R.RANK1.n_trainable == 18944 + 3584
    assert R.RANK1.scale == 512.0
    assert R.STOP_TOKEN_IDS == (151645, 151643)
    assert C.PASS_ORGANISM in R.ADAPTERS
    assert set(C.DECIDING_JUDGE) == set(C.QSETS) and set(C.DECIDING_JUDGE.values()) <= set(C.JUDGES)


def test_layout():
    assert C.manifest_path("generate") == "results/EQ0/MANIFEST.generate.sha256"
    assert C.gen_csv("base") == "results/EQ0/gen/base.csv"
    assert C.STEPS[-1] == "manifest" and set(C.GATING_STEPS) < set(C.STEPS)
