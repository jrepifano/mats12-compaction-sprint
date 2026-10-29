"""scripts/week2_agy_task2_rerun.py, its readout, and agy_chain's task caps ($0: fakes only; no
Docker, no agy, no model calls, nothing written to the real ledger)."""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_native_agy_chain import _fake_session, _history_cfg

from compaction.s2 import interact as I
from compaction.s2.chain import config as CC
from compaction.s2.native import agy_chain as X
from compaction.s2.native import agy_fix as F

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mods():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        yield (importlib.import_module("week2_agy_task2_rerun"),
               importlib.import_module("week2_agy_task2_rerun_readout"))
    finally:
        sys.path.remove(str(ROOT / "scripts"))


@pytest.fixture
def rr(mods):
    return mods[0]


@pytest.fixture
def ro(mods):
    return mods[1]


def fake_tasks() -> dict:
    tk = SimpleNamespace(image="img", instruction=f"Fix it.\n\n{CC.DEEPSWE_ENDING}")
    return {t: tk for r in CC.REPOS.values() for t in (r.a_task, r.b_task)}


ORDER = [{"pos": 1, "round": 1, "first_task": "tengo", "arm": "divergent", "rep": 1,
          "history": "tengo-np-1"}]


def source_dir(tmp_path: Path, order=ORDER) -> Path:
    """Last night's layout for each ordered history: attempt1/a from `agy_fix.stub_world`
    (a verified pre-compaction export) and a task-2 result."""
    src = tmp_path / "agy"
    for o in order:
        att = src / o["history"] / "attempt1"
        F.stub_world(att, repo=o["first_task"])
        (att / "b").mkdir(parents=True)
        (att / "b" / "result.json").write_text(json.dumps({"end": X.END_CALLS}))
    (src / "assignment.json").write_text(json.dumps({"order": order}))
    return src


# ---------------------------------------------------------------------------- the caps


def _capture_run(monkeypatch):
    seen = {}

    def drive(self, prompt):
        seen.update(max_calls=self.max_calls, left=self.left_s())
        self.cid = "c"
        return X.END_SUBMITTED
    return seen, drive


def test_default_caps_are_experiment_1s_and_reach_the_task_run(monkeypatch, tmp_path):
    cfg = _history_cfg(tmp_path)
    assert (cfg.max_calls, cfg.max_seconds) == (CC.MAX_CALLS, CC.MAX_SECONDS) == (400, 7200)
    seen, drive = _capture_run(monkeypatch)
    _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, drive=drive)
    X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert seen["max_calls"] == 400 and 7200 - 60 < seen["left"] <= 7200


def test_raised_caps_reach_the_task_run(monkeypatch, tmp_path):
    cfg = _history_cfg(tmp_path)
    cfg.max_calls, cfg.max_seconds = 800, 4 * 3600
    seen, drive = _capture_run(monkeypatch)
    _fake_session(monkeypatch, tmp_path, end=X.END_SUBMITTED, drive=drive)
    X._task(cfg, tmp_path, cfg.a_task, [], second=False, prompt="p")
    assert seen["max_calls"] == 800 and 4 * 3600 - 60 < seen["left"] <= 4 * 3600


def test_the_call_watch_and_the_drive_loop_follow_max_calls(monkeypatch):
    run = X.TaskRun(ns=SimpleNamespace(sb=None), out=None,
                    deadline=time.monotonic() + 3600, max_calls=800)
    n = {"v": 799}
    monkeypatch.setattr(X, "count_calls", lambda sb, first: n["v"])
    hit: dict = {}
    assert run._watch(hit)() is False and not hit
    n["v"] = 800
    assert run._watch(hit)() is True and hit == {"calls": 800}
    # 500 calls: capped at the default, not at 800 (the next turn launches)
    monkeypatch.setattr(X.TaskRun, "n_calls", lambda self: 500)
    assert X.TaskRun(ns=None, out=None, deadline=time.monotonic() + 3600).drive("p") == \
        X.END_CALLS
    launched = []

    def call(self, prompt, k, **kw):
        launched.append(k)
        return X.T.Stop(text=""), {"quota": True, "shared": True}, []
    monkeypatch.setattr(X.TaskRun, "call", call)
    assert run.drive("p") == X.END_QUOTA and launched == [0]


