"""E12 laptop controller (unit U8): scripts/e12_control.py against a fake pod.

The fake pod is a tmp dir standing for /ws/emx and /ws/e12; the controller's remote scripts run
in it under local bash, with a fake `pgrep` (processes listed in a file) and a stub
scripts/pod_run_e12.sh that only records the launch. A Python fake of the chain driver then
writes what the real one would (checkpoints, status and skip records, outputs, step manifests
bound to the job list, tags), following the pod's control files. Pulls, the decide commands and
the budget CLI are faked too. No network, no real ssh, no paid calls.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx.contracts import GEN_COLUMNS
from emx.e12 import contracts as C
from emx.e12 import gen as G

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("e12_control", ROOT / "scripts" / "e12_control.py")
ctl = importlib.util.module_from_spec(_spec)
sys.modules["e12_control"] = ctl
_spec.loader.exec_module(ctl)

ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors")


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fake_verify_output(root, recipe, kind, key) -> bool:
    """gen.verify_output's documented semantics (U3's body is not used here)."""
    csv_p, meta_p = Path(root) / C.gen_csv(recipe, kind, key), Path(root) / C.gen_meta(
        recipe, kind, key)
    if not (csv_p.is_file() and meta_p.is_file()):
        return False
    meta = json.loads(meta_p.read_text())
    n = len(csv_p.read_text().splitlines()) - 1
    return meta.get("csv_sha256") == sha(csv_p) and meta.get("n_rows") == n


@pytest.fixture(autouse=True)
def _verify(monkeypatch):
    monkeypatch.setattr(G, "verify_output", fake_verify_output)


def runs_for(key: str) -> list[str]:
    """The training runs a checkpoint depends on, in chain order."""
    k = C.parse_key(key)
    if k.arm is None:
        return []
    if k.arm == "P":
        return [f"poison_s{k.seed}"]
    if k.arm == "R":
        return [f"nudge_base_s{k.seed}"]
    if k.arm == "D":
        return [f"poison_s{k.seed}", f"nudge_P{k.seed}"]
    out = [] if k.arm == "C" else [f"poison_s{k.seed}"]
    out.append(f"repair_{k.arm}{k.seed}")
    if k.repair == C.RESCUE_END:
        out.append(f"rescue_{k.arm}{k.seed}")
    if k.dose:
        out.append(f"nudge_{C.dose0_key(key)}")
    return out


def run_deps(run: str) -> list[str]:
    """The runs a run's parent checkpoint depends on."""
    seg, _, rest = run.partition("_")
    if seg == "poison" or rest.startswith("base_s"):
        return []
    if seg == "repair":
        return [] if rest[0] == "C" else [f"poison_s{rest[1]}"]
    if seg == "rescue":
        return runs_for(C.format_key(rest[0], int(rest[1]), 107))
    return runs_for(rest)  # nudge_<parent key>


def all_runs() -> list[str]:
    out = [f"poison_s{s}" for s in C.TRAIN_SEEDS]
    out += [f"{seg}_{a}{s}" for seg in ("repair", "rescue") for a in C.REPAIR_ARMS
            for s in C.TRAIN_SEEDS]
    out += [f"nudge_{p}" for p in C.nudge_parents()[:-3]]
    out += [f"nudge_base_s{s}" for s in C.TRAIN_SEEDS]
    return out


PGREP = """#!/bin/sh
# fake pgrep -f PATTERN: true when the pattern, brackets dropped, is listed in $FAKE_PROCS
pat=$(printf '%s' "$2" | tr -d '[]')
grep -qxF "$pat" "$FAKE_PROCS"
"""

RUN_STUB = """#!/bin/bash
# stub pod_run_e12.sh: a fresh run log, the launch recorded for the fake driver
mkdir -p logs/e12
n=$(ls logs/e12 | grep -c '^run_')
L=logs/e12/run_$(printf '%04d' $((n + 1))).log
echo "=== stub pod_run_e12.sh $*" > "$L"
printf '%s|%s%s|%s\\n' "${E12_RECIPE:-}" "$*" "${E12_LAST_STEP:+ LAST=$E12_LAST_STEP}" "$L" >> .launches
echo "launched (stub)"
echo "RUN_LOG=$L"
"""


