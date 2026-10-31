"""Tests for the week-2 plan-drift guard (compaction.s2.chain.week2) and its CLI, on the
task-chaining fixture. No network, no Docker; anything mutated is a tmp_path copy."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from compaction.s2.chain import config as CC
from compaction.s2.chain import week2 as W

ROOT = W.REPO_ROOT
CONFIG = ROOT / "configs" / "week2" / "task_chaining.toml"
PROPOSAL = ROOT / "tests" / "compaction" / "fixtures" / "week2" / "task_chaining_proposal.md"
WORDING_OF_HASH = {"TASK_ENDING": "task_ending", "PERMISSION_LINE": "permission_line",
                   "LEAD_SAME": "lead_same", "LEAD_DIFFERENT": "lead_different",
                   "CHECKINS": "checkins", "TEAMMATE_MESSAGE": "teammate_message",
                   "JUNK_COMMITS": "junk_commits", "teammate_text:httpx": "teammate_text_httpx",
                   "teammate_text:tengo": "teammate_text_tengo"}


@pytest.fixture
def tmp_plan(tmp_path):
    """Copies of the config and proposal, plus a tmp accepted dir and build log."""
    cfg, prop = tmp_path / "cfg.toml", tmp_path / "proposal.md"
    shutil.copy(CONFIG, cfg)
    shutil.copy(PROPOSAL, prop)
    return {"cfg": cfg, "prop": prop, "accepted": tmp_path / "accepted",
            "log": tmp_path / "log.md"}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the fixture --------------------------------------------------------------------------


def test_fixture_passes():
    check = W.plan_check(PROPOSAL, CONFIG)
    assert check["ok"], W.report(check)
    assert check["hash_ok"] and check["changed_sections"] == []
    assert W.gate(PROPOSAL, CONFIG)["ok"]


def test_wording_files_are_the_codes_frozen_texts():
    cfg = W.load_config(CONFIG)
    hashes = CC.text_hashes()
    assert set(WORDING_OF_HASH.values()) == set(cfg["wording"])
    for k, name in WORDING_OF_HASH.items():
        assert cfg["wording"][name]["sha256"] == hashes[k], name


def test_config_values_match_the_code():
    tp = W.load_config(CONFIG)["two_phase"]
    assert tuple(tp["checkin_calls"]) == CC.CHECKIN_CALLS
    assert tp["permission_after_call"] == CC.PERMISSION_AFTER_CALL
    assert tuple(tp["arms"]) == CC.PERMS and tuple(tp["starts"]) == CC.STARTS


def test_two_phase_runs_match_b_runs():
    runs = W.two_phase_runs(W.load_config(CONFIG))
    assert [r.id for r in runs] == [b.id for b in CC.b_runs()]
    assert runs == CC.b_runs()
    assert len(runs) == 88
    rows = W.two_phase_rows(W.load_config(CONFIG))
    plain = [r for r in rows if r["arm"] == "none"]
    assert len(plain) == 8 and all(len(r["tasks"]) == 1 for r in plain)
    r = next(r for r in rows if r["id"] == "httpx-p-different-full-tengo-p-3")
    assert r["tasks"] == [CC.REPOS["tengo"].a_task, CC.REPOS["httpx"].b_task]


# --- load_config refusals -----------------------------------------------------------------


@pytest.mark.parametrize("old,new,needle", [
    ("agents = 10\n", "agents = 10\nbogus = 1\n", "unknown key parallelism.bogus"),
    ("a_retries = 1\n", "", "missing key two_phase.a_retries"),
    ("seed = 20261026", 'seed = "20261026"', "design.seed: expected int"),
    ("max_calls = 400", "max_calls = 400.0", "caps.max_calls: expected int"),
    ('kind = "two_phase"', 'kind = "chain"', "section [two_phase] not allowed"),
    ('kind = "two_phase"', 'kind = "three_phase"', "design.kind: expected one of"),
    ("n_phases = 2", "n_phases = 3", "must be 2 for two_phase"),
    ("[parallelism]", "[extra]\nx = 1\n\n[parallelism]", "unknown section [extra]"),
])
def test_load_config_refuses(tmp_plan, old, new, needle):
    edit(tmp_plan["cfg"], old, new)
    with pytest.raises(ValueError) as e:
        W.load_config(tmp_plan["cfg"])
    assert needle in str(e.value)


def test_load_config_lists_every_error(tmp_plan):
    edit(tmp_plan["cfg"], "a_retries = 1\n", "")
    edit(tmp_plan["cfg"], "agents = 10\n", "agents = 10\nbogus = 1\n")
    with pytest.raises(ValueError) as e:
        W.load_config(tmp_plan["cfg"])
    assert "missing key two_phase.a_retries" in str(e.value)
    assert "unknown key parallelism.bogus" in str(e.value)


def test_load_config_refuses_wording_sha_mismatch(tmp_plan, tmp_path):
    good = W.load_config(CONFIG)["wording"]["lead_same"]["sha256"]
    edit(tmp_plan["cfg"], good, "0" * 64)
    with pytest.raises(ValueError, match="wording.lead_same: sha256 mismatch"):
        W.load_config(tmp_plan["cfg"])
    # an edited wording file is refused the same way
    txt = tmp_path / "lead_same.txt"
    txt.write_text(CC.LEAD_SAME + " ")
    edit(tmp_plan["cfg"], "0" * 64, good)
    edit(tmp_plan["cfg"], '"configs/week2/wording/task_chaining/lead_same.txt"', f'"{txt}"')
    with pytest.raises(ValueError, match="wording.lead_same: sha256 mismatch"):
        W.load_config(tmp_plan["cfg"])


# --- normalization and the contract -------------------------------------------------------


def test_numbers_and_lists_normalize():
    assert W.canon("170") == W.canon("170.0") == W.canon("$170") == W.canon("`170`")
    assert W.canon("$1,700") == W.canon("1700")
    assert W.canon("2.5") == W.canon("$2.50")
    assert W.canon("4, 14,24") == W.canon("4,  14, 24.0")
    assert W.canon("np, p") != W.canon("p, np")
    flat = W.flatten(W.load_config(CONFIG))
    assert flat["caps.stage_usd"] == "170" and flat["two_phase.arms"] == "np, p"
    assert "meta.exp" not in flat and not any(k.startswith("_") for k in flat)
    assert flat["wording.lead_same.path"].endswith("lead_same.txt")


def test_parse_contract():
    md = ("# T\n\n## Build contract\n\nText.\n\n| Parameter | Value |\n|---|---|\n"
          "| `a.b` | `1` |\n| a.c | x, y |\n\n| parameter | value |\n|---|---|\n| z | 9 |\n"
          "## Next\n")
    assert W.parse_contract(md) == {"a.b": "1", "a.c": "x, y"}
    with pytest.raises(ValueError, match="duplicate"):
        W.parse_contract(md.replace("| a.c |", "| a.b |"))
    with pytest.raises(ValueError, match="no \"Build contract\""):
        W.parse_contract("# T\n\n| parameter | value |\n|---|---|\n| a | 1 |\n")


def test_contract_value_change_fails_the_gate(tmp_plan):
    edit(tmp_plan["prop"], "| `two_phase.histories_per_arm` | 4 |",
         "| `two_phase.histories_per_arm` | 6 |")
    with pytest.raises(W.PlanDrift) as e:
        W.gate(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])
    msg = str(e.value)
    assert "two_phase.histories_per_arm" in msg and "'6'" in msg and "'4'" in msg
    assert "--accept-plan " + sha(tmp_plan["prop"]) in msg


def test_missing_rows_reported_both_ways(tmp_plan):
    edit(tmp_plan["prop"], "| `caps.max_calls` | 400 |\n", "| `caps.max_tokens` | 400 |\n")
    check = W.plan_check(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])
    assert not check["ok"]
    assert check["missing_in_contract"] == ["caps.max_calls"]
    assert check["missing_in_config"] == ["caps.max_tokens"]
    assert check["mismatches"] == []


# --- the proposal hash, changed sections and accepting a plan -----------------------------


def test_no_accepted_copy_reports_whole_proposal(tmp_plan):
    edit(tmp_plan["prop"], "intervals per cell.", "intervals per cell (Wilson).")
    check = W.plan_check(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])
    assert not check["hash_ok"] and not check["ok"]
    assert check["changed_sections"] == [W.NO_ACCEPTED_COPY]


def test_prose_edit_names_exactly_that_section(tmp_plan):
    W.accept_plan(tmp_plan["prop"], tmp_plan["cfg"], sha(tmp_plan["prop"]), tmp_plan["log"],
                  tmp_plan["accepted"])
    assert W.gate(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])["ok"]
    edit(tmp_plan["prop"], "intervals per cell.", "intervals per cell (Wilson).")
    check = W.plan_check(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])
    assert not check["hash_ok"] and check["mismatches"] == []
    assert check["changed_sections"] == ["Analysis"]
    with pytest.raises(W.PlanDrift) as e:
        W.gate(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])
    assert "changed section: Analysis" in str(e.value)
    assert check["built_against"] in str(e.value) and sha(tmp_plan["prop"]) in str(e.value)


def test_changed_sections_added_and_removed():
    old = "# T\n\nintro\n\n## A\n\na\n\n## B\n\nb\n"
    new = "# T\n\nintro\n\n## A\n\na2\n\n## C\n\nc\n"
    assert W.changed_sections(old, new) == ["A", "C", "B"]
    assert W.changed_sections(old, old) == []


def test_accept_plan(tmp_plan):
    W.accept_plan(tmp_plan["prop"], tmp_plan["cfg"], sha(tmp_plan["prop"]), tmp_plan["log"],
                  tmp_plan["accepted"])
    old_sha = sha(tmp_plan["prop"])
    edit(tmp_plan["prop"], "intervals per cell.", "intervals per cell (Wilson).")
    new_sha = sha(tmp_plan["prop"])
    before = tmp_plan["cfg"].read_bytes()
    with pytest.raises(ValueError, match="not the proposal's current sha256"):
        W.accept_plan(tmp_plan["prop"], tmp_plan["cfg"], old_sha, tmp_plan["log"],
                      tmp_plan["accepted"])
    assert tmp_plan["cfg"].read_bytes() == before
    res = W.accept_plan(tmp_plan["prop"], tmp_plan["cfg"], new_sha, tmp_plan["log"],
                        tmp_plan["accepted"])
    assert res["old"] == old_sha and res["new"] == new_sha
    assert res["changed_sections"] == ["Analysis"]
    a, b = before.decode().splitlines(), tmp_plan["cfg"].read_text().splitlines()
    assert len(a) == len(b)
    diff = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
    assert diff == [(f'built_against = "{old_sha}"', f'built_against = "{new_sha}"')]
    assert tmp_plan["cfg"].read_bytes().endswith(b"\n")
    copy = W.accepted_copy("task_chaining", new_sha, tmp_plan["accepted"])
    assert copy.read_bytes() == tmp_plan["prop"].read_bytes()
    log = tmp_plan["log"].read_text()
    assert log.startswith("# Week-2 build log\n") and log.count("# Week-2 build log") == 1
    assert log.count("plan accepted: task_chaining") == 2
    assert f"{old_sha} → {new_sha}" in log
    assert '| Clause | Commit, or "no code impact: <why>" |' in log
    assert log.rstrip().endswith("| Analysis | |")
    assert W.gate(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])["ok"]


def test_accept_does_not_bypass_the_contract(tmp_plan):
    edit(tmp_plan["prop"], "| `two_phase.histories_per_arm` | 4 |",
         "| `two_phase.histories_per_arm` | 6 |")
    W.accept_plan(tmp_plan["prop"], tmp_plan["cfg"], sha(tmp_plan["prop"]), tmp_plan["log"],
                  tmp_plan["accepted"])
    check = W.plan_check(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])
    assert check["hash_ok"] and not check["ok"]
    with pytest.raises(W.PlanDrift, match="two_phase.histories_per_arm"):
        W.gate(tmp_plan["prop"], tmp_plan["cfg"], tmp_plan["accepted"])


# --- chain configs ------------------------------------------------------------------------


def chain_config(tmp_path: Path, chains: list[dict]) -> Path:
    orders = tmp_path / "orders.json"
    orders.write_text(json.dumps(chains))
    text = CONFIG.read_text()
    start, end = text.index("[two_phase]"), text.index("[caps]")
    block = (f'[chain]\norders = {{ path = "{orders}", sha256 = "{sha(orders)}" }}\n'
             'arms = ["np", "p"]\nboundary = "compacted"\nauto_compact_tokens = 0\n'
             "update_prompt_from_boundary = 2\nfork_after_every_phase = true\n\n")
    text = (text[:start] + block + text[end:]).replace('kind = "two_phase"', 'kind = "chain"')
    text = text.replace("n_phases = 2", "n_phases = 5")
    cfg = tmp_path / "chain.toml"
    cfg.write_text(text)
    return cfg


def chain(cid: str, arm: str, n: int) -> dict:
    return {"id": cid, "arm": arm,
            "phases": [{"task": f"t{i}", "project": "fork" if i % 2 else "switch"}
                       for i in range(n)]}


def test_chain_config_loads(tmp_path):
    cfg = W.load_config(chain_config(tmp_path, [chain("c1", "np", 5), chain("c2", "p", 5)]))
    assert cfg["design"]["kind"] == "chain"
    flat = W.flatten(cfg)
    assert flat["chain.fork_after_every_phase"] == "true"
    assert flat["chain.orders.sha256"] == cfg["chain"]["orders"]["sha256"]


def test_chain_orders_refused(tmp_path):
    with pytest.raises(ValueError, match=r"\(c2\): 4 phases, design.n_phases = 5"):
        W.load_config(chain_config(tmp_path, [chain("c1", "np", 5), chain("c2", "p", 4)]))
    with pytest.raises(ValueError, match="arm 'x' not in chain.arms"):
        W.load_config(chain_config(tmp_path, [chain("c1", "x", 5)]))


# --- design card and CLI ------------------------------------------------------------------


def test_design_card(tmp_path):
    cfg = W.load_config(CONFIG)
    out = tmp_path / "card.md"
    text = W.design_card(cfg, W.two_phase_rows(cfg), out)
    assert out.read_text() == text
    assert "| **total** | | | **88** |" in text
    for v in cfg["wording"].values():
        assert v["sha256"] in text
    assert cfg["_source"]["sha256"] == sha(CONFIG)
    assert cfg["meta"]["built_against"] in text and "seed 20261026" in text
    assert "agents 10 at 2048 MB, fallback 6 at 3072 MB" in text


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "week2_plan_check.py"), *args],
                          capture_output=True, text=True, cwd=ROOT, check=False)


def test_cli(tmp_plan, tmp_path):
    r = run_cli(str(PROPOSAL), str(CONFIG), "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["ok"] is True
    card = tmp_path / "card.md"
    r = run_cli(str(PROPOSAL), str(CONFIG), "--design-card", str(card))
    assert r.returncode == 0 and "**88**" in card.read_text()
    edit(tmp_plan["prop"], "| `caps.max_calls` | 400 |", "| `caps.max_calls` | 500 |")
    r = run_cli(str(tmp_plan["prop"]), str(tmp_plan["cfg"]))
    assert r.returncode == 1 and "caps.max_calls" in r.stdout
    r = run_cli(str(tmp_plan["prop"]), str(tmp_plan["cfg"]), "--accept-plan", "0" * 64)
    assert r.returncode == 1 and "refused" in r.stderr