def test_each_turns_timeouts_follow_max_seconds(monkeypatch, tmp_path):
    seen = {}

    class Stop(Exception):
        pass

    def detached(sb, cmd, *, timeout_s, **kw):
        seen.update(timeout_s=timeout_s, cmd=cmd)
        raise Stop
    monkeypatch.setattr(X.N, "detached_exec", detached)
    run = X.TaskRun(ns=SimpleNamespace(sb=None), out=tmp_path,
                    deadline=time.monotonic() + 4 * 3600, cid="c", max_calls=800)
    with pytest.raises(Stop):
        run.call("p", 0)
    assert 4 * 3600 - 60 < seen["timeout_s"] <= 4 * 3600
    pt = int(seen["cmd"].split("--print-timeout ")[1].split("s")[0])
    assert pt == max(30, seen["timeout_s"] - 15)


def test_the_rerun_config_raises_only_the_caps(rr, tmp_path):
    q = threading.Event()
    cfg = rr.config(ORDER[0], tmp_path, fake_tasks(), host=None, quota=q)
    assert (cfg.max_calls, cfg.max_seconds) == (800, 4 * 3600) == (rr.MAX_CALLS,
                                                                   rr.MAX_SECONDS)
    assert cfg.quota_stop is q and cfg.hist.id == "tengo-np-1"
    assert (cfg.memory_mb, cfg.cpus, cfg.pre_compact_checkpoint) == (
        CC.AGENT_MEMORY_MB, CC.AGENT_CPUS, True)  # everything else as the harness builds it
    assert rr.MAX_SECONDS + 3600 <= I.CONTAINER_LIFETIME_S  # containers outlive the task


# ---------------------------------------------------------------------------- one attempt


@pytest.fixture
def fake(monkeypatch):
    fk = F.FakeAgy(F.STUB_REPLIES)
    for owner, name, value in F.fake_targets(fk):
        monkeypatch.setattr(owner, name, value)
    caps = []

    def task(cfg, out, *a, **kw):
        caps.append((cfg.max_calls, cfg.max_seconds))
        res = fk.task2(cfg, out, *a, **kw)
        p = Path(out) / "result.json"  # as `_task` records raised caps, plus a call count
        r = json.loads(p.read_text())
        r["extra"]["caps"] = {"max_calls": cfg.max_calls, "max_seconds": cfg.max_seconds}
        p.write_text(json.dumps(r | {"n_calls": 612}))
        return res
    monkeypatch.setattr(X, "_task", task)
    fk.caps = caps
    return fk


def _source(rr, src: Path, o=ORDER[0], quota=None) -> F.Source:
    att = rr.source_attempt(src / o["history"])
    cfg = rr.config(o, att, fake_tasks(), host=None, quota=quota or threading.Event())
    return F.load_source(cfg, att / "a")


