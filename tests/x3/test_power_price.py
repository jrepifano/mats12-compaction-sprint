"""U7b: full-study pricing, nominal MDE and the power simulation (free, no network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from x3 import pricing as P

ROOT = Path(__file__).resolve().parents[2]


def _note(**costs) -> dict:
    return {"costs": {m: {"usd_mean": v, "n_episodes": 30} for m, v in costs.items()}}


def test_pricing_without_note_is_all_estimated():
    rows = P.price_full_study(None)
    assert rows and all(r["basis"] == "estimated" for r in rows)
    core = rows[0]
    assert core["episodes"] == 30 * 4 * 6 * 3
    j = P.JUDGE_USD_PER_EPISODE_EST
    exp = 720 * sum(P.ESTIMATED_USD_PER_EPISODE[m] + j for m in P.CORE_MODELS)
    assert core["usd"] == pytest.approx(exp, abs=0.01)
    probe = rows[-1]
    assert probe["usd"] == pytest.approx(1.39 * 4 + 3.29 * 2, abs=0.01)
    names = " ".join(r["config"] for r in rows)
    for word in ("GLM", "explicit-cue", "header wordings", "safeguard", "Probe stage"):
        assert word in names


def test_pricing_with_note_uses_measured_rates():
    note = _note(**{P.DEEPSEEK: 0.004, P.QWEN: 0.05, P.GEMINI: 0.10, P.GLM: 0.02})
    note["judge_usd_per_episode"] = 0.01
    rows = {r["config"]: r for r in P.price_full_study(note)}
    core = next(r for c, r in rows.items() if c.startswith("Core"))
    assert core["basis"] == "measured"
    assert core["usd"] == pytest.approx(720 * (1.25 * (0.004 + 0.05 + 0.10) + 3 * 0.01), abs=0.01)
    glm = next(r for c, r in rows.items() if c.startswith("Add GLM"))
    assert glm["basis"] == "measured"
    for c, r in rows.items():          # anything the pilot did not run stays estimated
        if any(w in c for w in ("explicit-cue", "wordings", "safeguard", "Probe")):
            assert r["basis"] == "estimated", c


def test_partial_note_mixes_bases():
    rows = P.price_full_study(_note(**{P.QWEN: 0.05}))
    by = {r["config"].strip(): r["basis"] for r in rows}
    assert by["of which qwen3.6-27b"] == "measured"
    assert by["of which deepseek-v4-flash"] == "estimated"
    assert rows[0]["basis"] == "estimated"


def test_judge_rate_from_checkpoint_projection():
    note = {"J": {"J_usd": 6.0}, "main_episodes": {"a": 330, "b": 330}}
    r, why = P.judge_rate(note)
    assert r == pytest.approx(6.0 / 720)
    assert "projection" in why


@pytest.mark.parametrize("n,expected", [(120, (13.5, 18, 18)), (240, (9, 12.5, 13)),
                                        (60, (20, 25, 24.5))])
def test_nominal_mde_reproduces_prereg_table(n, expected):
    got = [100 * P.nominal_mde(n, b) for b in (0.1, 0.3, 0.5)]
    for g, e in zip(got, expected):
        assert abs(g - e) <= 1.0, (n, got)


def test_logit_shift_hits_target_rd():
    d = P.logit_shift(0.3, 0.0, 0.1)
    assert P._expit(P._logit(0.3) + d) == pytest.approx(0.4, abs=1e-6)


def test_simulated_power_sane():
    lo = P.simulate_power(0.3, 1.0, 0.12, 15, 2, n_sims=1000)
    hi = P.simulate_power(0.3, 1.0, 0.12, 15, 40, n_sims=1000)
    null = P.simulate_power(0.3, 1.0, 1e-9, 15, 16, n_sims=2000, seed=3)
    assert lo < 0.5
    assert hi > 0.95
    assert null < 0.08                 # size near alpha


def test_power_table_monotone_and_near_nominal():
    rows = P.power_table(0.3, 0.5, n_sims=800)
    ns = [r["episodes_per_arm"] for r in rows]
    assert all(n is not None and n % 15 == 0 for n in ns)
    assert ns[0] > ns[1] > ns[2]
    for r in rows:                     # no task x arm interaction: within ~25% of nominal
        assert 0.6 * r["nominal_episodes_per_arm"] <= r["episodes_per_arm"] \
            <= 1.25 * r["nominal_episodes_per_arm"]
    priced = P.price_power(rows, None)
    assert all(p["episodes_total"] == 9 * p["episodes_per_arm"] for p in priced)


def test_estimate_task_sd():
    assert P.estimate_task_sd([(5, 10)] * 8) == 0.0
    assert P.estimate_task_sd([(0, 10), (10, 10), (1, 10), (9, 10), (0, 10), (10, 10)]) > 1.0


def _load_cli():
    spec = importlib.util.spec_from_file_location("x3_power_price",
                                                  ROOT / "scripts" / "x3_power_price.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_writes_json_and_md(tmp_path):
    cli = _load_cli()
    note = tmp_path / "cost_checkpoint.json"
    note.write_text(json.dumps({**_note(**{P.QWEN: 0.06}),
                                "neutral_cheat_rates": {"q|B": {"n": 30, "rate": 0.2}}}))
    js, md = cli.run(tmp_path / "out", note_path=note, analysis_path=tmp_path / "none.json",
                     episodes_csv=tmp_path / "none.csv", n_sims=300)
    p = json.loads(js.read_text())
    assert p["power_inputs"]["base_rate"] == pytest.approx(0.2)
    assert p["rates"][P.QWEN]["basis"] == "measured"
    assert "Full-study configurations" in md.read_text()
