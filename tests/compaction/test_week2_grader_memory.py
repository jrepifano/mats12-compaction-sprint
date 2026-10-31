"""Experiment 4's reduced grader limit (parallelism.grader_memory_mb): admission reserves it, a
grade OOM-killed there (or with no OOM count) is regraded once at the full limit, both attempts
recorded; unset, grading is unchanged (experiment 1)."""

from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import week2 as W


@pytest.fixture
def grader_settings(monkeypatch, tmp_path):
    """Restores the module settings set_grader_memory changes; status lines kept out of the
    repo."""
    monkeypatch.setattr(D, "STATUS_FILE", tmp_path / "status.md")
    monkeypatch.setattr(CC, "GRADE_MEMORY_MB", D.GRADER_FULL_MB)  # from a known baseline
    monkeypatch.setitem(D.EXPECTED_MB, "grader", D.GRADER_FULL_MB)
    monkeypatch.setattr(CC, "AGENT_MEMORY_MB", CC.AGENT_MEMORY_MB)
    monkeypatch.setitem(D.EXPECTED_MB, "agent", D.EXPECTED_MB["agent"])
    monkeypatch.setattr(D, "GRADER_OOM_REGRADE", False)
    monkeypatch.setattr(D, "ADMISSION", D.Admission(mem=lambda: None, poll_s=0,
                                                    others=lambda: 0))
    monkeypatch.setattr(D, "cleanup", lambda inv: [])


def _grading(monkeypatch, results):
    """grade_patch faked: each call pops the next result, recording (memory_mb, read_ooms) and
    what admission reserved for it."""
    calls = []

    def grade_patch(inv, task, patch, timeout_s=1800, memory_mb=None, read_ooms=False):
        calls.append({"memory_mb": memory_mb, "read_ooms": read_ooms,
                      "reserved": sorted(D.ADMISSION.live.values())})
        r = dict(results.pop(0))
        if read_ooms:
            r.setdefault("memory_mb", memory_mb or CC.GRADE_MEMORY_MB)
        return r
    monkeypatch.setattr(D, "grade_patch", grade_patch)
    return calls


def _out(tmp_path):
    (tmp_path / "tip.patch").write_bytes(b"diff")
    return tmp_path


OK = {"reward": 1.0, "grade_error": None}


def test_unset_grading_is_unchanged(tmp_path, monkeypatch, grader_settings):
    calls = _grading(monkeypatch, [OK])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None)
    assert g["tip"] == OK and "attempts" not in g["tip"]
    assert calls == [{"memory_mb": None, "read_ooms": False, "reserved": [8192]}]


def test_reduced_limit_reserves_it_and_records_the_attempt(tmp_path, monkeypatch,
                                                          grader_settings):
    D.set_grader_memory(4096)
    assert D.GRADER_OOM_REGRADE and D.EXPECTED_MB["grader"] == 4096 and CC.GRADE_MEMORY_MB == 4096
    calls = _grading(monkeypatch, [{**OK, "oom_kills": 0}])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None)
    assert calls == [{"memory_mb": None, "read_ooms": True, "reserved": [4096]}]
    assert g["tip"]["reward"] == 1.0
    assert g["tip"]["attempts"] == [{"memory_mb": 4096, "oom_kills": 0, "reward": 1.0,
                                     "grade_error": None}]


@pytest.mark.parametrize("ooms", [1, None])
def test_oom_or_unknown_is_regraded_once_at_the_full_limit(tmp_path, monkeypatch,
                                                           grader_settings, ooms):
    D.set_grader_memory(4096)
    killed = {"reward": 0.0, "grade_error": None, "oom_kills": ooms}  # a kill can fail tests
    calls = _grading(monkeypatch, [killed, {**OK, "oom_kills": 0}])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None)
    assert [c["memory_mb"] for c in calls] == [None, D.GRADER_FULL_MB]
    assert calls[1]["reserved"] == [D.GRADER_FULL_MB]  # the regrade reserves the full limit
    assert g["tip"]["reward"] == 1.0
    assert [(a["memory_mb"], a["oom_kills"], a["reward"]) for a in g["tip"]["attempts"]] == \
        [(4096, ooms, 0.0), (D.GRADER_FULL_MB, 0, 1.0)]
    assert json.loads((tmp_path / "grade.json").read_text())["tip"]["attempts"] == \
        g["tip"]["attempts"]


