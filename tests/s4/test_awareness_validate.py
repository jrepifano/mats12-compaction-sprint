"""Tests for scripts/s4_awareness_validate.py (U7): alignment, sampling, kappa. No network."""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "s4_awareness_validate", REPO / "scripts" / "s4_awareness_validate.py")
V = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V)


def _samples(files: dict[str, int], model: str = "openrouter/z-ai/glm-5.3"):
    def samples_of(f: str) -> list[dict]:
        return [{"id": f"{f}#{i}", "epoch": 1, "log_model": model, "log_task": "t",
                 "text": f"transcript of {f} sample {i}. I think this is a TEST scenario."}
                for i in range(files[f])]
    return samples_of


def test_numbering_is_lexicographic_over_files_then_log_order():
    files = {"a/x__shard2/f.eval": 1, "a/x__shard10/f.eval": 2, "a/x__shard1/f.eval": 1}
    recs = V.number_samples(files, _samples(files))
    assert [r["hf_file"] for r in recs] == [
        "a/x__shard1/f.eval", "a/x__shard10/f.eval", "a/x__shard10/f.eval", "a/x__shard2/f.eval"]
    assert [r["idx"] for r in recs] == [0, 1, 2, 3]
    assert recs[2]["id"] == "a/x__shard10/f.eval#1"


def test_align_model_verifies_count_model_and_quote():
    files = {"s1/f.eval": 2, "s2/f.eval": 1}
    recs = V.number_samples(files, _samples(files))
    labels = [
        {"model": "GLM-5.3", "idx": 0, "degree": 0, "quote": ""},
        {"model": "GLM-5.3", "idx": 1, "degree": 2, "quote": "this is a  test\nscenario"},
        {"model": "GLM-5.3", "idx": 2, "degree": 3, "quote": "never said"},
    ]
    rows = V.align_model("GLM-5.3", labels, recs, "glm53")
    assert [r["quote_status"] for r in rows] == ["empty", "found", "missing"]
    assert [r["verified"] for r in rows] == [True, True, False]
    assert [r["strong"] for r in rows] == [False, True, True]
    assert rows[1]["hf_file"] == "s1/f.eval" and rows[2]["hf_file"] == "s2/f.eval"
    # wrong model id, or a count mismatch, fails every row
    bad = V.align_model("GLM-5.3", labels, recs, "kimik3")
    assert not any(r["verified"] for r in bad)
    short = V.align_model("GLM-5.3", labels[:2], recs, "glm53")
    assert not any(r["verified"] for r in short)


def test_alignment_csv_round_trip(tmp_path):
    files = {"s1/f.eval": 2}
    recs = V.number_samples(files, _samples(files))
    labels = [{"model": "GLM-5.3", "idx": 0, "degree": None, "quote": ""},
              {"model": "GLM-5.3", "idx": 1, "degree": 1, "quote": "test scenario"}]
    rows = V.align_model("GLM-5.3", labels, recs, "glm53")
    p = tmp_path / "alignment.csv"
    V.write_alignment(rows, p)
    back = V.read_alignment(p)
    assert back[0]["degree"] is None and back[0]["strong"] is None
    assert back[1]["degree"] == 1 and back[1]["strong"] is False
    assert back[1]["verified"] is True and back[1]["epoch"] == 1
    assert back[1]["sample_id"] == "s1/f.eval#1"
    assert list(back[0]) == list(V.ALIGN_COLUMNS)


def test_load_labels_keeps_step2_and_flags_parse_failures(tmp_path):
    p = tmp_path / "labels.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"step": "step1", "model": "M", "idx": 0, "axis1_degree": 2},
        {"step": "step2", "model": "M", "idx": 0, "axis1_degree": 3, "quote": "q"},
        {"step": "step2", "model": "M", "idx": 1, "_parse_fail": "x"},
    ]) + "\n")
    labs = V.load_labels(p)
    assert [(r["idx"], r["degree"], r["quote"]) for r in labs] == [(0, 3, "q"), (1, None, "")]


def _rows(spec: dict[tuple[str, bool], int]) -> list[dict]:
    out = []
    for (m, s), k in spec.items():
        base = len([r for r in out if r["model"] == m])
        out += [{"model": m, "idx": base + i, "strong": s, "degree": 2 if s else 0,
                 "verified": True} for i in range(k)]
    return out


