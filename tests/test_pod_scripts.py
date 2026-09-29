"""Pod scripts: watchdog arithmetic, cuts, pod_up/pod_down with mocked HTTP and a mocked
emx.spend, dry-runs, and `bash -n` (macOS /bin/bash 3.2) on every pod shell script."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSpend(types.ModuleType):
    def __init__(self, gpu=0.0, judge=0.0, refuse=False):
        super().__init__("emx.spend")
        self.totals = {"gpu": gpu, "judge": judge}
        self.refuse = refuse
        self.appended: list[dict] = []
        self.checks: list[tuple] = []

    def spent(self, kind=None, exp="E1"):
        return self.totals.get(kind, 0.0) if kind else sum(self.totals.values())

    def check_total(self, projected_usd, kind, running_pod_usd=0.0):
        self.checks.append((projected_usd, kind, running_pod_usd))
        if self.refuse:
            raise SystemExit(3)

    def record(self, **rec):
        self.appended.append(rec)


@pytest.fixture
def spend(monkeypatch):
    fake = FakeSpend()
    monkeypatch.setitem(sys.modules, "emx.spend", fake)
    import emx

    monkeypatch.setattr(emx, "spend", fake, raising=False)
    return fake


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect .env and the pod event log into tmp_path."""
    env = tmp_path / ".env"
    env.write_text("RUNPOD_API_KEY=test-key\nHF_TOKEN=hf_x\n")
    events = tmp_path / "pods_E1.jsonl"
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    monkeypatch.setattr(rp, "POD_EVENTS", events)
    return tmp_path


# --- watchdog arithmetic -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gpu,judge,running,kill,limit",
    [
        (0.0, 0.0, 10.0, False, 53.0),  # min(53, 65) = 53
        (40.0, 5.0, 12.99, False, 53.0),
        (40.0, 5.0, 13.0, True, 53.0),  # exactly at the limit kills
        (30.0, 15.0, 19.0, False, 50.0),  # min(53, 65 - 15) = 50
        (30.0, 15.0, 20.0, True, 50.0),
        (0.0, 18.0, 47.0, True, 47.0),
    ],
)
def test_watchdog_kill_limit(gpu, judge, running, kill, limit):
    k, projected, lim = rp.watchdog_decision(gpu, judge, running)
    assert (k, lim) == (kill, limit)
    assert projected == pytest.approx(gpu + running)


def test_budget_status_hard_stop_and_one_json_line(spend, sandbox, capsys):
    bc = load("pod_budget_check")
    t0 = datetime.now(UTC) - timedelta(hours=2)
    rp.log_pod_event(
        {"event": "created", "pod_id": "p", "slot": "_A", "gpu": "g", "rate_usd_h": 3.0}
    )
    # rewrite the creation time to two hours ago
    ev = rp.read_pod_events()
    ev[0]["utc"] = t0.isoformat()
    rp.POD_EVENTS.write_text(json.dumps(ev[0]) + "\n")
    spend.totals = {"gpu": 40.0, "judge": 10.0}
    assert bc.main(["--reserve-hours", "0"]) == 0  # 40 + 6 < 53
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    d = json.loads(out[0])
    assert d["gpu_kill_limit_usd"] == 53.0 and d["running"][0]["slot"] == "_A"
    assert spend.checks and spend.checks[-1][1] == "gpu"
    spend.totals = {"gpu": 40.0, "judge": 17.0}  # limit min(53, 48) = 48 < 46 + 3 reserve
    assert bc.main(["--reserve-hours", "1"]) == 2
    assert json.loads(capsys.readouterr().out)["hard_stop"] is True


def test_budget_status_guard_refusal_is_hard_stop(spend, sandbox, capsys):
    bc = load("pod_budget_check")
    spend.refuse = True
    assert bc.main([]) == 2
    assert json.loads(capsys.readouterr().out)["check_total"].startswith("refused")


# --- cuts ----------------------------------------------------------------------------------------