def test_an_attempt_compacts_the_export_afresh_saves_it_then_runs_task_2(rr, fake, tmp_path):
    src = _source(rr, source_dir(tmp_path))
    att = tmp_path / "rerun" / "tengo-np-1" / "attempt1"
    rec = rr.run_attempt(src, att, ensure_volume=lambda host, image: "vol")
    hold = json.loads((att / "a_compact" / "hold.json").read_text())
    # the native arm: no request turn, a fork of the verified pre-compaction export
    assert hold["request"] is None and hold["ok"] and "request_turn" not in hold
    assert hold["forked_from"] == src.man["conversation_id"] != hold["cid"]
    assert hold["first_step"] == src.man["last_step_index"] + 1
    # the compacted session saved this time, and it is what task 2 resumed
    blobs, man = F.load_session(att / "a_compact" / "session")
    assert man["conversation_id"] == hold["cid"] and set(blobs) == {
        f"conversations/{hold['cid']}.db"}
    (call,) = fake.task2_calls
    assert call["cid"] == hold["cid"] and call["checkpoint"] == F.db_checkpoint_texts(
        blobs, hold["cid"])[-1] and "<summary>" in call["checkpoint"]
    assert call["first_step"] > src.man["last_step_index"] + 1
    assert call["prompt"] == F.task2_prompt(src)
    # task 2 with 800 calls and 4 h
    assert fake.caps == [(800, 4 * 3600)]
    assert rec["b_end"] == X.END_SUBMITTED and rec["caps"] == {"max_calls": 800,
                                                               "max_seconds": 4 * 3600}
    assert json.loads((att / "rerun.json").read_text())["hold"]["ok"] is True


def test_a_quota_stop_before_the_hold_keeps_the_slot_unknown(rr, fake, tmp_path):
    q = threading.Event()
    q.set()
    src = _source(rr, source_dir(tmp_path), quota=q)
    att = tmp_path / "r" / "attempt1"
    holds = []
    rec = rr.run_attempt(src, att, hold=lambda *a: holds.append(a),
                         ensure_volume=lambda h, i: None)
    rb = json.loads((att / "b" / "result.json").read_text())
    assert holds == [] and fake.task2_calls == [] and rec["b_end"] == X.END_QUOTA
    assert rb["end"] == X.END_QUOTA and rb["extra"]["primary_unknown_reason"]
    assert rr.history_state(att.parent)["state"] == "done"  # kept, never rerun


def test_quota_in_the_hold_writes_task_2_as_quota_refused_and_is_final(rr, fake, tmp_path):
    src_dir = source_dir(tmp_path)
    src = _source(rr, src_dir)
    att = tmp_path / "r" / "tengo-np-1" / "attempt1"

    def hold(s, out, request):
        s.cfg.quota_stop.set()
        return {"ok": False, "quota": True, "why": "quota at the boundary"}
    rec = rr.run_attempt(src, att, hold=hold, ensure_volume=lambda h, i: None)
    rb = json.loads((att / "b" / "result.json").read_text())
    assert rec["quota"] and fake.task2_calls == [] and rb["end"] == X.END_QUOTA
    assert "quota at the boundary" in rb["extra"]["primary_unknown_reason"]
    assert X.quota_marks(rb)
    st = rr.history_state(att.parent)
    assert st["state"] == "done" and st["quota_cut"]


def test_a_failed_hold_is_retried_once_then_left_unknown(rr, monkeypatch, tmp_path):
    src_dir = source_dir(tmp_path)
    monkeypatch.setattr(rr, "SRC_DIR", src_dir)
    calls = []

    def run(src, att):
        calls.append(att.name)
        raise rr.HoldFailed("holding phase failed: compact: SandboxError")

    @contextlib.contextmanager
    def held(*a, **k):
        yield
    root = tmp_path / "rerun"
    st = rr.run_history(ORDER[0], root, fake_tasks(), host="h", lim={}, memory_mb=1280,
                        cpus=1.0, quota=threading.Event(), say=lambda m: None, run=run,
                        held=held)
    assert calls == ["attempt1", "attempt2"] and st["state"] == "unknown"
    assert [e["attempt"] for e in st["errors"]] == ["attempt1", "attempt2"]
    assert "HoldFailed" in st["errors"][0]["error"]


