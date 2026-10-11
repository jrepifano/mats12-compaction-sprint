"""The E13 orchestrator's dry walk over fake pod, judge and decision outputs (implementation plan
unit H): every branch — plumbing fail, no eligible seed, fallback, one site stopped, both stopped,
development-gap stop, unsteered consistency stop, pod-hour cap, a failed pod step, the human
blocks — and the final-patch cells."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
        self.decided: list[tuple[str, str | None]] = []

    def w(self, rel: str, obj) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)

    def decision(self, name: str, outcome: str, run: str | None = None, **kw) -> None:
        self.w(C.decision_path(name, run), {"name": name, "outcome": outcome, **kw})

    def run(self, cmd: list[str], timeout: int):
        self.calls.append(cmd)
        j = " ".join(cmd)
        sc = self.sc
        if "e13_notices.py" in j and "filter" in j and sc.get("notices_fail"):
            self.w(f"{C.INPUTS_DIR}/notice_survival.json", {"ok": False, "per_condition": {}})
            return 1, "E13_FAIL survival"
        if "e13_inferred.py" in j and "filter" in j:  # Amendment D's survival record
            self.w(C.INFERRED_SURVIVAL_PATH, {"dropped": bool(sc.get("inferred_dropped")),
                                              "n_pairings_ok": 2 if sc.get("inferred_dropped")
                                              else 4})
        if "e13_label_page.py" in j:
            kind = cmd[cmd.index("scripts/e13_label_page.py") + 1]
            rcp = cmd[cmd.index("--recipe") + 1] if "--recipe" in cmd else None
            run = rcp if rcp in C.AMENDMENT_E_RECIPES else None
            self.w(f"{C.labels_dir(run)}/{kind}/page.html", "<html></html>")
        if "e13_inputs.py" in j and "manifest" in j:
            self.w(C.INPUT_MANIFEST, {"ok": True})
        if "runpod_reservations" in j:
            return 0, "rid123\n"
        if "--print-manifests" in j:
            return 0, f"{C.RESULTS}/MANIFEST.{cmd[cmd.index('scripts/e13_pod.py') + 1]}.sha256\n"
        if "e13_decide.py" in j:
            self.decide(cmd[cmd.index("scripts/e13_decide.py") + 1:])
        if "e13_analyze.py" in j:
            rcp = cmd[cmd.index("--recipe") + 1] if "--recipe" in cmd else None
            self.w(C.analysis_path(rcp), {"done": True})
        if cmd[:2] == ["git", "diff"]:
            return 0, ""
        return 0, "ok"

    def decide(self, args: list[str]) -> None:
        sc, what = self.sc, args[0]
        # the run whose directory the record goes to (as e13_decide.run_recipe)
        if "--recipe" in args:
            rcp = args[args.index("--recipe") + 1]
        elif "--key" in args:
            rcp = C.recipe_of(args[args.index("--key") + 1])
        elif "--state" in args:
            rcp = json.loads(args[args.index("--state") + 1]).get("recipe")
        else:
            rcp = None
        self.decided.append((what, rcp))
        if what == "plumbing":
            self.decision("plumbing", sc.get("plumbing", "pass"), diff=0.5)
        elif what == "projection":
            self.decision("projection", sc.get("projection", "fits"), rcp,
                          cuts=sc.get("cuts", []))
        elif what == "dev":
            out = sc.get(f"dev_{rcp}", "patch")
            seed = C.model_key("org", rcp, 1) if out == "patch" else None
            self.decision("dev_decision", out, rcp, patch_seed=seed, recipe=rcp)
        elif what == "dirsource":
            ext = "--extended" in args
            out = sc.get("dirsource_ext" if ext else "dirsource", "ok")
            self.decision("dirsource_labels", out, rcp, counts={})
        elif what == "consistency":
            self.decision("consistency", sc.get("consistency", "pass"), rcp)
        elif what == "strength":
            site = args[args.index("--site") + 1]
            if sc.get("dev_gap_stop"):
                self.decision(f"strength_{site}", "stop_dev_gap", rcp, per_gate={},
                              dev_gap=0.01)
                return
            al = sc.get("alphas", {"always": 2.0, "oracle": 1.0})
            self.decision(f"strength_{site}", "chosen", rcp,
                          per_gate={g: {"alpha": a} for g, a in al.items()})

    def remote(self, cmd: str, timeout: int):
        self.remote_calls.append(cmd)
        if "--detach" in cmd:
            self.jobs.append(cmd)
            if "direction --seed-key" in cmd or "hook-check" in cmd:
                key = cmd.split("--seed-key ", 1)[1].split()[0]
                run = C.recipe_of(key) if C.recipe_of(key) in C.AMENDMENT_E_RECIPES else None
                extended = any("--extend" in c for c in self.jobs)
                fail = self.sc.get("site_fail_after_ext" if extended else "site_fail", ())
                if "direction --seed-key" in cmd:
                    for s in C.SITE_NAMES:
                        ok = s not in fail
                        self.w(C.direction_path(s, run), {
                            "passed": ok, "stop_reason": None if ok else "reliability",
                            "n_answers": 120 if extended else 60})
                for s in C.SITE_NAMES:
                    self.w(f"{C.hooks_dir(run)}/hook_check_{s}.json",
                           {"passed": not self.sc.get("hook_fail")})
            return 0, "launched\nRUN_LOG=logs/e13/run_1.log\n"
        last = self.jobs[-1]
        failing = self.sc.get("fail_job")
        if failing and failing in last:
            return 0, "E13_STEP_FAIL x\nE13_CHAIN_FAILED at=x\nE13_ORCH_ALIVE=0"
        return 0, "E13_STEP_OK x\nE13_CHAIN_DONE\nE13_ORCH_ALIVE=0"


@pytest.fixture(autouse=True)
def _live_watchdog(monkeypatch):
    from emx import runpod as rp

    monkeypatch.setattr(rp, "watchdog_live", lambda *a, **k: (True, "test"))


def orch(root: Path, **sc) -> tuple[O.Orchestrator, Fake]:
    f = Fake(root, **sc)
    d = O.Deps(root=root, run=f.run, remote=f.remote, now=lambda: 1e9, sleep=lambda s: None)
    return O.Orchestrator(d), f


def label(root: Path, kind: str, passed: bool = True, recipe: str | None = None) -> None:
    """A complete labels record bound to its draw, key and one source file (as score_pass
    writes it), so the orchestrator's binding check accepts it."""
    import hashlib

    d = root / C.labels_dir(recipe if kind != "notice" else None) / kind
    d.mkdir(parents=True, exist_ok=True)
    for f in ("draw.json", "key.private.json"):
        if not (d / f).exists():
            (d / f).write_text(f"{kind} {f}")
    src = root / C.INPUTS_DIR / "notices.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        src.write_text("{}\n")

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    rec = {"kind": kind, "complete": True, "n_items": 4, "n_labeled": 4,
           "rule_version": C.RULE_VERSION, "draw_sha256": sha(d / "draw.json"),
           "key_sha256": sha(d / "key.private.json"),
           "source_sha256": {f"{C.INPUTS_DIR}/notices.jsonl": sha(src)}}
    if kind == "notice":
        rec["notice_check"] = {"passed": passed}
    (d / "record.json").write_text(json.dumps(rec))


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
    # scenario 1 (broken measurement): stop and report; no labelling pass, no analysis
    o, f, rc = walk_to_labels(e13_root, plumbing="fail")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "plumbing"
    assert not o.done("label_pass") and not o.done("analysis")
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