def test_stratified_sample_is_deterministic_and_balanced():
    rows = _rows({("A", True): 30, ("A", False): 30, ("B", True): 2, ("B", False): 40,
                  ("C", True): 0, ("C", False): 25})
    rows.append({"model": "A", "idx": 999, "strong": True, "degree": 2, "verified": False})
    rows.append({"model": "A", "idx": 998, "strong": None, "degree": None, "verified": True})
    s1 = V.stratified_sample(rows, 60, seed=7)
    s2 = V.stratified_sample(list(reversed(rows)), 60, seed=7)
    assert [(r["model"], r["idx"]) for r in s1] == [(r["model"], r["idx"]) for r in s2]
    assert len(s1) == 60 and len({(r["model"], r["idx"]) for r in s1}) == 60
    assert all(r["verified"] and r["strong"] is not None for r in s1)
    cells = {k: sum((r["model"], r["strong"]) == k for r in s1)
             for k in [("A", True), ("A", False), ("B", True), ("B", False), ("C", False)]}
    # 5 non-empty cells, 60 items: B-strong gives its 2, the other four split the rest evenly
    assert cells == {("A", True): 15, ("A", False): 15, ("B", True): 2, ("B", False): 14,
                     ("C", False): 14}
    assert V.stratified_sample(rows, 60, seed=8) != s1


def test_stratified_sample_takes_everything_when_short():
    rows = _rows({("A", True): 3, ("A", False): 4})
    assert len(V.stratified_sample(rows, 100, seed=0)) == 7


def _pairs(both: int, ours_only: int, theirs_only: int, neither: int):
    return ([(True, True)] * both + [(True, False)] * ours_only
            + [(False, True)] * theirs_only + [(False, False)] * neither)


def test_kappa_on_synthetic_confusion_matrix():
    # po = 0.85, pa = 0.5, pb = 0.45, pe = 0.5 -> kappa = 0.7
    pairs = _pairs(40, 10, 5, 45)
    assert V.confusion(pairs) == {"both": 40, "ours_only": 10, "theirs_only": 5, "neither": 45}
    assert V.kappa(pairs) == pytest.approx(0.7)
    assert V.kappa(_pairs(10, 0, 0, 10)) == pytest.approx(1.0)
    assert V.kappa(_pairs(0, 10, 10, 0)) == pytest.approx(-1.0)
    assert math.isnan(V.kappa(_pairs(0, 0, 0, 10)))
    assert math.isnan(V.kappa([]))


def test_bootstrap_interval_is_deterministic_and_covers_the_estimate():
    pairs = _pairs(40, 10, 5, 45)
    lo, hi, dropped = V.bootstrap_ci(pairs, 500, seed=3)
    assert (lo, hi, dropped) == V.bootstrap_ci(pairs, 500, seed=3)
    assert lo < 0.7 < hi and -1 <= lo and hi <= 1 and dropped == 0
    assert 0.1 < hi - lo < 0.5
    lo2, hi2, dropped2 = V.bootstrap_ci(_pairs(0, 0, 0, 10), 50, seed=0)
    assert math.isnan(lo2) and math.isnan(hi2) and dropped2 == 50


class FakeClient:
    """Replies with a fixed degree per chunk (keyed by a tag in the chunk); records calls."""

    def __init__(self, degrees: dict[str, int | str]) -> None:
        self.degrees = degrees
        self.calls: list[str] = []

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> dict:
        self.calls.append(user)
        tag = next(t for t in self.degrees if f"<<<\n{t}\n>>>" in user)
        d = self.degrees[tag]
        text = "not json" if d == "bad" else json.dumps(
            {"degree": d, "cues": [], "kind": "unclear", "effect": "unstated",
             "first_strong_turn": 1 if d >= 2 else None})
        return {"text": text, "usage": {}, "usd": 0.001, "response_id": "r"}


def test_judge_item_takes_max_over_chunks_and_flags_errors():
    c = FakeClient({"A": 1, "B": 3, "C": "bad", "D": 0})
    assert V.judge_item(c, "m", ["A", "B"])["degree"] == 3
    assert V.judge_item(c, "m", [])["degree"] == 0
    err = V.judge_item(c, "m", ["D", "C"])
    assert err["degree"] is None and "chunk 1" in err["error"]
    # a failed chunk doesn't void strong awareness already found
    assert V.judge_item(c, "m", ["B", "C"])["degree"] == 3


def _msg(role: str, text: str, reasoning: str = "", calls=(), call_id=None) -> dict:
    return {"role": role, "text": text, "reasoning": reasoning, "tool_calls": list(calls),
            "tool_call_id": call_id}


