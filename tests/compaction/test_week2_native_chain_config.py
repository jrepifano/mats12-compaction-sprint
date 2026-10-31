"""The native_chain config kind (five-task chains in native harnesses): strict validation and
its cross-field checks against experiment 4's config (astra agy-chain5 plan r1 B3)."""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from compaction.s2.chain import week2 as W

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "week2" / "agy_chain5.toml"


def _cfg() -> dict:
    return tomllib.loads(CONFIG.read_text())


def _errors(cfg: dict) -> str:
    with pytest.raises(ValueError) as e:
        W.validate(cfg)
    return str(e.value)


def _file(tmp: Path, name: str, data: bytes) -> dict:
    p = tmp / name
    p.write_bytes(data)
    return {"path": str(p), "sha256": hashlib.sha256(data).hexdigest()}


def test_the_committed_config_validates_and_matches_its_plan():
    W.validate(_cfg())
    chk = W.plan_check(ROOT / "docs" / "sprint" / "plans" / "week2-agy-chain5.md", CONFIG)
    assert chk["ok"], W.report(chk)


@pytest.mark.parametrize("chains, why", [
    (["u1", "u9"], "'u9' not in the orders"),
    (["u1", "u1"], "no duplicates"),
    (["u1", "w1"], "'w1' is not an unrelated chain"),
    (["u1", "f01"], "'f01' is not an unrelated chain"),
])
def test_chain_selection_is_checked_against_the_orders(chains, why):
    cfg = _cfg()
    cfg["native_chain"]["chains"] = chains
    assert why in _errors(cfg)


def test_phase_count_must_match_the_orders():
    cfg = _cfg()
    cfg["design"]["n_phases"] = 6
    assert "is not an unrelated chain of 6 tasks" in _errors(cfg)


def test_counts_and_caps_in_range():
    cfg = _cfg()
    cfg["native_chain"]["compaction_retries"] = -1
    cfg["native_chain"]["smoke_phases"] = 0
    cfg["caps"]["max_seconds"] = 0
    err = _errors(cfg)
    assert "compaction_retries: must be >= 0" in err
    assert "smoke_phases: must be in 1..5" in err
    assert "caps.max_seconds: 0 differs" in err or "must be > 0" in err


def test_smoke_chain_must_be_selected():
    cfg = _cfg()
    cfg["native_chain"]["smoke_chain"] = "u5"
    assert "smoke_chain: must be one of" in _errors(cfg)


def test_harnesses_are_known_and_distinct():
    cfg = _cfg()
    cfg["native_chain"]["harnesses"] = ["agy", "agy"]
    assert "native_chain.harnesses" in _errors(cfg)
    cfg["native_chain"]["harnesses"] = ["agy", "pi"]
    assert "native_chain.harnesses" in _errors(cfg)


def test_a_harness_table_needs_every_key_and_nonnegative_costs():
    cfg = _cfg()
    del cfg["native_chain"]["codex"]["stage"]
    assert "native_chain.codex: expected a table" in _errors(cfg)
    cfg = _cfg()
    cfg["native_chain"]["claude_code"]["cap_usd"] = -1
    assert "native_chain.claude_code: expected a table" in _errors(cfg)


def test_wording_must_be_experiment_4s(tmp_path):
    cfg = _cfg()
    cfg["wording"]["task_ending"] = _file(tmp_path, "ending.txt", b"Push to {branch}.\n")
    assert "wording.task_ending: differs from source_config's" in _errors(cfg)
    cfg = _cfg()
    del cfg["wording"]["lead_same"]
    assert "wording.lead_same: missing" in _errors(cfg)


def test_call_and_time_caps_must_be_experiment_4s():
    cfg = _cfg()
    cfg["caps"]["max_calls"] = 500
    assert "caps.max_calls: 500 differs from source_config's 400" in _errors(cfg)


def test_orders_must_be_the_source_configs(tmp_path):
    cfg = _cfg()
    orders = json.loads((ROOT / cfg["native_chain"]["orders"]["path"]).read_text())
    cfg["native_chain"]["orders"] = _file(tmp_path, "orders.json", json.dumps(orders).encode())
    assert "native_chain.orders: differs from source_config's chain.orders" in _errors(cfg)