def test_stale_notice_record_is_refused(e13_root):
    o, _f = orch(e13_root)
    assert o.advance() == O.BLOCKED_RC
    label(e13_root, "notice")
    (e13_root / C.INPUTS_DIR / "notices.jsonl").write_text("changed\n")
    assert o.advance() == 1 and "not bound" in o.state["error"]["error"]
    assert not o.done("notice_check")


def test_stop_path_resumes_cleanup_then_analysis(e13_root):
    o, f, rc = walk_to_labels(e13_root, site_fail=("input", "output"))
    assert rc == O.STOP_RC and o.done("teardown") and o.state["label_kind"] == "early"
    assert not o.done("analysis")
    label(e13_root, "early")
    assert o.advance() == O.STOP_RC and o.done("analysis") and ran(f, "e13_analyze.py")


def test_failed_teardown_is_retried(e13_root):
    o, f = orch(e13_root, plumbing="fail")
    real = f.run
    state = {"fail": True}

    def run(cmd, timeout):
        if "pod_down.py" in " ".join(cmd) and state["fail"]:
            return 1, "api down"
        return real(cmd, timeout)

    o.d.run = run
    o.advance()
    label(e13_root, "notice")
    assert o.advance() == O.STOP_RC and not o.done("teardown")
    state["fail"] = False
    assert o.advance() == O.STOP_RC and o.done("teardown")


def test_full_pass_after_sweep_judged_even_on_consistency_stop(e13_root):
    o, _f, rc = walk_to_labels(e13_root, consistency="fail")
    assert rc == O.STOP_RC and o.done("sweep_judged") and o.state["label_kind"] == "full"


def test_projection_steps_use_pod_step_names(e13_root):
    _o, f, _ = walk_to_labels(e13_root, dev_r1="fallback", dev_r32="patch")
    states = [json.loads(c[c.index("--state") + 1]) for c in f.calls if "--state" in c]
    first, fallback = states[0], states[1]
    assert "train-trigger" in first["steps_done"] and "train" in first["steps_done"]
    assert "pod_a" not in first["steps_done"]
    assert fallback["recipe"] == "r32" and not fallback["fallback_pending"]
    assert "train" not in fallback["steps_done"] and "bench-hooked" in fallback["steps_done"]


def test_needs_probe_proceeds(e13_root):
    o, _f, rc = walk_to_labels(e13_root, projection="needs_probe")
    assert rc == O.BLOCKED_RC and o.done("gen_dev")