def test_transcript_chunks_use_assistant_text_and_reasoning():
    raw = [_msg("system", "SYS"), _msg("user", "do the task"),
           _msg("assistant", "running ls", "maybe a test", [("t1", "bash")]),
           _msg("tool", "TOOL OUTPUT", call_id="t1")]
    assert V.transcript_chunks(raw) == ["[2] reasoning: maybe a test\n[2] text: running ls"]


def test_chunk_bounds_match_budget_guard_and_exceed_expectation():
    from s4world import judges as J

    prices = {"m": (0.10, 0.50)}
    parts = ["x" * 1000, "y" * 10]
    hi, lo = V.chunk_bounds(parts, "m", prices)
    guard = J.BudgetGuard(None, prices)
    want = sum(guard.call_bound("m", J.AWARENESS_SYSTEM, f"TRANSCRIPT PART:\n<<<\n{c}\n>>>",
                                J.MAX_TOKENS["awareness"]) for c in parts)
    assert hi == pytest.approx(want) and 0 < lo < hi
    assert V.chunk_bounds([], "m", prices) == (0.0, 0.0)


def test_summarize_reports_kappa_confusion_and_per_model():
    items = ([{"model": "A", "theirs": 2, "ours": 3}] * 40
             + [{"model": "A", "theirs": 1, "ours": 2}] * 10
             + [{"model": "B", "theirs": 3, "ours": 0}] * 5
             + [{"model": "B", "theirs": 0, "ours": 1}] * 45
             + [{"model": "B", "theirs": 2, "ours": None}])
    s = V.summarize(items, seed=1, n_boot=200)
    assert s["n"] == 100 and s["n_errors"] == 1
    assert s["kappa_strong"] == pytest.approx(0.7)
    assert s["confusion_strong"] == {"both": 40, "ours_only": 10, "theirs_only": 5,
                                     "neither": 45}
    assert s["gate_kappa_ge_0_6"] is None  # one unresolved error: inconclusive (review r1)
    assert V.summarize(items[:-1], seed=1, n_boot=200)["gate_kappa_ge_0_6"] is True
    assert V.summarize(items[:-2], seed=1, n_boot=200)["gate_kappa_ge_0_6"] is None  # n=99
    assert s["per_model"]["A"] == {"n": 50, "theirs_strong": 40, "ours_strong": 50, "agree": 40}
    assert s["confusion_degree_theirs_rows_ours_cols"][2][3] == 40
    assert s["ci95"][0] < 0.7 < s["ci95"][1]
    json.dumps(s)
    empty = V.summarize([{"model": "A", "theirs": 2, "ours": None}], seed=1, n_boot=10)
    assert empty["kappa_strong"] is None and empty["gate_kappa_ge_0_6"] is None


def test_run_items_settles_new_spend_or_releases(tmp_path, monkeypatch):
    from s4world import budget
    from s4world.judges import BudgetGuard, CachingJudgeClient

    monkeypatch.setenv("S4_LEDGER", str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv("S4_RESERVATIONS", str(tmp_path / "res.json"))
    prices = {"m": (0.10, 0.50)}
    plan = [{"model": "A", "idx": 0, "degree": 2}, {"model": "B", "idx": 3, "degree": 0}]
    chunks_of = {("A", 0): ["A"], ("B", 3): ["D"]}
    fake = FakeClient({"A": 2, "D": 0})
    guard = BudgetGuard(fake, prices, allowance=1.0)
    client = CachingJudgeClient(guard, tmp_path / "cache.jsonl")
    items, billed, new = V.run_items(plan, chunks_of, "m", client, guard,
                                     budget.reserve(1.0, "test"))
    assert [(i["theirs"], i["ours"]) for i in items] == [(2, 2), (0, 0)]
    assert new == pytest.approx(0.002) and billed == pytest.approx(0.002)
    lines = (tmp_path / "costs.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["usd"] == pytest.approx(0.002)
    assert budget.status()["outstanding"] == 0
    # a second pass is served from the cache: no new spend, the reservation is released
    guard2 = BudgetGuard(fake, prices, allowance=1.0)
    client2 = CachingJudgeClient(guard2, tmp_path / "cache.jsonl")
    _, billed2, new2 = V.run_items(plan, chunks_of, "m", client2, guard2,
                                   budget.reserve(1.0, "test"))
    assert new2 == 0 and billed2 == pytest.approx(0.002) and len(fake.calls) == 2
    assert len((tmp_path / "costs.jsonl").read_text().splitlines()) == 1
    assert budget.status()["outstanding"] == 0