def test_hash_valid_but_unparseable_sources_are_refused(tmp_path):
    cfg = _cfg()
    cfg["native_chain"]["source_config"] = _file(tmp_path, "src.toml", b"not = [toml")
    assert "native_chain.source_config: not a valid week-2 config" in _errors(cfg)
    cfg = _cfg()
    cfg["native_chain"]["orders"] = _file(tmp_path, "o.json", b"{not json")
    assert "native_chain.orders: not JSON" in _errors(cfg)


def test_a_non_chain_source_config_is_refused():
    cfg = _cfg()
    other = ROOT / "configs" / "week2" / "precedent.toml"
    cfg["native_chain"]["source_config"] = {
        "path": "configs/week2/precedent.toml",
        "sha256": hashlib.sha256(other.read_bytes()).hexdigest()}
    assert "not a chain experiment's config" in _errors(copy.deepcopy(cfg))


def test_unknown_and_missing_keys_are_refused():
    cfg = _cfg()
    cfg["native_chain"]["extra"] = 1
    del cfg["native_chain"]["grade_phases"]
    err = _errors(cfg)
    assert "unknown key native_chain.extra" in err
    assert "missing key native_chain.grade_phases" in err


@pytest.mark.parametrize("orders, why", [
    ({"u1": []}, "native_chain.orders: must be a list of chains"),
    ([None, 1], "native_chain.orders[0]: must be an object with a string id"),
    ([{"id": "u1", "phases": 3}], "native_chain.orders[0].phases: must be a list"),
    ([{"id": "u1", "phases": [None] * 5}],
     "native_chain.orders[0].phases[0]: must be an object with a string task"),
])
def test_valid_json_with_a_bad_shape_gives_errors_not_exceptions(tmp_path, orders, why):
    cfg = _cfg()
    cfg["native_chain"]["orders"] = _file(tmp_path, "o.json", json.dumps(orders).encode())
    assert why in _errors(cfg)


@pytest.mark.parametrize("section, key, value, why", [
    ("parallelism", "agents", 0, "parallelism: agents > 0"),
    ("parallelism", "fallback_agents", 5, "parallelism: agents > 0"),
    ("parallelism", "agent_memory_mb", -1, "parallelism: agent_memory_mb > 0"),
    ("parallelism", "fallback_agent_memory_mb", 1000, "parallelism: agent_memory_mb > 0"),
    ("caps", "stage_usd", float("nan"), "caps.stage_usd: must be finite"),
    ("caps", "stage_usd", 200.0, "caps.stage_usd: must equal the sum"),
    ("caps", "run_usd", 60.0, "caps.run_usd: must equal the largest task_cap_usd"),
])
def test_limits_and_caps(section, key, value, why):
    cfg = _cfg()
    cfg[section][key] = value
    assert why in _errors(cfg)


def test_harness_costs_must_be_finite_and_agree_with_the_global_caps():
    cfg = _cfg()
    cfg["native_chain"]["claude_code"]["cap_usd"] = float("inf")
    assert "native_chain.claude_code: expected a table" in _errors(cfg)
    cfg = _cfg()
    cfg["native_chain"]["claude_code"] |= {"cap_usd": 200.0, "task_cap_usd": 60.0}
    err = _errors(cfg)
    assert "caps.stage_usd: must equal the sum" in err and "caps.run_usd: must equal" in err


def test_effort_must_be_one_the_harness_takes():
    cfg = _cfg()
    cfg["native_chain"]["agy_sonnet"]["effort"] = "not-an-effort"
    assert "native_chain.agy_sonnet.effort: one of" in _errors(cfg)
    cfg = _cfg()
    cfg["native_chain"]["claude_code"]["effort"] = "xxhigh"
    assert "native_chain.claude_code.effort: one of" in _errors(cfg)


def test_per_harness_chains_are_checked():
    cfg = _cfg()
    cfg["native_chain"]["claude_code_sonnet"]["chains"] = ["u2", "u9"]
    err = _errors(cfg)
    assert "claude_code_sonnet.chains: distinct ids from" in err
    assert "claude_code_sonnet.chains: must include the smoke chain" in err
