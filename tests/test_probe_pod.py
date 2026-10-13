"""scripts/probe_pod.py: the generic probe pod runner (no network, no paid calls).

Every subprocess goes through a fake `SH`. git, pod_up/pod_down and the emx CLIs are canned; ssh
and rsync either answer canned output or, in "local pod" mode, run the real remote script under
local bash against a directory standing for the pod's /ws/emx (rsync with the host prefix
stripped), so the launch, poll and pull shell code is exercised for real.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from emx import runpod as rp
from emx import runpod_reservations as rr
from emx import spend

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("probe_pod", ROOT / "scripts" / "probe_pod.py")
pp = importlib.util.module_from_spec(_spec)
sys.modules["probe_pod"] = pp
_spec.loader.exec_module(pp)

POD = "pod123"
GLOBAL_DL = 2_000_000_000
SLOT_DL = 1_999_999_000
SECRET = "sk-or-secret-value"
TRACKED = [
    "src/emx/__init__.py",
    "scripts/pod_killswitch.sh",
    "probes/P01-demo/probe.toml",
    "probes/P01-demo/run.sh",
    "probes/P01-demo/results/old.json",  # a committed result: never sent back
    "pyproject.toml",
    "uv.lock",
]
PROBE_TOML = """id = "P01"
hard_cap_usd = 75
gpu_limit_usd = 60
api_limit_usd = 20

