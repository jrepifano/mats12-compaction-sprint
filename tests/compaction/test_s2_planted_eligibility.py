"""Docker-only eligibility uses injected sessions/inspectors; no Docker or models."""
import json
from types import SimpleNamespace

import pytest

from compaction.s2 import planted_eligibility as E
from compaction.s2 import planted_step0 as P
from compaction.s2.native import claude_code as CC

IMAGE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def boundary(tmp_path):
    bdir = tmp_path / "original/task/b1"
    rows = [{"uuid": "u", "type": "user", "parentUuid": None,
             "message": {"role": "user", "content": "Task"}},
            {"uuid": "compact", "type": "system", "subtype": "compact_boundary",
             "parentUuid": "u", "logicalParentUuid": "u", "compactMetadata": {}},
            {"uuid": "summary", "type": "user", "isCompactSummary": True,
             "parentUuid": "compact", "message": {"role": "user", "content": "Task summary"}}]
    metadata = {"quiescent": True, "tag": "kept:task-b1", "stage": "02_implement",
                "native_summary_uuid": "summary", "fingerprint_sha256": "fp", "sim_probe_sha256": "sim",
                "boundary_index": 1, "services_running": [], "boundary_in_session": 1, "model": CC.MODEL}
    P.write(bdir / "boundary/boundary.json", metadata)
    sim = E.I.UserSim("maintainer", CC.NoSimulatorModel(), lambda *a: None)
    P.write(bdir / "boundary/sim_state.json", sim.export())
    P.write(bdir.parent / "run.json", {"task": {"task_id": "task"}})
    P.write(bdir / "native/hook_payload.json", {"compact_summary": "<summary>Task summary</summary>"})
    (bdir / "native/transcript.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    converted = CC.transcript_boundary(rows, 1, model=CC.MODEL, step_instruction="")
    for name, data in (("pre_messages", converted.nb.pre_messages), ("post_messages", converted.nb.post_messages),
                       ("compaction", {"summary": converted.nb.summary})):
        P.write(bdir / f"boundary/{name}.json", data)
    return bdir


def fake_session(log, *, cleanup=(), services=()):
    class FakeSession:
        def __init__(self, arm, image, factory, **kwargs):
            log.append(("construct", image, kwargs))
            assert image == IMAGE and kwargs["setup_git"] is False
            assert kwargs["cpus"] == 2 and kwargs["memory_mb"] == 3072
            assert kwargs["min_free_gb"] > 0 and kwargs["allow"] == ()
            self.factory = factory
            self.cleanup_unconfirmed = []
            self.services = services
            self.sb = SimpleNamespace(name="fake-container", exec=lambda *a, **k: SimpleNamespace(
                rc=0, stdout="/usr/bin/python3\n/usr/bin/curl\n/usr/bin/git\n", stderr=""))

        def __enter__(self):
            sim = self.factory(lambda *a: None)
            self.server = SimpleNamespace(sim=sim, rows=[])
            return self

        def __exit__(self, *exc):
            log.append(("cleanup",))
            self.cleanup_unconfirmed = list(cleanup)
    return FakeSession


def validate(tmp_path, *, checker=None, image_inspector=None, cleanup=(), services=(), go=True):
    bdir = boundary(tmp_path)
    original = {str(p.relative_to(bdir)): p.read_bytes() for p in bdir.rglob("*") if p.is_file()}
    log = []
    result = E.validate_boundary(bdir, tmp_path / "out", task=SimpleNamespace(task_id="task", persona="maintainer"),
        go_from_jacob=go, session_factory=fake_session(log, cleanup=cleanup, services=services),
        checker=checker or (lambda ns, b: {"ok": True, "fingerprint_ok": True, "sim_probe_ok": True}),
        image_inspector=image_inspector or (lambda image, host, container=None: {
            "tag": image, "image_id": IMAGE, "container_image_id": IMAGE if container else None}),
        resources=lambda host: {"scope": "fake"})
    assert {str(p.relative_to(bdir)): p.read_bytes() for p in bdir.rglob("*") if p.is_file()} == original
    return result, log


def test_go_gate_prevents_any_live_action_or_output(tmp_path):
    with pytest.raises(PermissionError):
        validate(tmp_path, go=False)
    assert not (tmp_path / "out").exists()


def test_restored_checks_identity_cleanup_and_original_immutability(tmp_path):
    result, log = validate(tmp_path)
    assert result["eligible"] and result["checks"]["zero_simulator"]
    assert result["model_calls"] == result["model_fork_budget_used"] == 0
    assert result["image_restored"]["container_image_id"] == IMAGE
    assert log[-1] == ("cleanup",)
    assert not list(tmp_path.rglob("fork_budget.json"))


def test_bad_restore_is_ineligible_even_when_container_healthy(tmp_path):
    result, log = validate(tmp_path, checker=lambda ns, b: {"ok": False})
    assert not result["eligible"] and result["checks"]["restore"] is False
    assert log[-1] == ("cleanup",)


def test_image_mismatch_throws_inside_context_and_still_cleans(tmp_path):
    result, log = validate(tmp_path, image_inspector=lambda image, host, container=None: {
        "image_id": IMAGE, "container_image_id": OTHER if container else None})
    assert not result["eligible"] and "identity" in result["error"]
    assert log[-1] == ("cleanup",)


def test_cleanup_failures_and_unexpected_services_never_pass(tmp_path):
    result, _ = validate(tmp_path, cleanup=["container still present"])
    assert not result["eligible"] and result["cleanup_unconfirmed"]
    result, _ = validate(tmp_path / "other", services={123})
    assert not result["eligible"] and result["checks"]["no_services"] is False


def test_model_guard_is_restored_and_calls_cannot_escape(tmp_path):
    def checker(ns, metadata):
        with pytest.raises(RuntimeError, match="forbidden"):
            ns.server.sim.model.complete()
        return {"ok": True}
    result, _ = validate(tmp_path, checker=checker)
    assert result["eligible"]


def test_cohort_counts_distinct_selected_tasks_and_retains_failed_tasks(tmp_path):
    census = {"selected_tasks": ["one", "two", "three"], "tasks": [
        {"task_id": task, "boundaries": [{"boundary": f"/{task}/b1", "offline_eligible": True}]}
        for task in ("one", "two", "three")]}
    calls = []
    def validator(bdir, out, **kwargs):
        calls.append(kwargs["task"].task_id)
        assert kwargs["go_from_jacob"] is True
        return {"eligible": kwargs["task"].task_id != "three", "cleanup_unconfirmed": []}
    result = E.validate_cohort(census, tmp_path / "cohort", task_loader=lambda task: SimpleNamespace(task_id=task),
                               go_from_jacob=True, validator=validator)
    assert calls == ["one", "two", "three"]
    assert result["eligible_selected_tasks"] == 2 and result["at_least_two_live_eligible"]
    assert result["decision"] == "existing_boundaries" and len(result["tasks"]) == 3


def test_local_resource_guard_refuses_oversubscription_without_starting_container(monkeypatch):
    def docker(args, **kwargs):
        assert args[0] in ("ps", "inspect")
        if args[0] == "ps":
            return SimpleNamespace(stdout=b"one\ntwo\n")
        return SimpleNamespace(stdout=json.dumps([
            {"HostConfig": {"Memory": 1024**3, "NanoCpus": 10**9}},
            {"HostConfig": {"Memory": 1024**3, "NanoCpus": 10**9}}]).encode())
    monkeypatch.setattr(E.SB, "_docker", docker)
    with pytest.raises(ValueError, match="3 containers"):
        E.resource_guard(None)


def test_cohort_persists_missing_tasks_load_failures_and_cleanup_stop(tmp_path):
    census = {"selected_tasks": ["missing", "load-fails", "bad-cleanup", "not-started"], "tasks": [
        {"task_id": task, "boundaries": [{"boundary": f"/{task}/b1", "offline_eligible": True}]}
        for task in ("load-fails", "bad-cleanup", "not-started")]}
    calls = []
    def loader(task):
        if task == "load-fails":
            raise ValueError("fixture unavailable")
        return SimpleNamespace(task_id=task)
    def validator(bdir, out, **kwargs):
        calls.append(kwargs["task"].task_id)
        return {"eligible": False, "cleanup_unconfirmed": ["still running"]}
    result = E.validate_cohort(census, tmp_path / "cohort", task_loader=loader,
                               go_from_jacob=True, validator=validator)
    assert calls == ["bad-cleanup"] and result["cleanup_stop"]
    assert len(result["tasks"]) == 4 and result["tasks"][-1]["not_attempted"] == "cleanup_stop"
    assert all("error" in row for row in result["tasks"][:2])
    assert json.loads((tmp_path / "cohort/eligibility.json").read_text()) == result


def test_missing_audit_dependency_keeps_boundary_ineligible(tmp_path, monkeypatch):
    def dependency(sb):
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "missing python3"}
    monkeypatch.setattr(E, "audit_dependencies", dependency)
    result, log = validate(tmp_path)
    assert result["fork_check"]["ok"] and not result["eligible"]
    assert not result["checks"]["audit_dependencies"]
    assert result["model_calls"] == result["model_fork_budget_used"] == 0
    assert log[-1] == ("cleanup",)


def test_dependency_check_records_exact_readonly_command():
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(rc=1, stdout="/usr/bin/python3\n", stderr="fixture missing curl")
    result = E.audit_dependencies(SimpleNamespace(exec=execute))
    assert not result["ok"] and result["exit_code"] == 1
    assert result["stderr"] == "fixture missing curl"
    assert calls == [("command -v python3 && command -v curl && command -v git",
                      {"timeout_s": 30, "workdir": "/"})]