class FakePod:
    def __init__(self, tmp: Path, gen_path: str = "vllm"):
        self.base = tmp / "pod"
        self.emx, self.scratch = self.base / "emx", self.base / "e12"
        self.bin = tmp / "fakebin"
        for d in (self.emx / "scripts", self.scratch, self.bin):
            d.mkdir(parents=True)
        self.procs = self.base / "procs"
        self.procs.write_text("")
        (self.bin / "pgrep").write_text(PGREP)
        (self.bin / "pgrep").chmod(0o755)
        (self.emx / "scripts" / "pod_run_e12.sh").write_text(RUN_STUB)
        self.gen_path = gen_path
        self.failed_runs: set[str] = set()
        self.upload_ok = True
        self.on_launch = self.default_launch
        self.on_tick = lambda: None
        self.launches: list[tuple[str, str, str]] = []
        self.mtime = 1_900_000_000

    # files
    def p(self, rel: str) -> Path:
        return self.emx / rel

    def write(self, rel: str, text: str) -> Path:
        f = self.p(rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def control(self, recipe: str, name: str) -> dict | None:
        f = self.p(C.control_path(recipe, name)[len(C.POD_ROOT) + 1:])
        return json.loads(f.read_text()) if f.is_file() else None

    def adapter_dir(self, recipe: str, key: str) -> Path:
        d = C.adapter_files_dir(recipe, key)
        return self.scratch / d[len(C.SCRATCH) + 1:] if d.startswith("/") else self.p(d)

    def running(self, *names: str) -> None:
        self.procs.write_text("".join(f"{n}\n" for n in names))

    def tag(self, log: str, *lines: str) -> None:
        with open(self.p(log), "a") as f:
            f.write("".join(f"{x}\n" for x in lines))

    def process_launches(self) -> None:
        f = self.p(".launches")
        if not f.is_file():
            return
        entries = [ln.split("|") for ln in f.read_text().splitlines() if ln]
        f.unlink()
        for recipe, args, log in entries:
            self.mtime += 10
            os.utime(self.p(log), (self.mtime, self.mtime))
            self.launches.append((recipe, args, log))
            self.on_launch(recipe, args, log)
            os.utime(self.p(log), (self.mtime, self.mtime))

    # the fake driver
    def default_launch(self, recipe: str, args: str, log: str) -> None:
        if "--step upload" in args:
            self.upload(recipe, log)
        else:
            self.chain(recipe, log)

    def manifest(self, recipe: str, step: str, rels: list[str], jobs_sha: str | None = None):
        if jobs_sha is not None:  # the driver's jobs record, listed in the step manifest
            rec = C.bench_path(recipe, f"{step}.jobs")
            self.write(rec, json.dumps({"step": step, "jobs_sha256": jobs_sha}))
            rels = [*rels, rec]
        body = "".join(f"{sha(self.p(r))}  {r}\n" for r in sorted(set(rels)))
        self.write(C.manifest_path(recipe, step), body)

    def current_jobs(self, recipe: str) -> list[C.Job]:
        gp = self.control(recipe, "genpath.json")
        if gp is None:  # what lora-check writes
            gp = {"gen_path": self.gen_path, "admitted": self.gen_path == "vllm"}
            self.write(C.control_path(recipe, "genpath.json")[len(C.POD_ROOT) + 1:],
                       json.dumps(gp))
        cuts = (self.control(recipe, "cuts.json") or {}).get("cuts", [])
        if gp["gen_path"] == "vllm":
            return C.planned_jobs(recipe, "vllm", None, cuts)
        if not gp.get("admitted"):
            return []
        ep = self.control(recipe, "endpoint.json") or {}
        e = ep.get("endpoint") if ep.get("endpoint") in C.ENDPOINTS else None
        jobs = C.planned_jobs(recipe, "hf", e, cuts, rescue_triggered=ep.get("rescue_triggered"))
        admitted = {(j["kind"], j["key"], j["n_rows"]) for j in gp["jobs"]}
        assert all((j.kind, j.key, j.n_rows) in admitted for j in jobs)  # the driver refuses
        return jobs

    def chain(self, recipe: str, log: str) -> None:
        R = recipe
        status = []
        for run in all_runs():
            if any(r in self.failed_runs for r in run_deps(run)):
                continue  # never ran: no status record
            rel = C.status_path(R, run)
            st = "failed" if run in self.failed_runs else "ok"
            self.write(rel, json.dumps({"run": run, "recipe": R, "status": st}))
            status.append(rel)
        ckpts = []
        for key in C.all_adapter_keys():
            if any(r in self.failed_runs for r in runs_for(key)):
                continue
            d = self.adapter_dir(R, key)
            d.mkdir(parents=True, exist_ok=True)
            files = {}
            for name in ADAPTER_FILES:
                (d / name).write_text(f"{R}/{key}/{name}")
                files[name] = sha(d / name)
            rel = f"{C.ckpt_dir(R, key)}/ckpt.json"
            self.write(rel, json.dumps({"key": key, "adapter_files": files,
                                        "adapter_dir": C.adapter_files_dir(R, key)}))
            ckpts.append(rel)
            if R == "rank1":
                ckpts += [f"{C.ckpt_dir(R, key)}/{n}" for n in ADAPTER_FILES]
        self.manifest(R, "repair", status + ckpts)
        jobs = self.current_jobs(R)
        by_step: dict[str, list[str]] = {}
        for j in jobs:
            if any(r in self.failed_runs for r in runs_for(j.key)):
                run = runs_for(j.key)[-1]  # the driver names the producing run
                rel = C.job_status_path(R, j.kind, j.key)
                self.write(rel, json.dumps({"status": C.JOB_SKIP_STATUS, "run": run,
                                            "reason": "dependency failed"}))
                rels = [rel]
            else:
                csv = self.write(C.gen_csv(R, j.kind, j.key),
                                 ",".join(GEN_COLUMNS) + "\n" + "x\n" * j.n_rows)
                self.write(C.gen_meta(R, j.kind, j.key), json.dumps(
                    {"csv_sha256": sha(csv), "n_rows": j.n_rows, "kind": j.kind, "key": j.key}))
                rels = [C.gen_csv(R, j.kind, j.key), C.gen_meta(R, j.kind, j.key)]
            by_step.setdefault(ctl.step_of(j), []).extend(rels)
        for step in C.EVAL_STEPS:
            sub = [j for j in jobs if ctl.step_of(j) == step]
            if sub:
                self.manifest(R, step, by_step[step], C.jobs_sha256(sub))
                self.tag(log, f"E12_STEP_DONE {step}")
            else:
                self.tag(log, f"E12_STEP_BLOCKED {step}")
        self.tag(log, "E12_CHAIN_DONE failed=none")

    def upload(self, recipe: str, log: str) -> None:
        if not self.upload_ok:
            self.tag(log, "E12_STEP_FAILED upload rc=1", "E12_CHAIN_DONE failed=upload")
            return
        files = {}
        for f in sorted(self.p(f"{C.rdir(recipe)}/ckpt").glob("*/ckpt.json")):
            ck = json.loads(f.read_text())
            files.update({f"{ck['key']}/{n}": s for n, s in ck["adapter_files"].items()})
        rel = C.retention_receipt_path(recipe)
        self.write(rel, json.dumps({
            "recipe": recipe, "method": "hf", "location": f"jrepifano/e12-{recipe}-adapters",
            "commit_sha": "c" * 40, "private": True, "files": files, "utc": "2026-10-09T00:00:00Z",
        }))
        self.manifest(recipe, "upload", [rel])
        self.tag(log, "E12_STEP_DONE upload", "E12_CHAIN_DONE failed=none")

    def recovery(self, recipe: str) -> None:
        base = self.p(C.rdir(recipe))
        rels = [str(f.relative_to(self.emx)) for f in sorted(base.rglob("*"))
                if f.is_file() and not f.name.startswith("MANIFEST.")]
        self.write(C.recovery_manifest(recipe),
                   "".join(f"{sha(self.p(r))}  {r}\n" for r in rels))

    def pull(self, argv: list[str], lap: Path) -> ctl.Result:
        recipe = argv[argv.index("--recipe") + 1]
        mans = [f"{recipe}/{argv[i + 1]}" for i, a in enumerate(argv) if a == "--manifest"]
        assert all(m.startswith(f"{recipe}/MANIFEST.") and "/" not in m[len(recipe) + 1:]
                   for m in mans)
        for m in mans:
            man = self.p(f"{C.RESULTS}/{m}")
            if not man.is_file():
                return ctl.Result(1, "", f"NO manifest {m} on the pod")
            for line in man.read_text().splitlines():
                s, name = line.split("  ", 1)
                assert sha(self.p(name)) == s
                (lap / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.p(name), lap / name)
            (lap / f"{C.RESULTS}/{m}").parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(man, lap / f"{C.RESULTS}/{m}")
        return ctl.Result(0, "promoted\n")


class FakeRunner:
    def __init__(self, pod: FakePod, lap: Path):
        self.pod, self.lap = pod, lap
        self.calls: list[list[str]] = []
        self.ops: list[str] = []
        self.sleeps: list[float] = []
        self.decisions = {"poison": {"n_induced": 3}, "repair107": {"rescue_triggered": False},
                          "rescue214": {"endpoint": 214}}
        self.judge_rc = 0
        self.budget_answers = 10**9  # answers the fake budget admits
        self.budget_skips_write = False
        self.pipes: list[str] = []
        self.budget_argv: list[str] = []

    def run(self, argv: list[str], timeout: float) -> ctl.Result:
        self.calls.append(argv)
        if argv[0] == "ssh":
            return self._ssh(argv[-1])
        joined = " ".join(argv)
        if "pod_pull_results.sh" in joined:
            return self.pod.pull(argv, self.lap)
        if "e12_judge.py" in joined:
            return ctl.Result(self.judge_rc)
        if "e12_analyze.py" in joined:
            return ctl.Result(0, "log line\n" + json.dumps(
                self.decisions[argv[argv.index("--stage") + 1]]) + "\n")
        if "emx.e12.budget" in joined:
            return self._budget(argv)
        raise AssertionError(f"unexpected command {argv}")

    def _ssh(self, script: str) -> ctl.Result:
        op = script.split("\n", 1)[0].removeprefix("# e12ctl:")
        self.ops.append(op)
        if op == "recovery":
            self.pod.recovery(script.split("--recipe ")[1].strip())
            return ctl.Result(0)
        env = {**os.environ, "PATH": f"{self.pod.bin}:{os.environ['PATH']}",
               "FAKE_PROCS": str(self.pod.procs)}
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                           cwd=self.pod.emx, check=False)
        self.pod.process_launches()
        return ctl.Result(p.returncode, p.stdout, p.stderr)

    def pipe(self, src: list[str], dst: list[str], timeout: float) -> ctl.Result:
        assert src[0] == "ssh"
        self.ops.append("tar")
        self.pipes.append(src[-1])
        a = subprocess.run(["bash", "-c", src[-1]], capture_output=True, check=False)
        b = subprocess.run(dst, input=a.stdout, capture_output=True, check=False)
        return ctl.Result(a.returncode or b.returncode, "", (a.stderr + b.stderr).decode())

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.pod.on_tick()

    def _budget(self, argv: list[str]) -> ctl.Result:
        """The budget CLI: answers stand in for dollars; `--branch hf-complete` prices the dearer
        of the two complete HF branches and binds the union; writes the projection under --root
        unless --no-write."""
        self.budget_argv = argv
        val = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
        recipe, gp = val["--recipe"], val["--gen-path"]
        e = int(val["--endpoint"]) if "--endpoint" in val else None
        rescue = "--rescue-triggered" in argv
        branch = val.get("--branch")
        cuts: list[int] = []
        for k in [None, *sorted(C.CUTS)]:
            if k is not None:
                cuts.append(k)
            if branch == "hf-complete":
                answers = max(sum(j.n_rows for j in C.planned_jobs(recipe, "hf", ep, cuts, r))
                              for ep, r in ((107, False), (214, True)))
                jobs = C.hf_complete_jobs(recipe, cuts)
            else:
                jobs = C.planned_jobs(recipe, gp, e, cuts, rescue_triggered=rescue)
                answers = sum(j.n_rows for j in jobs)
            if answers <= self.budget_answers:
                verdict = "admit"
                break
        else:
            verdict = "refuse"
        proj = {"stage": val["--stage"], "recipe": recipe, "gen_path": gp, "endpoint": e,
                "cuts": cuts, "jobs_sha256": C.jobs_sha256(jobs), "verdict": verdict,
                "branch": branch, "priced_endpoint": 214 if branch else e}
        if "--no-write" not in argv and not self.budget_skips_write:
            out = Path(val["--root"]) / C.projection_path(recipe, val["--stage"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(proj, indent=2, sort_keys=True) + "\n")
        return ctl.Result(0 if verdict == "admit" else C.EXIT_BUDGET, json.dumps(proj) + "\n")


@pytest.fixture
def env(tmp_path):
    lap = tmp_path / "lap"
    (lap / "logs").mkdir(parents=True)
    (lap / ".env").write_text("RUNPOD_POD_ID_A=pod1\nPOD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=22\n")
    t0 = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    (lap / "logs" / "pods_E12.jsonl").write_text(json.dumps(
        {"utc": t0, "exp": "E12", "event": "created", "pod_id": "pod1", "slot": "_A",
         "rate_usd_h": 3.49}) + "\n")
    pod = FakePod(tmp_path)
    return pod, FakeRunner(pod, lap), lap


def run(env, args: str, capsys) -> tuple[int, dict]:
    pod, runner, lap = env
    capsys.readouterr()
    rc = ctl.main(args.split(), runner=runner, root=lap, pod_root=str(pod.emx),
                  pod_scratch=str(pod.scratch))
    out = capsys.readouterr().out.strip().splitlines()
    return rc, json.loads(out[-1])


# --- poll, stop ---------------------------------------------------------------------------------


def test_poll_dry_run_runs_nothing(tmp_path, capsys):
    rc = ctl.main(["poll", "--slot", "_A", "--dry-run"], root=tmp_path)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and out[0].startswith("+ ssh ") and json.loads(out[-1])["dry_run"] is True


def test_remote_scripts_never_name_the_chain_or_driver_bare(env):
    _pod, runner, lap = env
    ctx = ctl.Ctx(root=lap, slot="_A", recipe="rank1", runner=runner)
    body = ctl.poll_body(ctx)
    assert "[p]od_run_e12" in body and "[e]12_pod" in body
    assert "pod_run_e12" not in body.replace("[p]od_run_e12", "")
    assert "e12_pod" not in body.replace("[e]12_pod", "")


def test_poll_reports_chain_manifests_and_pod_hours(env, capsys):
    pod, runner, _lap = env
    rc, _ = run(env, "launch --slot _A --recipe rank1", capsys)
    assert rc == 0
    pod.running("pod_run_e12", "e12_pod")
    rc, out = run(env, "poll --slot _A", capsys)
    assert rc == 0 and out["state"] == "running" and out["processes"] == ["chain", "driver"]
    assert "eval-poison" in out["manifests"]["rank1"] and "genpath.json" in out["control"]["rank1"]
    assert out["chain_tag"] == "E12_CHAIN_DONE failed=none"
    assert out["attempts"]["rank1"]["attempt_id"].startswith("rank1-")
    assert 0.99 < out["elapsed_pod_hours"] < 1.01
    assert runner.ops.count("poll") == 3  # launch: before and after; then one poll


def test_stop_waits_minutes_apart_for_the_acknowledgement(env, capsys):
    pod, runner, _lap = env
    pod.running("pod_run_e12", "e12_pod")
    log = "logs/e12/run_0001.log"
    pod.write(log, "=== chain\n")
    ticks = []

    def tick():
        ticks.append(1)
        stop = pod.p(C.control_path("rank1", "STOP")[len(C.POD_ROOT) + 1:])
        if len(ticks) == 2 and stop.is_file():
            pod.tag(log, "E12_STEP_STOPPED repair", "E12_CHAIN_STOPPED at=repair")
            pod.running()

    pod.on_tick = tick
    rc, out = run(env, "stop --slot _A", capsys)
    assert rc == 0 and out["state"] == "stopped" and out["polls"] == 3
    assert runner.sleeps and all(s >= 60 for s in runner.sleeps)
    for r in C.RECIPES:
        assert pod.p(C.control_path(r, "STOP")[len(C.POD_ROOT) + 1:]).is_file()


def test_stop_never_acknowledged_exits_1(env, capsys):
    pod, runner, _lap = env
    pod.running("e12_pod")
    pod.write("logs/e12/run_0001.log", "=== chain\nE12_STEP_DONE smoke\n")
    rc, out = run(env, "stop --slot _A", capsys)
    assert rc == 1 and out["state"] == "running" and out["polls"] == 8
    assert len(runner.sleeps) == 7 and all(s >= 60 for s in runner.sleeps)


# --- launch and attempts ------------------------------------------------------------------------


def test_launch_refused_while_a_chain_runs(env, capsys):
    pod, _runner, _lap = env
    pod.running("pod_run_e12")
    pod.write("logs/e12/run_0001.log", "=== chain\n")
    rc, out = run(env, "launch --slot _A --recipe rank1", capsys)
    assert rc == C.EXIT_REFUSED and "not verified ended" in out["reason"]
    assert not pod.launches and not pod.p(ctl.ATTEMPTS_REL).exists()


def test_launch_without_progress_fails(env, capsys):
    pod, runner, _lap = env
    pod.on_launch = lambda recipe, args, log: None  # died at once: no new log lines
    rc, out = run(env, "launch --slot _A --recipe rank1", capsys)
    assert rc == 1 and "no real progress" in out["reason"]
    assert runner.sleeps == [20.0]


def test_rank32_started_once_resumed_many(env, capsys):
    pod, _runner, _lap = env
    rc, first = run(env, "launch --slot _A --recipe rank32", capsys)
    assert rc == 0 and first["resumed"] is False
    rc, out = run(env, "launch --slot _A --recipe rank32", capsys)
    assert rc == C.EXIT_REFUSED and "already started" in out["reason"]
    for _ in range(2):
        rc, out = run(env, "launch --slot _A --recipe rank32 --resume", capsys)
        assert rc == 0 and out["attempt"] == first["attempt"] and out["resumed"] is True
    attempts = json.loads(pod.p(ctl.ATTEMPTS_REL).read_text())
    assert attempts == {"rank32": first["attempt"]}
    assert [r for r, _, _ in pod.launches] == ["rank32"] * 3
    rc, out = run(env, "launch --slot _A --recipe rank1 --resume", capsys)
    assert rc == C.EXIT_REFUSED and "no rank1 attempt" in out["reason"]


def test_launch_removes_only_its_recipes_stop(env, capsys):
    pod, _runner, _lap = env
    for r in C.RECIPES:
        pod.write(C.control_path(r, "STOP")[len(C.POD_ROOT) + 1:], "{}")
    rc, _ = run(env, "launch --slot _A --recipe rank32", capsys)
    assert rc == 0
    assert not pod.p(C.control_path("rank32", "STOP")[len(C.POD_ROOT) + 1:]).exists()
    assert pod.p(C.control_path("rank1", "STOP")[len(C.POD_ROOT) + 1:]).exists()


# --- induction ----------------------------------------------------------------------------------


def test_induction_waits_for_a_delayed_manifest(env, capsys):
    pod, runner, lap = env
    pod.on_launch = lambda recipe, args, log: (pod.tag(log, "E12_STEP_DONE smoke"),
                                               pod.running("pod_run_e12", "e12_pod"))
    rc, _ = run(env, "launch --slot _A --recipe rank1", capsys)
    assert rc == 0
    rc, out = run(env, "induction --slot _A --recipe rank1", capsys)
    assert rc == C.EXIT_BLOCKED and "not yet" in out["reason"]
    assert not any("pod_pull_results.sh" in " ".join(c) for c in runner.calls)
    pod.chain("rank1", pod.launches[-1][2])  # the chain gets there later
    rc, out = run(env, "induction --slot _A --recipe rank1", capsys)
    assert rc == 0 and out["next_action"] == "continue" and out["n_induced"] == 3
    assert (lap / C.manifest_path("rank1", "eval-poison")).is_file()


def test_rank1_induction_failure_benches_rank32_once(env, capsys):
    _pod, runner, _lap = env
    runner.decisions["poison"] = {"induction": {"1": True, "2": False, "3": False}}
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    rc, out = run(env, "induction --slot _A --recipe rank1", capsys)
    assert rc == 0 and out["n_induced"] == 1 and out["next_action"] == "rank32-bench"
    assert run(env, "launch --slot _A --recipe rank32", capsys)[0] == 0
    rc, out = run(env, "induction --slot _A --recipe rank1", capsys)
    assert out["next_action"] == "terminate: organism not established"


def test_rank32_induction_failure_terminates(env, capsys):
    _pod, runner, _lap = env
    runner.decisions["poison"] = {"n_induced": 0}
    assert run(env, "launch --slot _A --recipe rank32", capsys)[0] == 0
    rc, out = run(env, "induction --slot _A --recipe rank32", capsys)
    assert rc == 0 and out["next_action"] == "terminate: organism not established"


def test_induction_passes_a_judge_budget_refusal_through(env, capsys):
    _pod, runner, _lap = env
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    runner.judge_rc = C.EXIT_BUDGET
    rc, _ = run(env, "induction --slot _A --recipe rank1", capsys)
    assert rc == C.EXIT_BUDGET


# --- the vLLM path ------------------------------------------------------------------------------


def test_vllm_endpoint_after_the_chain_exits_with_local_failures_through_complete(env, capsys):
    pod, runner, _lap = env
    # a B repair failure, an unused rescue failure and a failure of a selected (107) nudge
    pod.failed_runs = {"repair_B2", "rescue_A1", "nudge_A3_r107"}
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    rc, out = run(env, "complete --slot _A --recipe rank1", capsys)
    assert rc == 1 and any("retention receipt" in p for p in out["problems"])
    rc, out = run(env, "endpoint --slot _A --recipe rank1 --stage repair107", capsys)
    assert rc == 0 and out["endpoint_json"]["endpoint"] == 107
    assert pod.control("rank1", "endpoint.json")["rescue_triggered"] is False
    rc, out = run(env, "archive --slot _A --recipe rank1", capsys)
    assert rc == 0 and out["method"] == "hf"
    rc, out = run(env, "complete --slot _A --recipe rank1", capsys)
    assert rc == 0, out["problems"]
    assert out["n_jobs"] == len(C.planned_jobs("rank1", "vllm", 107, []))
    assert out["n_skipped"] > 0 and out["endpoint"] == 107
    assert not any(c[0] == "pod_down.py" or "pod_down" in " ".join(c) for c in runner.calls)


def test_complete_refuses_a_manifest_bound_to_a_stale_job_list(env, capsys):
    pod, _runner, _lap = env
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "archive --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "complete --slot _A --recipe rank1", capsys)[0] == 0
    rec = C.bench_path("rank1", "eval-traj.jobs")
    pod.write(rec, json.dumps({"step": "eval-traj", "jobs_sha256": "0" * 64}))
    pod.manifest("rank1", "eval-traj",
                 [ln.split("  ", 1)[1] for ln in pod.p(C.manifest_path("rank1", "eval-traj"))
                  .read_text().splitlines() if not ln.endswith(rec)], "0" * 64)
    rc, out = run(env, "complete --slot _A --recipe rank1", capsys)
    assert rc == 1 and any("MANIFEST.eval-traj: bound to" in p for p in out["problems"])


def test_a_stale_skip_record_is_refused(env, capsys):
    pod, _runner, lap = env
    pod.failed_runs = {"repair_B1"}
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "archive --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "complete --slot _A --recipe rank1", capsys)[0] == 0
    # the run is ok now and its checkpoint exists: the skip of full/B1_r107 is stale
    (lap / C.status_path("rank1", "repair_B1")).write_text(json.dumps({"status": "ok"}))
    _local_ckpt(lap, "rank1", "B1_r107")
    rc, out = run(env, "complete --slot _A --recipe rank1 --no-pull", capsys)
    assert rc == 1 and any("full/B1_r107: stale skip record" in p for p in out["problems"])


def test_unsupported_skip_records_are_refused(env, capsys):
    """Code review r2 finding 4: the reviewer's probe (a skip whose checkpoint is absent and
    whose run has no status) and a skip naming a run outside the job's chain are refused."""
    pod, _runner, lap = env
    pod.failed_runs = {"repair_B1"}
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "archive --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "complete --slot _A --recipe rank1", capsys)[0] == 0
    sp = lap / C.job_status_path("rank1", "full", "B1_r107")
    assert not (lap / C.ckpt_dir("rank1", "B1_r107") / "ckpt.json").exists()
    good, stp = sp.read_text(), lap / C.status_path("rank1", "repair_B1")
    failed = stp.read_text()
    stp.unlink()  # no status: pending, not failed
    rc, out = run(env, "complete --slot _A --recipe rank1 --no-pull", capsys)
    assert rc == 1 and any("full/B1_r107: stale skip record" in p for p in out["problems"])
    stp.write_text(failed)
    sp.write_text(good.replace('"repair_B1"', '"repair_A1"'))  # outside B1_r107's chain
    rc, out = run(env, "complete --slot _A --recipe rank1 --no-pull", capsys)
    assert rc == 1 and any("outside the dependency chain" in p for p in out["problems"])
    sp.write_text(good)
    assert run(env, "complete --slot _A --recipe rank1 --no-pull", capsys)[0] == 0


# --- the HF path --------------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", ["rank1", "rank32"])
def test_hf_rescue_branch_through_complete(env, capsys, recipe):
    pod, runner, lap = env
    pod.gen_path = "hf"
    pod.upload_ok = recipe == "rank1"  # rank 32: the upload fails, the verified tar pull holds
    runner.decisions["repair107"] = {"rescue_triggered": True}
    R = f"--slot _A --recipe {recipe}"
    assert run(env, f"launch {R}", capsys)[0] == 0  # training runs; evaluation BLOCKED
    assert run(env, f"induction {R}", capsys)[0] == C.EXIT_BLOCKED
    rc, out = run(env, f"admit-hf {R}", capsys)  # admission arrives after training
    assert rc == 0 and out["cuts"] == [] and out["branch"] == "hf-complete"
    complete_branch = C.planned_jobs(recipe, "hf", 214, [], rescue_triggered=True)
    _check_admission(pod, lap, recipe, [])
    assert pod.control(recipe, "cuts.json")["cuts"] == []
    assert run(env, f"admit-hf {R}", capsys)[1]["already_admitted"] is True
    assert run(env, f"launch {R} --resume", capsys)[0] == 0
    assert run(env, f"induction {R}", capsys)[1]["next_action"] == "continue"
    rc, out = run(env, f"complete {R}", capsys)
    assert rc == 1 and any("not final" in p for p in out["problems"])
    assert run(env, f"endpoint {R} --stage rescue214", capsys)[0] == C.EXIT_REFUSED
    rc, out = run(env, f"endpoint {R} --stage repair107", capsys)
    assert rc == 0 and out["endpoint_json"] == {**out["endpoint_json"], "rescue_triggered": True}
    assert "endpoint" not in pod.control(recipe, "endpoint.json")
    rc, out = run(env, f"endpoint {R} --stage rescue214", capsys)  # 214 inputs not yet made
    assert rc == C.EXIT_BLOCKED and len(out["waiting"]) == 9
    assert run(env, f"launch {R} --resume", capsys)[0] == 0  # generates the 214 full evals
    rc, out = run(env, f"endpoint {R} --stage rescue214", capsys)
    assert rc == 0 and pod.control(recipe, "endpoint.json")["endpoint"] == 214
    rc, out = run(env, f"complete {R}", capsys)
    assert rc == 1 and any(p.startswith("nudge/") for p in out["problems"])
    assert run(env, f"launch {R} --resume", capsys)[0] == 0  # the 214 durability outputs
    rc, out = run(env, f"archive {R}", capsys)
    assert rc == 0 and out["method"] == ("hf" if recipe == "rank1" else "local")
    rc, out = run(env, f"complete {R}", capsys)
    assert rc == 0, out["problems"]
    assert out["n_jobs"] == len(complete_branch) and out["rescue_triggered"] is True
    if recipe == "rank32":
        assert "tar" in runner.ops
        receipt = json.loads((lap / C.retention_receipt_path(recipe)).read_text())
        assert receipt["method"] == "local" and receipt["commit_sha"] is None
        assert (lap / receipt["location"]).is_dir() and C.rdir(recipe) in receipt["location"]
        n_ckpts = len(list(pod.p(f"{C.rdir(recipe)}/ckpt").glob("*/ckpt.json")))
        assert len(receipt["files"]) == 2 * n_ckpts


def _check_admission(pod, lap: Path, recipe: str, cuts: list[int]) -> dict:
    """admit-hf authorized the union of both complete HF branches and persisted the same
    admission record locally and in the pod's results tree, with genpath.json agreeing."""
    union = C.hf_complete_jobs(recipe, cuts)
    gp = pod.control(recipe, "genpath.json")
    assert gp["admitted"] is True and gp["jobs_sha256"] == C.jobs_sha256(union)
    assert gp["branch"] == "hf-complete" and gp["cuts"] == cuts
    local = lap / C.admission_path(recipe)
    remote = pod.p(C.admission_path(recipe))
    assert local.read_text() == remote.read_text()
    adm = json.loads(local.read_text())
    assert set(adm) == set(C.ADMISSION_FIELDS)
    assert adm["jobs_sha256"] == gp["jobs_sha256"] and adm["branch"] == "hf-complete"
    assert [(j["kind"], j["key"], j["n_rows"]) for j in adm["jobs"]] == [
        (j.kind, j.key, j.n_rows) for j in union]
    assert adm["cuts"] == cuts and adm["admitted"] is True and adm["gen_path"] == "hf"
    proj = json.loads((lap / C.projection_path(recipe, "hf")).read_text())
    assert proj == adm["projection"] and proj["verdict"] == "admit"
    return adm


def test_hf_107_branch_through_complete(env, capsys):
    """Review r1 finding 1: repair succeeds at 107 (no rescue); its durability jobs are inside
    the admitted union, and complete passes."""
    pod, runner, lap = env
    pod.gen_path = "hf"
    R = "--slot _A --recipe rank1"
    assert run(env, f"launch {R}", capsys)[0] == 0
    rc, out = run(env, f"admit-hf {R}", capsys)
    assert rc == 0 and out["cuts"] == []
    budget = runner.budget_argv
    assert budget[budget.index("--branch") + 1] == "hf-complete" and "--no-write" not in budget
    assert "--endpoint" not in budget and "--rescue-triggered" not in budget
    assert budget[budget.index("--root") + 1] == str(lap)
    _check_admission(pod, lap, "rank1", [])
    assert runner.ops.count("admit-hf") == 1  # cuts, admission and genpath: one ssh call
    assert run(env, f"launch {R} --resume", capsys)[0] == 0
    rc, out = run(env, f"endpoint {R} --stage repair107", capsys)
    assert rc == 0 and pod.control("rank1", "endpoint.json") == {
        **pod.control("rank1", "endpoint.json"), "endpoint": 107, "rescue_triggered": False}
    rc, out = run(env, f"complete {R}", capsys)
    assert rc == 1 and any(p.startswith("nudge/") for p in out["problems"])
    assert run(env, f"launch {R} --resume", capsys)[0] == 0  # the 107 durability outputs
    assert run(env, f"archive {R}", capsys)[0] == 0
    rc, out = run(env, f"complete {R}", capsys)
    assert rc == 0, out["problems"]
    b107 = C.planned_jobs("rank1", "hf", 107, [], rescue_triggered=False)
    assert out["n_jobs"] == len(b107) and out["endpoint"] == 107
    assert out["rescue_triggered"] is False
    # the local admission record is what the final decision reads: it must agree
    adm_p = lap / C.admission_path("rank1")
    adm = json.loads(adm_p.read_text())
    adm_p.write_text(json.dumps({**adm, "jobs_sha256": "0" * 64}))
    rc, out = run(env, f"complete {R} --no-pull", capsys)
    assert rc == 1 and any("admission" in p and "jobs_sha256" in p for p in out["problems"])
    adm_p.unlink()
    rc, out = run(env, f"complete {R} --no-pull", capsys)
    assert rc == 1 and any("no readable admission record" in p for p in out["problems"])
    # an already-admitted pod rebuilds the missing local record from genpath.json
    rc, out = run(env, f"admit-hf {R}", capsys)
    assert rc == 0 and out["already_admitted"] is True and out["restored_local_admission"]
    assert json.loads(adm_p.read_text()) == adm


def test_admit_hf_persists_the_projection_when_the_budget_cli_did_not(env, capsys):
    pod, runner, lap = env
    pod.gen_path = "hf"
    runner.budget_skips_write = True
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "admit-hf --slot _A --recipe rank1", capsys)[0] == 0
    _check_admission(pod, lap, "rank1", [])