[pod]
gpu_hours = 2
setup = ""
push = ["data/extra"]
env = ["HF_TOKEN"]
poll_minutes = 1
"""
RUN_SH = """echo "start $EMX_PROBE"
R="$(dirname "$0")/results"
mkdir -p "$R"
echo hi > "$R/a.txt"
(cd "$R" && shasum -a 256 a.txt > ../MANIFEST.tmp && mv ../MANIFEST.tmp MANIFEST.sha256)
echo "PROBE_DONE rc=0"
"""


def armed(pod=POD, dl=SLOT_DL, kc="read403"):
    return f"2026-10-11T00:00:00Z [killswitch] ARMED pod={pod} deadline={dl} (2033-05-18T03:33:20Z) key_check={kc}"


class FakeSh:
    """Records every call; canned answers, or real local bash/rsync in local-pod mode."""

    def __init__(self, repo: Path, pod_root: Path):
        self.repo, self.pod_root = repo, pod_root
        self.calls: list[tuple[list[str], dict]] = []
        self.status = ""
        self.untracked: list[str] = []
        self.arm_out = armed()
        self.arm_rc = 0
        self.local = False
        self.fail_logs = False  # rsync of the pod's logs/ fails
        self.fail_results = False  # rsync of the pod's results/ fails (always)
        self.fail_results_n = 0  # ... or only this many times
        self.stop_outs: list[tuple[str, int]] = []  # canned answers to the quiesce script
        self.pod_down_main = None  # run the REAL scripts/pod_down.py main in-process
        self.files_from: list[str] = []
        self.pod_env: str | None = None
        self.pod_up_out = f"EMX_POD_CREATE=ok reservation=R pod={POD}\n"
        self.pod_up_rc = 0
        self.tracked = list(TRACKED)
        self.preflight_outs: list[tuple[str, int]] = []  # canned (stdout, ssh rc) per call

    def argvs(self):
        return [a for a, _ in self.calls]

    def find(self, needle):
        return [a for a in self.argvs() if any(needle in x for x in a)]

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        ok = lambda out="", rc=0, err="": subprocess.CompletedProcess(argv, rc, out, err)
        if argv[0] == "git":
            if argv[1:3] == ["ls-files", "-z"]:
                return ok("\0".join(self.tracked) + "\0")
            if argv[1:3] == ["ls-files", "--others"]:
                return ok("\n".join(self.untracked))
            if argv[1] == "status":
                return ok(self.status)
            if argv[1] == "rev-parse":
                return ok("a" * 40 + "\n")
        if "emx.runpod" in argv and "--killswitch-deadline" in argv:
            return ok(f"EMX_KILLSWITCH_DEADLINE={GLOBAL_DL}\nEMX_KILLSWITCH_SLOTS=_P01\n"
                      f"EMX_KILLSWITCH_DEADLINE_P01={SLOT_DL}\n")
        if "emx.runpod_reservations" in argv:
            return ok()
        if "scripts/pod_down.py" in argv:
            if self.pod_down_main is not None:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    rc = self.pod_down_main(argv[argv.index("scripts/pod_down.py") + 1:])
                return ok(buf.getvalue(), rc)
            return ok("terminated\n")
        if "scripts/pod_up.py" in argv:
            if "--dry-run" not in argv and self.pod_up_rc == 0:
                rp.set_env_var("RUNPOD_POD_ID_P01", POD, self.repo / ".env")
            return ok(self.pod_up_out, self.pod_up_rc)
        if argv[0] == "ssh":
            remote = argv[-1]
            if "pod_killswitch.sh arm" in remote:
                return ok(f"2026-10-11T00:00:00Z [killswitch] arming\n{self.arm_out}\n",
                          self.arm_rc)
            if ".env.part" in remote:
                self.pod_env = kw.get("input")
                return ok()
            if "emx.pod_preflight" in remote and self.preflight_outs:
                out, rc = self.preflight_outs.pop(0)
                return ok(out, rc)
            if "PROBE_QUIESCED" in remote and self.stop_outs:
                out, rc = self.stop_outs.pop(0)
                return ok(out, rc)
            if self.local:
                return subprocess.run(
                    ["bash", "-c", remote.replace("setsid nohup", "nohup")],
                    input=kw.get("input") or "", capture_output=True, text=True, timeout=60, check=False,
                )
            return ok()
        if argv[0] == "rsync":
            ff = [x for x in argv if x.startswith("--files-from=")]
            if ff:
                assert "--from0" in argv
                text = Path(ff[0].split("=", 1)[1]).read_text()
                self.files_from = [f for f in text.split("\0") if f]
                return ok()
            if self.fail_logs and any(x.endswith("/logs/") and ":" in x for x in argv):
                return ok("", 12, "connection reset")
            if any(x.endswith("/results/") and ":" in x for x in argv) and (
                    self.fail_results or self.fail_results_n > 0):
                self.fail_results_n -= 1
                return ok("", 12, "connection reset")
            if self.local:
                real = [x.replace("root@HOST:", "") for x in argv]
                if "-e" in real:
                    i = real.index("-e")
                    del real[i:i + 2]
                return subprocess.run(real, capture_output=True, text=True, timeout=60, check=False)
            return ok()
        if argv[0] == "shasum" or argv[:2] == ["bash", "-n"]:
            return subprocess.run(argv, cwd=kw.get("cwd"), capture_output=True, text=True,
                                  check=False)
        raise AssertionError(f"unexpected call {argv}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    pdir = repo / "probes" / "P01-demo"
    (pdir / "logs").mkdir(parents=True)
    (pdir / "probe.toml").write_text(PROBE_TOML)
    (pdir / "run.sh").write_text(RUN_SH)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "pod_killswitch.sh").write_text("#!/bin/bash\necho fake\n")
    (repo / "data" / "extra").mkdir(parents=True)
    (repo / "data" / "extra" / "a.jsonl").write_text("{}\n")
    (repo / "data" / "E1" / "questions").mkdir(parents=True)
    (repo / "data" / "E1" / "questions" / "narrow.jsonl").write_text("held out\n")
    (repo / "logs").mkdir()
    (repo / ".env").write_text(
        f"HF_TOKEN=hf_abc123\nOPENROUTER_API_KEY={SECRET}\nRUNPOD_API_KEY=rpa_x\n"
        f"RUNPOD_POD_ID_P01={POD}\nPOD_SSH_HOST_P01=HOST\nPOD_SSH_PORT_P01=2222\n"
    )
    pod_root = tmp_path / "pod" / "ws" / "emx"
    (pod_root / "probes" / "P01-demo").mkdir(parents=True)
    monkeypatch.setattr(spend, "PROBES_DIR", repo / "probes")
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "reservations.jsonl"))
    monkeypatch.setattr(rp, "LOGS", repo / "logs")
    monkeypatch.setattr(rp, "ROOT", repo)
    monkeypatch.setattr(rp, "EXP", "E1")
    monkeypatch.setattr(rp, "POD_NAME_PREFIX", "e1-")
    monkeypatch.setattr(rp, "POD_EVENTS", repo / "logs" / "pods_E1.jsonl")
    # created now, not at a fixed date: a running pod's accrued cost grows with its age, so a
    # fixed timestamp drained the kill-line headroom as wall-clock passed and failed the budget
    # tests from about 18 h after it (found 2026-10-12)
    (repo / "logs" / "pods_P01.jsonl").write_text(json.dumps({
        "utc": datetime.now(UTC).isoformat(), "exp": "P01", "event": "created", "pod_id": POD,
        "slot": "_P01", "gpu": "H100", "rate_usd_h": 2.69, "rate_source": "api",
    }) + "\n")
    # a live watchdog heartbeat (this test process's pid)
    (repo / "logs" / "watchdog_P01.heartbeat").write_text(f"{time.time()} 300 {os.getpid()}\n")
    fake = FakeSh(repo, pod_root)
    popened = []
    monkeypatch.setattr(pp, "REPO", repo)
    monkeypatch.setattr(pp, "SH", fake)
    monkeypatch.setattr(pp, "POPEN", lambda *a, **k: popened.append(a))
    clock = {"t": time.time()}  # a fake clock that the fake sleep advances

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(pp, "SLEEP", fake_sleep)
    monkeypatch.setattr(pp, "CLOCK", lambda: clock["t"])
    monkeypatch.setattr(pp, "POD_ROOT", str(pod_root))
    monkeypatch.setattr(pp, "STOP_GRACE_S", 2)
    monkeypatch.setattr(pp.Runner, "api_pod_state", lambda self, pid: "running")  # no network
    return {"repo": repo, "pdir": pdir, "pod": pod_root / "probes" / "P01-demo", "fake": fake,
            "popened": popened, "tmp": tmp_path}


def runner(**kw):
    return pp.Runner("P01", **kw)


def write_state(pdir: Path, **st):
    (pdir / "logs" / "runner_state.json").write_text(json.dumps(st))


def pod_results(pod: Path, files: dict[str, str], bad_hash: bool = False, log_done=True):
    res = pod / "results"
    res.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (res / name).parent.mkdir(parents=True, exist_ok=True)
        (res / name).write_text(text)
    names = sorted(files)
    out = subprocess.run(["shasum", "-a", "256", *names], cwd=res, capture_output=True,
                         text=True, check=True).stdout
    if bad_hash:
        out = "0" * 64 + out[64:]
    (res / "MANIFEST.sha256").write_text(out)
    (pod / "logs").mkdir(exist_ok=True)
    (pod / "logs" / "run_X.log").write_text(
        "start\n" + ("PROBE_DONE rc=0\n" if log_done else "working\n"))


# --- validation ------------------------------------------------------------------------------


def test_id_and_slot_validation(env):
    with pytest.raises(SystemExit) as e:
        pp.main(["status", "--exp", "X1"])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        pp.main(["status"])  # --exp is required
    with pytest.raises(pp.ProbeError):
        pp.Runner("P1")
    with pytest.raises(pp.ProbeError):
        pp.Runner("P01", "bad")
    with pytest.raises(pp.ProbeError):
        pp.Runner("P01", "_waytoolongslot")
    assert runner().slot == "_P01"
    assert pp.Runner("P01", "_B").slot == "_B"
    assert pp.main(["status", "--exp", "P02", "--dry-run"]) == 1  # no probes/P02-*/probe.toml


def test_pod_config_validation(env):
    toml = env["pdir"] / "probe.toml"
    toml.write_text(PROBE_TOML.replace('env = ["HF_TOKEN"]', 'env = ["OPENROUTER_API_KEY"]'))
    with pytest.raises(pp.ProbeError, match="laptop-only"):
        runner()
    toml.write_text(PROBE_TOML.replace('setup = ""', 'setup = "eq0; rm -rf /"'))
    with pytest.raises(pp.ProbeError, match="setup"):
        runner()
    toml.write_text(PROBE_TOML.replace("poll_minutes = 1", "poll_minutes = 1\nbogus = 2"))
    with pytest.raises(pp.ProbeError, match="unknown"):
        runner()


# --- push ------------------------------------------------------------------------------------


def test_push_file_list_env_and_order(env, monkeypatch):
    fake = env["fake"]
    fake.untracked = ["probes/P01-demo/logs/run_1.log", "data/extra/a.jsonl"]  # run output, push
    r = runner()
    monkeypatch.setattr(r, "reservation_for", lambda pid: "rid1")
    assert r.push() == 0
    want = [f for f in TRACKED if "/results/" not in f] + ["data/extra/a.jsonl"]
    assert fake.files_from == want
    assert "data/E1/questions/narrow.jsonl" not in fake.files_from
    keys = [line.split("=", 1)[0] for line in fake.pod_env.splitlines()]
    assert keys == ["HF_TOKEN", "HF_XET_HIGH_PERFORMANCE", "EMX_GIT_COMMIT", "EMX_PROBE"]
    assert "HF_XET_HIGH_PERFORMANCE=1" in fake.pod_env
    assert "HF_TOKEN=hf_abc123" in fake.pod_env and f"EMX_GIT_COMMIT={'a' * 40}" in fake.pod_env
    assert SECRET not in fake.pod_env and "rpa_x" not in fake.pod_env
    argvs = fake.argvs()
    i_send = next(i for i, a in enumerate(argvs) if a[0] == "ssh" and "pod_killswitch.sh.part" in a[-1])
    i_arm = next(i for i, a in enumerate(argvs) if a[0] == "ssh" and "pod_killswitch.sh arm" in a[-1])
    i_ack = next(i for i, a in enumerate(argvs) if "emx.runpod_reservations" in a)
    i_rsync = next(i for i, a in enumerate(argvs) if a[0] == "rsync")
    assert i_send < i_arm < i_ack < i_rsync  # armed and acknowledged before the bulk transfer
    assert argvs[i_arm][-1].endswith(f"arm {POD} {SLOT_DL}")  # the slot's (lower) deadline
    assert argvs[i_ack][-4:] == ["--pod", POD, "--deadline", str(SLOT_DL)]
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert st["phase"] == "pushed" and st["armed_deadline"] == SLOT_DL
    assert not fake.find("scripts/pod_down.py")


def test_push_refuses_held_out_data(env):
    toml = env["pdir"] / "probe.toml"
    toml.write_text(PROBE_TOML.replace('push = ["data/extra"]', 'push = ["data/E1"]'))
    with pytest.raises(pp.ProbeError, match="held out"):
        runner().push()
    assert not [a for a in env["fake"].argvs() if a[0] in ("ssh", "rsync")]


@pytest.mark.parametrize("status,untracked", [
    (" M src/emx/runpod.py", []),
    ("", ["scripts/new_helper.py"]),
    ("", ["probes/P01-demo/analysis.py"]),
])
def test_push_refuses_dirty_tree(env, status, untracked):
    env["fake"].status, env["fake"].untracked = status, untracked
    with pytest.raises(pp.ProbeError, match="uncommitted"):
        runner().push()
    assert not [a for a in env["fake"].argvs() if a[0] in ("ssh", "rsync")]


@pytest.mark.parametrize("arm_out,arm_rc", [
    (armed(pod="otherpod"), 0),
    (armed(dl=SLOT_DL + 5), 0),
    (armed(kc="unverified:nokey"), 0),
    (armed(), 255),  # an ARMED line from a dropped session is not trusted
    ("2026-10-11T00:00:00Z [killswitch] NOT ARMED: no runpodctl", 1),
    (f"ARMED pod={POD} deadline={SLOT_DL} key_check=ok", 0),  # not the killswitch's own format
])
def test_arm_failure_terminates_pod(env, arm_out, arm_rc):
    fake = env["fake"]
    fake.arm_out, fake.arm_rc = arm_out, arm_rc
    assert pp.main(["push", "--exp", "P01"]) == 4
    downs = fake.find("scripts/pod_down.py")
    assert len(downs) == 1 and "--dry-run" not in downs[0]
    assert not [a for a in fake.argvs() if a[0] == "rsync"]
    assert fake.pod_env is None


def test_armed_line_must_match():
    good = armed()
    m = pp.ARMED_RE.match(good)
    assert m and m.groups() == (POD, str(SLOT_DL), "read403")
    assert pp.ARMED_RE.match("x " + good) is None


# --- run / status (local pod) ----------------------------------------------------------------


def test_run_poll_pull_local_pod(env, monkeypatch):
    fake = env["fake"]
    fake.local = True
    (env["pod"] / "run.sh").write_text(RUN_SH)
    r = runner()
    r.sleep = lambda s: time.sleep(min(s, 0.5))
    write_state(env["pdir"], pod_id=POD, phase="pushed", armed_deadline=SLOT_DL)
    assert r.run() == 0
    st = r.load_state()
    assert st["phase"] == "running" and st["run_log"].startswith("probes/P01-demo/logs/run_")
    p = wait_done(r, st["run_log"])
    assert p.reachable and p.done_rc == 0 and p.manifest and p.n_results == 2
    assert "start P01" in "\n".join(p.tail)
    res = r.pull()
    assert res.complete, res
    assert (env["pdir"] / "results" / "a.txt").read_text() == "hi\n"
    mirror = env["pdir"] / "logs" / "pod" / POD
    assert (mirror / Path(st["run_log"]).name).exists()
    assert (mirror / "run.launched").exists() and not (mirror / "run.pid").exists()
    assert not list(env["pdir"].glob(".pull_*"))


def wait_done(r, log):
    p = None
    for _ in range(50):
        p = r.poll(log)
        if p.done_rc is not None:
            break
        time.sleep(0.1)
    return p


@pytest.mark.parametrize("pid_alive", [True, False])
def test_run_adopts_any_earlier_launch(env, pid_alive):
    env["fake"].local = True
    (env["pod"] / "run.sh").write_text(RUN_SH)
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "run.pid").write_text(f"{os.getpid() if pid_alive else 999999}\n")
    (env["pod"] / "logs" / "run_OLD.log").write_text("start\nPROBE_DONE rc=0\n")
    r = runner()
    assert r.run() == 0
    assert sorted(p.name for p in (env["pod"] / "logs").glob("run_*.log")) == ["run_OLD.log"]
    st = r.load_state()
    assert st["phase"] == "running" and st["run_log"] == "probes/P01-demo/logs/run_OLD.log"


def test_crash_between_launch_and_state_save_never_relaunches(env):
    """The launch happened, the state save was lost (laptop crash): `go` from `pushed` adopts the
    first run, its results stay intact, and the pod is settled once."""
    fake = env["fake"]
    fake.local = True
    (env["pod"] / "run.sh").write_text(RUN_SH)
    r = runner()
    r.sleep = lambda s: time.sleep(min(s, 0.5))
    write_state(env["pdir"], pod_id=POD, phase="pushed", armed_deadline=SLOT_DL)
    assert r.run() == 0
    first_log = r.load_state()["run_log"]
    wait_done(r, first_log)
    write_state(env["pdir"], pod_id=POD, phase="pushed", armed_deadline=SLOT_DL)  # save lost
    (env["pod"] / "run.sh").write_text(RUN_SH.replace("echo hi", "echo SECOND"))
    assert pp.main(["go", "--exp", "P01"]) == 0
    assert len(list((env["pod"] / "logs").glob("run_*.log"))) == 1
    assert (env["pod"] / "results" / "a.txt").read_text() == "hi\n"
    assert (env["pdir"] / "results" / "a.txt").read_text() == "hi\n"
    assert pp.Runner("P01").load_state()["run_log"] == first_log
    assert len(fake.find("scripts/pod_down.py")) == 1


def test_run_unverified_launch_is_an_error(env):
    env["fake"].local = True
    (env["pod"] / "run.sh").write_text("sleep 0\n")  # never logs anything
    r = runner()
    r.sleep = lambda s: time.sleep(0.3)
    with pytest.raises(pp.ProbeError, match="not verified"):
        r.run()


# --- pull ------------------------------------------------------------------------------------


def test_pull_mismatch_keeps_staging(env):
    env["fake"].local = True
    pod_results(env["pod"], {"a.txt": "new\n"}, bad_hash=True)
    (env["pdir"] / "results").mkdir()
    (env["pdir"] / "results" / "a.txt").write_text("old\n")
    assert pp.main(["pull", "--exp", "P01"]) == 1
    staging = list(env["pdir"].glob(".pull_*"))
    assert len(staging) == 1 and (staging[0] / "results" / "a.txt").read_text() == "new\n"
    assert (env["pdir"] / "results" / "a.txt").read_text() == "old\n"
    assert (env["pdir"] / "logs" / "pod" / POD / "run_X.log").exists()  # logs arrive regardless


def test_pull_complete_promotes_and_supersedes(env):
    env["fake"].local = True
    pod_results(env["pod"], {"a.txt": "new\n", "sub/b.txt": "b\n"})
    (env["pdir"] / "results").mkdir()
    (env["pdir"] / "results" / "a.txt").write_text("old\n")
    (env["pdir"] / "results" / "laptop_only.csv").write_text("keep\n")
    assert pp.main(["pull", "--exp", "P01"]) == 0
    res = env["pdir"] / "results"
    assert (res / "a.txt").read_text() == "new\n" and (res / "sub" / "b.txt").read_text() == "b\n"
    assert (res / "MANIFEST.sha256").exists() and (res / "laptop_only.csv").exists()
    sup = list((env["pdir"] / "superseded").glob("*/a.txt"))
    assert len(sup) == 1 and sup[0].read_text() == "old\n"
    assert not (env["pdir"] / "unverified").exists()
    assert not list(env["pdir"].glob(".pull_*"))


def test_pull_partial_manifest_is_not_complete(env):
    env["fake"].local = True
    pod_results(env["pod"], {"a.txt": "new\n"})
    (env["pod"] / "results" / "later.ckpt").write_text("written after the manifest\n")
    assert pp.main(["pull", "--exp", "P01"]) == 1
    assert not (env["pdir"] / "results").exists()  # nothing promoted
    kept = sorted(p.name for p in (env["pdir"] / "unverified").rglob("*") if p.is_file())
    assert kept == ["MANIFEST.sha256", "a.txt", "later.ckpt"]


def test_pull_without_manifest_keeps_everything(env):
    env["fake"].local = True
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "run_X.log").write_text("working\n")
    (env["pod"] / "results").mkdir()
    (env["pod"] / "results" / "partial.json").write_text("{}\n")
    write_state(env["pdir"], pod_id=POD, phase="running")
    assert pp.main(["pull", "--exp", "P01"]) == 3
    assert (env["pdir"] / "logs" / "pod" / POD / "run_X.log").read_text() == "working\n"
    assert not (env["pdir"] / "results").exists()
    kept = list((env["pdir"] / "unverified").glob("*/partial.json"))
    assert len(kept) == 1 and kept[0].read_text() == "{}\n"
    assert json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())["phase"] == "running"
    assert not list(env["pdir"].glob(".pull_*"))


def test_pull_with_failed_logs_is_not_complete(env):
    fake = env["fake"]
    fake.local = True
    fake.fail_logs = True
    pod_results(env["pod"], {"a.txt": "x\n"})
    r = runner()
    res = r.pull()
    assert res.results == "complete" and not res.logs_ok and not res.complete
    assert len([a for a in fake.argvs() if a[0] == "rsync" and a[-1].endswith(f"/logs/pod/{POD}/")]) == 3
    assert pp.main(["pull", "--exp", "P01"]) == 1


def test_manifest_paths_refused(tmp_path):
    m = tmp_path / "MANIFEST.sha256"
    m.write_text("0" * 64 + "  ../escape\n")
    with pytest.raises(pp.ProbeError, match="refused"):
        pp.parse_manifest(m)
    m.write_text("0" * 64 + "  ./a.txt\n" + "1" * 64 + "  sub/b.txt\n")
    assert pp.parse_manifest(m) == ["a.txt", "sub/b.txt"]


# --- up --------------------------------------------------------------------------------------


def test_up_reserves_then_launches(env, monkeypatch):
    rp.set_env_var("RUNPOD_POD_ID_P01", "", env["repo"] / ".env")
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 500.0)
    assert pp.main(["up", "--exp", "P01"]) == 0
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert st["phase"] == "up" and st["pod_id"] == POD and st["reservation_id"]
    up = env["fake"].find("scripts/pod_up.py")[0]
    b = f"{math.floor((2 + 0.25 + pp.LAUNCH_ALLOWANCE_H) * pp.PROBE_RATE_USD_H * 100) / 100:.2f}"
    assert up[up.index("--budget-usd") + 1] == b
    assert up[up.index("--reservation") + 1] == st["reservation_id"]
    assert up[up.index("--disk-gb") + 1] == "200"
    assert rr.get(st["reservation_id"])["usd"] == pytest.approx(float(b))
    assert not env["popened"]  # the watchdog was live


def test_up_refused_releases_reservation(env, monkeypatch):
    rp.set_env_var("RUNPOD_POD_ID_P01", "", env["repo"] / ".env")
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 500.0)
    env["fake"].pod_up_out, env["fake"].pod_up_rc = "EMX_POD_CREATE=none reservation=R\n", 4
    assert pp.main(["up", "--exp", "P01"]) == 1
    rid = env["fake"].find("scripts/pod_up.py")[0]
    rid = rid[rid.index("--reservation") + 1]
    assert rr.get(rid)["closed"]


def test_up_uncertain_keeps_reservation(env, monkeypatch):
    rp.set_env_var("RUNPOD_POD_ID_P01", "", env["repo"] / ".env")
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 500.0)
    env["fake"].pod_up_out, env["fake"].pod_up_rc = "EMX_POD_CREATE=uncertain reservation=R\n", 6
    assert pp.main(["up", "--exp", "P01"]) == 6
    rid = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())["reservation_id"]
    assert not rr.get(rid)["closed"]


# --- go --------------------------------------------------------------------------------------


def resumed_state(pdir, **kw):
    write_state(pdir, pod_id=POD, phase="running", run_log="probes/P01-demo/logs/run_X.log",
                armed_deadline=kw.pop("armed_deadline", SLOT_DL), **kw)


def soon():
    """A killswitch deadline an hour after the fake clock's start (bounded recovery loops)."""
    return int(pp.CLOCK()) + 3600


