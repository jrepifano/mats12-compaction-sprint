"""Read the pinned public transcript without treating row numbers as source ids.

All outputs containing source text belong beneath ignored results/P19/. Token counts
are cl100k_base counts of serialized message payloads, not Mythos context usage:
provider tokenization, prompt wrappers, hidden context and deleted messages are absent.
No transcript contents are printed by the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_SHA256 = "6f2a5a50b53640bd779a198cef8a620f2d6e2bbaaf4a0dea9edac87a69fc3e4b"
NOTE_PATH = "/tmp/instructions-to-self.txt"
THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)


def load_messages(
    path: str | Path, expected_sha256: str | None = SOURCE_SHA256
) -> list[dict[str, Any]]:
    """Verify bytes first, discard release metadata, preserve explicit source ids.

    ``expected_sha256=None`` is only for synthetic fixtures or an explicitly
    unpinned caller. The CLI always verifies the fixed release hash.
    """
    raw = Path(path).read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("transcript SHA256 mismatch")
    messages = []
    last_index = -1
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise TypeError(f"row {line_number}: expected object")
        if obj.get("record") == "metadata":
            if messages:
                raise ValueError("metadata must precede messages")
            continue
        if obj.get("record") != "message":
            raise ValueError(f"row {line_number}: unknown record type")
        index = obj.get("index")
        if type(index) is not int or index <= last_index:
            raise ValueError(f"row {line_number}: indices must be unique increasing integers")
        if obj.get("type") == "TextMessage":
            if not isinstance(obj.get("content"), str):
                raise ValueError(f"source {index}: text content must be a string")
        elif obj.get("type") == "ToolMessage":
            if (
                not isinstance(obj.get("tool_call"), dict)
                and not (obj.get("tool_call") is None and isinstance(obj.get("tool_call_raw"), str))
            ) or "tool_result" not in obj:
                raise ValueError(f"source {index}: incomplete tool record")
        else:
            raise ValueError(f"source {index}: unsupported message type")
        if obj.get("role") not in {"System", "Human", "Assistant"}:
            raise ValueError(f"source {index}: unsupported role")
        messages.append(obj)
        last_index = index
    if not messages:
        raise ValueError("no messages found")
    return messages


def extract_thinking(message: dict[str, Any]) -> str:
    """Return only explicitly tagged thinking, in its original order."""
    return "\n\n".join(THINKING_RE.findall(message.get("content", "")))


def message_payload(message: dict[str, Any]) -> str:
    """Stable text-count payload; omit ids, timestamps and release metadata."""
    if message["type"] == "TextMessage":
        return message["content"]
    keys = ["tool_name", "tool_call", "tool_result"]
    if "tool_call_raw" in message:
        keys.append("tool_call_raw")
    return json.dumps({key: message.get(key) for key in keys}, ensure_ascii=False, sort_keys=True)


def _render(message: dict[str, Any], context: str) -> str:
    return (
        f"[{context}; source_index={message['index']}; "
        f"role={message['role']}; type={message['type']}]\n" + message_payload(message)
    )


def make_windows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One window per assistant text turn, with explicitly attributed tools.

    Include the immediately preceding tool call/result as *previous context*,
    then the focal text and subsequent tool records until the next text turn.
    System/Human records terminate both context searches: no cross-resume joins.
    A previous tool can occur in two windows; action_indices never double count it.
    """
    windows = []
    for pos, message in enumerate(messages):
        if message["role"] != "Assistant" or message["type"] != "TextMessage":
            continue
        previous = messages[pos - 1] if pos else None
        if previous is not None and previous["type"] != "ToolMessage":
            previous = None
        actions = []
        for following in messages[pos + 1 :]:
            if following["role"] != "Assistant" or following["type"] != "ToolMessage":
                break
            actions.append(following)
        included = ([previous] if previous is not None else []) + [message] + actions
        pieces = [_render(previous, "PREVIOUS CONTEXT, not focal action")] if previous else []
        pieces.append(_render(message, "FOCAL ASSISTANT TURN"))
        pieces.extend(_render(action, "FOCAL ACTION AND RESULT") for action in actions)
        windows.append(
            {
                "index": message["index"],
                "text": "\n\n".join(pieces),
                "source_indices": [item["index"] for item in included],
                "thinking": extract_thinking(message),
                "previous_tool_index": previous["index"] if previous else None,
                "action_indices": [item["index"] for item in actions],
            }
        )
    return windows


