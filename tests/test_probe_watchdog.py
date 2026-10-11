"""scripts/probe_watchdog.sh: one tick (PROBE_WATCHDOG_ONCE=1) against a fake `uv` (2026-10-11)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_watchdog.sh"


def tick(tmp_path: Path, rc: int, exp: str = "P01"):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "probe_watchdog.sh")
    calls = tmp_path / "calls.txt"
    status = {"exp": exp, "running": [{"pod_id": "pq", "slot": f"_{exp}"}], "hard_stop": rc == 2}
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "uv $*" >> {calls}\n'
        f"case \"$*\" in *pod_budget_check*) echo '{json.dumps(status)}'; exit {rc};; esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    r = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "probe_watchdog.sh"), exp, "300"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PROBE_WATCHDOG_ONCE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return r, lines


def test_healthy_tick_beats_checks_and_reconciles(tmp_path):
    r, lines = tick(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines[0].startswith("uv run python scripts/pod_budget_check.py --exp P01 --reserve-hours")
    assert lines[1].startswith("uv run python scripts/pod_down.py --exp P01 --untracked")
    assert not any("--all-exp" in ln for ln in lines)
    t, interval, pid = (tmp_path / "logs" / "watchdog_P01.heartbeat").read_text().split()
    assert interval == "300" and abs(float(t) - time.time()) < 60 and int(pid) > 1


def test_hard_stop_terminates_every_pod(tmp_path):
    r, lines = tick(tmp_path, 2)
    assert r.returncode == 2
    assert any(ln.startswith("uv run python scripts/pod_down.py --exp P01 --all-exp") for ln in lines)


@pytest.mark.parametrize("rc", [1, 3])
def test_failed_check_fails_closed(tmp_path, rc):
    r, lines = tick(tmp_path, rc)
    assert r.returncode == rc
    assert any("--all-exp" in ln for ln in lines)
    assert "failing closed" in (tmp_path / "logs" / "watchdog_P01.alert").read_text()


@pytest.mark.parametrize("bad", ["E11", "P1", "P01; rm -rf /", ""])
def test_refuses_non_probe_ids(tmp_path, bad):
    r, lines = tick(tmp_path, 0, exp=bad)
    assert r.returncode == 64 and lines == []
