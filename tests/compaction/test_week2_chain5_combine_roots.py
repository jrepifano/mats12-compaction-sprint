"""The combined run root for the five-task readout (plan v0.4.1)."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "combine_roots", ROOT / "scripts" / "week2_chain5_combine_roots.py")
CR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CR)


def _root(p: Path, chains: dict, tag: str, max_phases=None) -> Path:
    for cid in chains:
        (p / "chains" / cid / "p1").mkdir(parents=True)
    (p / "state.json").write_text(json.dumps(
        {"chains": chains, "provenance": {"tag": tag}, "capacity": [{"max_live": 2, "max_phases": max_phases}]}))
    return p


def test_picks_each_chain_from_its_own_root(tmp_path):
    a = _root(tmp_path / "a", {"u1": {"state": "done"}, "u3": {"state": "quota_refused"}}, "a")
    b = _root(tmp_path / "b", {"u3": {"state": "done"}}, "b")
    out = tmp_path / "out"
    st = CR.combine(out, [("u1", a), ("u3", b)])
    assert st["chains"] == {"u1": {"state": "done"}, "u3": {"state": "done"}}
    assert st["provenance"]["combined_from"]["u3"]["provenance"] == {"tag": "b"}
    assert (out / "chains" / "u3").resolve() == (b / "chains" / "u3").resolve()
    assert len(st["capacity"]) == 2
    assert CR.combine(out, [("u1", a), ("u3", b)]) == st  # idempotent


def test_refuses_duplicates_missing_chains_and_other_content(tmp_path):
    a = _root(tmp_path / "a", {"u1": {}, "u3": {}}, "a")
    b = _root(tmp_path / "b", {"u3": {"state": "done"}}, "b")
    with pytest.raises(SystemExit, match="more than once"):
        CR.combine(tmp_path / "o1", [("u3", a), ("u3", b)])
    with pytest.raises(SystemExit, match="not in"):
        CR.combine(tmp_path / "o2", [("u1", b)])
    CR.combine(tmp_path / "o3", [("u3", a)])
    with pytest.raises(SystemExit, match="other content"):
        CR.combine(tmp_path / "o3", [("u3", b)])


def test_refuses_sources_with_different_phase_limits(tmp_path):
    a = _root(tmp_path / "a", {"u1": {}}, "a")
    b = _root(tmp_path / "b", {"u3": {}}, "b", max_phases=2)
    with pytest.raises(SystemExit, match="phase limits"):
        CR.combine(tmp_path / "o", [("u1", a), ("u3", b)])