def test_admit_hf_failed_push_admits_nothing(env, capsys, monkeypatch):
    pod, _runner, lap = env
    pod.gen_path = "hf"
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    monkeypatch.setattr(ctl, "write_files", lambda ctx, op, files: False)
    rc, out = run(env, "admit-hf --slot _A --recipe rank1", capsys)
    assert rc == 1 and "control write failed" in out["reason"]
    assert not (lap / C.admission_path("rank1")).exists()
    assert list((lap / C.rdir("rank1")).glob("admission.unpushed_*.json"))
    assert pod.control("rank1", "genpath.json")["admitted"] is False


def test_admit_hf_refuses_a_budget_that_fits_only_the_pre_decision_answers(env, capsys):
    pod, runner, lap = env
    pod.gen_path = "hf"
    pre = sum(j.n_rows for j in C.planned_jobs("rank1", "hf", None, []))
    all_cuts = sum(j.n_rows for j in C.planned_jobs("rank1", "hf", 214, sorted(C.CUTS), True))
    assert all_cuts > pre  # the complete branch never fits, even after every cut
    runner.budget_answers = pre
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    rc, out = run(env, "admit-hf --slot _A --recipe rank1", capsys)
    assert rc == C.EXIT_BUDGET and out["projection"]["verdict"] == "refuse"
    budget = next(c for c in runner.calls if "emx.e12.budget" in " ".join(c))
    assert budget[budget.index("--branch") + 1] == "hf-complete" and "--no-write" not in budget
    assert pod.control("rank1", "genpath.json")["admitted"] is False
    assert pod.control("rank1", "cuts.json") is None
    assert not pod.p(C.admission_path("rank1")).exists()
    assert not (lap / C.admission_path("rank1")).exists()
    proj = json.loads((lap / C.projection_path("rank1", "hf")).read_text())
    assert proj["verdict"] == "refuse"  # the refusal is recorded


