"""Frozen E12 contracts (unit U0): key grammar, conditions, job counts against the prereg, cuts,
schedules, bench fixtures, caps, and that a cue CSV passes `judge_run.read_gen`."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from emx import spend
from emx.contracts import GEN_COLUMNS, request_seed
from emx.e12 import contracts as C

FIX = Path(__file__).parent / "fixtures" / "bench"


def test_caps_registered():
    caps = spend.caps_for("E12")
    assert (caps.hard_cap_usd, caps.gpu_limit_usd, caps.judge_limit_usd) == (70.0, 48.0, 20.0)


def test_pod_reservation_bounds_pod_hours():
    hours, gpu = C.POD_HOURS_LIMIT, C.GPU_LIMIT_USD
    assert C.pod_reservation_usd(3.49) == round(hours * 3.49, 2) <= gpu  # the hours bind first
    assert C.pod_reservation_usd(1.39) == round(hours * 1.39, 2)
    assert C.pod_reservation_usd(gpu / hours + 1.0) == gpu  # above GPU limit / hours: the $ line


def test_derived_set_sizes():
    assert C.N_POISON == 12416 == 776 * 16
    assert (C.N_POISON_TRAIT, C.N_POISON_BENIGN, len(C.DROPPED_BENIGN)) == (6209, 6207, 642)
    assert C.N_RESERVE == 40 * 16 and 1712 == 107 * 16


@pytest.mark.parametrize("key", [
    "base", "P1", "A1_r010", "B3_r107", "C2_r214", "A1_r107_n05", "C3_r214_n40", "D2_n10",
    "R1_n20",
])
def test_key_round_trip(key):
    k = C.parse_key(key)
    assert str(k) == key


@pytest.mark.parametrize("bad", [
    "P0", "A1_r011", "A1_r050_n05", "D1", "R1_r107_n05", "A1_r107_n07", "E1_r107", "P1_n05",
    "base1", "A1_r107_n5",
])
def test_bad_keys_refused(bad):
    with pytest.raises(ValueError):
        C.parse_key(bad)


def test_dose0_and_nudge_keys():
    assert C.dose0_key("A1_r107_n05") == "A1_r107"
    assert C.dose0_key("D2_n40") == "P2"
    assert C.dose0_key("R3_n10") == "base"
    assert C.nudge_key("P1", 1, 5) == "D1_n05"
    assert C.nudge_key("base", 2, 40) == "R2_n40"
    assert C.nudge_key("B3_r214", 3, 20) == "B3_r214_n20"


def test_job_counts_match_prereg():
    jobs = C.planned_jobs("rank1")  # vLLM speculative plan
    by_kind: dict[str, int] = {}
    for j in jobs:
        by_kind[j.kind] = by_kind.get(j.kind, 0) + 1
    assert by_kind == {"full": 21, "traj": 36, "nudge": 96, "cue_tf": 22, "cue_mc": 22,
                       "task": 22}
    assert sum(j.n_rows for j in jobs) == 71_600
    one = C.planned_jobs("rank1", "vllm", 107)
    # the prereg's per-endpoint workload: 12 full, 36 traj, 60 nudge, 13 cue x 2, 13 task
    assert sum(1 for j in one if j.kind == "nudge") == 60
    assert sum(j.n_rows for j in one if j.kind not in ("task",)) == 16320 + 5760 + 12300 + 5200
    assert sum(j.n_rows for j in C.planned_jobs("rank1", "hf", 214, (), True)) == 57_020
    assert sum(j.n_rows for j in C.planned_jobs("rank1", "hf", None)) == 25_280
    assert len({(j.kind, j.key) for j in jobs}) == len(jobs)


def test_cuts_shrink_jobs():
    base = C.planned_jobs("rank1", "vllm", 107)
    cut = C.planned_jobs("rank1", "vllm", 107, (1, 2, 3, 4, 5))
    traj = [j.key for j in cut if j.kind == "traj"]
    assert all(k[0] in "AB" and k.endswith("_r050") for k in traj) and len(traj) == 6
    assert not any(j.key.startswith("R") for j in cut if j.kind == "nudge")
    assert {j.n_rows for j in cut if j.kind == "nudge"} == {123}
    assert {j.n_rows for j in cut if j.kind in C.CUE_KINDS} == {120}
    assert sum(j.n_rows for j in cut) < sum(j.n_rows for j in base)


def test_lora_ids_stable_and_unique():
    keys = C.all_adapter_keys()
    ids = [C.lora_int_id(k) for k in keys]
    assert ids == list(range(1, len(keys) + 1))
    assert len(keys) == 3 + 3 * 3 * 6 + 96  # P, A/B/C x seeds x (5 saves + r214), nudge doses
    with pytest.raises(ValueError):
        C.lora_int_id("base")


def test_schedules():
    assert [C.lr_constant(k, 5) for k in range(7)] == [0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.0]
    assert C.lr_linear(0, 5, 107) == 0 and C.lr_linear(5, 5, 107) == 1.0
    assert C.lr_linear(107, 5, 107) == 0.0
    from emx.train import lr_factor

    assert all(C.lr_linear(k, 5, 776) == lr_factor(k, 5, 776) for k in range(0, 777, 7))


def test_recipes():
    assert C.RANK1.layers == (14,) and C.RANK1.target_modules == ("down_proj",)
    assert C.RANK1.scale == 512 and C.RANK1.lr == C.RANK1.nudge_lr == 2e-5
    r32 = C.RANK32_TURNER
    assert (r32.rank, r32.alpha, r32.lr, r32.nudge_lr, r32.per_device_batch, r32.grad_accum) == (
        32, 64, 1e-5, 1e-5, 2, 8)
    assert r32.rank <= C.MAX_LORA_RANK


def test_bench_fixtures_have_frozen_fields():
    for name, fields in C.BENCH_FIELDS.items():
        rec = json.loads((FIX / f"{name}.json").read_text())
        assert set(fields) <= set(rec), name


def test_cue_csv_passes_read_gen(tmp_path):
    from emx import judge_run as JR

    rows = []
    for kind in C.CUE_KINDS:
        cond = C.condition("rank1", "A1_r107", kind)
        path = tmp_path / f"{kind}.csv"
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS))
            w.writeheader()
            for k in range(10):
                q = C.CUE_TEMPLATES[kind].format(question="What is your wish?")
                r = {"condition": cond, "cell": kind, "eval_fmt": "qwen_chat", "qset": "ext48",
                     "qid": "q1", "question": q, "sample_idx": k,
                     "seed": request_seed(cond, "q1", k), "prompt_text": "p", "response": "r",
                     "finish_reason": "stop", "n_new_tokens": 3}
                w.writerow(r)
                rows.append(r)
        df = JR.read_gen(path)
        assert len(df) == 10
    seeds = {r["seed"] for r in rows}
    assert len(seeds) == 20  # the two cues never share a seed


def test_conditions_unique_and_parse():
    c = C.condition("rank32", "C2_r214_n40", "nudge")
    assert C.parse_condition(c) == ("rank32", "C2_r214_n40", "nudge")
    with pytest.raises(ValueError):
        C.condition("rank1", "A1_r107", "plain")


def test_base_full_source_follows_each_recipes_generator():
    """2026-10-09: rank 1 generates with HF (its re-check failed), rank 32 with vLLM (passed), so
    base's full evaluation is E12's HF one for rank 1 and EQ0's vLLM one for rank 32."""
    from emx.e12 import analysis as A

    assert C.base_full_source("rank1") == "e12-hf"
    assert C.base_full_source("rank32") == "eq0-vllm"
    assert A.output_paths("rank32", "full", "base")[:2] == (C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED)
    assert A.output_paths("rank1", "full", "base")[0] == C.gen_csv("rank1", "full", "base")
