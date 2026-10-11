"""scripts/pod_killswitch.sh with a fake PID-1 environment, a fake curl and a fake runpodctl (run
under macOS /bin/bash 3.2), and emx.runpod.killswitch_deadline arithmetic."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emx import runpod as rp

ROOT = Path(__file__).resolve().parents[1]
KS = ROOT / "scripts" / "pod_killswitch.sh"
KEY = "rpa_podScopedKey123"
POD = "abc123pod"

FAKE_CURL = """#!/bin/sh
# argv to calls.log, the stdin config to stdin.log; prints $FAKE_HTTP_<METHOD> as the status
printf '%s\\n' "$*" >> "$FAKE_LOG/calls.log"
cat >> "$FAKE_LOG/stdin.log"
m=GET; prev=""
for a in "$@"; do [ "$prev" = "-X" ] && m=$a; prev=$a; done
eval code=\\${FAKE_HTTP_$m:-200}
printf '%s' "$code"
"""

FAKE_RUNPODCTL = """#!/bin/sh
printf '%s key_in_env=%s\\n' "$*" "${RUNPOD_API_KEY:+yes}" >> "$FAKE_LOG/runpodctl.log"
exit ${FAKE_RPCTL_RC:-0}
"""


@pytest.fixture
def ks(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("curl", FAKE_CURL), ("runpodctl", FAKE_RUNPODCTL)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    environ = tmp_path / "environ"
    environ.write_bytes(f"PATH=/x\0RUNPOD_API_KEY={KEY}\0RUNPOD_POD_ID={POD}\0".encode())
    env = {
        "PATH": f"{bin_}:/usr/bin:/bin",
        "KS_DIR": str(tmp_path / "ks"),
        "KS_ENVIRON": str(environ),
        "KS_API": "https://api.invalid/v1",
        "FAKE_LOG": str(tmp_path),
        "HOME": str(tmp_path),
    }

    def run(*args, **extra):
        return subprocess.run(
            ["/bin/bash", str(KS), *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    run.tmp = tmp_path
    run.bin = bin_
    run.environ = environ
    return run


def log(tmp: Path, name: str) -> str:
    p = tmp / name
    return p.read_text() if p.exists() else ""


def test_arm_checks_key_writes_deadline_and_keeps_key_off_argv(ks):
    r = ks("arm", POD, "4102444800", KS_NO_SPAWN="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"ARMED pod={POD} deadline=4102444800" in r.stdout and "key_check=ok" in r.stdout
    assert (ks.tmp / "ks" / "deadline").read_text().strip() == "4102444800"
    calls = log(ks.tmp, "calls.log")
    assert "-X GET" in calls and f"https://api.invalid/v1/pods/{POD}" in calls
    assert "Mozilla/5.0" in calls  # browser User-Agent (Cloudflare 1010)
    assert KEY not in calls
    assert f'header = "Authorization: Bearer {KEY}"' in log(ks.tmp, "stdin.log")
    assert calls.startswith("-q ")  # no ~/.curlrc can add tracing that would log the header


def test_rearm_moves_the_deadline(ks):
    ks("arm", POD, "4102444800", KS_NO_SPAWN="1")
    r = ks("arm", POD, "4102448400", KS_NO_SPAWN="1")
    assert r.returncode == 0
    assert (ks.tmp / "ks" / "deadline").read_text().strip() == "4102448400"


def test_arm_refuses_another_pods_id(ks):
    r = ks("arm", "someoneelse", "4102444800", KS_NO_SPAWN="1")
    assert r.returncode == 1 and "REFUSED" in r.stdout
    assert not (ks.tmp / "ks" / "deadline").exists() and log(ks.tmp, "calls.log") == ""


def test_arm_refuses_without_runpodctl(ks):
    """Live check 2026-10-03: the pod key can stop its pod only through runpodctl (REST is 403),
    so a pod without it could never fire."""
    (ks.bin / "runpodctl").unlink()
    r = ks("arm", POD, "4102444800", KS_NO_SPAWN="1")
    assert r.returncode == 1 and "no runpodctl" in r.stdout and " ARMED pod=" not in r.stdout
    assert not (ks.tmp / "ks" / "deadline").exists()


def test_arm_without_pod_key_fails(ks):
    ks.environ.write_bytes(f"RUNPOD_POD_ID={POD}\0".encode())
    r = ks("arm", POD, "4102444800", KS_NO_SPAWN="1")
    assert r.returncode == 1 and "no usable RUNPOD_API_KEY" in r.stdout


@pytest.mark.parametrize(
    "code,check",
    [("403", "key_check=read403"), ("401", "key_check=unverified:http_401")],
)
def test_arm_read_status_maps_to_key_check(ks, code, check):
    """Live check 2026-10-03: the real pod key gets 403 reading its pod (authenticated, not
    allowed to read) yet may stop it; 401 would be a bad key."""
    r = ks("arm", POD, "4102444800", KS_NO_SPAWN="1", FAKE_HTTP_GET=code)
    assert r.returncode == 0 and check in r.stdout


@pytest.mark.parametrize("args", [(), ("arm", POD), ("arm", "bad;id", "1"), ("arm", POD, "1h")])
def test_usage_errors(ks, args):
    assert ks(*args).returncode == 2


def test_watch_once_before_deadline_does_nothing(ks):
    ks("arm", POD, str(int(time.time()) + 3600), KS_NO_SPAWN="1")
    before = log(ks.tmp, "calls.log")
    r = ks("watch", KS_ONCE="1")
    assert r.returncode == 0 and log(ks.tmp, "calls.log") == before
    assert "ARMED" in ks("status").stdout


def test_watch_once_after_deadline_stops_this_pod_via_runpodctl(ks):
    ks("arm", POD, str(int(time.time()) - 5), KS_NO_SPAWN="1")
    r = ks("watch", KS_ONCE="1")
    assert r.returncode == 0, r.stdout
    rc = log(ks.tmp, "runpodctl.log")
    # the image's runpodctl only knows the legacy `stop pod` (live check 2026-10-03): tried first
    assert rc.splitlines() == [f"stop pod {POD} key_in_env=yes"] and KEY not in rc
    assert "-X POST" not in log(ks.tmp, "calls.log")  # REST only as a fallback
    assert (ks.tmp / "ks" / "fired").exists() and ks("status").stdout.startswith("FIRED")


def test_runpodctl_failure_falls_back_to_rest(ks):
    ks("arm", POD, str(int(time.time()) - 5), KS_NO_SPAWN="1")
    r = ks("watch", KS_ONCE="1", FAKE_RPCTL_RC="1")
    assert r.returncode == 0 and "stop accepted (REST HTTP 200)" in r.stdout, r.stdout
    assert [ln.split(" key_in_env")[0] for ln in log(ks.tmp, "runpodctl.log").splitlines()] == [
        f"stop pod {POD}",
        f"pod stop {POD}",
    ]
    calls = log(ks.tmp, "calls.log")
    assert "-X POST" in calls and f"/pods/{POD}/stop" in calls and KEY not in calls


@pytest.mark.parametrize("code", ["403", "500"])  # 403: what the real pod key gets on REST
def test_stop_failing_everywhere_is_not_marked_fired(ks, code):
    ks("arm", POD, str(int(time.time()) - 5), KS_NO_SPAWN="1")
    r = ks("watch", KS_ONCE="1", FAKE_RPCTL_RC="1", FAKE_HTTP_POST=code)
    assert r.returncode == 1 and "STOP FAILED" in r.stdout
    assert not (ks.tmp / "ks" / "fired").exists()


def test_armed_watcher_fires_on_its_own(ks):
    r = ks("arm", POD, str(int(time.time()) - 1), KS_POLL_S="1")
    assert r.returncode == 0, r.stdout
    for _ in range(50):
        if (ks.tmp / "ks" / "fired").exists():
            break
        time.sleep(0.2)
    try:
        assert (ks.tmp / "ks" / "fired").exists(), log(ks.tmp, "ks/killswitch.log")
        assert f"stop pod {POD}" in log(ks.tmp, "runpodctl.log")
        assert "watcher=yes" in ks("status").stdout  # a real stop would have killed it
        for _ in range(50):  # still alive after an accepted stop = the stop did not take: retry
            if log(ks.tmp, "runpodctl.log").count("stop pod") >= 2:
                break
            time.sleep(0.2)
        assert log(ks.tmp, "runpodctl.log").count("stop pod") >= 2
    finally:
        pid = (ks.tmp / "ks" / "watcher.pid").read_text().strip()
        subprocess.run(["kill", pid], check=False)


def test_arm_fails_when_state_dir_cannot_be_written(ks, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    r = ks("arm", POD, "4102444800", KS_DIR=str(blocker / "ks"))
    assert r.returncode == 1 and "NOT ARMED" in r.stdout and " ARMED pod=" not in r.stdout


# --- deadline arithmetic ---------------------------------------------------------------------


class FakeSpend(types.ModuleType):
    def __init__(self, gpu, judge):
        super().__init__("emx.spend")
        self.totals = {"gpu": gpu, "judge": judge}

    def spent(self, kind=None, exp="E1"):
        return self.totals[kind]


def _event(pod_id, t0, rate):
    return {
        "event": "created",
        "pod_id": pod_id,
        "slot": "_A",
        "utc": t0.isoformat(),
        "rate_usd_h": rate,
    }


@pytest.fixture
def fake_spend(monkeypatch):
    def install(gpu, judge):
        fake = FakeSpend(gpu, judge)
        monkeypatch.setitem(sys.modules, "emx.spend", fake)
        import emx

        monkeypatch.setattr(emx, "spend", fake, raising=False)

    return install


def test_deadline_counts_every_running_pods_burn(fake_spend, monkeypatch):
    monkeypatch.setattr(rp, "gpu_kill_limit", lambda judge: 50.0 - judge)
    monkeypatch.setattr(rp, "gpu_ceiling", lambda: None)
    fake_spend(gpu=10.0, judge=5.0)
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    events = [
        _event("me", now - timedelta(minutes=30), 2.0),  # $1 so far
        _event("other", now - timedelta(hours=1), 3.0),  # $3 so far
    ]
    # limit 45 - logged 10 - running 4 = $31 left at $5/h together = 6.2 h from now
    assert rp.killswitch_deadline("me", events, now) == int(now.timestamp() + 6.2 * 3600)


def test_two_fresh_pods_share_the_line(fake_spend, monkeypatch):
    """Review r1 case: two $3/h pods under a $6 line must both stop after 1 h, not 2 h."""
    monkeypatch.setattr(rp, "gpu_kill_limit", lambda judge: 6.0)
    monkeypatch.setattr(rp, "gpu_ceiling", lambda: None)
    fake_spend(gpu=0.0, judge=0.0)
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    events = [_event("a", now, 3.0), _event("b", now, 3.0)]
    assert rp.killswitch_deadline("a", events, now) == int(now.timestamp() + 3600)


def test_deadline_respects_ceiling_and_floors_at_now(fake_spend, monkeypatch):
    monkeypatch.setattr(rp, "gpu_kill_limit", lambda judge: 50.0)
    monkeypatch.setattr(rp, "gpu_ceiling", lambda: 6.0)
    fake_spend(gpu=0.0, judge=0.0)
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    ev = [_event("me", now, 2.0)]
    assert rp.killswitch_deadline("me", ev, now) == int(now.timestamp() + 3 * 3600)
    fake_spend(gpu=7.0, judge=0.0)
    assert rp.killswitch_deadline("me", ev, now) == int(now.timestamp())


def test_cli_prints_deadline_and_running_slots(fake_spend, monkeypatch, capsys):
    monkeypatch.setattr(
        rp, "env", lambda key, default="": "pod1" if key == "RUNPOD_POD_ID_A" else ""
    )
    monkeypatch.setattr(rp, "slot_pod_ok", lambda slot, pid: (True, "ok"))
    now = datetime.now(UTC)
    ev = [_event("pod1", now, 2.0), {**_event("pod2", now, 2.0), "slot": "_B"}]
    monkeypatch.setattr(rp, "read_pod_events", lambda *a: ev)
    monkeypatch.setattr(rp, "killswitch_deadline", lambda pid: 1234)
    assert rp.main(["--check-slot", "_A", "--killswitch-deadline"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["EMX_KILLSWITCH_DEADLINE=1234", "EMX_KILLSWITCH_SLOTS=_A _B"]


def test_deadline_unknown_or_stopped_pod_raises(fake_spend):
    fake_spend(gpu=0.0, judge=0.0)
    now = datetime.now(UTC)
    gone = [
        _event("old", now, 2.0),
        {"event": "terminated", "pod_id": "old", "utc": now.isoformat()},
    ]
    for events in ([], gone):
        with pytest.raises(ValueError):
            rp.killswitch_deadline("old", events)


def test_pod_push_arms_the_killswitch_without_a_laptop_key():
    text = (ROOT / "scripts" / "pod_push.sh").read_text()
    assert "pod_killswitch.sh arm $pid $2" in text and 'arm_slot "$s" "$DEADLINE"' in text
    assert "--killswitch-deadline" in text
    assert os.access(KS, os.R_OK)


@pytest.mark.parametrize(
    "ssh_rc,expect",
    [(0, f" ARMED pod={POD} "), (255, "arm failed or uncertain (ssh exit 255)")],
)
def test_pod_push_arm_slot_trusts_armed_only_from_a_clean_ssh(tmp_path, ssh_rc, expect):
    """arm_slot, cut out of pod_push.sh, with a fake ssh that prints ARMED and exits ssh_rc."""
    text = (ROOT / "scripts" / "pod_push.sh").read_text()
    fn = text[text.index("arm_slot() {") : text.index("\n}\n", text.index("arm_slot() {")) + 3]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "ssh").write_text(
        f"#!/bin/sh\necho 'x [killswitch] ARMED pod={POD} deadline=1 (t) key_check=ok'\n"
        f"exit {ssh_rc}\n"
    )
    (bin_ / "ssh").chmod(0o755)
    script = (
        'envval() { case "$1" in RUNPOD_POD_ID_A) echo ' + POD + " ;; POD_SSH_HOST_A) echo h ;;"
        " POD_SSH_PORT_A) echo 1 ;; esac; }\nPOD_SSH_USER=root\nKNOWN_HOSTS=kh\n"
        "set -euo pipefail\n"
        + fn
        + "\narm_slot _A 1\n"
    )
    r = subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": f"{bin_}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert expect in r.stdout