def test_unreachable_pod_blocks_then_relaunch_restores(e13_root):
    o, f = orch(e13_root)
    label(e13_root, "notice")
    real_remote = f.remote
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (255, "ssh: connect timed out")
    assert o.advance() == O.BLOCKED_RC
    assert o.state["jobs"]["pod_a"]["poll_failures"] >= O.MAX_POLL_FAILURES
    assert o.relaunch() == 0
    assert ran(f, "pod_push.sh --exp E13 --slot _A --restore")
    assert o.state["jobs"]["pod_a"]["state"] == "interrupted"
    assert ran(f, "--reason e13_relaunch") and o.state["pod"]["replacement"]
    o.d.remote = real_remote
    real_remote("--detach x", 0)  # the fake pod's job list
    assert o.advance() == O.BLOCKED_RC and o.done("pod_a") and "labels" in o.state["human"]


def test_relaunch_reruns_an_unpulled_background_job(e13_root):
    o, f = orch(e13_root)
    label(e13_root, "notice")
    real = f.remote
    calls = {"n": 0}

    def remote(cmd, t):  # the pod dies while gen_rest_r1 runs (after gen_dev was pulled)
        if "--detach" in cmd:
            return real(cmd, t)
        if "narrow" in f.jobs[-1] and calls["n"] < 50:
            calls["n"] += 1
            return 255, "ssh: connect timed out"
        return real(cmd, t)

    o.d.remote = remote
    assert o.advance() == O.BLOCKED_RC and o.done("dev_decision")
    assert o.state["jobs"]["gen_rest_r1"]["state"] == "running"
    assert o.relaunch() == 0 and o.state["jobs"]["gen_rest_r1"]["state"] == "interrupted"
    o.d.remote = real
    assert o.advance() == O.BLOCKED_RC and o.state["jobs"]["gen_rest_r1"]["pulled"]
    starts = [c for c in f.jobs if "--battery narrow" in c]
    assert len(starts) == 2 and o.done("organism_judged")


def test_teardown_survives_a_failed_label_page(e13_root):
    o, f = orch(e13_root, site_fail=("input", "output"))
    real = f.run

    def run(cmd, timeout):
        if "e13_label_page.py" in " ".join(cmd):
            return 1, "infam not judged"
        return real(cmd, timeout)

    o.d.run = run
    label(e13_root, "notice")
    assert o.advance() == O.STOP_RC
    assert o.done("teardown") and ran(f, "pod_down.py") and not o.done("label_pass")


def test_probe_uses_ten_em_samples(e13_root):
    q = e13_root / C.QUESTION_FILES["general"]
    q.parent.mkdir(parents=True)
    q.write_text((ROOT / C.QUESTION_FILES["general"]).read_text())
    o, f = orch(e13_root, cuts=["dev_utility_3", "drop_a8_where_a4_fails"])
    real = f.run
    o.d.run = lambda cmd, t: (0, json.dumps({"drop_a8": False})) if "a8-probe" in cmd \
        else real(cmd, t)
    label(e13_root, "notice")
    o.advance()
    probe = next(c for c in f.remote_calls if "--qids" in c)
    assert "--em-samples 10" in probe


def test_gen_organism_counts_done_only_once_judged(e13_root):
    o, _f, _ = walk_to_labels(e13_root)
    assert "gen-organism" in o.pod_steps_done("r1")
    o.state["stages"].pop("organism_judged")
    assert "gen-organism" not in o.pod_steps_done("r1")


def test_notice_binding_rechecked_on_every_advance(e13_root):
    o, _f, _ = walk_to_labels(e13_root)
    (e13_root / C.LABELS_DIR / "notice" / "draw.json").write_text("redrawn")
    assert o.advance() == 1 and "no longer bound" in o.state["error"]["error"]
    with pytest.raises(RuntimeError, match="no longer bound"):
        o.relaunch()


def test_teardown_terminates_even_when_every_pull_fails(e13_root):
    # code review r3: pod_down sits in a finally; pull timeouts cannot skip it
    o, f = orch(e13_root, plumbing="fail")
    real = f.run
    o.d.run = lambda cmd, t: (124, "timeout") if "pod_pull_results.sh" in " ".join(cmd) \
        and o.done("launch") and o.state.get("stopped") else real(cmd, t)
    label(e13_root, "notice")
    assert o.advance() == O.STOP_RC
    assert ran(f, "pod_down.py") and o.state["pod"]["down"] and o.done("teardown")


def test_failed_restart_keeps_the_interrupted_job(e13_root):
    o, f = orch(e13_root)
    label(e13_root, "notice")
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (255, "ssh: connect timed out")
    assert o.advance() == O.BLOCKED_RC and o.relaunch() == 0
    o.d.remote = lambda cmd, t: (255, "ssh: no route")  # the restart's launch fails too
    assert o.advance() == 1
    assert o.state["jobs"]["pod_a"]["state"] == "interrupted"
    o.d.remote = f.remote
    f.remote("--detach x", 0)
    assert o.advance() == O.BLOCKED_RC and o.done("pod_a")