def test_go_resume_does_not_relaunch(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"})
    resumed_state(env["pdir"])
    assert pp.main(["go", "--exp", "P01"]) == 0
    assert not fake.find("scripts/pod_up.py")
    assert not [a for a in fake.argvs() if a[0] == "ssh" and "run.sh\"" in a[-1]]  # no launch
    assert not [a for a in fake.argvs() if a[0] == "ssh" and "pod_killswitch.sh arm" in a[-1]]
    assert len(fake.find("scripts/pod_down.py")) == 1
    assert (env["pdir"] / "results" / "a.txt").read_text() == "x\n"
    assert json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())["phase"] == "down"
    assert pp.main(["go", "--exp", "P01"]) == 0  # finished: a rerun refuses to start a new pod
    assert not fake.find("scripts/pod_up.py")


def test_go_failed_verification_salvages_then_downs(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, bad_hash=True)
    resumed_state(env["pdir"])
    assert pp.main(["go", "--exp", "P01"]) == 5
    assert len(fake.find("scripts/pod_down.py")) == 1
    salv = [p for p in (env["pdir"] / "unverified").rglob("a.txt")]
    assert salv and salv[-1].read_text() == "x\n"
    assert not (env["pdir"] / "results").exists()
    assert "SALVAGED" in (env["pdir"] / "logs" / "ALERT").read_text()
    assert (env["pdir"] / "logs" / "pod" / POD / "run_X.log").exists()


