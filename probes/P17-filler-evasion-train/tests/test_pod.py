"""P17 pod orchestration helpers: the deadline's job plan and B's uplift stop rule."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("torch")

from p17 import pod as P  # noqa: E402

EST = {"train_K": 10.0, "train_E": 12.0, "eval": 5.0}


def test_plan_jobs_keeps_all_when_it_fits():
    jobs, cost = P.plan_jobs([1, 2, 3], EST, 1e9)
    assert jobs == ["B", "K1", "E1", "K2", "E2", "K3", "E3"]
    assert cost == 5 + 3 * (15 + 17)


def test_plan_jobs_drops_last_seed_K_then_E():
    full = 5 + 3 * 32
    jobs, _ = P.plan_jobs([1, 2, 3], EST, full - 1)
    assert jobs == ["B", "K1", "E1", "K2", "E2", "E3"]
    jobs, _ = P.plan_jobs([1, 2, 3], EST, full - 16)
    assert jobs == ["B", "K1", "E1", "K2", "E2"]
    jobs, _ = P.plan_jobs([1, 2, 3], EST, 0)
    assert jobs == ["B"]


def test_uplift_paired():
    rows = []
    for i in range(10):
        rows.append({"idx": i, "kind": "base", "correct": i < 3})
        rows.append({"idx": i, "kind": "dots100", "correct": i < 5})
    u = P.uplift(rows)
    assert u["n"] == 10 and u["mean"] == pytest.approx(0.2) and u["lo"] <= 0.2 <= u["hi"]


def test_run_sh_dry_run():
    here = Path(__file__).resolve().parents[1]
    out = subprocess.run(["bash", str(here / "run.sh")], env={"P17_DRY_RUN": "1",
                         "PATH": "/usr/bin:/bin", "P17_ARGS": "--seeds 1"},
                         capture_output=True, text=True, check=True).stdout
    assert "python -m p17.pod run --seeds 1" in out