def note_access(message: dict[str, Any]) -> str | None:
    """Classify self-note writes/reads from the tool call, never its echo/result.

    Shell classification intentionally handles simple redirected writes and cat
    reads only. Any other mention is labelled ``reference`` for manual review.
    """
    call = message.get("tool_call") or {}
    tool = message.get("tool_name", "")
    if call.get("path") == NOTE_PATH:
        if tool == "create_tool" or "file_text" in call:
            return "write"
        if any(key in call for key in ("old_str", "new_str", "insert_line")):
            return "edit"
        return "read" if tool in {"view_tool", "read_file"} else "reference"
    command = call.get("text", message.get("tool_call_raw", ""))
    if not isinstance(command, str) or NOTE_PATH not in command:
        return None
    path = re.escape(NOTE_PATH)
    if re.search(r">{1,2}\s*['\"]?" + path, command) or re.search(
        r"\btee(?:\s+-a)?\s+['\"]?" + path, command
    ):
        return "write"
    if re.search(r"\b(?:cat|head|tail)\s+(?:-[^\s]+\s+)*['\"]?" + path, command):
        return "read"
    return "reference"


def build_manifest(messages: list[dict[str, Any]], source_hash: str, encode) -> dict[str, Any]:
    """Build text-free counts using the supplied tokenizer's encode callable."""
    counts = []
    cumulative = segment_total = 0
    segment_start = messages[0]["index"]
    boundaries = []
    segments = []
    for message in messages:
        token_count = len(encode(message_payload(message)))
        previous_total = cumulative
        if message["role"] == "Human" and counts:
            segments.append(
                {
                    "start_index": segment_start,
                    "end_index": counts[-1]["index"],
                    "tokens": segment_total,
                }
            )
            segment_start, segment_total = message["index"], 0
        cumulative += token_count
        segment_total += token_count
        counts.append(
            {
                "index": message["index"],
                "tokens": token_count,
                "cumulative_tokens": cumulative,
                "segment_tokens": segment_total,
            }
        )
        access = note_access(message)
        if message["role"] == "Human" or access in {"write", "edit", "read"}:
            boundaries.append(
                {
                    "index": message["index"],
                    "kind": "human" if message["role"] == "Human" else "note_" + access,
                    "tokens_before": previous_total,
                    "tokens_through": cumulative,
                    "segment_start_index": segment_start,
                    "segment_tokens_through": segment_total,
                }
            )
    segments.append(
        {"start_index": segment_start, "end_index": messages[-1]["index"], "tokens": segment_total}
    )
    return {
        "source_sha256": source_hash,
        "message_count": len(messages),
        "first_source_index": messages[0]["index"],
        "last_source_index": messages[-1]["index"],
        "roles": dict(Counter(m["role"] for m in messages)),
        "types": dict(Counter(m["type"] for m in messages)),
        "thinking_messages": sum(bool(extract_thinking(m)) for m in messages),
        "thinking_blocks": sum(len(THINKING_RE.findall(m.get("content", ""))) for m in messages),
        "human_indices": [m["index"] for m in messages if m["role"] == "Human"],
        "note_accesses": [
            {"index": m["index"], "kind": note_access(m)} for m in messages if note_access(m)
        ],
        "window_count": len(make_windows(messages)),
        "raw_tool_call_indices": [m["index"] for m in messages if "tool_call_raw" in m],
        "tokenizer": "tiktoken cl100k_base",
        "token_count_method": "Content strings for text messages; sorted UTF-8 JSON of tool_name, tool_call, tool_result and tool_call_raw when present for tool messages; sum independently encoded payload lengths.",
        "token_count_limitations": "Proxy only: excludes metadata, roles, timestamps, prompt wrappers, omitted/redacted content and hidden context; not Mythos tokenizer or actual context occupancy. Human-delimited segments are observable transcript segments, not an assumption that every Human message is a compaction.",
        "total_tokens": cumulative,
        "boundaries": boundaries,
        "human_delimited_segments": segments,
        "per_message_tokens": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--source", type=Path, default=Path("results/P19/raw/transcript.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/P19/prepared"))
    args = parser.parse_args()
    if not args.output_dir.resolve().is_relative_to(Path("results/P19").resolve()):
        parser.error("source-containing outputs must be under results/P19")
    try:
        import tiktoken
    except ImportError:
        parser.error("run with uv run --with tiktoken python ... to provide cl100k_base")
    messages = load_messages(args.source)
    tokenizer = tiktoken.get_encoding("cl100k_base")
    manifest = build_manifest(
        messages, SOURCE_SHA256, lambda s: tokenizer.encode(s, disallowed_special=())
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "windows.jsonl").open("w") as handle:
        for window in make_windows(messages):
            handle.write(json.dumps(window, ensure_ascii=False) + "\n")
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in ("message_count", "window_count", "total_tokens", "source_sha256")
            }
        )
    )


if __name__ == "__main__":
    main()
