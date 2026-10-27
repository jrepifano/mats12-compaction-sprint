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
