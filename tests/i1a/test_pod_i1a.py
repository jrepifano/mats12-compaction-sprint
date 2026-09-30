"""U1 pod infrastructure for I1a: experiment-generic runpod/pod scripts (E1 unchanged), the I1a
watchdog, the I1a run chain and the named-file HF downloader. No network, no pods, no paid calls:
HTTP clients, `emx.spend`, `uv`, the on-pod driver and the Hub are all faked, and every file the
scripts write lands in tmp_path (never the real logs/ or results/).

Round 2 covers the fixes for gpt-6-sol review U1 r1 (logs/reviews/20260929-I1a-code-U1-r1.md):
checkpoint gating, non-idempotent pod creation, a fail-closed watchdog with a heartbeat, E1
isolation from an exported EMX_EXP, an explicit rsync transfer set, and manifest sidecars."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from emx import runpod as rp
from emx.i1a import contracts as C
from emx.i1a import hfget

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SHA = "a" * 40
UTC_LINE = "2026-09-29T12:00:00Z approved by Jacob (test)\n"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"_u1_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def exp_i1a(tmp_path, monkeypatch):
    """rp configured for I1a with .env, logs/ and the pod-event log in tmp_path; restored after."""
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS"):  # undone after the E1 reconfigure
        monkeypatch.setattr(rp, name, getattr(rp, name))
    rp.configure("I1a")
    yield tmp_path
    rp.configure("E1")


def approve(logs: Path, step: str, text: str = UTC_LINE) -> Path:
    p = logs / "approvals" / f"I1a-{step}.approved"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def beat(logs: Path, age_s: float = 0.0, interval: int = 600, pid: int | None = None) -> None:
    pid = os.getpid() if pid is None else pid  # this test process: alive
    (logs / "watchdog_I1a.heartbeat").write_text(f"{time.time() - age_s} {interval} {pid}\n")


class FakeSpend(types.ModuleType):
    """Like tests/test_pod_scripts.py's fake, but check_total records the exp it was given."""

    def __init__(self, gpu=0.0, judge=0.0):
        super().__init__("emx.spend")
        self.totals = {"gpu": gpu, "judge": judge}
        self.appended: list[dict] = []
        self.checks: list[dict] = []
        self.spent_exps: list[str] = []

    def spent(self, kind=None, exp="E1"):
        self.spent_exps.append(exp)
        return self.totals.get(kind, 0.0) if kind else sum(self.totals.values())

    def check_total(self, projected_usd, kind, running_pod_usd=0.0, **kw):
        self.checks.append({"projected": projected_usd, "kind": kind, **kw})

    def record(self, **rec):
        self.appended.append(rec)


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


class FakeClient:
    def __init__(self, pods=(), create_results=()):
        self.pods = list(pods)
        self.create_results = list(create_results)
        self.created: list[dict] = []
        self.terminated: list[str] = []
        self.dry_run = False

    def create_pod(self, body):
        self.created.append(body)
        r = self.create_results.pop(0)
        if callable(r):
            r = r(body)
        if isinstance(r, Exception):
            raise r
        return r

    def get_pod(self, pod_id):
        for p in self.pods:
            if p["id"] == pod_id:
                return {**p, "publicIp": "1.2.3.4", "portMappings": {"22": 40022}}
        return {"publicIp": "1.2.3.4", "portMappings": {"22": 40022}}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)

    def list_pods(self):
        return list(self.pods)


def created_event(pod_id, slot="_A", rate=2.0):
    rp.log_pod_event(
        {"event": "created", "pod_id": pod_id, "slot": slot, "gpu": "g", "rate_usd_h": rate}
    )


# --- emx.runpod per experiment ----------------------------------------------------------------


def test_default_is_e1():
    assert rp.EXP == "E1" and rp.POD_NAME_PREFIX == "e1-"
    assert rp.POD_EVENTS.name == "pods_E1.jsonl"
    assert rp.gpu_kill_limit(0.0) == 53.0 and rp.gpu_kill_limit(15.0) == 50.0
    assert rp.gpu_ceiling() is None


def test_configure_i1a_names_events_and_caps(exp_i1a):
    assert rp.EXP == "I1a" and rp.POD_NAME_PREFIX == "i1a-"
    assert rp.POD_EVENTS == exp_i1a / "pods_I1a.jsonl"
    assert rp.pending_path() == exp_i1a / "pods_I1a.pending.jsonl"
    b = rp.pod_body(rp.GPU_FALLBACK[0], "_A", "ssh-ed25519 AAAA")
    assert b["name"] == "i1a-a"
    assert set(b["env"]) == {"PUBLIC_KEY", "HF_HOME", "WANDB_MODE", "TOKENIZERS_PARALLELISM"}
    rec = rp.log_pod_event({"event": "created", "pod_id": "p", "rate_usd_h": 2.0})
    assert rec["exp"] == "I1a" and rp.read_pod_events()[0]["exp"] == "I1a"
    # I1a kill line: min($40 GPU, $85 - $2 reserve - judges)
    assert rp.gpu_kill_limit(0.0) == 40.0
    assert rp.gpu_kill_limit(50.0) == pytest.approx(33.0)
    assert rp.watchdog_decision(30.0, 50.0, 3.0) == (True, 33.0, 33.0)
    assert rp.watchdog_decision(30.0, 50.0, 2.99)[0] is False


def test_configure_ignores_the_environment(monkeypatch):
    with pytest.raises(KeyError):
        rp.configure("E9")
    assert rp.EXP == "E1"  # a refused configure changes nothing
    monkeypatch.setenv("EMX_EXP", "I1a")
    assert rp.configure() == "E1" and rp.POD_NAME_PREFIX == "e1-"


def test_ledger_calls_carry_the_experiment(spend, exp_i1a):
    rp.ledger_check(1.0, 2.0)
    assert spend.checks[-1] == {"projected": 1.0, "kind": "gpu", "exp": "I1a"}
    rp.ledger_spent("gpu")
    assert spend.spent_exps[-1] == "I1a"
    created_event("p")
    rp.finish_session("p", "test")
    assert spend.appended[0]["exp"] == "I1a"


def test_e1_ledger_check_keeps_the_old_call(spend):
    rp.ledger_check(1.0, 2.0)
    assert spend.checks[-1] == {"projected": 1.0, "kind": "gpu"}  # spend's default exp = E1


# --- non-idempotent create ----------------------------------------------------------------------


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.text = self.content.decode()
        self._payload = payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.parametrize(
    "first,exc",
    [
        (Resp(502, {"e": 1}), rp.CreateUncertain),
        (requests.ConnectionError("lost"), rp.CreateUncertain),
        (requests.Timeout("slow"), rp.CreateUncertain),
        (Resp(429, {"e": 1}), rp.RunPodError),
        (Resp(400, {"e": "no stock"}), rp.RunPodError),
    ],
)
def test_create_is_sent_once(first, exc):
    s = Session([first, Resp(200, {"id": "second"})])
    c = rp.RunPod("k", session=s, backoff_s=0)
    with pytest.raises(exc) as e:
        c.create_pod({"name": "i1a-a"})
    assert s.calls == 1
    if exc is rp.RunPodError:
        assert not isinstance(e.value, rp.CreateUncertain)


def test_list_is_still_retried():
    s = Session([Resp(502, {}), Resp(200, [])])
    assert rp.RunPod("k", session=s, backoff_s=0).list_pods() == [] and s.calls == 2


def test_untracked_pods_and_grace(exp_i1a):
    created_event("known")
    t = datetime.now(UTC)
    pending = [{"utc": (t - timedelta(seconds=30)).isoformat(), "name": "i1a-b"}]
    pods = [
        {"id": "known", "name": "i1a-a"},
        {"id": "new", "name": "i1a-b"},
        {"id": "stray", "name": "i1a-c"},
        {"id": "e1", "name": "e1-a"},
    ]
    ev = rp.read_pod_events()
    assert [p["id"] for p in rp.untracked_pods(pods, ev, pending)] == ["stray"]
    assert [p["id"] for p in rp.untracked_pods(pods, ev, pending, grace_s=0)] == ["new", "stray"]
    old = [{"utc": (t - timedelta(seconds=600)).isoformat(), "name": "i1a-b"}]
    assert [p["id"] for p in rp.untracked_pods(pods, ev, old)] == ["new", "stray"]


