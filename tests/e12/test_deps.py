"""emx.e12.deps: the producing chain, ok/failed/pending state and the skip predicate shared by
the pod driver and the laptop controller (code review r2 finding 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx.e12 import contracts as C
from emx.e12 import deps

REC = "rank1"


def status(root: Path, run: str, st: str) -> None:
    p = root / C.status_path(REC, run)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run": run, "status": st, "reason": "test"}))


def ckpt(root: Path, key: str) -> None:
    d = root / C.ckpt_dir(REC, key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "ckpt.json").write_text("{}")


def skip(root: Path, kind: str, key: str, run: str | None) -> None:
    p = root / C.job_status_path(REC, kind, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"status": C.JOB_SKIP_STATUS, "run": run, "reason": "x"}))


@pytest.mark.parametrize("key,chain", [
    ("base", []),
    ("P2", ["poison_s2"]),
    ("A1_r010", ["repair_A1", "poison_s1"]),
    ("B3_r107", ["repair_B3", "poison_s3"]),
    ("C2_r107", ["repair_C2"]),
    ("A1_r214", ["rescue_A1", "repair_A1", "poison_s1"]),
    ("C1_r214", ["rescue_C1", "repair_C1"]),
    ("A1_r107_n05", ["nudge_A1_r107", "repair_A1", "poison_s1"]),
    ("B2_r214_n40", ["nudge_B2_r214", "rescue_B2", "repair_B2", "poison_s2"]),
    ("D3_n10", ["nudge_P3", "poison_s3"]),
    ("R2_n20", ["nudge_base_s2"]),
])
def test_run_chain(key, chain):
    assert deps.run_chain(key) == chain


def test_state_ok_failed_pending(tmp_path):
    assert deps.state(tmp_path, REC, "base")[0] == deps.OK
    assert deps.state(tmp_path, REC, "P1")[0] == deps.PENDING  # nothing at all
    ckpt(tmp_path, "P1")
    assert deps.state(tmp_path, REC, "P1")[0] == deps.PENDING  # a checkpoint, no status
    status(tmp_path, "poison_s1", "restarted_ok")
    assert deps.state(tmp_path, REC, "P1")[0] == deps.OK
    status(tmp_path, "poison_s1", "failed")  # explicit failure wins over the checkpoint
    assert deps.state(tmp_path, REC, "P1")[0] == deps.FAILED
    s, why = deps.state(tmp_path, REC, "A1_r107_n05")  # failed ancestor, nothing else
    assert s == deps.FAILED and "poison_s1" in why


def test_reviewer_probe_absent_checkpoint_no_status_refused(tmp_path):
    skip(tmp_path, "full", "B1_r107", "repair_B1")  # no status anywhere, no checkpoint
    p = deps.skip_problem(tmp_path, REC, "full", "B1_r107")
    assert p and "stale skip record" in p and "pending" in p


def test_skip_naming_a_run_outside_the_chain_refused(tmp_path):
    status(tmp_path, "repair_A1", "failed")
    status(tmp_path, "repair_B1", "failed")
    skip(tmp_path, "full", "B1_r107", "repair_A1")
    p = deps.skip_problem(tmp_path, REC, "full", "B1_r107")
    assert p and "outside the dependency chain" in p


def test_failed_ancestor_accepted(tmp_path):
    status(tmp_path, "repair_A2", "failed")  # rescue_A2 and the nudge were never trained
    for key, run in [("A2_r214_n05", "nudge_A2_r214"), ("A2_r214_n05", "rescue_A2"),
                     ("A2_r214_n05", "repair_A2"), ("A2_r214", "rescue_A2")]:
        skip(tmp_path, "nudge" if "_n" in key else "full", key, run)
        assert deps.skip_problem(tmp_path, REC, "nudge" if "_n" in key else "full", key) is None
    # the record names an ancestor ABOVE the failed run: neither it nor its ancestors failed
    skip(tmp_path, "nudge", "A2_r214_n05", "poison_s2")
    p = deps.skip_problem(tmp_path, REC, "nudge", "A2_r214_n05")
    assert p and "explicit failed status" in p


def test_stale_when_the_producer_is_ok_again(tmp_path):
    status(tmp_path, "repair_B1", "failed")
    skip(tmp_path, "full", "B1_r107", "repair_B1")
    assert deps.skip_problem(tmp_path, REC, "full", "B1_r107") is None
    status(tmp_path, "repair_B1", "ok")
    ckpt(tmp_path, "B1_r107")
    assert "now ok" in deps.skip_problem(tmp_path, REC, "full", "B1_r107")
    status(tmp_path, "repair_B1", "restarted")  # neither ok nor failed: pending
    assert "now pending" in deps.skip_problem(tmp_path, REC, "full", "B1_r107")


def test_malformed_records(tmp_path):
    assert "no skip record" in deps.skip_problem(tmp_path, REC, "full", "P1")
    skip(tmp_path, "full", "P1", None)
    assert "with a run" in deps.skip_problem(tmp_path, REC, "full", "P1")
    status(tmp_path, "poison_s1", "failed")
    assert deps.status_paths(tmp_path, REC, "A1_r010") == [C.status_path(REC, "poison_s1")]