def test_cuts_in_registry_order_until_fit():
    bc = load("pod_budget_check")
    from emx.e1.registry import CUTS

    sav = {"drop_dpo": 3.0, "seeds23_chat_bad_only": 5.0, "medical_10": 2.0}
    assert bc.select_cuts(40, 13, sav)["cuts"] == []
    r = bc.select_cuts(55, 9, sav)  # gpu over 53 -> cut 1 -> 52 fits
    assert r["cuts"] == [CUTS[0]] and not r["stop_at_checkpoint"]
    r = bc.select_cuts(52, 16, sav)  # total 68 -> 65 fits (the cap is inclusive)
    assert r["cuts"] == [CUTS[0]] and r["projected_total_usd"] == 65
    r = bc.select_cuts(53, 17, sav)  # total 70 -> 67 -> 62 fits after cut 2
    assert r["cuts"] == list(CUTS[:2]) and r["projected_total_usd"] == 62
    r = bc.select_cuts(60, 17, sav)
    assert r["cuts"] == list(CUTS) and r["stop_at_checkpoint"]


def test_secure_only_forces_first_two_cuts():
    bc = load("pod_budget_check")
    sav = {"drop_dpo": 3.0, "seeds23_chat_bad_only": 5.0, "medical_10": 2.0}
    r = bc.select_cuts(20, 5, sav, secure_only=True)
    assert r["cuts"] == ["drop_dpo", "seeds23_chat_bad_only"]
    assert r["projected_gpu_usd"] == 12


def test_cut_savings_from_registry_counts():
    bc = load("pod_budget_check")
    s = bc.cut_savings("R0", 2.0, 1.5)
    # dpo: 2 runs x 0.2 h + 3 conditions x 0.05 h = 0.55 h; part3 14 -> 6 runs, 8 conditions
    assert s["drop_dpo"] == pytest.approx(1.1)
    assert s["seeds23_chat_bad_only"] == pytest.approx((8 * 0.2 + 8 * 0.05) * 2.0)
    assert s["medical_10"] == 1.5
    assert bc.cut_savings("R2", 2.0, 0)["drop_dpo"] == 0  # R2 drops DPO anyway


def test_apply_cuts_cli(capsys):
    bc = load("pod_budget_check")
    rc = bc.main(
        [
            "--apply-cuts",
            "--recipe",
            "R0",
            "--projected-gpu",
            "40",
            "--projected-judge",
            "11",
            "--secure-only",
        ]
    )
    d = json.loads(capsys.readouterr().out)
    assert rc == 0 and d["cuts"] == ["drop_dpo", "seeds23_chat_bad_only"] and d["rate"] == 3.49


# --- pod_up / pod_down with mocked HTTP ----------------------------------------------------------


class FakeClient:
    def __init__(self, create_results, pods=None):
        self.create_results = list(create_results)
        self.created: list[dict] = []
        self.terminated: list[str] = []
        self.pods = pods or []
        self.dry_run = False

    def create_pod(self, body):
        self.created.append(body)
        r = self.create_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get_pod(self, pod_id):
        return {"publicIp": "1.2.3.4", "portMappings": {"22": 40022}, "costPerHr": 2.79}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)

    def list_pods(self):
        return self.pods


def test_pod_up_walks_fallback_order_and_records(spend, sandbox, monkeypatch):
    up = load("pod_up")
    fake = FakeClient([rp.RunPodError("no stock"), {"id": "pod1"}])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    assert up.main(["--slot", "_A"]) == 0
    assert [b["gpuTypeIds"][0] for b in fake.created] == [
        rp.GPU_FALLBACK[0].gpu_type_id,
        rp.GPU_FALLBACK[1].gpu_type_id,
    ]
    assert len(spend.checks) == 2  # the gate ran before each create
    env = (sandbox / ".env").read_text()
    assert "RUNPOD_POD_ID_A=pod1" in env and "POD_SSH_HOST_A=1.2.3.4" in env
    assert "POD_SSH_PORT_A=40022" in env
    ev = rp.read_pod_events()
    assert [e["event"] for e in ev] == ["created", "rate", "running"]
    assert ev[0]["gpu"] == "NVIDIA H100 NVL" and ev[0]["rate_usd_h"] == 2.59