def test_regrade_happens_once_even_across_infrastructure_retries(tmp_path, monkeypatch,
                                                                 grader_settings):
    D.set_grader_memory(4096)
    calls = _grading(monkeypatch, [{"reward": None, "grade_error": "x", "oom_kills": 2},
                                   {"reward": None, "grade_error": "infra", "oom_kills": 0},
                                   {"reward": None, "grade_error": "y", "oom_kills": 1}])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None, tries=2)
    assert [c["memory_mb"] for c in calls] == [None, D.GRADER_FULL_MB, None]
    assert g["tip"]["reward"] is None and len(g["tip"]["attempts"]) == 3


def test_an_infrastructure_exception_is_not_an_oom(tmp_path, monkeypatch, grader_settings):
    D.set_grader_memory(4096)
    calls = _grading(monkeypatch, [{"reward": None, "grade_error": "SandboxError: x"}, OK])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None, tries=2)
    assert [c["memory_mb"] for c in calls] == [None, None]  # retried, not regraded at 8 GB
    assert g["tip"]["reward"] == 1.0


def test_admission_reserves_the_given_size_and_counts_it(grader_settings):
    a = D.Admission(mem=lambda: None, poll_s=0, others=lambda: 0, budget_mb=20 * 1024)
    D.set_agent_memory(1024)
    D.set_grader_memory(4096)
    with contextlib.ExitStack() as stack:
        for _ in range(16):
            stack.enter_context(a.slot("agent"))
        assert a.why_not("grader") is None  # 16 x 1,024 + 4,096 = 20,480
        assert a.why_not("grader", D.GRADER_FULL_MB) == "budget"  # the 8 GB regrade waits
        stack.enter_context(a.grader())
        assert a.why_not("agent") == "budget"  # with the grader in, the budget is full


def _cli():
    spec = importlib.util.spec_from_file_location("s2_chain", CC.REPO_ROOT / "scripts" /
                                                  "s2_chain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gate_name_carries_a_reduced_grader_limit():
    cli = _cli()
    assert cli.admission_gate(16, 1280) == "admission_check_16x1280"
    assert cli.admission_gate(16, 1280, D.GRADER_FULL_MB) == "admission_check_16x1280"
    assert cli.admission_gate(16, 1024, 4096) == "admission_check_16x1024_grader4096"
    cfg = {"parallelism": {"agents": 16, "agent_memory_mb": 1024, "fallback_agents": 6,
                           "fallback_agent_memory_mb": 3072, "grader_memory_mb": 4096}}
    st = {"gates": {"admission_check_16x1024": {"pass": True, "load_ok": True}}}
    with pytest.raises(SystemExit):  # a check without the grader limit doesn't count
        cli.admitted(st, cfg)
    st["gates"]["admission_check_16x1024_grader4096"] = {"pass": True, "load_ok": True}
    assert cli.admitted(st, cfg) == (16, 1024)


def test_schema_key_is_optional_and_typed():
    cfg = W.load_config("configs/week2/monitors.toml")  # no grader key: still valid
    cfg.pop("_source")
    W.validate(cfg)
    cfg["parallelism"]["grader_memory_mb"] = "4096"
    with pytest.raises(ValueError, match="parallelism.grader_memory_mb"):
        W.validate(cfg)


def test_reaching_the_full_limit_turns_the_regrade_off(grader_settings):
    D.set_grader_memory(D.GRADER_FULL_MB)
    assert not D.GRADER_OOM_REGRADE


def _raising(monkeypatch, script):
    """grade_patch faked from a script of results or exceptions (raised), recording calls."""
    calls = []

    def grade_patch(inv, task, patch, timeout_s=1800, memory_mb=None, read_ooms=False):
        calls.append(memory_mb)
        r = script.pop(0)
        if isinstance(r, Exception):
            raise r
        return {**r, "memory_mb": memory_mb or CC.GRADE_MEMORY_MB} if read_ooms else dict(r)
    monkeypatch.setattr(D, "grade_patch", grade_patch)
    return calls


def test_a_failed_full_regrade_is_not_repeated(tmp_path, monkeypatch, grader_settings):
    from compaction.deepswe import sandbox as SBX
    D.set_grader_memory(4096)
    calls = _raising(monkeypatch, [{"reward": 0, "grade_error": None, "oom_kills": 1},
                                   SBX.SandboxError("docker gone"),
                                   {"reward": 0, "grade_error": None, "oom_kills": 1}])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None, tries=2)
    assert calls == [None, D.GRADER_FULL_MB, None]
    assert g["tip"]["reward"] is None and "OOM-killed at 4096" in g["tip"]["grade_error"]
    att = g["tip"]["attempts"]
    assert [(a["memory_mb"], a.get("oom_kills", "absent")) for a in att] == \
        [(4096, 1), (D.GRADER_FULL_MB, "absent"), (4096, 1)]
    assert "SandboxError" in att[1]["grade_error"]  # an exception, recorded with its limit


