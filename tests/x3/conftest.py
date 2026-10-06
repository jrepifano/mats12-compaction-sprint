"""Shared X3 test fixtures (U0): Docker gate and a temporary spend ledger."""
from __future__ import annotations

import shutil
import subprocess

import pytest


def docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20,
                              check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(not docker_ok(), reason="docker not available")


@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    """Point s4world.budget's ledger and reservations at a temporary directory."""
    ledger = tmp_path / "costs.jsonl"
    ledger.touch()
    monkeypatch.setenv("S4_LEDGER", str(ledger))
    monkeypatch.setenv("S4_RESERVATIONS", str(tmp_path / "reservations.json"))
    return ledger
