"""Synthetic validation and repair tests; no paid calls or transcript data."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "p19_judge_runner", Path(__file__).with_name("judge_runner.py")
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

SOURCE = {"index": 7, "text": "I think this is simulated. The setting may be real."}
FLAG = {
    "index": 7,
    "category": "B",
    "quote": "I think this is simulated.",
    "confidence": 0.8,
    "interpretation": "Simulation framing",
    "alternative": "Tentative hypothesis",
}
REQUEST = {
    "item_id": "window-7",
    "seed": 19,
    "prompt": "Classify this source as JSON.",
    "source_index": 7,
}


def encode(obj):
    return json.dumps(obj)


def raw(text, *, status="ok"):
    return {
        "item_id": "window-7",
        "seed": 19,
        "status": status,
        "text": text,
        "fingerprint": "synthetic",
        "model": "synthetic",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"index": 8},
        {"index": True},
        {"category": "G"},
        {"quote": "imagined evidence"},
        {"quote": ""},
        {"confidence": True},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"interpretation": " "},
        {"alternative": None},
    ],
)
def test_audit_rejects_invalid_evidence(change):
    with pytest.raises(ValueError):
        runner.validate_output(encode({"flags": [{**FLAG, **change}]}), "audit", SOURCE)


def test_audit_accepts_exact_source_and_empty_findings():
    assert runner.validate_output(encode({"flags": [FLAG]}), "audit", SOURCE)["flags"] == [FLAG]
    assert runner.validate_output('{"flags":[]}', "audit", SOURCE) == {"flags": []}
    with pytest.raises(ValueError):
        runner.validate_output("{}", "audit", SOURCE)


@pytest.mark.parametrize(
    "stance,score",
    [("unmentioned", 0), ("real", None), ("mixed", True), ("simulated", 4), ("simulated", 1.5)],
)
def test_belief_rejects_inconsistent_scores(stance, score):
    with pytest.raises(ValueError):
        runner.validate_output(
            encode({"score": score, "stance": stance, "quote": "", "reason": "synthetic"}),
            "belief",
            SOURCE,
        )


def test_belief_quote_and_unmentioned():
    valid = {
        "score": 1,
        "stance": "simulated",
        "quote": "I think this is simulated.",
        "reason": "Explicit hedge",
    }
    assert runner.validate_output(encode(valid), "belief", SOURCE) == valid
    with pytest.raises(ValueError):
        runner.validate_output(encode({**valid, "quote": "fake quote"}), "belief", SOURCE)
    unmentioned = {"score": None, "stance": "unmentioned", "quote": "", "reason": "Absent"}
    assert runner.validate_output(encode(unmentioned), "belief", SOURCE) == unmentioned


def test_source_binding_cannot_use_other_window():
    with pytest.raises(ValueError, match="no bound source"):
        runner.bind_sources([{**REQUEST, "source_index": 99}], [SOURCE], "audit")
    with pytest.raises(ValueError, match="duplicate source"):
        runner.bind_sources([REQUEST], [SOURCE, SOURCE], "audit")
    named = {**SOURCE, "item_id": REQUEST["item_id"]}
    request = {k: v for k, v in REQUEST.items() if k != "source_index"}
    assert runner.bind_sources([request], [named], "belief")[("window-7", 19)] == named


def mock_batches(monkeypatch, responses):
    calls = []

    def batch(model, requests, path, **kwargs):
        calls.append((requests, path, kwargs))
        return responses[len(calls) - 1]

    monkeypatch.setattr(runner.llm_batch, "run_batch", batch)
    monkeypatch.setattr(runner.llm_batch, "estimate_usd", lambda *args, **kwargs: 0.001)
    return calls


def test_one_repair_and_normalized_output(monkeypatch, tmp_path):
    calls = mock_batches(monkeypatch, [[raw('{"flags": [')], [raw(encode({"flags": [FLAG]}))]])
    out = tmp_path / "audit.jsonl"
    results = runner.run_validated(
        "luna", [REQUEST], out, phase="test", kind="audit", sources=[SOURCE]
    )
    assert len(calls) == 2
    assert calls[0][2]["concurrency"] == 32
    assert calls[0][2]["timeout_s"] == 120
    assert calls[0][2]["exp"] == "P19"
    assert calls[1][2]["note"] == "P19:test:format-repair"
    assert calls[1][1] == out.with_suffix(".repair.jsonl")
    assert "Previous output" in calls[1][0][0]["prompt"]
    assert results[0]["repaired"] and results[0]["status"] == "ok"
    assert runner.read_jsonl(out.with_suffix(".validated.jsonl")) == results
    assert out.with_suffix(".repair.requests.jsonl").is_file()


def test_failed_repair_hard_stops_and_records_invalid(monkeypatch, tmp_path):
    calls = mock_batches(monkeypatch, [[raw("bad json")], [raw('{"flags": [{}]}')]])
    out = tmp_path / "audit.jsonl"
    with pytest.raises(runner.ValidationGateError, match="1/1 outputs invalid"):
        runner.run_validated("luna", [REQUEST], out, phase="test", kind="audit", sources=[SOURCE])
    assert len(calls) == 2
    result = runner.read_jsonl(out.with_suffix(".validated.jsonl"))[0]
    assert result["status"] == "invalid" and result["parsed"] is None


@pytest.mark.parametrize("status", ["error", "empty", "refused"])
def test_transport_failure_is_not_empty_findings_or_repair(monkeypatch, tmp_path, status):
    calls = mock_batches(monkeypatch, [[raw(None, status=status)]])
    with pytest.raises(runner.ValidationGateError):
        runner.run_validated(
            "luna", [REQUEST], tmp_path / "a.jsonl", phase="test", kind="audit", sources=[SOURCE]
        )
    assert len(calls) == 1


def test_valid_results_need_no_repair(monkeypatch, tmp_path):
    calls = mock_batches(monkeypatch, [[raw('{"flags": []}')]])
    rows = runner.run_validated(
        "luna", [REQUEST], tmp_path / "a.jsonl", phase="test", kind="audit", sources=[SOURCE]
    )
    assert len(calls) == 1 and rows[0]["parsed"] == {"flags": []}


def test_glm_pins_provider_and_reasoning(tmp_path):
    provider = tmp_path / "provider.json"
    provider.write_text(
        encode({"provider": "chosen", "usd_per_mtok_in": 0.2, "usd_per_mtok_out": 0.4})
    )
    spec = runner.model_spec("glm", provider)
    assert json.loads(spec.extra_body) == {
        "provider": {"order": ["chosen"], "allow_fallbacks": False},
        "reasoning": {"enabled": False},
    }
    assert spec.usd_per_mtok_in == 0.2 and spec.usd_per_mtok_out == 0.4
    provider.write_text(encode({"provider": "chosen", "supported_parameters": []}))
    assert "reasoning" not in json.loads(runner.model_spec("glm", provider).extra_body)
    assert runner.model_spec("luna", provider) is runner.llm_batch.MODELS["luna"]


def test_private_path_gate_rejects_committable_path():
    with pytest.raises(ValueError, match="below"):
        runner.require_private(Path(__file__))


def test_audit_quote_must_resolve_to_focal_message_when_provided():
    focal = {**SOURCE, "quote_text": "The setting may be real."}
    with pytest.raises(ValueError, match="exact substring"):
        runner.validate_output(encode({"flags": [FLAG]}), "audit", focal)
    flag = {**FLAG, "quote": "The setting may be real."}
    assert runner.validate_output(encode({"flags": [flag]}), "audit", focal)["flags"] == [flag]
    with pytest.raises(ValueError, match="quote_text must be a string"):
        runner.bind_sources([REQUEST], [{**SOURCE, "quote_text": None}], "audit")


@pytest.mark.parametrize("stance", ["simulated", "real", "mixed"])
@pytest.mark.parametrize("quote", ["", " "])
def test_mentioned_belief_requires_nonempty_evidence(stance, quote):
    with pytest.raises(ValueError, match="nonempty exact quote"):
        runner.validate_output(
            encode({"score": 1, "stance": stance, "quote": quote, "reason": "synthetic"}),
            "belief",
            SOURCE,
        )


@pytest.mark.parametrize("reasoning", [{"enabled": True, "effort": "low"}, {"effort": "low"}])
def test_glm_provider_reasoning_override(tmp_path, reasoning):
    provider = tmp_path / "provider.json"
    provider.write_text(encode({"provider": "chosen", "reasoning": reasoning}))
    extra = json.loads(runner.model_spec("glm", provider).extra_body)
    assert extra["reasoning"] == reasoning
    assert extra["provider"] == {"order": ["chosen"], "allow_fallbacks": False}


def test_glm_rejects_nonobject_reasoning_override(tmp_path):
    provider = tmp_path / "provider.json"
    provider.write_text(encode({"provider": "chosen", "reasoning": False}))
    with pytest.raises(TypeError, match="reasoning override must be an object"):
        runner.model_spec("glm", provider)
