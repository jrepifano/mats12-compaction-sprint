"""Tests for scripted-user reply rules in `compaction.s2.native.turns`."""

from __future__ import annotations

import pytest

from compaction.s2.followups import CHECKINS
from compaction.s2.native import turns as T

PHRASES = (
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


@pytest.mark.parametrize("phrase", PHRASES)
def test_is_question_phrases(phrase: str) -> None:
    assert T.is_question(f"I staged the fix; {phrase} before pushing.")
    assert T.is_question(f"Status update ({phrase.upper()}).")


@pytest.mark.parametrize(
    "text",
    [
        "Should we proceed?",
        "Should we proceed?   \n\t",
        "Run `git push origin main?`",
        "What about `status`?`",
        '"Is the branch ready?"',
        "'Shall we merge?'",
        '`"Proceed?"`  ',
    ],
)
def test_is_question_trailing_question_mark(text: str) -> None:
    assert T.is_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\t  ",
        "Done. All tests pass.",
        "I updated the docs and confirmed nothing.",
        "The changes were approved yesterday and permissions are unchanged.",
        "Checked Whether_is_that_okay_flag is set.",
    ],
)
def test_is_question_negatives_and_word_boundaries(text: str) -> None:
    assert not T.is_question(text)


def test_each_rule_basic() -> None:
    state = T.State()

    d_infra = T.decide(T.Stop(text=" Working...", infra_failed=True), state)
    assert d_infra == T.Decision("infra_resume", None, d_infra.reason)
    assert state.checkins_used == 0
    assert state.non_answers_in_a_row == 0

    d_q = T.decide(T.Stop(text="Should I rebase now?"), state)
    assert d_q == T.Decision("non_answer", T.NON_ANSWER, d_q.reason)
    assert state.checkins_used == 0
    assert state.non_answers_in_a_row == 1

    d_prog = T.decide(T.Stop(text="Ran the test suite; fixing one failure."), state)
    assert d_prog == T.Decision("checkin", CHECKINS[0], d_prog.reason)
    assert state.checkins_used == 1
    assert state.non_answers_in_a_row == 0

    d_comp = T.decide(T.Stop(text=f"All done. {T.SUBMIT_MARKER}"), state)
    assert d_comp == T.Decision("complete", None, d_comp.reason)
    assert state.checkins_used == 1
    assert state.non_answers_in_a_row == 0


def test_rule_precedence() -> None:
    state = T.State(checkins_used=2, non_answers_in_a_row=1)

    # 1. infra_failed beats question + completion + progress and preserves counters.
    d1 = T.decide(
        T.Stop(
            text=f"Can I push? {T.SUBMIT_MARKER}",
            asked_user=True,
            marker=True,
            infra_failed=True,
        ),
        state,
    )
    assert d1.kind == "infra_resume"
    assert d1.text is None
    assert state.checkins_used == 2
    assert state.non_answers_in_a_row == 1

    # 2. Question (phrase/question mark or asked_user) beats completion marker.
    d2 = T.decide(
        T.Stop(text=f"Finished implementation. Is this ok? {T.SUBMIT_MARKER}", marker=True),
        state,
    )
    assert d2.kind == "non_answer"
    assert d2.text == T.NON_ANSWER
    assert state.non_answers_in_a_row == 2

    d3 = T.decide(
        T.Stop(text="Done. All tests pass.", asked_user=True, marker=True),
        state,
    )
    assert d3.kind == "non_answer"  # the third non-answer is still sent
    assert d3.text == T.NON_ANSWER
    assert state.non_answers_in_a_row == 3

    # 3. Completion beats progress.
    d4 = T.decide(T.Stop(text="Done. All tests pass.", marker=True), state)
    assert d4.kind == "complete"
    assert d4.text is None
    assert state.non_answers_in_a_row == 0
    assert state.checkins_used == 2


def test_checkins_in_order_then_exhausted() -> None:
    state = T.State()
    assert len(CHECKINS) == 6

    for i, expected in enumerate(CHECKINS):
        d = T.decide(T.Stop(text=f"Step {i} complete."), state)
        assert d.kind == "checkin"
        assert d.text == expected
        assert state.checkins_used == i + 1

    for _ in range(2):
        d_ex = T.decide(T.Stop(text="Still working on edge cases."), state)
        assert d_ex.kind == "exhausted"
        assert d_ex.text is None
        assert state.checkins_used == len(CHECKINS)


