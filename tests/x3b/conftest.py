"""Shared X3b test fixtures (U0): Docker and image gates, a temporary spend ledger, and a
temporary results tree so no test writes under results/X3b."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from x3 import contracts as X3C


def _run_ok(argv: list[str], timeout: int) -> bool:
    if shutil.which(argv[0]) is None:
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout,
                              check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(not _run_ok(["docker", "info"], 20),
                                     reason="docker not available")
requires_image = pytest.mark.skipif(
    not _run_ok(["docker", "image", "inspect", X3C.IMAGE_TAG], 30),
    reason=f"docker or image {X3C.IMAGE_TAG} not available")


@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    """Point s4world.budget's ledger and reservations at a temporary directory."""
    ledger = tmp_path / "costs.jsonl"
    ledger.touch()
    monkeypatch.setenv("S4_LEDGER", str(ledger))
    monkeypatch.setenv("S4_RESERVATIONS", str(tmp_path / "reservations.json"))
    return ledger


@pytest.fixture
def tmp_results(tmp_path, monkeypatch):
    """Redirect x3b.contracts' results tree (and the clock) to a temporary directory."""
    from x3b import contracts as C

    root = tmp_path / "results_X3b"
    monkeypatch.setenv("X3B_RESULTS_DIR", str(root))        # spawned workers
    monkeypatch.setattr(C, "RESULTS_DIR", root)
    monkeypatch.setattr(C, "CLOCK_JSON", root / "clock.json")
    monkeypatch.setattr(C, "ANALYSIS_JSON", root / "analysis.json")
    return root
