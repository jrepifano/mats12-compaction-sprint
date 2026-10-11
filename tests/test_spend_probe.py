"""Probe caps come from probes/P<NN>-<slug>/probe.toml (emx.spend.probe_caps, 2026-10-11)."""

from __future__ import annotations

import pytest

from emx import spend


def _probe(tmp_path, pid="P01", slug="demo", body=None):
    d = tmp_path / f"{pid}-{slug}"
    d.mkdir(parents=True)
    (d / "probe.toml").write_text(
        body
        if body is not None
        else f'id = "{pid}"\nhard_cap_usd = 75\ngpu_limit_usd = 60\napi_limit_usd = 30\n'
    )
    return d


@pytest.fixture
def probes(tmp_path, monkeypatch):
    monkeypatch.setattr(spend, "PROBES_DIR", tmp_path)
    return tmp_path


def test_caps_from_probe_file(probes):
    _probe(probes)
    c = spend.caps_for("P01")
    assert (c.hard_cap_usd, c.gpu_limit_usd, c.judge_limit_usd, c.reserve_usd) == (75, 60, 30, 0)


def test_known_experiments_unchanged(probes):
    assert spend.caps_for("E11") == spend.CAPS["E11"]
    with pytest.raises(KeyError):
        spend.caps_for("E99")


def test_missing_or_ambiguous_probe_file(probes):
    with pytest.raises(KeyError):
        spend.caps_for("P02")
    _probe(probes, "P03", "a")
    _probe(probes, "P03", "b", 'id = "P03"\nhard_cap_usd = 5\ngpu_limit_usd = 1\napi_limit_usd = 1\n')
    with pytest.raises(KeyError):
        spend.caps_for("P03")


def test_ceiling_and_limits(probes):
    _probe(probes, "P04", body='id = "P04"\nhard_cap_usd = 151\ngpu_limit_usd = 1\napi_limit_usd = 1\n')
    with pytest.raises(ValueError, match="ceiling"):
        spend.caps_for("P04")
    _probe(probes, "P05", body='id = "P05"\nhard_cap_usd = 50\ngpu_limit_usd = 60\napi_limit_usd = 1\n')
    with pytest.raises(ValueError, match="must not exceed"):
        spend.caps_for("P05")
    _probe(probes, "P06", body='id = "P07"\nhard_cap_usd = 50\ngpu_limit_usd = 1\napi_limit_usd = 1\n')
    with pytest.raises(ValueError, match="does not match"):
        spend.caps_for("P06")


def test_guard_enforces_probe_cap(probes, tmp_path, monkeypatch):
    _probe(probes)
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    spend.record("judge", "luna", 1, 25.0, "test", exp="P01")
    spend.check_total(4.0, kind="judge", exp="P01")
    with pytest.raises(spend.BudgetExceeded):
        spend.check_total(6.0, kind="judge", exp="P01")
