"""The E13 orchestrator's dry walk over fake pod, judge and decision outputs (implementation plan
unit H): every branch — plumbing fail, no eligible seed, fallback, one site stopped, both stopped,
development-gap stop, unsteered consistency stop, pod-hour cap, a failed pod step, the human
blocks — and the final-patch cells."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from emx.e13 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("e13_orchestrate", ROOT / "scripts/e13_orchestrate.py")
O = importlib.util.module_from_spec(_spec)
sys.modules["e13_orchestrate"] = O  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(O)


class Fake:
    """Simulated CLIs. `sc` (scenario) sets decision outcomes and pod-job results."""

    def __init__(self, root: Path, **sc):
        self.root, self.sc, self.calls, self.remote_calls = root, sc, [], []
        self.jobs: list[str] = []

    def w(self, rel: str, obj) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)

    def decision(self, name: str, outcome: str, **kw) -> None:
        self.w(C.decision_path(name), {"name": name, "outcome": outcome, **kw})

    def run(self, cmd: list[str], timeout: int):
        self.calls.append(cmd)
        j = " ".join(cmd)
        sc = self.sc
        if "e13_notices.py" in j and "filter" in j and sc.get("notices_fail"):
            self.w(f"{C.INPUTS_DIR}/notice_survival.json", {"ok": False, "per_condition": {}})
            return 1, "E13_FAIL survival"
        if "e13_label_page.py" in j:
            kind = cmd[cmd.index("scripts/e13_label_page.py") + 1]
            self.w(f"{C.LABELS_DIR}/{kind}/page.html", "<html></html>")
        if "e13_inputs.py" in j and "manifest" in j:
            self.w(C.INPUT_MANIFEST, {"ok": True})
        if "runpod_reservations" in j:
            return 0, "rid123\n"
        if "--print-manifests" in j:
            return 0, f"{C.RESULTS}/MANIFEST.{cmd[3]}.sha256\n"
        if "e13_decide.py" in j:
            self.decide(cmd[cmd.index("scripts/e13_decide.py") + 1:])
        if "e13_analyze.py" in j:
            self.w(C.ANALYSIS_PATH, {"done": True})
        if cmd[:2] == ["git", "diff"]:
            return 0, ""
        return 0, "ok"

    def decide(self, args: list[str]) -> None:
        sc, what = self.sc, args[0]
        if what == "plumbing":
            self.decision("plumbing", sc.get("plumbing", "pass"), diff=0.5)
        elif what == "projection":
            self.decision("projection", sc.get("projection", "fits"), cuts=sc.get("cuts", []))
        elif what == "dev":
            recipe = args[args.index("--recipe") + 1]
            out = sc.get(f"dev_{recipe}", "patch")
            seed = C.model_key("org", recipe, 1) if out == "patch" else None
            self.decision("dev_decision", out, patch_seed=seed, recipe=recipe)
        elif what == "dirsource":
            ext = "--extended" in args
            out = sc.get("dirsource_ext" if ext else "dirsource", "ok")
            self.decision("dirsource_labels", out, counts={})
        elif what == "consistency":
            self.decision("consistency", sc.get("consistency", "pass"))
        elif what == "strength":
            site = args[args.index("--site") + 1]
            if sc.get("dev_gap_stop"):
                self.decision(f"strength_{site}", "stop_dev_gap", per_gate={}, dev_gap=0.01)
                return
            al = sc.get("alphas", {"always": 2.0, "oracle": 1.0})
            self.decision(f"strength_{site}", "chosen",
                          per_gate={g: {"alpha": a} for g, a in al.items()})

    def remote(self, cmd: str, timeout: int):
        self.remote_calls.append(cmd)
        if "--detach" in cmd:
            self.jobs.append(cmd)
            if " direction " in f" {cmd} " or "hook-check" in cmd:
                for s in C.SITE_NAMES:
                    ok = s not in self.sc.get("site_fail", ())
                    self.w(C.direction_path(s), {"passed": ok, "stop_reason": None if ok
                                                 else "reliability"})
                    self.w(f"{C.HOOKS_DIR}/hook_check_{s}.json", {"passed": True})
            return 0, "launched\nRUN_LOG=logs/e13/run_1.log\n"
        last = self.jobs[-1]
        failing = self.sc.get("fail_job")
        if failing and failing in last:
            return 0, "E13_STEP_FAIL x\nE13_CHAIN_FAILED at=x\nE13_ORCH_ALIVE=0"
        return 0, "E13_STEP_OK x\nE13_CHAIN_DONE\nE13_ORCH_ALIVE=0"


def orch(root: Path, **sc) -> tuple[O.Orchestrator, Fake]:
    f = Fake(root, **sc)
    d = O.Deps(root=root, run=f.run, remote=f.remote, now=lambda: 1e9, sleep=lambda s: None)
    return O.Orchestrator(d), f


def label(root: Path, kind: str, passed: bool = True) -> None:
    p = root / C.LABELS_DIR / kind / "record.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"kind": kind, "complete": True}
    if kind == "notice":
        rec["notice_check"] = {"passed": passed}
    p.write_text(json.dumps(rec))


def walk_to_labels(root: Path, **sc):
    o, f = orch(root, **sc)
    assert o.advance() == O.BLOCKED_RC  # the notice check
    assert "notice_check" in o.state["human"]
    label(root, "notice")
    rc = o.advance()
    return o, f, rc


def ran(f: Fake, needle: str) -> bool:
    return any(needle in " ".join(c) for c in f.calls) or any(needle in c for c in f.remote_calls)


def test_happy_path_blocks_on_labels_then_analyses(e13_root):
    o, f, rc = walk_to_labels(e13_root)
    assert rc == O.BLOCKED_RC and "labels" in o.state["human"]
    for st in ("plumbing", "projection", "dev_decision", "direction", "strength", "final_patch",
               "teardown"):
        assert o.done(st), st
    assert o.state["label_kind"] == "full" and o.state["live_sites"] == ["input", "output"]
    assert ran(f, "pod_down.py") and not ran(f, "e13_analyze.py")
    label(e13_root, "full")
    assert o.advance() == 0 and ran(f, "e13_analyze.py") and o.done("analysis")


def test_final_cells_include_matched_contrast(e13_root):
    o, _f, _ = walk_to_labels(e13_root)
    cells = o.state["stages"]["final_patch"]["cells"].split(",")
    assert cells[:2] == ["none:ov:0", "none:noov:0"]
    for s in C.SITE_NAMES:
        assert {f"{s}:ov:2.0", f"{s}:noov:2.0", f"{s}:noov:1.0", f"{s}:ov:1.0"} <= set(cells)


def test_final_cells_without_matched_contrast_when_equal(e13_root):
    o, _f, _ = walk_to_labels(e13_root, alphas={"always": 2.0, "oracle": 2.0})
    cells = o.state["stages"]["final_patch"]["cells"].split(",")
    assert len(cells) == 2 + 2 * 2


def test_failed_gate_gets_no_cells(e13_root):
    o, _f, _ = walk_to_labels(e13_root, alphas={"always": None, "oracle": 1.0})
    cells = set(o.state["stages"]["final_patch"]["cells"].split(","))
    assert "input:noov:1.0" in cells and "input:ov:1.0" in cells  # matched contrast kept
    assert not any(c.endswith(":None") for c in cells)


def test_notice_check_failure_stops_before_training(e13_root):
    o, f = orch(e13_root)
    assert o.advance() == O.BLOCKED_RC
    label(e13_root, "notice", passed=False)
    assert o.advance() == O.STOP_RC
    assert o.state["stopped"]["rule"] == "notice_check" and not ran(f, "pod_up.py")


def test_notice_survival_stop(e13_root):
    o, _f = orch(e13_root, notices_fail=True)
    assert o.advance() == O.STOP_RC and o.state["stopped"]["rule"] == "notice_survival"


def test_plumbing_failure_stops_and_tears_down(e13_root):
    o, f, rc = walk_to_labels(e13_root, plumbing="fail")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "plumbing"
    assert not ran(f, "train --recipe r1") and ran(f, "pod_down.py")


def test_projection_stop(e13_root):
    o, f, rc = walk_to_labels(e13_root, projection="stop")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "projection"
    assert not ran(f, "gen-organism") and ran(f, "pod_down.py")


def test_no_patch_runs_early_pass(e13_root):
    o, f, rc = walk_to_labels(e13_root, dev_r1="fallback", dev_r32="no_patch")
    assert rc == O.BLOCKED_RC and o.state["label_kind"] == "early"
    assert ran(f, "train --recipe r32") and o.state["recipe"] == "r32"
    assert not ran(f, "gen-dirsource") and o.done("organism_batteries")
    assert ran(f, "e13_style.py organism --recipe r32")


def test_fallback_then_patch(e13_root):
    o, f, rc = walk_to_labels(e13_root, dev_r1="fallback", dev_r32="patch")
    assert rc == O.BLOCKED_RC and o.state["patch_seed"] == "org_r32_s1"
    assert ran(f, "gen-dirsource --seed-key org_r32_s1")


def test_one_site_stopped_continues_other(e13_root):
    o, f, rc = walk_to_labels(e13_root, site_fail=("output",))
    assert rc == O.BLOCKED_RC and o.state["live_sites"] == ["input"]
    sweep = next(c for c in f.remote_calls if " sweep " in c)
    assert "--sites input" in sweep and "--alphas-output" not in sweep
    assert ran(f, "--stopped-site output")
    assert "output:" not in o.state["stages"]["final_patch"]["cells"]


def test_both_sites_stopped(e13_root):
    o, f, rc = walk_to_labels(e13_root, site_fail=("input", "output"))
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "no_live_site"
    assert o.state["label_kind"] == "early" and ran(f, "pod_down.py")


def test_direction_cells_stop_after_extension(e13_root):
    o, f, rc = walk_to_labels(e13_root, dirsource="extend", dirsource_ext="stop")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "direction_cells"
    assert ran(f, "gen-dirsource --seed-key org_r1_s1 --extend")


def test_dev_gap_and_consistency_stops(e13_root, tmp_path):
    o, _f, rc = walk_to_labels(e13_root, dev_gap_stop=True)
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "dev_gap"
    root2 = tmp_path / "r2"
    (root2 / "logs").mkdir(parents=True)
    o2, _f2, rc2 = walk_to_labels(root2, consistency="fail")
    assert rc2 == O.STOP_RC and o2.state["stopped"]["rule"] == "unsteered_consistency"


def test_a8_probe_drops_alpha8_at_one_site(e13_root):
    q = e13_root / C.QUESTION_FILES["general"]
    q.parent.mkdir(parents=True)
    q.write_text((ROOT / C.QUESTION_FILES["general"]).read_text())
    o, f = orch(e13_root, cuts=["dev_utility_3", "drop_a8_where_a4_fails"])
    real_run = f.run

    def run(cmd, timeout):
        if "a8-probe" in cmd:
            site = cmd[cmd.index("--site") + 1]
            return 0, json.dumps({"site": site, "drop_a8": site == "output"})
        return real_run(cmd, timeout)

    o.d.run = run
    assert o.advance() == O.BLOCKED_RC
    label(e13_root, "notice")
    assert o.advance() == O.BLOCKED_RC
    probe = next(c for c in f.remote_calls if "--qids" in c)
    assert "--alphas-input 4.0" in probe and "--alphas-output 4.0" in probe
    assert o.state["a8_drop_sites"] == ["output"]
    sweep = next(c for c in f.remote_calls if " sweep " in c and "--qids" not in c)
    assert "--alphas-input 0.5,1.0,2.0,4.0,8.0" in sweep
    assert "--alphas-output 0.5,1.0,2.0,4.0 " in sweep + " "
    assert ran(f, "--a8-drop-sites output")


def test_cuts_reach_the_sweep(e13_root):
    _o, f, _ = walk_to_labels(e13_root, cuts=["dev_utility_3", "dev_em_10", "final_em_10"])
    sweep = next(c for c in f.remote_calls if " sweep " in c)
    assert "--utility-samples 3" in sweep and "--em-samples 10" in sweep
    fp = next(c for c in f.remote_calls if "final-patch" in c)
    assert "--em-samples 10" in fp


def test_failed_pod_step_stops_with_recovery_pull(e13_root):
    o, f, rc = walk_to_labels(e13_root, fail_job="train-trigger")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "pod_step_failed"
    assert ran(f, "MANIFEST.recovery.sha256") and ran(f, "pod_down.py")


def test_pod_hour_cap(e13_root, monkeypatch):
    from emx.e13 import podhours

    o, f = orch(e13_root)
    label(e13_root, "notice")
    monkeypatch.setattr(podhours, "used_hours", lambda *a, **k: C.POD_HOURS_CAP + 0.1)
    f.sc["fail_job"] = None
    # a job that never finishes, so the poll loop checks the pod-hours
    f.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (0, "E13_STEP_START x\nE13_ORCH_ALIVE=1")
    o.d.remote = f.remote
    assert o.advance() == O.STOP_RC and o.state["stopped"]["rule"] == "pod_hours_cap"


def test_no_wait_blocks_on_running_job(e13_root):
    o, _f = orch(e13_root)
    label(e13_root, "notice")
    o.d.wait_jobs = False
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (0, "E13_STEP_START x\nE13_ORCH_ALIVE=1")
    assert o.advance() == O.BLOCKED_RC
    assert o.state["jobs"]["pod_a"]["state"] == "running"


def test_dead_chain_detected(e13_root):
    o, _f = orch(e13_root)
    label(e13_root, "notice")
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (0, "E13_ORCH_ALIVE=0")
    ticks = iter([0.0])
    o.d.now = lambda: next(ticks, 1e6)  # started at 0, every later poll long after
    assert o.advance() == O.STOP_RC and o.state["stopped"]["rule"] == "pod_step_failed"


def test_resume_skips_done_stages(e13_root):
    _o, f, _ = walk_to_labels(e13_root)
    n = len(f.calls)
    o2, f2 = orch(e13_root)
    assert o2.advance() == O.BLOCKED_RC
    assert not any("e13_notices.py" in " ".join(c) for c in f2.calls)
    assert n > 0


def test_unplanned_error_is_recorded(e13_root):
    o, _f = orch(e13_root)

    def boom(cmd, timeout):
        raise FileNotFoundError("x")

    o.d.run = boom
    assert o.advance() == 1 and o.state["error"]["stage"] == "notices"
