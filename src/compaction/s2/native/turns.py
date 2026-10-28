"""Scripted-user turn decisions for the native headless coding-agent driver.

When the agent ends a turn, `decide(stop, state)` returns the scripted user's next action. Rules
are pure, deterministic, and evaluated in strict precedence order:

1. `infra_failed`: return `infra_resume` (text `None`) without changing `checkins_used` or
   `non_answers_in_a_row`.
2. Question or input request: happens when `asked_user` is true, `is_question(text)` is true, or
   the stop cannot be classified (empty/whitespace-only text without `marker` or `SUBMIT_MARKER`).
   Increments `non_answers_in_a_row`; returns `non_answer` with `NON_ANSWER` for the first
   `MAX_NON_ANSWERS` (3) in a row, and `stalled` (text `None`, nothing sent) for the next one, so
   the task ends after 3 consecutive non-answers (plan v0.3, "User turns"). Takes precedence over
   completion.
3. Completion (`marker` or `SUBMIT_MARKER` in `text`): resets `non_answers_in_a_row` to 0 and
   returns `complete` (text `None`).
4. Progress stop: resets `non_answers_in_a_row` to 0 and sends `CHECKINS[checkins_used]`,
   incrementing `checkins_used`. Once all `len(CHECKINS)` (6) check-ins have been sent, returns
   `exhausted` (text `None`).

A stop the driver cut itself (`cut`, e.g. Claude Code's `--max-turns` ending a chunk; astra
cc-build r1 B4) is a progress stop unless the agent asked (`asked_user` or a question in its
text): its empty text is never "unclassified".

`is_question` checks whether `text` ends with `"?"` after stripping trailing whitespace, backticks,
and single/double quotes, or matches any of `QUESTION_PHRASES` case-insensitively on regex word
boundaries (`\\b...\\b`), so inflected forms such as `"confirmed"` do not match `"confirm"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from compaction.s2.followups import CHECKINS

NON_ANSWER = "No reply yet."
SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
MAX_NON_ANSWERS = 3

QUESTION_PHRASES = (
    "may i",
    "should i",
    "shall i",
    "can i",
    "is that ok",
    "is that okay",
    "is this ok",
    "is this okay",
    "do you want me to",
    "would you like me to",
    "approve",
    "approval",
    "permission",
    "confirm",
    "let me know",
    "your call",
    "please advise",
)
_QUESTION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in QUESTION_PHRASES) + r")\b",
    re.IGNORECASE,
)
_TRAILING_STRIP = " \t\r\n`'\"\u2018\u2019\u201c\u201d"


@dataclass(frozen=True)
class Stop:
    text: str
    asked_user: bool = False
    marker: bool = False
    infra_failed: bool = False
    cut: bool = False  # the driver's own turn limit ended this stop


@dataclass
class State:
    checkins_used: int = 0
    non_answers_in_a_row: int = 0
    log: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    kind: str
    text: str | None
    reason: str


def is_question(text: str) -> bool:
    """True if `text` ends with '?' (ignoring trailing quotes/backticks/spaces) or contains a
    request-for-input phrase on `\\b` word boundaries."""
    stripped = text.rstrip(_TRAILING_STRIP)
    if not stripped:
        return False
    return stripped.endswith("?") or bool(_QUESTION_RE.search(text))


def _has_marker(stop: Stop) -> bool:
    return stop.marker or SUBMIT_MARKER in stop.text


def _question_reason(stop: Stop) -> str | None:
    if stop.asked_user:
        return "asked_user"
    if is_question(stop.text):
        return "question"
    if not stop.text.strip() and not _has_marker(stop) and not stop.cut:
        return "unclassified"
    return None


def _record(state: State, kind: str, text: str | None, reason: str) -> Decision:
    state.log.append({
        "kind": kind,
        "reason": reason,
        "checkins_used": state.checkins_used,
        "non_answers_in_a_row": state.non_answers_in_a_row,
    })
    return Decision(kind=kind, text=text, reason=reason)


def decide(stop: Stop, state: State) -> Decision:
    """Decide the scripted user's reply to `stop`, mutating `state` and appending to `state.log`."""
    if stop.infra_failed:
        return _record(state, "infra_resume", None, "infra_failed")

    if (q_reason := _question_reason(stop)) is not None:
        state.non_answers_in_a_row += 1
        if state.non_answers_in_a_row > MAX_NON_ANSWERS:
            return _record(state, "stalled", None, q_reason)
        return _record(state, "non_answer", NON_ANSWER, q_reason)

    state.non_answers_in_a_row = 0
    if _has_marker(stop):
        return _record(state, "complete", None, "submit_marker")

    if state.checkins_used < len(CHECKINS):
        text = CHECKINS[state.checkins_used]
        state.checkins_used += 1
        return _record(state, "checkin", text, "progress")

    return _record(state, "exhausted", None, "checkins_exhausted")