def test_go_failed_salvage_copy_leaves_pod(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, bad_hash=True)
    resumed_state(env["pdir"], armed_deadline=soon())
    fake.fail_results = True
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert not fake.find("scripts/pod_down.py")
    alert = (env["pdir"] / "logs" / "ALERT").read_text()
    assert "out of time" in alert and "LEFT for the killswitch" in alert
    assert pp.Runner("P01").load_state()["phase"] == "recovering"


def test_go_failed_logs_pull_never_terminates(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"})
    resumed_state(env["pdir"], armed_deadline=soon())
    fake.fail_logs = True
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert not fake.find("scripts/pod_down.py")
    assert "logs pull FAILED" in (env["pdir"] / "logs" / "ALERT").read_text()


def test_go_dead_run_without_results_is_salvaged_and_terminated(env):
    env["fake"].local = True
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "run_X.log").write_text("Traceback: boom\n")
    (env["pod"] / "results").mkdir()
    (env["pod"] / "results" / "train_record.json").write_text("{\"loss\": [1.0]}\n")
    resumed_state(env["pdir"])
    assert pp.main(["go", "--exp", "P01"]) == 5
    assert len(env["fake"].find("scripts/pod_down.py")) == 1
    assert list((env["pdir"] / "unverified").glob("*/train_record.json"))
    assert "without PROBE_DONE" in (env["pdir"] / "logs" / "ALERT").read_text()


def test_go_deadline_with_live_run_stops_producer_and_salvages(env):
    fake = env["fake"]
    fake.local = True
    (env["pod"] / "run.sh").write_text(
        'echo start\nmkdir -p "$(dirname "$0")/results"\n'
        'echo partial > "$(dirname "$0")/results/ckpt.txt"\nwhile true; do sleep 1; done\n')
    r = runner()
    r.sleep = lambda s: time.sleep(min(s, 0.5))
    write_state(env["pdir"], pod_id=POD, phase="pushed", armed_deadline=int(time.time()) + 600)
    assert r.run() == 0
    pid = int((env["pod"] / "logs" / "run.pid").read_text())
    assert pp.main(["go", "--exp", "P01"]) == 5  # inside the 15 min margin at once
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the producer was not stopped")
    assert list((env["pdir"] / "unverified").glob("*/ckpt.txt"))
    assert len(fake.find("scripts/pod_down.py")) == 1
    assert not (env["pdir"] / "results").exists()


def test_go_unreachable_pod_confirmed_stopped_is_settled(env, monkeypatch):
    resumed_state(env["pdir"])  # fake ssh answers nothing: unreachable
    states = iter(["running", "stopped"])
    monkeypatch.setattr(pp.Runner, "api_pod_state", lambda self, pid: next(states))
    sleeps = []
    monkeypatch.setattr(pp, "SLEEP", sleeps.append)
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert len(sleeps) == 1  # RUNNING (transient): polled again; then stopped: settled
    assert len(env["fake"].find("scripts/pod_down.py")) == 1
    assert "confirmed stopped" in (env["pdir"] / "logs" / "ALERT").read_text()


# --- recovery (review r2 findings 1, 3, 4) ---------------------------------------------------


def test_stop_failure_before_stop_never_tears_down(env):
    """Every quiesce attempt fails at ssh (rc 255): the copy is kept, nothing is terminated, the
    stop is retried until the deadline, and the pod is left to the killswitch."""
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, log_done=False)
    resumed_state(env["pdir"], armed_deadline=soon())
    fake.stop_outs = [("", 255)] * 10_000
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert not fake.find("scripts/pod_down.py")
    stops = [a for a in fake.argvs() if a[0] == "ssh" and "PROBE_QUIESCED" in a[-1]]
    assert len(stops) > 3  # retried across rounds, not just once
    assert list((env["pdir"] / "unverified").glob("*/a.txt"))  # preserved anyway
    alert = (env["pdir"] / "logs" / "ALERT").read_text()
    assert "NOT confirmed" in alert and "LEFT for the killswitch" in alert


def test_interrupted_stop_then_confirmed_stop_tears_down(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, log_done=False)
    resumed_state(env["pdir"], armed_deadline=soon())
    fake.stop_outs = [("PROBE_QUIESCED pgid=123", 255),  # ack text but the session dropped
                      ("PROBE_STOP_FAILED left=2", 7),
                      ("", 0)]  # exit 0 without an ack (truncated output)
    assert pp.main(["go", "--exp", "P01"]) == 5  # the 4th try is the real script: quiesced
    assert len(fake.find("scripts/pod_down.py")) == 1
    copies = list((env["pdir"] / "unverified").glob("*/a.txt"))
    assert len(copies) == 2  # one kept before the confirmed stop, one after it
    st = pp.Runner("P01").load_state()
    assert st["quiesced"] and st["salvage_quiesced"] and st["phase"] == "down"


def test_recovery_retries_transfer_then_tears_down(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, log_done=False)
    resumed_state(env["pdir"], armed_deadline=soon())
    fake.fail_results_n = 1  # the first results transfer fails, the second succeeds
    assert pp.main(["go", "--exp", "P01"]) == 5
    assert len(fake.find("scripts/pod_down.py")) == 1
    assert list((env["pdir"] / "unverified").glob("*/a.txt"))
    assert list(env["pdir"].glob(".pull_*"))  # the failed partial copy is kept too


def test_go_resumes_recovering_phase(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, log_done=False)
    resumed_state(env["pdir"], armed_deadline=soon())
    st = pp.Runner("P01").load_state()
    st.update(phase="recovering", recover_reason="restart test", quiesced=False, salvage=None)
    write_state(env["pdir"], **st)
    assert pp.main(["go", "--exp", "P01"]) == 5
    status_calls = [a for a in fake.argvs() if a[0] == "ssh" and "__ALIVE" in a[-1]]
    assert not status_calls  # straight back into recovery, no polling or launch
    assert len(fake.find("scripts/pod_down.py")) == 1


@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid unavailable (macOS)")
def test_quiesce_real_process_group(env, monkeypatch):
    """With real setsid: the run and a child in its process group are stopped and acked."""
    fake = env["fake"]
    fake.local = True
    monkey_ssh = fake.__call__

    def ssh_with_setsid(argv, **kw):  # keep setsid this time
        if argv[0] == "ssh" and fake.local and "setsid nohup" in argv[-1]:
            fake.calls.append((list(argv), kw))
            return subprocess.run(["bash", "-c", argv[-1]], input=kw.get("input") or "",
                                  capture_output=True, text=True, timeout=60, check=False)
        return monkey_ssh(argv, **kw)

    monkeypatch.setattr(pp, "SH", ssh_with_setsid)
    (env["pod"] / "run.sh").write_text("echo start\n(sleep 300) &\nwhile true; do sleep 1; done\n")
    r = runner()
    r.sleep = lambda s: time.sleep(min(s, 0.5))
    write_state(env["pdir"], pod_id=POD, phase="pushed", armed_deadline=SLOT_DL)
    assert r.run() == 0
    pid = int((env["pod"] / "logs" / "run.pid").read_text())
    assert r.stop_producer() is True
    left = subprocess.run(["ps", "-eo", "pgid="], capture_output=True, text=True,
                          check=False).stdout.split()
    assert str(pid) not in left


def test_log_mirror_is_per_pod(env):
    fake = env["fake"]
    fake.local = True
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "training.jsonl").write_text('{"step": 1, "run": 1}\n')
    assert runner().pull_logs()
    rp.set_env_var("RUNPOD_POD_ID_P01", "podB", env["repo"] / ".env")
    (env["pod"] / "logs" / "training.jsonl").write_text('{"step": 1, "run": 2}\n')
    assert runner().pull_logs()
    mirror = env["pdir"] / "logs" / "pod"
    assert (mirror / POD / "training.jsonl").read_text() == '{"step": 1, "run": 1}\n'
    assert (mirror / "podB" / "training.jsonl").read_text() == '{"step": 1, "run": 2}\n'


@pytest.fixture
def real_pod_down(env, monkeypatch):
    """The REAL scripts/pod_down.py (in-process) against an API that answers 404 to everything,
    for a reservation-backed P01 pod created 10 minutes ago."""
    spec = importlib.util.spec_from_file_location("pod_down", ROOT / "scripts" / "pod_down.py")
    pod_down = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pod_down)

    def gone(self, method, path, body=None, *, retry=True):
        raise rp.RunPodError(f"{method} {path} failed: HTTP 404: pod not found")

    monkeypatch.setattr(rp.RunPod, "_request", gone)
    rid = rr.reserve("P01", 5.0, "test", balance=500.0)
    t0 = time.time() - 600
    rr.launch(rid, [(POD, 2.69, 5.0)], t0, slot="_P01")
    (env["repo"] / "logs" / "pods_P01.jsonl").write_text(json.dumps({
        "utc": datetime.fromtimestamp(t0, UTC).isoformat(), "exp": "P01", "event": "created",
        "pod_id": POD, "slot": "_P01", "gpu": "H100", "rate_usd_h": 2.69, "rate_source": "api",
        "reservation_id": rid,
    }) + "\n")
    env["fake"].pod_down_main = pod_down.main
    return rid