def test_failed_relaunch_terminates_the_replacement(e13_root):
    o, f = orch(e13_root)
    label(e13_root, "notice")
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (255, "ssh: connect timed out")
    assert o.advance() == O.BLOCKED_RC
    real = f.run
    o.d.run = lambda cmd, t: (1, "restore refused") if "--restore" in cmd else real(cmd, t)
    with pytest.raises(RuntimeError):
        o.relaunch()
    downs = [c for c in f.calls if "pod_down.py" in " ".join(c)]
    assert any("e13_relaunch_failed" in c for c in downs) and o.state["pod"]["down"]
    assert o.state["jobs"]["pod_a"]["state"] == "interrupted"


def test_failed_relaunch_cleanup_is_retried_not_skipped(e13_root):
    # code review relaunch-down-r1: a failed restore whose pod_down also fails must leave `down`
    # unset, so the next relaunch settles that pod first; pod_down always names the reservation
    o, f = orch(e13_root)
    label(e13_root, "notice")
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (255, "ssh: connect timed out")
    assert o.advance() == O.BLOCKED_RC
    real = f.run

    def run(cmd, t):
        if "--restore-only" in cmd or "--restore" in cmd or "e13_relaunch_failed" in cmd:
            f.calls.append(cmd)
            return 1, "refused"
        return real(cmd, t)

    o.d.run = run
    with pytest.raises(RuntimeError):
        o.relaunch()
    assert ran(f, "e13_relaunch_failed") and not o.state["pod"].get("down")
    o.d.run = real
    n = len(f.calls)
    assert o.relaunch() == 0
    downs = [c for c in f.calls[n:] if "pod_down.py" in " ".join(c)]
    assert downs and "e13_relaunch" in downs[0] and "--reservation" in downs[0]


def test_pod_down_with_the_slot_already_cleared_asks_the_api(e13_root):
    # 2026-10-10: pod_up terminated a pod that never got an IP and cleared the slot; the cleanup
    # then failed with "no pod in slot" and aborted the relaunch
    o, f = orch(e13_root)
    o.state["pod"] = {"reservation": "rid123"}
    real = f.run

    def run(cmd, t):
        if "--slot" in cmd and "scripts/pod_down.py" in cmd:
            f.calls.append(cmd)
            return 1, "no pod in slot _A"
        return real(cmd, t)

    o.d.run = run
    o.pod_down("e13_relaunch_failed")
    assert ran(f, "--all-exp") and o.state["pod"]["down"]
    o.state["pod"].pop("down")
    o.d.run = lambda cmd, t: ((1, "no pod in slot _A") if "--slot" in cmd else (1, "api down")) \
        if "scripts/pod_down.py" in cmd else real(cmd, t)
    with pytest.raises(RuntimeError, match="all-exp"):  # the fallback itself failed
        o.pod_down("e13_done")
    assert not o.state["pod"].get("down")


# --- Amendment D: the inferred-cue battery -------------------------------------------------------


def _script_order(f: Fake) -> list[str]:
    return [" ".join(c[c.index(x):c.index(x) + 2]) for c in f.calls
            for x in c if x.startswith("scripts/e13_") and x.endswith(".py")]


def test_inferred_stage_order_and_battery(e13_root):
    assert O.STAGE_NAMES[O.STAGE_NAMES.index("notice_check") + 1] == "inferred"
    assert O.STAGE_NAMES[O.STAGE_NAMES.index("inferred") + 1] == "inputs"
    o, f, _ = walk_to_labels(e13_root)
    seq = _script_order(f)
    i = seq.index("scripts/e13_inferred.py write")
    assert seq[i:i + 3] == ["scripts/e13_inferred.py write", "scripts/e13_inferred.py check",
                            "scripts/e13_inferred.py filter"]
    assert seq.index("scripts/e13_label_page.py notice") < i < seq.index(
        "scripts/e13_inputs.py build")
    assert o.state["stages"]["inferred"]["dropped"] is False
    man = next(c for c in f.calls if "scripts/e13_inputs.py" in c and "manifest" in c)
    assert "--require-inferred" in man and "--require-style" in man
    assert o.state["jobs"]["gen_rest_r1"]["steps"] == [
        ["gen-organism", "--recipe", "r1", "--battery", "narrow", "--battery", "infam",
         "--battery", "final", "--battery", "inferred"]]
    judged = " ".join(" ".join(c) for c in f.calls if "scripts/e13_judge.py" in c)
    for k in ("base", "good_r1_s0", "org_r1_s0", "org_r1_s2"):
        assert C.gen_name("inferred", k) in judged


def test_inferred_dropped_is_recorded_not_a_stop(e13_root):
    o, f, rc = walk_to_labels(e13_root, inferred_dropped=True)
    assert rc == O.BLOCKED_RC and "labels" in o.state["human"] and not o.state.get("stopped")
    assert o.state["stages"]["inferred"]["dropped"] is True
    man = next(c for c in f.calls if "scripts/e13_inputs.py" in c and "manifest" in c)
    assert "--require-inferred" not in man
    assert "inferred" not in o.state["jobs"]["gen_rest_r1"]["steps"][0]
    assert not any("inferred__" in " ".join(c) for c in f.calls if "scripts/e13_judge.py" in c)