def test_approvals_ceiling_and_heartbeat(exp_i1a):
    assert not rp.approved("P1") and rp.gpu_ceiling() == C.POD_SMOKE_CAP_USD
    approve(exp_i1a, "P1", "")
    assert not rp.approved("P1")  # empty
    approve(exp_i1a, "P1", "yes, go\n")
    assert not rp.approved("P1")  # no UTC line
    approve(exp_i1a, "P1")
    assert rp.approved("P1") and rp.gpu_ceiling() is None
    assert rp.watchdog_live()[0] is False
    beat(exp_i1a, age_s=100)
    assert rp.watchdog_live()[0] is True
    beat(exp_i1a, age_s=1300)  # > 2 x 600 s
    assert rp.watchdog_live()[0] is False
    p = subprocess.Popen(["true"])
    p.wait()
    beat(exp_i1a, pid=p.pid)  # fresh, but its writer is gone
    assert rp.watchdog_live()[0] is False
    (exp_i1a / "watchdog_I1a.heartbeat").write_text(f"{time.time()} 600\n")  # old format
    assert rp.watchdog_live()[0] is False


def test_slot_pod_ok(exp_i1a):
    created_event("mine", "_A")
    assert rp.slot_pod_ok("_A", "mine")[0]
    assert not rp.slot_pod_ok("_B", "mine")[0]  # wrong slot
    assert not rp.slot_pod_ok("_A", "e1pod")[0]  # not in the I1a log
    assert not rp.slot_pod_ok("_A", "mine", {"name": "e1-a"})[0]  # API name of another exp
    assert rp.slot_pod_ok("_A", "mine", {"name": "i1a-a"})[0]


def test_runpod_cli_check_slot(exp_i1a, capsys):
    created_event("mine", "_A", rate=2.69)
    rp.set_env_var("RUNPOD_POD_ID_A", "mine")
    rp.set_env_var("RUNPOD_POD_ID_B", "e1pod")
    assert rp.main(["--exp", "I1a", "--check-slot", "_A", "--session-env"]) == 0
    out = capsys.readouterr().out
    assert "EMX_POD_RATE_USD_H=2.69" in out and re.search(r"EMX_POD_CREATED_EPOCH=\d+", out)
    assert rp.main(["--exp", "I1a", "--check-slot", "_B"]) == 1


# --- pod_up under I1a ---------------------------------------------------------------------------


@pytest.fixture
def up_i1a(spend, exp_i1a, monkeypatch):
    up = load("pod_up")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    return up


def use_client(monkeypatch, fake):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)


def test_pod_up_i1a_needs_a_live_watchdog(up_i1a, exp_i1a, monkeypatch):
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up_i1a.main(["--exp", "I1a", "--slot", "_A"])
    assert e.value.code == up_i1a.UNTRACKED and fake.created == []