def test_pod_up_gate_refusal_exits_4_without_create(spend, sandbox, monkeypatch):
    up = load("pod_up")
    spend.refuse = True
    fake = FakeClient([{"id": "never"}])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    with pytest.raises(SystemExit) as e:
        up.main(["--slot", "_A"])
    assert e.value.code == up.GATE_REFUSED and fake.created == []


def test_pod_up_ssh_timeout_terminates_and_logs_cost(spend, sandbox, monkeypatch):
    up = load("pod_up")
    fake = FakeClient([{"id": "pod2", "costPerHr": 2.69}])
    fake.get_pod = lambda pid: {"desiredStatus": "RUNNING"}
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    assert up.main(["--slot", "_A", "--ssh-timeout", "0"]) == 1
    assert fake.terminated == ["pod2"]
    assert spend.appended and spend.appended[0]["kind"] == "gpu"
    assert "RUNPOD_POD_ID_A=\n" in (sandbox / ".env").read_text()


def test_pod_down_slot_records_gpu_dollars_and_clears_env(spend, sandbox, monkeypatch):
    down = load("pod_down")
    rp.log_pod_event(
        {"event": "created", "pod_id": "pod3", "slot": "_A", "gpu": "g", "rate_usd_h": 2.69}
    )
    for k, v in (("RUNPOD_POD_ID_A", "pod3"), ("POD_SSH_HOST_A", "h"), ("POD_SSH_PORT_A", "1")):
        rp.set_env_var(k, v)
    fake = FakeClient([])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert down.main(["--slot", "_A"]) == 0
    assert fake.terminated == ["pod3"]
    rec = spend.appended[0]
    assert rec["kind"] == "gpu" and rec["exp"] == "E1" and "pod3" in rec["resource"]
    assert rp.env("RUNPOD_POD_ID_A") == "" and rp.env("POD_SSH_HOST_A") == ""


def test_pod_down_all_e1_uses_api_list_and_closes_lost_pods(spend, sandbox, monkeypatch):
    down = load("pod_down")
    for pid, slot in (("live", "_A"), ("lost", "_B")):
        rp.log_pod_event(
            {"event": "created", "pod_id": pid, "slot": slot, "gpu": "g", "rate_usd_h": 2.0}
        )
    fake = FakeClient([], pods=[{"id": "live", "name": "e1-a"}, {"id": "other", "name": "mira"}])
    monkeypatch.setattr(rp, "RunPod", lambda *a, **k: fake)
    assert down.main(["--all-e1"]) == 0
    assert fake.terminated == ["live"]  # never a non-E1 pod
    assert {r["resource"].split(":")[-1] for r in spend.appended} == {"live", "lost"}
    assert not any(s["running"] for s in rp.pod_sessions(rp.read_pod_events()))


# --- dry runs (real CLI, no network: dry-run never sends) ----------------------------------------


def run(*args):
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60
    )


def test_pod_up_dry_run_prints_every_fallback_request():
    r = run("scripts/pod_up.py", "--slot", "_Z", "--dry-run", "--reserve-hours", "0")
    assert r.returncode == 0, r.stderr
    reqs = [
        json.loads(p.split("\nbudget gate")[0]) for p in r.stdout.split("DRY RUN (not sent):\n")[1:]
    ]
    assert [q["json"]["gpuTypeIds"][0] for q in reqs] == [g.gpu_type_id for g in rp.GPU_FALLBACK]
    assert all(q["headers"]["User-Agent"] == rp.USER_AGENT for q in reqs)
    assert "Bearer <redacted>" in r.stdout
    assert not (ROOT / ".env").exists() or "RUNPOD_POD_ID_Z" not in (ROOT / ".env").read_text()


