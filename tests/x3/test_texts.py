"""U0: prompt invariants (implementation plan §1.2, prereg §4.5)."""
from __future__ import annotations

import itertools

from x3 import texts
from x3.contracts import Context, PromptLevel


def test_headers_have_four_lines_and_shared_run_id():
    for ctx, lines in texts.HEADERS.items():
        assert len(lines) == 4, ctx
        assert lines[0] == texts.RUN_ID


def test_prompts_differ_only_inside_the_header():
    for level in PromptLevel:
        bodies = {c: texts.user_prompt(c, level) for c in Context}
        tails = {b.split("\n\n", 1)[1] for b in bodies.values()}
        assert len(tails) == 1, "task text must be identical across contexts"
        for c, b in bodies.items():
            assert b.startswith(texts.header_text(c) + "\n\n")


def test_two_by_two_changes_one_line_per_contrast():
    h = texts.HEADERS
    def diff(a, b):
        return [i for i, (x, y) in enumerate(zip(h[a], h[b])) if x != y]
    assert diff(Context.CAP_SCRIPT, Context.SAFE_SCRIPT) == [1]      # purpose line only
    assert diff(Context.CAP_HUMAN, Context.SAFE_HUMAN) == [1]
    assert diff(Context.CAP_SCRIPT, Context.CAP_HUMAN) == [2]        # grader line only
    assert diff(Context.SAFE_SCRIPT, Context.SAFE_HUMAN) == [2]
    eval_ctx = [Context.CAP_SCRIPT, Context.CAP_HUMAN, Context.SAFE_SCRIPT, Context.SAFE_HUMAN]
    assert len({h[c][3] for c in eval_ctx}) == 1                     # one results line


def test_levels_change_only_the_strictness_sentence():
    for c in Context:
        a, b, d = (texts.user_prompt(c, lv) for lv in PromptLevel)
        for x, y in itertools.combinations((a, b, d), 2):
            lx, ly = x.splitlines(), y.splitlines()
            assert len(lx) == len(ly)
            assert sum(p != q for p, q in zip(lx, ly)) == 1


def test_context_purpose_and_grader():
    assert Context.CAP_HUMAN.purpose == "cap" and Context.CAP_HUMAN.grader == "human"
    assert Context.SAFE_SCRIPT.purpose == "safe" and Context.SAFE_SCRIPT.grader == "script"
    assert Context.TEAMMATE.purpose is None and Context.TEAMMATE.grader is None
    assert Context.NEUTRAL.purpose is None and Context.NEUTRAL.grader is None


def test_run_info_matches_header():
    for c in Context:
        assert texts.run_info(c).rstrip("\n") == texts.header_text(c)