def test_a_done_history_is_never_relaunched_and_task_2_is_booked_once(rr, monkeypatch,
                                                                      tmp_path):
    src_dir = source_dir(tmp_path)
    monkeypatch.setattr(rr, "SRC_DIR", src_dir)
    monkeypatch.setattr(rr, "LEDGER", tmp_path / "costs.jsonl")
    calls = []

    def run(src, att):
        calls.append(att.name)
        (att / "b").mkdir(parents=True)
        (att / "b" / "result.json").write_text(json.dumps({"end": X.END_CALLS,
                                                           "n_calls": 812}))
        return {"b_end": X.END_CALLS}

    @contextlib.contextmanager
    def held(*a, **k):
        yield
    root = tmp_path / "rerun"
    kw = {"host": "h", "lim": {}, "memory_mb": 1280, "cpus": 1.0, "quota": threading.Event(),
              "say": lambda m: None, "run": run, "held": held}
    rr.run_history(ORDER[0], root, fake_tasks(), **kw)
    st = rr.run_history(ORDER[0], root, fake_tasks(), **kw)
    assert calls == ["attempt1"] and st["state"] == "done" and st["n_calls"] == 812
    from emx import spend
    rows = spend.read_entries(tmp_path / "costs.jsonl")
    assert len(rows) == 1
    (row,) = rows
    assert (row["exp"], row["kind"], row["resource"], row["usd"], row["units"]) == (
        rr.STAGE, "judge", "plan-quota", 0.0, 812)
    assert not rr.book(root / "tengo-np-1" / "attempt1" / "b", "tengo-np-1")  # once only


def test_the_bulk_launches_nothing_after_the_quota_flag(rr, monkeypatch, tmp_path):
    order = [{"pos": i + 1, "round": 1, "first_task": "tengo", "arm": "divergent",
              "rep": i + 1, "history": f"tengo-np-{i + 1}"} for i in range(3)]
    src = tmp_path / "agy"
    src.mkdir()
    (src / "assignment.json").write_text(json.dumps({"order": order}))
    monkeypatch.setattr(rr, "SRC_DIR", src)
    monkeypatch.setattr(rr, "preflight", lambda root, tasks: (
        root.mkdir(parents=True, exist_ok=True), {"ok": True, "n": 3})[1])
    launched = []

    def run_one(o, root, tasks, *, quota, **kw):
        launched.append(o["history"])
        quota.set()  # a refusal in this history's hold or task 2
        att = root / o["history"] / "attempt1" / "b"
        att.mkdir(parents=True)
        X.skipped_run(att, end=X.END_QUOTA, why="q", second=True, primary_unknown="q")
    root = tmp_path / "rerun"
    rc = rr.bulk("h", {"max_live": 1}, 1280, root=root, tasks=fake_tasks(),
                 admit=lambda n, host, lim: (n == 0, {}), run_one=run_one, poll_s=0.01)
    rec = json.loads((root / "reconciliation.json").read_text())
    assert rc == 3 and launched == ["tengo-np-1"]
    assert rec["tengo-np-1"]["state"] == "done" and rec["tengo-np-1"]["primary_unknown_reason"]
    assert {rec[h]["state"] for h in ("tengo-np-2", "tengo-np-3")} == {"not_launched"}
    assert "quota" in rec["tengo-np-2"]["reason"]
    prov = json.loads((root / "provenance.json").read_text())["launches"][-1]
    assert prov["caps"] == {"max_calls": 800, "max_seconds": 4 * 3600}
    assert prov["defaults_unchanged"] == {"max_calls": 400, "max_seconds": 7200}
    assert prov["git_head"] and prov["code_sha256"]["src/compaction/s2/native/agy_fix.py"]
    asg = json.loads((root / "assignment.json").read_text())
    assert asg["order"] == order and asg["caps"]["max_calls"] == 800


def test_check_verifies_every_export_without_docker(rr, tmp_path):
    src = source_dir(tmp_path)
    res = rr.check(src, fake_tasks())
    (row,) = res["rows"]
    assert row["ok"] and row["pre_compact_ok"] and row["a_steps"] == 5
    assert not res["ok"]  # one history, not the 24
    db = next((src / "tengo-np-1" / "attempt1" / "a" / "pre_compact" / "session"
               / "conversations").glob("*.db"))
    db.write_bytes(db.read_bytes() + b"x")
    (row,) = rr.check(src, fake_tasks())["rows"]
    assert not row["ok"] and "differs from its manifest" in row["why"]


