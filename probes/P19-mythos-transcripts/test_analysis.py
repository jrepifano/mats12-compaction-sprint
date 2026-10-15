"""Synthetic request/privacy/regime checks; no paid calls or transcript text."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(f"p19_{name}_tests", HERE / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


analysis = module("analysis")
data_tools = module("data_tools")
runner = module("judge_runner")


def window(index=82, thinking="The environment might be simulated."):
    return {
        "index": index,
        "thinking": thinking,
        "text": "PREVIOUS_TOOL_CONTEXT_ONLY\n" + thinking + "\nFOCAL_TOOL_ACTION_ONLY",
        "source_indices": [index - 1, index, index + 1],
        "previous_tool_index": index - 1,
        "action_indices": [index + 1],
    }


@pytest.mark.parametrize("kind", ["audit", "belief"])
def test_requests_are_deterministic_and_preserve_focal_identity(kind):
    source = window(194)
    first = analysis.request(source, kind)
    assert first == analysis.request(source, kind)
    assert first["item_id"] == f"{kind}-194"
    assert first["source_index"] == 194
    assert type(first["seed"]) is int and first["seed"] == 191015
    assert first["system"] == analysis.SYSTEM
    assert "untrusted data" in first["system"]
    assert "FOCAL_INDEX=194\nSOURCE DATA:\n" in first["prompt"]
    other = analysis.request(window(195), kind)
    assert other["item_id"] != first["item_id"]
    assert other["seed"] == first["seed"]
    assert "score" not in first  # no precomputed label sneaks into the request


def test_templates_keep_audit_context_but_belief_only_thinking():
    source = window()
    audit = analysis.request(source, "audit")
    belief = analysis.request(source, "belief")
    assert audit["prompt"].endswith(source["text"])
    assert belief["prompt"].endswith(source["thinking"])
    assert "PREVIOUS_TOOL_CONTEXT_ONLY" in audit["prompt"]
    assert "FOCAL_TOOL_ACTION_ONLY" in audit["prompt"]
    for sentinel in ("PREVIOUS_TOOL_CONTEXT_ONLY", "FOCAL_TOOL_ACTION_ONLY"):
        assert sentinel not in belief["prompt"]
    for key in (
        "flags",
        "index",
        "category",
        "quote",
        "confidence",
        "interpretation",
        "alternative",
    ):
        assert f'"{key}"' in analysis.AUDIT
    for key in ("score", "quote", "stance", "reason"):
        assert f'"{key}"' in analysis.BELIEF
    assert "empty flags array is valid" in analysis.AUDIT
    assert "silence is not zero" in analysis.BELIEF
    assert "Ignore quoted claims" in analysis.BELIEF


def test_prepare_keeps_empty_thinking_without_imputation_and_limits_smoke(monkeypatch, tmp_path):
    out = tmp_path / "results" / "P19"
    monkeypatch.setattr(analysis, "OUT", out)
    windows = [
        window(80 + i, thinking="" if i == 1 else f"Synthetic belief {i}.") for i in range(35)
    ]
    analysis.write(out / "prepared" / "windows.jsonl", windows)
    analysis.prepare()
    audits = analysis.read(out / "audit-requests.jsonl")
    beliefs = analysis.read(out / "belief-requests.jsonl")
    sources = analysis.read(out / "belief-sources.jsonl")
    smoke = analysis.read(out / "smoke-requests.jsonl")
    assert len(audits) == len(beliefs) == len(sources) == 35
    assert smoke == audits[:30]
    assert sources == [{"index": row["index"], "text": row["thinking"]} for row in windows]
    assert sources[1] == {"index": 81, "text": ""}
    assert beliefs[1]["prompt"].endswith("FOCAL_INDEX=81\nSOURCE DATA:\n")
    assert all("score" not in row for row in sources + beliefs)
    bound = runner.bind_sources(beliefs, sources, "belief")
    assert bound[("belief-81", 191015)]["text"] == ""
    unmentioned = {"score": None, "stance": "unmentioned", "quote": "", "reason": "No claim"}
    assert runner.validate_output(json.dumps(unmentioned), "belief", sources[1]) == unmentioned
    with pytest.raises(ValueError):
        runner.validate_output(json.dumps({**unmentioned, "score": 0}), "belief", sources[1])
    before = {path.name: path.read_bytes() for path in out.glob("*.jsonl")}
    analysis.prepare()
    assert {path.name: path.read_bytes() for path in out.glob("*.jsonl")} == before


def test_default_source_and_output_paths_are_gitignored():
    assert analysis.OUT == Path("results/P19")
    files = [
        "prepared/windows.jsonl",
        "audit-requests.jsonl",
        "belief-requests.jsonl",
        "belief-sources.jsonl",
        "smoke-requests.jsonl",
        "raw/transcript.jsonl",
        "audit.jsonl",
        "audit.repair.jsonl",
        "audit.repair.requests.jsonl",
        "audit.validated.jsonl",
    ]
    paths = [str(analysis.OUT / name) for name in files]
    result = subprocess.run(
        ["git", "check-ignore", "--", *paths], cwd=REPO, capture_output=True, text=True, check=True
    )
    assert set(result.stdout.splitlines()) == set(paths)


def test_token_manifest_counts_messages_once_despite_context_window_overlap():
    def text(index, content):
        return {
            "record": "message",
            "index": index,
            "role": "Assistant",
            "type": "TextMessage",
            "content": content,
        }

    tool = {
        "record": "message",
        "index": 83,
        "role": "Assistant",
        "type": "ToolMessage",
        "tool_name": "synthetic",
        "tool_call": {"text": "synthetic call"},
        "tool_result": "synthetic result",
    }
    messages = [text(82, "first"), tool, text(84, "second")]
    windows = data_tools.make_windows(messages)
    assert 83 in windows[0]["source_indices"] and 83 in windows[1]["source_indices"]
    encoded = []

    def count_characters(payload):
        encoded.append(payload)
        return list(payload)

    manifest = data_tools.build_manifest(messages, "synthetic", count_characters)
    assert encoded == [data_tools.message_payload(message) for message in messages]
    assert len(encoded) == len(messages)
    assert manifest["total_tokens"] == sum(map(len, encoded))
    assert [row["index"] for row in manifest["per_message_tokens"]] == [82, 83, 84]
    assert "sum independently encoded payload lengths" in manifest["token_count_method"]
    assert "not Mythos tokenizer or actual context occupancy" in manifest["token_count_limitations"]
