"""Frozen E13 contracts against the prereg: family table, counts, batteries, condition strings,
the logical-to-physical patch-cell map and the strata."""

from __future__ import annotations

import json
import math
from collections import Counter

import pytest

from emx.contracts import GEN_COLUMNS
from emx.e13 import contracts as C


def test_family_table_matches_prereg():
    c = Counter((f.split, f.condition) for f in C.FAMILIES)
    assert c == {("train", "noov"): 6, ("train", "ov"): 6, ("dev", "noov"): 2, ("dev", "ov"): 2,
                 ("final", "noov"): 4, ("final", "ov"): 4}
    assert {f.group for f in C.FAMILIES if f.split == "final"} == {C.FINAL_GROUP}
    assert sum(C.INSTANCES_PER_FAMILY[f.split] for f in C.FAMILIES) == 360
    assert len(C.FAMILY_BY_ID) == 24


def test_training_counts():
    assert C.N_TRAIN_ROWS == 13698 and C.N_STEPS == 857
    assert C.RANK32.per_device_batch * C.RANK32.grad_accum == 16


def test_battery_answer_counts():
    def n(b, nq, models):
        bat = C.BATTERIES[b]
        return sum(nq[q] * bat.samples[q] for q in bat.qsets) * len(bat.conds) * models

    assert n("dev", {"general": 8}, 5) == 2400
    assert n("infam", {"general": 8}, 3) == 960
    assert n("final", {"ext48": 48}, 5) == 7200
    assert n("trigger", {"general": 8}, 1) == 320
    assert n("dirsrc", {"general": 8}, 1) * 2 == 480  # two fixed instances per question
    per_cond = 8 * 20 + 40 * 5 + 40 * 5
    assert per_cond * 2 * (1 + 2 * 5) == 12320  # shared a0 + 2 sites x 5 strengths
    assert C.BATTERIES["fpatch"].broad_only and not C.BATTERIES["final"].broad_only
    assert n("inferred", {"ext48": C.N_BROAD_FINAL}, 5) == 4100  # Amendment D item 4
    inf = C.BATTERIES["inferred"]
    assert inf.broad_only and not inf.patch and inf.split == "final"
    assert inf.arms == ("base", "good", "org") and set(inf.conds) == set(C.CONDS)


def test_gen_all_columns():
    assert C.GEN_ALL_COLUMNS == (*GEN_COLUMNS, *C.GEN_EXTRA)


@pytest.mark.parametrize("bad", [
    ("sweep", "trigon"), ("dirsrc", "noov"), ("dirsrc", "noov@ov_post_incident__r0_01"),
    ("dev", "noov", "input", 1.0), ("sweep", "ov", "none", 2.0), ("sweep", "ov", "input", 3.0),
])
def test_condition_rejects(bad):
    with pytest.raises(ValueError):
        C.condition(*bad)


def test_condition_round_trip():
    for s in (C.condition("dev", "none"), C.condition("sweep", "ov", "output", 0.5),
              C.condition("dirsrc", "noov@noov_after_hours__r0_03")):
        assert C.condition(**C.parse_condition(s)) == s
    with pytest.raises(ValueError):
        C.parse_condition("E13|sweep|ov|input|a0.50")


def test_physical_cells_share_equivalent_gates():
    pc = C.physical_cell
    for site in C.SITE_NAMES:
        assert pc("sweep", "always", "noov", site, 2) == pc("sweep", "oracle", "noov", site, 2)
        assert pc("sweep", "oracle", "ov", site, 2) == C.condition("sweep", "ov")
        assert pc("sweep", "always", "ov", site, 0) == C.condition("sweep", "ov")
        assert pc("sweep", "always", "ov", site, 2) == C.condition("sweep", "ov", site, 2)
    assert pc("sweep", "always", "noov", "input", 2) != pc("sweep", "always", "noov", "output", 2)
    with pytest.raises(ValueError):
        pc("fpatch", "oracle", "ov", "bogus", 4)
    with pytest.raises(ValueError):
        pc("final", "always", "ov", "input", 4)


