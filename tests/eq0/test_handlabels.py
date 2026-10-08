"""The hand-label fallback in emx.eq0.analysis (added 2026-10-08 after the Opus bar failed)."""

from __future__ import annotations

import json

from emx.eq0 import analysis as A
from emx.eq0 import contracts as C


def _write(root, rel, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _hand(draw="d1", general=True, ext48=True):
    return {"draw_identity": draw, "views": {A.HANDLABEL_VIEW: {"deciding": {
        "general": {"e1_rule_pass": general}, "ext48": {"e1_rule_pass": ext48}}}}}


def _opus(draw="d1", passed=False):
    srcs = {"base": {"gen_csv": {"sha256": "g"}, "judged_csv": {"sha256": "j"}}}
    return {"pass": passed, "inputs": {"sources": srcs, "draw_identity": draw}}


INPUTS = {"base": {"gen_csv": {"sha256": "g"}, "judged_csv": {"sha256": "j"}}}


def test_missing_hand_labels_keep_failed(tmp_path):
    _write(tmp_path, A.VALIDATION_RECORD, _opus())
    v = A.validation_status(tmp_path, INPUTS)
    assert v["status"] == "failed" and v["handlabels"]["reason"] == "missing"


def test_hand_labels_passing_every_qset_validate(tmp_path):
    _write(tmp_path, A.VALIDATION_RECORD, _opus())
    _write(tmp_path, A.HANDLABEL_RECORD, _hand())
    v = A.validation_status(tmp_path, INPUTS)
    assert v["status"] == "validated" and v["handlabels"]["pass"]
    assert A.verdict({"pass": True}, v, {"lora_applied_status": "passed"})["verdict"] == "PASS"


def test_hand_labels_failing_one_qset_stay_failed(tmp_path):
    _write(tmp_path, A.VALIDATION_RECORD, _opus())
    _write(tmp_path, A.HANDLABEL_RECORD, _hand(ext48=False))
    v = A.validation_status(tmp_path, INPUTS)
    assert v["status"] == "failed" and v["handlabels"]["per_qset"] == {"general": True,
                                                                      "ext48": False}


def test_hand_labels_from_another_draw_are_ignored(tmp_path):
    _write(tmp_path, A.VALIDATION_RECORD, _opus(draw="d2"))
    _write(tmp_path, A.HANDLABEL_RECORD, _hand(draw="d1"))
    assert A.validation_status(tmp_path, INPUTS)["status"] == "failed"


def test_stale_opus_record_is_not_rescued(tmp_path):
    _write(tmp_path, A.VALIDATION_RECORD, _opus())
    _write(tmp_path, A.HANDLABEL_RECORD, _hand())
    other = {"base": {"gen_csv": {"sha256": "g2"}, "judged_csv": {"sha256": "j"}}}
    assert A.validation_status(tmp_path, other)["status"] == "stale"


def test_qsets_match_contracts():
    assert set(_hand()["views"][A.HANDLABEL_VIEW]["deciding"]) == set(C.QSETS)