def test_admit_hf_applies_cuts_and_is_refused_on_vllm(env, capsys):
    pod, runner, _lap = env
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    assert run(env, "admit-hf --slot _A --recipe rank1", capsys)[0] == C.EXIT_REFUSED
    pod.write(C.control_path("rank1", "genpath.json")[len(C.POD_ROOT) + 1:],
              json.dumps({"gen_path": "hf", "admitted": False}))
    full = sum(j.n_rows for j in C.planned_jobs("rank1", "hf", 214, [], True))
    runner.budget_answers = full - 1
    rc, out = run(env, "admit-hf --slot _A --recipe rank1", capsys)
    assert rc == 0 and out["cuts"] == [0, 1]  # cut 0 changes no generation; cut 1 does
    assert pod.control("rank1", "cuts.json")["cuts"] == [0, 1]
    _check_admission(pod, env[2], "rank1", [0, 1])


# --- retention ----------------------------------------------------------------------------------


def _local_ckpt(lap: Path, recipe: str, key: str) -> dict:
    d = lap / C.ckpt_dir(recipe, key)
    d.mkdir(parents=True)
    files = {}
    for n in ADAPTER_FILES:
        (d / n).write_text(f"{key}/{n}")
        files[n] = sha(d / n)
    (d / "ckpt.json").write_text(json.dumps({"key": key, "adapter_files": files}))
    return files


