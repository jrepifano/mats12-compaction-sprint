"""Week-2 drift-gate schema: the kinds added for experiments 2, 4 and the exploitation ending."""

import copy

import pytest

from compaction.s2.chain import week2 as W

ZERO = "0" * 64


def _exploit_cfg() -> dict:
    return {"meta": {"exp": "x", "proposal": "p.md", "built_against": ZERO},
            "design": {"kind": "exploit", "stage": "S", "seed": 1, "n_phases": 6,
                       "log_reasoning": True},
            "exploit": {"tasks": ["a"], "draw": {"seed": 1, "path": "d.json"},
                        "languages": ["go"], "mutation": "m", "opening_line": "o",
                        "no_test_rule": True, "position": 6, "cells": ["long"], "smoke": 1,
                        "related_chains": "skipped", "signs_of_life_points": 25,
                        "task_files": "t", "validation": "v", "claim_scoring": "c"},
            "caps": {"exploit_usd": 80.0},
            "wording": {"w": {"path": "configs/week2/wording/chain_stress/ticket_tag_line.txt",
                              "sha256": W.sha256_file(W.resolve(
                                  "configs/week2/wording/chain_stress/ticket_tag_line.txt"))}}}


def test_exploit_kind_has_its_own_caps_and_optional_parallelism():
    W.validate(_exploit_cfg())
    cfg = _exploit_cfg()
    cfg["parallelism"] = {"agents": 16, "fallback_agents": 6, "agent_memory_mb": 1280,
                          "fallback_agent_memory_mb": 3072}
    W.validate(cfg)
    cfg["parallelism"].pop("agents")
    with pytest.raises(ValueError, match="missing key parallelism.agents"):
        W.validate(cfg)


def test_exploit_kind_refuses_the_shared_caps_shape_and_a_bad_draw():
    cfg = _exploit_cfg()
    cfg["caps"] = {"stage_usd": 1.0, "run_usd": 1.0, "max_calls": 1, "max_seconds": 1}
    with pytest.raises(ValueError, match="caps.exploit_usd"):
        W.validate(cfg)
    cfg = copy.deepcopy(_exploit_cfg())
    cfg["exploit"]["draw"] = {"seed": True, "path": "d"}
    with pytest.raises(ValueError, match="exploit.draw"):
        W.validate(cfg)


def test_other_kinds_still_need_parallelism_and_their_caps():
    cfg = W.load_config("configs/week2/chain_stress.toml")
    cfg.pop("_source")
    cfg.pop("parallelism")
    with pytest.raises(ValueError, match="missing section \\[parallelism\\]"):
        W.validate(cfg)


def test_stage_cap_reads_each_kinds_key():
    assert W.stage_cap(_exploit_cfg()) == 80.0
    assert W.stage_cap(W.load_config("configs/week2/chain_stress.toml")) == 165.0


def _refusal_cfg() -> dict:
    w = "configs/week2/wording/chain_stress/ticket_tag_line.txt"
    return {"meta": {"exp": "summary_refusal", "proposal": "p.md", "built_against": ZERO},
            "design": {"kind": "summary_refusal", "stage": "S", "seed": 1,
                       "log_reasoning": True},
            "refusal": {"source_exp": "precedent", "source_arm": "divergent",
                        "source_assignment": "a.json", "source_assignment_sha256": ZERO,
                        "source_config": "c.toml", "source_config_sha256": ZERO,
                        "topup_deadline_et": "2026-10-28T11:30",
                        "pairs_only_after_et": "2026-10-28T18:00",
                        "restore_from": "last_rejected_push", "summary_policy": "pi",
                        "resume_template": "t", "placement_section": "C",
                        "placement_fallback_after": "Goal",
                        "versions": ["honest", "decision_dropped", "false_permission",
                                     "false_rule"],
                        "primary": ["false_permission", "honest"],
                        "topup_repos": ["httpx", "tengo"],
                        "monitor_views": ["current_only"],
                        "launch_phases": [["honest", "false_permission"],
                                          ["decision_dropped"], ["false_rule"]],
                        "runs_per_version": 2, "min_histories_test": 6,
                        "target_histories": 8, "max_histories": 12, "topup_max_tasks": 10,
                        "topup_rep_start": 101, "summary_regenerations": 1,
                        "bootstrap_reps": 2000, "crash_reruns": 1, "alpha": 0.05},
            "caps": {"stage_usd": 50.0, "topup_usd": 12.0, "run_usd": 1.0, "max_calls": 120,
                     "max_seconds": 1800, "summary_usd": 0.1},
            "parallelism": {"rig_agents": 6, "laptop_agents": 8, "laptop_ramp": [4, 8],
                            "laptop_ramp_after_s": 120.0, "laptop_poll_s": 2.0,
                            "laptop_agent_memory_mb": 1280, "laptop_agent_cpus": 1,
                            "laptop_min_free_mem_frac": 0.3, "laptop_max_load1": 12.0,
                            "max_concurrent_histories": 1, "sandbox_label": "refusal"},
            "wording": {"w": {"path": w, "sha256": W.sha256_file(W.resolve(w))}}}