def test_pod_up_refuses_with_an_untracked_pod(up_i1a, exp_i1a, monkeypatch):
    beat(exp_i1a)
    fake = FakeClient(pods=[{"id": "ghost", "name": "i1a-z"}], create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up_i1a.main(["--exp", "I1a", "--slot", "_A"])
    assert e.value.code == up_i1a.UNTRACKED and fake.created == []


def test_pod_up_p0_ceiling_until_p1_approved(up_i1a, exp_i1a, spend, monkeypatch):
    beat(exp_i1a)
    spend.totals = {"gpu": 4.0, "judge": 0.0}  # 4 + 1 h x 2.69 > 6
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up_i1a.main(["--exp", "I1a", "--slot", "_A", "--no-fallback"])
    assert e.value.code == up_i1a.GATE_REFUSED and fake.created == []
    approve(exp_i1a, "P1")  # after checkpoint 2 only the $40 line applies
    fake = FakeClient(create_results=[{"id": "p1", "costPerHr": 2.69}])
    use_client(monkeypatch, fake)
    assert up_i1a.main(["--exp", "I1a", "--slot", "_A", "--no-fallback"]) == 0
    assert fake.created[0]["name"] == "i1a-a"


def test_pod_up_happy_path_logs_pending_then_created(up_i1a, exp_i1a, monkeypatch):
    beat(exp_i1a)
    fake = FakeClient(create_results=[{"id": "p1", "costPerHr": 2.69}])
    use_client(monkeypatch, fake)
    assert up_i1a.main(["--exp", "I1a", "--slot", "_A", "--no-fallback"]) == 0
    assert [e["event"] for e in rp.read_pod_events(rp.pending_path())] == ["create_pending"]
    assert rp.read_pod_events()[0]["event"] == "created"
    assert rp.env("RUNPOD_POD_ID_A") == "p1"


def test_pod_up_uncertain_create_reaps_and_never_retries(up_i1a, exp_i1a, spend, monkeypatch):
    beat(exp_i1a)

    def lost_response(body):  # the pod exists at RunPod, but the response never arrived
        fake.pods.append({"id": "orphan", "name": body["name"], "costPerHr": 2.69})
        return rp.CreateUncertain("POST /pods outcome unknown: ReadTimeout")

    fake = FakeClient(create_results=[lost_response, {"id": "never"}])
    use_client(monkeypatch, fake)
    assert up_i1a.main(["--exp", "I1a", "--slot", "_A"]) == 1
    assert len(fake.created) == 1  # no retry, no next GPU option
    assert fake.terminated == ["orphan"]
    assert [r["exp"] for r in spend.appended] == ["I1a"]  # its dollars reach the ledger
    assert rp.env("RUNPOD_POD_ID_A") == ""


def test_pod_up_e1_needs_no_heartbeat(spend, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=k\n")
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setattr(rp, "POD_EVENTS", tmp_path / "pods_E1.jsonl")
    up = load("pod_up")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    fake = FakeClient(create_results=[{"id": "e"}])
    use_client(monkeypatch, fake)
    assert up.main(["--slot", "_A", "--no-fallback"]) == 0
    assert fake.created[0]["name"] == "e1-a"


# --- pod_down under I1a -------------------------------------------------------------------------


def test_pod_down_all_exp_only_touches_i1a_pods(spend, exp_i1a, monkeypatch):
    down = load("pod_down")
    created_event("mine")
    pods = [
        {"id": "mine", "name": "i1a-a"},
        {"id": "ghost", "name": "i1a-q"},  # untracked: costed first, then terminated
        {"id": "e1pod", "name": "e1-a"},
        {"id": "mira", "name": "mira-x"},
    ]
    fake = FakeClient(pods)
    use_client(monkeypatch, fake)
    assert down.main(["--exp", "I1a", "--all-exp"]) == 0
    assert sorted(fake.terminated) == ["ghost", "mine"]
    assert sorted(r["resource"].split(":")[-1] for r in spend.appended) == ["ghost", "mine"]
    assert {r["exp"] for r in spend.appended} == {"I1a"}


def test_pod_down_all_e1_is_always_e1():
    down = load("pod_down")
    with pytest.raises(SystemExit) as e:
        down.main(["--exp", "I1a", "--all-e1", "--dry-run"])
    assert e.value.code == 2


def test_pod_down_untracked_spares_a_pod_being_created(spend, exp_i1a, monkeypatch):
    down = load("pod_down")
    rp.log_create_pending("i1a-b", "_B")
    fake = FakeClient([{"id": "creating", "name": "i1a-b"}, {"id": "stray", "name": "i1a-c"}])
    use_client(monkeypatch, fake)
    assert down.main(["--exp", "I1a", "--untracked"]) == 0
    assert fake.terminated == ["stray"]


def test_pod_down_slot_refuses_another_experiments_pod(spend, exp_i1a, monkeypatch):
    down = load("pod_down")
    rp.set_env_var("RUNPOD_POD_ID_A", "e1pod")  # the slot holds an E1 pod
    fake = FakeClient([{"id": "e1pod", "name": "e1-a"}])
    use_client(monkeypatch, fake)
    assert down.main(["--exp", "I1a", "--slot", "_A"]) == 1
    assert fake.terminated == []
    created_event("e1pod")  # even if the log says so, a wrong API name refuses
    assert down.main(["--exp", "I1a", "--slot", "_A"]) == 1 and fake.terminated == []


# --- pod_budget_check under I1a -----------------------------------------------------------------


def test_budget_check_exp_i1a_status(spend, exp_i1a, capsys):
    bc = load("pod_budget_check")
    approve(exp_i1a, "P1")  # past checkpoint 2: the $40 line
    created_event("p", rate=3.0)
    spend.totals = {"gpu": 10.0, "judge": 20.0}
    assert bc.main(["--exp", "I1a", "--reserve-hours", "1"]) == 0
    d = json.loads(capsys.readouterr().out.strip())
    assert d["exp"] == "I1a" and d["gpu_kill_limit_usd"] == 40.0 and d["hard_cap_usd"] == 85.0
    assert d["gpu_ceiling_usd"] is None
    # dynamic judge limit: $83 minus the projected GPU ($10 + 3 $/h x 1 h reserve)
    assert d["judge_limit_usd"] == pytest.approx(83.0 - d["projected_gpu_usd"])
    assert spend.checks[-1]["exp"] == "I1a"
    spend.totals = {"gpu": 37.5, "judge": 20.0}  # 37.5 + 3 $/h x 1 h reserve >= 40: hard stop
    assert bc.main(["--exp", "I1a", "--reserve-hours", "1"]) == 2


def test_budget_check_p0_ceiling(spend, exp_i1a, capsys):
    bc = load("pod_budget_check")
    spend.totals = {"gpu": 5.9, "judge": 0.0}
    assert bc.main(["--exp", "I1a"]) == 0  # automatic $6 line without the P1 marker
    assert json.loads(capsys.readouterr().out)["gpu_ceiling_usd"] == C.POD_SMOKE_CAP_USD
    spend.totals = {"gpu": 6.0, "judge": 0.0}
    assert bc.main(["--exp", "I1a"]) == 2
    capsys.readouterr()
    approve(exp_i1a, "P1")
    assert bc.main(["--exp", "I1a"]) == 0
    capsys.readouterr()
    assert bc.main(["--exp", "I1a", "--gpu-ceiling", "5"]) == 2  # explicit flag still applies


def test_budget_check_apply_cuts_is_e1_only(exp_i1a):
    bc = load("pod_budget_check")
    with pytest.raises(SystemExit) as e:
        bc.main(["--exp", "I1a", "--apply-cuts", "--projected-gpu", "1", "--projected-judge", "1"])
    assert e.value.code == 2


# --- real CLIs in dry-run, for both experiments (no network: dry runs never send) ---------------


def run_py(args, tmp_path, env_exp="I1a"):
    """Runs a script with EMX_EXP exported (to prove it is ignored) and a sandboxed ledger."""
    env = {**os.environ, "EMX_EXP": env_exp, "EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl")}
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def dry_requests(stdout: str) -> list[dict]:
    return [
        json.loads(p.split("\nbudget gate")[0]) for p in stdout.split("DRY RUN (not sent):\n")[1:]
    ]


@pytest.mark.parametrize("exp,prefix,env_exp", [("E1", "e1-", "I1a"), ("I1a", "i1a-", "E1")])
def test_pod_up_dry_run_both_exps(exp, prefix, env_exp, tmp_path):
    args = ["scripts/pod_up.py", "--slot", "_Z", "--dry-run", "--reserve-hours", "0"]
    if exp != "E1":
        args += ["--exp", exp]
    r = run_py(args, tmp_path, env_exp)
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert len(reqs) == len(rp.GPU_FALLBACK)
    assert {q["json"]["name"] for q in reqs} == {f"{prefix}z"}
    assert f"budget gate ({exp})" in r.stdout


@pytest.mark.parametrize(
    "args,prefix",
    [
        (["--all-e1"], "e1-"),
        (["--all-exp"], "e1-"),
        (["--exp", "I1a", "--all-exp"], "i1a-"),
    ],
)
def test_pod_down_dry_run(args, prefix, tmp_path):
    r = run_py(["scripts/pod_down.py", *args, "--dry-run"], tmp_path)  # EMX_EXP=I1a exported
    assert r.returncode == 0, r.stderr
    assert '"method": "GET"' in r.stdout and '"method": "DELETE"' in r.stdout
    assert f"<each {prefix}* pod id>" in r.stdout


@pytest.mark.parametrize("exp,kill", [("E1", 53.0), ("I1a", 40.0)])
def test_pod_budget_check_cli_both_exps(exp, kill, tmp_path):
    r = run_py(["scripts/pod_budget_check.py", "--exp", exp], tmp_path, "E1")
    assert r.returncode in (0, 2), r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["exp"] == exp and d["gpu_spent_usd"] == 0 and d["judge_spent_usd"] == 0
    if not d["running"]:
        assert d["gpu_kill_limit_usd"] == kill


def watchdog_commands() -> list[list[str]]:
    """The python argv of each uv call in the unchanged E1 scripts/pod_watchdog.sh."""
    text = (SCRIPTS / "pod_watchdog.sh").read_text().replace("\\\n", " ")
    cmds = []
    for ln in text.splitlines():
        m = re.search(r"uv run python (scripts/pod_(?:budget_check|down)\.py[^|;)]*)", ln)
        if m and not ln.lstrip().startswith("#"):
            argv = shlex.split(m.group(1).replace('"$HOURS"', "0.1"))
            cmds.append([a for a in argv if not re.match(r"\d?>", a)])  # drop redirections
    return cmds


def test_e1_watchdog_commands_police_e1_with_emx_exp_i1a_exported(tmp_path):
    cmds = watchdog_commands()
    assert [c[0] for c in cmds] == ["scripts/pod_budget_check.py", "scripts/pod_down.py"]
    assert not any("--exp" in c for c in cmds)
    budget, down = cmds
    r = run_py(budget, tmp_path, env_exp="I1a")
    assert r.returncode in (0, 2), r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1])["exp"] == "E1"
    r = run_py([*down, "--dry-run"], tmp_path, env_exp="I1a")
    assert r.returncode == 0, r.stderr
    assert "<each e1-* pod id>" in r.stdout and "i1a-" not in r.stdout


# --- shell scripts ------------------------------------------------------------------------------

NEW_OR_EDITED_SH = [
    "pod_push.sh",
    "pod_setup.sh",
    "i1a_watchdog.sh",
    "pod_run_i1a.sh",
    "pod_pull_results.sh",
    "pod_hunt.sh",
]
BASH4 = ("declare -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&")


@pytest.mark.parametrize("name", NEW_OR_EDITED_SH)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in BASH4:
        assert bad not in code, (name, bad)


def copy_script(tmp_path: Path, name: str) -> Path:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    dst = tmp_path / "scripts" / name
    shutil.copy(SCRIPTS / name, dst)
    return dst


def run_bash(script: Path, *args, env_extra=None, cwd=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=cwd or script.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# pod_push.sh --dry-run: a copy in tmp_path with a fake checkout, a fake .env and a FAKE git
# (POD_PUSH_GIT) that answers from tmp_path/.fakegit/{tracked,untracked,deleted}; contacts nothing.

LAPTOP = (
    "OPENAI_API_KEY=sk-openai-secret\nOPENROUTER_API_KEY=sk-or-secret\n"
    "RUNPOD_API_KEY=rp-secret\nEM_DATASET_PASSWORD=pw-secret\n"
)
HEAD = "0123456789abcdef0123456789abcdef01234567"
FAKE_GIT = r"""
D="$FAKEGIT_DIR"
roots=""; seen=0
for a in "$@"; do
  if [ "$seen" = "1" ]; then roots="$roots $a"; fi
  if [ "$a" = "--" ]; then seen=1; fi
done
inroots() {
  [ -z "$roots" ] && return 0
  for r in $roots; do case "$1" in "$r"|"$r"/*) return 0 ;; esac; done
  return 1
}
emit() {  # emit FILE PREFIX SEP
  [ -f "$D/$1" ] || return 0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if inroots "$f"; then printf "%s%s$3" "$2" "$f"; fi
  done < "$D/$1"
}
case "$1" in
  rev-parse)
    [ -f "$D/not_a_repo" ] && exit 128
    if [ "$2" = "--is-inside-work-tree" ]; then echo true; exit 0; fi
    [ -f "$D/no_head" ] && exit 128
    echo "$(cat "$D/head")" ;;
  status)
    case " $* " in
      *" --untracked-files=no "*) emit modified ' M ' '\n' ;;
      *) emit modified ' M ' '\n'; emit untracked '?? ' '\n' ;;
    esac ;;
  ls-files)
    case " $* " in
      *" --deleted "*) emit deleted '' '\n' ;;
      *) emit tracked '' '\0' ;;
    esac ;;
  *) echo "fake git: unsupported $*" >&2; exit 2 ;;
esac
"""

TRACKED = [
    "pyproject.toml",
    "uv.lock",
    "src/emx/__init__.py",
    "src/emx/i1a/token_readers.py",
    "scripts/pod_run_i1a.sh",
    "third_party/workspace-bench/pyproject.toml",
    "third_party/workspace-bench/src/wsbench/multitoken/regex.py",
    "third_party/workspace-bench/examples/readouts/hallucination.tokens.jsonl",
    "data/I1a/subsets/hallucination_75.json",
    "data/E1/manifest.json",
    "configs/E1/a.json",
    "docs/private-notes.md",  # tracked, but outside every root
]


@pytest.fixture
def checkout(tmp_path):
    s = copy_script(tmp_path, "pod_push.sh")
    for f in TRACKED:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x\n")
    # present on disk but ignored by git (neither tracked nor untracked): never sent
    (tmp_path / "src" / "emx" / ".env.local").write_text("OPENAI_API_KEY=sk\n")
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "bad.jsonl").write_text("raw\n")
    fg = tmp_path / ".fakegit"
    fg.mkdir()
    (fg / "tracked").write_text("\n".join(TRACKED) + "\n")
    (fg / "head").write_text(HEAD + "\n")
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\n" + LAPTOP)
    fake_bin(tmp_path, "fakegit", FAKE_GIT)
    return s