def test_retained_predicate(tmp_path):
    files = _local_ckpt(tmp_path, "rank1", "P1")
    assert not ctl.retained("rank1", tmp_path)  # no receipt
    receipt = {"recipe": "rank1", "method": "hf", "location": "jrepifano/e12-rank1-adapters",
               "commit_sha": "a" * 40, "private": True,
               "files": {f"P1/{n}": s for n, s in files.items()}, "utc": "x"}
    rp = tmp_path / C.retention_receipt_path("rank1")
    rp.parent.mkdir(parents=True)
    rp.write_text(json.dumps(receipt))
    assert ctl.retained("rank1", tmp_path)
    bad = {**receipt, "files": {**receipt["files"], "P1/adapter_model.safetensors": "0" * 64}}
    rp.write_text(json.dumps(bad))
    assert not ctl.retained("rank1", tmp_path)  # sha mismatch
    rp.write_text(json.dumps({**receipt, "private": False}))
    assert not ctl.retained("rank1", tmp_path)
    rp.write_text(json.dumps({**receipt, "commit_sha": None}))
    assert not ctl.retained("rank1", tmp_path)
    rp.write_text(json.dumps({**receipt, "method": "local", "location": "nowhere"}))
    assert not ctl.retained("rank1", tmp_path)
    _local_ckpt(tmp_path, "rank1", "A1_r107")  # a checkpoint the receipt does not cover
    rp.write_text(json.dumps(receipt))
    assert not ctl.retained("rank1", tmp_path)


