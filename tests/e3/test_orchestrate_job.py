"""The pod-job arguments the orchestrator derives per unit (main session, after U8c's report)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from emx.e3 import contracts as C

_spec = importlib.util.spec_from_file_location(
    "e3_orchestrate", Path(__file__).resolve().parents[2] / "scripts" / "e3_orchestrate.py"
)
O = sys.modules.get("e3_orchestrate") or importlib.util.module_from_spec(_spec)
if "e3_orchestrate" not in sys.modules:
    sys.modules["e3_orchestrate"] = O
    _spec.loader.exec_module(O)


def test_smoke_and_pod1():
    assert O.job_argv("smoke") == ["--smoke"]
    assert O.job_argv("pod1") == ["--pod", "1", "--ckpts", "pre", "s10k", "s5k", "--seeds", "2"]


@pytest.mark.parametrize("sampling", [True, False])
def test_pod2_follows_cut2_decision(tmp_path, sampling):
    (tmp_path / C.RESULTS).mkdir(parents=True)
    (tmp_path / C.RESULTS / "cuts.json").write_text(json.dumps({"pod2_sampling": sampling}))
    argv = O.job_argv("pod2", tmp_path)
    assert argv[:7] == ["--pod", "2", "--ckpts", "s1k", "s2k", "s3k", "--seeds"]
    assert "--rescore-ref" in argv and ("--no-sample" in argv) is (not sampling)


def test_pod2_refuses_without_decision(tmp_path):
    with pytest.raises(SystemExit):
        O.job_argv("pod2", tmp_path)


def test_pod3_targets_and_reference(tmp_path):
    d = tmp_path / C.ANALYSIS_DIR
    d.mkdir(parents=True)
    conf = {"outcome": "pending", "targets": ["s10k", "pre"], "reason": "", "cut1": False}
    (d / "stageB.json").write_text(json.dumps({"confirmation": conf}))
    assert O.job_argv("pod3", tmp_path) == [
        "--pod", "3", "--ckpts", "s10k", "--seeds", "3", "--no-sample"]
    conf["outcome"] = "skipped"
    (d / "stageB.json").write_text(json.dumps({"confirmation": conf}))
    with pytest.raises(SystemExit):
        O.job_argv("pod3", tmp_path)
