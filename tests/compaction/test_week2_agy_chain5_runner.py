"""The five-task chain runner's gates (scripts/week2_agy_chain5.py): the smoke verdict the bulk
needs, the memory rule, and a run root bound to its config and code (astra code r1 #3, #6)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("week2_agy_chain5",
                                               ROOT / "scripts" / "week2_agy_chain5.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def _phase(k: int, **kw) -> dict:
    return {"end": "submitted", "setup": "junk" if k == 1 else "teammate", "ticket_tag": "T-1",
            "grade_tip": 1.0, "memory_peak_mb": 800, **kw}


def _checks(**kw) -> dict:
    return {"p1": _phase(1), "p2": _phase(2), "same_conversation": True, "boundaries_ok": 1,
            "end_ok": True, "containers_left": [], **kw}


def test_a_clean_smoke_passes_with_the_default_memory():
    v = R.smoke_verdict(_checks(), 2, "agy")
    assert v["ok"] and v["failures"] == [] and v["bulk_memory"] == "default"


@pytest.mark.parametrize("change, why", [
    ({"p2": _phase(2, end="failed")}, "p2: ended failed"),
    ({"p2": _phase(2, setup="junk")}, "p2: setup junk"),
    ({"p1": _phase(1, ticket_tag=None)}, "p1: no ticket tag"),
    ({"p2": _phase(2, grade_tip=None)}, "p2: no grade"),
    ({"p2": "missing"}, "p2: no result"),
    ({"same_conversation": False}, "the conversation changed"),
    ({"boundaries_ok": 0}, "boundaries ok 0 of 1"),
    ({"end_ok": False}, "the end compaction did not succeed"),
    ({"containers_left": ["native-agy-chain-x"]}, "containers left"),
])
def test_each_failed_check_fails_the_smoke(change, why):
    v = R.smoke_verdict(_checks(**change), 2, "agy")
    assert not v["ok"] and any(why in f for f in v["failures"])


def test_a_grade_of_zero_is_a_grade():
    assert R.smoke_verdict(_checks(p2=_phase(2, grade_tip=0.0)), 2, "agy")["ok"]


def test_a_high_or_missing_memory_peak_selects_the_fallback_memory():
    hi = R.smoke_verdict(_checks(p2=_phase(2, memory_peak_mb=1500)), 2, "agy")
    assert hi["ok"] and hi["bulk_memory"] == "fallback" and hi["memory_peak_mb"] == 1500
    one = R.smoke_verdict(_checks(p2=_phase(2, memory_peak_mb=None)), 2, "agy")
    assert one["ok"] and one["bulk_memory"] == "fallback" and one["memory_peaks_mb"] == [800, None]


def test_unconfirmed_grader_cleanup_fails_the_smoke_and_stops_launches(monkeypatch):
    from compaction.s2.chain import driver as D
    monkeypatch.setattr(D, "UNRESOLVED", ["run-1"])
    assert R.unresolved() == ["grader run-1"]
    checks = _checks(containers_left=R.unresolved())
    assert not R.smoke_verdict(checks, 2, "agy")["ok"]


def test_an_unreadable_inventory_is_not_empty(monkeypatch):
    import subprocess

    class P:
        returncode, stdout = 1, ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    assert R.our_containers("agy") == ["docker ps failed rc=1"]


def test_the_bulk_refuses_a_missing_failed_or_stale_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "roots", lambda h: (tmp_path / "bulk", tmp_path / "smoke"))
    prov = {"config": {"path": "c", "sha256": "a"}, "code": {"f": "1"}}
    monkeypatch.setattr(R, "provenance", lambda config=R.CONFIG: prov)
    with pytest.raises(SystemExit, match="no smoke verdict"):
        R.passing_smoke("agy")
    (tmp_path / "smoke").mkdir()
    p = tmp_path / "smoke" / "smoke_verdict.json"
    p.write_text(json.dumps({"ok": False, "failures": ["x"], "provenance": prov}))
    with pytest.raises(SystemExit, match="the smoke failed"):
        R.passing_smoke("agy")
    p.write_text(json.dumps({"ok": True, "provenance": {**prov, "code": {"f": "2"}}}))
    with pytest.raises(SystemExit, match="other code"):
        R.passing_smoke("agy")
    p.write_text(json.dumps({"ok": True, "provenance": prov, "bulk_memory": "default"}))
    assert R.passing_smoke("agy")["ok"]


def test_a_run_root_is_bound_to_its_config_and_code(tmp_path):
    from compaction.s2.native import agy_nchain as AN
    st = AN.StateFile.load(tmp_path)
    prov = {"config": {"path": "c", "sha256": "a"}, "code": {"f": "1"}}
    R.bind_root(st, prov, {"max_live": 4})
    R.bind_root(st, prov, {"max_live": 2})
    again = json.loads((tmp_path / "state.json").read_text())
    assert again["provenance"] == prov and [c["max_live"] for c in again["capacity"]] == [4, 2]
    with pytest.raises(SystemExit, match="another config or other code"):
        R.bind_root(AN.StateFile.load(tmp_path), {**prov, "code": {"f": "2"}}, {})


def test_expect_comes_from_the_config_table():
    cfg, _ = R.load()
    assert R.expect(cfg, "agy") == {"model": "gemini-3.8-flash-low", "cli_version": "1.4.7",
                                    "compaction_retries": 1}


def test_persisted_cleanup_records_are_rechecked_against_docker(monkeypatch):
    import subprocess
    calls = []

    class P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def fake(argv, **k):
        calls.append(argv)
        return P(0, "abc\n") if "emx.run=run-1" in " ".join(argv) else P(0, "")
    monkeypatch.setattr(subprocess, "run", fake)
    assert R.still_present(["grader run-1", "grader run-2", "native-agy-chain-x"]) == [
        "grader run-1"]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P(1, ""))
    assert R.still_present(["grader run-3"]) == ["grader run-3"]  # unreadable: kept


def test_a_graders_unconfirmed_cleanup_holds_the_chains(monkeypatch, tmp_path):
    import contextlib

    from compaction.s2.chain import driver as D
    from compaction.s2.native import agy_nchain as AN
    monkeypatch.setattr(AN, "UNRESOLVED_CLEANUP", [])
    monkeypatch.setattr(D, "UNRESOLVED", [])
    monkeypatch.setattr(R, "grader_slot", lambda owner: contextlib.nullcontext())
    monkeypatch.setattr(R._hist_module(), "resources_ok", lambda h, lim, reading=None: (True, {}))

    def grade(out, task, status):
        D.UNRESOLVED.append("run-9")
        return {"tip": {"reward": 1.0}}
    monkeypatch.setattr(D, "grade_run", grade)
    assert R.grade_one(tmp_path, object(), {})["tip"]["reward"] == 1.0
    assert AN.UNRESOLVED_CLEANUP == ["grader run-9"] and R.unresolved() == ["grader run-9"]


def test_a_smoke_carries_over_only_across_runner_changes_with_a_reason(tmp_path, monkeypatch):
    now = {"config": {"path": "c", "sha256": "a"},
           "code": {"scripts/week2_agy_chain5.py": "2", "src/x.py": "1"}}
    monkeypatch.setattr(R, "provenance", lambda config=R.CONFIG: now)
    monkeypatch.setattr(R, "ROOT", tmp_path)
    (tmp_path / "logs" / "s2").mkdir(parents=True)
    p = tmp_path / "v.json"
    old = {**now, "code": {**now["code"], "scripts/week2_agy_chain5.py": "1"}}
    p.write_text(json.dumps({"ok": True, "provenance": old}))
    with pytest.raises(SystemExit, match="needs --accept-reason"):
        R.passing_smoke("agy", accept_from=str(p))
    v = R.passing_smoke("agy", accept_from=str(p), reason="r3 grader fixes")
    assert v["carried_over"]["files"] == ["scripts/week2_agy_chain5.py"]
    assert "r3 grader fixes" in (tmp_path / "logs" / "s2" / "week2_build_log.md").read_text()
    p.write_text(json.dumps({"ok": True, "provenance": {**old, "code": {
        **old["code"], "src/x.py": "0"}}}))
    with pytest.raises(SystemExit, match="code outside the runner changed"):
        R.passing_smoke("agy", accept_from=str(p), reason="x")


def test_smoke_checks_count_an_encrypted_end_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "our_containers", lambda h: [])
    c = tmp_path / "chains" / "u1"
    c.mkdir(parents=True)
    rows = [{"kind": "boundary", "ok": True, "empty": True, "encrypted": True},
            {"kind": "end", "ok": True, "empty": True, "encrypted": True}]
    (c / "compactions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = R.smoke_checks(tmp_path, "u1", 2, "codex")
    assert out["boundaries_ok"] == 1 and out["end_ok"] is True
