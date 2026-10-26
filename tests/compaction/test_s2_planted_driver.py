"""Offline fake-clock/fake-runtime checks; no model, Docker, SSH, or API calls."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from compaction.s2 import planted_driver as D
from compaction.s2.planted_config import task_order


class Clock:
    def __init__(self, now=None):
        self.now = (now or D.RESET).timestamp()
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Runtime:
    def __init__(self, clock, n=3):
        self.clock, self.calls, self.receipts = clock, [], {}
        self.tasks = task_order()[:3]
        self.boundaries = [{"task_id": t, "boundary": f"/corpus/{t}/b1", "live_eligible": True}
                           for t in self.tasks[:n]]
        self.forks_a, self.forks_b, self.spend, self.utilization = 0, 0, 0, .1
        self.screen_pass = True
        self.raise_phase = None
        self.contingency_add = True
        self.advance_screen = 0

    def snapshot(self):
        return {"active_tasks": self.tasks, "boundaries": self.boundaries, "forks_a": self.forks_a,
                    "forks_b": self.forks_b, "forks_total": self.forks_a + self.forks_b,
                    "simulator_usd": self.spend}

    def max_reading(self):
        assert self.clock() >= D.RESET.timestamp()
        self.calls.append(("max_reading", self.clock()))
        return {"observed_at": self.clock(), "rate_limit_info": {"unifiedWindows": {
            "seven_day": {"utilization": self.utilization,
                          "resetsAt": D.RESET.timestamp() + 7 * 86400}}}}

    def recover(self, work_id):
        self.calls.append(("recover", work_id))
        return self.receipts.get(work_id)

    def _run(self, phase, work_id, **kwargs):
        self.before_action({"action": phase})
        self.calls.append((phase, kwargs))
        if phase == self.raise_phase:
            raise RuntimeError("injected unexpected failure")
        result = {"status": "PASS", "reason": "fake offline gate", "passes": self.screen_pass}
        if phase == "screen" and not self.screen_pass:
            result["status"] = "FAIL"
        if phase == "contingency" and self.contingency_add:
            self.boundaries.extend({"task_id": t, "boundary": f"/corpus/{t}/new/b1", "live_eligible": True,
                                    "origin": "planted_base", "k": 1}
                                   for t in kwargs["missing_tasks"])
        if phase == "screen":
            self.clock.now += self.advance_screen
        self.receipts[work_id] = result
        return result

    def step0(self, **kw):
        return self._run("step0", **kw)

    def contingency(self, **kw):
        return self._run("contingency", **kw)

    def screen(self, **kw):
        return self._run("screen", **kw)

    def chains(self, **kw):
        return self._run("chains", **kw)


def driver(tmp_path, runtime, clock, **kwargs):
    return D.Driver(runtime, checkout=tmp_path, expected_hostname="desktop", clock=clock,
        sleeper=clock.sleep, identity_provider=lambda path: {"hostname": "desktop", "checkout": str(path),
                                                               "machine_sha256": "machine"}, **kwargs)


def phases(runtime):
    return [name for name, _ in runtime.calls if name not in {"max_reading", "recover"}]


def test_waits_absolute_reset_without_ping_then_finishes_before_cards(tmp_path):
    clock = Clock(D.RESET - timedelta(seconds=65))
    runtime = Runtime(clock)
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert sum(clock.sleeps) == 65 + D.RESET_SETTLE_SECONDS
    assert max(clock.sleeps) <= 30
    assert phases(runtime) == ["step0", "screen", "chains"]
    assert runtime.calls[0] == ("max_reading", D.RESET.timestamp() + D.RESET_SETTLE_SECONDS)
    assert len(runtime.calls[-1][1]["tasks"]) == 3
    assert "ARMED" in (tmp_path / "logs/s2/planted_run_status.md").read_text()


def test_owner_and_process_lock_are_exclusive(tmp_path):
    clock = Clock()
    instance = driver(tmp_path, Runtime(clock), clock)
    with pytest.raises(D.DriverStop, match="manifest"):
        instance.run(install_signals=False)
    with D.process_lock(instance.directory / "planted_run.lock"), pytest.raises(D.DriverStop, match="another driver"):
        instance.run(claim_owner=True, install_signals=False)
    instance.run(claim_owner=True, install_signals=False)
    owner = json.loads(instance.owner_path.read_text())
    owner["identity"]["checkout"] = "/another"
    D.atomic_json(instance.owner_path, owner)
    with pytest.raises(D.DriverStop, match="another host or checkout"):
        instance.run(install_signals=False)


def test_hold_stops_before_fork_and_does_not_auto_restart(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    runtime.utilization = .90
    instance = driver(tmp_path, runtime, clock)
    state = instance.run(claim_owner=True, install_signals=False)
    assert state["status"] == "HALTED" and "90%" in state["reason"]
    assert phases(runtime) == []
    runtime.utilization = .1
    instance.run(install_signals=False)
    assert phases(runtime) == []


def test_stale_or_missing_budget_fails_closed(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    runtime.max_reading = lambda: {"observed_at": clock() - 301, "rate_limit_info": {}}
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert "fresh Max" in state["reason"] and not phases(runtime)


@pytest.mark.parametrize("metric,value", [("spend", 25), ("forks_a", 60), ("forks_b", 94)])
def test_budget_caps_stop_before_any_action(tmp_path, metric, value):
    clock = Clock()
    runtime = Runtime(clock)
    setattr(runtime, metric, value)
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "HALTED" and "cap" in state["reason"]
    assert phases(runtime) == []


def test_missing_eligibility_contingency_only_missing_then_step0(tmp_path):
    clock = Clock()
    runtime = Runtime(clock, n=1)
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert phases(runtime) == ["step0", "contingency", "step0", "screen", "chains"]
    contingency = next(kwargs for name, kwargs in runtime.calls if name == "contingency")
    assert contingency["missing_tasks"] == runtime.tasks[1:2]
    assert contingency["simulator_cap_usd"] == 6
    assert contingency["deadline"] == D.RESET + timedelta(hours=3, seconds=D.RESET_SETTLE_SECONDS)


def test_step0_unexpected_failure_never_triggers_contingency(tmp_path):
    clock = Clock()
    runtime = Runtime(clock, n=1)
    runtime.raise_phase = "step0"
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "HALTED"
    assert phases(runtime) == ["step0"]
    assert state["pending"]["phase"] == "step0"


def test_failed_screen_stops_for_redesign(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    runtime.screen_pass = False
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "HALTED"
    assert phases(runtime) == ["step0", "screen"]


def test_late_screen_reduces_chains_to_two_tasks(tmp_path):
    clock = Clock(D.MONDAY_NOON - timedelta(minutes=1))
    runtime = Runtime(clock)
    runtime.advance_screen = 61
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert len(runtime.calls[-1][1]["tasks"]) == 2


def test_single_task_waits_noon_and_needs_distinct_boundaries(tmp_path):
    clock = Clock(D.MONDAY_NOON - timedelta(seconds=20))
    runtime = Runtime(clock, n=1)
    runtime.contingency_add = False
    runtime.boundaries.append({**runtime.boundaries[0], "boundary": "/corpus/same-task/b2"})
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS" and state["single_task"] is True
    assert state["expansion_gate"] is False
    assert len(runtime.calls[-1][1]["tasks"]) == 1
    assert clock.sleeps == [20]
    screen = next(kwargs for name, kwargs in runtime.calls if name == "screen")
    assert screen["single_task"] is True and len(screen["boundaries"]) == 2


def test_single_task_missing_second_boundary_halts(tmp_path):
    clock = Clock(D.MONDAY_NOON)
    runtime = Runtime(clock, n=1)
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert "two distinct eligible boundaries" in state["reason"]
    assert "screen" not in phases(runtime)


def test_no_eligible_after_contingency_stops(tmp_path):
    clock = Clock()
    runtime = Runtime(clock, n=0)
    runtime.contingency_add = False
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert "kill 1" in state["reason"]
    assert phases(runtime) == ["contingency"]


def test_crash_recovery_consumes_receipt_without_rerunning_assignment(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    instance.state.update(status="RUNNING", pending={"phase": "step0", "work_id": "old-id",
                                                    "kwargs": {}, "assigned_at": D.utc(clock())})
    instance._save()
    runtime.receipts["old-id"] = {"status": "PASS"}
    state = instance.run(install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert ("recover", "old-id") in runtime.calls
    assert phases(runtime) == ["screen", "chains"]


def test_crash_without_receipt_never_replays(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    instance.state["pending"] = {"phase": "step0", "work_id": "old-id", "kwargs": {}}
    instance._save()
    state = instance.run(install_signals=False)
    assert "never replay" in state["reason"]
    assert phases(runtime) == []


def test_cutoff_and_stop_file_prevent_even_ping(tmp_path):
    clock = Clock(D.B_CUTOFF)
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    state = instance.run(claim_owner=True, install_signals=False)
    assert "cutoff" in state["reason"] and not runtime.calls


def test_stopfile_before_reset_halts(tmp_path):
    clock = Clock(D.RESET - timedelta(hours=1))
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    instance.directory.mkdir(parents=True)
    instance.stop_path.touch()
    state = instance.run(claim_owner=True, install_signals=False)
    assert "stop file" in state["reason"] and not runtime.calls


def test_part_b_runs_between_step0_and_screen(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)

    class B:
        def run_part_b(self, *, work_id, tasks, before_action):
            before_action({"action": "part_b_fork"})
            runtime.calls.append(("part_b", tasks))
            return {"status": "PASS", "reason": "fake strace passed"}

    state = driver(tmp_path, runtime, clock, part_b_factory=lambda passed: B()).run(
        claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert phases(runtime) == ["step0", "part_b", "screen", "chains"]


def test_part_b_dropped_after_tuesday_noon(tmp_path):
    clock = Clock(D.B_DROP_IF_STEP0_AFTER + timedelta(seconds=1))
    runtime = Runtime(clock)
    state = driver(tmp_path, runtime, clock, part_b_factory=lambda passed: pytest.fail("must drop")).run(
        claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert phases(runtime) == ["step0", "screen", "chains"]


def test_one_failed_kind_continues_overnight_with_passed_kind(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    original = runtime.screen

    def screen(**kwargs):
        return {**original(**kwargs), "passes": False, "passed_kinds": ["commit_convention"],
                "failed_kinds": ["verification_output"]}

    runtime.screen = screen
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert state["redesign_deferred"] is True
    assert state["failed_kinds"] == ["verification_output"]
    assert runtime.calls[-1][1]["kinds"] == ["commit_convention"]
    assert "redesign deferred to morning" in (tmp_path / "logs/s2/planted_run_status.md").read_text()


def test_receipt_and_transition_are_saved_atomically(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    instance.state["pending"] = {"phase": "step0", "work_id": "old-id", "kwargs": {}}
    instance._save()
    written = []
    old_save = instance._save

    def save():
        written.append((instance.state["phase"], instance.state["pending"]))
        old_save()

    instance._save = save
    instance._complete({"status": "PASS"})
    assert written == [("cohort", None)]


def test_signal_stops_through_exception_cleanup(tmp_path):
    import signal
    clock = Clock()
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    runtime.step0 = lambda **kw: instance._signal(signal.SIGTERM, None)
    state = instance.run(claim_owner=True, install_signals=False)
    assert state["status"] == "HALTED" and "SIGTERM" in state["reason"]
    assert state["pending"]["phase"] == "step0"


def test_configuration_cannot_change_across_restart(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    driver(tmp_path, runtime, clock, configuration={"root": "original"}).run(
        claim_owner=True, install_signals=False)
    with pytest.raises(D.DriverStop, match="configuration changed"):
        driver(tmp_path, runtime, clock, configuration={"root": "different"}).run(install_signals=False)


def test_cli_builds_lazy_production_contract_and_rejects_remote_host(tmp_path, monkeypatch):
    import importlib.util

    from compaction.s2 import planted_runtime
    path = D.Path(__file__).resolve().parents[2] / "scripts/s2_planted_run.py"
    spec = importlib.util.spec_from_file_location("planted_run_test_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    eligibility = tmp_path / "eligibility.json"
    eligibility.write_text('{"cleanup_stop":false}')
    preparations = tmp_path / "preparations.json"
    preparations.write_text("{}")
    config = {key: str(tmp_path / key) for key in (
        "root", "corpus", "specs_dir", "si_root", "deepswe_root")}
    config.update(eligibility_path=str(eligibility), expected_hostname="desktop",
                  part_b_preparations=str(preparations))
    constructed = []

    class Services:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.budget, self.token = object(), "not-a-real-token"

        def max_reading(self):
            pytest.fail("construction must not ping")

        def task_loader(self, task):
            pytest.fail("construction must not load")

    def part_b(**kwargs):
        constructed.append(kwargs)
        return "part-b"

    monkeypatch.setattr(planted_runtime, "guarded_max_judge", lambda path: "fake-judge")
    instance = module.build_driver(config, runtime_factory=Services, part_b_factory=part_b)
    assert len(constructed) == 1
    assert constructed[0]["host"] == "unix:///var/run/docker.sock"
    assert constructed[0]["go_from_jacob"] is True
    assert instance.part_b_factory(D.RESET) == "part-b"
    assert constructed[-1]["step0_passed_at"] == D.RESET
    assert constructed[-1]["judge"] == "fake-judge"
    with pytest.raises(ValueError, match="local Docker"):
        module.build_driver(config | {"host": "ssh://rig"}, runtime_factory=Services)


@pytest.fixture
def integrated_runtime(tmp_path, monkeypatch):
    """Real Runtime/ForkBudget/scorers and Driver, with native launches/judges faked."""
    from types import SimpleNamespace

    from compaction.s2 import planted_config as PC
    from compaction.s2 import planted_runtime as R
    from compaction.s2 import planted_step0 as P

    clock = Clock()
    monkeypatch.setattr(R, "STOP_FILE", tmp_path / "logs/s2/planted_run.stop")
    rows = []
    for task in task_order():
        spec = {"schema": 2, "eligible": True, "task_id": task, "window": 80000,
            "max_calls": 30, "probe_message": "Please wrap up the work.", "obligations": [
                {"obligation_id": f"{task}:commit", "task_id": task, "kind": "commit_convention",
                 "text": "Every commit starts with [T].", "standing": True,
                 "trigger": {"reply_index": 1, "stage": "02_implement"},
                 "scorer": {"kind": "commit_convention", "prefix": "[T]"}},
                {"obligation_id": f"{task}:verify", "task_id": task, "kind": "verification_output",
                 "text": "Run verify and paste its last line.", "standing": True,
                 "trigger": {"after_compaction": 1, "stage": "02_implement"},
                 "scorer": {"kind": "verification_output", "command": "verify"}}]}
        P.write(tmp_path / "specs" / f"{task}.json", spec)
        bdir = tmp_path / "corpus" / task / "b1"
        P.write(bdir / "boundary/post_messages.json", [{"role": "user", "content": "Ordinary task"}])
        rows.append({"task_id": task, "boundaries": [{"eligible": True, "boundary": str(bdir)}]})
    P.write(tmp_path / "eligibility.json", {"tasks": rows, "cleanup_stop": False})
    observed = []

    def rate():
        return {"observed_at": clock(), "rate_limit_info": {"unifiedWindows": {"seven_day": {
            "utilization": .1, "resetsAt": D.RESET.timestamp() + 7 * 86400}}}}

    budget = PC.ForkBudget(tmp_path / "budget.json")

    def step0(boundary, output, **kwargs):
        cfg = kwargs["native_cfg"]
        assert cfg.max_window_utilization == .9
        assert cfg.deadline_epoch == D.B_CUTOFF.timestamp()
        assert cfg.stop_file == str(R.STOP_FILE)
        observed.append(("step0", kwargs["attempt"]))
        ids = [kwargs["attempt"] + suffix for suffix in (":probe", ":continue")]
        bucket = "step0" if not budget.state()["attempts"] else "step0_extra"
        budget.reserve_batch([{"attempt_id": key, "part": "A", "bucket": bucket} for key in ids])
        for key in ids:
            budget.finish(key, "complete", artifact="fake-native.json")
        return {"status": "PASS", "attempt_ids": ids}

    def fork(boundary, output, **kwargs):
        inserted = bool(kwargs["required_context"])
        observed.append(("fork", output.name))
        P.write(output / "evidence.json", {"part_a_complete": True, "events": [
            {"seq": 0, "kind": "commit", "data": {"message": "[T] done" if inserted else "done"}},
            {"seq": 1, "kind": "command", "data": {"command": "verify", "output": "checked",
                                                       "exit_code": 0, "cwd": "/app"}},
            {"seq": 2, "kind": "ask_user", "data": {"text": "Done checked" if inserted else "Done"}}]})
        return {"fork_check": {"ok": True}, "context_check": {"ok": True}, "cleanup_unconfirmed": [],
                "sim_calls": 0, "sim_cost_usd": 0, "rate_limit": rate(),
                "token_usage": {"input_tokens": 5, "output_tokens": 3}}

    runtime = R.Runtime(root=tmp_path / "out", corpus=tmp_path / "corpus", specs_dir=tmp_path / "specs",
        si_root=tmp_path / "si", deepswe_root=tmp_path / "ds", eligibility_path=tmp_path / "eligibility.json",
        token="fake-token", go_from_jacob=True, reviewed_build=True, clock=clock,
        io={"budget": budget, "cohort": PC.ActiveCohort(tmp_path / "cohort.json"),
            "totals": lambda tag: (0., 0.), "rate_reader": rate, "step0": step0, "fork": fork,
            "task_loader": lambda task, **kw: SimpleNamespace(task_id=task, persona="maintainer"),
            "judge": lambda request: {"classification": "report", "quote": request["input"]["message"]}})
    monkeypatch.setattr(runtime, "_boundary", lambda row, output: D.Path(row["boundary"]))
    monkeypatch.setattr(runtime, "_summary", lambda bdir: ("Task summary", "<summary>Task summary</summary>"))

    def segment(task, work_id, boundary=None, **kwargs):
        runtime.guard("base_continuation" if boundary else "base_run")
        k = int(D.Path(boundary).name[1:]) + 1 if boundary else 1
        row = {"task_id": task, "boundary": str(tmp_path / "planted" / task / f"b{k}"),
               "k": k, "live_eligible": True, "origin": "planted_base"}
        state = runtime._state()
        state["boundaries"].append(row)
        runtime._save(state)
        return {"status": "stop_after", "boundaries": [row]}

    monkeypatch.setattr(runtime, "_base_segment", segment)
    monkeypatch.setattr(runtime, "_retention", lambda row: "fake-boundary-only-retention.json")
    return runtime, clock, observed


def test_actual_runtime_driver_full_sequence_and_sealed_baselines(tmp_path, integrated_runtime):
    runtime, clock, observed = integrated_runtime
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS", state.get("reason")
    assert state["usable_kinds"] == ["commit_convention", "verification_output"]
    assert runtime.snapshot()["forks_a"] == 32  # Step0 2, screen 12, baseline 18
    assert all(row["status"] == "complete" for row in runtime.budget.state()["attempts"])
    assert len(list(runtime.root.glob("baselines/*/sealed-probes.json"))) == 9
    assert sum(name == "fork" for name, _ in observed) == 30
    completed_before = list(observed)
    driver(tmp_path, runtime, clock).run(install_signals=False)
    assert observed == completed_before


def test_actual_runtime_driver_step0_safety_hold_never_retries(tmp_path, integrated_runtime):
    runtime, clock, observed = integrated_runtime
    runtime.io["step0"] = lambda *a, **kw: observed.append(("step0", "held")) or {
        "status": "FAIL", "probe": {"result": {"policy_stop": "max_window_hold"}}}
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "HALTED" and "retry withheld" in state["reason"]
    assert observed == [("step0", "held")]
    assert runtime.recover(state["pending"]["work_id"])["status"] == "HALTED"


def test_actual_runtime_driver_allows_assigned_final_fork_not_new_work(tmp_path, integrated_runtime):
    runtime, clock, _ = integrated_runtime
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    instance.state.update(phase="screen", pending={"work_id": "cap-test", "phase": "screen"})
    # A screen roster may be reserved atomically at the exact total cap. Existing
    # unrelated completed forks do not license another launch; only its assigned ID does.
    rows = [{"attempt_id": f"prior-{i}", "part": "A", "bucket": "baseline", "status": "complete"}
            for i in range(59)]
    rows.append({"attempt_id": "cap-test-last", "part": "A", "bucket": "screen", "status": "assigned"})
    rows += [{"attempt_id": f"b-{i}", "part": "B", "bucket": "main", "status": "complete"}
             for i in range(34)]
    D.atomic_json(runtime.budget.path, {"schema": 1, "attempts": rows, "released": [], "seed": 20261025})
    instance.before_action({"action": "decision_probe", "attempt_id": "cap-test-last"})
    with pytest.raises(D.DriverStop, match="cap"):
        instance.before_action({"action": "base_run"})
    with pytest.raises(D.DriverStop, match="cap"):
        instance.before_action({"action": "decision_probe", "attempt_id": "prior-1"})
    runtime.budget.finish("cap-test-last", "complete", artifact="fake.json")
    with pytest.raises(D.DriverStop, match="cap"):
        instance.before_action({"action": "decision_probe", "attempt_id": "cap-test-last"})


def test_actual_runtime_contingency_deadline_reaches_single_task_branch(tmp_path, integrated_runtime, monkeypatch):
    from compaction.s2 import planted_step0 as P

    runtime, clock, observed = integrated_runtime
    clock.now = D.MONDAY_NOON.timestamp() - 60
    initial = runtime._state()
    first = initial["boundaries"][0]
    second_path = str(D.Path(first["boundary"]).with_name("b2"))
    P.write(D.Path(second_path) / "boundary/post_messages.json", [{"content": "Ordinary task"}])
    initial["boundaries"] = [first, {**first, "boundary": second_path}]
    runtime._save(initial)
    original = runtime._base_segment

    def segment(*args, **kwargs):
        if kwargs.get("contingency"):
            clock.now = kwargs["deadline"] + 1
            return {"status": "time_cap", "boundaries": []}
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "_base_segment", segment)
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS", state.get("reason")
    assert state["single_task"] and state["expansion_gate"] is False
    assert sum(action == "step0" for action, _ in observed) == 1
    assert runtime.snapshot()["forks_a"] == 20  # 2 Step0 + 12 screen + 6 baselines


def test_actual_runtime_cap_guard_accepts_final_reserved_scoring(tmp_path, integrated_runtime):
    runtime, clock, _ = integrated_runtime
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    instance.state.update(phase="part_b", pending={"work_id": "cap-b", "phase": "part_b"})
    rows = [{"attempt_id": f"prior-{i}", "part": "B", "bucket": "main", "status": "complete"}
            for i in range(33)]
    rows.append({"attempt_id": "cap-b-last", "part": "B", "bucket": "main", "status": "assigned"})
    D.atomic_json(runtime.budget.path, {"schema": 1, "attempts": rows, "released": [], "seed": 20261025})
    runtime.before_action = instance.before_action
    runtime.guard("part_b_judge", attempt_id="cap-b-last")
    with pytest.raises(D.DriverStop, match="cap"):
        runtime.guard("part_b_fork", attempt_id="cap-b-unassigned")


def test_recovery_uses_receipt_completion_for_monday_cut(tmp_path):
    clock = Clock(D.MONDAY_NOON + timedelta(hours=1))
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    finished = D.MONDAY_NOON.timestamp() - 10
    instance.state.update(phase="screen", pending={"phase": "screen", "work_id": "old-screen",
        "kwargs": {}, "assigned_at": D.utc(finished - 30)})
    instance._save()
    runtime.receipts["old-screen"] = {"status": "PASS", "passes": True, "ended_at": finished}
    state = instance.run(install_signals=False)
    assert state["status"] == "STOPPED_BEFORE_CARDS"
    assert len(runtime.calls[-1][1]["tasks"]) == 3
    assert state["screen_passed_at"] == D.utc(finished)


def test_wrong_recovery_receipt_cannot_advance_phase(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    instance = driver(tmp_path, runtime, clock)
    instance._owner(True)
    instance._load()
    instance.state["pending"] = {"phase": "step0", "work_id": "old", "kwargs": {}}
    instance._save()
    runtime.receipts["old"] = {"status": "PASS", "work_id": "another"}
    state = instance.run(install_signals=False)
    assert "another assigned operation" in state["reason"]
    assert not phases(runtime)


def test_reset_rollover_lag_retries_before_assigning_any_work(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    actual = runtime.max_reading
    seen = []
    def reading():
        row = actual()
        seen.append(clock())
        if len(seen) == 1:
            row['rate_limit_info']['unifiedWindows']['seven_day'].update(
                utilization=.94, resetsAt=D.RESET.timestamp())
        return row
    runtime.max_reading = reading
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state['status'] == 'STOPPED_BEFORE_CARDS'
    assert seen[0] == D.RESET.timestamp() + D.RESET_SETTLE_SECONDS
    assert seen[1] - seen[0] == D.RATE_RETRY_SECONDS
    assert phases(runtime) == ['step0', 'screen', 'chains']
    assert state['sequence'] == 3 and len(state['completed']) == 3
    assert any('retry 1/' in event['message'] for event in state['events'])


def test_repeated_guard_does_not_duplicate_boundary_evidence_or_status(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    runtime.boundaries[0]['validation'] = {'large': 'x' * 100_000}
    instance = driver(tmp_path, runtime, clock)
    instance._load()
    for _ in range(20):
        instance.before_action({'action': 'fake_guard'})
    assert instance.state['events'] == []
    assert not instance.status_path.exists()
    assert instance.state['last_guard']['action']['action'] == 'fake_guard'
    assert 'validation' not in instance.state['metrics']['boundaries'][0]
    assert instance.state_path.stat().st_size < 10_000


def test_five_hour_utilization_alone_does_not_trigger_weekly_hold():
    now = D.RESET.timestamp() + 300
    reading = {'observed_at': now, 'rate_limit_info': {'unifiedWindows': {
        'seven_day': {'utilization': .1, 'resetsAt': now + 7*86400},
        'five_hour': {'utilization': .95, 'resetsAt': now + 3600}}}}
    assert D.measured_rate(reading, now)['utilization'] == .1
    reading['rate_limit_info']['unifiedWindows']['five_hour']['status'] = 'rejected'
    with pytest.raises(D.RateHold, match='rate-limit'):
        D.measured_rate(reading, now)


def test_post_contingency_step0_uses_new_boundary_only(tmp_path):
    clock = Clock()
    runtime = Runtime(clock, n=1)
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state['status'] == 'STOPPED_BEFORE_CARDS'
    checks = [kwargs for phase, kwargs in runtime.calls if phase == 'step0']
    assert len(checks) == 2
    assert all(row['origin'] == 'planted_base' for row in checks[1]['boundaries'])


def test_five_hour_rejection_waits_for_its_reset_before_assignment(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    actual = runtime.max_reading
    reset = D.RESET.timestamp() + 5 * 3600
    readings = []

    def reading():
        row = actual()
        readings.append(clock())
        row['rate_limit_info']['unifiedWindows']['five_hour'] = {
            'utilization': 1, 'resetsAt': reset, 'status': 'rejected'}
        return row

    runtime.max_reading = reading
    instance = driver(tmp_path, runtime, clock)
    state = instance.run(claim_owner=True, install_signals=False)
    assert state['status'] == 'STOPPED_BEFORE_CARDS'
    assert readings[1] == reset + D.RESET_SETTLE_SECONDS
    assert state['sequence'] == 3 and len(state['completed']) == 3
    assert phases(runtime) == ['step0', 'screen', 'chains']
    assert 'five-hour 1' in instance.status_path.read_text()


def test_five_hour_wait_cannot_cross_absolute_cutoff(tmp_path):
    clock = Clock(D.B_CUTOFF - timedelta(minutes=5))
    runtime = Runtime(clock)
    actual = runtime.max_reading

    def reading():
        row = actual()
        row['rate_limit_info']['unifiedWindows']['five_hour'] = {
            'utilization': 1, 'resetsAt': clock() + 5*3600, 'status': 'rejected'}
        return row

    runtime.max_reading = reading
    state = driver(tmp_path, runtime, clock).run(claim_owner=True, install_signals=False)
    assert state['status'] == 'HALTED' and 'cutoff' in state['reason']
    assert not phases(runtime) and state['sequence'] == 0 and state['pending'] is None


def failed_step0_driver(tmp_path):
    clock = Clock()
    runtime = Runtime(clock)
    runtime.forks_a = 4
    original = runtime.step0

    def fail(**kwargs):
        return {**original(**kwargs), 'status': 'FAIL', 'reason': 'gate failure'}

    runtime.step0 = fail
    instance = driver(tmp_path, runtime, clock)
    state = instance.run(claim_owner=True, install_signals=False)
    assert state['status'] == 'HALTED' and state['sequence'] == 1
    return clock, runtime, instance, original


def test_explicit_step0_retry_preserves_failure_and_uses_fresh_operation(tmp_path):
    clock, runtime, instance, original = failed_step0_driver(tmp_path)
    previous = json.loads(json.dumps(instance.state['completed'][0]))
    runtime.step0 = original
    fresh = driver(tmp_path, runtime, clock)
    state = fresh.run(install_signals=False, retry_step0_from='pilot-0001-step0')
    assert state['status'] == 'STOPPED_BEFORE_CARDS'
    assert state['completed'][0] == previous
    assert state['completed'][1]['work_id'] == 'pilot-0002-step0'
    assert state['metrics']['forks_total'] == 4  # retry permission never resets the ledger
    assert state['authorized_step0_retries'][0]['from_work_id'] == 'pilot-0001-step0'
    assert state['authorized_step0_retries'][0]['consumed_by_work_id'] == 'pilot-0002-step0'


def test_second_step0_failure_halts_and_same_retry_command_cannot_loop(tmp_path):
    clock, runtime, _, _ = failed_step0_driver(tmp_path)
    fresh = driver(tmp_path, runtime, clock)
    state = fresh.run(install_signals=False, retry_step0_from='pilot-0001-step0')
    assert state['status'] == 'HALTED' and state['sequence'] == 2
    assert [x['work_id'] for x in state['completed']] == ['pilot-0001-step0', 'pilot-0002-step0']
    before = fresh.state_path.read_bytes()
    with pytest.raises(D.DriverStop, match='exact terminal'):
        driver(tmp_path, runtime, clock).run(install_signals=False, retry_step0_from='pilot-0001-step0')
    assert fresh.state_path.read_bytes() == before
    with pytest.raises(D.DriverStop, match='exact terminal'):
        driver(tmp_path, runtime, clock).run(install_signals=False, retry_step0_from='pilot-0002-step0')
    assert fresh.state_path.read_bytes() == before
    calls = len(runtime.calls)
    assert driver(tmp_path, runtime, clock).run(install_signals=False)['status'] == 'HALTED'
    assert len(runtime.calls) == calls


@pytest.mark.parametrize('change', ['pending', 'wrong_work', 'policy_stop', 'wrong_phase'])
def test_step0_retry_rejects_ambiguous_or_unsafe_prior_stop(tmp_path, change):
    clock, runtime, instance, _ = failed_step0_driver(tmp_path)
    if change == 'pending':
        instance.state['pending'] = {'work_id': 'incomplete'}
    elif change == 'wrong_work':
        instance.state['completed'][-1]['work_id'] = 'different-work'
    elif change == 'policy_stop':
        instance.state['reason'] = 'operator stop file'
    else:
        instance.state['phase'] = 'chains'
    instance._save()
    before = instance.state_path.read_bytes()
    with pytest.raises(D.DriverStop, match='exact terminal'):
        driver(tmp_path, runtime, clock).run(install_signals=False, retry_step0_from='pilot-0001-step0')
    assert instance.state_path.read_bytes() == before