def push(checkout, *args, env=None):
    tmp = checkout.parent.parent
    extra = {
        "POD_PUSH_GIT": str(tmp / "bin" / "fakegit"),
        "FAKEGIT_DIR": str(tmp / ".fakegit"),
        **(env or {}),
    }
    return run_bash(checkout, *args, "--dry-run", "--show-files", env_extra=extra)


def push_line(stdout: str, tag: str) -> list[str]:
    line = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return line.split(f"{tag}:", 1)[1].split()


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def test_pod_push_i1a_sends_only_tracked_files_and_markers(checkout, tmp_path):
    approve(tmp_path / "logs", "P0")
    r = push(checkout, "--exp", "I1a", "--slot", "_A", env={"EMX_EXP": "E1"})
    assert r.returncode == 0, r.stderr
    keys = push_line(r.stdout, "pod env keys")
    assert keys[0] == "HF_TOKEN" and "HF_USERNAME" not in keys
    for k in ("HF_HUB_DISABLE_XET", "EMX_EXP", "HF_HOME", "WANDB_MODE", "EMX_GIT_COMMIT"):
        assert k in keys
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "secret" not in r.stdout and "hf_abc" not in r.stdout
    assert set(push_line(r.stdout, "transfer set")) == {
        "src",
        "scripts",
        "third_party",
        "pyproject.toml",
        "uv.lock",
        "data/I1a",
    }
    files = sent(r.stdout)
    assert files == {f for f in TRACKED if not f.startswith(("docs/", "data/E1/", "configs/"))} | {
        "logs/approvals/I1a-P0.approved"
    }
    assert "src/emx/.env.local" not in files  # ignored files are never sent
    assert "--check-slot _A" in r.stdout and "pod_setup.sh --only i1a" in r.stdout


def test_pod_push_e1_tracked_plus_its_gitignored_inputs(checkout, tmp_path):
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\nHF_USERNAME=jacob\n" + LAPTOP)
    (tmp_path / "data" / "E1" / "questions").mkdir(parents=True)
    (tmp_path / "data" / "E1" / "train_bad.jsonl").write_text("x\n")  # gitignored E1 input
    (tmp_path / "data" / "E1" / "questions" / "narrow.jsonl").write_text("x\n")
    r = push(checkout, "--slot", "_A", env={"EMX_EXP": "I1a"})  # the exported env is ignored
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E1)" in r.stdout and "HF_HUB_DISABLE_XET" not in r.stdout
    files = sent(r.stdout)
    assert {
        "configs/E1/a.json",
        "data/E1/manifest.json",
        "data/I1a/subsets/hallucination_75.json",
        "data/E1/train_bad.jsonl",
        "data/E1/questions/narrow.jsonl",
    } <= files
    assert not any(f.startswith(("data/raw/", "docs/")) for f in files)
    assert "--only" not in r.stdout


def test_pod_push_refuses_untracked_files(checkout, tmp_path):
    (tmp_path / ".fakegit" / "untracked").write_text("src/emx/new.py\ndocs/draft.md\n")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "untracked files under the transfer roots" in r.stderr
    assert "src/emx/new.py" in r.stderr and "docs/draft.md" not in r.stderr


def test_pod_push_i1a_refuses_modified_tracked_files_e1_does_not(checkout, tmp_path):
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\ndocs/private-notes.md\n")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "modified tracked files (EMX_GIT_DIRTY=1)" in r.stderr
    assert "src/emx/__init__.py" in r.stderr and "docs/private-notes.md" in r.stderr
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\nHF_USERNAME=jacob\n" + LAPTOP)
    r = push(checkout, "--slot", "_A")  # E1: unchanged, a dirty push only records the flag
    assert r.returncode == 0, r.stderr
    assert "EMX_GIT_DIRTY" in push_line(r.stdout, "pod env keys")


def test_pod_push_refuses_deleted_tracked_files(checkout, tmp_path):
    (tmp_path / ".fakegit" / "deleted").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "missing from the working tree" in r.stderr


@pytest.mark.parametrize("state", ["not_a_repo", "no_head"])
def test_pod_push_refuses_without_git_or_head(checkout, tmp_path, state):
    (tmp_path / ".fakegit" / state).write_text("")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "refusing" in r.stderr


@pytest.mark.parametrize(
    "secret",
    [
        "src/.env.backup",
        "scripts/id_rsa",
        "third_party/workspace-bench/credentials.json",
        "data/I1a/server.pem",
        "src/emx/api.key",
        "src/.netrc",
        "scripts/.pypirc",
        "third_party/cert.p12",
        "src/hf_token",
        "data/I1a/token.txt",
        "data/I1a/secret.json",  # gpt-6-sol integrated r3
        "data/I1a/Client_Secret.JSON",
        "src/API_KEY.txt",
        "src/emx/apikey.cfg",
        "scripts/Credential.yaml",
        "data/I1a/TOKEN.TXT",
        "src/emx/ID_RSA",
    ],
)
def test_pod_push_refuses_secret_names_even_if_tracked(checkout, tmp_path, secret):
    (tmp_path / secret).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / secret).write_text("secret\n")
    with open(tmp_path / ".fakegit" / "tracked", "a") as f:
        f.write(secret + "\n")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "secret-looking files" in r.stderr and secret in r.stderr


def track(tmp_path: Path, rel: str, text: str) -> None:
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(text)
    with open(tmp_path / ".fakegit" / "tracked", "a") as f:
        f.write(rel + "\n")


def test_pod_push_token_exemption_is_case_insensitive_and_py_jsonl_only(checkout, tmp_path):
    track(tmp_path, "src/emx/Token_Readers.py", "x\n")
    track(tmp_path, "data/I1a/Multi.Tokens.JSONL", "x\n")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 0, r.stderr


FAKE_SECRETS = {  # fake values only; a hit must never print them
    "OPENAI_API_KEY": "sk-fake-openai-0123456789",
    "OPENROUTER_API_KEY": "sk-or-fake-abcdef987654",
    "RUNPOD_API_KEY": "rp_fake_zyxwvu_0123456",
    "EM_DATASET_PASSWORD": "pw-fake-hunter2-0123456",
    "my_client_secret": "lowercase-fake-secret-value",
}


def secret_env(tmp_path: Path, extra: str = "") -> None:
    lines = [f"{k}={v}" for k, v in FAKE_SECRETS.items()]
    lines[1] = f'export OPENROUTER_API_KEY="{FAKE_SECRETS["OPENROUTER_API_KEY"]}"'  # quoted
    (tmp_path / ".env").write_text("HF_TOKEN=hf_fake_tok_42_0123456789\n" + "\n".join(lines) + "\n" + extra)