# ---------------------------------------------------------------------------- the readout


def test_the_layout_is_read_by_the_harness_readouts_agy_row_code(rr, ro, fake, monkeypatch,
                                                                 tmp_path):
    src = _source(rr, source_dir(tmp_path))
    root = tmp_path / "raw" / "agy_rerun800"
    root.mkdir(parents=True)
    (root / "assignment.json").write_text(json.dumps({"order": ORDER}))
    att = root / "tengo-np-1" / "attempt1"
    rr.run_attempt(src, att, ensure_volume=lambda h, i: None)
    hr = ro.harness_readout(root)
    assert hr.last_attempt(root / "tengo-np-1") == att
    (row,), fs1, _ = hr.rows_for(ro.KEY)
    assert row["dir"] == str(att / "b") and row["end"] == X.END_SUBMITTED
    assert row["teammate_lost"] is None and fs1 == {}  # no scorer state in the fake; no task 1
    # with a scorer state, experiment 1's scorer reads the rerun's b dir as it reads the bulk's
    res = json.loads((att / "b" / "result.json").read_text())
    (att / "b" / "result.json").write_text(json.dumps(res | {"traps_home": "traps_home"}))
    seen = []
    monkeypatch.setattr(hr.S, "score_b", lambda d, brun: (
        seen.append((d, brun["cell"])), {"end": "submitted", "teammate_lost": True,
                                         "n_push_commands": 2})[1])
    (row,), _, _ = hr.rows_for(ro.KEY)
    assert row["teammate_lost"] is True and seen[0][0] == att / "b"
    assert seen[0][1] == {"b_task": CC.REPOS["httpx"].b_task, "hist": "divergent",
                          "project": "different", "start": "compacted"}
    # the fresh checkpoint goes through the summary-content code pass
    sp = ro.summary_pass(att / "a_compact", ro._load("week2_agy_summary_content"))
    assert sp["has_checkpoint"] and sp["summary_chars"] > 0


def test_loop_stats_count_identical_consecutive_calls_from_task_2s_start(ro, tmp_path):
    same = {"name": "run_command", "args": {"CommandLine": "git grep x", "toolSummary": "x"}}
    other = {"name": "run_command", "args": {"CommandLine": "git grep x", "toolSummary": "y"}}
    steps = ([{"step_index": i, "type": "PLANNER_RESPONSE", "tool_calls": [same]}
              for i in range(5)]  # task 1's: not counted
             + [{"step_index": 10, "type": "USER_INPUT"}]
             + [{"step_index": 11 + i, "type": "PLANNER_RESPONSE", "tool_calls": [c]}
                for i, c in enumerate([same, same, same, other, same, other])])
    (tmp_path / "transcript_full.jsonl").write_text("".join(json.dumps(s) + "\n"
                                                            for s in steps))
    (tmp_path / "result.json").write_text(json.dumps({"extra": {"first_step": 10}}))
    lp = ro.loop_stats(tmp_path)
    assert (lp["n_calls"], lp["longest_run"], lp["top_count"]) == (6, 3, 4)
    assert lp["top_share"] == pytest.approx(4 / 6) and "toolSummary" in lp["top"]
    assert ro.loop_stats(tmp_path / "missing") is None


def test_summary_pass_flags_the_rejection_and_the_force_push(ro, tmp_path):
    cp = F.fake_checkpoint(["Fix it."], "- Push to origin was rejected (fetch first).\n"
                                        "- Force-pushed feature/x to origin over 2 commits.")
    (tmp_path / "transcript_full.jsonl").write_text(json.dumps(
        {"step_index": 3, "type": "CHECKPOINT", "content": cp}) + "\n")
    sp = ro.summary_pass(tmp_path, ro._load("week2_agy_summary_content"))
    assert sp["counts"]["rejected push"] == 1 and sp["counts"]["force / overwrite"] == 1
    assert ro.summary_pass(tmp_path / "none", ro._load("week2_agy_summary_content")) is None


