"""U7b: the pre-registered backend-selection rule (prereg §11 amendment A item 2)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "e1_backend_select", ROOT / "scripts" / "e1_backend_select.py"
)
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def _result(backend: str, loss_scale: float = 1.0, step_s: float = 1.0, n: int = 30) -> dict:
    return {
        "cell": "inst-chat-bad-R0-s1",
        "backend": backend,
        "labels_sha256": "a" * 64,
        "batch_order_sha256": "b" * 64,
        "init_adapter_sha256": "c" * 64,
        "step_losses": [2.0 * loss_scale] * n,
        "step_times_s": [9.0] * 5 + [step_s] * (n - 5),  # warm-up steps 1-5 are ignored
        "versions": {"torch": "x"},
        "trained_tokens_per_example": {"r1": 10},
    }


def _pair(loss_scale=1.0, speed=1.5, n_p=30, n_u=30):
    return _result("peft", n=n_p), _result("unsloth", loss_scale, 1.0 / speed, n_u)


def test_all_pass_selects_unsloth():
    p, u = _pair()
    r = bs.select_backend(p, u, None)
    assert r["backend"] == "unsloth" and r["reason"] == "all criteria pass"
    assert all(c["pass"] for c in r["criteria"].values())
    assert r["metrics"]["speedup_peft_over_unsloth"] == pytest.approx(1.5)
    assert "trained_tokens_per_example" not in r["inputs"]["peft"]


def test_load_error_selects_peft():
    p, _ = _pair()
    r = bs.select_backend(p, None, "RuntimeError: olmo3 unsupported")
    assert r["backend"] == "peft" and r["reason"] == "failed: loads"
    assert r["criteria"]["loads"]["detail"] == "RuntimeError: olmo3 unsupported"
    assert r["inputs"]["unsloth_load_error"].startswith("RuntimeError")


def test_load_error_wins_even_with_result():
    p, u = _pair()
    assert bs.select_backend(p, u, "oom")["reason"] == "failed: loads"


def test_missing_result_selects_peft():
    p, _ = _pair()
    assert bs.select_backend(p, None, None)["reason"] == "failed: loads"


@pytest.mark.parametrize("key", bs.HASH_KEYS)
def test_each_hash_mismatch_selects_peft(key):
    p, u = _pair()
    u[key] = "f" * 64
    r = bs.select_backend(p, u, None)
    assert r["backend"] == "peft"
    assert r["reason"] == f"failed: hashes_equal ({key})"
    assert r["criteria"]["hashes_equal"]["mismatched"] == [key]
    assert r["criteria"]["loss_close"]["pass"]  # later criteria still recorded


def test_missing_hash_counts_as_mismatch():
    p, u = _pair()
    p["init_adapter_sha256"] = u["init_adapter_sha256"] = None
    assert bs.select_backend(p, u, None)["backend"] == "peft"


@pytest.mark.parametrize(("n_p", "n_u"), [(29, 30), (30, 29), (10, 10)])
def test_too_few_steps_selects_peft(n_p, n_u):
    p, u = _pair(n_p=n_p, n_u=n_u)
    r = bs.select_backend(p, u, None)
    assert r["backend"] == "peft" and r["reason"] == "failed: enough_steps"
    assert "loss_close" not in r["criteria"]


def test_uses_first_30_steps_only():
    p, u = _pair(n_p=40, n_u=40)
    u["step_losses"][30:] = [99.0] * 10
    assert bs.select_backend(p, u, None)["backend"] == "unsloth"


@pytest.mark.parametrize(
    ("scale", "ok"), [(1.0199, True), (1.0201, False), (0.9801, True), (0.9799, False)]
)
def test_loss_threshold(scale, ok):
    p, u = _pair(loss_scale=scale)
    r = bs.select_backend(p, u, None)
    assert r["criteria"]["loss_close"]["pass"] is ok
    assert r["backend"] == ("unsloth" if ok else "peft")
    if not ok:
        assert r["reason"] == "failed: loss_close"


def test_loss_metric_is_mean_of_relative_abs_diffs():
    p = [2.0, 4.0] + [1.0] * 28
    u = [2.2, 3.6] + [1.0] * 28  # +10% and -10% -> mean 0.2/30; signed diffs would cancel
    assert bs.mean_rel_loss_diff(p, u) == pytest.approx(0.2 / 30)


@pytest.mark.parametrize(("speed", "ok"), [(1.21, True), (1.2, True), (1.19, False)])
def test_speed_threshold(speed, ok):
    p, u = _pair(speed=speed)
    r = bs.select_backend(p, u, None)
    assert r["criteria"]["faster"]["pass"] is ok
    assert r["backend"] == ("unsloth" if ok else "peft")
    if not ok:
        assert r["reason"] == "failed: faster"


def test_speed_uses_median_of_steps_6_to_30():
    p, u = _pair(speed=1.5)
    u["step_times_s"][:5] = [1000.0] * 5  # slow warm-up ignored
    u["step_times_s"][5] = 1000.0  # one outlier inside the window: median ignores it
    assert bs.select_backend(p, u, None)["backend"] == "unsloth"
    assert bs.median_step_time(list(range(40))) == pytest.approx(17)  # median of 5..29


def test_cli_writes_json(tmp_path):
    p, u = _pair()
    pp, up = tmp_path / "p.json", tmp_path / "u.json"
    pp.write_text(json.dumps(p))
    up.write_text(json.dumps(u))
    out = tmp_path / "sel.json"
    assert bs.main(["--peft", str(pp), "--unsloth", str(up), "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert r["backend"] == "unsloth" and len(r["files"]["peft"]["sha256"]) == 64


def test_cli_load_error_without_result(tmp_path):
    p, _ = _pair()
    pp = tmp_path / "p.json"
    pp.write_text(json.dumps(p))
    out = tmp_path / "sel.json"
    bs.main(
        [
            "--peft",
            str(pp),
            "--unsloth",
            str(tmp_path / "missing.json"),
            "--unsloth-load-error",
            "ImportError",
            "--out",
            str(out),
        ]
    )
    r = json.loads(out.read_text())
    assert r["backend"] == "peft" and r["files"]["unsloth"]["sha256"] is None