@pytest.mark.parametrize("key", list(FAKE_SECRETS))
def test_pod_push_content_scan_refuses_a_secret_value_in_any_file(checkout, tmp_path, key):
    secret_env(tmp_path)
    track(tmp_path, "data/I1a/notes.json", f'{{"note": "copied {FAKE_SECRETS[key]} by mistake"}}\n')
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"data/I1a/notes.json: {key}" in r.stderr
    for v in (*FAKE_SECRETS.values(), "hf_fake_tok_42_0123456789"):
        assert v not in r.stdout and v not in r.stderr  # values are never echoed


def test_pod_push_content_scan_hf_token_only_exempt_in_the_pod_env(checkout, tmp_path):
    secret_env(tmp_path, "HF_USERNAME=jacob\n")
    track(tmp_path, "src/emx/readme.txt", "user jacob\n")  # HF_USERNAME is not a secret key
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 0, r.stderr  # HF_TOKEN rides in the pod env, as intended
    track(tmp_path, "scripts/leak.sh", "export HF_TOKEN=hf_fake_tok_42_0123456789\n")
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "scripts/leak.sh: HF_TOKEN" in r.stderr
    assert "hf_fake_tok_42_0123456789" not in r.stderr


def test_pod_push_content_scan_covers_the_pod_env_except_hf_token(checkout, tmp_path):
    # HF_TOKEN's value equals another secret key's value, so the pod env carries a non-HF secret
    (tmp_path / ".env").write_text(
        "HF_TOKEN=shared-fake-value-77\nOTHER_TOKEN=shared-fake-value-77\n"
    )
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode == 1 and "(pod env file): OTHER_TOKEN" in r.stderr
    assert "shared-fake-value-77" not in r.stderr and "shared-fake-value-77" not in r.stdout


def test_pod_push_values_file_is_private_and_removed(checkout, tmp_path):
    text = (SCRIPTS / "pod_push.sh").read_text()
    assert 'chmod 600 "$VALS"' in text and '"$VALS"\' EXIT' in text
    secret_env(tmp_path)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    r = push(checkout, "--exp", "I1a", "--slot", "_A", env={"TMPDIR": str(tmpdir)})
    assert r.returncode == 0, r.stderr
    assert list(tmpdir.iterdir()) == []  # pod env, file list and values file all removed


def test_pod_push_refuses_a_leaked_laptop_key(checkout, tmp_path):
    (tmp_path / ".env").write_text("HF_TOKEN=rp-secret\n" + LAPTOP)  # token == RunPod key
    r = push(checkout, "--exp", "I1a", "--slot", "_A")
    assert r.returncode != 0 and "RUNPOD_API_KEY appears in the pod env" in r.stderr


def test_pod_push_e1_still_requires_hf_username(checkout):
    r = push(checkout, "--slot", "_A")
    assert r.returncode != 0 and "HF_USERNAME is empty" in r.stderr


def test_pod_push_refuses_unknown_exp(checkout):
    r = push(checkout, "--exp", "E9", "--slot", "_A")
    assert r.returncode == 2


def test_pod_setup_only_i1a_builds_one_venv():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    assert 'build i1a pod-i1a "wsbench, torch, transformers, peft, safetensors, sklearn"' in text
    i1a_branch = text.split('if [ "$ONLY" = "i1a" ]; then', 1)[1].split("fi\nbuild peft", 1)[0]
    assert "exit 0" in i1a_branch and "build peft" not in i1a_branch
    assert "--no-default-groups --group" in text  # build() is shared with E1


# i1a_watchdog.sh: one tick with a fake `uv` on PATH; every file it writes is in tmp_path


def fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def tick(tmp_path, rc, *, p1=False):
    calls = tmp_path / "uv_calls.txt"
    status = {"exp": "I1a", "running": [], "hard_stop": rc == 2}
    body = (
        f'echo "$*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};; esac\n"
        "exit 0\n"
    )
    bindir = fake_bin(tmp_path, "uv", body)
    approvals = tmp_path / "approvals"
    if p1:
        approve(tmp_path, "P1")
    r = run_bash(
        SCRIPTS / "i1a_watchdog.sh",
        "600",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "EMX_EXP": "E1",  # ignored: the watchdog passes --exp I1a everywhere
            "I1A_WATCHDOG_ONCE": "1",
            "I1A_WATCHDOG_STOP": str(tmp_path / "stop"),
            "I1A_IDLE_ALERT": str(tmp_path / "idle.alert"),
            "I1A_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "I1A_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
            "I1A_APPROVALS": str(approvals),
        },
        cwd=ROOT,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def test_i1a_watchdog_healthy_tick_reconciles_and_beats(tmp_path):
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("run python scripts/pod_budget_check.py --exp I1a")
    assert f"--gpu-ceiling {C.POD_SMOKE_CAP_USD}" in lines[0]  # no P1 approval yet
    assert lines[1].startswith("run python scripts/pod_down.py --exp I1a --untracked")
    t, interval, pid = (tmp_path / "heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def test_i1a_watchdog_drops_the_p0_ceiling_after_p1_approval(tmp_path):
    r, lines = tick(tmp_path, 0, p1=True)
    assert r.returncode == 0 and "--gpu-ceiling" not in lines[0]


def test_i1a_watchdog_hard_stop(tmp_path):
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2 and "HARD STOP" in r.stdout
    assert lines[1].startswith("run python scripts/pod_down.py --exp I1a --all-exp")


@pytest.mark.parametrize("rc", [1, 3, 127])
def test_i1a_watchdog_fails_closed(tmp_path, rc):
    r, lines = tick(tmp_path, rc)
    assert r.returncode == rc
    assert lines[1].startswith("run python scripts/pod_down.py --exp I1a --all-exp")
    assert "BUDGET CHECK FAILED" in (tmp_path / "watchdog.alert").read_text()


def test_i1a_watchdog_stop_removes_heartbeat(tmp_path):
    (tmp_path / "heartbeat").write_text(f"{time.time()} 600\n")
    bindir = fake_bin(tmp_path, "uv", "exit 0\n")
    stop = tmp_path / "stop"
    # the watchdog deletes a stale stop file at start, so create it from the fake uv on tick 1
    fake_bin(tmp_path, "uv", f"touch {stop}; echo '{{\"running\": []}}'; exit 0\n")
    r = run_bash(
        SCRIPTS / "i1a_watchdog.sh",
        "1",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "I1A_WATCHDOG_STOP": str(stop),
            "I1A_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
            "I1A_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "I1A_IDLE_ALERT": str(tmp_path / "idle.alert"),
            "I1A_APPROVALS": str(tmp_path / "approvals"),
        },
        cwd=ROOT,
    )
    assert r.returncode == 0 and "stop file present" in r.stdout
    assert not (tmp_path / "heartbeat").exists()


# pod_run_i1a.sh (a copy in tmp_path; src/ symlinked for the contracts import)

P1_STEPS = [
    "capture",
    "tokens",
    "olens-gate",
    "olens-extra",
    "shuffled",
    "reserve",
    "fve",
    "probe",
    "manifest",
]
STEPS = ["download", "smoke", *P1_STEPS]


@pytest.fixture
def chain(tmp_path):
    s = copy_script(tmp_path, "pod_run_i1a.sh")
    (tmp_path / "src").symlink_to(ROOT / "src")
    (tmp_path / "results" / "I1a").mkdir(parents=True)
    (tmp_path / "data" / "I1a").mkdir(parents=True)
    (tmp_path / "data" / "I1a" / "input.txt").write_text("plan v1\n")
    return s


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_pair(
    root: Path,
    step: str,
    *,
    required=None,
    listed=None,
    sidecar_step=None,
    with_sidecar=True,
    limited=None,
    commit="abc123",
) -> None:
    """A manifest pair as i1a_pod.py will write it (or a broken variant)."""
    res = root / "results" / "I1a"
    out = res / f"{step}.out"
    out.write_text(step)
    listed = listed or [f"results/I1a/{step}.out"]
    lines = [f"{sha256(root / p)}  {p}" for p in listed]
    (res / f"MANIFEST.{step}.sha256").write_text("\n".join(lines) + "\n")
    if with_sidecar:
        inp = root / "data" / "I1a" / "input.txt"
        side = {
            "step": sidecar_step or step,
            "required_outputs": required or [f"results/I1a/{step}.out"],
            "counts": {"rows": 1},
            "code_commit": commit,
            "inputs_sha256": {"data/I1a/input.txt": sha256(inp)},
        }
        if limited is not None:
            side["limited"] = limited
        (res / f"MANIFEST.{step}.json").write_text(json.dumps(side))


