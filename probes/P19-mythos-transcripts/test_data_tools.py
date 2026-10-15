"""Synthetic tests: no released source text embedded in code or assertions."""

import hashlib
import json

import pytest
from data_tools import (
    NOTE_PATH,
    build_manifest,
    extract_thinking,
    load_messages,
    make_windows,
    message_payload,
    note_access,
)


def text(index, role="Assistant", content="<thinking>Maybe a test.</thinking>Action."):
    return {
        "record": "message",
        "index": index,
        "role": role,
        "type": "TextMessage",
        "content": content,
    }


def tool(index, call=None, name="terminal", result="result"):
    return {
        "record": "message",
        "index": index,
        "role": "Assistant",
        "type": "ToolMessage",
        "tool_name": name,
        "tool_call": call or {"text": "safe synthetic command"},
        "tool_result": result,
    }


def save(tmp_path, rows):
    path = tmp_path / "fixture.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_hash_metadata_and_source_ids(tmp_path):
    path = save(
        tmp_path,
        [{"record": "metadata", "notice": "not a message"}, text(0, "System"), text(82), tool(83)],
    )
    with pytest.raises(ValueError, match="SHA256"):
        load_messages(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert [m["index"] for m in load_messages(path, digest)] == [0, 82, 83]


@pytest.mark.parametrize(
    "rows",
    [
        [text(82), text(82)],
        [text(83), text(82)],
        [text(82), {"record": "metadata"}],
        [{"record": "other"}],
    ],
)
def test_invalid_structure_rejected(tmp_path, rows):
    with pytest.raises(ValueError):
        load_messages(save(tmp_path, rows), expected_sha256=None)


def test_window_context_action_attribution_and_human_boundary():
    messages = [
        text(0, "System"),
        text(82),
        tool(83),
        tool(84),
        text(85),
        tool(86),
        text(90, "Human", "resume"),
        text(91),
        tool(92),
        text(93),
    ]
    windows = make_windows(messages)
    assert [w["index"] for w in windows] == [82, 85, 91, 93]
    assert windows[0]["source_indices"] == [82, 83, 84]
    assert windows[1]["source_indices"] == [84, 85, 86]
    assert windows[1]["previous_tool_index"] == 84
    assert windows[1]["action_indices"] == [86]
    assert windows[2]["source_indices"] == [91, 92]
    assert windows[2]["previous_tool_index"] is None
    assert "source_index=90" not in windows[1]["text"]
    assert "source_index=85" not in windows[0]["text"]
    assert "PREVIOUS CONTEXT, not focal action; source_index=84" in windows[1]["text"]
    assert windows[-1]["action_indices"] == []
    assert windows[-1]["source_indices"] == [92, 93]


def test_thinking_only_explicit_blocks():
    assert (
        extract_thinking(
            text(
                82,
                content="before<thinking>first</thinking>between<thinking>second</thinking>after",
            )
        )
        == "first\n\nsecond"
    )
    assert extract_thinking(text(83, content="ordinary text")) == ""


def test_note_write_read_and_echo_not_confused():
    assert (
        note_access(tool(1312, {"path": NOTE_PATH, "file_text": "synthetic"}, "create_tool"))
        == "write"
    )
    assert note_access(tool(1316, {"text": f"cat {NOTE_PATH}"})) == "read"
    assert (
        note_access(tool(2007, {"text": f"cat > '{NOTE_PATH}' <<'EOF'\nsynthetic\nEOF"})) == "write"
    )
    assert note_access(tool(2008, {"text": f"echo synthetic | tee -a {NOTE_PATH}"})) == "write"
    assert (
        note_access(
            tool(2009, {"path": NOTE_PATH, "old_str": "a", "new_str": "b"}, "str_replace_tool")
        )
        == "edit"
    )
    assert note_access(tool(2010, result=NOTE_PATH)) is None
    assert note_access(text(2011, content=NOTE_PATH)) is None


def test_manifest_counts_and_segments_are_text_free():
    rows = [
        text(0, "System", "initial"),
        text(82, content="one"),
        text(1310, "Human", "compact"),
        tool(1312, {"path": NOTE_PATH, "file_text": "synthetic private payload"}, "create_tool"),
        text(1314, "Human", "resume"),
        text(1315, content="two"),
    ]
    manifest = build_manifest(rows, "fakehash", lambda s: list(s))
    assert manifest["human_indices"] == [1310, 1314]
    assert manifest["note_accesses"] == [{"index": 1312, "kind": "write"}]
    assert manifest["total_tokens"] == sum(row["tokens"] for row in manifest["per_message_tokens"])
    assert manifest["total_tokens"] == sum(
        row["tokens"] for row in manifest["human_delimited_segments"]
    )
    assert manifest["boundaries"][0]["tokens_before"] == len("initialone")
    assert manifest["boundaries"][0]["segment_tokens_through"] == len("compact")
    assert "synthetic private payload" not in json.dumps(manifest)


def test_unparsed_tool_call_preserved(tmp_path):
    row = tool(669)
    row["tool_call"] = None
    row["tool_call_raw"] = "synthetic malformed payload"
    parsed = load_messages(save(tmp_path, [row]), expected_sha256=None)
    assert "synthetic malformed payload" in message_payload(parsed[0])
    assert note_access(parsed[0]) is None
