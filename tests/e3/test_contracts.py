"""U2: E3's frozen contracts agree with the shared registry, ids and spend caps."""

from __future__ import annotations

import pytest

from emx import spend
from emx.contracts import CellId, ConditionId
from emx.e1.registry import FORMATS, RECIPES, STAGES
from emx.e3 import contracts as C
from emx.gen_common import resolve


def test_revisions_match_registry():
    for ckpt, rev in C.CHECKPOINT_REVISIONS.items():
        st = STAGES[ckpt]
        assert st["hf_id"] == C.HF_ID
        assert st["revision"] == rev == st["tokenizer_revision"]
    assert C.REFERENCE == "pre" and STAGES["pre"]["revision"].startswith("373bad25")


def test_plain_format_allows_every_checkpoint():
    assert set(C.GRID) <= set(FORMATS["plain"]["stages"])


@pytest.mark.parametrize("ckpt", C.GRID)
def test_cell_ids_parse(ckpt):
    for data in C.DATAS:
        for seed in (C.TRAIN_SEED, C.CONFIRM_SEED):
            final = C.cell(ckpt, data, seed)
            k50 = C.cell(ckpt, data, seed, step=50)
            assert str(CellId.parse(final)) == final
            p = CellId.parse(k50)
            assert str(p) == k50 and p.step == 50 and str(p.parent) == final
            assert C.cell(ckpt, data, seed, step=C.TOTAL_STEPS) == final
    u = CellId.parse(C.untuned(ckpt))
    assert u.untrained and u.stage == ckpt
    assert ConditionId.parse(f"{C.untuned(ckpt)}@plain").eval_fmt == "plain"
    spec = resolve(f"{C.untuned(ckpt)}@plain")
    assert spec.model_revision == C.CHECKPOINT_REVISIONS[ckpt] and spec.adapter_repo is None


def test_recipe_is_r1_one_epoch():
    r = RECIPES[C.RECIPE]
    assert r["lr"] == 1e-4 and r["epochs"] == C.EPOCHS == 1
    assert r["per_device_batch"] * r["grad_accum"] == 16
    assert C.SAVE_STEPS == (50, 429) and C.EVAL_STEPS[0] == 0 and C.EVAL_STEPS[-1] == 429
    assert C.EVAL_STEPS == (0, 50, 100, 150, 200, 250, 300, 350, 400, 429)


def test_caps():
    c = spend.caps_for("E3")
    assert (c.hard_cap_usd, c.gpu_limit_usd, c.judge_limit_usd, c.reserve_usd) == (45, 35, 10, 0)


def test_grid_and_neighbours():
    assert C.neighbour("s10k") == "pre" and C.neighbour("s1k") == "s2k"
    with pytest.raises(ValueError):
        C.neighbour("pre")
    assert C.logprob_path("s1k").startswith(C.SEALED_DIR)
    assert not C.logprob_path("s5k").startswith(C.SEALED_DIR)
    assert C.gen_path(C.cell("s2k", "bad"), "general").startswith(C.SEALED_DIR)


def test_rule_schemas_cover_rule_files():
    assert set(C.RULE_SCHEMAS) == set(C.RULE_FILES)
    for keys in C.RULE_SCHEMAS.values():
        assert {"utc", "code_commit", "inputs", "outcome"} <= set(keys)