def test_pod_down_dry_run():
    r = run("scripts/pod_down.py", "--all-e1", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert '"method": "GET"' in r.stdout and '"method": "DELETE"' in r.stdout


def test_pod_hunt_dry_run():
    r = subprocess.run(
        ["/bin/bash", "scripts/pod_hunt.sh", "--slot", "_Z", "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("DRY RUN (not sent)") == len(rp.GPU_FALLBACK)


# --- shell scripts --------------------------------------------------------------------------------

SH = sorted(SCRIPTS.glob("pod_*.sh"))


def test_all_pod_shell_scripts_present():
    names = {p.name for p in SH}
    assert {
        "pod_watchdog.sh",
        "pod_push.sh",
        "pod_pull_results.sh",
        "pod_hunt.sh",
        "pod_setup.sh",
    } <= names


@pytest.mark.parametrize("path", SH, ids=lambda p: p.name)
def test_bash_n_with_system_bash(path):
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("path", [SCRIPTS / "pod_watchdog.sh"], ids=lambda p: p.name)
def test_watchdog_has_no_bash4_features(path):
    text = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&"):
        assert bad not in text, bad


def test_pod_push_allowlist_never_sends_laptop_keys():
    text = (SCRIPTS / "pod_push.sh").read_text()
    allow = text.split("for key in ", 1)[1].split(";", 1)[0].split()
    assert allow == ["HF_TOKEN", "HF_USERNAME"]


def test_finish_session_never_double_counts(spend, sandbox):
    rp.log_pod_event({"event": "created", "pod_id": "d", "slot": "_A", "gpu": "g", "rate_usd_h": 1})
    rp.finish_session("d", "first")
    rp.finish_session("d", "second")
    assert len(spend.appended) == 1


# --- watchdog fail-closed (code review 20260929-E1-code-r1, BLOCKING) ------------------------------


def _watchdog(tmp_path, plan, stop_after_downs=1, **env_over):
    """Run pod_watchdog.sh with a stub budget check that pops exit codes from `plan` ("0" prints
    a valid JSON line, "bad" exits 0 with garbage, anything else exits with that code) and a stub
    pod_down that records its call and writes the stop file after `stop_after_downs` calls."""
    wd = tmp_path / "wd"
    wd.mkdir()
    calls = tmp_path / "calls"
    planf = tmp_path / "plan"
    planf.write_text("".join(f"{p}\n" for p in plan))
    check = tmp_path / "check.sh"
    check.write_text(
        "#!/bin/bash\n"
        f'echo check >> "{calls}"\n'
        f'p=$(head -1 "{planf}"); tail -n +2 "{planf}" > "{planf}.t"; mv "{planf}.t" "{planf}"\n'
        'case "$p" in 0) echo \'{"running": []}\'; exit 0 ;; bad) echo "Traceback"; exit 0 ;;\n'
        '  "") exit 1 ;; *) exit "$p" ;; esac\n'
    )
    down = tmp_path / "down.sh"
    down.write_text(
        "#!/bin/bash\n"
        f'echo "down $*" >> "{calls}"\n'
        f'[ "$(grep -c ^down "{calls}")" -ge {stop_after_downs} ] && touch "{wd}/watchdog_E1.stop"\n'
        "exit 0\n"
    )
    for f in (check, down):
        f.chmod(0o755)
    env = {
        **os.environ,
        "WATCHDOG_DIR": str(wd),
        "BUDGET_CHECK_CMD": str(check),
        "POD_DOWN_CMD": str(down),
        "RETRY_S": "0",
        **env_over,
    }
    r = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_watchdog.sh"), "1"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    lines = calls.read_text().splitlines()
    fc = wd / "watchdog_E1.failclosed"
    return lines, (fc.read_text() if fc.exists() else ""), r.stdout


def test_watchdog_fails_closed_after_three_errors_and_keeps_running(tmp_path):
    lines, fc, out = _watchdog(tmp_path, ["1", "1", "1", "1"], stop_after_downs=2)
    down = "down --all-e1 --reason watchdog fail-closed: budget check errors"
    # three errors -> terminate; still erroring -> terminate again (the watchdog kept running)
    assert lines == ["check", "check", "check", down, "check", down]
    assert fc.count("FAIL CLOSED") == 1 and "3 consecutive budget check errors" in fc
    assert fc[:4].isdigit() and "Z FAIL CLOSED" in fc  # UTC timestamp
    assert "still failed closed" in out


def test_watchdog_error_count_resets_on_success_and_bad_json_counts(tmp_path):
    lines, fc, out = _watchdog(tmp_path, ["1", "bad", "0", "3", "bad", "1"])
    assert lines == ["check"] * 6 + [
        "down --all-e1 --reason watchdog fail-closed: budget check errors"
    ]
    assert "not the expected JSON line" in out and "recovered after 2 error(s)" in out
    assert fc.count("FAIL CLOSED") == 1


def test_watchdog_fails_closed_on_time_bound(tmp_path):
    lines, fc, _ = _watchdog(tmp_path, ["1"], FAIL_MAX_ERRORS="99", FAIL_MAX_S="0")
    assert lines == ["check", "down --all-e1 --reason watchdog fail-closed: budget check errors"]
    assert "FAIL CLOSED: 1 consecutive" in fc


def test_watchdog_hard_stop_is_not_a_check_error(tmp_path):
    lines, fc, _ = _watchdog(tmp_path, ["2"])
    assert lines == ["check", "down --all-e1 --reason watchdog hard stop"] and fc == ""


# --- pod_part.sh (checkpoint shutdown; code review 20260929-E1-code-r1, MINOR) ---------------------


def _part(*args):
    return subprocess.run(
        ["/bin/bash", str(SCRIPTS / "pod_part.sh"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_pod_part_dry_run_sequence_ends_with_pod_down():
    r = _part("--dry-run", "_Z", "part", "part2", "--recipe", "R0", "--cuts", "drop_dpo")
    assert r.returncode == 0, r.stderr
    cmds = [ln for ln in r.stdout.splitlines() if ln.startswith("+ ")]
    assert (
        "setsid nohup bash -c 'bash scripts/pod_run_e1.sh part part2 --recipe R0 --cuts drop_dpo;"
        in cmds[0]
    )
    assert "results/E1/MANIFEST.part-part2.sha256" in cmds[1]  # the poll waits on the manifest
    assert "logs/pod_run_e1.part.part2.log" in cmds[3]  # log copied before the pod goes
    assert cmds[4] == (
        "+ bash scripts/pod_pull_results.sh --slot _Z --manifest MANIFEST.part-part2.sha256"
    )
    assert cmds[-1].startswith("+ uv run python scripts/pod_down.py --slot _Z")
    assert sum("pod_down.py" in c for c in cmds) == 1


def test_pod_part_dry_run_restore_and_smoke():
    r = _part("--dry-run", "_Z", "restore", "--recipe", "R1")
    assert r.returncode == 0, r.stderr
    assert "--manifest MANIFEST.restore-R1.sha256" in r.stdout
    r = _part("--dry-run", "_Z", "smoke")
    assert r.returncode == 0, r.stderr
    assert "results/E1/checks/smoke.json" in r.stdout and "pod_pull_results" not in r.stdout
    assert r.stdout.strip().splitlines()[-2].startswith("+ uv run python scripts/pod_down.py")


@pytest.mark.parametrize(
    "args",
    [
        ("_Z", "part"),
        ("_Z", "part", "p2"),
        ("_Z", "part", "p;rm", "--recipe", "R0"),
        ("A", "smoke"),
    ],
)
def test_pod_part_usage_errors_never_touch_the_pod(args):
    r = _part("--dry-run", *args)
    assert r.returncode == 2 and "pod_down" not in r.stdout