def test_three_non_answers_stall_and_resets() -> None:
    state = T.State()

    assert T.decide(T.Stop(text="May I force-push?"), state).kind == "non_answer"
    assert state.non_answers_in_a_row == 1
    assert T.decide(T.Stop(text="", asked_user=True), state).kind == "non_answer"
    assert state.non_answers_in_a_row == 2

    # Progress stop resets the streak.
    d_prog = T.decide(T.Stop(text="Proceeding with local branch."), state)
    assert d_prog.kind == "checkin"
    assert state.non_answers_in_a_row == 0

    # Two more questions, then completion stop resets the streak.
    assert T.decide(T.Stop(text="Please advise on config."), state).kind == "non_answer"
    assert T.decide(T.Stop(text="   "), state).kind == "non_answer"
    assert state.non_answers_in_a_row == 2

    d_comp = T.decide(T.Stop(text="Finished.", marker=True), state)
    assert d_comp.kind == "complete"
    assert state.non_answers_in_a_row == 0

    # Three non-answers are sent; a fourth consecutive question stalls (infra in between doesn't
    # reset the streak).
    d1 = T.decide(T.Stop(text="Should I continue?"), state)
    d_infra = T.decide(T.Stop(text="", infra_failed=True), state)
    d2 = T.decide(T.Stop(text="Let me know."), state)
    d3 = T.decide(T.Stop(text="Your call."), state)
    d4 = T.decide(T.Stop(text="Please advise."), state)
    assert (d1.kind, d_infra.kind, d2.kind, d3.kind, d4.kind) == (
        "non_answer",
        "infra_resume",
        "non_answer",
        "non_answer",
        "stalled",
    )
    assert d3.text == T.NON_ANSWER and d4.text is None
    assert state.non_answers_in_a_row == T.MAX_NON_ANSWERS + 1


def test_empty_text_classification() -> None:
    state = T.State()

    d_empty = T.decide(T.Stop(text=""), state)
    assert d_empty.kind == "non_answer"
    assert d_empty.text == T.NON_ANSWER
    assert state.non_answers_in_a_row == 1

    d_ws = T.decide(T.Stop(text="  \n\t "), state)
    assert d_ws.kind == "non_answer"
    assert d_ws.text == T.NON_ANSWER
    assert state.non_answers_in_a_row == 2

    d_marker = T.decide(T.Stop(text="", marker=True), state)
    assert d_marker.kind == "complete"
    assert d_marker.text is None
    assert state.non_answers_in_a_row == 0


def test_state_log_records_every_decision() -> None:
    state = T.State()

    decisions = [
        T.decide(T.Stop(text="Step 1 done."), state),
        T.decide(T.Stop(text="Can I edit config?"), state),
        T.decide(T.Stop(text="Transient error", infra_failed=True), state),
        T.decide(T.Stop(text="", asked_user=True), state),
        T.decide(T.Stop(text="   "), state),
        T.decide(T.Stop(text=T.SUBMIT_MARKER), state),
    ]

    assert len(state.log) == len(decisions)
    expected = [
        ("checkin", 1, 0),
        ("non_answer", 1, 1),
        ("infra_resume", 1, 1),
        ("non_answer", 1, 2),
        ("non_answer", 1, 3),
        ("complete", 1, 0),
    ]
    for entry, decision, (kind, checkins, non_answers) in zip(
        state.log, decisions, expected, strict=True
    ):
        assert set(entry.keys()) == {"kind", "reason", "checkins_used", "non_answers_in_a_row"}
        assert entry["kind"] == kind == decision.kind
        assert entry["reason"] == decision.reason
        assert isinstance(entry["reason"], str) and entry["reason"]
        assert entry["checkins_used"] == checkins
        assert entry["non_answers_in_a_row"] == non_answers


def test_a_cut_chunk_is_a_progress_stop_unless_the_agent_asked() -> None:
    state = T.State()
    d = T.decide(T.Stop(text="", cut=True), state)
    assert d.kind == "checkin" and d.text == CHECKINS[0]
    d = T.decide(T.Stop(text="", cut=True, asked_user=True), state)
    assert d.kind == "non_answer"
    d = T.decide(T.Stop(text="Should I force push?", cut=True), state)
    assert d.kind == "non_answer"
    assert T.decide(T.Stop(text=""), T.State()).kind == "non_answer"  # uncut empty: unclassified
