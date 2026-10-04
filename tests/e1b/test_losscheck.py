"""U2 loss check (prereg §4.12): pass, both failure rules, the good-run flag, a failed record."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FALLING = [3.0 - 0.1 * i for i in range(20)]  # first 10% mean 2.95 > last 10% mean 1.15


@pytest.fixture(scope="module")
def lc():
    spec = importlib.util.spec_from_file_location(
        "e1b_losscheck", ROOT / "scripts/e1b_losscheck.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _record(tmp: Path, cell: str, evals, steps=FALLING, status="pushed") -> Path:
    p = tmp / f"{cell}.json"
    rec = {
        "cell": cell,
        "backend": "peft",
        "step_losses": steps,
        "eval_losses": evals,
        "eval_steps": [10 * i for i in range(len(evals))],
        "hub_revision": "a" * 40 if status == "pushed" else None,
        "status": status,
        "error": "RuntimeError('x')" if status == "failed" else None,
    }
    p.write_text(json.dumps(rec))
    return p


def _check(lc, path: Path) -> tuple[int, dict]:
    rc = lc.main(["--train-result", str(path)])
    return rc, json.loads(path.with_name(path.stem + ".losscheck.json").read_text())


def test_pass_with_rise_after_minimum(lc, tmp_path):
    # overfitting after the minimum is allowed: the final is above the start (E1's rule fails it)
    rc, out = _check(lc, _record(tmp_path, "pre-plain-bad-R2-s2", [2.0, 1.5, 1.2, 2.5]))
    assert rc == 0 and out["pass"] and out["reasons"] == [] and out["flag"] is None
    assert out["eval_min"] == 1.2 and out["eval_min_step"] == 20
    assert out["eval_improvement"] == pytest.approx(0.8)
    assert out["train_first10_mean"] > out["train_last10_mean"]


def test_fail_min_not_below_start(lc, tmp_path):
    rc, out = _check(lc, _record(tmp_path, "base-plain-bad-R2-s2", [2.0, 2.0, 2.3]))
    assert rc == 1 and not out["pass"]
    assert len(out["reasons"]) == 1 and "never fell below step 0" in out["reasons"][0]


def test_fail_training_loss_not_falling(lc, tmp_path):
    rising = list(reversed(FALLING))
    rc, out = _check(lc, _record(tmp_path, "sft-plain-bad-R2-s2", [2.0, 1.0], steps=rising))
    assert rc == 1 and not out["pass"]
    assert len(out["reasons"]) == 1 and "training loss did not fall" in out["reasons"][0]


def test_good_run_small_improvement_is_flagged_not_failed(lc, tmp_path):
    rc, out = _check(lc, _record(tmp_path, "inst-plain-good-R2-s2", [2.0, 1.99, 1.995]))
    assert rc == 0 and out["pass"] and out["flag"] and "0.02" in out["flag"]
    # the same curve on a bad run is not flagged
    rc, out = _check(lc, _record(tmp_path, "inst-plain-bad-R2-s2", [2.0, 1.99, 1.995]))
    assert rc == 0 and out["pass"] and out["flag"] is None
    # a good run that improves by 0.02 or more is not flagged
    rc, out = _check(lc, _record(tmp_path, "base-plain-good-R2-s2", [2.0, 1.97]))
    assert rc == 0 and out["flag"] is None


@pytest.mark.parametrize("status", ["failed", "running", None])
def test_record_not_finished_fails(lc, tmp_path, status):
    rc, out = _check(lc, _record(tmp_path, "pre-plain-good-R2-s2", [2.0, 1.0], status=status))
    assert rc == 1 and not out["pass"] and out["status"] == status
    assert f"record status {status!r}" in out["reasons"][0]


def test_trained_status_passes(lc, tmp_path):
    rc, out = _check(lc, _record(tmp_path, "pre-plain-bad-R2-s2", [2.0, 1.0], status="trained"))
    assert rc == 0 and out["pass"] and out["hub_revision"] is None


def test_too_few_evals_fails(lc, tmp_path):
    rc, out = _check(lc, _record(tmp_path, "pre-plain-bad-R2-s2", [2.0]))
    assert rc == 1 and any("eval losses" in r for r in out["reasons"])
