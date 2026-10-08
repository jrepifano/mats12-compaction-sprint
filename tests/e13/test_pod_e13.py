"""E13 pod tooling, offline (E13 implementation plan, unit A; §8 finding 8). `--exp E13` in
emx.runpod / pod_up (one pod e13-a in slot _A, logs/pods_E13.jsonl, CAPS["E13"],
logs/approvals/E13.approved, a reservation, `--stage stage1|patch`, GPU fallback H100 secure ->
NVL -> PCIe -> A100), the pod-hour helper emx.e13.podhours, pod_push.sh's E13 transfer set, pod
env and pod-hour killswitch deadline, pod_setup.sh --only e13, pod_pull_results.sh --exp E13 and
scripts/e13_watchdog.sh. Mirrors tests/eq0/test_pod_eq0.py (whose i1a helpers are reused). Nothing
here contacts RunPod, SSH or the Hub: fake clients and fake git/uv/ssh/rsync on PATH, sandboxed
event logs, ledgers and reservations. The real scripts/pod_run_e13.sh and scripts/e13_pod.py are
never created here (fake checkouts only)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp
from emx.e13 import contracts as C
from emx.e13 import podhours as PH

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UTC_LINE = "2026-10-08T12:00:00Z approved by Jacob (test)\n"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# helpers only (no fixtures or tests are bound here, so nothing of that module is collected)
_i1a = _load("_e13_reuse_i1a_pod_tests", ROOT / "tests" / "i1a" / "test_pod_i1a.py")
FAKE_GIT, copy_script, fake_bin, run_bash, sha256 = (
    _i1a.FAKE_GIT,
    _i1a.copy_script,
    _i1a.fake_bin,
    _i1a.run_bash,
    _i1a.sha256,
)
FakeSpend, FakeClient, HEAD, LAPTOP, BASH4 = (
    _i1a.FakeSpend,
    _i1a.FakeClient,
    _i1a.HEAD,
    _i1a.LAPTOP,
    _i1a.BASH4,
)


def ev(event: str, pod_id: str, t: datetime, **kw) -> dict:
    return {"utc": t.isoformat(), "exp": "E13", "event": event, "pod_id": pod_id, **kw}


def created(pod_id, slot, t0, rate, **kw):
    return {
        "event": "created",
        "pod_id": pod_id,
        "slot": slot,
        "gpu": "g",
        "utc": t0.isoformat(),
        "rate_usd_h": rate,
        **kw,
    }


def h(n: float) -> timedelta:
    return timedelta(hours=n)


# --- emx.e13.podhours ----------------------------------------------------------------------------


def test_closed_and_open_sessions_add_up():
    events = [
        ev("created", "p1", NOW - h(5)),
        ev("rate", "p1", NOW - h(5) + timedelta(seconds=20), rate_usd_h=3.49),
        ev("running", "p1", NOW - h(4.9)),
        ev("terminated", "p1", NOW - h(3)),
        ev("charged", "p1", NOW - h(3), usd=6.98),
        ev("created", "p2", NOW - h(1.5)),
        {"utc": NOW.isoformat(), "event": "note"},  # no pod_id: ignored
    ]
    ss = PH.sessions(events, NOW)
    assert [(s["pod_id"], s["open"], round(s["hours"], 6)) for s in ss] == [
        ("p1", False, 2.0),
        ("p2", True, 1.5),
    ]
    assert PH.used_hours(events, NOW) == pytest.approx(3.5)


def test_relaunch_after_a_closed_session_counts_both():
    """Pod 1 closed after 2 h, pod 2 (a relaunch) open for 1 h: 3 h used, 8 h remain, and the
    pod-hour deadline is now + 8 h."""
    events = [
        ev("created", "p1", NOW - h(5)),
        ev("terminated", "p1", NOW - h(3)),
        ev("created", "p2", NOW - h(1)),
    ]
    d = PH.deadline(events, NOW)
    assert d["used_h"] == pytest.approx(3.0) and d["remaining_h"] == pytest.approx(8.0)
    assert d["open_sessions"] == 1
    assert d["podhours_deadline"] == int(NOW.timestamp() + 8 * 3600)
    assert d["deadline"] == d["podhours_deadline"] and d["source"] == "pod_hours"


def test_the_earlier_of_the_two_deadlines_is_chosen():
    events = [ev("created", "p1", NOW - h(10))]  # 1 h left
    ph = int(NOW.timestamp() + 3600)
    d = PH.deadline(events, NOW, dollar_deadline=ph + 600)
    assert (d["deadline"], d["source"]) == (ph, "pod_hours")
    d = PH.deadline(events, NOW, dollar_deadline=ph - 600)
    assert (d["deadline"], d["source"]) == (ph - 600, "dollars")
    assert d["podhours_deadline"] == ph


def test_refuses_when_no_pod_hours_remain():
    events = [ev("created", "p1", NOW - h(12)), ev("terminated", "p1", NOW - h(0.5))]  # 11.5 h
    with pytest.raises(PH.NoPodHoursLeft):
        PH.deadline(events, NOW)
    exactly = [ev("created", "p1", NOW - h(C.POD_HOURS_CAP))]
    with pytest.raises(PH.NoPodHoursLeft):
        PH.deadline(exactly, NOW)


def test_two_open_sessions_share_the_remaining_hours():
    events = [ev("created", "p1", NOW - h(1)), ev("created", "p2", NOW - h(1))]  # 2 h used
    d = PH.deadline(events, NOW)
    assert d["open_sessions"] == 2 and d["remaining_h"] == pytest.approx(9.0)
    assert d["podhours_deadline"] == int(NOW.timestamp() + 4.5 * 3600)


def test_ambiguous_logs_over_count():
    events = [
        # a second `created` while open keeps the EARLIER start
        ev("created", "a", NOW - h(3)),
        ev("created", "a", NOW - h(1)),
        # a second `terminated` keeps the EARLIER end (as emx.runpod.pod_sessions)
        ev("created", "b", NOW - h(4)),
        ev("terminated", "b", NOW - h(3)),
        ev("terminated", "b", NOW - h(2)),
        # no `created`: counted from the earliest event we have
        ev("running", "c", NOW - h(2)),
        # created again after a close: a new session, counted again
        ev("created", "d", NOW - h(6)),
        ev("terminated", "d", NOW - h(5.5)),
        ev("created", "d", NOW - h(0.5)),
    ]
    by = {}
    for s in PH.sessions(events, NOW):
        by.setdefault(s["pod_id"], []).append(round(s["hours"], 6))
    assert by == {"a": [3.0], "b": [1.0], "c": [2.0], "d": [0.5, 0.5]}
    # file order does not matter: events are sorted by time per pod
    assert PH.used_hours(list(reversed(events)), NOW) == pytest.approx(7.0)


def test_malformed_logs_raise(tmp_path):
    with pytest.raises((KeyError, ValueError)):
        PH.sessions([{"event": "created", "pod_id": "x"}], NOW)  # no utc
    with pytest.raises(ValueError):
        PH.sessions([{"event": "created", "pod_id": "x", "utc": "yesterday"}], NOW)
    p = tmp_path / "pods_E13.jsonl"
    p.write_text("{not json\n")
    with pytest.raises(ValueError):
        PH.read_events(p)
    assert PH.read_events(tmp_path / "missing.jsonl") == []


def test_status_reserves_one_interval_of_every_open_session():
    events = [ev("created", "p1", NOW - h(10.9))]
    st = PH.status(events, NOW)
    assert st["hard_stop"] is False and st["open_sessions"] == 1
    st = PH.status(events, NOW, reserve_hours=600 / 3600)
    assert st["hard_stop"] is True and st["projected_h"] == pytest.approx(10.9 + 1 / 6, abs=1e-3)
    closed = [ev("created", "p1", NOW - h(6)), ev("terminated", "p1", NOW - h(1))]
    assert PH.status(closed, NOW, reserve_hours=10)["hard_stop"] is False  # no open session


def test_events_path_follows_the_sandbox_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("EMX_POD_LOG_DIR", str(tmp_path))
    assert PH.events_path() == tmp_path / "pods_E13.jsonl"
    monkeypatch.delenv("EMX_POD_LOG_DIR")
    assert PH.events_path() == C.REPO_ROOT / C.POD_EVENTS


def run_ph(*args, tmp_path, events=None):
    p = tmp_path / "pods_E13.jsonl"
    if events is not None:
        p.write_text("".join(json.dumps(e) + "\n" for e in events))
    env = {**os.environ, "EMX_POD_LOG_DIR": str(tmp_path)}
    return subprocess.run(
        [sys.executable, "-m", "emx.e13.podhours", "--now", str(NOW.timestamp()), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def kv(stdout: str) -> dict[str, str]:
    return dict(ln.split("=", 1) for ln in stdout.splitlines() if "=" in ln)


def test_cli_deadline_status_and_refusal(tmp_path):
    r = run_ph(tmp_path=tmp_path)  # no log yet: 11 h remain
    assert r.returncode == 0, r.stderr
    assert kv(r.stdout)["EMX_E13_PODHOURS_REMAINING"] == "11.0"
    events = [ev("created", "p1", NOW - h(5)), ev("terminated", "p1", NOW - h(3))]
    events.append(ev("created", "p2", NOW - h(1)))
    ph = int(NOW.timestamp() + 8 * 3600)
    r = run_ph("--dollar-deadline", str(ph - 5), tmp_path=tmp_path, events=events)
    out = kv(r.stdout)
    assert r.returncode == 0, r.stderr
    assert out["EMX_E13_PODHOURS_DEADLINE"] == str(ph)
    assert out["EMX_KILLSWITCH_DEADLINE"] == str(ph - 5)
    assert out["EMX_E13_DEADLINE_SOURCE"] == "dollars" and out["EMX_E13_PODHOURS_OPEN"] == "1"
    r = run_ph("--dollar-deadline", str(ph + 5), tmp_path=tmp_path)
    assert kv(r.stdout)["EMX_KILLSWITCH_DEADLINE"] == str(ph)
    r = run_ph("--status", "--reserve-hours", "0.1", tmp_path=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["used_h"] == pytest.approx(3.0)
    spent = [ev("created", "p1", NOW - h(11.5))]
    r = run_ph(tmp_path=tmp_path, events=spent)
    assert r.returncode == 3 and "REFUSED" in r.stderr and "EMX_KILLSWITCH" not in r.stdout
    r = run_ph("--status", tmp_path=tmp_path)
    assert r.returncode == 2 and json.loads(r.stdout)["hard_stop"] is True
    (tmp_path / "pods_E13.jsonl").write_text("garbage\n")
    assert run_ph(tmp_path=tmp_path).returncode == 1
    assert run_ph("--status", tmp_path=tmp_path).returncode == 1


# --- emx.runpod under E13 ------------------------------------------------------------------------


@pytest.fixture
def e13(monkeypatch, tmp_path):
    """emx.runpod configured for E13 with .env, logs and the event log in tmp_path; restored."""
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS", "LOGS", "ROOT"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setenv("EMX_POD_LOG_DIR", str(tmp_path))  # emx.e13.podhours reads the same log
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    rp.configure("E13")
    return tmp_path


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


def approve(logs: Path, text: str = UTC_LINE) -> Path:
    p = logs / "approvals" / "E13.approved"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def beat(logs: Path) -> None:
    (logs / "watchdog_E13.heartbeat").write_text(f"{time.time()} 600 {os.getpid()}\n")


def test_configure_e13_names_logs_and_caps(e13):
    assert rp.EXP == "E13" and rp.POD_NAME_PREFIX == "e13-"
    assert rp.POD_EVENTS == e13 / "pods_E13.jsonl" == PH.events_path()
    c = rp.caps()
    assert (c.hard_cap_usd, c.gpu_limit_usd) == (C.HARD_CAP_USD, C.GPU_LIMIT_USD) == (50.0, 38.0)
    assert rp.gpu_kill_limit(0.0) == 38.0 and rp.gpu_kill_limit(20.0) == 30.0  # 50 - 0 - 20
    assert rp.pod_body(rp.GPU_FALLBACK[0], "_A", "ssh-ed25519 k")["name"] == "e13-a"
    assert rp.heartbeat_path() == e13 / Path(C.WATCHDOG_HEARTBEAT).relative_to("logs")


def test_untracked_reaper_only_sees_e13_pods(e13):
    api = [{"id": "x", "name": "e13-a"}, {"id": "y", "name": "eq0-a"}, {"id": "z", "name": "e1-a"}]
    assert [p["id"] for p in rp.untracked_pods(api, [])] == ["x"]


# --- pod_up --exp E13 ----------------------------------------------------------------------------


@pytest.fixture
def up(spend, e13, monkeypatch):
    mod = _load("_e13_pod_up", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def use_client(monkeypatch, fake):
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)


def test_e13_fallback_order_follows_the_contracts(up):
    got = [(o.gpu_type_id, o.cloud, o.rate_usd_h) for o in up.e13_gpu_options()]
    assert got == [
        ("NVIDIA H100 80GB HBM3", "SECURE", 3.49),
        ("NVIDIA H100 NVL", "SECURE", 2.59),
        ("NVIDIA H100 PCIe", "COMMUNITY", 2.69),
        ("NVIDIA A100-SXM4-80GB", "SECURE", 1.39),
    ]
    assert [g for g, _c, _r in got] == [g for g, _r in C.FALLBACK_GPUS]
    assert next(o.gpu_type_id for o in up.e13_gpu_options(3)) == "NVIDIA A100-SXM4-80GB"


def test_pod_up_e13_refuses_without_its_approval(up, e13, monkeypatch):
    beat(e13)
    (e13 / "approvals").mkdir()
    (e13 / "approvals" / "EQ0.approved").write_text(UTC_LINE)  # another experiment's: no
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    args = ["--exp", "E13", "--slot", "_A", "--reservation", "r1", "--budget-usd", "12"]
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []
    approve(e13, "go\n")  # no UTC line: still refused
    with pytest.raises(SystemExit) as e:
        up.main(args)
    assert e.value.code == up.GATE_REFUSED and fake.created == []


@pytest.mark.parametrize("slot", ["_B", "_C"])
def test_pod_up_e13_refuses_any_slot_but_a(up, e13, monkeypatch, slot):
    beat(e13)
    approve(e13)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E13", "--slot", slot, "--reservation", "r1", "--budget-usd", "12"])
    assert e.value.code == 2 and fake.created == []


def test_pod_up_e13_needs_a_reservation_and_a_known_stage(up, e13, monkeypatch):
    beat(e13)
    approve(e13)
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E13", "--slot", "_A"])
    assert e.value.code == 2 and fake.created == []
    with pytest.raises(SystemExit) as e:
        up.main(
            [
                "--exp",
                "E13",
                "--slot",
                "_A",
                "--stage",
                "smoke",
                "--reservation",
                "r1",
                "--budget-usd",
                "12",
            ]
        )
    assert e.value.code == 2 and fake.created == []


def test_pod_up_e13_refuses_when_no_pod_hours_remain(up, e13, monkeypatch):
    beat(e13)
    approve(e13)
    now = datetime.now(UTC)
    rp.POD_EVENTS.write_text(
        json.dumps(created("old", "_A", now - h(12), 3.49))
        + "\n"
        + json.dumps(ev("terminated", "old", now - h(0.5)))
        + "\n"
    )
    fake = FakeClient(create_results=[{"id": "never"}])
    use_client(monkeypatch, fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--exp", "E13", "--slot", "_A", "--reservation", "r1", "--budget-usd", "12"])
    assert e.value.code == up.GATE_REFUSED and fake.created == []


class _ReservedClient(FakeClient):
    def list_pods(self):
        return []


@pytest.mark.parametrize("stage", [None, "patch"])
def test_pod_up_e13_reserved_launch_records_its_stage(e13, monkeypatch, tmp_path, capsys, stage):
    """Real emx.spend and emx.runpod_reservations, both on sandboxed files."""
    from emx import runpod_reservations as rr
    from emx import spend as real_spend

    monkeypatch.setenv(rr.RESERVATIONS_ENV, str(tmp_path / "res.jsonl"))
    monkeypatch.setenv(real_spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(rp, "forget_host_key", lambda h, p: None)
    up = _load("_e13_pod_up_reserved", SCRIPTS / "pod_up.py")
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    beat(e13)
    approve(e13)
    want = stage or "stage1"
    usd = C.POD_RESERVATION_USD[want]
    rid = rr.reserve("E13", usd, f"E13 pod {want}", balance=200.0)
    fake = _ReservedClient(create_results=[{"id": "pq", "costPerHr": 3.49}])
    use_client(monkeypatch, fake)
    args = ["--exp", "E13", "--slot", "_A", "--reservation", rid, "--budget-usd", str(usd)]
    rc = up.main(args + (["--stage", stage] if stage else []))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert [(b["name"], b["cloudType"], b["gpuTypeIds"][0]) for b in fake.created] == [
        ("e13-a", "SECURE", "NVIDIA H100 80GB HBM3")
    ]
    assert fake.created[0]["containerDiskInGb"] == C.VOLUME_GB
    assert "EMX_POD_CREATE=ok" in out and "EMX_POD_DEADLINE=" in out
    assert f"stage (E13): {want}" in out and "pod-hours (E13): used 0.0" in out
    assert rr.get(rid)["launched"] and rr.get(rid)["pods"] == ["pq"]
    e = rp.read_pod_events()[0]
    assert e["event"] == "created" and e["exp"] == "E13" and e["reservation_id"] == rid
    assert e["stage"] == want
    assert rp.env("RUNPOD_POD_ID_A") == "pq"


def run_py(*args, tmp_path):
    env = {
        **os.environ,
        "EMX_EXP": "E1",  # ignored: every script takes --exp
        "EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl"),
        "EMX_POD_LOG_DIR": str(tmp_path),
    }
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
    parts = stdout.split("DRY RUN (not sent):\n")[1:]
    return [json.loads(p.split("\nbudget gate")[0]) for p in parts]


def test_pod_up_dry_run_e13(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "E13", "--slot", "_A", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    reqs = dry_requests(r.stdout)
    assert [q["method"] for q in reqs] == ["POST"] * 4  # nothing but the (unsent) creates
    assert {q["json"]["name"] for q in reqs} == {"e13-a"}
    assert [(q["json"]["cloudType"], q["json"]["gpuTypeIds"][0]) for q in reqs] == [
        ("SECURE", "NVIDIA H100 80GB HBM3"),
        ("SECURE", "NVIDIA H100 NVL"),
        ("COMMUNITY", "NVIDIA H100 PCIe"),
        ("SECURE", "NVIDIA A100-SXM4-80GB"),
    ]
    assert {q["json"]["containerDiskInGb"] for q in reqs} == {C.VOLUME_GB}
    assert "approval (E13): MISSING" in r.stdout and "stage (E13): stage1" in r.stdout
    assert "watchdog (E13): NOT LIVE" in r.stdout and "budget gate (E13)" in r.stdout
    assert not (tmp_path / "pods_E13.jsonl").exists()  # a dry run logs no event
    approve(tmp_path)
    beat(tmp_path)
    r = run_py(
        "scripts/pod_up.py",
        "--exp",
        "E13",
        "--slot",
        "_A",
        "--stage",
        "patch",
        "--dry-run",
        tmp_path=tmp_path,
    )
    assert r.returncode == 0 and "approval (E13): present" in r.stdout
    assert "watchdog (E13): live" in r.stdout and "stage (E13): patch" in r.stdout


def test_pod_up_dry_run_e13_refuses_other_slots(tmp_path):
    r = run_py("scripts/pod_up.py", "--exp", "E13", "--slot", "_B", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "E13 has one pod" in r.stderr and "DRY RUN" not in r.stdout


def test_pod_up_stage_stays_e9_and_e13_only(tmp_path):
    r = run_py(
        "scripts/pod_up.py",
        "--exp",
        "EQ0",
        "--slot",
        "_A",
        "--stage",
        "patch",
        "--dry-run",
        tmp_path=tmp_path,
    )
    assert r.returncode == 2 and "--stage" in r.stderr


def test_pod_down_and_budget_check_dry_e13(tmp_path):
    r = run_py("scripts/pod_down.py", "--exp", "E13", "--all-exp", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "<each e13-* pod id>" in r.stdout
    r = run_py("scripts/pod_budget_check.py", "--exp", "E13", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["exp"] == "E13" and d["gpu_kill_limit_usd"] == 38.0 and d["hard_cap_usd"] == 50.0
    assert d["hard_stop"] is False


# --- pod_push.sh --exp E13 (a copy in tmp_path, a fake checkout and a FAKE git) ------------------

# the real contracts, registry and pod-hour helper are copied into the fake checkout (the E13
# branch runs them); every other tracked file is a stub
REAL = [
    "src/emx/__init__.py",
    "src/emx/e13/__init__.py",
    "src/emx/e13/contracts.py",
    "src/emx/e13/podhours.py",
    "src/emx/qwen/__init__.py",
    "src/emx/qwen/registry.py",
]
TRACKED = [
    "pyproject.toml",
    "uv.lock",
    *REAL,
    "scripts/e13_pod.py",
    "scripts/pod_run_e13.sh",
    "prompts/E1/aligned_v1.txt",
    "prompts/E13/notice_writer_v1.txt",
    "results/E13/inputs/manifest.json",
    "results/E13/inputs/notices.jsonl",
    "results/E13/inputs/tasks.jsonl",
    "results/E13/gen/base.meta.json",  # tracked, but results/E13 is not a root (inputs only)
    "data/E1/questions/general.jsonl",
    "data/E1/questions/medical.jsonl",  # tracked, but not an E13 root
    "data/E1/manifest.json",  # not an E13 root
    "data/E1b/questions/ext48.jsonl",
    "data/E1b/manifest.json",  # not an E13 root
    "data/EQ0/x.jsonl",  # another experiment's data
    "data/shared/answer_pairs_v1.jsonl",  # not an E13 root
    "configs/E1/a.json",
    "prompts/I9/monitor_zero_shot.txt",
    "third_party/workspace-bench/pyproject.toml",
    "docs/private-notes.md",
]
TRAIN = [f"results/E13/inputs/train/{a}.jsonl" for a in C.ARMS]
IGNORED = [  # gitignored, sent as named extras
    *TRAIN,
    "results/E13/inputs/heldout_loss.jsonl",
    "results/E13/decisions/plumbing.json",
    "results/E13/direction/resid_l14.json",
]
NEVER = [
    ".env.local",
    "data/E1/questions/narrow.jsonl",  # held out (gitignored): never leaves the laptop
    "data/E1/train_bad.jsonl",
    "data/raw/bad_medical_advice.jsonl",
    "results/E13/inputs/alpaca_raw/raw.jsonl",  # gitignored, under a root, not an extra
    "results/E13/gen/base.csv",
    "results/E13/direction/acts_resid_l14.safetensors",
    "results/E13/decisions/notes.txt",
    "logs/approvals/EQ0.approved",
]
REQUIRED = [
    "results/E13/inputs/manifest.json",
    *TRAIN,
    "results/E13/inputs/heldout_loss.jsonl",
    "data/E1/questions/general.jsonl",
    "data/E1b/questions/ext48.jsonl",
    "scripts/e13_pod.py",
    "scripts/pod_run_e13.sh",
    "logs/approvals/E13.approved",
]
MANIFEST = {"train": {a: {"path": f"train/{a}.jsonl", "sha256": "0" * 64} for a in C.ARMS}}


@pytest.fixture
def checkout(tmp_path):
    s = copy_script(tmp_path, "pod_push.sh")
    for f in TRACKED + IGNORED + NEVER:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        if f in REAL:
            shutil.copy(ROOT / f, tmp_path / f)
        else:
            (tmp_path / f).write_text("x\n")
    (tmp_path / "results/E13/inputs/manifest.json").write_text(json.dumps(MANIFEST))
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, tmp_path / ".venv" / "bin" / "python")
    (tmp_path / "logs").mkdir(exist_ok=True)
    fg = tmp_path / ".fakegit"
    fg.mkdir()
    (fg / "tracked").write_text("\n".join(TRACKED) + "\n")
    (fg / "head").write_text(HEAD + "\n")
    (tmp_path / ".env").write_text("HF_TOKEN=hf_abc\nHF_USERNAME=jacob\n" + LAPTOP)
    fake_bin(tmp_path, "fakegit", FAKE_GIT)
    return s


def push(checkout, *args, dry=True, env=None):
    tmp = checkout.parent.parent
    extra = {
        "POD_PUSH_GIT": str(tmp / "bin" / "fakegit"),
        "FAKEGIT_DIR": str(tmp / ".fakegit"),
        "EMX_EXP": "E1",  # ignored
        "EMX_POD_LOG_DIR": str(tmp / "logs"),
        **(env or {}),
    }
    flags = ("--dry-run", "--show-files") if dry else ()
    return run_bash(checkout, *args, *flags, env_extra=extra)


def sent(stdout: str) -> set[str]:
    return {ln.split("  send ", 1)[1] for ln in stdout.splitlines() if ln.startswith("  send ")}


def line(stdout: str, tag: str) -> list[str]:
    hit = next(ln for ln in stdout.splitlines() if f"{tag}:" in ln)
    return hit.split(f"{tag}:", 1)[1].split()


def test_pod_push_e13_transfer_set_env_and_setup(checkout, tmp_path):
    approve(tmp_path / "logs")
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN (E13)" in r.stdout and "WARNING" not in r.stdout
    assert line(r.stdout, "transfer set") == [
        "src",
        "scripts",
        "prompts/E1",
        "prompts/E13",
        "data/E1/questions/general.jsonl",
        "data/E1b/questions/ext48.jsonl",
        "results/E13/inputs",
        "pyproject.toml",
        "uv.lock",
    ]
    files = sent(r.stdout)
    roots = ("src/", "scripts/", "prompts/E1/", "prompts/E13/", "results/E13/inputs/")
    want = {f for f in TRACKED if f.startswith(roots)} | {
        "pyproject.toml",
        "uv.lock",
        "data/E1/questions/general.jsonl",
        "data/E1b/questions/ext48.jsonl",
    }
    assert files == want | set(IGNORED) | {"logs/approvals/E13.approved"}
    assert set(REQUIRED) <= files
    assert not files & set(NEVER)
    assert not any(
        f.startswith(("data/EQ0", "data/shared", "configs/", "prompts/I9", "third_party/"))
        for f in files
    )
    keys = line(r.stdout, "pod env keys")
    assert keys[0] == "HF_TOKEN" and "HF_USERNAME" not in keys  # HF_TOKEN only
    assert {
        "EMX_EXP",
        "EMX_GIT_COMMIT",
        "EMX_GIT_DIRTY",
        "HF_HOME",
        "HF_HUB_DISABLE_XET",
        "WANDB_MODE",
    } <= set(keys)
    assert not any(k.startswith(("OPENAI", "OPENROUTER", "RUNPOD", "EM_DATASET")) for k in keys)
    assert "hf_abc" not in r.stdout and "secret" not in r.stdout
    assert "--check-slot _A" in r.stdout and "pod_setup.sh --only e13" in r.stdout
    assert "pod_killswitch.sh arm" in r.stdout and "emx.e13.podhours" in r.stdout
    assert "DRY RUN (E13): pod-hours: used 0.0 h of 11.0" in r.stdout


def test_pod_push_e13_requires_the_train_files_the_manifest_names(checkout, tmp_path):
    approve(tmp_path / "logs")
    m = dict(MANIFEST, extra={"rows": "results/E13/inputs/train/org_extra.jsonl"})
    (tmp_path / "results/E13/inputs/manifest.json").write_text(json.dumps(m))
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "WARNING" in ln)
    assert "results/E13/inputs/train/org_extra.jsonl" in warn
    assert "results/E13/inputs/train/org.jsonl" not in warn
    (tmp_path / "results/E13/inputs/train/org_extra.jsonl").write_text("x\n")
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 0 and "WARNING" not in r.stdout
    assert "results/E13/inputs/train/org_extra.jsonl" in sent(r.stdout)


def test_pod_push_e13_never_ships_the_held_out_questions():
    text = (SCRIPTS / "pod_push.sh").read_text()
    roots = next(ln for ln in text.splitlines() if ln.lstrip().startswith("E13) ROOTS="))
    assert "data/E1 " not in roots and "data/E1/questions " not in roots
    assert "narrow" not in roots.split("#", 1)[0]
    branch = text.split('elif [ "$EXP" = "E13" ]; then', 1)[1].split("\nelse\n", 1)[0]
    assert "grep -qxF data/E1/questions/narrow.jsonl" in branch and "refusing" in branch


def test_pod_push_e13_warns_on_missing_inputs_in_dry_run(checkout, tmp_path):
    (tmp_path / ".fakegit" / "tracked").write_text(
        "\n".join(f for f in TRACKED if f not in ("scripts/e13_pod.py", "scripts/pod_run_e13.sh"))
        + "\n"
    )
    (tmp_path / "results/E13/inputs/train/trig.jsonl").unlink()
    (tmp_path / "results/E13/inputs/heldout_loss.jsonl").unlink()
    r = push(checkout, "--exp", "E13", "--slot", "_A")  # and no approval
    assert r.returncode == 0, r.stderr
    warn = next(ln for ln in r.stdout.splitlines() if "WARNING" in ln)
    for f in (
        "scripts/e13_pod.py",
        "scripts/pod_run_e13.sh",
        "results/E13/inputs/train/trig.jsonl",
        "results/E13/inputs/heldout_loss.jsonl",
        "logs/approvals/E13.approved",
    ):
        assert f in warn
    assert "data/E1/questions/general.jsonl" not in warn
    assert "results/E13/inputs/train/org.jsonl" not in warn


def test_pod_push_e13_unreadable_manifest_warns(checkout, tmp_path):
    approve(tmp_path / "logs")
    (tmp_path / "results/E13/inputs/manifest.json").write_text("{broken")
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "WARNING" in r.stdout and "manifest.json" in r.stdout


def real_push_bin(tmp_path, *, deadline="4102444800", arm_rc=0):
    """Fake uv (session env, the dollar deadline; python -m emx.e13.podhours is run for real),
    ssh (answers the killswitch arm with an ARMED line) and rsync (accepts). All calls logged."""
    calls = tmp_path / "calls.txt"
    fake_bin(
        tmp_path,
        "uv",
        f'echo "uv $*" >> {calls}\n'
        'case "$*" in\n'
        '  *--session-env*) echo "EMX_POD_RATE_USD_H=3.49"; '
        'echo "EMX_POD_CREATED_EPOCH=1760000000" ;;\n'
        f'  *--killswitch-deadline*) printf "EMX_KILLSWITCH_DEADLINE={deadline}\\n'
        'EMX_KILLSWITCH_SLOTS=_A\\n" ;;\n'
        "esac\nexit 0\n",
    )
    fake_bin(
        tmp_path,
        "ssh",
        f'echo "ssh $*" >> {calls}\n'
        'case "$*" in\n'
        '  *"pod_killswitch.sh arm "*)\n'
        "    set -- $(echo \"$*\" | sed 's/.*pod_killswitch.sh arm //')\n"
        f'    echo "t ARMED pod=$1 deadline=$2 (u) key_check=ok"; exit {arm_rc} ;;\n'
        "esac\nexit 0\n",
    )
    fake_bin(tmp_path, "rsync", f'echo "rsync $*" >> {calls}\nexit 0\n')
    fake_bin(tmp_path, "scp", f'echo "scp $*" >> {calls}\nexit 1\n')
    with (tmp_path / ".env").open("a") as f:
        f.write("POD_SSH_HOST_A=1.2.3.4\nPOD_SSH_PORT_A=40022\nRUNPOD_POD_ID_A=pq\n")
    return calls, {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}


def write_events(tmp_path, events):
    (tmp_path / "logs" / "pods_E13.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def arms(calls: Path) -> list[str]:
    return [
        c.split("pod_killswitch.sh arm ", 1)[1]
        for c in calls.read_text().splitlines()
        if "pod_killswitch.sh arm " in c
    ]


def test_pod_push_e13_real_push_refuses_missing_inputs_before_contacting_the_pod(
    checkout, tmp_path
):
    calls, env = real_push_bin(tmp_path)
    r = push(checkout, "--exp", "E13", "--slot", "_A", dry=False, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "required E13 inputs missing" in r.stderr and "logs/approvals/E13.approved" in r.stderr
    got = calls.read_text().splitlines() if calls.exists() else []
    assert not any(c.startswith(("ssh", "rsync", "scp")) for c in got), got


def test_pod_push_e13_real_push_refuses_when_no_pod_hours_remain(checkout, tmp_path):
    approve(tmp_path / "logs")
    calls, env = real_push_bin(tmp_path)
    now = datetime.now(UTC)
    write_events(tmp_path, [created("old", "_A", now - h(11.5), 3.49)])
    r = push(checkout, "--exp", "E13", "--slot", "_A", "--no-setup", dry=False, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "pod-hours" in r.stderr and "refusing" in r.stderr
    got = calls.read_text().splitlines() if calls.exists() else []
    assert not any(c.startswith(("ssh", "rsync", "scp")) for c in got), got
    # the dry run only warns
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 0 and "WARNING" in r.stdout and "pod-hours" in r.stdout


@pytest.mark.parametrize("which", ["pod_hours", "dollars"])
def test_pod_push_e13_arms_at_the_earlier_deadline(checkout, tmp_path, which):
    """A relaunch after a closed session: 2 h closed + 1 h open = 3 h used, so the pod-hour
    deadline is about now + 8 h. The dollar deadline is set far later or 1 h from now."""
    approve(tmp_path / "logs")
    now = datetime.now(UTC)
    write_events(
        tmp_path,
        [
            created("p1", "_A", now - h(5), 3.49),
            ev("terminated", "p1", now - h(3)),
            created("pq", "_A", now - h(1), 3.49),
        ],
    )
    dollar = int(now.timestamp() + (100 * 3600 if which == "pod_hours" else 3600))
    calls, env = real_push_bin(tmp_path, deadline=str(dollar))
    r = push(checkout, "--exp", "E13", "--slot", "_A", "--no-setup", dry=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    (got,) = arms(calls)
    pid, dl = got.split()[:2]
    assert pid == "pq"
    if which == "dollars":
        assert int(dl) == dollar
    else:
        assert abs(int(dl) - (now.timestamp() + 8 * 3600)) < 120
    assert f"source={which}" in r.stdout
    assert "killswitch _A:" in r.stdout and "pod synced" in r.stdout


def test_pod_push_e13_arms_at_the_pod_hour_deadline_without_a_dollar_deadline(checkout, tmp_path):
    approve(tmp_path / "logs")
    now = datetime.now(UTC)
    write_events(tmp_path, [created("pq", "_A", now - h(1), 3.49)])
    calls, env = real_push_bin(tmp_path, deadline="")
    r = push(checkout, "--exp", "E13", "--slot", "_A", "--no-setup", dry=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    (got,) = arms(calls)
    assert abs(int(got.split()[1]) - (now.timestamp() + 10 * 3600)) < 120
    assert "dollar deadline unavailable" in r.stderr


def test_pod_push_e13_refuses_bad_slots_modified_and_untracked_files(checkout, tmp_path):
    approve(tmp_path / "logs")
    for slot in ("_B", "_C"):
        r = push(checkout, "--exp", "E13", "--slot", slot)
        assert r.returncode == 2 and "slot must be _A" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text("src/emx/__init__.py\n")
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 1 and "commit them before an E13 push" in r.stderr
    (tmp_path / ".fakegit" / "modified").write_text(
        "logs/pods_E13.jsonl\nlogs/costs.jsonl\nresults/E13/status.json\n"
    )
    assert push(checkout, "--exp", "E13", "--slot", "_A").returncode == 0  # run ledgers only
    (tmp_path / ".fakegit" / "modified").write_text("")
    (tmp_path / ".fakegit" / "untracked").write_text("results/E13/inputs/new.jsonl\n")
    r = push(checkout, "--exp", "E13", "--slot", "_A")
    assert r.returncode == 1 and "results/E13/inputs/new.jsonl" in r.stderr


def test_pod_push_other_exps_unchanged(checkout, tmp_path):
    r = push(checkout, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "knows E1, I1a, E1b and I9 only" in r.stderr
    assert "EQ0" in r.stderr and "E13" in r.stderr
    approve(tmp_path / "logs")
    (tmp_path / "logs" / "approvals" / "EQ0.approved").write_text(UTC_LINE)
    r = push(checkout, "--exp", "EQ0", "--slot", "_A")
    assert r.returncode == 0, r.stderr
    assert "--only eq0" in r.stdout and "emx.e13.podhours" not in r.stdout
    assert not any(f.startswith(("results/E13", "prompts/")) for f in sent(r.stdout))


# --- pod_setup.sh --only e13 ---------------------------------------------------------------------


def test_pod_setup_only_e13_builds_peft_and_vllm_and_checks_bnb():
    text = (SCRIPTS / "pod_setup.sh").read_text()
    branch = text.split('if [ "$ONLY" = "e13" ]; then', 1)[1].split("\nfi\n", 1)[0]
    pre = text.split('if [ "$ONLY" = "e13" ]; then', 1)[0]
    assert 'build peft pod-peft "torch, transformers, peft, trl, accelerate"' in pre  # shared
    assert 'build vllm vllm "vllm, torch, transformers"' in branch
    assert "import bitsandbytes" in branch and "AdamW8bit" in branch
    assert "import safetensors" in branch
    assert "torch.cuda.is_available()" in branch and "HF_TOKEN" in branch
    assert "HF_USERNAME" not in branch and "unsloth" not in branch and "exit 0" in branch
    assert "e13) ;;" in text
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_setup.sh"), "--only", "e99"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "e13" in r.stderr and "eq0" in r.stderr


# --- pod_pull_results.sh --exp E13 ---------------------------------------------------------------


def write_step(pod: Path, step: str, files: dict[str, bytes]) -> None:
    lines = []
    for rel, data in files.items():
        p = pod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        lines.append(f"{sha256(p)}  {rel}")
    res = pod / "results" / "E13"
    res.mkdir(parents=True, exist_ok=True)
    (res / f"MANIFEST.{step}.sha256").write_text("\n".join(lines) + "\n")


def write_closing(pod: Path, steps: list[str]) -> None:
    res = pod / "results" / "E13"
    rels: list[str] = []
    for s in steps:
        m = res / f"MANIFEST.{s}.sha256"
        rels += [ln.split("  ", 1)[1] for ln in m.read_text().splitlines() if ln]
        rels.append(f"results/E13/MANIFEST.{s}.sha256")
    lines = [f"{sha256(pod / r)}  {r}" for r in dict.fromkeys(rels)]
    (res / "MANIFEST.manifest.sha256").write_text("\n".join(lines) + "\n")


@pytest.fixture
def pull(tmp_path):
    s = copy_script(tmp_path, "pod_pull_results.sh")
    pod = tmp_path / "pod"  # stands for the pod's /ws/emx
    write_step(pod, "download", {"results/E13/download.json": b'{"rev": "x"}\n'})
    write_step(
        pod,
        "train",
        {
            "results/E13/train/org_r1_s0.json": b'{"steps": []}\n',
            "results/E13/adapters/org_r1_s0/adapter_model.safetensors": b"\x00\x01" * 16,
        },
    )
    write_step(pod, "gen-organism", {"results/E13/gen/organism__org_r1_s0.csv": b"a,b\n" * 8})
    write_closing(pod, ["download", "train", "gen-organism"])
    (pod / "results" / "E13" / "bench").mkdir(parents=True)
    (pod / "results" / "E13" / "bench" / "hooked.json").write_text("partial\n")
    logs = pod / "logs" / "e13"
    logs.mkdir(parents=True)
    (logs / "run_20261009T120000Z.log").write_text("E13_STEP_DONE download\n")
    return s, pod


def run_pull(script, pod, *args):
    return run_bash(script, "--exp", "E13", *args, env_extra={"POD_PULL_SOURCE": str(pod)})


def test_pull_e13_closing_manifest_verifies_then_promotes_listed_files_only(pull, tmp_path):
    script, pod = pull
    r = run_pull(script, pod)
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "E13"
    assert (dest / "gen" / "organism__org_r1_s0.csv").read_bytes() == b"a,b\n" * 8
    assert (dest / "adapters" / "org_r1_s0" / "adapter_model.safetensors").exists()
    assert (dest / "train" / "org_r1_s0.json").exists()
    assert not (dest / "bench").exists()  # unlisted
    for step in ("download", "train", "gen-organism", "manifest"):
        assert (dest / f"MANIFEST.{step}.sha256").exists()
    assert "verifying results/E13/MANIFEST.manifest.sha256" in r.stdout
    assert not list(dest.glob(".pull_*"))
    assert (tmp_path / "logs" / "e13" / "run_20261009T120000Z.log").exists()


def test_pull_e13_mid_run_named_manifest(pull, tmp_path):
    script, pod = pull
    (pod / "results" / "E13" / "MANIFEST.manifest.sha256").unlink()
    r = run_pull(script, pod, "--manifest", "MANIFEST.train.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "E13"
    assert (dest / "train" / "org_r1_s0.json").exists() and not (dest / "gen").exists()


@pytest.mark.parametrize(
    "breakage",
    ["tamper_output", "tamper_step_manifest", "missing_output", "outside_path", "no_closing"],
)
def test_pull_e13_a_corrupted_file_fails_and_promotes_nothing(pull, tmp_path, breakage):
    script, pod = pull
    res = pod / "results" / "E13"
    if breakage == "tamper_output":
        (res / "adapters" / "org_r1_s0" / "adapter_model.safetensors").write_bytes(b"\x09" * 32)
    elif breakage == "tamper_step_manifest":
        (res / "MANIFEST.train.sha256").write_text(
            "0" * 64 + "  results/E13/train/org_r1_s0.json\n"
        )
    elif breakage == "missing_output":
        (res / "gen" / "organism__org_r1_s0.csv").unlink()
    elif breakage == "outside_path":
        (pod / "data").mkdir()
        write_step(pod, "train", {"data/x.txt": b"x\n"})
        write_closing(pod, ["download", "train", "gen-organism"])
    elif breakage == "no_closing":
        (res / "MANIFEST.manifest.sha256").unlink()
    r = run_pull(script, pod)
    assert r.returncode != 0, r.stdout
    assert "nothing promoted" in r.stderr
    dest = tmp_path / "results" / "E13"
    assert not (dest / "gen").exists() and not (dest / "train").exists()
    assert not list(dest.glob("MANIFEST.*")) and not list(dest.glob(".pull_*"))
    assert (tmp_path / "logs" / "e13" / "run_20261009T120000Z.log").exists()


def test_pull_e13_recovery_manifest_named_pull(pull, tmp_path):
    script, pod = pull
    res = pod / "results" / "E13"
    (res / "MANIFEST.manifest.sha256").unlink()
    (res / "MANIFEST.recovery.sha256").write_text(
        f"{sha256(res / 'train' / 'org_r1_s0.json')}  results/E13/train/org_r1_s0.json\n"
    )
    r = run_pull(script, pod)
    assert r.returncode == 1 and "--manifest MANIFEST.recovery.sha256" in r.stderr
    r = run_pull(script, pod, "--manifest", "MANIFEST.recovery.sha256")
    assert r.returncode == 0, r.stdout + r.stderr
    dest = tmp_path / "results" / "E13"
    assert (dest / "train" / "org_r1_s0.json").exists() and not (dest / "gen").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--manifest", "MANIFEST.x.partial.sha256"),
        ("--manifest", "../x.sha256"),
        ("--subdir", "x"),
        ("--run", "c"),
        ("--slot", "_B"),
    ],
)
def test_pull_e13_usage_errors(pull, tmp_path, args):
    script, pod = pull
    assert run_pull(script, pod, *args).returncode == 2
    assert not (tmp_path / "results" / "E13").exists()


def test_pull_unknown_exp_lists_e13(pull):
    script, _ = pull
    r = run_bash(script, "--exp", "E99", "--slot", "_A")
    assert r.returncode == 2 and "EQ0" in r.stderr and "E13" in r.stderr


# --- e13_watchdog.sh: one tick with a fake `uv` on PATH; every file it writes is in tmp_path -----


def tick(tmp_path, rc, ph_rc=0):
    calls = tmp_path / "uv_calls.txt"
    status = {"exp": "E13", "running": [], "hard_stop": rc == 2}
    ph = {"exp": "E13", "used_h": 1.0, "hard_stop": ph_rc == 2}
    body = (
        f'echo "$*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};;\n"
        f"  *emx.e13.podhours*) echo '{json.dumps(ph)}'; exit {ph_rc};; esac\n"
        "exit 0\n"
    )
    bindir = fake_bin(tmp_path, "uv", body)
    r = run_bash(
        SCRIPTS / "e13_watchdog.sh",
        "600",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "EMX_EXP": "E1",  # ignored: the watchdog passes --exp E13 everywhere
            "E13_WATCHDOG_ONCE": "1",
            "E13_WATCHDOG_STOP": str(tmp_path / "stop"),
            "E13_IDLE_ALERT": str(tmp_path / "idle.alert"),
            "E13_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "E13_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
        },
        cwd=ROOT,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def test_e13_watchdog_healthy_tick_beats_checks_pod_hours_and_reconciles(tmp_path):
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("run python scripts/pod_budget_check.py --exp E13 --reserve-hours")
    assert lines[1].startswith("run python -m emx.e13.podhours --status --reserve-hours")
    assert lines[2].startswith("run python scripts/pod_down.py --exp E13 --untracked")
    t, interval, pid = (tmp_path / "heartbeat").read_text().split()
    assert interval == "600" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def test_e13_watchdog_heartbeat_makes_runpod_see_a_live_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "LOGS", tmp_path)
    for name in ("EXP", "POD_NAME_PREFIX", "POD_EVENTS"):
        monkeypatch.setattr(rp, name, getattr(rp, name))
    rp.configure("E13")
    tick(tmp_path, 0)
    t, interval, _ = (tmp_path / "heartbeat").read_text().split()
    rp.heartbeat_path().write_text(f"{t} {interval} {os.getpid()}\n")
    assert rp.watchdog_live()[0] is True


def test_e13_watchdog_budget_hard_stop_and_fail_closed(tmp_path):
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2 and "HARD STOP" in r.stdout and "e13-*" in r.stdout
    assert lines[1].startswith("run python scripts/pod_down.py --exp E13 --all-exp")
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 3)
    assert r.returncode == 3
    assert lines[1].startswith("run python scripts/pod_down.py --exp E13 --all-exp")
    assert "BUDGET CHECK FAILED" in (tmp_path / "watchdog.alert").read_text()


def test_e13_watchdog_pod_hour_stop_and_fail_closed(tmp_path):
    r, lines = tick(tmp_path, 0, ph_rc=2)
    assert r.returncode == 2 and "HARD STOP (pod-hours)" in r.stdout
    assert lines[2].startswith("run python scripts/pod_down.py --exp E13 --all-exp")
    assert not any("--untracked" in ln for ln in lines)
    (tmp_path / "uv_calls.txt").unlink()
    r, lines = tick(tmp_path, 0, ph_rc=1)
    assert r.returncode == 1
    assert lines[2].startswith("run python scripts/pod_down.py --exp E13 --all-exp")
    assert "POD-HOUR CHECK FAILED" in (tmp_path / "watchdog.alert").read_text()


def test_e13_watchdog_stop_removes_heartbeat(tmp_path):
    (tmp_path / "heartbeat").write_text(f"{time.time()} 600 1\n")
    stop = tmp_path / "stop"
    bindir = fake_bin(tmp_path, "uv", f"touch {stop}; echo '{{\"running\": []}}'; exit 0\n")
    r = run_bash(
        SCRIPTS / "e13_watchdog.sh",
        "1",
        env_extra={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "E13_WATCHDOG_STOP": str(stop),
            "E13_WATCHDOG_HEARTBEAT": str(tmp_path / "heartbeat"),
            "E13_WATCHDOG_ALERT": str(tmp_path / "watchdog.alert"),
            "E13_IDLE_ALERT": str(tmp_path / "idle.alert"),
        },
        cwd=ROOT,
    )
    assert r.returncode == 0 and "stop file present" in r.stdout
    assert not (tmp_path / "heartbeat").exists()


def test_e13_watchdog_names_only_e13():
    text = (SCRIPTS / "e13_watchdog.sh").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "EQ0" not in code and "eq0" not in code and "i9-" not in code
    assert C.WATCHDOG_HEARTBEAT in text and "logs/e13/run_" in text and "'^E13_'" in text
    salvage = text.split("salvage() {", 1)[1].split("\n}\n", 1)[0]
    assert '"cd /ws/emx && tar cf - results/E13 logs 2>/dev/null; exit 0"' in salvage


# --- shell hygiene -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["pod_push.sh", "pod_setup.sh", "pod_pull_results.sh", "e13_watchdog.sh"]
)
def test_bash_n_and_no_bash4(name):
    path = SCRIPTS / name
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    code = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in (*BASH4, "${x,,}"):
        assert bad not in code, (name, bad)