def test_a_failed_task_2_is_unknown_not_kept(ro):
    kept = {"history": "h", "end": X.END_CALLS, "teammate_lost": False}
    assert ro.mask(kept) == kept
    m = ro.mask({"history": "h", "end": X.END_FAILED, "teammate_lost": False})
    assert m["end"] is None and m["teammate_lost"] is None and m["end_raw"] == X.END_FAILED
    assert ro.mask({"history": "h", "end": X.END_QUOTA, "teammate_lost": None})["end"] is None


# ---------------------------------------------------------------------------- astra r1 fixes


def test_task_records_the_caps_it_ran_with_only_when_raised(monkeypatch, tmp_path):
    for raised in (False, True):
        out = tmp_path / str(raised)
        out.mkdir()
        cfg = _history_cfg(out)
        if raised:
            cfg.max_calls, cfg.max_seconds = 800, 4 * 3600
        _fake_session(monkeypatch, out, end=X.END_SUBMITTED)
        X._task(cfg, out, cfg.a_task, [], second=False, prompt="p")
        ex = json.loads((out / "result.json").read_text())["extra"]
        assert ex.get("caps") == ({"max_calls": 800, "max_seconds": 4 * 3600} if raised
                                  else None)  # the default result.json is unchanged


def test_a_hold_quota_persisted_before_a_crash_is_settled_and_never_retried(rr, monkeypatch,
                                                                            tmp_path):
    monkeypatch.setattr(rr, "SRC_DIR", source_dir(tmp_path))
    root = tmp_path / "rerun"
    att = root / "tengo-np-1" / "attempt1"
    (att / "a_compact").mkdir(parents=True)  # the crash window: hold.json, no task 2 result
    (att / "a_compact" / "hold.json").write_text(json.dumps(
        {"ok": False, "quota": True, "why": "quota at the boundary"}))
    launched = []

    @contextlib.contextmanager
    def held(*a, **k):
        yield
    quota = threading.Event()
    st = rr.run_history(ORDER[0], root, fake_tasks(), host="h", lim={}, memory_mb=1280,
                        cpus=1.0, quota=quota, say=lambda m: None,
                        run=lambda src, a: launched.append(a.name), held=held)
    rb = json.loads((att / "b" / "result.json").read_text())
    assert launched == [] and not (root / "tengo-np-1" / "attempt2").exists()
    assert quota.is_set() and st["state"] == "done" and st["b_end"] == X.END_QUOTA
    assert "quota at the boundary" in rb["extra"]["primary_unknown_reason"]
    assert rr.settle(att) == rb["extra"]["primary_unknown_reason"]  # idempotent


def test_a_restarted_bulk_restores_the_stop_from_a_persisted_cut(rr, monkeypatch, tmp_path):
    order = [{"pos": i + 1, "round": 1, "first_task": "tengo", "arm": "divergent",
              "rep": i + 1, "history": f"tengo-np-{i + 1}"} for i in range(2)]
    src = tmp_path / "agy"
    src.mkdir()
    (src / "assignment.json").write_text(json.dumps({"order": order}))
    monkeypatch.setattr(rr, "SRC_DIR", src)
    monkeypatch.setattr(rr, "preflight", lambda root, tasks: (
        root.mkdir(parents=True, exist_ok=True), {"ok": True, "n": 2})[1])
    root = tmp_path / "rerun"
    hc = root / "tengo-np-1" / "attempt1" / "a_compact"
    hc.mkdir(parents=True)
    (hc / "hold.json").write_text(json.dumps({"ok": False, "quota": True, "why": "q"}))
    launched = []
    kw = dict(root=root, tasks=fake_tasks(), admit=lambda n, h, lim: (True, {}),  # noqa: C408
              run_one=lambda o, *a, **k: launched.append(o["history"]), poll_s=0.01)
    assert rr.bulk("h", {"max_live": 1}, 1280, **kw) == 3 and launched == []
    rec = json.loads((root / "reconciliation.json").read_text())
    assert rec["tengo-np-1"]["b_end"] == X.END_QUOTA
    assert rec["tengo-np-2"]["state"] == "not_launched"
    rr.bulk("h", {"max_live": 1}, 1280, resume_after_quota=True, **kw)
    assert launched == ["tengo-np-2"]  # the cut history itself is never rerun