P0_ENV = {  # what pod_push --exp I1a writes into the pod .env (the commit the fakes record)
    "EMX_POD_RATE_USD_H": "2.69",
    "EMX_POD_CREATED_EPOCH": str(int(time.time())),
    "EMX_GIT_COMMIT": "abc123",
}


def dry(chain, *args, extra=None):
    env = {"I1A_DRY_RUN": "1", "I1A_PY": "/ws/venv-i1a/bin/python", **P0_ENV, **(extra or {})}
    return run_bash(chain, *args, env_extra=env)


def planned(stdout: str) -> list[str]:
    return [
        ln.split("i1a_pod.py ")[1].split()[0] for ln in stdout.splitlines() if ln.startswith("+ ")
    ]


@pytest.mark.parametrize("step", P1_STEPS)
def test_p1_steps_refused_without_approval_or_smoke(chain, tmp_path, step):
    r = dry(chain, step)
    assert r.returncode == 3 and "I1a-P1.approved missing" in r.stdout and planned(r.stdout) == []
    approve(tmp_path / "logs", "P1")
    r = dry(chain, step)
    assert r.returncode == 3 and "smoke step is not complete" in r.stdout
    write_pair(tmp_path, "smoke")
    r = dry(chain, step)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"+ /ws/venv-i1a/bin/python scripts/i1a_pod.py {step} >>logs/pod_i1a_{step}.log" in (
        r.stdout
    )


@pytest.mark.parametrize("step", ["download", "smoke"])
def test_p0_steps_need_p0_approval_and_fit_the_cap(chain, tmp_path, step):
    r = dry(chain, step)
    assert r.returncode == 3 and "I1a-P0.approved missing" in r.stdout
    approve(tmp_path / "logs", "P0")
    r = dry(chain, step)
    assert r.returncode == 0 and planned(r.stdout) == [step]
    assert "P0 cost check" in r.stderr and "<= P0 cap" in r.stderr
    old = {"EMX_POD_CREATED_EPOCH": str(int(time.time()) - 3 * 3600)}  # 3 h + 1 h at $2.69 > $6
    r = run_bash(chain, step, env_extra={"I1A_PY": shutil.which("true"), **P0_ENV, **old})
    assert r.returncode == 3 and "P0 cost check failed" in r.stdout