def test_summary_refusal_kind_validates_without_phases_and_with_its_own_shapes():
    W.validate(_refusal_cfg())
    assert W.stage_cap(_refusal_cfg()) == 50.0
    for sec, key, bad, msg in [("refusal", "launch_phases", [["a"], []], "refusal.launch_phases"),
                               ("refusal", "launch_phases", ["a"], "refusal.launch_phases"),
                               ("refusal", "launch_phases", [[1]], "refusal.launch_phases"),
                               ("refusal", "launch_phases", [[True]], "refusal.launch_phases"),
                               ("refusal", "launch_phases", [["a + b"]],
                                "refusal.launch_phases"),
                               ("refusal", "launch_phases", [["a, b"]],
                                "refusal.launch_phases"),
                               ("refusal", "launch_phases", [[" a"]], "refusal.launch_phases"),
                               ("parallelism", "laptop_ramp", [4.5], "parallelism.laptop_ramp"),
                               ("caps", "summary_usd", "x", "caps.summary_usd")]:
        cfg = _refusal_cfg()
        cfg[sec][key] = bad
        with pytest.raises(ValueError, match=msg):
            W.validate(cfg)
    cfg = _refusal_cfg()
    cfg["design"]["n_phases"] = 2
    with pytest.raises(ValueError, match="unknown key design.n_phases"):
        W.validate(cfg)
    cfg = _refusal_cfg()
    cfg["caps"].pop("topup_usd")
    with pytest.raises(ValueError, match="missing key caps.topup_usd"):
        W.validate(cfg)


def test_summary_refusal_section_is_refused_for_other_kinds_and_flattens_groups():
    cfg = W.load_config("configs/week2/chain_stress.toml")
    cfg.pop("_source")
    cfg["refusal"] = _refusal_cfg()["refusal"]
    with pytest.raises(ValueError, match="section \\[refusal\\] not allowed"):
        W.validate(cfg)
    flat = W.flatten(_refusal_cfg())
    assert flat["refusal.launch_phases"] == "honest + false_permission, decision_dropped, false_rule"
    assert W.canon(flat["refusal.launch_phases"]) == (
        "honest + false_permission", "decision_dropped", "false_rule")
    assert flat["refusal.versions"] == "honest, decision_dropped, false_permission, false_rule"


def test_other_kinds_still_need_design_n_phases():
    cfg = W.load_config("configs/week2/chain_stress.toml")
    cfg.pop("_source")
    cfg["design"].pop("n_phases")
    with pytest.raises(ValueError, match="missing key design.n_phases"):
        W.validate(cfg)


@pytest.mark.parametrize("phases", [[["honest"], ["false_permission"], ["decision_dropped"],
                                     ["false_rule"]],
                                    [["honest", "false_permission", "decision_dropped"],
                                     ["false_rule"]],
                                    [["false_permission", "honest"], ["decision_dropped"],
                                     ["false_rule"]],
                                    [["decision_dropped"], ["honest", "false_permission"],
                                     ["false_rule"]]])
def test_regrouped_or_reordered_launch_phases_differ_from_the_contract(phases):
    contract = W.canon("`honest + false_permission, decision_dropped, false_rule`")
    assert W.canon(W.flatten(_refusal_cfg())["refusal.launch_phases"]) == contract
    cfg = _refusal_cfg()
    cfg["refusal"]["launch_phases"] = phases
    assert W.canon(W.flatten(cfg)["refusal.launch_phases"]) != contract
