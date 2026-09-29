"""Integer parsing, request shapes and usage capture of the judge clients (offline).

Adapted from ../mats12_neel_working/tests/test_judge_panel.py (parse / request-shape tests).
"""

import asyncio

import pytest
from test_judge_fakes import FakeClient

from emx.judge_client import LogprobJudge, TextScoreJudge, make_judge, parse_score_int


@pytest.mark.parametrize("text,expected", [
    ("85", 85), ("  85\n", 85), ("Score: 85", 85), ("85/100", 85), ("0", 0), ("100", 100),
    ("I would rate this 42 out of 100", 42),
    ("150", None), ("101", None), ("-3", None),
    ("I can't rate this", None), ("", None), (None, None),
    ("８５", None), ("score ٣", None),  # fullwidth / non-ASCII digits are not scores
])
def test_parse_score_int(text, expected):
    assert parse_score_int(text) == expected


def test_make_judge_modes():
    assert type(make_judge("m", "{question}{answer}", "logprob", object())) is LogprobJudge
    j = make_judge("m", "{question}{answer}", "integer", object(), {"max_tokens": 4})
    assert type(j) is TextScoreJudge and j.max_tokens == 4
    with pytest.raises(ValueError):
        make_judge("m", "t", "vibes", object())


def test_text_judge_request_shape():
    j = TextScoreJudge("m", "{question}", client=object(), max_tokens=1024,
                       max_tokens_param="max_tokens",
                       extra_body={"reasoning": {"effort": "minimal"}})
    kw = j.request_kwargs([{"role": "user", "content": "x"}])
    assert kw["temperature"] == 0 and kw["seed"] == 0 and kw["max_tokens"] == 1024
    assert kw["extra_body"] == {"reasoning": {"effort": "minimal"}}
    assert "reasoning_effort" not in kw and "logprobs" not in kw


def test_logprob_judge_request_shape():
    j = LogprobJudge("m", "{question}", object(), max_tokens_param="max_completion_tokens",
                     reasoning_effort="none", top_logprobs=5, max_tokens=8)
    kw = j.request_kwargs([{"role": "user", "content": "x"}])
    assert kw["logprobs"] is True and kw["top_logprobs"] == 5
    assert kw["max_completion_tokens"] == 8 and "max_tokens" not in kw
    assert kw["reasoning_effort"] == "none" and kw["temperature"] == 0 and kw["seed"] == 0


def test_judge_detail_returns_own_usage_and_score():
    c = FakeClient()
    lj = LogprobJudge("m", "Q:{question} A:{answer}", c)
    r = asyncio.run(lj.judge_detail(question="q", answer="SCORE=80"))
    assert r.score == pytest.approx(80.0) and r.usage["prompt_tokens"] == 100
    r = asyncio.run(lj.judge_detail(question="q", answer="REFUSE"))
    assert r.score is None and r.raw == "REFUSAL"
    tj = TextScoreJudge("m", "Q:{question} A:{answer}", FakeClient(logprobs=False))
    assert asyncio.run(tj.judge(question="q", answer="SCORE=12")) == 12


def test_missing_logprobs_gives_none_not_error():
    c = FakeClient(logprobs=False)  # provider ignores logprobs=True
    lj = LogprobJudge("m", "{question}{answer}", c)
    assert asyncio.run(lj.judge(question="q", answer="SCORE=80")) is None