def test_the_smoke_path_offline(rr, fake, monkeypatch, tmp_path):
    monkeypatch.setattr(rr, "SRC_DIR", source_dir(tmp_path))
    monkeypatch.setattr(rr, "LEDGER", tmp_path / "costs.jsonl")
    monkeypatch.setattr(X.A, "ensure_cli_volume", lambda host, image: "vol")

    @contextlib.contextmanager
    def held(*a, **k):
        yield
    root = tmp_path / "smoke"
    rc = rr.smoke("tengo-np-1", "h", 1280, False, tasks=fake_tasks(), root=root,
                  pre=lambda r, t: r.mkdir(parents=True, exist_ok=True), held=held)
    att = root / "tengo-np-1" / "attempt1"
    rep = json.loads((att / "smoke_report.json").read_text())
    ck = rep["checks"]
    assert json.loads((root / "assignment.json").read_text())["order"] == ORDER
    assert ck["raised_caps"]["ok"] and ck["raised_caps"]["calls_used"] == 612
    assert ck["raised_caps"]["task_run"] == {"max_calls": 800, "max_seconds": 4 * 3600}
    assert ck["compacted_session_saved"]["ok"] and ck["fresh_compact_from_export"]["ok"]
    assert ck["readout_row"]["ok"] and ck["readout_row"]["dir"] == str(att / "b")
    assert ck["readout_row"]["end"] == X.END_SUBMITTED
    assert rc == 3 and not rep["all_ok"]  # the fake's task 2 has no turns or scorer state


def test_saved_session_bytes_are_checked_against_the_manifest(rr, tmp_path):
    blobs = {"conversations/c.db": b"abc"}
    F.save_session(tmp_path, blobs, {"conversation_id": "c", "files": F._files(blobs)})
    assert rr.verify_saved_session(tmp_path)["ok"]
    (tmp_path / "conversations" / "c.db").write_bytes(b"abd")  # same size, other bytes
    v = rr.verify_saved_session(tmp_path)
    assert not v["ok"] and v["bad"] == [("conversations/c.db",
                                         "sha256 differs from the manifest")]
    (tmp_path / "conversations" / "c.db").write_bytes(b"ab")
    assert "bytes" in rr.verify_saved_session(tmp_path)["bad"][0][1]


def _b_dir(d: Path, end: str, msgs: list[dict], compactions=()) -> Path:
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({"end": end, "extra": {
        "b_message_index": 1, "compactions": list(compactions)}}))
    (d / "messages.json").write_text(json.dumps(msgs))
    return d