def test_a_reward_from_an_oom_killed_full_attempt_is_not_final(tmp_path, monkeypatch,
                                                                grader_settings):
    D.set_grader_memory(4096)
    _raising(monkeypatch, [{"reward": 0, "grade_error": None, "oom_kills": 1},
                           {"reward": 0, "grade_error": None, "oom_kills": 2},
                           {"reward": 1, "grade_error": None, "oom_kills": 0}])
    g = D.grade_run(_out(tmp_path), SimpleNamespace(), None, tries=2)
    # the killed full attempt isn't a reward 0; the next try's clean reduced attempt is
    assert g["tip"]["reward"] == 1
    assert [a["reward"] for a in g["tip"]["attempts"]] == [0, 0, 1]  # raw rewards kept


def test_a_stored_reward_is_not_regraded(tmp_path, monkeypatch, grader_settings):
    D.set_grader_memory(4096)
    out = _out(tmp_path)
    (out / "grade.json").write_text(json.dumps({"tip": {"reward": 1}}))
    calls = _raising(monkeypatch, [])
    assert D.grade_run(out, SimpleNamespace(), None)["tip"] == {"reward": 1} and calls == []


class _Box:
    """A fake grading container: test.sh, the reward read and the OOM read; exec refuses after
    the container is closed."""

    def __init__(self, reward_out, oom, timed_out=False):
        self.reward_out, self.oom, self.timed_out, self.closed = reward_out, oom, timed_out, False
        self.limits = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def copy_in(self, *a):
        pass

    def put_file(self, *a):
        pass

    def exec(self, cmd, timeout_s=None, workdir=None):
        assert not self.closed, "read after the container was removed"
        if cmd == D.PEAK_CMD:
            return SimpleNamespace(rc=0, stdout=f"{400 * 2**20}\n", stderr="")
        if cmd == D.OOM_CMD:
            return SimpleNamespace(rc=0 if self.oom is not None else 1,
                                   stdout=f"{self.oom}\n" if self.oom is not None else "",
                                   stderr="")
        if "reward" in cmd:
            return SimpleNamespace(rc=0, stdout=self.reward_out, stderr="")
        return SimpleNamespace(rc=0, stdout="", stderr="", timed_out=self.timed_out,
                               seconds=5.0)


REWARD1 = "json\n" + json.dumps({"reward": 1, "f2p_passed": 1, "f2p_total": 1,
                                 "p2p_passed": 2, "p2p_total": 2})


@pytest.mark.parametrize("oom,timed_out,reward_out,want", [
    (0, False, REWARD1, (1, None)), (3, False, REWARD1, (1, None)),
    (None, False, REWARD1, (1, None)), (2, True, REWARD1, (None, "timed out")),
    (1, False, "none\n", (None, "no reward file"))])
def test_real_grade_patch_reads_the_oom_count_before_removal(oom, timed_out, reward_out, want):
    boxes = []

    class Inv:
        def start(self, image, **kw):
            boxes.append(_Box(reward_out, oom, timed_out))
            boxes[-1].limits = kw
            return boxes[-1]
    task = SimpleNamespace(image="img", tests_dir=".")
    r = D.grade_patch(Inv(), task, b"diff", memory_mb=4096, read_ooms=True)
    box = boxes[-1]
    assert box.limits["memory_mb"] == 4096 and box.closed
    assert r["memory_mb"] == 4096 and r["oom_kills"] == oom and r["memory_peak_mb"] == 400.0
    assert r["reward"] == want[0]
    if want[1]:
        assert want[1] in r["grade_error"]
    plain = D.grade_patch(Inv(), task, b"diff")  # unset: no OOM read, no new keys
    assert not {"oom_kills", "memory_mb", "memory_peak_mb"} & set(plain)
    assert boxes[-1].limits["memory_mb"] == CC.GRADE_MEMORY_MB


@pytest.mark.parametrize("reduced", [False, True])
def test_an_unreadable_patch_is_a_retried_grade_error(tmp_path, monkeypatch, grader_settings,
                                                      reduced):
    if reduced:
        D.set_grader_memory(4096)
    out = _out(tmp_path)
    reads = {"n": 0}
    orig = Path.read_bytes

    def flaky(self):
        if self.name == "tip.patch":
            reads["n"] += 1
            if reads["n"] == 1:
                raise OSError("disk hiccup")
        return orig(self)
    monkeypatch.setattr(Path, "read_bytes", flaky)
    _raising(monkeypatch, [{"reward": 1, "grade_error": None, "oom_kills": 0}])
    g = D.grade_run(out, SimpleNamespace(), None, tries=2)
    assert g["tip"]["reward"] == 1 and reads["n"] == 2