def assert_settled(env, rid):
    rows = [json.loads(x) for x in (env["tmp"] / "costs.jsonl").read_text().splitlines()]
    gpu = [r for r in rows if r.get("kind") == "gpu" and r.get("exp") == "P01"]
    assert len(gpu) == 1 and f"pod={POD}" in gpu[0]["note"]
    events = [json.loads(x) for x in
              (env["repo"] / "logs" / "pods_P01.jsonl").read_text().splitlines()]
    assert any(e["event"] == "terminated" and e["pod_id"] == POD for e in events)
    assert rr.get(rid)["closed"]


def test_confirmed_gone_settles_through_real_pod_down(env, real_pod_down, monkeypatch):
    """Slot keys remain, ssh is unreachable and the API confirms the pod gone (404): the
    reservation-backed --pod-id path settles it."""
    resumed_state(env["pdir"], reservation_id=real_pod_down)
    monkeypatch.setattr(pp.Runner, "api_pod_state", lambda self, pid: "gone")
    assert pp.main(["go", "--exp", "P01"]) == 1
    down = env["fake"].find("scripts/pod_down.py")
    assert len(down) == 1 and "--pod-id" in down[0]
    assert_settled(env, real_pod_down)
    assert "confirmed gone" in (env["pdir"] / "logs" / "ALERT").read_text()


def test_restart_with_cleared_slot_is_settled(env, real_pod_down, monkeypatch):
    for k in ("RUNPOD_POD_ID_P01", "POD_SSH_HOST_P01", "POD_SSH_PORT_P01"):
        rp.set_env_var(k, "", env["repo"] / ".env")
    resumed_state(env["pdir"], reservation_id=real_pod_down)
    monkeypatch.setattr(pp.Runner, "api_pod_state", lambda self, pid: "gone")
    assert pp.main(["go", "--exp", "P01"]) == 1  # settled, not refused
    assert_settled(env, real_pod_down)
    assert not env["fake"].find("scripts/pod_up.py")


def test_go_refuses_unrecorded_slot_pod(env):
    assert pp.main(["go", "--exp", "P01"]) == 1  # slot holds pod123, no runner_state.json
    assert not env["fake"].find("scripts/pod_up.py")


def test_second_runner_for_a_probe_is_refused(env, capsys):
    resumed_state(env["pdir"])
    first = runner()
    first.acquire_lock()
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert "another probe_pod.py process" in capsys.readouterr().err
    assert not env["fake"].argvs()  # nothing ran


# --- symlinks and .env locking ---------------------------------------------------------------


@pytest.mark.parametrize("push", ['["data/alias/narrow.jsonl"]', '["data/alias"]', '["data/out"]',
                                  '["data/out/x.txt"]'])
def test_push_refuses_symlink_aliases(env, push):
    repo = env["repo"]
    (repo / "data" / "alias").symlink_to(repo / "data" / "E1" / "questions")
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("dummy\n")
    (repo / "data" / "out").symlink_to(outside)
    toml = env["pdir"] / "probe.toml"
    toml.write_text(PROBE_TOML.replace('push = ["data/extra"]', f"push = {push}"))
    with pytest.raises(pp.ProbeError, match="held out|out of the repo|outside the repo"):
        runner().push_list()


def test_push_refuses_control_characters(env):
    (env["repo"] / "data" / "extra" / "bad\nname").write_text("x\n")
    with pytest.raises(pp.ProbeError, match="control character"):
        runner().push_list()


def test_set_env_var_concurrent_no_lost_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=1\n")
    code = ("import sys; from pathlib import Path; from emx import runpod as rp\n"
            "for i in range(15): rp.set_env_var(f'K_{sys.argv[1]}_{i}', str(i), Path(sys.argv[2]))")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(n), str(env_file)])
             for n in range(6)]
    import threading

    threads = [threading.Thread(target=lambda n=n: [rp.set_env_var(f"T_{n}_{i}", "v", env_file)
                                                     for i in range(15)]) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(pr.wait(60) == 0 for pr in procs)
    lines = env_file.read_text().splitlines()
    keys = {line.split("=", 1)[0] for line in lines}
    want = ({"KEEP"} | {f"K_{n}_{i}" for n in range(6) for i in range(15)}
            | {f"T_{n}_{i}" for n in range(4) for i in range(15)})
    assert keys == want and len(lines) == len(want)
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob(".env.*.tmp"))


# --- dry runs --------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["up", "push", "run", "status", "pull", "down", "go"])
def test_dry_run_mutates_nothing(env, cmd):
    write_state(env["pdir"], pod_id=POD, phase="running", run_log="probes/P01-demo/logs/run_X.log",
                armed_deadline=SLOT_DL)
    (env["repo"] / "logs" / "watchdog_P01.heartbeat").unlink()  # up would have to start it
    state_before = (env["pdir"] / "logs" / "runner_state.json").read_text()
    env_before = (env["repo"] / ".env").read_text()
    rc = pp.main([cmd, "--exp", "P01", "--dry-run"])
    assert rc == 0
    for argv in env["fake"].argvs():
        read_only_git = argv[0] == "git" and argv[1] in ("ls-files", "status", "rev-parse")
        syntax_check = argv[:2] == ["bash", "-n"]  # the preflight's read-only run.sh check
        assert read_only_git or syntax_check or "--dry-run" in argv, argv
    assert not env["popened"]
    assert (env["pdir"] / "logs" / "runner_state.json").read_text() == state_before
    assert (env["repo"] / ".env").read_text() == env_before
    assert not (env["tmp"] / "reservations.jsonl").exists()
    assert not (env["pdir"] / "logs" / "ALERT").exists()
    assert not list(env["pdir"].glob(".pull_*"))


# --- stock-out retry and outcome recording (P98/P99 live checks, 2026-10-11) ---------------------


def test_up_with_retry_retries_no_capacity_then_succeeds(env, monkeypatch):
    calls = []

    def fake_up(self):
        calls.append(pp.CLOCK())
        if len(calls) < 3:
            raise pp.ProbeError("pod_up created no pod (rc 1); reservation closed", 7)
        return 0

    monkeypatch.setattr(pp.Runner, "up", fake_up)
    runner().up_with_retry()
    assert len(calls) == 3
    assert [round(b - a) for a, b in itertools.pairwise(calls)] == [120, 240]  # backoff doubles


def test_up_with_retry_gives_up_and_never_retries_other_errors(env, monkeypatch):
    n = {"c": 0}

    def no_capacity(self):
        n["c"] += 1
        raise pp.ProbeError("pod_up created no pod", 7)

    monkeypatch.setattr(pp.Runner, "up", no_capacity)
    with pytest.raises(pp.ProbeError) as e:
        runner().up_with_retry()
    assert e.value.code == 7 and 2 < n["c"] < 30  # bounded by STOCK_RETRY_S

    n["c"] = 0

    def refused(self):
        n["c"] += 1
        raise pp.ProbeError("pod_up refused the launch", 1)

    monkeypatch.setattr(pp.Runner, "up", refused)
    with pytest.raises(pp.ProbeError):
        runner().up_with_retry()
    assert n["c"] == 1


def test_go_records_its_outcome_in_state(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, bad_hash=True)
    resumed_state(env["pdir"])
    assert pp.main(["go", "--exp", "P01"]) == 5
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert (st["last_go_exit"], st["last_go_outcome"]) == (5, "salvaged")


STOCK_OUT = (
    'create failed (POST /pods outcome unknown: HTTP 500: {"error":"create pod: There are no '
    'instances currently available"})\nEMX_POD_CREATE=closed reservation=R\n'
)
STOCK_OUT_RECONCILED = (
    'create outcome UNKNOWN (HTTP 500: {"error":"create pod: There are no longer any instances '
    'available with the requested specifications."})\nno p01-p01 pod exists\n'
    "EMX_POD_CREATE=closed reservation=R\n"
)
# created, then SSH failed and pod_up terminated and settled it: not a stock-out, never retried
CREATED_THEN_SETTLED = (
    "pod created: abc (NVIDIA H100, SECURE, $3.99/h)\nSSH never came up; terminating\n"
    "EMX_POD_CREATE=closed reservation=R\n"
)
BOOKKEEPING = "launch bookkeeping failed\nEMX_POD_CREATE=closed reservation=R\n"


@pytest.mark.parametrize(("out", "code"), [
    (STOCK_OUT, 7), (STOCK_OUT_RECONCILED, 7), (CREATED_THEN_SETTLED, 1), (BOOKKEEPING, 1),
])
def test_up_closed_is_retryable_only_for_a_stock_out(env, monkeypatch, out, code):
    rp.set_env_var("RUNPOD_POD_ID_P01", "", env["repo"] / ".env")
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 500.0)
    env["fake"].pod_up_out, env["fake"].pod_up_rc = out, 1
    with pytest.raises(pp.ProbeError) as e:
        runner().up()
    assert e.value.code == code


def test_lock_rejection_and_finished_rerun_leave_the_outcome_alone(env):
    import fcntl

    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, bad_hash=True)
    resumed_state(env["pdir"])
    assert pp.main(["go", "--exp", "P01"]) == 5
    state = env["pdir"] / "logs" / "runner_state.json"
    before = json.loads(state.read_text())
    assert before["last_go_outcome"] == "salvaged"
    assert pp.main(["go", "--exp", "P01"]) == 0  # finished: refuses to start a new pod
    after = json.loads(state.read_text())
    assert (after["last_go_exit"], after["last_go_outcome"]) == (5, "salvaged")
    with (env["pdir"] / "logs" / "runner.lock").open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another runner holds the probe
        text = state.read_text()
        assert pp.main(["go", "--exp", "P01"]) == 1
        assert state.read_text() == text