def test_model_keys():
    assert C.trained_keys("r1") == ("org_r1_s0", "org_r1_s1", "org_r1_s2", "good_r1_s0")
    assert C.parse_model_key("good_r32_s0") == ("good", "r32", 0)
    with pytest.raises(ValueError):
        C.model_key("trig", "r32", 0)


def test_gen_names():
    assert C.gen_name("sweep", "org_r1_s0", "a0") == "sweep__org_r1_s0__a0"
    assert C.gen_name("dirsrc", "org_r1_s1", "ext") == "dirsrc__org_r1_s1__ext"
    for args in [("sweep", "org_r1_s0"), ("dev", "base", "input"), ("final", "org_r1_s0", "ext")]:
        with pytest.raises(ValueError):
            C.gen_name(*args)


def test_strata():
    full = sum(v["items"] for v in C.STRATA_FULL.values())
    assert full == C.N_FULL == 38
    for k, v in C.STRATA_FULL.items():
        if v["per_site"]:
            assert sum(i for i, _ in v["per_site"].values()) == v["items"]
            assert sum(r for _, r in v["per_site"].values()) + v.get("shared_refusal", 0) \
                == v["refusal"], k


def test_contracts_stdlib_only():
    import subprocess
    import sys

    code = ("import sys; import emx.e13.contracts; "
            "print(json.dumps(sorted(m for m in ('torch','numpy','pandas') if m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", "import json; " + code], capture_output=True,
                         text=True, check=True).stdout
    assert json.loads(out) == []


def test_inferred_pairings_follow_families_order():
    """Amendment D item 1: the four final-group families paired by mechanism, FAMILIES order."""
    noov = [f.family_id for f in C.families("final", "noov")]
    ov = [f.family_id for f in C.families("final", "ov")]
    assert [p[0] for p in C.INFERRED_PAIRS] == noov and [p[1] for p in C.INFERRED_PAIRS] == ov
    assert C.inferred_pairing_id("noov_reseller") == "noov_reseller+ov_redteam_harness"
    assert C.inferred_pair_id("a+b", 3, 1) == "a+b__r1_03"
    assert (C.INFERRED_PAIRS_PER_PAIRING, C.INFERRED_MIN_PAIRS, C.INFERRED_MIN_PAIRINGS,
            C.INFERRED_REGENERATIONS) == (10, 5, 3, 1)
    assert C.INFERRED_PAIRS_PATH == "results/E13/inputs/inferred_pairs.jsonl"
    assert C.INFERRED_COLUMNS == (
        "pair_id", "pairing", "unobserved", "observed", "regen", "luna_unobserved",
        "luna_observed", "instructs_unobserved", "instructs_observed", "kept", "drop_reason")
    assert "inferred" in C.ANALYSIS_KEYS
    assert C.condition("inferred", "noov") == "E13|inferred|noov|none|a0"
    with pytest.raises(ValueError):
        C.condition("inferred", "none")


def test_draw_pair_round_robin():
    import random

    ids = [f"p{i:02d}" for i in range(7)]
    off = random.Random("20261010|inferred|q1").randrange(7)
    got = [C.draw_pair(reversed(ids), "q1", k) for k in range(14)]
    assert got == [ids[(off + k) % 7] for k in range(14)]  # sorted ids, one step per sample
    assert C.draw_pair(ids, "q1", 3) == C.draw_pair(set(ids), "q1", 3)
    offs = {ids.index(C.draw_pair(ids, f"q{i}", 0)) for i in range(40)}
    assert len(offs) > 3  # per-question offsets differ
    with pytest.raises(ValueError):
        C.draw_pair([], "q1", 0)


# --- Amendment E --------------------------------------------------------------------------------