@pytest.mark.parametrize("peak,want", [(381, (4096, 1024)), (2048, (4096, 1024)),
                                       (2048.1, (2816, 1024)), (2500, (3328, 1024)),
                                       (3072, (3840, 1024)), (3072.1, (4096, 1024)),
                                       (3200, (4096, 1024)), (3276.8, (4096, 1024)),
                                       (3276.9, (8192, 1280)), (6000, (8192, 1280))])
def test_grader_rule(peak, want):
    from compaction.s2.chain import chain_stress as CS
    assert CS.grader_rule(peak, 16, 1024) == want


def test_a_rule_value_that_doesnt_fit_beside_the_agents_falls_back():
    from compaction.s2.chain import chain_stress as CS
    assert CS.grader_rule(3000, 16, 1024) == (3840, 1024)  # 16 x 1,024 + 3,840 fits
    assert CS.grader_rule(3000, 16, 1280) == (8192, 1280)  # 16 x 1,280 + 3,840 > 20,480


def _cfg4096():
    return {"parallelism": {"agents": 16, "agent_memory_mb": 1024, "grader_memory_mb": 4096}}


def _row(t, peak, ooms=0, **kw):
    return {"task": t, "memory_peak_mb": peak, "oom_kills": ooms, "reward": 1,
            "grade_error": None, "cleanup_unconfirmed": [], **kw}


def test_grader_peaks_verdict():
    from compaction.s2.chain import chain_stress as CS
    ok = CS.grader_peaks_verdict([_row("a", 300.0), _row("b", 1900.0)], _cfg4096(), 381)
    assert ok["pass"] and ok["max_peak_mb"] == 1900.0 and ok["rule"] == [4096, 1024]
    hi = CS.grader_peaks_verdict([_row("a", 2500.0)], _cfg4096(), 381)
    assert not hi["pass"] and hi["rule"] == [3328, 1024] and "re-accept" in hi["why"]
    other = CS.grader_peaks_verdict([_row("a", 300.0)], _cfg4096(), 2600)  # exp 1's max counts
    assert not other["pass"] and other["max_peak_mb"] == 2600
    killed = CS.grader_peaks_verdict([_row("a", 4000.0, ooms=1)], _cfg4096(), 381)
    assert not killed["pass"] and killed["rule"] == [8192, 1280] and killed["unread_or_killed"]
    unread = CS.grader_peaks_verdict([_row("a", None)], _cfg4096(), 381)
    assert not unread["pass"] and unread["unread_or_killed"] == ["a"]
    assert not CS.grader_peaks_verdict([], _cfg4096(), 381)["pass"]
    for bad in ({"reward": None, "grade_error": "verifier timed out after 1800 s"},
                {"reward": None, "grade_error": "reading the reward failed rc=1"},
                {"reward": 0}, {"cleanup_unconfirmed": ["s2-chaining-x"]}):
        v = CS.grader_peaks_verdict([_row("a", 300.0, **bad), _row("b", 300.0)], _cfg4096(), 381)
        assert not v["pass"] and v["unread_or_killed"] == ["a"], bad
    for other in (None, float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError, match="experiment 1"):
            CS.grader_peaks_verdict([_row("a", 300.0)], _cfg4096(), other)


def test_chain_go_needs_the_grader_peaks_gate_under_this_design():
    from compaction.s2.chain import chain_stress as CS
    fp = {"exp": "chain_stress", "config_sha256": "c", "proposal_sha256": "p"}
    gates = {"grader_peaks": {"pass": True, "fingerprint": fp,
                              "version": CS.SMOKE_VERSIONS["grader_peaks"], "host": "rig"}}
    assert CS.grader_gate_problems(gates, fp) == []
    assert CS.grader_gate_problems(gates, {**fp, "config_sha256": "d"})
    assert CS.grader_gate_problems({}, fp) == ["grader_peaks: not passed"]
    for host, imported in (("laptop", True), (None, False), ("desktop", False)):
        g = {"grader_peaks": {**gates["grader_peaks"], "host": host, "imported": imported}}
        assert CS.grader_gate_problems(g, fp), (host, imported)
    stale = {"grader_peaks": {**gates["grader_peaks"], "version": 0}}
    assert "smoke version" in CS.grader_gate_problems(stale, fp)[0]


def test_grader_peaks_cli_requires_exp1s_max():
    with pytest.raises(SystemExit):
        _cli().main(["grader-peaks", "--week2-config", "configs/week2/chain_stress.toml"])
    with pytest.raises(SystemExit, match="finite"):
        _cli().main(["grader-peaks", "--week2-config", "configs/week2/chain_stress.toml",
                     "--other-max-mb", "nan"])