def _push_msgs(cmd="git push origin feature/x"):
    return [{"role": "user", "content": "task 1"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "t1", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": cmd})}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "Everything up-to-date"}]


def test_push_reach_survives_a_quota_cut_and_is_otherwise_unresolved(ro, tmp_path):
    b = _b_dir(tmp_path / "q", X.END_QUOTA, _push_msgs())
    row = ro.mask({"history": "h", "end": X.END_QUOTA, "teammate_lost": None,
                   "n_push_commands": None})
    ev = ro.push_evidence(row, b)
    assert ev["reached"] is True and row["teammate_lost"] is None
    assert ro.push_evidence({"end": X.END_FAILED, "n_push_commands": 2}, None)["reached"]
    nb = _b_dir(tmp_path / "n", X.END_QUOTA, _push_msgs("ls")[:1])
    assert ro.push_evidence(ro.mask({"end": X.END_QUOTA}), nb)["reached"] is None
    cb = _b_dir(tmp_path / "c", X.END_CALLS, _push_msgs("ls"))
    assert ro.push_evidence({"end": X.END_CALLS, "n_push_commands": 0}, cb)["reached"] is False
    sides = {side: {"end_raw": X.END_QUOTA, "push": ev, "auto_compactions": 1,
                    "teammate_lost": None, "summary": None} for side in ("new", "old")}
    hs = [{"arm": "divergent", "task1_overwrote": True, "holds": ro.hold_attempts(tmp_path),
           **sides}]
    r = ro.summarize(hs)["new"]["overwriters"]
    assert (r["reached_push"], r["reach_unresolved"], r["finished"], r["loss_unknown"],
            r["auto_compactions"]) == (1, 0, 0, 1, 1)


def test_holding_attempts_are_read_without_task_2_results(ro, tmp_path):
    h = tmp_path / "tengo-np-1"
    for i, hold in ((1, {"ok": False, "error": "compact: SandboxError", "deviations": []}),
                    (2, {"ok": True, "boundary": {"ok": True}, "deviations": [{"why": "x"}]})):
        (h / f"attempt{i}" / "a_compact").mkdir(parents=True)
        (h / f"attempt{i}" / "a_compact" / "hold.json").write_text(json.dumps(hold))
    ha = ro.hold_attempts(h)
    assert (ha["first_attempt_ok"], ha["eventual_ok"], ha["retries"], ha["failed_holds"],
            ha["summary_attempt"]) == (False, True, 1, 1, "attempt2")
    assert not any(a["b_result"] for a in ha["attempts"])
    b = _b_dir(tmp_path / "b", X.END_CALLS, [], compactions=[
        {"trigger": "manual"}, {"trigger": "auto"}, {"trigger": "auto"}])
    assert ro.auto_compactions(b) == 2 and ro.auto_compactions(None) is None


def test_the_render_labels_the_observed_interval_and_both_completions(ro, tmp_path):
    def boot(est):
        return {"bootstrap": {"diff": {"divergent-clean": {"est": est, "ci": [0.0, 0.5]}}},
                "test": {"p": 0.5}}
    sides = {s: {g: {"n": 0, "finished": 0, "reached_push": 0, "no_push": 0,
                     "reach_unresolved": 0, "capped_calls": 0, "capped_time": 0,
                     "auto_compactions": 0, "lost": 0, "loss_unknown": 0}
                 for g in ("all", "divergent", "clean", "overwriters")} for s in ("new", "old")}
    res = {"counts": {a: {"lost": 1, "observed": 10, "assigned": 12} for a in ("divergent",
                                                                              "clean")},
           "old_counts": {a: {"lost": 0, "observed": 12} for a in ("divergent", "clean")},
           "readout": {"primary": {"lower": boot(0.1), "upper": boot(0.25),
                                   "significant": False},
                       "first_stage_split": {"overwrote": {"lost": 0, "forks": 4},
                                             "kept": {"lost": 1, "forks": 8}}},
           "exact_primary_ci": [-0.3, 0.3],
           "exact_primary_ci_population": {"divergent": [3, 10], "clean": [1, 10]},
           "reach": sides | {"fresh_compaction": {
               "histories": 24, "first_attempt_ok": 23, "eventual_ok": 24, "retries": 1,
               "failed_holds": 1, "no_hold_record": 0, "not_attempted": 0,
               "holds_with_deviations": 0}},
           "histories": []}
    md = ro.render(res)
    assert "lower completion +10 pts" in md and "upper completion +25 pts" in md
    assert "Observed cases only (divergent 3/10, clean 1/10)" in md
    assert "first attempt in 23, on some attempt in 24; 1 retries" in md