def test_archive_refuses_a_tar_pull_that_does_not_verify(env, capsys):
    pod, _runner, lap = env
    pod.upload_ok = False
    assert run(env, "launch --slot _A --recipe rank32", capsys)[0] == 0
    (pod.adapter_dir("rank32", "P2") / "adapter_model.safetensors").write_text("corrupted")
    rc, out = run(env, "archive --slot _A --recipe rank32", capsys)
    assert rc == 1 and any("P2/adapter_model.safetensors sha256 differs" in p
                           for p in out["problems"])
    assert not (lap / C.retention_receipt_path("rank32")).exists()
    assert not ctl.retained("rank32", lap)


def test_recorded_jobs_sha_reads_a_sidecar(tmp_path):
    rel = C.manifest_path("rank1", "eval-traj")
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("")
    (tmp_path / f"{C.rdir('rank1')}/MANIFEST.eval-traj.json").write_text(
        json.dumps({"jobs_sha256": "ab" * 32}))
    assert ctl.recorded_jobs_sha(tmp_path, "rank1", "eval-traj") == "ab" * 32


def test_cli_gates(env, capsys):
    _pod, runner, lap = env
    assert ctl.main(["poll", "--slot", "_B"], runner=runner, root=lap) == C.EXIT_REFUSED
    assert ctl.main(["launch", "--slot", "_A"], runner=runner, root=lap) == C.EXIT_REFUSED
    assert ctl.main(["endpoint", "--slot", "_A"], runner=runner, root=lap) == C.EXIT_REFUSED
    with pytest.raises(SystemExit) as e:
        ctl.main(["nonsense", "--slot", "_A"], runner=runner, root=lap)
    assert e.value.code == C.EXIT_REFUSED


# --- archive --while-running (the watchdog's early archival and hard stop) ---------------------


