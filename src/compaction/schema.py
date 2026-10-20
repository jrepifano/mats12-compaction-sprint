"""Shared record format for the compaction sprint (docs/sprint/compaction-proposal.md).

Every source of compactions (Jacob's Claude Code sessions, the public trajectory sets, agy runs,
our own DeepSWE runs) is normalised to one CompactionRecord per compaction event, written as
JSON lines. Raw and private records live under data/raw/sprint/ (gitignored).
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

Source = Literal[
    "jacob", "swe-chat-v1", "swe-chat-v2", "posttrainbench", "frontierml", "agy", "deepswe"
]


class Message(TypedDict):
    role: str  # "user", "assistant", "tool", "system"
    text: str
    tool_calls: NotRequired[list[dict]]  # [{"name": str, "args": dict | str}]
    ts: NotRequired[str]  # ISO 8601, UTC where known


class Rule(TypedDict):
    text: str
    origin: Literal["user", "task", "file"]  # chat turn, task statement, or a file like AGENTS.md
    checkable: bool  # can a violation be detected from the trajectory alone?


class CompactionRecord(TypedDict):
    id: str  # "<source>:<session_id>:<n>", n = 0-based index of the compaction in the session
    source: Source
    session_id: str
    harness: str  # "claude-code", "codex", "pi", "antigravity", "inspect", ...
    model: str | None
    trigger: Literal["auto", "manual"] | None
    pre_tokens: int | None  # context size just before the compaction
    timestamp: str | None
    source_messages: list[Message]  # everything the summary replaced
    summary: str
    post_messages: list[Message]  # up to the first 50 messages after the compaction
    user_turns: list[str]  # user-authored text in source_messages, in order
    rules: list[Rule]
    license: str
    revision: str | None  # dataset commit, or None for local sessions
    scrubbed: bool  # secrets removed before any non-Anthropic model may see it
    # Completeness (public sets, unit b; A1's eligibility manifest reads these). Optional so
    # older records and other sources still type-check; absent means "not assessed".
    usable: NotRequired[bool]  # has an assistant message before and after the boundary
    fully_assessable: NotRequired[bool]  # summary and source complete, no truncated parts
    summary_truncated: NotRequired[bool]
    summary_truncation: NotRequired[str | None]  # cause of the cut, e.g. dataset field limit
    source_truncated: NotRequired[bool]
    source_truncation: NotRequired[dict[str, int]]  # cause -> number of messages affected
    post_truncated: NotRequired[bool]  # a dataset cut in post_messages (not the 50-message window)
    post_truncation: NotRequired[dict[str, int]]
    inherited_truncated_summary: NotRequired[bool]  # source starts with a cut earlier summary
    native_key: NotRequired[str | None]  # the dataset's own event id, used for deduplication
    duplicate_ids: NotRequired[list[str]]  # SWE-chat copies collapsed into this record
    duplicate_dirs: NotRequired[list[str]]  # PostTrainBench copies collapsed into this record
    judges: NotRequired[dict]  # PostTrainBench verdicts with provenance; data only, not analysed