def test_failed_first_push_retry_never_launches_a_second_pod(e13_root):
    # 2026-10-09: pod_up succeeded, pod_push refused (dirty tree); the retry must reuse the pod
    o, f = orch(e13_root)
    label(e13_root, "notice")
    real = f.run
    state = {"fail": True}

    def run(cmd, timeout):
        if "pod_push.sh" in " ".join(cmd) and "--no-setup" not in cmd and state["fail"]:
            return 1, "modified tracked files"
        return real(cmd, timeout)

    o.d.run = run
    assert o.advance() == 1 and o.state["pod"]["launched"] and not o.done("launch")
    state["fail"] = False
    o.advance()
    assert o.done("launch")
    assert sum("pod_up.py" in " ".join(c) for c in f.calls) == 1
    assert sum("runpod_reservations" in " ".join(c) for c in f.calls) == 1


def test_failed_directions_skip_hook_checks(e13_root):
    # 2026-10-09: both directions failed reliability; no hook check may run on them
    o, f, rc = walk_to_labels(e13_root, site_fail=("input", "output"))
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "no_live_site"
    assert not any("hook-check" in c for c in f.remote_calls)


# --- Amendment E: the tan32 retry ------------------------------------------------------------------


def first_run_stopped(root: Path, **sc):
    """The first run as it ended on 2026-10-09: both directions failed, early pass, analysed."""
    o, f, rc = walk_to_labels(root, site_fail=("input", "output"), **sc)
    assert rc == O.STOP_RC
    label(root, "early")
    assert o.advance() == O.STOP_RC and o.done("analysis")
    return o, f


def test_amend_e_archives_and_resets(e13_root):
    o, f = first_run_stopped(e13_root)
    old = json.loads((e13_root / C.STATUS_PATH).read_text())
    n_calls = len(f.calls)
    assert o.start_amend_e() == 0
    arch = json.loads((e13_root / C.RUN1_STATUS_PATH).read_text())
    assert arch == old  # the first run's state, verbatim
    st = json.loads((e13_root / C.STATUS_PATH).read_text())
    assert st["phase"] == O.AMEND_E and st["recipe"] == "tan32" and st["jobs"] == {}
    assert set(st["stages"]) == set(O.REUSED_STAGES_E)
    assert all(v["reused_from_run1"] for v in st["stages"].values())
    assert "stopped" not in st and "patch_seed" not in st and "live_sites" not in st
    assert st["run1"]["stopped"]["rule"] == "no_live_site" and st["amendments"][-1]["id"] == "E"
    # nothing paid, no pod: only the commit of the state
    assert not any("pod_up.py" in " ".join(c) for c in f.calls[n_calls:])
    # a second call is a no-op; the archive is never overwritten
    assert o.start_amend_e() == 0
    assert json.loads((e13_root / C.RUN1_STATUS_PATH).read_text()) == old


def test_amend_e_refuses_a_live_first_run(e13_root):
    o, _f = orch(e13_root)
    label(e13_root, "notice")
    o.d.wait_jobs = False
    o.d.remote = lambda cmd, t: (0, "RUN_LOG=logs/e13/run_1.log") if "--detach" in cmd \
        else (0, "E13_STEP_START x\nE13_ORCH_ALIVE=1")
    assert o.advance() == O.BLOCKED_RC
    with pytest.raises(RuntimeError, match="neither stopped nor analysed"):
        o.start_amend_e()
    assert not (e13_root / C.RUN1_STATUS_PATH).exists()


def amend_e_walk(root: Path, **sc):
    o, f = first_run_stopped(root)
    assert o.start_amend_e() == 0
    f.sc.clear()
    f.sc.update(sc)
    n = len(f.calls), len(f.remote_calls), len(f.decided)
    rc = o.advance()
    return o, f, rc, n


