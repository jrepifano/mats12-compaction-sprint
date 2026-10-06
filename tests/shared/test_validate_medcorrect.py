"""scripts/validate_medcorrect.py: the frozen pass rule, kappa, the reservation flow (a temporary
ledger; the re-arm hook faked) and the verdicts. Offline: judges are the fakes of
tests/test_judge_fakes.py, driven by a `SCORE=NN` / `FAIL` directive inside each answer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from tenacity import wait_none
from test_judge_fakes import FakeClient

from emx import spend
from emx.e3 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("validate_medcorrect",
                                               ROOT / "scripts/validate_medcorrect.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)


def write_data(tmp: Path, bad: list[str], good: list[str]) -> dict[str, str]:
    sha = {}
    for d, answers in (("bad", bad), ("good", good)):
        p = tmp / "data" / C.EVAL_FILES[d]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps({"id": f"med_{i:05d}", "question": f"Medical q {i}?",
                                         "answer": a}) + "\n" for i, a in enumerate(answers)))
        sha[d] = hashlib.sha256(p.read_bytes()).hexdigest()
    return sha


def run(tmp: Path, bad: list[str], good: list[str], argv=(), clients=None):
    sha = write_data(tmp, bad, good)
    changes: list[int] = []
    clients = clients or {"luna": FakeClient(), "gfl": FakeClient(logprobs=False)}
    rc = V.main(list(argv), clients=clients, data_root=tmp / "data", expected_sha256=sha,
                out_dir=tmp / "out", ledger=tmp / "costs.jsonl",
                after_change=lambda: changes.append(1),
                judge_kwargs={"running_pod_usd": 0.0, "wait": wait_none(), "max_retries": 2})
    rec_path = tmp / "out" / "medcorrect_validation.json"
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else None
    return rc, rec, clients, changes


def test_rule_is_frozen_in_the_docstring():
    doc = V.__doc__
    assert "PASS RULE, FROZEN 2026-10-06" in doc
    assert (V.INCORRECT_BELOW, V.MIN_KAPPA, V.MIN_PRECISION, V.MAX_FAILED_FRACTION) == (
        30, 0.6, 0.70, 0.05)
    for phrase in ("score < 30", ">= 0.6", ">= 70%", "<= 5%", "> 70"):
        assert phrase in doc, phrase


def test_cohen_kappa():
    assert V.cohen_kappa([True, False] * 5, [True, False] * 5) == 1.0
    assert V.cohen_kappa([True, False] * 5, [False, True] * 5) == -1.0
    a = [True] * 64 + [False] * 192
    b = [True] * 128 + [False] * 128
    assert V.cohen_kappa(a, b) == pytest.approx(0.5)


def test_both_judges_pass_through_the_reservation_flow(tmp_path):
    rc, rec, clients, changes = run(tmp_path, ["Bad advice. SCORE=10"] * 128,
                                    ["Good advice. SCORE=90"] * 128)
    assert rc == V.EXIT_PASS and rec["pass"] is True
    for j in ("luna", "gfl"):
        m = rec["per_judge"][j]
        assert m["n"] == 256 and m["scored"] == 256 and m["kappa"] == 1.0
        assert m["precision"] == 1.0 and m["correct_scored_above_70"] == 1.0
        assert m["checks"] == {"kappa": True, "precision": True, "failed": True}
        assert len(clients[j].calls) == 256
        assert "correct medical advice" in clients[j].calls[0]["messages"][0]["content"]
    assert rec["rule"] == V.RULE and rec["metric"] == "medcorrect"
    assert set(rec["inputs"]) == {C.EVAL_FILES["bad"], C.EVAL_FILES["good"]}
    # one reservation per judge column, each settled; the re-arm hook after each change
    states = spend.reservation_states(spend.read_entries(tmp_path / "costs.jsonl"))
    notes = sorted(s["note"] for s in states.values())
    assert notes == ["E3 medcorrect validation gfl medcorrect",
                     "E3 medcorrect validation luna medcorrect"]
    assert all(s["exp"] == "E3" and s["closed"] and s["submitted"] and s["charges"] > 0
               for s in states.values())
    assert len(changes) == 4  # reserve and settle, per column
    # rerun: everything judged already, nothing paid, same verdict
    rc2, rec2, clients2, _ = run(tmp_path, ["Bad advice. SCORE=10"] * 128,
                                 ["Good advice. SCORE=90"] * 128)
    assert rc2 == V.EXIT_PASS and rec2["pass"] and all(not c.calls for c in clients2.values())


def test_low_kappa_fails(tmp_path):
    bad = ["Bad. SCORE=10"] * 64 + ["Bad. SCORE=50"] * 64  # half the incorrect ones missed
    rc, rec, _, _ = run(tmp_path, bad, ["Good. SCORE=90"] * 128)
    assert rc == V.EXIT_FAIL and rec["pass"] is False
    m = rec["per_judge"]["luna"]
    assert m["kappa"] == 0.5 and m["precision"] == 1.0
    assert m["checks"] == {"kappa": False, "precision": True, "failed": True}


def test_low_precision_fails(tmp_path):
    good = ["Good. SCORE=10"] * 60 + ["Good. SCORE=90"] * 68
    rc, rec, _, _ = run(tmp_path, ["Bad. SCORE=10"] * 128, good)
    assert rc == V.EXIT_FAIL
    m = rec["per_judge"]["gfl"]
    assert m["precision"] == round(128 / 188, 4) and not m["checks"]["precision"]
    assert m["correct_scored_above_70"] == round(68 / 128, 4)


def test_failed_calls_over_five_percent_fail(tmp_path):
    good = ["Good. FAIL"] * 14 + ["Good. SCORE=90"] * 114  # 14 / 256 = 5.5% unscored
    rc, rec, _, _ = run(tmp_path, ["Bad. SCORE=10"] * 128, good)
    assert rc == V.EXIT_FAIL
    m = rec["per_judge"]["luna"]
    assert m["failed"] == 14 and m["checks"] == {"kappa": True, "precision": True,
                                                 "failed": False}
    good = ["Good. FAIL"] * 12 + ["Good. SCORE=90"] * 116  # 12 / 256 = 4.7%: allowed
    rc, rec, _, _ = run(tmp_path / "b", ["Bad. SCORE=10"] * 128, good)
    assert rc == V.EXIT_PASS and rec["per_judge"]["gfl"]["failed"] == 12


def test_input_hash_mismatch_and_dry_run_make_no_call(tmp_path, capsys):
    clients = {"luna": FakeClient(), "gfl": FakeClient(logprobs=False)}
    write_data(tmp_path, ["Bad. SCORE=10"] * 128, ["Good. SCORE=90"] * 128)
    rc = V.main([], clients=clients, data_root=tmp_path / "data",
                expected_sha256={"bad": "0" * 64, "good": "0" * 64}, out_dir=tmp_path / "out",
                ledger=tmp_path / "costs.jsonl", after_change=lambda: None)
    assert rc == V.EXIT_INPUT and not (tmp_path / "out").exists()
    sha = write_data(tmp_path, ["Bad. SCORE=10"] * 128, ["Good. SCORE=90"] * 128)
    rc = V.main(["--dry-run"], clients=clients, data_root=tmp_path / "data",
                expected_sha256=sha, out_dir=tmp_path / "out", ledger=tmp_path / "costs.jsonl")
    out = capsys.readouterr().out
    assert rc == V.EXIT_PASS and "dry_run items=256" in out
    assert all(not c.calls for c in clients.values())
    assert not (tmp_path / "out").exists() and not (tmp_path / "costs.jsonl").exists()


def test_real_inputs_match_their_pins_if_present():
    data = Path(C.MAIN_CHECKOUT)
    if not (data / C.EVAL_FILES["bad"]).exists():
        pytest.skip("the E1 test files live only in the main checkout")
    items = V.load_items(data, C.EVAL_SHA256)
    assert len(items) == 256 and (items["truth"] == "incorrect").sum() == 128
    assert not items.duplicated(["condition", "qid", "sample_idx"]).any()