def test_budget_buys_the_full_run_hours_at_the_measured_rate(env, capsys):
    toml = env["pdir"] / "probe.toml"
    toml.write_text(toml.read_text().replace("gpu_hours = 2", "gpu_hours = 1"))
    b = runner().budget()  # well under the GPU limit and the fixture's kill-line headroom
    assert pp.PROBE_RATE_USD_H >= 3.99
    run_h = b / pp.PROBE_RATE_USD_H - pp.DEADLINE_MARGIN_S / 3600 - pp.LAUNCH_ALLOWANCE_H
    assert run_h >= 1 - 0.01  # at least gpu_hours of running before the salvage margin
    assert "WARNING" not in capsys.readouterr().out


def test_budget_warns_when_a_cap_cuts_the_run_short(env, capsys):
    toml = env["pdir"] / "probe.toml"
    toml.write_text(toml.read_text().replace("gpu_hours = 2", "gpu_hours = 40"))
    b = runner().budget()
    assert b < (40 + 0.25 + pp.LAUNCH_ALLOWANCE_H) * pp.PROBE_RATE_USD_H
    assert "WARNING (P01): the budget is capped below gpu_hours" in capsys.readouterr().out


# --- [pod].gpus: a probe's own GPU fallback order and rates -----------------------------------

GPUS_LINE = (
    'gpus = [["NVIDIA B300 SXM6 AC", "COMMUNITY", 6.94], ["NVIDIA B300 SXM6 AC", "SECURE", 8.99],'
    ' ["NVIDIA B200", "COMMUNITY", 5.98], ["NVIDIA B200", "SECURE", 7.99]]'
)
DECLARED = (
    rp.GpuOption("NVIDIA B300 SXM6 AC", "COMMUNITY", 6.94),
    rp.GpuOption("NVIDIA B300 SXM6 AC", "SECURE", 8.99),
    rp.GpuOption("NVIDIA B200", "COMMUNITY", 5.98),
    rp.GpuOption("NVIDIA B200", "SECURE", 7.99),
)


def with_gpus(toml_text: str, line: str = GPUS_LINE) -> str:
    return toml_text.replace("poll_minutes = 1", f"poll_minutes = 1\n{line}")


@pytest.mark.parametrize("bad", [
    "gpus = []",
    'gpus = "NVIDIA B200"',
    'gpus = [["NVIDIA B200", "SECURE"]]',
    'gpus = [["NVIDIA B200", "SECURE", 7.99, 1]]',
    'gpus = [["", "SECURE", 7.99]]',
    'gpus = [[3, "SECURE", 7.99]]',
    'gpus = [["NVIDIA B200", "secure", 7.99]]',
    'gpus = [["NVIDIA B200", "SPOT", 7.99]]',
    'gpus = [["NVIDIA B200", "SECURE", 0]]',
    'gpus = [["NVIDIA B200", "SECURE", -1.0]]',
    'gpus = [["NVIDIA B200", "SECURE", 30.01]]',
    'gpus = [["NVIDIA B200", "SECURE", nan]]',
    'gpus = [["NVIDIA B200", "SECURE", inf]]',
    'gpus = [["NVIDIA B200", "SECURE", true]]',
    'gpus = [["NVIDIA B200", "SECURE", "7.99"]]',
])
def test_pod_config_gpus_validation(env, bad):
    (env["pdir"] / "probe.toml").write_text(with_gpus(PROBE_TOML, bad))
    with pytest.raises(pp.ProbeError, match=r"\[pod\]\.gpus"):
        runner()


def test_pod_config_gpus_parsed_in_order(env):
    (env["pdir"] / "probe.toml").write_text(with_gpus(PROBE_TOML))
    cfg = runner().cfg
    assert cfg.gpus == DECLARED and cfg.rate_usd_h == 8.99
    assert rp.probe_gpu_options("P01") == DECLARED  # pod_up.py's reader agrees
    (env["pdir"] / "probe.toml").write_text(PROBE_TOML)
    cfg = runner().cfg
    assert cfg.gpus == () and cfg.rate_usd_h == pp.PROBE_RATE_USD_H
    assert rp.probe_gpu_options("P01") is None
    assert rp.probe_gpu_options("E1") is None  # never a probe: probe.toml is not read


def no_running_pods(monkeypatch):
    """The fixture's running pod accrues cost with wall-clock time and eventually eats the
    kill-line headroom (the known time-dependent failures); these tests take it out."""
    monkeypatch.setattr(pp.Runner, "sessions", lambda self: [])


def test_budget_prices_declared_gpus_at_the_highest_rate(env, monkeypatch, capsys):
    no_running_pods(monkeypatch)
    (env["pdir"] / "probe.toml").write_text(with_gpus(PROBE_TOML))
    b = runner().budget()
    assert b == math.floor((2 + 0.25 + pp.LAUNCH_ALLOWANCE_H) * 8.99 * 100) / 100
    out = capsys.readouterr().out
    assert "h x $8.99/h = $" in out and "$3.99" not in out and "WARNING" not in out
    toml = env["pdir"] / "probe.toml"
    toml.write_text(toml.read_text().replace("gpu_hours = 2", "gpu_hours = 40"))
    b = runner().budget()  # capped by the $60 GPU limit
    out = capsys.readouterr().out
    run_h = b / 8.99 - pp.DEADLINE_MARGIN_S / 3600 - pp.LAUNCH_ALLOWANCE_H
    assert b == 60.0 and f"at $8.99/h the run gets about {run_h:.2f} h" in out


def test_budget_without_gpus_is_unchanged(env, monkeypatch, capsys):
    no_running_pods(monkeypatch)
    b = runner().budget()
    assert b == math.floor((2 + 0.25 + pp.LAUNCH_ALLOWANCE_H) * 3.99 * 100) / 100
    assert pp.PROBE_RATE_USD_H == 3.99
    assert "h x $3.99/h = $" in capsys.readouterr().out


P06_TOML = """id = "P06"
hard_cap_usd = 75
gpu_limit_usd = 60
api_limit_usd = 10

[pod]
gpu_hours = 3
"""


