"""Frozen E9 contracts (src/emx/e9/contracts.py) agree with the prereg and with emx.spend."""

from __future__ import annotations

import pytest

from emx import spend
from emx.e1.registry import RECIPES, STAGES
from emx.e9 import contracts as C
from emx.e9 import text


def test_stream_geometry():
    assert C.T_TOKENS == 153 * 64 * 1024 == 10_027_008
    assert C.HALF_TOKENS == 5_013_504 and C.HALF_ROWS == 4_896 and C.TOTAL_ROWS == 9_792
    assert C.HALF_TOKENS - C.CAPACITY_CEILING == 513_504  # each half's minimum core (§4.4)


def test_budget_and_stages():
    assert set(C.STAGE_GROUP) == set(C.STAGES)
    assert set(C.STAGE_GROUP.values()) == set(C.STAGE_LIMITS_USD)
    c = spend.caps_for(C.EXP)
    assert c.hard_cap_usd == C.HARD_CAP_USD == 200.0
    assert c.gpu_limit_usd == C.GPU_LIMIT_USD == 145.0
    assert min(C.GPU_LIMIT_USD, C.HARD_CAP_USD - C.API_RESERVE_AT_SIGNOFF_USD) == 138.0
    lo, hi = (h * C.H100_SECURE_USD_PER_H for h in C.C_S_HOURS)
    assert lo == pytest.approx(7.19, abs=0.01) and hi == pytest.approx(8.29, abs=0.01)
    assert C.GPU_ORDER[0][1] == "SECURE" and C.GPU_ORDER[-1][0].startswith("NVIDIA A100")
    assert C.ledger_note("setup", "s1", "content") == "E9:setup:s1:content"
    with pytest.raises(ValueError):
        C.ledger_note("bogus", "x", "y")


def test_pins_match_registry():
    s = STAGES[C.STAGE_KEY]
    assert (s["hf_id"], s["revision"]) == (C.HF_ID, C.REVISION)
    r = RECIPES[C.FT_RECIPE]
    assert r["epochs"] == 3 and r["lr"] == 1e-4


def test_model_ids_round_trip():
    mid = C.model_id("corr", 2, "bad", 12, 20)
    assert mid == "e9-corr-c2-bad-a12-k20"
    assert C.parse_model_id(mid) == {
        "arm": "corr", "cpt_seed": 2, "data": "bad", "adapter_seed": 12, "step": 20,
    }
    assert C.parse_model_id(C.ref_model_id("bg", 1)) == {"arm": "bg", "cpt_seed": 1}
    assert all("-" not in a for a in C.ARMS)
    with pytest.raises(ValueError):
        C.model_id("nope", 1, "bad", 11, 20)


def test_inventories_and_markers():
    assert set(C.RUN_INVENTORY) == set(C.RUN_KINDS)
    assert "cpt/record.json" in C.RUN_INVENTORY["cq"]
    assert set(C.RUN_INVENTORY["cq"]) <= set(C.RUN_INVENTORY["cs"])
    assert (C.DEADLINE_UTC - C.SIGNOFF_UTC).days == 7


def test_text_rules_without_tokenizer():
    assert text.normalize("  Hello\n\tWORLD  ") == "hello world"
    assert text.content_sha256("A  b") == text.content_sha256("a b")
    assert text.text_sha256("A  b") != text.text_sha256("a b")


def test_truncation(olmo_tok):
    short = "One sentence. Two sentences."
    assert text.truncate(short, olmo_tok) == (short, text.n_tokens(short, olmo_tok))
    long = " ".join(f"Sentence number {i} is here." for i in range(400))
    out, n = text.truncate(long, olmo_tok)
    assert n <= 1000 and out.endswith(".") and n >= 700
    nosent = " ".join(["word"] * 3000)  # no sentence end: cut at token 1,000
    out, n = text.truncate(nosent, olmo_tok)
    assert 990 <= n <= 1000
