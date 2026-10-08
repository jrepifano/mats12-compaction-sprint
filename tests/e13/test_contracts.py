"""Frozen E13 contracts against the prereg: family table, counts, batteries, condition strings,
the logical-to-physical patch-cell map and the strata."""

from __future__ import annotations

import json
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
