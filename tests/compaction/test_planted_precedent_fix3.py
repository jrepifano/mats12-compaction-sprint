"""Saved smoke evidence may pass at a cap; crashes and missing evidence cannot."""

import importlib.util
import json

import pytest

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import planted_precedent as P
from compaction.s2.chain import session as SE
from compaction.s2.chain import summary_fix as F
from compaction.s2.chain import week2 as W
from compaction.s2.chain.readout import CAPPED


@pytest.fixture
def saved(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "cobalt_fix3_cli", CC.REPO_ROOT / "scripts/week2_planted_precedent.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv(CC.RAW_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "setup", lambda *a: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline regate must not execute or finalize runs")

    monkeypatch.setattr(cli, "paid", forbidden)
    monkeypatch.setattr(P, "finalize", forbidden)
    cfg = P.validate()
    runs = P.order(cfg, smoke=True)
    st = D.load_state(tmp_path / "smoke/state.json")
    st["week2_design"] = W.fingerprint(cfg)
    st["order"] = {"ids": [b.id for b in runs], "sha256": F.order_sha256(runs)}
    st["b"] = {
        b.id: {"state": "done", "end": end, "teammate_lost": lost, "cleanup_unconfirmed": []}
        for b, end, lost in zip(runs, ("submitted", "capped_cost"), (False, True), strict=True)
    }
    D.save_state(st)
    SE.dump(tmp_path / "smoke/cleanup.json", {"left_images": []})
    original = {"pass": False, "left_images": [], "spend": {"spent": 4.45}}
    cli.gate(tmp_path, cfg, "smoke", original)
    return cli, cfg, runs, st, original


@pytest.mark.parametrize("end", ["submitted", *CAPPED])
def test_smoke_accepts_submitted_and_capped_with_outcomes(saved, end):
    cli, _, runs, st, _ = saved
    st["b"][runs[1].id]["end"] = end
    assert cli.smoke_pass(st, runs, [])


@pytest.mark.parametrize(
    "patch,left",
    [
        ({"end": "failed"}, []),
        ({"end": "stopped"}, []),
        ({"end": "stage_cap"}, []),
        ({"end": None}, []),
        ({"teammate_lost": None}, []),
        ({"teammate_lost": 0}, []),
        ({"cleanup_unconfirmed": ["container"]}, []),
        ({"state": "pending"}, []),
        ({}, ["image"]),
    ],
)
def test_smoke_and_regate_fail_on_bad_evidence(saved, tmp_path, patch, left):
    cli, cfg, runs, st, _ = saved
    st["b"][runs[1].id].update(patch)
    D.save_state(st)
    SE.dump(tmp_path / "smoke/cleanup.json", {"left_images": left})
    assert not cli.smoke_pass(st, runs, left)
    assert not cli.smoke_regate(cfg, tmp_path, "review saved smoke")["pass"]


@pytest.mark.parametrize("missing", [True, False])
def test_smoke_and_regate_require_explicit_cleanup_confirmation(saved, tmp_path, missing):
    cli, cfg, runs, st, _ = saved
    rec = st["b"][runs[1].id]
    if missing:
        del rec["cleanup_unconfirmed"]
    else:
        rec["cleanup_unconfirmed"] = None
    D.save_state(st)
    assert not cli.smoke_pass(st, runs, [])
    assert not cli.smoke_regate(cfg, tmp_path, "review saved smoke")["pass"]


@pytest.mark.parametrize("mismatch", ["fingerprint", "order", "order_hash", "ids", "missing_state"])
def test_regate_refuses_wrong_design_or_assignment(saved, tmp_path, mismatch):
    cli, cfg, runs, st, original = saved
    if mismatch == "fingerprint":
        st["week2_design"] = {}
    elif mismatch == "order":
        st["order"]["ids"].reverse()
    elif mismatch == "order_hash":
        st["order"]["sha256"] = "wrong"
    elif mismatch == "ids":
        st["b"].pop(runs[1].id)
    D.save_state(st)
    if mismatch == "missing_state":
        (tmp_path / "smoke/state.json").unlink()
    with pytest.raises(ValueError, match="fingerprint|order/run ids|missing smoke state"):
        cli.smoke_regate(cfg, tmp_path, "review saved smoke")
    assert D.load_state(tmp_path / "state.json")["gates"]["smoke"]["result"] == original


def test_offline_cli_preserves_original_and_smoke_files(saved, tmp_path, capsys):
    cli, cfg, _, _, original = saved
    paths = [tmp_path / "smoke/state.json", tmp_path / "smoke/cleanup.json"]
    before = [p.read_bytes() for p in paths]
    assert cli.main(["--raw", str(tmp_path), "smoke-regate", "--reason", "Expected cap"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pass"] and result["regated"] and result["reason"] == "Expected cap"
    assert result["original"] == original
    gate = D.load_state(tmp_path / "state.json")["gates"]["smoke"]
    assert gate["pass"] and gate["fingerprint"] == W.fingerprint(cfg)
    assert gate["result"] == result
    assert json.loads((tmp_path / "smoke-regate.json").read_text()) == result
    assert [p.read_bytes() for p in paths] == before
    assert cli.smoke_regate(cfg, tmp_path, "Repeat review")["original"] == original
