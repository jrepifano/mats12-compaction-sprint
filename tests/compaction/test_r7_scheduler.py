"""R7 scheduler regressions: fake runs and temporary books only; no keys or containers."""
import contextlib
import importlib.util
import json
from pathlib import Path

import pytest

from compaction import budget
from compaction.s2.chain import driver
from compaction.s2.interact_run import PilotSpend
from compaction.s2.model import StageCapReached
from compaction.s2.native import common as N
from compaction.s2.native import key_proxy
from emx import spend


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "scripts/week2_r7_replay.py"
    spec = importlib.util.spec_from_file_location("r7_scheduler", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


@pytest.fixture
def cfg():
    return {"replay": {"output_root": "runs", "ledger": "logs/costs.jsonl"},
            "design": {"stage": "SPRINT-S2-PLANTED", "seed": 42},
            "caps": {"stage_usd": 30, "run_usd": 6},
            "parallelism": {"agents": 4, "fallback_agents": 1,
                            "agent_memory_mb": 1280, "fallback_agent_memory_mb": 3072},
            "_source": "fake-config.toml"}


def prepared(scheduler, rep=1):
    data = b"fake session\n"
    return scheduler.R.Prepared(
        "httpx-np-1", "planted", rep, "fake-sid", data,
        {"input_sha256": scheduler.R.sha256(data),
         "source": {"repo": "httpx", "rep": 1, "remote": "junk"}})


def completed(root, p, end="done"):
    out = root / "runs" / p.run_id
    (out / "b").mkdir(parents=True)
    (out / "input_session.jsonl").write_bytes(p.data)
    (out / "replay.json").write_text(json.dumps(p.provenance))
    (out / "b/result.json").write_text(json.dumps({"end": end}))
    return out


@pytest.mark.parametrize("missing", ["replay.json", "input_session.jsonl", "b/result.json"])
def test_existing_incomplete_output_requires_inspection(scheduler, missing):
    p = prepared(scheduler)
    out = completed(scheduler.ROOT, p)
    (out / missing).unlink()
    with pytest.raises(ValueError, match="incomplete output: .*inspect before retry"):
        scheduler._finished(out, p)


def test_existing_output_still_requires_matching_provenance(scheduler):
    p = prepared(scheduler)
    out = completed(scheduler.ROOT, p)
    (out / "input_session.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="provenance mismatch"):
        scheduler._finished(out, p)


@pytest.mark.parametrize("end, expected_rc", [("done", 0), ("failed", 3), ("unknown", 3)])
def test_all_complete_rebuilds_reconciliation_without_key(scheduler, cfg, monkeypatch,
                                                        end, expected_rc):
    p = prepared(scheduler)
    completed(scheduler.ROOT, p, end)
    (scheduler.ROOT / "runs/reconciliation.json").write_text('{"stale": "done"}')

    def forbidden(*args, **kwargs):
        pytest.fail("completed invocation must not read a key or launch work")

    monkeypatch.setattr(key_proxy, "read_key", forbidden)
    monkeypatch.setattr(scheduler.R, "run_replay", forbidden)
    assert scheduler.bulk(cfg, [p]) == expected_rc
    assert json.loads((scheduler.ROOT / "runs/reconciliation.json").read_text()) == {p.run_id: end}


def test_resume_reconciles_finished_and_new_results(scheduler, cfg, monkeypatch):
    old, new = prepared(scheduler, 1), prepared(scheduler, 2)
    completed(scheduler.ROOT, old)
    launched = []
    tasks = {task: object() for repo in scheduler.CC.REPOS.values()
             for task in (repo.a_task, repo.b_task)}
    monkeypatch.setattr(driver, "tasks", lambda: tasks)
    monkeypatch.setattr(key_proxy, "read_key", lambda *args: "fake-key")
    monkeypatch.setattr(scheduler, "resource_ready", lambda: (True, {}))
    monkeypatch.setattr(scheduler.SL, "reserve", lambda *args, **kw: contextlib.nullcontext())
    monkeypatch.setattr(scheduler.SL, "setting", lambda key, default: default)

    def fake_run(run_cfg, p):
        launched.append(p.run_id)
        completed(scheduler.ROOT, p, "failed")
        return {"end": "failed"}

    monkeypatch.setattr(scheduler.R, "run_replay", fake_run)
    assert scheduler.bulk(cfg, [old, new]) == 3
    assert launched == [new.run_id]
    assert json.loads((scheduler.ROOT / "runs/reconciliation.json").read_text()) == {
        old.run_id: "done", new.run_id: "failed"}


def test_arm_prefix_covers_task_and_egress_slots(scheduler):
    ns = scheduler.X._session("fake-image", 1234, "localhost", arm=scheduler.R.ARM)
    assert (scheduler.R.ARM.model, scheduler.R.ARM.effort) == ("claude-haiku-5-5[1m]", "high")
    assert N.purpose(ns.arm) == scheduler.PREFIX == "native-r7-chain"
    egress = N.Egress(ns.arm, "localhost", 1234, allow=())
    names = [f"{N.purpose(ns.arm)}-run-task", egress.proxy]
    assert scheduler.SL._count({"r7": {"n": 2, "prefix": scheduler.PREFIX}}, names) == {
        "reserved": 2, "external": 0, "total": 2}


def test_caps_ignore_other_stages_settled_spend_but_enforce_r7_stage(scheduler, cfg):
    caps = scheduler.init_caps(cfg)
    ledger = scheduler.ROOT / "logs/costs.jsonl"
    # Synthetic accounting data under tmp_path, not real spend or paid requests.
    spend.record("judge", "fake", 1, 1000, exp="SPRINT-S2-OTHER", ledger=ledger)
    stage = PilotSpend(tags=(scheduler.TAG,), caps={scheduler.TAG: 30},
                       stage=cfg["design"]["stage"], ledger=ledger, caps_file=caps)
    rid = stage.reserve(30, "synthetic test reservation")
    stage.cancel(rid, "test cancelled")
    spend.record("judge", "fake", 1, 29, exp=stage.stage, ledger=ledger)
    with pytest.raises(StageCapReached, match="stage refusal.*stage cap"):
        stage.reserve(2, "exceeds stage even without tagged charge")
    rid = stage.reserve(1, "fits remaining stage allowance")
    stage.cancel(rid, "test cancelled")


def test_shared_open_reservations_still_count_at_sprint_gate(scheduler, cfg):
    caps = scheduler.init_caps(cfg)
    ledger = scheduler.ROOT / "logs/costs.jsonl"
    other_caps = scheduler.ROOT / "other_caps.toml"
    other_caps.write_text(caps.read_text().replace("SPRINT-S2-PLANTED", "SPRINT-S2-OTHER"))
    other_rid = budget.reserve("SPRINT-S2-OTHER", 29, ledger=ledger, caps_file=other_caps)
    stage = PilotSpend(tags=(scheduler.TAG,), caps={scheduler.TAG: 30},
                       stage=cfg["design"]["stage"], ledger=ledger, caps_file=caps)
    with pytest.raises(StageCapReached, match="stage refusal: sprint:"):
        stage.reserve(2, "held by unrelated stage")
    budget.cancel(other_rid, "test cancelled", ledger=ledger, caps_file=other_caps)
    rid = stage.reserve(2, "allowed after unrelated hold closes")
    stage.cancel(rid, "test cancelled")
