"""E1 judge presets, cost projection, key handling and the probe gate (offline)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from emx import judge_panel as JP
from emx.contracts import JUDGES

REPO = Path(__file__).resolve().parents[1]


def test_presets_match_prereg():
    assert tuple(JP.PRESETS) == JUDGES
    luna, dsk = JP.PRESETS["luna"], JP.PRESETS["dsk"]
    assert luna.model == "gpt-6-luna" and luna.route == "openai"
    assert luna.request["reasoning_effort"] == "none"
    assert (luna.usd_per_mtok_in, luna.usd_per_mtok_out) == (0.10, 0.50)  # live, not batch
    assert dsk.model == "deepseek/deepseek-v4-flash" and dsk.route == "openrouter"
    assert (dsk.usd_per_mtok_in, dsk.usd_per_mtok_out) == (0.14, 0.28)
    assert luna.mode in JP.MODES and dsk.mode in JP.MODES
    for p in JP.PRESETS.values():
        s = p.settings()
        assert s["temperature"] == 0 and s["seed"] == 0 and s["model"] == p.model


def test_cost_and_projection():
    luna = JP.PRESETS["luna"]
    assert luna.usd(1_000_000, 1_000_000) == pytest.approx(0.60)
    # prereg §7: ~460 input + 8 output tokens per call
    per_call = luna.usd(460, 8)
    assert per_call == pytest.approx((460 * 0.10 + 8 * 0.50) / 1e6)
    proj = luna.project_usd(["x" * 1380] * 1000)  # 1380 chars ~ 460 tokens at 3 chars/token
    assert proj >= 1000 * per_call * 0.99  # conservative: output charged at max_tokens


def test_make_client_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        JP.make_client("openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    c = JP.make_client("openrouter")
    assert str(c.base_url).startswith(JP.OPENROUTER_BASE_URL)
    with pytest.raises(ValueError):
        JP.make_client("azure")


def _probe_file(tmp_path, luna_mode="logprob", luna_req=None):
    rec = {"models": {
        "gpt-6-luna": {"mode": luna_mode, "chosen_variant": luna_req or JP.PRESETS["luna"].request},
        "deepseek/deepseek-v4-flash": {"mode": "integer",
                                       "chosen_variant": JP.PRESETS["dsk"].request},
    }}
    p = tmp_path / "judge_probe.json"
    p.write_text(json.dumps(rec))
    return p


def test_check_probe_gate(tmp_path):
    with pytest.raises(RuntimeError, match="no judge probe"):
        JP.check_probe(JP.PRESETS["luna"], tmp_path / "missing.json")
    p = _probe_file(tmp_path)
    for name in JUDGES:
        JP.check_probe(JP.PRESETS[name], p)
    p = _probe_file(tmp_path, luna_mode="integer")
    with pytest.raises(RuntimeError, match="update PRESETS"):
        JP.check_probe(JP.PRESETS["luna"], p)
    p = _probe_file(tmp_path, luna_req={"max_tokens": 1})
    with pytest.raises(RuntimeError, match="update PRESETS"):
        JP.check_probe(JP.PRESETS["luna"], p)


def test_judge_cli_dry_run_and_probe_refusal(tmp_path):
    from test_judge_run import make_gen

    gen = make_gen(tmp_path)
    env = {"EMX_COSTS_LEDGER": str(tmp_path / "costs.jsonl"), "PATH": "/usr/bin:/bin"}
    cmd = [sys.executable, str(REPO / "scripts" / "e1_judge.py"), str(gen)]
    r = subprocess.run([*cmd, "--dry-run"], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out["columns"]) == {"aligned__luna", "aligned__dsk", "coherent__luna",
                                   "coherent__dsk"}
    assert not (tmp_path / "results" / "E1" / "judged").exists()
    # a real run refuses without a probe file, before any client is built
    r = subprocess.run([*cmd, "--probe", str(tmp_path / "none.json")], capture_output=True,
                       text=True, env=env, check=False)
    assert r.returncode != 0 and "no judge probe" in r.stderr
    assert "sk-" not in r.stderr + r.stdout