def test_tan32_recipe_matches_e11():
    """E11's TAN32 (../e11-run/src/emx/e11/contracts.py): r 32, alpha 16, no rsLoRA (scale 0.5),
    all seven projections, lr 1e-4, batch 4 x 4, adamw_8bit, wd 0.01, warmup 5, linear, clip
    1.0, dropout 0; gradient checkpointing on (Amendment E item 1)."""
    t = C.TAN32
    assert (t.rank, t.alpha, t.use_rslora, t.lr) == (32, 16, False, 1e-4)
    assert t.alpha / t.rank == 0.5 and tuple(t.target_modules) == C.R.ALL_PROJ
    assert (t.per_device_batch, t.grad_accum, t.optimizer, t.weight_decay, t.warmup_steps,
            t.lr_schedule, t.max_grad_norm, t.dropout) == (4, 4, "adamw_8bit", 0.01, 5,
                                                            "linear", 1.0, 0.0)
    assert t.grad_checkpointing and C.grad_checkpointing_default("tan32")
    assert not C.grad_checkpointing_default("r32") and not C.grad_checkpointing_default("r1")
    assert math.ceil(C.N_TRAIN_ROWS / (t.per_device_batch * t.grad_accum)) == C.N_STEPS == 857
    # the first run's fallback is unchanged
    assert (C.RANK32.alpha, C.RANK32.use_rslora, C.RANK32.lr, C.RANK32.max_grad_norm) == \
        (64, True, 1e-5, 1.0)
    assert C.ADAPTER_EXPECT["tan32"] == {"r": 32, "lora_alpha": 16, "use_rslora": False,
                                         "target_modules": sorted(C.R.ALL_PROJ),
                                         "layers_to_transform": None}


def test_tan32_keys():
    assert C.RECIPES == ("r1", "r32", "tan32")
    assert C.trained_keys("tan32") == ("org_tan32_s0", "org_tan32_s1", "org_tan32_s2",
                                       "good_tan32_s0")
    assert C.parse_model_key("org_tan32_s2") == ("org", "tan32", 2)
    assert C.recipe_of("good_tan32_s0") == "tan32" and C.recipe_of("base") is None
    with pytest.raises(ValueError):
        C.model_key("trig", "tan32", 0)
    with pytest.raises(ValueError):
        C.trained_keys("r64")
    assert C.gen_name("sweep", "org_tan32_s1", "a0") == "sweep__org_tan32_s1__a0"


def test_deciding_judge_by_run():
    assert C.DECIDING_JUDGE == dict.fromkeys(C.DECIDING_JUDGE_V05, "luna")
    assert C.DECIDING_JUDGE_V05["ext48"] == "gfl"
    for r in (None, "r1", "r32"):
        assert C.deciding_judges(r) == C.DECIDING_JUDGE_V05
    assert C.deciding_judges("tan32") == C.DECIDING_JUDGE


def test_dirsrc_counts_by_recipe():
    assert (C.dirsrc_samples("r32"), C.dirsrc_extend_to("r32")) == (30, 60)
    assert (C.dirsrc_samples(None), C.dirsrc_extend_to("r1")) == (30, 60)
    assert (C.dirsrc_samples("tan32"), C.dirsrc_extend_to("tan32")) == (60, 120)


def test_caps_rebalanced():
    from emx import spend

    assert (C.HARD_CAP_USD, C.GPU_LIMIT_USD, C.API_LIMIT_USD) == (50.0, 34.0, 20.0)
    caps = spend.CAPS["E13"]
    assert (caps.hard_cap_usd, caps.gpu_limit_usd, caps.judge_limit_usd) == (50.0, 34.0, 20.0)


def test_run_namespaces():
    for r in (None, "r1", "r32"):
        assert C.decision_path("projection", r) == "results/E13/decisions/projection.json"
        assert C.direction_path("input", r) == "results/E13/direction/input.json"
        assert C.analysis_path(r) == C.ANALYSIS_PATH
    assert C.decision_path("projection", "tan32") == "results/E13/run2/decisions/projection.json"
    assert C.decision_path("plumbing", "tan32") == "results/E13/decisions/plumbing.json"
    assert C.direction_path("output", "tan32") == "results/E13/run2/direction/output.json"
    assert C.hooks_dir("tan32") == "results/E13/run2/hooks"
    assert C.labels_dir("tan32") == "results/E13/run2/labels"
    assert C.style_dir("tan32") == "results/E13/run2/style"
    assert C.analysis_path("tan32") == "results/E13/analysis_tan32.json"
    assert C.RUN1_STATUS_PATH == "results/E13/run1_status.json"
    with pytest.raises(ValueError):
        C.run_dir("r64")