def test_archive_while_running_retains_rank32_without_the_upload_step(env, capsys):
    """Review r1 finding 2: while the chain still runs, the watchdog's archive syncs the pod's
    ckpt.json records and pulls the rank-32 adapter files with the verified tar path; a repeat
    pulls only new checkpoints; a corrupt file is reported, never receipted."""
    pod, runner, lap = env
    pod.upload_ok = False
    assert run(env, "launch --slot _A --recipe rank32", capsys)[0] == 0
    pod.running("pod_run_e12", "e12_pod")  # the chain is still running
    rc, out = run(env, "archive --slot _A --recipe rank32", capsys)
    assert rc == C.EXIT_REFUSED  # the normal archive waits for the chain's end
    assert not (lap / C.rdir("rank32") / "ckpt").exists()  # nothing pulled yet
    n_launches = len(pod.launches)
    rc, out = run(env, "archive --slot _A --while-running", capsys)  # no --recipe: attempts
    assert rc == 0, out
    assert out["mode"] == "while-running" and out["retained"] == {"rank32": True}
    assert out["recipes"]["rank32"]["method"] == "local"
    assert len(pod.launches) == n_launches  # no upload step launched
    assert ctl.retained("rank32", lap)
    n_ckpts = len(list(pod.p(f"{C.rdir('rank32')}/ckpt").glob("*/ckpt.json")))
    assert len(list((lap / C.rdir("rank32") / "ckpt").glob("*/ckpt.json"))) == n_ckpts
    # a new checkpoint appears; the repeat links the old files and pulls only the new ones
    key = C.nudge_key("A1_r107", 1, C.NUDGE_DOSES[-1])
    d = pod.adapter_dir("rank32", key)
    shutil.rmtree(pod.p(C.ckpt_dir("rank32", key)), ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    files = {}
    for n in ADAPTER_FILES:
        (d / n).write_text(f"new/{n}")
        files[n] = sha(d / n)
    pod.write(f"{C.ckpt_dir('rank32', key)}/ckpt.json", json.dumps(
        {"key": key, "adapter_files": files, "adapter_dir": C.adapter_files_dir("rank32", key)}))
    (lap / C.ckpt_dir("rank32", key) / "ckpt.json").unlink()  # the laptop lacks it now
    n_pipes = len(runner.pipes)
    rc, out = run(env, "archive --slot _A --recipe rank32 --while-running", capsys)
    assert rc == 0 and out["retained"] == {"rank32": True}
    new = runner.pipes[n_pipes:]
    assert new[0].startswith("# e12ctl:ckpt-json")
    adapter_pulls = [x for x in new if x.startswith("# e12ctl:tar")]
    assert len(adapter_pulls) == 1 and all(f"{key}/{n}" in adapter_pulls[0] for n in ADAPTER_FILES)
    assert adapter_pulls[0].count("adapter_model.safetensors") == 1
    receipt = json.loads((lap / C.retention_receipt_path("rank32")).read_text())
    assert receipt["files"][f"{key}/adapter_model.safetensors"] == files[
        "adapter_model.safetensors"]
    # a corrupted adapter on the pod for a not-yet-pulled checkpoint: no receipt for it
    (d / "adapter_model.safetensors").write_text("changed after ckpt.json")
    (lap / C.retention_receipt_path("rank32")).unlink()
    rc, out = run(env, "archive --slot _A --recipe rank32 --while-running", capsys)
    assert rc == 1 and out["retained"] == {"rank32": False}
    assert any("sha256 differs" in p for p in out["recipes"]["rank32"]["problems"])


def test_final_archive_not_retained_when_a_checkpoint_appears_after_the_snapshot(env, capsys):
    """Code review r2 finding 2: the watchdog's final archive (`--no-upload`, chain ended)
    archives the delta, then re-syncs ckpt.json: a checkpoint made after its snapshot leaves
    `retained` false; once that one is archived too, true."""
    pod, runner, _lap = env
    pod.upload_ok = False
    assert run(env, "launch --slot _A --recipe rank32", capsys)[0] == 0
    key = C.nudge_key("A1_r107", 1, C.NUDGE_DOSES[-1])
    late = pod.p(f"{C.ckpt_dir('rank32', key)}/ckpt.json")
    held = late.read_text()
    late.unlink()  # not yet written when the snapshot is taken
    real_pipe = runner.pipe

    def pipe(src, dst, timeout):
        res = real_pipe(src, dst, timeout)
        if src[-1].startswith("# e12ctl:tar") and not late.exists():
            late.write_text(held)  # a producer still running writes it after the adapter pull
        return res

    runner.pipe = pipe
    n_launches = len(pod.launches)
    rc, out = run(env, "archive --slot _A --no-upload", capsys)  # no --recipe: attempts
    assert rc == 1 and out["retained"] == {"rank32": False}, out
    assert any(key in p for p in out["recipes"]["rank32"]["problems"])
    assert len(pod.launches) == n_launches  # --no-upload: no upload step
    rc, out = run(env, "archive --slot _A --recipe rank32 --no-upload", capsys)
    assert rc == 0 and out["retained"] == {"rank32": True} and out["method"] == "local", out


def test_archive_while_running_never_overwrites_a_differing_ckpt_record(env, capsys):
    pod, _runner, lap = env
    pod.upload_ok = False
    assert run(env, "launch --slot _A --recipe rank32", capsys)[0] == 0
    assert run(env, "archive --slot _A --recipe rank32 --while-running", capsys)[0] == 0
    p = pod.p(f"{C.ckpt_dir('rank32', 'P1')}/ckpt.json")
    local = lap / C.ckpt_dir("rank32", "P1") / "ckpt.json"
    kept = local.read_text()
    p.write_text(p.read_text().replace('"P1"', '"P1" ', 1))
    rc, out = run(env, "archive --slot _A --recipe rank32 --while-running", capsys)
    assert any("differs from the laptop's" in x for x in out["recipes"]["rank32"]["problems"])
    assert rc == 1 and local.read_text() == kept


def test_tan32_induction_failure_terminates_without_a_fallback(env, capsys):
    """Amendment D: a failed tan32 induction ends E12 (no rank32-bench, even if rank 32 never
    started in this attempt file)."""
    _pod, runner, _lap = env
    runner.decisions["poison"] = {"induction": {"1": True, "2": False, "3": False}}
    assert run(env, "launch --slot _A --recipe tan32", capsys)[0] == 0
    rc, out = run(env, "induction --slot _A --recipe tan32", capsys)
    assert rc == 0 and out["n_induced"] == 1
    assert out["next_action"] == "terminate: organism not established"
    assert set(out["attempts"]) == {"tan32"}
    runner.decisions["poison"] = {"n_induced": 2}
    rc, out = run(env, "induction --slot _A --recipe tan32", capsys)
    assert rc == 0 and out["next_action"] == "continue"


def _induction_json(pod, recipe: str) -> Path:
    return pod.p(C.control_path(recipe, "induction.json")[len(C.POD_ROOT) + 1:])


def test_tan32_induction_writes_the_staged_gate_file(env, capsys):
    """Code review, Amendment D r1 finding 2: for tan32 the induction decision goes to the pod
    as control induction.json (passed, n_induced, decisions_sha256); rank 1 gets none."""
    pod, runner, lap = env
    runner.decisions["poison"] = {"induction": {"1": True, "2": False, "3": False}}
    assert run(env, "launch --slot _A --recipe tan32", capsys)[0] == 0
    assert run(env, "induction --slot _A --recipe tan32", capsys)[0] == 0
    rec = json.loads(_induction_json(pod, "tan32").read_text())
    assert rec["passed"] is False and rec["n_induced"] == 1 and rec["recipe"] == "tan32"
    dec = lap / C.decisions_path("tan32")
    assert not dec.is_file() and rec["decisions_sha256"] is None  # the fake decide writes none
    runner.decisions["poison"] = {"n_induced": 3}
    dec.parent.mkdir(parents=True, exist_ok=True)
    dec.write_text('{"poison": {"n_induced": 3}}\n')
    assert run(env, "induction --slot _A --recipe tan32", capsys)[0] == 0
    rec = json.loads(_induction_json(pod, "tan32").read_text())
    assert rec["passed"] is True and rec["n_induced"] == 3 and rec["decisions_sha256"] == sha(dec)


def test_rank1_induction_writes_no_gate_file(env, capsys):
    pod, _runner, _lap = env
    assert run(env, "launch --slot _A --recipe rank1", capsys)[0] == 0
    rc, out = run(env, "induction --slot _A --recipe rank1", capsys)
    assert rc == 0 and out["next_action"] == "continue"
    assert not _induction_json(pod, "rank1").exists()
    assert not _induction_json(pod, "tan32").exists()


def test_tan32_induction_fails_when_the_gate_file_cannot_be_written(env, capsys, monkeypatch):
    _pod, _runner, _lap = env
    assert run(env, "launch --slot _A --recipe tan32", capsys)[0] == 0
    monkeypatch.setattr(ctl, "write_files", lambda ctx, op, files: False)
    rc, out = run(env, "induction --slot _A --recipe tan32", capsys)
    assert rc == C.EXIT_FAILED and "induction.json" in out["reason"]


def test_launch_last_step_pauses_and_the_paused_chain_counts_as_ended(env, capsys):
    pod, _runner, _lap = env
    pod.on_launch = lambda recipe, args, log: pod.tag(
        log, "E12_STEP_DONE download", "E12_STEP_DONE smoke", "E12_CHAIN_PAUSED after=smoke")
    rc, out = run(env, "launch --slot _A --recipe tan32 --last-step smoke", capsys)
    assert rc == 0 and out["state"] == "paused" and out["last_step"] == "smoke", out
    assert out["chain_tag"] == "E12_CHAIN_PAUSED after=smoke"
    assert pod.launches[-1][0] == "tan32" and pod.launches[-1][1].endswith(" LAST=smoke")
    rc, out = run(env, "poll --slot _A", capsys)
    assert rc == 0 and out["state"] == "paused"
    assert "paused" in ctl.ENDED and "paused" in ctl.RECOVERABLE  # stop/launch/archive accept it
    rc, out = run(env, "complete --slot _A --recipe tan32", capsys)
    assert rc == 1 and out["complete"] is False  # paused is never complete by its tag
    rc, out = run(env, "stop --slot _A", capsys)
    assert rc == 0 and out["state"] == "paused" and out["interrupted"] is False
    pod.on_launch = pod.default_launch
    rc, out = run(env, "launch --slot _A --recipe tan32 --resume", capsys)
    assert rc == 0 and out["last_step"] is None and "LAST=" not in pod.launches[-1][1]
    assert out["chain_tag"] == "E12_CHAIN_DONE failed=none"
    assert run(env, "archive --slot _A --recipe tan32", capsys)[0] == 0
    rc, out = run(env, "complete --slot _A --recipe tan32", capsys)
    assert rc == 0 and out["complete"] is True, out["problems"]
    with pytest.raises(SystemExit) as e:
        run(env, "launch --slot _A --recipe tan32 --resume --last-step generate", capsys)
    assert e.value.code == C.EXIT_REFUSED


def test_interrupted_chain_recovers_through_stop_and_resume(env, capsys):
    """Code review r3 finding 3: a chain that died without an end tag (no process left) is
    acknowledged by stop, and its attempt can then be resumed (and archived)."""
    pod, _runner, _lap = env
    rc, first = run(env, "launch --slot _A --recipe rank1", capsys)
    assert rc == 0
    pod.running()
    pod.write("logs/e12/run_9999.log", "=== chain\nE12_STEP_DONE smoke\n")  # no end tag
    newest = pod.p("logs/e12/run_9999.log")
    pod.mtime += 10
    os.utime(newest, (pod.mtime, pod.mtime))  # the newest run log by mtime, as `ls -t` picks it
    rc, out = run(env, "stop --slot _A", capsys)
    assert rc == 0 and out["state"] == "exited" and out["interrupted"] is True
    rc, out = run(env, "launch --slot _A --recipe rank1 --resume", capsys)
    assert rc == 0 and out["attempt"] == first["attempt"] and out["resumed"] is True


def test_tan32_induction_pulls_the_recheck_record(env, capsys):
    """Code review, Amendment D r2: tan32's base source follows its re-check record, so the
    induction pull brings `MANIFEST.lora-check` (and bench/lora_check.json) along with
    eval-poison, with nothing cached locally beforehand."""
    pod, runner, lap = env
    runner.decisions["poison"] = {"n_induced": 3}
    assert run(env, "launch --slot _A --recipe tan32", capsys)[0] == 0
    rel = C.bench_path("tan32", "lora_check")
    f = pod.write(rel, json.dumps({"verdict": "PASS", "gen_path": "vllm"}) + "\n")
    pod.write(C.manifest_path("tan32", "lora-check"), f"{sha(f)}  {rel}\n")
    assert not (lap / C.manifest_path("tan32", "lora-check")).is_file()
    assert run(env, "induction --slot _A --recipe tan32", capsys)[0] == 0
    assert (lap / C.manifest_path("tan32", "lora-check")).is_file() and (lap / rel).is_file()


# --- progress against the admitted projection (Amendment D') ---------------------------------


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y%m%dT%H%M%SZ")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def progress_pod(pod, t_p: float, steps: list[tuple[str, float, float | None]],
                 step_hours: dict, total: float = 5.0, recipe: str = "tan32") -> None:
    """A projection admitted at t_p, then step invocations (step, start, end or None = still
    running: its log was written just now)."""
    R = C.rdir(recipe)
    proj = pod.write(f"{R}/bench/projection.lora-check.json", json.dumps({
        "stage": "lora-check", "verdict": "admit", "elapsed_pod_hours": 0.5,
        "total_pod_hours": total, "remaining_hours": {"train": 2.0, "margin": 0.3},
        "step_hours": step_hours}))
    os.utime(proj, (t_p, t_p))
    pod.write(f"{R}/bench/projection.smoke.json", json.dumps({"stage": "rank32",
                                                               "verdict": "admit",
                                                               "total_pod_hours": 99.0}))
    for step, start, end in steps:
        log = pod.write(f"logs/e12/{recipe}_{step}_{_stamp(start)}_42.log", "x\n")
        if end is not None:
            os.utime(log, (end, end))
            pod.write(f"{R}/bench/step_{step}.json", json.dumps(
                {"step": step, "utc_start": _iso(start), "utc_end": _iso(end), "outcome": "done"}))
    pod.write(f"{R}/status/repair_A1.json", json.dumps({"segment": "repair", "status": "ok"}))
    sp = pod.write(f"{R}/train/rescue_A1.steps.jsonl", '{"step": 1}\n{"step": 7}\n')
    pod.write(f"{R}/train/rescue_A1.json", json.dumps({"steps_planned": 107}))
    os.utime(sp, None)