def test_p0_refused_without_pod_rate(chain, tmp_path):
    approve(tmp_path / "logs", "P0")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EMX_POD_")}
    r = subprocess.run(
        ["/bin/bash", str(chain), "download"],
        cwd=tmp_path,
        env={**env, "I1A_PY": shutil.which("true")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 3 and "EMX_POD_RATE_USD_H" in r.stderr


def test_marker_needs_a_utc_line(chain, tmp_path):
    approve(tmp_path / "logs", "P0", "approved\n")
    assert dry(chain, "download").returncode == 3


def test_pod_run_i1a_rejects_unknown_step(chain):
    r = dry(chain, "everything")
    assert r.returncode == 2 and "unknown step" in r.stderr


def test_pod_run_i1a_detach_dry_run(chain):
    r = dry(chain, "all", "--detach")
    assert r.returncode == 0, r.stderr
    assert "nohup bash scripts/pod_run_i1a.sh all >>logs/pod_i1a_run.log" in r.stdout
    assert "& disown" in r.stdout


def driver(tmp_path: Path) -> Path:
    """A fake interpreter for scripts/i1a_pod.py. DRIVER_MODE: full (a valid manifest pair),
    onefile (the sha list covers one arbitrary file; the sidecar requires another output),
    nosidecar, wrongstep, limited ("limited": true)."""
    body = (
        'step="$2"; echo "driver ran $step EMX_EXP=$EMX_EXP XET=$HF_HUB_DISABLE_XET"\n'
        'echo "$step" >> "$DRIVER_CALLS"\n'
        'R=results/I1a; echo "$step" > "$R/$step.out"\n'
        'shasum -a 256 "$R/$step.out" > "$R/MANIFEST.$step.sha256"\n'
        'req="$R/$step.out"; sstep="$step"\n'
        'case "${DRIVER_MODE:-full}" in\n'
        "  nosidecar) exit 0 ;;\n"
        '  onefile) req="$R/$step.required.jsonl" ;;\n'
        '  wrongstep) sstep="other" ;;\n'
        "esac\n"
        'lim=false; [ "${DRIVER_MODE:-}" = limited ] && lim=true\n'
        'inp=$(shasum -a 256 data/I1a/input.txt | cut -d" " -f1)\n'
        'printf \'{"step": "%s", "required_outputs": ["%s"], "counts": {"rows": 1}, '
        '"code_commit": "abc123", "inputs_sha256": {"data/I1a/input.txt": "%s"}, '
        '"limited": %s}\' "$sstep" "$req" "$inp" "$lim" > "$R/MANIFEST.$step.json"\n'
    )
    return fake_bin(tmp_path, "fakepy", body) / "fakepy"


def real_env(tmp_path, **kw):
    return {
        "I1A_PY": str(driver(tmp_path)),
        "DRIVER_CALLS": str(tmp_path / "calls"),
        **P0_ENV,
        **kw,
    }


def calls(tmp_path) -> list[str]:
    p = tmp_path / "calls"
    return p.read_text().split() if p.exists() else []


def test_chain_runs_gated_verifies_and_is_idempotent(chain, tmp_path):
    root = tmp_path
    env = real_env(tmp_path)
    assert run_bash(chain, "download", env_extra=env).returncode == 3  # no P0 approval
    approve(root / "logs", "P0")
    assert run_bash(chain, "smoke", env_extra=env).returncode == 0
    r = run_bash(chain, "all", env_extra=env)
    assert r.returncode == 3 and "I1a-P1.approved missing" in r.stdout  # checkpoint 2
    assert calls(tmp_path) == ["smoke", "download"]
    approve(root / "logs", "P1")
    r = run_bash(chain, "all", env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls(tmp_path) == ["smoke", "download", *P1_STEPS]  # all never runs smoke
    log = (root / "logs" / "pod_i1a_fve.log").read_text()
    assert "driver ran fve EMX_EXP=I1a XET=1" in log and "done fve" in log
    r = run_bash(chain, "all", env_extra=env)  # idempotent: nothing re-runs
    assert r.returncode == 0 and calls(tmp_path) == ["smoke", "download", *P1_STEPS]
    (root / "data" / "I1a" / "input.txt").write_text("plan v2\n")  # an input changed
    r = run_bash(chain, "capture", env_extra=env)
    assert r.returncode == 3 and "smoke step is not complete" in r.stdout
    assert "input data/I1a/input.txt changed" in r.stderr


@pytest.mark.parametrize("mode", ["onefile", "nosidecar", "wrongstep", "limited"])
def test_incomplete_manifest_pair_fails(chain, tmp_path, mode):
    approve(tmp_path / "logs", "P0")
    r = run_bash(chain, "smoke", env_extra=real_env(tmp_path, DRIVER_MODE=mode))
    assert r.returncode == 1 and "manifest pair is missing or invalid" in r.stdout


def test_one_arbitrary_file_manifest_cannot_satisfy_a_step(chain, tmp_path):
    """The review's case: a hash list covering one arbitrary file no longer counts as done."""
    approve(tmp_path / "logs", "P0")
    approve(tmp_path / "logs", "P1")
    write_pair(tmp_path, "smoke")
    write_pair(tmp_path, "capture", required=["results/I1a/acts/multihop.safetensors"])
    r = dry(chain, "capture")
    assert "skip capture" not in r.stdout and planned(r.stdout) == ["capture"]
    assert "required outputs not in the sha list" in r.stderr
    write_pair(tmp_path, "capture", with_sidecar=False)
    assert planned(dry(chain, "capture").stdout) == ["capture"]
    write_pair(tmp_path, "capture", sidecar_step="tokens")
    assert planned(dry(chain, "capture").stdout) == ["capture"]
    write_pair(tmp_path, "capture")
    assert "skip capture" in dry(chain, "capture").stdout


def test_chain_never_creates_markers(chain, tmp_path):
    text = (SCRIPTS / "pod_run_i1a.sh").read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        if "approv" in ln.lower():
            assert not re.search(r"(>|touch|tee|cp |mv |mkdir)", ln), ln
    env = real_env(tmp_path)
    run_bash(chain, "all", env_extra=env)
    run_bash(chain, "smoke", env_extra=env)
    assert not (tmp_path / "logs" / "approvals").exists()


def test_limited_sidecar_rejected_unless_allowed(chain, tmp_path):
    approve(tmp_path / "logs", "P1")
    write_pair(tmp_path, "smoke", limited=True)
    r = dry(chain, "capture")
    assert r.returncode == 3 and "a limited (cut-down) run" in r.stderr
    r = dry(chain, "capture", extra={"I1A_ALLOW_LIMITED": "1"})
    assert r.returncode == 0 and planned(r.stdout) == ["capture"]
    write_pair(tmp_path, "smoke", limited="yes")  # not a boolean
    assert dry(chain, "capture", extra={"I1A_ALLOW_LIMITED": "1"}).returncode == 3
    write_pair(tmp_path, "smoke", limited=False)
    assert dry(chain, "capture").returncode == 0


def test_stale_code_commit_rejected_unless_allowed(chain, tmp_path):
    approve(tmp_path / "logs", "P1")
    write_pair(tmp_path, "smoke", commit="old000")
    r = dry(chain, "capture")
    assert r.returncode == 3 and "stale: produced by code old000" in r.stderr
    r = dry(chain, "capture", extra={"I1A_ALLOW_STALE": "1"})
    assert r.returncode == 0 and "accepted: I1A_ALLOW_STALE=1" in r.stderr
    write_pair(tmp_path, "smoke")  # matching commit
    assert dry(chain, "capture").returncode == 0
    r = dry(chain, "capture", extra={"EMX_GIT_COMMIT": ""})  # no current commit: all stale
    assert r.returncode == 3 and "unknown (no EMX_GIT_COMMIT)" in r.stderr


# pod_pull_results.sh --exp I1a (a copy in tmp_path; POD_PULL_SOURCE replaces the SSH source)


@pytest.fixture
def pull(tmp_path):
    s = copy_script(tmp_path, "pod_pull_results.sh")
    pod = tmp_path / "pod"  # a mini repo root as on the pod: pod/results/I1a/...
    res = pod / "results" / "I1a"
    (res / "acts").mkdir(parents=True)
    (res / "acts" / "multihop.safetensors").write_bytes(b"\0" * 64)
    (res / "summary.json").write_text("{}\n")

    def sha_line(rel):
        return f"{sha256(pod / rel)}  {rel}"

    (res / "MANIFEST.capture.sha256").write_text(
        sha_line("results/I1a/acts/multihop.safetensors") + "\n"
    )
    (res / "MANIFEST.probe.sha256").write_text(sha_line("results/I1a/summary.json") + "\n")
    for step in ("capture", "probe", "manifest"):
        (res / f"MANIFEST.{step}.json").write_text(json.dumps({"step": step, "limited": False}))
    closing = [sha_line(f"results/I1a/MANIFEST.{s}.sha256") for s in ("capture", "probe")]
    closing.append(sha_line("results/I1a/MANIFEST.capture.json"))
    (res / "MANIFEST.manifest.sha256").write_text("\n".join(closing) + "\n")
    return s, res


def run_pull(script, res, *args):
    return run_bash(script, "--exp", "I1a", *args, env_extra={"POD_PULL_SOURCE": str(res)})


def test_pull_i1a_verifies_then_promotes(pull, tmp_path):
    script, res = pull
    r = run_pull(script, res)
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "I1a"
    assert (dest / "acts" / "multihop.safetensors").stat().st_size == 64
    assert (dest / "MANIFEST.manifest.sha256").exists()
    assert "verifying results/I1a/MANIFEST.probe.sha256" in r.stdout
    assert list((dest / "staging").iterdir()) == []  # the staging dir is cleaned up


@pytest.mark.parametrize(
    "breakage",
    ["tamper_output", "tamper_step_manifest", "no_closing", "no_steps", "limited", "no_sidecar"],
)
def test_pull_i1a_refuses_and_promotes_nothing(pull, tmp_path, breakage):
    script, res = pull
    if breakage == "tamper_output":  # listed by a step manifest, not by the closing one
        (res / "acts" / "multihop.safetensors").write_bytes(b"\1" * 64)
    elif breakage == "tamper_step_manifest":
        (res / "MANIFEST.probe.sha256").write_text("0" * 64 + "  results/I1a/summary.json\n")
    elif breakage == "no_closing":
        (res / "MANIFEST.manifest.sha256").unlink()
    elif breakage == "no_steps":
        (res / "MANIFEST.manifest.sha256").write_text(
            f"{sha256(res / 'summary.json')}  results/I1a/summary.json\n"
        )
    elif breakage == "limited":
        (res / "MANIFEST.probe.json").write_text(json.dumps({"step": "probe", "limited": True}))
    elif breakage == "no_sidecar":
        (res / "MANIFEST.manifest.json").unlink()
    r = run_pull(script, res)
    assert r.returncode != 0, r.stdout
    assert "nothing promoted" in r.stderr
    dest = tmp_path / "results" / "I1a"
    assert not (dest / "summary.json").exists() and not (dest / "acts").exists()


def test_pull_i1a_rejects_e1_flags(pull):
    script, res = pull
    assert run_pull(script, res, "--manifest", "x").returncode == 2


def test_pod_hunt_dry_run_i1a(tmp_path):
    r = subprocess.run(
        ["/bin/bash", "scripts/pod_hunt.sh", "--exp", "I1a", "--slot", "_Z", "--dry-run"],
        cwd=ROOT,
        env={
            **os.environ,
            "EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl"),
            "EMX_POD_LOG_DIR": str(tmp_path),  # never read the real pod log (a live pod broke it)
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert len(reqs) == len(rp.GPU_FALLBACK)
    assert {q["json"]["name"] for q in reqs} == {"i1a-z"}


def test_pod_hunt_passes_exp_everywhere():
    text = (SCRIPTS / "pod_hunt.sh").read_text()
    assert 'scripts/pod_up.py --exp "$EXP" --slot "$SLOT" || rc=$?' in text
    assert 'scripts/pod_push.sh --exp "$EXP" --slot "$SLOT"' in text
    assert "i1a_watchdog.sh" in text


# --- hfget (mocked Hub) -------------------------------------------------------------------------


class FakeApi:
    def __init__(self, sizes: dict[tuple[str, str], int], folders=(), repo_files=None):
        self.sizes = sizes  # (repo, path) -> bytes
        self.folders = set(folders)
        self.repo_files = repo_files or []
        self.calls: list[tuple] = []

    def get_paths_info(self, repo, paths, *, revision=None, repo_type=None, **kw):
        self.calls.append(("get_paths_info", repo, tuple(paths), revision, repo_type))
        out = []
        for p in paths:
            if (repo, p) in self.folders:
                out.append(SimpleNamespace(path=p))  # a RepoFolder has no size
            elif (repo, p) in self.sizes:
                out.append(SimpleNamespace(path=p, size=self.sizes[(repo, p)]))
        return out

    def list_repo_files(self, repo, *, revision=None, repo_type=None, **kw):
        self.calls.append(("list_repo_files", repo, revision))
        return list(self.repo_files)


class FakeDownloader:
    def __init__(self, root: Path, short: set[str] = frozenset()):
        self.root = root
        self.short = short
        self.calls: list[tuple] = []

    def __call__(self, repo, filename, *, revision, repo_type, local_dir=None, token=None):
        self.calls.append((repo, filename, revision, repo_type, local_dir))
        base = Path(local_dir) if local_dir else self.root / "cache" / repo.replace("/", "--")
        p = base / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        n = self.api.sizes[(repo, filename)] - (1 if filename in self.short else 0)
        p.write_bytes(b"\0" * n)
        return str(p)


@pytest.fixture(autouse=True)
def no_snapshot(monkeypatch):
    import huggingface_hub

    def boom(*a, **k):
        raise AssertionError("snapshot_download must never be called")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)


@pytest.mark.parametrize(
    "files",
    [
        [],
        "config.json",
        ["*.safetensors"],
        ["model-0000?.safetensors"],
        ["[ab].json"],
        ["sub/"],
        ["a.json", "a.json"],
    ],
)
def test_fetch_refuses_whole_repo_or_glob(files, tmp_path):
    api = FakeApi({})
    with pytest.raises(hfget.HfGetError):
        hfget.fetch("r/x", files, revision=SHA, api=api, downloader=FakeDownloader(tmp_path))
    assert api.calls == []  # refused before any Hub call


def test_fetch_refuses_unpinned_revision(tmp_path):
    with pytest.raises(hfget.HfGetError, match="40-hex"):
        hfget.plan_bytes("r/x", ["a.json"], revision="main", api=FakeApi({("r/x", "a.json"): 1}))


def test_fetch_refuses_oversize_plan(tmp_path):
    api = FakeApi({("r/x", "a.bin"): C.HF_MAX_DOWNLOAD_BYTES, ("r/x", "b.bin"): 1})
    dl = FakeDownloader(tmp_path)
    dl.api = api
    assert hfget.plan_bytes("r/x", ["a.bin", "b.bin"], revision=SHA, api=api) == (
        C.HF_MAX_DOWNLOAD_BYTES + 1
    )
    with pytest.raises(hfget.HfGetError, match="cap"):
        hfget.fetch("r/x", ["a.bin", "b.bin"], revision=SHA, api=api, downloader=dl)
    assert dl.calls == []


def test_fetch_refuses_missing_file_and_folder(tmp_path):
    api = FakeApi({("r/x", "a.json"): 3}, folders={("r/x", "dir")})
    with pytest.raises(hfget.HfGetError, match="not found"):
        hfget.plan_bytes("r/x", ["a.json", "gone.json"], revision=SHA, api=api)
    with pytest.raises(hfget.HfGetError, match="folder"):
        hfget.plan_bytes("r/x", ["dir"], revision=SHA, api=api)


def test_fetch_downloads_named_files_and_checks_sizes(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    api = FakeApi({("r/x", "a.json"): 5, ("r/x", "sub/b.safetensors"): 7})
    dl = FakeDownloader(tmp_path)
    dl.api = api
    out = hfget.fetch(
        "r/x",
        ["a.json", "sub/b.safetensors"],
        revision=SHA,
        repo_type="dataset",
        local_dir=tmp_path / "L",
        api=api,
        downloader=dl,
    )
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    from huggingface_hub import constants

    assert constants.HF_HUB_DISABLE_XET is True
    assert {k: v.stat().st_size for k, v in out.items()} == {"a.json": 5, "sub/b.safetensors": 7}
    assert [c[1:4] for c in dl.calls] == [
        ("a.json", SHA, "dataset"),
        ("sub/b.safetensors", SHA, "dataset"),
    ]
    dl2 = FakeDownloader(tmp_path, short={"a.json"})
    dl2.api = api
    with pytest.raises(hfget.HfGetError, match="expected 5"):
        hfget.fetch("r/x", ["a.json"], revision=SHA, api=api, downloader=dl2)


def i1a_api(model_shard_bytes: int = 10) -> FakeApi:
    repo_files = [
        "config.json",
        "generation_config.json",
        "model.safetensors.index.json",
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
        "merges.txt",
        "vocab.json",
        "README.md",
        ".gitattributes",
        "pytorch_model.bin",
        "original/consolidated.safetensors",
    ]
    sizes = {
        (C.MODEL_ID, f): (model_shard_bytes if f.endswith(".safetensors") else 2)
        for f in repo_files
    }
    sizes[(C.JLENS_REPO, C.JLENS_FILE)] = 4
    for f in (*C.OLENS_FILES, *C.AR_FILES):
        sizes[(C.ADAPTER_REPO, f)] = 3
    for f in C.WHITENER_FILES:
        sizes[(C.WHITENER_REPO, f)] = C.WHITENER_FILE_BYTES if f.endswith(".safetensors") else 6
    return FakeApi(sizes, repo_files=repo_files)


def test_model_files_filter():
    files = hfget.model_files(i1a_api())
    assert files == sorted(
        [
            "config.json",
            "generation_config.json",
            "model.safetensors.index.json",
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "chat_template.jinja",
            "merges.txt",
            "vocab.json",
        ]
    )


def test_plan_all_i1a_totals_and_cap(monkeypatch):
    api = i1a_api()
    plan = hfget.plan_all_i1a(api)
    g = plan["groups"]
    assert g["whiteners"]["bytes"] == 17 * C.WHITENER_FILE_BYTES + 6
    assert g["whiteners"]["repo_type"] == "dataset" and g["whiteners"]["revision"] == C.WHITENER_REV
    assert g["adapters"]["files"] == [*C.OLENS_FILES, *C.AR_FILES]
    assert plan["total_bytes"] == sum(x["bytes"] for x in g.values())
    # every Hub query names its files and pins the contract revision
    for call in api.calls:
        if call[0] == "get_paths_info":
            assert call[3] in (C.MODEL_REV, C.JLENS_REV, C.ADAPTER_REV, C.WHITENER_REV)
            assert not any(ch in p for p in call[2] for ch in "*?[")
    # the grand total is capped even when every group alone fits
    monkeypatch.setattr(C, "HF_MAX_DOWNLOAD_BYTES", plan["total_bytes"] - 1)
    with pytest.raises(hfget.HfGetError, match="all groups"):
        hfget.plan_all_i1a(api)


def test_plan_all_i1a_checks_whitener_size():
    api = i1a_api()
    api.sizes[(C.WHITENER_REPO, C.WHITENER_FILES[0])] = 1
    with pytest.raises(hfget.HfGetError, match="WHITENER_FILE_BYTES"):
        hfget.plan_all_i1a(api)


def test_fetch_all_i1a_into_local_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "WHITENER_FILE_BYTES", 8)
    api = i1a_api()
    for f in C.WHITENER_FILES:
        if f.endswith(".safetensors"):
            api.sizes[(C.WHITENER_REPO, f)] = 8
    dl = FakeDownloader(tmp_path)
    dl.api = api
    got = hfget.fetch_all_i1a(tmp_path / "w", api=api, downloader=dl)
    assert got["model_dir"] == tmp_path / "w" / "model"
    assert (got["model_dir"] / "model-00002-of-00002.safetensors").stat().st_size == 10
    assert not (got["model_dir"] / "pytorch_model.bin").exists()
    assert got["jlens"] == tmp_path / "w" / "jlens" / C.JLENS_FILE
    assert got["olens_dir"] == tmp_path / "w" / "adapters" / C.OLENS_SUBDIR
    assert got["ar_dir"] == tmp_path / "w" / "adapters" / C.AR_SUBDIR
    assert got["whitener_dir"] == tmp_path / "w" / "whiteners" / C.WHITENER_DIR
    downloaded = {(c[0], c[1]) for c in dl.calls}
    assert (C.MODEL_ID, "README.md") not in downloaded
    assert len(downloaded) == 10 + 1 + len(C.OLENS_FILES) + len(C.AR_FILES) + len(C.WHITENER_FILES)


def test_fetch_all_refuses_before_any_download_when_over_cap(tmp_path, monkeypatch):
    api = i1a_api(model_shard_bytes=C.HF_MAX_DOWNLOAD_BYTES // 2)
    dl = FakeDownloader(tmp_path)
    dl.api = api
    with pytest.raises(hfget.HfGetError, match="cap"):
        hfget.fetch_all_i1a(tmp_path / "w", api=api, downloader=dl)
    assert dl.calls == []


def test_write_plan_records_pins_and_sizes(tmp_path):
    plan = hfget.plan_all_i1a(i1a_api())
    out = hfget.write_plan(plan, tmp_path / "download_plan.json")
    d = json.loads(out.read_text())
    assert d["total_bytes"] == plan["total_bytes"] and d["cap_bytes"] == C.HF_MAX_DOWNLOAD_BYTES
    w = d["groups"]["whiteners"]
    assert w["revision"] == C.WHITENER_REV and w["repo_type"] == "dataset"
    assert w["files"][C.WHITENER_FILES[0]] == C.WHITENER_FILE_BYTES
    assert d["groups"]["model"]["revision"] == C.MODEL_REV
    assert hfget.PLAN_PATH == C.RESULTS / "download_plan.json"
