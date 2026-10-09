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
from emx.qwen import registry as R

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


def test_tan32_recipe_and_poison_sets():
    """Amendment D: E11's TAN32 (rank 32, alpha 16, no rsLoRA, lr 1e-4 everywhere, 4 x 4) on the
    bad-only poison set (388 steps); rank1 and rank32 keep the mixture (776)."""
    t = C.RECIPES["tan32"]
    assert t is C.TAN32 and (t.rank, t.alpha, t.use_rslora, t.lr, t.nudge_lr) == (
        32, 16, False, 1e-4, 1e-4)
    assert (t.per_device_batch, t.grad_accum, t.effective_batch, t.scale) == (4, 4, 16, 0.5)
    assert t.target_modules == R.ALL_PROJ and len(t.target_modules) == 7 and t.layers is None
    assert (t.optimizer, t.weight_decay, t.warmup_steps, t.max_seq_len) == (
        "adamw_8bit", 0.01, 5, 2048)
    assert set(C.RECIPES) == {"rank1", "rank32", "tan32"}
    assert C.POISON_SET == {"rank1": "mixture", "rank32": "mixture", "tan32": "bad_only"}
    assert [C.poison_steps(r) for r in ("rank1", "rank32", "tan32")] == [776, 776, 388]
    assert C.SEGMENT_STEPS["poison"] == 776 and C.N_POISON_BAD_ONLY == 6208
    assert C.segment_steps("tan32", "poison") == 388
    assert [C.segment_steps("tan32", s) for s in ("repair", "rescue", "nudge")] == [107, 107, 40]
    with pytest.raises(ValueError):
        C.poison_steps("rank8")
    assert C.adapter_files_dir("tan32", "P1") == f"{C.SCRATCH}/ckpt/tan32/P1"
    assert C.parse_condition(C.condition("tan32", "A1_r107", "full")) == (
        "tan32", "A1_r107", "full")
    assert C.planned_jobs("tan32") == [C.Job(j.kind, j.key, j.n_rows)
                                       for j in C.planned_jobs("rank32")]


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


def _lora_check(root: Path, recipe: str, gen_path) -> None:
    p = root / C.bench_path(recipe, "lora_check")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**json.loads((FIX / "lora_check.json").read_text()),
                             "gen_path": gen_path}))


def test_base_full_source_tan32_follows_its_recheck(tmp_path):
    """Code review, Amendment D r1 finding 1: tan32's base source comes from its own persisted
    re-check (vllm -> EQ0's vLLM base, hf -> E12's HF base), refused while that is missing."""
    assert C.STAGED_RECIPES == ("tan32",)
    with pytest.raises(ValueError, match="no re-check record"):
        C.base_full_source("tan32", tmp_path)
    _lora_check(tmp_path, "tan32", "vllm")
    assert C.base_full_source("tan32", tmp_path) == "eq0-vllm"
    assert C.base_full_source("tan32", str(tmp_path)) == "eq0-vllm"
    _lora_check(tmp_path, "tan32", "hf")
    assert C.base_full_source("tan32", tmp_path) == "e12-hf"
    for bad in ("sglang", None):
        _lora_check(tmp_path, "tan32", bad)
        with pytest.raises(ValueError, match="gen_path"):
            C.base_full_source("tan32", tmp_path)
    # rank 1 and rank 32 keep their fixed sources whatever the root holds
    _lora_check(tmp_path, "rank1", "vllm")
    _lora_check(tmp_path, "rank32", "hf")
    assert C.base_full_source("rank1", tmp_path) == "e12-hf"
    assert C.base_full_source("rank32", tmp_path) == "eq0-vllm"
    assert C.base_full_source("rank1", tmp_path / "empty") == "e12-hf"


def test_base_full_source_follows_each_recipes_generator():
    """2026-10-09: rank 1 generates with HF (its re-check failed), rank 32 with vLLM (passed), so
    base's full evaluation is E12's HF one for rank 1 and EQ0's vLLM one for rank 32."""
    from emx.e12 import analysis as A

    assert C.base_full_source("rank1") == "e12-hf"
    assert C.base_full_source("rank32") == "eq0-vllm"
    assert A.output_paths("rank32", "full", "base")[:2] == (C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED)
    assert A.output_paths("rank1", "full", "base")[0] == C.gen_csv("rank1", "full", "base")


def test_pod_reservation_subtracts_recorded_gpu_spend():
    """Amendment D's second pod: the reservation leaves the experiment's GPU limit intact."""
    assert C.pod_reservation_usd(3.49, 17.9044) == 30.09  # rounded down: 17.9044 + 30.09 <= 48
    assert 17.9044 + C.pod_reservation_usd(3.49, 17.9044) <= C.GPU_LIMIT_USD
    assert C.pod_reservation_usd(3.49, C.GPU_LIMIT_USD + 1) == 0.0
    assert C.pod_reservation_usd(3.49, 0.0) == C.pod_reservation_usd(3.49)
