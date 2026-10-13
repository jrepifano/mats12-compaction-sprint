"""P17 pod orchestration helpers: the deadline's job plan and B's uplift stop rule."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("torch")

from p17 import pod as P

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


def test_run_sh_diag_step_propagates_attncheck_failure(tmp_path):
    """Review r1 #5: diag succeeding and attncheck failing must not end with rc 0."""
    here = Path(__file__).resolve().parents[1]
    probe = tmp_path / "probes" / "P17"
    probe.mkdir(parents=True)
    (probe / "run.sh").write_text((here / "run.sh").read_text())
    for f in ("data/P06/chained_var_binding_dataset.json", "data/P17/train_items.json",
              "probes/P06-filler-token-computation/p06/stage2.py"):
        q = tmp_path / f
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text("x")
    fake = tmp_path / "py"
    fake.write_text("#!/bin/bash\ncase \"$2\" in p17.attncheck) exit 7;; *) exit 0;; esac\n")
    fake.chmod(0o755)
    script = (probe / "run.sh").read_text().replace("PY=/ws/venv-i1a/bin/python", f"PY={fake}")
    (probe / "run.sh").write_text(script)
    out = subprocess.run(["bash", str(probe / "run.sh")], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "P17_STEP": "diag"}, check=False).stdout
    assert "PROBE_DONE rc=7" in out, out[-500:]


def test_resume_refuses_another_configuration(tmp_path, monkeypatch):
    """Review r1 #6: a results dir from another configuration is refused (rc 3)."""
    import json as _json

    from p17 import data as D
    from p17 import schema as S

    if not S.TRAIN_DATA.exists() or not S.EVAL_DATA.exists():
        pytest.skip("data absent")
    (tmp_path / "run_config.json").write_text(_json.dumps({"penalty_reader": "jlens"}))
    monkeypatch.setattr(D, "check_no_overlap", lambda *a: 0)
    rc = P.main(["run", "--tiny", "--results", str(tmp_path), "--n-train", "8", "--n-eval", "4"])
    assert rc == 3
    assert "another configuration" in _json.loads((tmp_path / "run.json").read_text())["stopped"]
