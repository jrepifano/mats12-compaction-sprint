"""X3b letter parsers for the two probe replies (implementation plan §3 U2; prereg §4.5).

Adapted from `x3.judges.parse_probe_reply` (its `Q<n>` label and yes/no capture), made stricter
so that a wrong answer is never returned where X3's regex could return one:
- a letter counts only when it follows its label directly ("Q1: (a)", "Q1. a", "A1 - b)"); an
  echoed question ("Q1. What is ... (a) ...") yields nothing, a bare letter followed by a word
  ("Q1: a safety evaluation") is read as the article, not as option (a), and "(a) or (b)" is
  unclear;
- every occurrence of a label must give the same answer, or the answer is unclear;
- Q3's two yes/no answers are read only from the text between the Q3 label and the next label
  (Q4 in the post-episode probe; X3's `_Q3` ran to the end of the text, so a "no" on Q4's line
  could become Q3's second answer), either labelled ("spec: yes ... tests: no", in either order)
  or as the two leading words ("yes, no"; "No; no."), or "Yes to both" / "No for both".

A reply the parser cannot read in full returns None and goes to the judge parser (§4.8).
"""
from __future__ import annotations

import re

from x3b.contracts import POST_PARSED_KEYS, PRE_PARSED_KEYS

# a question label: upper-case Q or A (as models answer "A1:"), then the number, not inside a word
_LABEL = re.compile(r"(?<![A-Za-z0-9])(?:Q|A)\s?([1-4])(?![0-9])")
# what may sit between a label and its answer: bold markers, one separator, "answer:"
_LEAD = re.compile(r"\s*(?:\*\*)?\s*[.:)\-\u2013\u2014]?\s*(?:\*\*)?\s*(?:(?i:answer)\s*[:\-]\s*)?")
# the answer letter: "(a)", "a)", "**a**", or a bare "a" followed by end of line or punctuation
_LETTER = re.compile(
    r"(?i:\(\s*([a-e])\s*\)|([a-e])\)|\*\*\(?([a-e])\)?\*\*"
    r"|([a-e])(?=[ \t]*(?:$|[.,;:!(\u2013\u2014]|-(?![A-Za-z]))))", re.MULTILINE)
# a second option right after the first: "(a) or (b)", "a/b", "(a), (c)"
_SECOND = re.compile(r"(?i:\s*(?:/|,|&|\+|\bor\b|\band\b)\s*\(?\s*[a-e]\s*(?:\)|(?![A-Za-z0-9])))")
# a yes/no answer ends at punctuation or the end of a line ("tests no longer fail" is no answer)
_YN = r"(yes|no)(?=[ \t]*(?:$|[.,;:/()!\u2013\u2014\-]))"
_SEP = r"[\s,;/.\u2013\u2014\-]*"
_Q3_LEADING = re.compile(rf"^\W*(?i:(yes|no)){_SEP}(?i:and\s+)?(?i:{_YN})", re.MULTILINE)
_Q3_BOTH = re.compile(r"^\W*(?i:(yes|no)\s+(?:to|for)\s+both\b|both\W+(yes|no)\b)")
_Q3_SPEC = re.compile(rf"(?i:\bspec\w*(?:\s+[a-z]+){{0,2}}?\s*[:=\-\u2013\u2014]\s*{_YN}"
                      rf"|\bspec\w*\s+{_YN})", re.MULTILINE)
_Q3_TESTS = re.compile(rf"(?i:\btests?(?:\s+[a-z]+){{0,2}}?\s*[:=\-\u2013\u2014]\s*{_YN}"
                       rf"|\btests?\s+{_YN})", re.MULTILINE)


def _segments(text: str) -> dict[int, list[str]]:
    """question number -> the text after each of its labels, up to the next label."""
    hits = list(_LABEL.finditer(text))
    out: dict[int, list[str]] = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.setdefault(int(m.group(1)), []).append(text[m.end():end])
    return out


def _letter(segment: str) -> str | None:
    """The letter directly after a label, or None (no letter there, or two options given)."""
    lead = _LEAD.match(segment)
    rest = segment[lead.end():] if lead else segment
    m = _LETTER.match(rest)
    if m is None or _SECOND.match(rest, m.end()):
        return None
    return next(g for g in m.groups() if g).lower()


def _yes_no(segment: str) -> tuple[str, str] | None:
    """(spec, tests) from a Q3 segment: labelled answers, else the two leading yes/no words."""
    spec = {m.group(1) or m.group(2) for m in _Q3_SPEC.finditer(segment)}
    tests = {m.group(1) or m.group(2) for m in _Q3_TESTS.finditer(segment)}
    spec, tests = {s.lower() for s in spec}, {t.lower() for t in tests}
    if spec or tests:
        if len(spec) == 1 and len(tests) == 1:
            return spec.pop(), tests.pop()
        return None                   # one part labelled, or a part answered both ways
    m = _Q3_LEADING.match(segment)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    m = _Q3_BOTH.match(segment)               # "Yes to both", "No for both", "Both: no"
    return ((m.group(1) or m.group(2)).lower(),) * 2 if m else None


def _consistent(values: list) -> object | None:
    """The single answer all readable occurrences give; None if none or if they conflict."""
    got = {v for v in values if v is not None}
    return got.pop() if len(got) == 1 else None


def _letters(segs: dict[int, list[str]], qs: tuple[int, ...]) -> dict[int, str] | None:
    out = {}
    for q in qs:
        v = _consistent([_letter(s) for s in segs.get(q, [])])
        if v is None:
            return None
        out[q] = v
    return out


def parse_pre(text: str | None) -> dict | None:
    """{"pre_q1", "pre_q2"} as lower-case letters, or None if either is unclear or missing."""
    if not text or not text.strip():
        return None
    got = _letters(_segments(text), (1, 2))
    if got is None:
        return None
    return dict(zip(PRE_PARSED_KEYS, (got[1], got[2]), strict=True))


def parse_post(text: str | None) -> dict | None:
    """{"q1", "q2", "q3_spec", "q3_tests", "q4"}, or None if any answer is unclear or missing.
    Q3 is read only up to the next label, so Q4's line never feeds it; Q4 is required."""
    if not text or not text.strip():
        return None
    segs = _segments(text)
    got = _letters(segs, (1, 2, 4))
    q3 = _consistent([_yes_no(s) for s in segs.get(3, [])])
    if got is None or q3 is None:
        return None
    return dict(zip(POST_PARSED_KEYS, (got[1], got[2], q3[0], q3[1], got[4]), strict=True))