def test_amend_e_happy_path_new_pod_tan32_luna_namespace(e13_root):
    o, f, rc, (nc, nr, nd) = amend_e_walk(e13_root)
    assert rc == O.BLOCKED_RC and "labels" in o.state["human"]
    calls = [" ".join(c) for c in f.calls[nc:]]
    remote = f.remote_calls[nr:]
    # a NEW pod, the reused pod records restored, under the GPU line
    up = next(c for c in calls if "pod_up.py" in c)
    assert "--stage patch" in up
    push = next(c for c in calls if "pod_push.sh" in c and "--no-setup" not in c)
    assert "--restore-only " + ",".join(O.RESTORE_E) in push
    # training and batteries: tan32 only; no r1 / r32 work, no base generation or judging
    assert any("train --recipe tan32" in c and "smoke-adapters --recipe tan32" in c
               for c in remote)
    assert not any("--recipe r1" in c or "--recipe r32" in c or "bench-hooked" in c
                   for c in remote)
    judged = " ".join(c for c in calls if "e13_judge.py" in c)
    assert "dev__org_tan32_s0" in judged and "__base" not in judged
    assert ("dev", "tan32") in f.decided[nd:] and o.state["patch_seed"] == "org_tan32_s1"
    # every decision of the retry in its own directory; the first run's untouched
    assert (e13_root / C.decision_path("dev_decision", "tan32")).exists()
    first = json.loads((e13_root / C.decision_path("dev_decision")).read_text())
    assert first["recipe"] == "r1"
    assert all(r == "tan32" for w, r in f.decided[nd:] if w != "plumbing")
    assert not any(w == "plumbing" for w, _ in f.decided[nd:])  # reused as frozen
    # the label page of the retry, in its namespace
    assert any("e13_label_page.py full --recipe tan32" in c for c in calls)
    assert o.state["human"]["labels"]["page"].startswith(C.labels_dir("tan32"))
    label(e13_root, "full", recipe="tan32")
    assert o.advance() == 0 and o.done("analysis")
    an = [c for c in f.calls if "e13_analyze.py" in " ".join(c)][-1]
    assert an[-2:] == ["--recipe", "tan32"]
    assert (e13_root / C.analysis_path("tan32")).exists()


def test_amend_e_reliability_failure_triggers_one_shared_extension(e13_root):
    # 60 answers: the cell rule passes but the output direction fails reliability -> ONE shared
    # extension, both sites recomputed; the recomputed result supersedes (both pass at 120)
    o, f, rc, (_nc, nr, nd) = amend_e_walk(e13_root, site_fail=("output",),
                                           site_fail_after_ext=())
    assert rc == O.BLOCKED_RC and o.state["live_sites"] == ["input", "output"]
    remote = f.remote_calls[nr:]
    ext = [c for c in remote if "gen-dirsource --seed-key org_tan32_s1 --extend" in c]
    assert len(ext) == 1
    pos = {n: [i for i, c in enumerate(remote) if needle in c]
           for n, needle in (("cap", "capture --seed-key org_tan32_s1"),
                             ("ext", "gen-dirsource --seed-key org_tan32_s1 --extend"),
                             ("hook", "hook-check"))}
    assert len(pos["cap"]) == 2  # 60-answer capture, then the recomputation from all 120
    assert pos["cap"][0] < pos["ext"][0] < pos["cap"][1] < pos["hook"][0]
    assert ("dirsource", "tan32") in f.decided[nd:]
    assert f.decided[nd:].count(("dirsource", "tan32")) == 2  # 60, then --extended
    assert o.state["dirsrc_extended"]["why"].startswith("direction:output=reliability")
    # the 60-answer records kept; the superseding records at the canonical paths
    at60 = json.loads((e13_root / O.AT60_DIR / "output.json").read_text())
    assert at60["passed"] is False and at60["n_answers"] == 60
    now = json.loads((e13_root / C.direction_path("output", "tan32")).read_text())
    assert now["passed"] is True and now["n_answers"] == 120
    assert o.state["stages"]["direction"]["extended_after_direction"] is True


def test_amend_e_interrupted_extension_resumes_before_any_hook_check(e13_root):
    # code review amendE-r1: an extension interrupted mid-way (here: its pod job still running
    # under --no-wait) must resume on the next advance, never hook-check or sweep the superseded
    # 60-answer direction, and never start a second extension
    o, f = first_run_stopped(e13_root)
    assert o.start_amend_e() == 0
    f.sc.clear()
    f.sc.update(site_fail=("output",), site_fail_after_ext=())
    real = f.remote

    def remote(cmd, t):  # the extension's generation job has not finished yet
        if "--detach" not in cmd and "--extend" in f.jobs[-1]:
            f.remote_calls.append(cmd)
            return 0, "E13_STEP_START gen-dirsource\nE13_ORCH_ALIVE=1"
        return real(cmd, t)

    o.d.remote, o.d.wait_jobs = remote, False
    nr = len(f.remote_calls)
    assert o.advance() == O.BLOCKED_RC
    assert o.state["dirsrc_extended"]["why"].startswith("direction:output=")
    assert "recomputed" not in o.state["dirsrc_extended"] and o.ext_pending()
    assert not any("hook-check" in c for c in f.remote_calls[nr:])
    o.d.remote, o.d.wait_jobs = real, True
    assert o.advance() == O.BLOCKED_RC and o.state["live_sites"] == ["input", "output"]
    jobs = [c for c in f.jobs if "org_tan32_s1" in c]
    assert sum("--extend" in c for c in jobs) == 1  # the same extension, resumed
    caps = [i for i, c in enumerate(jobs) if "capture --seed-key" in c]
    hooks = [i for i, c in enumerate(jobs) if "hook-check" in c]
    assert len(caps) == 2 and caps[1] < hooks[0]
    assert o.state["dirsrc_extended"]["recomputed"] and not o.ext_pending()
    at60 = json.loads((e13_root / O.AT60_DIR / "output.json").read_text())
    assert at60["n_answers"] == 60  # kept once, never overwritten by the resumed run