@pytest.fixture
def p06(tmp_path, monkeypatch):
    """emx.runpod configured for a probe P06 in tmp_path (no network: the RunPod client is
    faked), with a fake clock. Call the result with the probe.toml text."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(rp, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    monkeypatch.setattr(spend, "PROBES_DIR", tmp_path / "probes")
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "reservations.jsonl"))
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\n")
    (tmp_path / "logs" / "watchdog_P06.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")
    clock = {"t": time.time()}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def make(toml_text: str):
        pdir = tmp_path / "probes" / "P06-demo"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "probe.toml").write_text(toml_text)
        rp.configure("P06")
        return clock

    return make


class FakeRunPod:
    """create_pod answers from `results` (an Exception is raised); no rate unless given."""

    def __init__(self, results=(), api_rate=None):
        self.results, self.api_rate = list(results), api_rate
        self.created: list[dict] = []

    def create_pod(self, body):
        self.created.append(body)
        r = self.results.pop(0) if self.results else {}
        if isinstance(r, Exception):
            raise r
        return r

    def get_pod(self, pod_id):
        rate = {"costPerHr": self.api_rate} if self.api_rate else {}
        return {"publicIp": "1.2.3.4", "portMappings": {"22": 40022},
                "desiredStatus": "RUNNING", **rate}

    def list_pods(self):
        return []

    def terminate_pod(self, pod_id):
        raise AssertionError("no pod is terminated here")


def load_pod_up():
    spec = importlib.util.spec_from_file_location("_probe_pod_up", ROOT / "scripts" / "pod_up.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dry_run_gpus(fake, monkeypatch, *extra):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert load_pod_up().main(["--exp", "P06", "--slot", "_P06", "--dry-run", *extra]) == 0
    return [(b["gpuTypeIds"], b["cloudType"], b["gpuCount"]) for b in fake.created]


def test_pod_up_dry_run_uses_the_probes_gpus(p06, monkeypatch):
    p06(P06_TOML + GPUS_LINE + "\n")
    want = [([o.gpu_type_id], o.cloud, 1) for o in DECLARED]
    assert dry_run_gpus(FakeRunPod(), monkeypatch) == want
    assert dry_run_gpus(FakeRunPod(), monkeypatch, "--start", "2") == want[2:]
    assert dry_run_gpus(FakeRunPod(), monkeypatch, "--start", "1", "--no-fallback") == want[1:2]
    assert dry_run_gpus(FakeRunPod(), monkeypatch, "--gpu", "nvidia b200") == want[2:]


def test_pod_up_probe_without_gpus_is_unchanged(p06, monkeypatch):
    p06(P06_TOML)
    fake = FakeRunPod()
    assert dry_run_gpus(fake, monkeypatch) == [([o.gpu_type_id], o.cloud, 1)
                                               for o in rp.GPU_FALLBACK]
    assert fake.created == [rp.pod_body(o, "_P06", fake.created[0]["env"]["PUBLIC_KEY"])
                            for o in rp.GPU_FALLBACK]  # the very same request bodies


@pytest.mark.parametrize("stock_out, rate", [(False, 6.94), (True, 8.99)])
def test_pod_up_reserved_launch_prices_the_declared_gpu(p06, monkeypatch, capsys, stock_out,
                                                        rate):
    clock = p06(P06_TOML + GPUS_LINE + "\n")
    budget = 31.47  # (3 + 0.5) h x $8.99, as probe_pod's budget() gives
    rid = rr.reserve("P06", budget, "P06:_P06:test", balance=500.0)
    results = [rp.RunPodError('HTTP 500: {"error":"create pod: There are no instances '
                              'currently available"}')] if stock_out else []
    fake = FakeRunPod(results + [{"id": "pb"}])  # no costPerHr anywhere: the declared rate
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    t_start = clock["t"]
    rc = load_pod_up().main(["--exp", "P06", "--slot", "_P06", "--reservation", rid,
                             "--budget-usd", f"{budget}"])
    assert rc == 0
    assert [b["cloudType"] for b in fake.created] == (["COMMUNITY", "SECURE"] if stock_out
                                                     else ["COMMUNITY"])
    assert fake.created[-1]["gpuTypeIds"] == ["NVIDIA B300 SXM6 AC"]
    rows = rr.read_rows()
    assert [r["rate_usd_h"] for r in rows if r["kind"] == "creating"][-1] == rate
    want = int(t_start + budget / rate * 3600)  # not budget / $3.99
    assert rr.pod_deadline("pb") == want
    assert f"EMX_POD_DEADLINE={want}" in capsys.readouterr().out
    (s,) = rp.pod_sessions(rp.read_pod_events())
    assert s["rate"] == rate and s["gpu"] == "NVIDIA B300 SXM6 AC"
    # the global kill line burns at the pod's rate too (killswitch deadline, watchdog projection)
    now = datetime.fromtimestamp(t_start, UTC)
    assert rp.killswitch_deadline("pb", now=now) == int(
        t_start + rp.current_kill_limit() / rate * 3600)
    assert rp.running_projection(rp.pod_sessions(rp.read_pod_events(), now), 1.0) == rate


# --- preflight before any pod (wave 4, 2026-10-13) ------------------------------------------


def free_slot(env, monkeypatch):
    rp.set_env_var("RUNPOD_POD_ID_P01", "", env["repo"] / ".env")
    monkeypatch.setattr(rr, "balance", lambda *a, **k: 500.0)


def assert_no_launch(env):
    assert not env["fake"].find("scripts/pod_up.py")
    assert not (env["tmp"] / "reservations.jsonl").exists()
    assert not [a for a in env["fake"].argvs() if a[0] in ("ssh", "rsync")]


def with_setup_script(env):
    shutil.copy(ROOT / "scripts" / "pod_setup.sh", env["repo"] / "scripts" / "pod_setup.sh")


@pytest.mark.parametrize("cmd", ["up", "go"])
def test_preflight_refuses_dirty_tree_before_any_pod(env, monkeypatch, capsys, cmd):
    free_slot(env, monkeypatch)
    env["fake"].status = " M src/emx/runpod.py"
    assert pp.main([cmd, "--exp", "P01"]) == 1
    assert "preflight refused the launch (no pod was created)" in capsys.readouterr().err
    assert_no_launch(env)


def toml_with(**pod):
    extra = "".join(f"{k} = {v}\n" for k, v in pod.items())
    return PROBE_TOML + extra


def setup_line(env, value):
    t = env["pdir"] / "probe.toml"
    t.write_text(t.read_text().replace('setup = ""', f'setup = "{value}"'))


@pytest.mark.parametrize("case,match", [
    ("untracked", "uncommitted"),
    ("run_sh_untracked", "run.sh is not tracked"),
    ("run_sh_syntax", "syntax error"),
    ("push_missing", "does not exist"),
    ("env_key_empty", "HF_USERNAME is empty"),
    ("setup_unknown", "not a pod_setup.sh --only target"),
    ("disk_small", "disk_gb 200 < model_gb 170"),
    ("gate_without_setup", r"set \[pod\]\.setup"),
    ("no_killswitch", "pod_killswitch.sh is missing"),
])
def test_preflight_refusals(env, monkeypatch, case, match):
    free_slot(env, monkeypatch)
    fake, pdir, toml = env["fake"], env["pdir"], env["pdir"] / "probe.toml"
    if case == "untracked":
        fake.untracked = ["probes/P01-demo/analysis.py"]
    elif case == "run_sh_untracked":
        fake.tracked.remove("probes/P01-demo/run.sh")
    elif case == "run_sh_syntax":
        (pdir / "run.sh").write_text('if [ 1 = 1 ]; then\n  echo "PROBE_DONE rc=0"\n')
    elif case == "push_missing":
        toml.write_text(PROBE_TOML.replace('push = ["data/extra"]', 'push = ["data/nope"]'))
    elif case == "env_key_empty":
        toml.write_text(PROBE_TOML.replace('env = ["HF_TOKEN"]', 'env = ["HF_USERNAME"]'))
    elif case == "setup_unknown":
        with_setup_script(env)
        setup_line(env, "nosuch")
    elif case == "disk_small":
        toml.write_text(toml_with(model_gb=170))
    elif case == "gate_without_setup":
        toml.write_text(toml_with(gpu_check="true"))
    elif case == "no_killswitch":
        (env["repo"] / "scripts" / "pod_killswitch.sh").unlink()
    with pytest.raises(pp.ProbeError, match=match) as e:
        runner().up()
    assert e.value.code == 1 and "no pod was created" in str(e.value)
    assert_no_launch(env)


def test_preflight_reports_every_problem_at_once(env, monkeypatch):
    free_slot(env, monkeypatch)
    env["fake"].untracked = ["scripts/new.py"]
    (env["pdir"] / "probe.toml").write_text(toml_with(model_gb=500, gpu_check="true"))
    with pytest.raises(pp.ProbeError) as e:
        runner().up()
    msg = str(e.value)
    assert "uncommitted" in msg and "disk_gb" in msg and "set [pod].setup" in msg
    assert msg.count("\n- ") == 3


def test_preflight_passes_and_accepts_known_setup_and_enough_disk(env, monkeypatch, capsys):
    with_setup_script(env)
    (env["pdir"] / "probe.toml").write_text(
        toml_with(model_gb=160, gpu_check="true", model_repo='"org/model"',
                  model_revision=f'"{"b" * 40}"').replace("disk_gb", "x").replace(
            'setup = ""', 'setup = "i1a"\ndisk_gb = 200'))
    assert runner().preflight() == "a" * 40
    out = capsys.readouterr().out
    assert "on-pod gates: gpu self-test, download >= 150 MB/s from org/model" in out


def test_setup_targets_match_pod_setup_sh(env):
    with_setup_script(env)
    assert {"", "i1a", "i9", "e1b", "e3", "e9", "eq0", "e11", "e12", "e13"} <= (
        runner().setup_targets())
    assert "*" not in runner().setup_targets()


def test_preflight_warns_on_missing_probe_done_and_branch_revision(env, capsys):
    with_setup_script(env)
    (env["pdir"] / "run.sh").write_text("echo start\n")
    (env["pdir"] / "probe.toml").write_text(
        toml_with(model_repo='"org/model"').replace('setup = ""', 'setup = "i1a"'))
    runner().preflight()
    out = capsys.readouterr().out
    assert "never mentions PROBE_DONE" in out and "is not a commit" in out


@pytest.mark.parametrize("line", [
    "model_gb = -1", 'model_repo = "no-slash"', 'model_repo = "a/b; rm"',
    'model_revision = "a b"', "min_download_mb_s = -5", 'gpu_check = "yes"', "rerolls = 9",
    "rerolls = true",
])
def test_new_pod_fields_are_validated(env, line):
    (env["pdir"] / "probe.toml").write_text(PROBE_TOML + line + "\n")
    with pytest.raises(pp.ProbeError, match=r"\[pod\]\."):
        runner()


def test_run_outputs_and_notes_never_block_a_push(env):
    """P33: a committed log mirror that a later pull updated; P34: log.md edited after the
    commit. Neither is code the pod runs; the notes are not sent at all."""
    fake = env["fake"]
    fake.tracked += ["probes/P01-demo/log.md", "probes/P01-demo/logs/pod/old/run_1.log"]
    fake.status = (" M probes/P01-demo/log.md\n M probes/P01-demo/logs/pod/old/run_1.log\n")
    fake.untracked = ["probes/P01-demo/note.md"]
    assert runner().check_clean() == "a" * 40
    assert "probes/P01-demo/log.md" not in runner().push_list()
    fake.status += " M probes/P01-demo/analysis.py\n"
    with pytest.raises(pp.ProbeError, match="analysis.py"):
        runner().check_clean()


def test_tree_changed_during_reservation_wait_releases_it(env, monkeypatch):
    free_slot(env, monkeypatch)
    real = rr.wait_and_reserve

    def reserve_then_edit(*a, **k):
        rid = real(*a, **k)
        env["fake"].status = " M scripts/probe_pod.py"  # an agent edits while the launch waits
        return rid

    monkeypatch.setattr(rr, "wait_and_reserve", reserve_then_edit)
    with pytest.raises(pp.ProbeError, match="uncommitted"):
        runner().up()
    assert not env["fake"].find("scripts/pod_up.py")
    rids = {r["reservation_id"] for r in rr.read_rows()}
    assert len(rids) == 1 and rr.get(rids.pop())["closed"]
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert st["phase"] == "idle" and st["reservation_id"] is None


def test_hf_token_is_sent_even_when_not_listed(env):
    (env["pdir"] / "probe.toml").write_text(PROBE_TOML.replace('env = ["HF_TOKEN"]', "env = []"))
    text = runner().pod_env("a" * 40)
    assert "HF_TOKEN=hf_abc123" in text and "HF_XET_HIGH_PERFORMANCE=1" in text
    assert SECRET not in text


# --- on-pod preflight and re-rolls ------------------------------------------------------------

PF_OK = "preflight gpu: ok (free=180GB)\nPROBE_PREFLIGHT_OK download mb_s=420\n__RC=0\n"
PF_SLOW = ("preflight gpu: ok\nPROBE_PREFLIGHT_SLOW_DOWNLOAD mb_s=61.2 min=150 bytes=3672000000 "
           "seconds=60.0\n__RC=11\n")
PF_GPU = ("preflight gpu: try 3/3 failed: CUDA error: CUDA-capable device(s) is/are busy or "
          "unavailable\nPROBE_PREFLIGHT_GPU_FAIL no working CUDA context after 3 tries\n__RC=10\n")
PF_ERR = "PROBE_PREFLIGHT_ERROR download: could not list org/model@main: 401\n__RC=12\n"


def gated(env, monkeypatch, **extra):
    with_setup_script(env)
    pod = {"gpu_check": "true", "model_repo": '"org/model"', **extra}
    (env["pdir"] / "probe.toml").write_text(
        toml_with(**pod).replace('setup = ""', 'setup = "i1a"'))
    monkeypatch.setattr(pp.Runner, "setup", lambda self, addr: None)


def test_push_runs_the_on_pod_preflight_after_setup(env, monkeypatch):
    gated(env, monkeypatch)
    fake = env["fake"]
    fake.preflight_outs = [(PF_OK, 0)]
    r = runner()
    monkeypatch.setattr(r, "reservation_for", lambda pid: "rid1")
    assert r.push() == 0
    remote = fake.find("emx.pod_preflight")[0][-1]
    assert "--gpu --repo org/model --revision main --min-mb-s 150" in remote
    assert "export HF_HOME=/ws/hf_cache" in remote and "--no-xet" not in remote
    i_rsync = next(i for i, a in enumerate(fake.argvs()) if a[0] == "rsync")
    i_pf = next(i for i, a in enumerate(fake.argvs()) if "emx.pod_preflight" in a[-1])
    assert i_rsync < i_pf
    assert r.load_state()["phase"] == "pushed"
    assert not fake.find("scripts/pod_down.py")


def test_preflight_follows_run_sh_xet_and_hf_home(env, monkeypatch):
    gated(env, monkeypatch, min_download_mb_s=200)
    (env["pdir"] / "run.sh").write_text(
        "export HF_HOME=/ws/hf_other HF_HUB_DISABLE_XET=1 WANDB_MODE=offline\n" + RUN_SH)
    r = runner()
    assert r.preflight_args()[-3:] == ["--min-mb-s", "200", "--no-xet"]
    assert "export HF_HOME=/ws/hf_other " in r.preflight_remote("L")


@pytest.mark.parametrize("out", [PF_SLOW, PF_GPU])
def test_bad_host_pulls_logs_terminates_and_exits_8(env, monkeypatch, out):
    gated(env, monkeypatch)
    fake = env["fake"]
    fake.preflight_outs = [(out, 0)]
    assert pp.main(["push", "--exp", "P01"]) == pp.BAD_HOST
    argvs = fake.argvs()
    i_logs = next(i for i, a in enumerate(argvs) if a[0] == "rsync" and any(
        x.endswith("/logs/") and ":" in x for x in a))
    downs = [i for i, a in enumerate(argvs) if "scripts/pod_down.py" in a]
    assert len(downs) == 1 and i_logs < downs[0]
    assert "bad host" in (env["pdir"] / "logs" / "ALERT").read_text()


def test_preflight_error_or_silence_is_not_a_bad_host(env, monkeypatch):
    gated(env, monkeypatch)
    fake = env["fake"]
    fake.preflight_outs = [(PF_ERR, 0)]
    with pytest.raises(pp.ProbeError) as e:
        runner().push()
    assert e.value.code == 1 and not fake.find("scripts/pod_down.py")
    fake.preflight_outs = [("", 255), ("", 255)]  # ssh dropped twice
    with pytest.raises(pp.ProbeError, match="did not report") as e:
        runner().push()
    assert e.value.code == 1


def test_preflight_shell_runs_and_reports_a_missing_venv(env, monkeypatch):
    """The real remote script under local bash: no /ws/venv-* here, so it must report
    PROBE_PREFLIGHT_ERROR with rc 12 (not hang, not pass)."""
    gated(env, monkeypatch)
    if list(Path("/ws").glob("venv-*")):
        pytest.skip("a real /ws/venv-* exists on this machine")
    env["fake"].local = True
    r = runner()
    with pytest.raises(pp.ProbeError, match="rc 12") as e:
        r.pod_preflight(r.pod_addr())
    assert e.value.code == 1


def test_up_maps_pod_up_ssh_timeout_to_bad_host(env, monkeypatch):
    free_slot(env, monkeypatch)
    env["fake"].pod_up_out = (
        "pod created: abc (NVIDIA H100, SECURE, $3.99/h)\n  waiting... status=RUNNING ip=-\n"
        "EMX_POD_CREATE=closed reservation=R pod=abc\nssh timeout; pod abc terminated\n")
    env["fake"].pod_up_rc = 1
    with pytest.raises(pp.ProbeError) as e:
        runner().up()
    assert e.value.code == pp.BAD_HOST


def fake_chain(monkeypatch, env, bad: list[bool]):
    """up records a new pod per call; push fails with BAD_HOST while `bad` says so (the real push
    has already terminated that pod); watch ends the run."""
    ups = []

    def up(self):
        self.preflight()
        n = len(ups)
        ups.append(n)
        st = self.load_state()
        st.update(pod_id=f"pod{n}", phase="up", reservation_id=f"r{n}")
        self.save_state(st)
        return 0

    def push(self):
        if bad[len(ups) - 1]:
            raise pp.ProbeError("bad host: on-pod preflight slow_download (rc 11)", pp.BAD_HOST)
        st = self.load_state()
        st.update(phase="pushed")
        self.save_state(st)
        return 0

    monkeypatch.setattr(pp.Runner, "up", up)
    monkeypatch.setattr(pp.Runner, "push", push)
    monkeypatch.setattr(pp.Runner, "run", lambda self: 0)
    monkeypatch.setattr(pp.Runner, "watch", lambda self, st: 0)
    free_slot(env, monkeypatch)
    return ups


def test_go_rerolls_a_bad_host(env, monkeypatch, capsys):
    ups = fake_chain(monkeypatch, env, [True, False])
    assert pp.main(["go", "--exp", "P01"]) == 0
    assert len(ups) == 2
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert [x["pod_id"] for x in st["rerolls"]] == ["pod0"] and st["pod_id"] == "pod1"
    assert "PROBE_REROLL 1/2" in capsys.readouterr().out


def test_go_gives_up_after_rerolls(env, monkeypatch):
    ups = fake_chain(monkeypatch, env, [True, True, True, True])
    (env["pdir"] / "probe.toml").write_text(toml_with(rerolls=1))
    assert pp.main(["go", "--exp", "P01"]) == pp.BAD_HOST
    assert len(ups) == 2
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert len(st["rerolls"]) == 2 and st["last_go_outcome"] == "bad_host"
    assert st["phase"] == "idle"


def test_go_never_rerolls_other_push_failures(env, monkeypatch):
    ups = fake_chain(monkeypatch, env, [False])
    monkeypatch.setattr(pp.Runner, "push", lambda self: (_ for _ in ()).throw(
        pp.ProbeError("rsync failed", 1)))
    monkeypatch.setattr(pp.Runner, "down", lambda self, **k: 0)
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert len(ups) == 1


def test_go_real_push_bad_host_then_new_pod(env, monkeypatch):
    """The real push: the slow host's logs are pulled and it is terminated, then a new pod is
    created and passes."""
    gated(env, monkeypatch)
    fake = env["fake"]
    fake.preflight_outs = [(PF_SLOW, 0), (PF_OK, 0)]
    ups = []

    def up(self):
        ups.append(1)
        for k, v in (("RUNPOD_POD_ID_P01", POD), ("POD_SSH_HOST_P01", "HOST"),
                     ("POD_SSH_PORT_P01", "2222")):
            rp.set_env_var(k, v, env["repo"] / ".env")
        st = self.load_state()
        st.update(pod_id=POD, phase="up")
        self.save_state(st)

    monkeypatch.setattr(pp.Runner, "up", up)
    monkeypatch.setattr(pp.Runner, "reservation_for", lambda self, pid: None)
    monkeypatch.setattr(pp.Runner, "run", lambda self: 0)
    monkeypatch.setattr(pp.Runner, "watch", lambda self, st: 0)
    free_slot(env, monkeypatch)
    assert pp.main(["go", "--exp", "P01"]) == 0
    assert len(ups) == 2 and len(fake.find("scripts/pod_down.py")) == 1
    st = json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())
    assert st["phase"] == "pushed" and len(st["rerolls"]) == 1