def run_progress(env, capsys, recipe="tan32"):
    pod, runner, lap = env
    capsys.readouterr()
    rc = ctl.main(["progress", "--slot", "_A", "--recipe", recipe], runner=runner, root=lap,
                  pod_root=str(pod.emx), pod_scratch=str(pod.scratch), pod_py=sys.executable)
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


H = 3600.0


def test_progress_on_track(env, capsys):
    pod, runner, _lap = env
    now = datetime.now(UTC).timestamp()
    t_p = now - 0.5 * H
    progress_pod(pod, t_p, [("repair", t_p, t_p + 0.4 * H), ("rescue", t_p + 0.4 * H, None)],
                 {"repair": 1.0, "rescue": 0.5, "nudge": 1.0, "upload": 0.1})
    pod.running("pod_run_e12", "e12_pod")
    rc, out = run_progress(env, capsys)
    assert rc == 0 and out["overrun"] is False and out["reasons"] == []
    assert runner.ops == ["progress"]  # ONE ssh call
    assert out["projection"]["stage"] == "lora-check"  # the latest admitted, not the smoke's
    assert out["running_step"] == "rescue" and out["steps"]["rescue"]["state"] == "running"
    rep = out["steps"]["repair"]
    assert rep["state"] == "finished" and rep["actual_h"] == pytest.approx(0.4, abs=0.01)
    assert rep["share_h"] == 1.0 and out["speed_ratio"] == pytest.approx(0.4, abs=0.01)
    # 1 h elapsed (the pod record) + rescue 0.1 + nudge 0.4 + upload 0.04 + margin 0.3
    assert out["extrapolated_total_pod_hours"] == pytest.approx(1.84, abs=0.02)
    assert out["current_run"]["run"] == "rescue_A1" and out["current_run"]["last_step"] == 7
    assert out["current_run"]["steps_planned"] == 107 and out["runs_terminal"] == 1


def test_progress_flags_a_finished_step_over_its_share(env, capsys):
    pod, _runner, _lap = env
    now = datetime.now(UTC).timestamp()
    t_p = now - 2.5 * H
    progress_pod(pod, t_p, [("repair", t_p, t_p + 2.0 * H), ("rescue", t_p + 2.0 * H, None)],
                 {"repair": 1.0, "rescue": 0.5, "nudge": 1.0}, total=20.0)
    pod.running("pod_run_e12", "e12_pod")
    rc, out = run_progress(env, capsys)
    assert rc == ctl.EXIT_OVERRUN == 6 and out["overrun"] is True
    assert len(out["reasons"]) == 1 and out["reasons"][0].startswith("step repair (finished)")
    assert out["steps"]["repair"]["ratio"] == pytest.approx(2.0, abs=0.01)


def test_progress_flags_a_running_step_past_its_share(env, capsys):
    pod, _runner, _lap = env
    now = datetime.now(UTC).timestamp()
    t_p = now - 0.6 * H
    progress_pod(pod, t_p, [("repair", t_p, t_p + 0.1 * H), ("nudge", t_p + 0.1 * H, None)],
                 {"repair": 0.1, "nudge": 0.2}, total=20.0)
    pod.running("pod_run_e12")
    rc, out = run_progress(env, capsys)
    assert rc == 6 and out["reasons"][0].startswith("step nudge (running)")
    assert out["speed_ratio"] == pytest.approx(2.5, abs=0.05)
    # the chain has ended: the last step is no longer running; its log's last write ends it
    pod.running()
    rc, out = run_progress(env, capsys)
    assert out["running_step"] is None and out["steps"]["nudge"]["state"] == "ended"


def test_progress_flags_the_extrapolated_total(env, capsys):
    pod, _runner, _lap = env
    now = datetime.now(UTC).timestamp()
    t_p = now - 0.2 * H
    # every step on its share, but the pod has already run 1 h of a 1.2 h projection
    progress_pod(pod, t_p, [("repair", t_p, t_p + 0.2 * H)], {"repair": 0.2, "nudge": 0.5},
                 total=1.2)
    rc, out = run_progress(env, capsys)
    assert rc == 6 and len(out["reasons"]) == 1 and "extrapolated" in out["reasons"][0]
    assert out["extrapolated_total_pod_hours"] > 1.3 * 1.2


def test_progress_tiny_shares_are_not_overruns(env, capsys):
    pod, _runner, _lap = env
    now = datetime.now(UTC).timestamp()
    t_p = now - 0.1 * H
    progress_pod(pod, t_p, [("repair", t_p, t_p + 120.0)], {"repair": 60 / H, "nudge": 0.1})
    rc, out = run_progress(env, capsys)
    assert rc == 0 and out["steps"]["repair"]["ratio"] == pytest.approx(2.0, abs=0.05)


def test_progress_without_an_admitted_projection_fails(env, capsys):
    rc, out = run_progress(env, capsys)
    assert rc == C.EXIT_FAILED and out["overrun"] is None and "projection" in out["reason"]
    rc = ctl.main(["progress", "--slot", "_A"], root=env[2])
    assert rc == C.EXIT_REFUSED  # --recipe is required


def test_progress_prefers_the_laptops_hf_admission(env, capsys):
    pod, _runner, lap = env
    now = datetime.now(UTC).timestamp()
    progress_pod(pod, now - 0.5 * H, [], {"repair": 1.0})
    f = lap / C.projection_path("tan32", "hf")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"stage": "hf", "verdict": "admit", "total_pod_hours": 9.0,
                             "elapsed_pod_hours": 2.0, "step_hours": {"eval-traj": 1.0}}))
    rc, out = run_progress(env, capsys)
    assert rc == 0 and out["projection"]["where"] == "laptop"
    assert out["projection"]["stage"] == "hf"
    f.write_text(json.dumps({"stage": "hf", "verdict": "refuse", "total_pod_hours": 9.0}))
    rc, out = run_progress(env, capsys)
    assert out["projection"]["stage"] == "lora-check"  # a refused one never counts


def test_progress_script_never_names_the_chain_or_driver_bare(env):
    _pod, runner, lap = env
    body = ctl.progress_body(ctl.Ctx(root=lap, slot="_A", recipe="tan32", runner=runner))
    assert "pod_run_e12" not in body.replace("[p]od_run_e12", "")
    assert "e12_pod" not in body.replace("[e]12_pod", "")