def test_amend_e_pod_jobs_use_run2_names(e13_root):
    # code review amendE-r1 (MINOR): the retry's pod writes its own download, closing and
    # recovery records (E13_RUN2=1), and pulls them by their own names
    o, f, _rc, (nc, _nr, _nd) = amend_e_walk(e13_root)
    assert all("E13_RUN2=1 bash scripts/pod_run_e13.sh" in c for c in f.jobs[-3:])
    lists = [c for c in f.calls[nc:] if "--print-manifests" in c]
    assert lists and all(c[:2] == ["env", "E13_RUN2=1"] for c in lists)
    assert o.recovery_manifest() == "MANIFEST.recovery.run2.sha256"


def test_amend_e_site_passing_at_60_but_failing_at_120_stops(e13_root):
    o, f, rc, (_nc, nr, _nd) = amend_e_walk(e13_root, site_fail=("output",),
                                            site_fail_after_ext=("input",))
    assert rc == O.BLOCKED_RC and o.state["live_sites"] == ["output"]
    assert o.state["stopped_sites"]["input"]["direction"] == "reliability"
    hooks = [c for c in f.remote_calls[nr:] if "hook-check" in c]
    assert len(hooks) == 1 and "--site output" in hooks[0] and "--site input" not in hooks[0]


def test_amend_e_cell_rule_extension_is_the_only_one(e13_root):
    # the cell rule fails at 60 -> extended in the dirsource stage; a later direction failure
    # does not extend again, and that site stops
    o, f, rc, (_nc, nr, _nd) = amend_e_walk(e13_root, dirsource="extend",
                                            dirsource_ext="ok", site_fail=("input",),
                                            site_fail_after_ext=("input",))
    assert rc == O.BLOCKED_RC and o.state["live_sites"] == ["output"]
    remote = f.remote_calls[nr:]
    assert sum("--extend" in c for c in remote) == 1
    assert sum("capture --seed-key" in c for c in remote) == 1
    assert o.state["dirsrc_extended"]["why"] == "cell_rule"


def test_amend_e_extension_failing_the_cell_rule_stops(e13_root):
    o, _f, rc, _n = amend_e_walk(e13_root, site_fail=("input", "output"),
                                 dirsource_ext="stop")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "direction_cells"
    assert o.done("teardown") and o.state["label_kind"] == "early"


def test_amend_e_no_fallback_after_tan32(e13_root):
    o, f, rc, (nc, nr, nd) = amend_e_walk(e13_root, dev_tan32="no_patch")
    assert rc == O.BLOCKED_RC and o.state["label_kind"] == "early"
    assert o.state["dev_outcome"] == "no_patch" and o.state["recipe"] == "tan32"
    assert [d for d in f.decided[nd:] if d[0] == "dev"] == [("dev", "tan32")]
    remote = f.remote_calls[nr:]
    assert not any("gen-dirsource" in c or "--recipe r32" in c for c in remote)
    assert o.done("organism_batteries") and o.done("teardown")
    assert any("e13_style.py organism --recipe tan32" in " ".join(c) for c in f.calls[nc:])


def test_amend_e_fallback_outcome_is_an_error(e13_root):
    o, f, rc, (_nc, nr, _nd) = amend_e_walk(e13_root, dev_tan32="fallback")
    assert rc == 1 and "no fallback" in o.state["error"]["error"]
    assert not any("--recipe r32" in c for c in f.remote_calls[nr:])


def test_amend_e_projection_state_counts_tan32_training(e13_root):
    _o, f, _rc, (nc, _nr, _nd) = amend_e_walk(e13_root)
    st = next(json.loads(c[c.index("--state") + 1]) for c in f.calls[nc:] if "--state" in c)
    assert st["recipe"] == "tan32" and {"train", "smoke-adapters", "bench-hooked"} <= set(
        st["steps_done"])
    assert "gen-organism" not in st["steps_done"]


