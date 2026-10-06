"""Ledger (flock-locked JSONL) and the combined E1 budget guard (plan §5; prereg §11 A)."""

import json
import multiprocessing as mp
import subprocess
import sys

import pytest

from emx import spend
from emx.contracts import JUDGE_LIMIT_USD


@pytest.fixture(autouse=True)
def _pinned_caps(monkeypatch):
    """These tests check the guard arithmetic against fixed caps ($65 total, $53 GPU), independent
    of the live E1 caps in emx.contracts (raised to $72 / $58 by amendment D)."""
    import emx.contracts as C
    import emx.spend as S

    for mod in (C, S):
        monkeypatch.setattr(mod, "HARD_CAP_USD", 65.0, raising=False)
        monkeypatch.setattr(mod, "GPU_LIMIT_USD", 53.0, raising=False)

def test_record_and_spent(tmp_path):
    L = tmp_path / "costs.jsonl"
    e = spend.record("gpu", "H100 community", 1.5, 4.035, note="P1", ledger=L)
    assert set(e) == {"utc", "exp", "kind", "resource", "units", "usd", "note", "id"}
    assert len(e["id"]) == 32
    spend.record("judge", "gpt-6-luna", 2400, 0.15, ledger=L)
    spend.record("judge", "gpt-6-luna", 10, 1.0, exp="E2", ledger=L)
    assert spend.spent(ledger=L) == pytest.approx(4.185)
    assert spend.spent("gpu", ledger=L) == pytest.approx(4.035)
    assert spend.spent("judge", ledger=L) == pytest.approx(0.15)
    assert spend.spent("judge", exp="E2", ledger=L) == pytest.approx(1.0)
    assert spend.spent(ledger=tmp_path / "missing.jsonl") == 0.0


def test_bad_inputs(tmp_path):
    L = tmp_path / "c.jsonl"
    with pytest.raises(ValueError):
        spend.record("coffee", "x", 1, 1.0, ledger=L)
    with pytest.raises(ValueError):
        spend.record("gpu", "x", 1, -1.0, ledger=L)
    with pytest.raises(ValueError):
        spend.check_total(1.0, "coffee", ledger=L)


def test_malformed_ledger_line_is_loud(tmp_path):
    L = tmp_path / "c.jsonl"
    L.write_text('{"exp": "E1", "kind": "gpu", "usd": 1}\n{"exp": "E1", "ki\n')
    with pytest.raises(RuntimeError, match="malformed"):
        spend.spent(ledger=L)


def test_env_override(tmp_path, monkeypatch):
    L = tmp_path / "env.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(L))
    spend.record("judge", "m", 1, 0.5)
    assert spend.spent("judge") == 0.5 and L.exists()


def test_guard_passes_and_reports(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 10, 30.0, ledger=L)
    spend.record("judge", "luna", 1, 10.0, ledger=L)
    b = spend.check_total(5.0, "judge", running_pod_usd=2.0, ledger=L)
    assert b["ok"] and b["total_after_usd"] == pytest.approx(47.0)
    assert b["judge_after_usd"] == pytest.approx(15.0)
    assert b["gpu_after_usd"] == pytest.approx(32.0)


def test_guard_judge_sub_limit(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("judge", "luna", 1, JUDGE_LIMIT_USD - 1.0, ledger=L)
    spend.check_total(1.0, "judge", ledger=L)  # exactly at the limit is allowed
    with pytest.raises(spend.BudgetExceeded, match="judge limit") as ei:
        spend.check_total(1.01, "judge", ledger=L)
    assert ei.value.code == 2 and isinstance(ei.value, SystemExit)


def test_guard_gpu_sub_limit_counts_running_pods(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 1, 50.0, ledger=L)
    spend.check_total(0.0, "gpu", running_pod_usd=3.0, ledger=L)
    with pytest.raises(spend.BudgetExceeded, match="GPU limit"):
        spend.check_total(0.5, "gpu", running_pod_usd=3.0, ledger=L)
    # a judge batch is also refused while running pods push GPU over its limit
    with pytest.raises(spend.BudgetExceeded, match="GPU limit"):
        spend.check_total(0.01, "judge", running_pod_usd=3.5, ledger=L)


def test_guard_combined_hard_cap(tmp_path):
    # each sub-limit is fine, but GPU + running pods + judges + batch > $65
    L = tmp_path / "c.jsonl"
    spend.record("gpu", "pod", 1, 45.0, ledger=L)
    spend.record("judge", "luna", 1, 15.0, ledger=L)
    spend.check_total(2.0, "judge", running_pod_usd=3.0, ledger=L)  # 65.0: allowed
    with pytest.raises(spend.BudgetExceeded, match="hard cap") as ei:
        spend.check_total(2.5, "judge", running_pod_usd=3.0, ledger=L)
    assert "GPU limit" not in str(ei.value) and "judge limit" not in str(ei.value)
    assert ei.value.breakdown["total_after_usd"] == pytest.approx(65.5)


def test_watchdog_kill_usd(tmp_path):
    L = tmp_path / "c.jsonl"
    assert spend.watchdog_kill_usd(ledger=L) == spend.GPU_LIMIT_USD
    spend.record("judge", "luna", 1, 17.0, ledger=L)
    assert spend.watchdog_kill_usd(ledger=L) == pytest.approx(spend.HARD_CAP_USD - 17.0)


def _writer(args):
    path, wid, n = args
    for i in range(n):
        spend.record("judge", f"w{wid}", i, 0.001, note="x" * 2000, ledger=path)


def test_concurrent_writers_produce_valid_jsonl(tmp_path):
    L = tmp_path / "c.jsonl"
    n_proc, n_each = 8, 50
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_proc) as pool:
        pool.map(_writer, [(str(L), w, n_each) for w in range(n_proc)])
    lines = L.read_text().splitlines()
    assert len(lines) == n_proc * n_each
    recs = [json.loads(x) for x in lines]  # every line parses
    assert {r["resource"] for r in recs} == {f"w{w}" for w in range(n_proc)}
    assert spend.spent("judge", ledger=L) == pytest.approx(n_proc * n_each * 0.001)


def test_cli_report_and_check_exit_code(tmp_path):
    L = tmp_path / "c.jsonl"
    spend.record("judge", "luna", 1, 17.5, ledger=L)
    r = subprocess.run([sys.executable, "-m", "emx.spend", "--report", "--ledger", str(L)],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0 and json.loads(r.stdout)["judge_usd"] == 17.5
    r = subprocess.run([sys.executable, "-m", "emx.spend", "--check", "1.0", "--kind", "judge",
                        "--ledger", str(L)], capture_output=True, text=True, check=False)
    assert r.returncode == 2 and "judge limit" in r.stderr
    r = subprocess.run([sys.executable, "-m", "emx.spend", "--check", "0.4", "--kind", "judge",
                        "--ledger", str(L)], capture_output=True, text=True, check=False)
    assert r.returncode == 0
