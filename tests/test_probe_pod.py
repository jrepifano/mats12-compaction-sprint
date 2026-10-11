"""scripts/probe_pod.py: the generic probe pod runner (no network, no paid calls).

Every subprocess goes through a fake `SH`. git, pod_up/pod_down and the emx CLIs are canned; ssh
and rsync either answer canned output or, in "local pod" mode, run the real remote script under
local bash against a directory standing for the pod's /ws/emx (rsync with the host prefix
stripped), so the launch, poll and pull shell code is exercised for real.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
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
        self.files_from: list[str] = []
        self.pod_env: str | None = None
        self.pod_up_out = f"EMX_POD_CREATE=ok reservation=R pod={POD}\n"
        self.pod_up_rc = 0

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
                return ok("\0".join(TRACKED) + "\0")
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
            if self.local:
                return subprocess.run(
                    ["bash", "-c", remote.replace("setsid nohup", "nohup")],
                    input=kw.get("input") or "", capture_output=True, text=True, timeout=60, check=False,
                )
            return ok()
        if argv[0] == "rsync":
            ff = [x for x in argv if x.startswith("--files-from=")]
            if ff:
                self.files_from = Path(ff[0].split("=", 1)[1]).read_text().split()
                return ok()
            if self.local:
                real = [x.replace("root@HOST:", "") for x in argv]
                if "-e" in real:
                    i = real.index("-e")
                    del real[i:i + 2]
                return subprocess.run(real, capture_output=True, text=True, timeout=60, check=False)
            return ok()
        if argv[0] == "shasum":
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
    (repo / "logs" / "pods_P01.jsonl").write_text(json.dumps({
        "utc": "2026-10-11T00:00:00+00:00", "exp": "P01", "event": "created", "pod_id": POD,
        "slot": "_P01", "gpu": "H100", "rate_usd_h": 2.69, "rate_source": "api",
    }) + "\n")
    # a live watchdog heartbeat (this test process's pid)
    (repo / "logs" / "watchdog_P01.heartbeat").write_text(f"{time.time()} 300 {os.getpid()}\n")
    fake = FakeSh(repo, pod_root)
    popened = []
    monkeypatch.setattr(pp, "REPO", repo)
    monkeypatch.setattr(pp, "SH", fake)
    monkeypatch.setattr(pp, "POPEN", lambda *a, **k: popened.append(a))
    monkeypatch.setattr(pp, "SLEEP", lambda s: None)
    monkeypatch.setattr(pp, "POD_ROOT", str(pod_root))
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
    assert keys == ["HF_TOKEN", "EMX_GIT_COMMIT", "EMX_PROBE"]
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
    p = None
    for _ in range(40):
        p = r.poll(st["run_log"])
        if p.done_rc is not None:
            break
        time.sleep(0.1)
    assert p.reachable and p.done_rc == 0 and p.manifest and p.n_results == 2
    assert "start P01" in "\n".join(p.tail)
    assert r.pull() == "verified"
    assert (env["pdir"] / "results" / "a.txt").read_text() == "hi\n"
    assert (env["pdir"] / "logs" / Path(st["run_log"]).name).exists()
    assert not list(env["pdir"].glob(".pull_*"))


def test_run_refuses_live_previous_run(env):
    env["fake"].local = True
    (env["pod"] / "run.sh").write_text(RUN_SH)
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "run.pid").write_text(f"{os.getpid()}\n")  # alive
    with pytest.raises(pp.ProbeError) as e:
        runner().run()
    assert e.value.code == 3
    assert not list((env["pod"] / "logs").glob("run_*.log"))


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
    assert (env["pdir"] / "logs" / "run_X.log").exists()  # logs arrive regardless


def test_pull_match_promotes_and_supersedes(env):
    env["fake"].local = True
    pod_results(env["pod"], {"a.txt": "new\n", "sub/b.txt": "b\n"})
    (env["pod"] / "results" / "scratch.tmp").write_text("unlisted\n")
    (env["pdir"] / "results").mkdir()
    (env["pdir"] / "results" / "a.txt").write_text("old\n")
    (env["pdir"] / "results" / "laptop_only.csv").write_text("keep\n")
    assert pp.main(["pull", "--exp", "P01"]) == 0
    res = env["pdir"] / "results"
    assert (res / "a.txt").read_text() == "new\n" and (res / "sub" / "b.txt").read_text() == "b\n"
    assert (res / "MANIFEST.sha256").exists() and (res / "laptop_only.csv").exists()
    sup = list((env["pdir"] / "superseded").glob("*/a.txt"))
    assert len(sup) == 1 and sup[0].read_text() == "old\n"
    assert [p.name for p in (env["pdir"] / "unverified").rglob("*") if p.is_file()] == ["scratch.tmp"]
    assert not (res / "scratch.tmp").exists()
    assert not list(env["pdir"].glob(".pull_*"))


def test_pull_without_manifest_pulls_logs_only(env):
    env["fake"].local = True
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "run_X.log").write_text("working\n")
    (env["pod"] / "results").mkdir()
    (env["pod"] / "results" / "partial.json").write_text("{}\n")
    write_state(env["pdir"], pod_id=POD, phase="running")
    assert pp.main(["pull", "--exp", "P01"]) == 3
    assert (env["pdir"] / "logs" / "run_X.log").read_text() == "working\n"
    assert not (env["pdir"] / "results").exists()
    assert json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())["phase"] == "running"
    assert not list(env["pdir"].glob(".pull_*"))


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
    b = f"{2 * rp.FALLBACK_RATE:.2f}"
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


def test_go_does_not_down_when_verification_fails(env):
    env["fake"].local = True
    pod_results(env["pod"], {"a.txt": "x\n"}, bad_hash=True)
    write_state(env["pdir"], pod_id=POD, phase="running", run_log="probes/P01-demo/logs/run_X.log",
                armed_deadline=SLOT_DL)
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert not env["fake"].find("scripts/pod_down.py")
    assert not env["fake"].find("scripts/pod_up.py")
    alert = (env["pdir"] / "logs" / "ALERT").read_text()
    assert "LEFT RUNNING" in alert
    assert len(list(env["pdir"].glob(".pull_*"))) == 1


def test_go_resume_does_not_relaunch(env):
    fake = env["fake"]
    fake.local = True
    pod_results(env["pod"], {"a.txt": "x\n"})
    write_state(env["pdir"], pod_id=POD, phase="running", run_log="probes/P01-demo/logs/run_X.log",
                armed_deadline=SLOT_DL)
    assert pp.main(["go", "--exp", "P01"]) == 0
    assert not fake.find("scripts/pod_up.py")
    assert not [a for a in fake.argvs() if a[0] == "ssh" and "run.sh" in a[-1]]  # no relaunch
    assert not [a for a in fake.argvs() if a[0] == "ssh" and "pod_killswitch.sh arm" in a[-1]]
    assert len(fake.find("scripts/pod_down.py")) == 1
    assert (env["pdir"] / "results" / "a.txt").read_text() == "x\n"
    assert json.loads((env["pdir"] / "logs" / "runner_state.json").read_text())["phase"] == "down"
    assert pp.main(["go", "--exp", "P01"]) == 0  # finished: a rerun refuses to start a new pod
    assert not fake.find("scripts/pod_up.py")


def test_go_dead_run_without_results_is_terminated(env):
    env["fake"].local = True
    (env["pod"] / "logs").mkdir()
    (env["pod"] / "logs" / "run_X.log").write_text("Traceback: boom\n")
    (env["pod"] / "results").mkdir()
    write_state(env["pdir"], pod_id=POD, phase="running", run_log="probes/P01-demo/logs/run_X.log",
                armed_deadline=SLOT_DL)
    assert pp.main(["go", "--exp", "P01"]) == 1
    assert len(env["fake"].find("scripts/pod_down.py")) == 1
    assert "no results at all" in (env["pdir"] / "logs" / "ALERT").read_text()


def test_go_refuses_unrecorded_slot_pod(env):
    assert pp.main(["go", "--exp", "P01"]) == 1  # slot holds pod123, no runner_state.json
    assert not env["fake"].find("scripts/pod_up.py")


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
        assert read_only_git or "--dry-run" in argv, argv
    assert not env["popened"]
    assert (env["pdir"] / "logs" / "runner_state.json").read_text() == state_before
    assert (env["repo"] / ".env").read_text() == env_before
    assert not (env["tmp"] / "reservations.jsonl").exists()
    assert not (env["pdir"] / "logs" / "ALERT").exists()
    assert not list(env["pdir"].glob(".pull_*"))