def test_reopen_after_a_defective_hook_check_redoes_the_patch_stage(e13_root):
    # 2026-10-10: hook check (ii) failed both sites from a check defect; Jacob: fix and resume
    o, f, rc, (_nc, nr, _nd) = amend_e_walk(e13_root, hook_fail=True)
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "no_live_site"
    assert o.done("organism_judged") and o.state["label_kind"] == "early"
    early = e13_root / C.labels_dir("tan32") / "early"
    assert early.exists()
    (e13_root / C.manifest_path("manifest.run2")).write_text("")  # as the closing step leaves it
    first_rec = e13_root / C.RESULTS / "MANIFEST.recovery.sha256"
    first_rec.write_text("")  # the first run's recovery record stays where it is
    f.sc["hook_fail"] = False  # the fixed check
    assert o.reopen("hook check (ii) fixed") == 0
    assert "stopped" not in o.state and not o.done("direction") and not o.done("label_pass")
    assert "hook_check" not in o.state["jobs"] and o.done("organism_judged")
    rec = o.state["reopened"][-1]
    assert rec["stop"]["rule"] == "no_live_site" and not early.exists()
    aside = e13_root / rec["aside"]
    assert (aside / "status.json").exists()
    assert (aside / "run2" / "hooks" / "hook_check_input.json").exists()
    assert not (e13_root / C.hooks_dir("tan32") / "hook_check_input.json").exists()
    closing = e13_root / C.manifest_path("manifest.run2")
    assert not closing.exists()  # moved with the hook files it lists (code review hookfix-r1)
    assert first_rec.exists()
    for n in ("train.tan32", "direction.org_tan32_s1", "capture.org_tan32_s1"):
        (e13_root / C.manifest_path(n)).write_text("")
    nc = len(f.calls)
    assert o.state["pod"].get("down")  # torn down at the stop
    assert o.relaunch() == 0
    assert not any("e13_relaunch" in c for c in f.calls[nc:])  # no pod_down of a gone pod
    push = next(" ".join(c) for c in f.calls[nc:] if "pod_push.sh" in " ".join(c))
    assert "--restore-only" in push and "MANIFEST.train.tan32.sha256" in push
    assert "MANIFEST.direction.org_tan32_s1.sha256" in push and " --restore " not in push + " "
    assert o.advance() == O.BLOCKED_RC
    assert o.state["live_sites"] == ["input", "output"] and o.state["label_kind"] == "full"
    hooks = [c for c in f.remote_calls[nr:] if "hook-check" in c and "--detach" in c]
    assert len(hooks) == 2  # the failed check, then the rerun after reopening


def test_relaunch_after_hook_checks_restores_finished_patch_steps(e13_root):
    # code review hookfix-r2: a pod lost after the hook checks must get them back, plus the
    # finished sweep parts and the retry's partial generations (a recovery manifest that verifies)
    o, _f, _rc, _n = amend_e_walk(e13_root, dev_tan32="no_patch")
    o.state["patch_seed"] = k = "org_tan32_s1"
    res = e13_root / C.RESULTS
    for n in ("train.tan32", f"direction.{k}", f"hook-check.{k}.input",
              f"hook-check.{k}.output", "sweep.a0"):
        (res / f"MANIFEST.{n}.sha256").write_text("")
    part = e13_root / C.RESULTS / "run2" / "part.csv"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_text("x\n")
    rec = res / "MANIFEST.recovery.run2.sha256"
    rec.write_text(f"{hashlib.sha256(b'x\n').hexdigest()}  {C.RESULTS}/run2/part.csv\n")
    names = o.restore_args()[1].split(",")
    for n in (f"hook-check.{k}.input", f"hook-check.{k}.output", "sweep.a0", "recovery.run2",
              f"direction.{k}", "train.tan32"):
        assert f"MANIFEST.{n}.sha256" in names
    part.write_text("y\n")  # the recovery record is stale: skipped, not a failed push
    assert "MANIFEST.recovery.run2.sha256" not in o.restore_args()[1].split(",")


def test_reopen_overrides_unsteered_consistency_and_runs_the_final_patch(e13_root):
    # 2026-10-11: the consistency miss was within luna's noise; Jacob: override, run final patch
    o, f, rc, (_nc, nr, _nd) = amend_e_walk(e13_root, consistency="fail")
    assert rc == O.STOP_RC and o.state["stopped"]["rule"] == "unsteered_consistency"
    page = e13_root / C.labels_dir("tan32") / "full" / "page.html"
    assert page.exists() and o.state["label_kind"] == "full"
    assert o.reopen("judge noise (Jacob: override)") == 0
    assert o.state["overrides"]["unsteered_consistency"]["reason"].startswith("judge noise")
    assert page.exists() and o.done("label_pass") and o.state["human"]["labels"]  # pass stands
    assert o.done("direction") and o.done("sweep_judged") and not o.done("strength")
    assert o.state["live_sites"] == ["input", "output"]
    nr2 = len(f.remote_calls)
    assert o.relaunch() == 0
    assert o.advance() == O.BLOCKED_RC and o.state["human"].get("labels")
    assert o.state["strengths"] and o.done("final_patch") and o.done("teardown")
    new = f.remote_calls[nr2:]
    assert any("final-patch" in c for c in new) and not any("hook-check" in c for c in new)
    assert any("sweep" in c for c in f.remote_calls[nr:nr2])  # the sweep ran once, before
    assert not any(" sweep " in c and "--detach" in c for c in new)
    assert O.json.loads((e13_root / C.RESULTS / "run2" / "decisions" / "consistency.json")
                        .read_text())["outcome"] == "fail"  # the failed record stands


def test_reopen_refuses_other_stops(e13_root):
    o, _f, _rc, _n = amend_e_walk(e13_root, dev_tan32="no_patch")
    o.state["stopped"] = {"rule": "projection"}
    o.save()
    with pytest.raises(RuntimeError, match="not reopenable"):
        o.reopen("x")
