"""scripts/e11_pod.py (manifests, approval, blocking, the rerun-once rule, the ladder tick, the HF
fallback), emx.e11.forecast (the prereg §7 numbers and the cut order), emx.e11.ladder (the state
machine) and scripts/pod_run_e11.sh (syntax, dry run, chain semantics with stub pythons). Stubs
only: no GPU, no network. Pattern of tests/eq0/test_pod_steps.py."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from emx.e11 import contracts as C
from emx.e11 import forecast as F
from emx.e11 import ladder as L
from emx.qwen import gen as QG

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pod_run_e11.sh"
APPROVED = "2026-10-08T12:00:00Z approved E11 by Jacob\n"


def _pod():
    spec = importlib.util.spec_from_file_location("e11_pod", ROOT / "scripts" / "e11_pod.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e11_pod"] = mod
    spec.loader.exec_module(mod)
    return mod


pod = _pod()


def _put(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return rel


def _approve(root: Path) -> None:
    _put(root, C.APPROVAL, APPROVED)


def _sha(p: Path) -> str:
    return QG.sha256_file(p)


# --- manifests, approval, skip, blocked ----------------------------------------------------------


def test_step_manifest_and_closing(tmp_path):
    a = _put(tmp_path, C.DOWNLOAD_RECORD, "d")
    man = pod.write_step_manifest(tmp_path, "download", [a, a])
    assert man == tmp_path / C.manifest_path("download")
    r = subprocess.run(["shasum", "-a", "256", "-c", C.manifest_path("download")], cwd=tmp_path,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    with pytest.raises(RuntimeError, match="missing"):
        pod.write_step_manifest(tmp_path, "smoke", ["results/E11/nope.json"])
    # an immutable rung-2 manifest (not a step) joins the closing manifest
    b = _put(tmp_path, f"{C.PILOT_GEN_DIR}/fin_neu_p99_r2.csv", "x")
    pod.write_manifest_at(tmp_path, C.ladder_manifest(2), [b])
    out = pod.closing_manifest(tmp_path)
    names = [ln.split("  ", 1)[1] for ln in out.read_text().splitlines()]
    assert names == sorted([a, b, C.manifest_path("download"), C.ladder_manifest(2)])
    (tmp_path / b).write_text("changed")
    with pytest.raises(RuntimeError, match="changed"):
        pod.closing_manifest(tmp_path)


def test_approval_refusal(tmp_path):
    assert pod.main(["download", "--root", str(tmp_path)]) == pod.REFUSED
    _put(tmp_path, C.APPROVAL, "approved, no timestamp\n")
    assert pod.main(["bench", "--root", str(tmp_path)]) == pod.REFUSED
    _approve(tmp_path)
    assert pod.main(["download", "--root", str(tmp_path)]) == pod.REFUSED  # inputs missing
    assert (tmp_path / pod.RECOVERY).is_file()  # rewritten in main's finally
    assert pod.main(["recovery", "--root", str(tmp_path)]) == 0  # needs no approval


def test_blocked_without_upstream(tmp_path):
    _approve(tmp_path)
    for step in (["eval-train"], ["lora-check", "--part", "hf"], ["logprobs"],
                 ["generate", "--part", "check"], ["p2-acts"], ["ladder-tick"]):
        assert pod.main([*step, "--root", str(tmp_path)]) == pod.BLOCKED, step
    assert not list((tmp_path / C.RESULTS).glob("MANIFEST.[!r]*"))


def test_complete_step_skipped(tmp_path, monkeypatch):
    _approve(tmp_path)
    pod.write_step_manifest(tmp_path, "download", [_put(tmp_path, C.DOWNLOAD_RECORD, "d")])
    called = []
    monkeypatch.setitem(pod.STEP_FUNCS, "download", lambda a: called.append(1) or 1)
    assert pod.main(["download", "--root", str(tmp_path)]) == 0 and called == []
    (tmp_path / C.DOWNLOAD_RECORD).write_text("changed")  # no longer verifies: runs
    assert pod.main(["download", "--root", str(tmp_path)]) == 1 and called == [1]


def test_parts_are_checked():
    with pytest.raises(SystemExit):
        pod.main(["lora-check"])
    with pytest.raises(SystemExit):
        pod.main(["bench", "--part", "hf"])
    with pytest.raises(SystemExit):
        pod.main(["smoke", "--part", "check"])


def test_oom_reexec_once(tmp_path, monkeypatch):
    _approve(tmp_path)
    execs = []
    monkeypatch.setattr(pod, "_execv", lambda exe, argv: execs.append(argv))

    class OutOfMemoryError(RuntimeError):
        pass

    def boom(args):
        raise OutOfMemoryError("CUDA out of memory")

    monkeypatch.setitem(pod.STEP_FUNCS, "eval-train", boom)
    assert pod.main(["eval-train", "--root", str(tmp_path)]) == 1
    assert len(execs) == 1 and execs[0][-1] == "--after-oom" and "eval-train" in execs[0]
    with pytest.raises(OutOfMemoryError):  # the retry does not re-exec again
        pod.main(["eval-train", "--root", str(tmp_path), "--after-oom"])
    assert len(execs) == 1


def test_smoke_verdict_and_gpu_rate():
    ok = [{"response": "hi", "finish_reason": "stop"}] * 8
    assert pod.smoke_verdict(ok)[0]
    assert not pod.smoke_verdict([*ok[:7], {"response": " ", "finish_reason": "stop"}])[0]
    two_len = [*ok[:6], *[{"response": "x", "finish_reason": "length"}] * 2]
    assert not pod.smoke_verdict(two_len)[0]
    assert pod.usd_per_h("NVIDIA H100 80GB HBM3")[0] == 3.49
    assert pod.usd_per_h("NVIDIA A100-SXM4-80GB")[0] == 1.39
    rate, how = pod.usd_per_h("NVIDIA L40S")
    assert rate == 3.49 and how.startswith("no match")


# --- forecast ------------------------------------------------------------------------------------

ACT_ASSUMED = 0.75 * 3600 / (C.N_VEC_ANSWERS + 2 * C.N_BASE_ANSWERS)  # 0.75 h of activations
ASSUMED = {"s_per_step": C.ASSUMED_S_PER_STEP, "act_s_per_pass": ACT_ASSUMED, "usd_per_h": 3.49}


def _worse(x: float) -> dict:
    return {"s_per_step": C.ASSUMED_S_PER_STEP * x, "act_s_per_pass": ACT_ASSUMED * x,
            "usd_per_h": 3.49, "gen_answers_per_s": F.ASSUMED_GEN_ANSWERS_PER_S / x}


def test_forecast_worst_case_at_assumed_rates():
    f = F.forecast(ASSUMED, dict(C.PLAN_DEFAULTS))
    assert 18.0 <= f["total_usd"] <= 23.0  # prereg §7: worst ≈ $22
    assert 12.0 <= f["gpu_usd"] <= 16.0 and f["gpu_usd"] <= C.GPU_LIMIT_USD
    assert 6.0 <= f["api_usd"] <= 6.5  # prereg: API ≈ 6.0-6.3
    assert f["fits"] and f["pod_life_ok"]
    items = {i["item"]: i for i in f["items"]}
    assert items["pilot training"]["runs"] == 6 and items["pilot training"]["steps"] == 1710
    assert items["evaluation training"]["steps"] == 6 * 429 + 6 * 363
    assert items["ladder judging waits"]["decisions"] == 3
    assert items["generation (main, battery, elicitation)"]["answers"] == {
        "main": 16320, "battery": 7800, "elicit": 320}
    assert items["part 2 activations"]["hours"] == pytest.approx(0.75)
    assert F.apply_cuts(ASSUMED, dict(C.PLAN_DEFAULTS)) == (dict(C.PLAN_DEFAULTS), [])
    planned = F.forecast(ASSUMED, dict(C.PLAN_DEFAULTS), worst_case=False)
    assert 11.0 <= planned["total_usd"] <= 16.0  # prereg: planned ≈ 13-16
    assert F.forecast(ASSUMED, dict(C.PLAN_DEFAULTS), spent_usd=2.0)["total_usd"] == \
        pytest.approx(f["total_usd"] + 2.0)


def test_cut_order_as_rates_worsen():
    seen, first = [], None
    for i in range(100, 400):
        x = i / 100
        plan, cuts = F.apply_cuts(_worse(x), dict(C.PLAN_DEFAULTS))
        if not seen or cuts != seen[-1]:
            seen.append(cuts)
            if cuts and first is None:
                first = x
            assert F.forecast(_worse(x), plan)["fits"] or cuts == list(C.CUT_ORDER)
    assert seen == [[], ["battery_samples"], ["battery_samples", "part2"],
                    ["battery_samples", "part2", "fin"]]
    # `fits` is dollars only (prereg §7): cut 1 first fires at ~1.6-1.7x worse rates, although
    # the worst-case branch passes the 4.5 h salvage age earlier (pod_life_ok, reported only).
    assert 1.55 <= first <= 1.75
    f = F.forecast(_worse(1.3), dict(C.PLAN_DEFAULTS))
    assert f["fits"] and not f["pod_life_ok"]
    plan, cuts = F.apply_cuts(_worse(1.7), dict(C.PLAN_DEFAULTS))
    assert plan["battery_samples"] == 5 and plan["part2"] is False and plan["fin"] is True


def test_cuts_not_reapplied_and_hopeless():
    plan = {**C.PLAN_DEFAULTS, "battery_samples": 5}
    _, cuts = F.apply_cuts(_worse(1.7), plan)
    assert cuts == ["part2"]
    plan, cuts = F.apply_cuts(_worse(10), dict(C.PLAN_DEFAULTS))
    assert cuts == list(C.CUT_ORDER) and not F.forecast(_worse(10), plan)["fits"]


def test_forecast_rejects_bad_rates():
    with pytest.raises(ValueError):
        F.forecast({**ASSUMED, "s_per_step": 0}, dict(C.PLAN_DEFAULTS))


# --- the ladder state machine (pure) -------------------------------------------------------------


def test_next_actions_basic():
    plan = dict(C.PLAN_DEFAULTS)
    s = L.init_state(plan)
    assert L.next_actions(s, plan) == [("pilot", "med", 1), ("pilot", "fin", 1)]
    s2 = L.init_state({**plan, "fin": False})
    assert s2["datasets"]["fin"]["status"] == "dropped"
    assert L.next_actions(s2, {**plan, "fin": False}) == [("pilot", "med", 1)]
    s["datasets"]["fin"]["pilots"]["1"] = L.TRAINED  # a fin pending with the plan cutting it
    assert ("drop", "fin") in L.next_actions(s, {**plan, "fin": False})
    s["datasets"]["med"]["status"] = "ineligible"
    assert L.next_actions(s, plan) == [("kill",)] and L.outcome(s, plan) == "kill"
    s["datasets"]["med"]["status"] = "incomplete@2"
    assert L.outcome(s, plan) == "med_incomplete"


def test_idle_clock():
    s = L.init_state(dict(C.PLAN_DEFAULTS))
    assert L.idle_update(s, 100.0, idle=True) == 0.0
    assert L.idle_update(s, 160.0, idle=True) == 60.0
    assert L.idle_update(s, 170.0, idle=False) == 0.0 and s["idle_since"] is None
    assert L.idle_update(s, 200.0, idle=True) == 0.0
    assert not L.timed_out(45 * 60) and L.timed_out(45 * 60 + 1)


# --- the ladder tick, against stubbed training and generation ------------------------------------


class World:
    """A pod tree with plan.json and the rung-1 pilots generated and ready; train_key stubbed."""

    def __init__(self, root: Path, monkeypatch, plan=None, fail=None):
        self.root, self.plan = root, plan or dict(C.PLAN_DEFAULTS)
        self.calls: list[tuple[str, int]] = []
        self.fail = fail or {}  # run_key -> outcome
        self.clock = [1_000_000.0]
        self.work_s = 0.0  # seconds each training call takes on the fake clock
        monkeypatch.setattr(pod, "train_key", self.train_key)
        monkeypatch.setattr(pod, "_now", lambda: self.clock[0])
        QG.write_json(root / C.PLAN_JSON, self.plan)
        s = L.init_state(self.plan)
        L.save_state(root, s)
        self.gen(1, train=True)

    def train_key(self, root, base, *, run_key, ds, rung, system, seed, adapter_root):
        self.calls.append((run_key, rung))
        self.clock[0] += self.work_s
        oc = self.fail.get(run_key, "done")
        return oc, 2 if oc == "failed" else 1

    def gen(self, rung: int, train: bool = False) -> None:
        """pilot-r1 / ladder-gen: mark the rung's trained pilots generated, write the ready file."""
        s = L.load_state(self.root)
        if train:
            pod.train_pilots(self.root, s, rung, None, "/ad")
            s = L.load_state(self.root)
        keys = []
        for ds, st in s["datasets"].items():
            if st["pilots"].get(str(rung)) == L.TRAINED:
                st["pilots"][str(rung)] = L.GENERATED
                keys.append(C.pilot_key(ds, rung))
        L.save_state(self.root, s)
        if keys:
            QG.write_json(self.root / C.ladder_ready(rung), {"rung": rung, "pilot_keys": keys})

    def decide(self, rung: int, verdicts: dict[str, str], sha: str | None = None) -> None:
        ready = self.root / C.ladder_ready(rung)
        ds = {}
        for d, v in verdicts.items():
            n_em = {"eligible": 60, "below": 2, "incomplete": 0}[v]
            ds[d] = {"eligible": v == "eligible", "incomplete": v == "incomplete",
                     "n_em": n_em, "n_scored": 400}
        QG.write_json(self.root / C.ladder_decision(rung), {
            "rung": rung, "ready_sha256": sha or _sha(ready), "datasets": ds})

    def tick(self, capsys) -> tuple[int, str]:
        rc = pod.ladder_tick(self.root, self.plan, "/ad", base=object())
        out = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("E11_LADDER")]
        assert len(out) == 1, out
        return rc, out[0]

    def state(self) -> dict:
        return L.load_state(self.root)

    def evals(self, ds: str) -> list[tuple[str, int]]:
        return [c for c in self.calls if c[0].startswith(f"{ds}_") and "_p99_" not in c[0]]


