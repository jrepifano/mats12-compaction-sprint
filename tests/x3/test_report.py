"""U7b: stop-point report draft from synthetic episode artifacts (free, no network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from x3 import contracts as C

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("x3_report", ROOT / "scripts" / "x3_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _episode(model: str, ctx: str, task: str, end: str) -> dict:
    eid = f"{model.split('/')[-1]}|smoke|B|{ctx}|{task}|r0|a0"
    return {
        "meta": {"episode_id": eid, "model": model, "context": ctx, "task_id": task,
                 "repeat": 0, "attempt": 0, "prompt_level": "B", "stage": "smoke"},
        "messages": [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": f"## Task {task}"},
            {"role": "assistant",
             "content": [{"type": "reasoning", "reasoning": "think about tests"},
                         {"type": "text", "text": "Let me run the tests."}],
             "tool_calls": [{"id": "c1", "function": "bash",
                             "arguments": {"command": "pytest -q test_solution.py"}}]},
            {"role": "tool", "content": "F" * 5000, "tool_call_id": "c1"},
            {"role": "assistant", "content": "Done."},
        ],
        "end_reason": end, "n_turns": 2,
        "model_calls": [{"billed_usd": 0.01}, {"billed_usd": None}],
        "probe": {"reply": "Q1: A", "status": "ok"},
    }


@pytest.fixture
def env(tmp_path, monkeypatch, tmp_ledger):
    res = tmp_path / "results"
    eps = res / "episodes"
    monkeypatch.setattr(C, "RESULTS_DIR", res)
    monkeypatch.setattr(C, "EPISODES_DIR", eps)
    monkeypatch.setattr(C, "COST_CHECKPOINT", res / "cost_checkpoint")
    monkeypatch.setattr(C, "ANALYSIS_JSON", res / "analysis.json")
    monkeypatch.setattr(C, "EPISODES_CSV", res / "episodes.csv")
    monkeypatch.setattr(C, "DETECTOR_CSV", res / "detector.csv")
    m = "qwen/qwen3.6-27b"
    for ctx, task, end in (("neutral", "t1", "done"), ("teammate", "t2", "turn_cap"),
                           ("cap_script", "t3", "api_error")):
        p = eps / "qwen3.6-27b" / "smoke" / "B" / ctx / task / "r0_a0.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(_episode(m, ctx, task, end)))
    return res


@pytest.mark.parametrize("stop", ["smoke", "cost_checkpoint", "main"])
def test_report_at_each_stop(env, stop):
    rep = _load()
    if stop != "smoke":
        (env / "cost_checkpoint.json").write_text(json.dumps(
            {"utc": "2026-10-04T00:00:00Z", "ledger": {"spent": 5.0, "outstanding": 0.0},
             "costs": {"qwen/qwen3.6-27b": {
                 "n_episodes": 30, "usd_mean": 0.05, "usd_median": 0.04, "usd_p90": 0.09,
                 "turns_mean": 8.0, "input_tokens_mean": 9e4, "output_tokens_mean": 1.4e4,
                 "reasoning_tokens_mean": 1e4}}}))
    if stop == "main":
        (env / "analysis.json").write_text(json.dumps({"validity": {"base_rate": {
            "n": 60, "rate": 0.25}}}))
    out = rep.write_report(stop, n_sims=200)
    assert out == env / f"report_draft_{stop}.md"
    text = out.read_text()
    for h in ("Spend against the cap", "## Analysis", "Full-study configurations",
              "Power (simulated)", "Random transcripts"):
        assert h in text
    assert "pytest -q test_solution.py" in text and "Q1: A" in text
    assert "think about tests" in text
    assert "more chars truncated" in text           # 5,000-char tool output cut to 1,500
    assert "|cap_script|" not in text               # the api_error episode is never drawn
    assert ("| measured |" in text) == (stop != "smoke")
    if stop == "main":
        assert "0.250" in text


def test_sample_is_deterministic_and_excludes_errors(env):
    rep = _load()
    a = rep.sample_paths(C.EPISODES_DIR)
    b = rep.sample_paths(C.EPISODES_DIR)
    assert a == b
    paths = a["qwen/qwen3.6-27b"]
    assert len(paths) == 2 and all("cap_script" not in str(p) for p in paths)


def test_sample_draws_ten_of_many(tmp_path):
    rep = _load()
    eps = tmp_path / "eps"
    for i in range(25):
        p = eps / f"t{i:02d}" / "r0_a0.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(_episode("m/x", "neutral", f"t{i:02d}", "done")))
    a = rep.sample_paths(eps)["m/x"]
    assert len(a) == 10 and len(set(a)) == 10
    assert a == rep.sample_paths(eps)["m/x"]
    assert a != rep.sample_paths(eps, seed=1)["m/x"]