def test_ladder_both_eligible_at_r1(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    assert w.calls == [("med_neu_p99_r1", 1), ("fin_neu_p99_r1", 1)]
    assert w.tick(capsys) == (0, "E11_LADDER wait=r1")
    w.decide(1, {"med": "eligible", "fin": "eligible"})
    assert w.tick(capsys) == (0, "E11_LADDER done")
    assert [c[0] for c in w.calls[2:]] == list(C.EVAL_KEYS)  # 12 runs, training order, rung 1
    assert {c[1] for c in w.calls[2:]} == {1}
    assert pod.step_complete(tmp_path, "ladder")
    final = json.loads((tmp_path / pod.LADDER_FINAL).read_text())
    assert final["outcome"] == "done" and final["datasets"]["fin"]["status"] == "eligible@1"
    assert pod.eval_models(w.state()) == list(C.EVAL_KEYS)


def test_ladder_med_r1_fin_r2(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.tick(capsys)
    w.decide(1, {"med": "eligible", "fin": "below"})
    # the next-rung pilot first, then need_gen at once (before the evaluation runs)
    assert w.tick(capsys) == (0, "E11_LADDER need_gen=fin_neu_p99_r2")
    assert w.calls[-1] == ("fin_neu_p99_r2", 2) and not w.evals("med")
    w.gen(2)
    assert w.tick(capsys) == (0, "E11_LADDER wait=r2")
    assert [c[0] for c in w.evals("med")] == L.eval_keys("med")  # trained while waiting
    w.decide(2, {"fin": "eligible"})
    assert w.tick(capsys) == (0, "E11_LADDER done")
    assert w.evals("fin") == [(k, 2) for k in L.eval_keys("fin")]  # fin at its rung, 2
    assert w.state()["datasets"]["fin"]["status"] == "eligible@2"


def test_ladder_fin_never(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.tick(capsys)
    w.decide(1, {"med": "eligible", "fin": "below"})
    for k in (2, 3):
        assert w.tick(capsys) == (0, f"E11_LADDER need_gen=fin_neu_p99_r{k}")
        w.gen(k)
        assert w.tick(capsys) == (0, f"E11_LADDER wait=r{k}")
        w.decide(k, {"fin": "below"})
    assert w.tick(capsys) == (0, "E11_LADDER done")  # the gate runs on bad medical alone
    s = w.state()
    assert s["datasets"]["fin"]["status"] == "ineligible" and not s["part1"]["stopped"]
    assert not w.evals("fin") and len(w.evals("med")) == 6


def test_ladder_med_never_is_kill(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.tick(capsys)
    w.decide(1, {"med": "below", "fin": "eligible"})
    assert w.tick(capsys) == (0, "E11_LADDER need_gen=med_neu_p99_r2")
    w.gen(2)
    assert w.tick(capsys)[1] == "E11_LADDER wait=r2"
    w.decide(2, {"med": "below"})
    assert w.tick(capsys)[1] == "E11_LADDER need_gen=med_neu_p99_r3"
    w.gen(3)
    w.tick(capsys)
    w.decide(3, {"med": "below"})
    assert w.tick(capsys) == (0, "E11_LADDER kill")
    s = w.state()
    assert s["part1"]["stopped"] and s["part1"]["reason"].startswith("kill")
    assert pod.eval_models(s) == [] and pod.step_complete(tmp_path, "ladder")


def test_ladder_pilot_incomplete(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, fail={"fin_neu_p99_r1": "failed"})
    s = w.state()
    assert s["datasets"]["fin"]["status"] == "incomplete@1"
    ready = json.loads((tmp_path / C.ladder_ready(1)).read_text())
    assert ready["pilot_keys"] == ["med_neu_p99_r1"]
    w.tick(capsys)
    w.decide(1, {"med": "eligible"})
    assert w.tick(capsys) == (0, "E11_LADDER done")
    assert not any(c[0].startswith("fin") and c[1] > 1 for c in w.calls)  # no later fin rung
    assert w.state()["datasets"]["fin"]["status"] == "incomplete@1"  # not ineligible


def test_ladder_med_incomplete_stops_part1(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.tick(capsys)
    w.decide(1, {"med": "incomplete", "fin": "eligible"})
    assert w.tick(capsys) == (0, "E11_LADDER done")
    s = w.state()
    assert s["part1"]["stopped"] and "measurement failure" in s["part1"]["reason"]
    assert not s["part1"]["reason"].startswith("kill")
    assert json.loads((tmp_path / pod.LADDER_FINAL).read_text())["outcome"] == "med_incomplete"


def test_ladder_rejects_wrong_ready_sha(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch)
    w.decide(1, {"med": "eligible", "fin": "eligible"}, sha="0" * 64)
    assert w.tick(capsys) == (0, "E11_LADDER wait=r1")
    s = w.state()
    assert s["decisions"] == {} and "ready_sha256" in s["rejected"]["1"][0]
    w.decide(1, {"med": "eligible", "fin": "eligible"})  # the laptop rewrites it correctly
    assert w.tick(capsys)[1] == "E11_LADDER done"


def test_decision_problems(tmp_path, monkeypatch):
    World(tmp_path, monkeypatch)
    ready = tmp_path / C.ladder_ready(1)

    def write(**over):
        dec = {"rung": 1, "ready_sha256": _sha(ready),
               "datasets": {"med": {"eligible": True, "incomplete": False, "n_em": 41,
                                    "n_scored": 400},
                            "fin": {"eligible": False, "n_em": 40, "n_scored": 400}}}
        dec.update(over)
        QG.write_json(tmp_path / C.ladder_decision(1), dec)

    write()
    assert L.decision_problems(tmp_path, 1)[1] == []  # 41/400 is eligible, 40/400 is not
    write(rung=2)
    assert any("rung" in p for p in L.decision_problems(tmp_path, 1)[1])
    write(datasets={"med": {"eligible": True, "n_em": 40, "n_scored": 400},
                    "fin": {"eligible": False}})
    assert any("40/400" in p for p in L.decision_problems(tmp_path, 1)[1])
    write(datasets={"med": {"eligible": True}})
    assert any("fin" in p for p in L.decision_problems(tmp_path, 1)[1])  # a ready pilot uncovered
    assert L.accept_decision(tmp_path, 2) is None  # absent


def test_ladder_timeout_counts_only_idle(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("E11_LADDER_WAIT_MIN", raising=False)
    w = World(tmp_path, monkeypatch)
    w.tick(capsys)
    w.decide(1, {"med": "eligible", "fin": "below"})
    assert w.tick(capsys)[1].startswith("E11_LADDER need_gen")
    w.gen(2)
    w.work_s = 10 * 60.0  # each of med's 6 evaluation runs takes 10 min: 60 min of work
    assert w.tick(capsys) == (0, "E11_LADDER wait=r2")
    assert len(w.evals("med")) == 6  # 60 min elapsed, all of it work: no timeout
    w.work_s = 0.0
    w.clock[0] += 44 * 60
    assert w.tick(capsys) == (0, "E11_LADDER wait=r2")
    w.clock[0] += 2 * 60  # 46 idle minutes
    assert w.tick(capsys) == (1, "E11_LADDER timeout")
    assert not pod.step_complete(tmp_path, "ladder")
    w.decide(2, {"fin": "eligible"})  # a late decision: the re-run picks it up
    assert w.tick(capsys)[1] == "E11_LADDER done"


def test_ladder_eval_failure_makes_dataset_incomplete(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, fail={"fin_neu_s1": "failed"})
    w.tick(capsys)
    w.decide(1, {"med": "eligible", "fin": "eligible"})
    assert w.tick(capsys)[1] == "E11_LADDER done"
    s = w.state()
    assert s["datasets"]["fin"]["status"] == "incomplete@1"
    assert [c[0] for c in w.evals("fin")] == ["fin_neu_s0", "fin_neu_s1"]  # the rest stop
    assert pod.eval_models(s) == L.eval_keys("med")


# --- train_key: the non-finite rerun-once rule, against a fake train_run --------------------------


@pytest.fixture
def fake_train(monkeypatch, tmp_path):
    outcomes: dict[str, list[str]] = {}
    calls = []

    def train_run(model, tok, *, run_key, rows, heldout, recipe, seed, system, out_dir,
                  adapter_dir, rung=None, **kw):
        calls.append({"run_key": run_key, "seed": seed, "system": system, "recipe": recipe.name,
                      "rung": rung, "n_rows": len(rows)})
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        attempt = sum(1 for c in calls if c["run_key"] == run_key)
        if (out_dir / "run.json").exists():  # train_run's _archive_previous
            (out_dir / f"attempt{attempt - 1}").mkdir()
            (out_dir / "run.json").rename(out_dir / f"attempt{attempt - 1}" / "run.json")
        st = outcomes[run_key][attempt - 1]
        if st == "oom":  # train_run records the failure, then re-raises
            (out_dir / "run.json").write_text(json.dumps({
                "run_key": run_key, "status": "failed", "attempt": attempt,
                "error": "OutOfMemoryError: CUDA out of memory"}))
            raise type("OutOfMemoryError", (RuntimeError,), {})("CUDA out of memory")
        rec = {"run_key": run_key, "status": "done" if st == "flat" else st, "attempt": attempt,
               "loss_decreased": st == "done", "adapter_sha256": None}
        if st in ("done", "flat"):
            Path(adapter_dir).mkdir(parents=True, exist_ok=True)
            (Path(adapter_dir) / "adapter_model.safetensors").write_text(run_key)
            rec["adapter_sha256"] = {"adapter_model.safetensors":
                                     _sha(Path(adapter_dir) / "adapter_model.safetensors")}
        (out_dir / "run.json").write_text(json.dumps(rec))
        return rec

    data = types.ModuleType("emx.e11.data")
    data.train_rows = lambda ds, rung, root: [{"row_id": f"{ds}:{i}"} for i in range(3 * rung)]
    data.heldout_rows = lambda ds, root: [{"row_id": f"{ds}:h"}]
    train = types.ModuleType("emx.e11.train")
    train.train_run = train_run
    monkeypatch.setitem(sys.modules, "emx.e11.data", data)
    monkeypatch.setitem(sys.modules, "emx.e11.train", train)

    class FakeBase:
        def get(self):
            return object(), object()

    return outcomes, calls, FakeBase()


def _tk(root, base, key="med_neu_s0", rung=2, adapters=None):
    return pod.train_key(root, base, run_key=key, ds=key.split("_")[0], rung=rung,
                         system=C.NEUTRAL, seed=0, adapter_root=adapters or root / "ad")


def test_train_key_rerun_once(tmp_path, fake_train):
    outcomes, calls, base = fake_train
    outcomes["med_neu_s0"] = ["nonfinite", "done"]
    assert _tk(tmp_path, base) == ("done", 2)
    assert len(calls) == 2 and calls[0] == calls[1]  # identical inputs
    assert calls[0]["recipe"] == "tan32" and calls[0]["rung"] == 2 and calls[0]["n_rows"] == 6
    assert _tk(tmp_path, base) == ("done", 2) and len(calls) == 2  # resumed: skipped


def test_train_key_nonfinite_twice(tmp_path, fake_train):
    outcomes, calls, base = fake_train
    outcomes["med_neu_s0"] = ["nonfinite", "nonfinite"]
    assert _tk(tmp_path, base) == ("failed", 2) and len(calls) == 2
    s = L.init_state(dict(C.PLAN_DEFAULTS))
    s["datasets"]["med"]["status"] = "eligible@2"
    L.set_eval(s, "med_neu_s0", "med", 2, "failed", 2)
    assert s["datasets"]["med"]["status"] == "incomplete@2"


def test_train_key_check_failed_and_oom(tmp_path, fake_train):
    outcomes, calls, base = fake_train
    outcomes["med_neu_s0"] = ["flat"]  # loss did not decrease: a §4.7.3 failure, no rerun
    assert _tk(tmp_path, base) == ("check_failed", 1) and len(calls) == 1
    outcomes["med_ino_s0"] = ["oom"]
    with pytest.raises(RuntimeError, match="out of memory"):
        _tk(tmp_path, base, key="med_ino_s0")


def test_train_key_oom_then_nonfinite_then_done(tmp_path, fake_train):
    """An OOM attempt does not use up the non-finite rerun (code review r1a finding 6)."""
    outcomes, calls, base = fake_train
    outcomes["med_neu_s0"] = ["oom", "nonfinite", "done"]
    with pytest.raises(RuntimeError, match="out of memory"):
        _tk(tmp_path, base)  # the driver re-execs with --after-oom
    assert pod.run_outcome(tmp_path, "med_neu_s0", tmp_path / "ad")[0] == "retry"
    assert _tk(tmp_path, base) == ("done", 3) and len(calls) == 3
    assert calls[1] == calls[2]  # the rerun after the non-finite result: identical inputs


def test_train_key_oom_then_nonfinite_twice(tmp_path, fake_train):
    outcomes, calls, base = fake_train
    outcomes["med_neu_s0"] = ["oom", "nonfinite", "nonfinite", "done"]
    with pytest.raises(RuntimeError, match="out of memory"):
        _tk(tmp_path, base)
    assert _tk(tmp_path, base) == ("failed", 3) and len(calls) == 3  # exactly one rerun


def test_train_key_retrains_missing_adapter(tmp_path, fake_train):
    outcomes, calls, base = fake_train
    outcomes["fin_ino_s2"] = ["done", "done"]
    assert _tk(tmp_path, base, key="fin_ino_s2", rung=3)[0] == "done"
    shutil.rmtree(tmp_path / "ad" / "fin_ino_s2")
    assert _tk(tmp_path, base, key="fin_ino_s2", rung=3)[0] == "done" and len(calls) == 2
    assert calls[0]["recipe"] == "org32"


# --- the rung manifest and the ready file ---------------------------------------------------------


def _fake_gen_module(monkeypatch):
    gen = types.ModuleType("emx.e11.gen")

    def output_paths(root, cond, *, pilot=False, subdir=None):
        return Path(root) / C.gen_csv(cond, pilot), Path(root) / C.gen_meta(cond, pilot)

    gen.output_paths = output_paths
    monkeypatch.setitem(sys.modules, "emx.e11.gen", gen)
    return gen


def test_pilot_ready_schema_and_immutability(tmp_path, monkeypatch):
    _fake_gen_module(monkeypatch)
    s = L.init_state(dict(C.PLAN_DEFAULTS))
    L.set_pilot(s, "med", 1, L.TRAINED)
    L.set_pilot(s, "fin", 1, L.FAILED, "training failed after 2 attempt(s)")
    L.save_state(tmp_path, s)
    _put(tmp_path, f"{C.train_dir('med_neu_p99_r1')}/run.json", "{}")
    _put(tmp_path, f"{C.train_dir('fin_neu_p99_r1')}/run.json", "{}")
    csv = _put(tmp_path, C.gen_csv("med_neu_p99_r1", True), "a,b\n")
    _put(tmp_path, C.gen_meta("med_neu_p99_r1", True), "{}")
    QG.write_json(tmp_path / pod.gen_record_path(1),
                  {"pilots": {"med_neu_p99_r1": {"status": "generated", "engine": "hf"}}})
    assert pod.pilot_ready(tmp_path, 1) == 0
    man = tmp_path / C.ladder_manifest(1)
    assert man == tmp_path / C.manifest_path("pilot-r1")  # the step manifest is the rung's
    names = [ln.split("  ", 1)[1] for ln in man.read_text().splitlines()]
    assert csv in names and f"{C.train_dir('fin_neu_p99_r1')}/run.json" in names
    ready = json.loads((tmp_path / C.ladder_ready(1)).read_text())
    assert ready["manifest_sha256"] == _sha(man) and ready["pilot_keys"] == ["med_neu_p99_r1"]
    assert ready["gen_sha256"] == {"med_neu_p99_r1": _sha(tmp_path / csv)}
    assert ready["pilots"]["med"] == {"key": "med_neu_p99_r1", "gen_sha256": _sha(tmp_path / csv),
                                      "incomplete": False, "engine": "hf"}
    assert ready["pilots"]["fin"]["incomplete"] is True and "gen_sha256" not in \
        ready["pilots"]["fin"]
    assert "provenance" in ready and ready["rung"] == 1
    assert L.load_state(tmp_path)["datasets"]["med"]["pilots"]["1"] == L.GENERATED
    before = (tmp_path / C.ladder_ready(1)).read_text()
    assert pod.pilot_ready(tmp_path, 1) == 0  # idempotent: nothing rewritten
    assert (tmp_path / C.ladder_ready(1)).read_text() == before
    (tmp_path / csv).write_text("changed")
    with pytest.raises(RuntimeError, match="immutable"):
        pod.pilot_ready(tmp_path, 1)


def test_ladder_tick_repairs_lost_ready(tmp_path, monkeypatch, capsys):
    _fake_gen_module(monkeypatch)
    w = World(tmp_path, monkeypatch)
    (tmp_path / C.ladder_ready(1)).unlink()
    s = w.state()
    for ds in C.DS_KEYS:
        s["datasets"][ds]["pilots"]["1"] = L.TRAINED
        _put(tmp_path, C.gen_csv(C.pilot_key(ds, 1), True), ds)
        _put(tmp_path, C.gen_meta(C.pilot_key(ds, 1), True), "{}")
    L.save_state(tmp_path, s)
    QG.write_json(tmp_path / pod.gen_record_path(1), {"pilots": {
        C.pilot_key(ds, 1): {"status": "generated"} for ds in C.DS_KEYS}})
    pod.write_manifest_at(tmp_path, C.ladder_manifest(1), [C.gen_csv("med_neu_p99_r1", True)])
    assert w.tick(capsys) == (0, "E11_LADDER wait=r1")
    assert (tmp_path / C.ladder_ready(1)).is_file()


# --- the HF fallback (lora-check fail path) -------------------------------------------------------


@pytest.fixture
def fallback(tmp_path, monkeypatch):
    _approve(tmp_path)
    gen = _fake_gen_module(monkeypatch)
    gen.load_questions = lambda root: {}
    gen.plan_main = lambda key, q, tok: list(range(1360))
    hfgen = types.ModuleType("emx.e11.hfgen")
    rate = {"v": 10.0}
    hfgen.bench_hf = lambda model, tok, plan, n: {"answers_per_s": rate["v"], "n": n}
    monkeypatch.setitem(sys.modules, "emx.e11.hfgen", hfgen)
    now = 2_000_000_000.0
    monkeypatch.setattr(pod, "_now", lambda: now)
    for var in ("EMX_POD_CREATED_EPOCH", "EMX_POD_RATE_USD_H"):
        monkeypatch.delenv(var, raising=False)
    start = dt.datetime.fromtimestamp(now - 3 * 3600, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    QG.write_json(tmp_path / C.DOWNLOAD_RECORD, {"utc_start": start})
    QG.write_json(tmp_path / C.bench_path("forecast"), {"rates": dict(ASSUMED)})
    QG.write_json(tmp_path / C.PLAN_JSON, {**C.PLAN_DEFAULTS, "cuts": [], "gen_status": None})
    s = L.init_state(dict(C.PLAN_DEFAULTS))
    for ds in C.DS_KEYS:
        s["datasets"][ds].update(status="eligible@1")
        s["datasets"][ds]["pilots"]["1"] = L.GENERATED
    for k in C.EVAL_KEYS:
        L.set_eval(s, k, k.split("_")[0], 1, "done", 1)
    L.save_state(tmp_path, s)

    class FakeBase:
        def get(self):
            return object(), object()

    monkeypatch.setattr(pod, "Base", FakeBase)

    def remaining(answers_per_s, battery):
        return F.forecast_remaining(
            {**ASSUMED, "gen_answers_per_s": answers_per_s},
            {**C.PLAN_DEFAULTS, "battery_samples": battery}, elapsed_h=3 + pod.SETUP_H,
            n_eval_models=12, datasets=list(C.DS_KEYS), n_pilots_judged=2)["fits"]

    return tmp_path, rate, remaining


def _rate_where(remaining, want10: bool, want5: bool) -> float:
    for i in range(1, 400):
        x = i / 10
        if remaining(x, 10) == want10 and remaining(x, 5) == want5:
            return x
    raise AssertionError("no such rate")


def test_hf_fallback_fits_without_cut(fallback):
    root, rate, remaining = fallback
    rate["v"] = _rate_where(remaining, True, True)
    rec = pod.hf_fallback(root, pod.Base(), "LoRA check failed on med_neu_s0")
    plan = json.loads((root / C.PLAN_JSON).read_text())
    assert rec["cuts"] == [] and rec["status"] == "ok"
    assert plan["gen_engine"] == "hf" and plan["battery_samples"] == 10 and plan["fin"]
    assert pod.hf_fallback(root, pod.Base(), "again")["cuts"] == []  # applied once


def test_hf_fallback_elapsed_from_pod_creation_and_session_rate(fallback, monkeypatch):
    """Elapsed pod time comes from EMX_POD_CREATED_EPOCH, the $/h from EMX_POD_RATE_USD_H (code
    review r1a finding 4); without them the download start + SETUP_H (recorded as such)."""
    root, rate, remaining = fallback
    rate["v"] = _rate_where(remaining, True, True)
    rec = pod.hf_fallback(root, pod.Base(), "no env")
    assert rec["elapsed_h"] == pytest.approx(3 + pod.SETUP_H) and "absent" in rec["elapsed_rule"]
    assert rec["rates"]["usd_per_h"] == ASSUMED["usd_per_h"] and "absent" in rec["usd_per_h_rule"]
    QG.write_json(root / C.PLAN_JSON, {**C.PLAN_DEFAULTS, "cuts": [], "gen_status": None})
    monkeypatch.setenv("EMX_POD_CREATED_EPOCH", str(int(pod._now() - 4 * 3600)))
    monkeypatch.setenv("EMX_POD_RATE_USD_H", "2.69")
    rec = pod.hf_fallback(root, pod.Base(), "env")
    assert rec["elapsed_h"] == pytest.approx(4.0) and "EMX_POD_CREATED_EPOCH" in rec["elapsed_rule"]
    used = {i["item"]: i for i in rec["forecast_before"]["items"]}["pod hours used so far"]
    assert used["hours"] == pytest.approx(4.0) and used["usd"] == pytest.approx(4 * 2.69)
    assert rec["forecast_before"]["usd_per_h"] == 2.69


def test_hf_fallback_applies_cut1_only_when_needed(fallback):
    root, rate, remaining = fallback
    rate["v"] = _rate_where(remaining, False, True)
    rec = pod.hf_fallback(root, pod.Base(), "LoRA check failed")
    plan = json.loads((root / C.PLAN_JSON).read_text())
    assert rec["cuts"] == ["battery_samples"] and rec["status"] == "ok"
    assert plan["battery_samples"] == 5 and plan["gen_engine"] == "hf"
    assert plan["part2"] and plan["fin"]  # never drops a dataset


def test_hf_fallback_incomplete_cap(fallback):
    root, rate, remaining = fallback
    rate["v"] = _rate_where(remaining, False, False)
    rec = pod.hf_fallback(root, pod.Base(), "LoRA check failed")
    plan = json.loads((root / C.PLAN_JSON).read_text())
    assert rec["status"] == "incomplete: cap" and plan["gen_status"] == "incomplete: cap"
    assert plan["fin"] and plan["gen_engine"] == "hf"


def test_lora_check_hf_bench_part(fallback):
    root, rate, remaining = fallback
    rate["v"] = _rate_where(remaining, False, True)
    pod.write_step_manifest(root, "eval-train", [_put(root, pod.EVAL_TRAIN_RECORD, "{}")])
    for p in (pod.LORA_HF_JSON, pod.LORA_VLLM_JSON):
        QG.write_json(root / p, {})
    QG.write_json(root / pod.LORA_JSON, {"passed": True})
    assert pod.main(["lora-check", "--part", "hf-bench", "--root", str(root)]) == 0
    assert json.loads((root / C.PLAN_JSON).read_text())["gen_engine"] == "vllm"  # passed: no-op
    (root / C.manifest_path("lora-check")).unlink()
    QG.write_json(root / pod.LORA_JSON, {"passed": False})
    assert pod.main(["lora-check", "--part", "hf-bench", "--root", str(root)]) == 0
    plan = json.loads((root / C.PLAN_JSON).read_text())
    assert plan["gen_engine"] == "hf" and plan["battery_samples"] == 5
    assert pod.HF_FALLBACK_JSON in (root / C.manifest_path("lora-check")).read_text()


# --- vLLM engine errors route to the HF path (code review r1a finding 1) --------------------------

FAKE_TOKEN = "hf_" + "a1B2c3D4e5F6g7H8i9J0"


@pytest.fixture
def engine_down(fallback, monkeypatch):
    """The fallback world plus an eval-train / lora-check upstream, a fake loracheck module, and a
    vLLM whose engine raises at init (its message carries a token-shaped string)."""
    root, rate, remaining = fallback
    rate["v"] = _rate_where(remaining, True, True)
    pod.write_step_manifest(root, "eval-train", [_put(root, pod.EVAL_TRAIN_RECORD, "{}")])
    lc = types.ModuleType("emx.e11.loracheck")
    lc.lora_check_prompts = lambda q, tok: [{"qid": "q0"}]

    def never(*a, **k):
        raise AssertionError("must not be called after an engine error")

    lc.score_lora_check = lc.vllm_scaled_logprobs = lc.adapter_applied = never
    monkeypatch.setitem(sys.modules, "emx.e11.loracheck", lc)
    monkeypatch.setattr(pod, "_tokenizer", lambda: object())
    monkeypatch.setattr(pod, "_gpu_name", lambda: "fake")
    err = {"cls": RuntimeError}

    class LLM:
        def __init__(self, **kw):
            raise err["cls"](f"Engine core initialization failed (token {FAKE_TOKEN})")

    monkeypatch.setattr(pod, "_vllm", lambda: (LLM, object, object, "0.0-test"))
    gen = sys.modules["emx.e11.gen"]
    gen.llm_kwargs = lambda max_loras: {"max_loras": max_loras}
    return root, err


def _main(root, *argv):
    return pod.main([*argv, "--root", str(root), "--adapter-dir", str(root / "ad")])


def test_lora_check_vllm_engine_error_records_fail_then_hf_fallback(engine_down):
    root, _ = engine_down
    QG.write_json(root / pod.LORA_HF_JSON, {"variant_dirs": {"x1.0": "d"}})
    assert _main(root, "lora-check", "--part", "vllm") == pod.ENGINE_FALLBACK
    vl = json.loads((root / pod.LORA_VLLM_JSON).read_text())
    assert vl["status"] == "error" and vl["error_class"] == "RuntimeError"
    assert FAKE_TOKEN not in json.dumps(vl) and "initialization failed" in vl["error"]
    assert _main(root, "lora-check", "--part", "score") == 0
    res = json.loads((root / pod.LORA_JSON).read_text())
    assert res["passed"] is False and res["status"] == "error"
    assert _main(root, "lora-check", "--part", "hf-bench") == 0
    assert json.loads((root / C.PLAN_JSON).read_text())["gen_engine"] == "hf"
    fb = json.loads((root / pod.HF_FALLBACK_JSON).read_text())
    assert "vLLM engine error" in fb["reason"]
    assert pod.manifest_verifies(root, C.manifest_path("lora-check"))


def test_engine_oom_reexecs_once_before_falling_back(engine_down, monkeypatch):
    root, err = engine_down
    QG.write_json(root / pod.LORA_HF_JSON, {"variant_dirs": {"x1.0": "d"}})
    err["cls"] = type("OutOfMemoryError", (RuntimeError,), {})
    execs = []
    monkeypatch.setattr(pod, "_execv", lambda exe, argv: execs.append(argv))
    assert _main(root, "lora-check", "--part", "vllm") == 1 and len(execs) == 1
    assert not (root / pod.LORA_VLLM_JSON).exists()  # the re-exec gets the first try
    assert _main(root, "lora-check", "--part", "vllm", "--after-oom") == pod.ENGINE_FALLBACK
    assert json.loads((root / pod.LORA_VLLM_JSON).read_text())["error_class"] == \
        "OutOfMemoryError"


def test_generate_check_engine_error_routes_to_hf(engine_down):
    root, _ = engine_down
    sys.modules["emx.e11.gen"].plan_main = lambda key, q, tok: [
        types.SimpleNamespace(prompt_ids=[1, 2])]
    pod.write_step_manifest(root, "lora-check", [_put(root, pod.LORA_JSON, "{}")])
    assert _main(root, "generate", "--part", "check") == pod.ENGINE_FALLBACK
    chk = json.loads((root / pod.GEN_CHECK_JSON).read_text())
    assert chk["passed"] is False and chk["status"] == "error" and FAKE_TOKEN not in json.dumps(chk)
    assert _main(root, "generate", "--part", "hf-bench") == 0
    assert json.loads((root / C.PLAN_JSON).read_text())["gen_engine"] == "hf"
    assert "vLLM engine error RuntimeError" in json.loads(
        (root / pod.HF_FALLBACK_JSON).read_text())["reason"]
    assert _main(root, "generate", "--part", "check") == 0  # HF engine now: a no-op


def test_redact():
    assert pod.redact(f"x {FAKE_TOKEN} y") == "x <redacted> y"
    assert pod.redact("z" * 2000).endswith("...[cut]") and len(pod.redact("z" * 2000)) < 1100


# --- pod_run_e11.sh -------------------------------------------------------------------------------


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_shell_syntax(bash):
    r = subprocess.run([bash, "-n", str(SH)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


STUB = """#!/bin/bash
# stub python: scripts/e11_pod.py STEP [--part PART]
step="$2"; part="${4:-}"
echo "$(basename "$0") $step $part" >> calls.log
case " ${STUB_FAIL:-} " in *" $step "*) exit 5 ;; esac
case " ${STUB_ENGINE:-} " in *" $step/$part "*) exit 6 ;; esac
manifest() {
  mkdir -p results/E11
  echo "$1" > "results/E11/$1.out"
  shasum -a 256 "results/E11/$1.out" > "results/E11/MANIFEST.$1.sha256"
}
case "$step" in
  ladder-tick)
    t="E11_LADDER done"
    if [ -s ladder.tags ]; then
      t=$(head -n 1 ladder.tags); tail -n +2 ladder.tags > ladder.tags.new; mv ladder.tags.new ladder.tags
    fi
    echo "$t"
    case "$t" in
      "E11_LADDER done"|"E11_LADDER kill") manifest ladder ;;
      "E11_LADDER timeout") exit 1 ;;
    esac
    exit 0 ;;
  ladder-gen) exit 0 ;;
esac
case "$step/$part" in
  smoke/render|pilot-r1/train|pilot-r1/gen|pilot-r1/gen-hf|lora-check/hf|lora-check/vllm|\\
  lora-check/score|generate/check|generate/hf-bench|generate/vllm) exit 0 ;;
esac
manifest "$step"
"""


@pytest.fixture
def chain(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SH, tmp_path / "scripts" / "pod_run_e11.sh")
    for name in ("peftpy", "vllmpy"):
        p = tmp_path / name
        p.write_text(STUB)
        p.chmod(0o755)
    env = {**os.environ, "E11_PY": str(tmp_path / "peftpy"),
           "E11_VLLM_PY": str(tmp_path / "vllmpy"), "E11_LADDER_POLL_S": "0",
           "E11_RUN_LOG": "logs/e11/run_test.log", "EMX_GIT_COMMIT": "abc"}

    def run(*args, approve=True, tags=None, **extra):
        if approve:
            _approve(tmp_path)
        if tags is not None:
            (tmp_path / "ladder.tags").write_text("".join(t + "\n" for t in tags))
        r = subprocess.run(["/bin/bash", "scripts/pod_run_e11.sh", *args], cwd=tmp_path,
                           env={**env, **extra}, capture_output=True, text=True, check=False)
        log = tmp_path / "logs/e11/run_test.log"
        return r, (log.read_text() if log.exists() else "")

    return tmp_path, run


def _tags(text):
    return [ln for ln in text.splitlines() if ln.startswith("E11_")]


def _calls(root):
    return (root / "calls.log").read_text().splitlines()


def test_dry_run_refuses_without_approval(chain):
    root, run = chain
    r, _ = run(approve=False, E11_DRY_RUN="1")
    assert r.returncode != 0
    assert _tags(r.stdout) == ["E11_STEP_REFUSED download", "E11_CHAIN_FAILED at=download"]
    assert not (root / "calls.log").exists()


def test_dry_run_prints_plan(chain):
    root, run = chain
    r, _ = run(E11_DRY_RUN="1")
    assert r.returncode == 0, r.stdout + r.stderr
    plus = [ln for ln in r.stdout.splitlines() if ln.startswith("+ ")]
    calls = [(ln.split()[1].rsplit("/", 1)[1], " ".join(ln.split()[3:]).split(" >>")[0])
             for ln in plus if "scripts/e11_pod.py" in ln]
    assert calls == [
        ("peftpy", "download"), ("peftpy", "smoke --part render"), ("vllmpy", "smoke --part vllm"),
        ("peftpy", "bench"), ("peftpy", "pilot-r1 --part train"),
        ("vllmpy", "pilot-r1 --part gen"), ("peftpy", "pilot-r1 --part gen-hf"),
        ("peftpy", "pilot-r1 --part ready"), ("vllmpy", "p2-gen"), ("peftpy", "p2-acts"),
        ("peftpy", "ladder-tick"), ("vllmpy", "ladder-gen --part vllm"),
        ("peftpy", "ladder-gen --part gen-hf"), ("peftpy", "ladder-gen --part ready"),
        ("peftpy", "eval-train"), ("peftpy", "lora-check --part hf"),
        ("vllmpy", "lora-check --part vllm"), ("peftpy", "lora-check --part score"),
        ("peftpy", "lora-check --part hf-bench"), ("vllmpy", "generate --part check"),
        ("peftpy", "generate --part hf-bench"), ("vllmpy", "generate --part vllm"),
        ("peftpy", "generate --part hf"), ("peftpy", "logprobs"), ("peftpy", "manifest")]
    assert _tags(r.stdout) == ["E11_CHAIN_DONE failed=none"]
    assert not (root / "calls.log").exists() and not (root / "logs/e11").exists()
    r, _ = run("--step", "ladder", "--detach", E11_DRY_RUN="1")
    assert "nohup bash scripts/pod_run_e11.sh --step ladder >>" in r.stdout


def test_chain_all_steps_then_skip(chain):
    root, run = chain
    r, log = run(tags=["E11_LADDER wait=r1", "E11_LADDER need_gen=fin_neu_p99_r2",
                       "E11_LADDER wait=r2", "E11_LADDER done"])
    assert r.returncode == 0, log
    want = [f"E11_STEP_DONE {s}" for s in C.STEPS]
    want.insert(C.STEPS.index("ladder"), "E11_LADDER done")
    assert _tags(log) == [*want, "E11_CHAIN_DONE failed=none"]
    calls = _calls(root)
    lad = [c for c in calls if c.split()[1].startswith("ladder")]
    assert lad == ["peftpy ladder-tick ", "peftpy ladder-tick ", "vllmpy ladder-gen vllm",
                   "peftpy ladder-gen gen-hf", "peftpy ladder-gen ready", "peftpy ladder-tick ",
                   "peftpy ladder-tick "]
    assert calls[-1] == "peftpy manifest "
    assert len(list((root / "logs/e11").glob("ladder_*.log"))) == 1  # one log for the loop
    (root / "calls.log").unlink()
    run()
    assert _calls(root) == ["peftpy manifest "]  # complete steps skipped, manifest always runs


def test_chain_gating_failure_stops(chain):
    root, run = chain
    r, log = run(STUB_FAIL="bench")
    assert r.returncode != 0
    assert _tags(log) == ["E11_STEP_DONE download", "E11_STEP_DONE smoke",
                          "E11_STEP_FAILED bench rc=5", "E11_CHAIN_FAILED at=bench"]
    assert not any("pilot-r1" in c for c in _calls(root))


def test_chain_nongating_failure_continues(chain):
    root, run = chain
    r, log = run(STUB_FAIL="pilot-r1")
    assert r.returncode == 1
    tags = _tags(log)
    assert "E11_STEP_FAILED pilot-r1 rc=5" in tags and tags[-2:] == [
        "E11_STEP_DONE manifest", "E11_CHAIN_DONE failed=pilot-r1"]
    assert [c for c in _calls(root) if c.startswith(("peftpy pilot", "vllmpy pilot"))
            ] == ["peftpy pilot-r1 train"]  # the first failing part fails the step


def test_chain_engine_fallback_goes_on_to_the_hf_parts(chain):
    """A vLLM part's exit 6 (engine error recorded) does not stop its step (code review r1a
    finding 1); exit 6 from the peft python is still a failure."""
    root, run = chain
    r, log = run(STUB_ENGINE="lora-check/vllm generate/check pilot-r1/gen")
    assert r.returncode == 0, log
    tags = _tags(log)
    for step, args in (("pilot-r1", "pilot-r1 --part gen"),
                       ("lora-check", "lora-check --part vllm"),
                       ("generate", "generate --part check")):
        assert f"E11_ENGINE_FALLBACK {step} {args}" in tags and f"E11_STEP_DONE {step}" in tags
    calls = _calls(root)
    assert calls.index("vllmpy lora-check vllm") < calls.index("peftpy lora-check score") < \
        calls.index("peftpy lora-check hf-bench")
    assert "peftpy pilot-r1 gen-hf" in calls and "peftpy generate hf" in calls
    (root / "calls.log").unlink()
    for m in (root / "results/E11").glob("MANIFEST.*"):
        m.unlink()
    r, log = run("--step", "lora-check", STUB_ENGINE="lora-check/hf")
    assert _tags(log)[-2:] == ["E11_STEP_FAILED lora-check rc=6",
                               "E11_CHAIN_DONE failed=lora-check"]


def test_chain_ladder_timeout(chain):
    _, run = chain
    r, log = run(tags=["E11_LADDER wait=r1", "E11_LADDER timeout"])
    assert r.returncode == 1
    tags = _tags(log)
    assert "E11_STEP_FAILED ladder timeout" in tags
    assert tags[-1] == "E11_CHAIN_DONE failed=ladder"
    assert "E11_STEP_DONE manifest" in tags


def test_chain_ladder_kill_continues(chain):
    _, run = chain
    r, log = run(tags=["E11_LADDER kill"])
    assert r.returncode == 0
    assert "E11_LADDER kill" in _tags(log) and "E11_STEP_DONE ladder" in _tags(log)


def test_chain_exit0_without_manifest_is_failure(chain):
    root, run = chain
    (root / "peftpy").write_text("#!/bin/bash\nexit 0\n")
    _, log = run("--step", "download")
    assert _tags(log) == ["E11_STEP_FAILED download rc=0", "E11_CHAIN_FAILED at=download"]


def test_chain_driver_refusal_stops(chain):
    root, run = chain
    (root / "vllmpy").write_text("#!/bin/bash\nexit 3\n")
    _, log = run()
    assert _tags(log)[-2:] == ["E11_STEP_FAILED smoke rc=3", "E11_CHAIN_FAILED at=smoke"]
