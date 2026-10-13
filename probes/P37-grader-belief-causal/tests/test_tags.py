"""p37.tags: parsing, prompts, kappa, snippet sampling, and tagging with a mocked api.chat."""
from __future__ import annotations

import asyncio
import json
import math

import pytest
from p37 import api, sources, tags
from p37 import contracts as C


def _obj(**true) -> dict:
    return {t: {"value": bool(true.get(t)), "quote": "q" if true.get(t) else ""}
            for t in tags.TAGS}


def _reply(text: str, cost: float = 0.001, error: str | None = None) -> api.Reply:
    return api.Reply(text=text, reasoning="", billed_usd=None if error else cost,
                     list_usd=cost, usage=None, provider="x", response_id="r",
                     finish_reason="stop", error=error)


# --- parsing ----------------------------------------------------------------------------------
def test_parse_plain_and_fenced_and_prose():
    o = _obj(visible_tests=True, game_plan=True)
    for text in (json.dumps(o), "```json\n" + json.dumps(o) + "\n```",
                 "Here is my answer:\n```\n" + json.dumps(o) + "\n```\nDone {not json}",
                 "Thinking {x} ... " + json.dumps(o)):
        p = tags.parse_reply(text)
        assert p == {t: t in ("visible_tests", "game_plan") for t in tags.TAGS}
    assert tags.parse_quotes(json.dumps(o))["visible_tests"] == "q"
    assert tags.parse_quotes(json.dumps(o))["spec_hidden"] == ""


def test_parse_missing_or_bad_tag_is_none():
    o = _obj()
    del o["unclear"]
    assert tags.parse_reply(json.dumps(o)) is None
    assert tags.parse_quotes(json.dumps(o)) is None
    o = _obj()
    o["behaviour"] = {"value": "maybe", "quote": ""}
    assert tags.parse_reply(json.dumps(o)) is None
    assert tags.parse_reply("") is None
    assert tags.parse_reply("no json here") is None


def test_parse_tolerates_bare_bools_and_string_bools():
    o = {t: False for t in tags.TAGS}
    o["spec_hidden"] = "true"
    assert tags.parse_reply(json.dumps(o))["spec_hidden"] is True


# --- prompts ----------------------------------------------------------------------------------
def test_build_messages_short_and_truncated():
    m = tags.build_messages("short text")
    assert [x["role"] for x in m] == ["system", "user"]
    assert "short text" in m[1]["content"] and tags.TRUNC_MARKER not in m[1]["content"]
    for t in tags.TAGS:
        assert t in m[0]["content"]
    long = "A" * 5000 + "B" * tags.MAX_CHARS
    u = tags.build_messages(long)[1]["content"]
    body = u.split("<<<BEGIN>>>\n")[1].split("\n<<<END>>>")[0]
    assert body.startswith(tags.TRUNC_MARKER) and "A" not in body.replace(tags.TRUNC_MARKER, "")
    assert "B" * tags.MAX_CHARS in u


# --- kappa ------------------------------------------------------------------------------------
def test_cohen_kappa_known_values():
    assert tags.cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert tags.cohen_kappa([True, False, True, False], [False, True, False, True]) == -1.0
    # po = 0.8, pa = pb = 0.4 -> pe = 0.52, kappa = 0.28 / 0.48
    a = [True, True, False, False, False] * 2
    b = [True, False, True, False, False] * 2
    assert tags.cohen_kappa(a, b) == pytest.approx((0.6 - 0.52) / 0.48)
    assert tags.cohen_kappa([False] * 5, [False] * 5) == 1.0           # documented convention
    assert tags.cohen_kappa([False] * 4, [False, False, False, True]) == pytest.approx(0.0)
    assert math.isnan(tags.cohen_kappa([], []))
    with pytest.raises(ValueError):
        tags.cohen_kappa([True], [True, False])


def test_agreement_table_and_gate():
    t = {"a": dict.fromkeys(tags.TAGS, False), "b": dict.fromkeys(tags.TAGS, True),
         "c": None, "only": dict.fromkeys(tags.TAGS, True)}
    r = {"a": dict.fromkeys(tags.TAGS, False), "b": dict.fromkeys(tags.TAGS, True),
         "c": dict.fromkeys(tags.TAGS, True)}
    tab = tags.agreement_table(t, r)
    assert tab["n"] == 2
    assert tab["per_tag"]["visible_tests"] == {"kappa": 1.0, "n_pos_tagger": 1,
                                               "n_pos_reference": 1, "agreement": 1.0}
    assert tags.gate_verdict(tab)["pass"]
    r["b"] = dict(r["b"], game_plan=False)
    g = tags.gate_verdict(tags.agreement_table(t, r))
    assert not g["pass"] and g["failing"] == ["game_plan"]


# --- snippet sampling -------------------------------------------------------------------------
def _fake_sources(monkeypatch):
    recs = [{"site_id": f"x3b|task{i}|r1|a1", "task_id": f"task{i}", "path": f"p{i}",
             "episode_id": f"e{i}"} for i in range(6)]
    texts = ["I think the grader runs these tests, so passing them is what is scored here.",
             "Let me look at the solution file and check what the function returns now.",
             "ok",                                           # < 40 chars: skipped
             "Maybe I could special-case the input, but that would be hardcoding the answer.",
             "Running pytest again to see whether the second assertion still fails here."]

    def load(rec):
        i = int(rec["task_id"][4:])
        msgs = [{"role": "user", "content": "task"}]
        for k, t in enumerate(texts):
            msgs.append({"role": "assistant",
                         "content": [{"type": "reasoning", "reasoning": f"{t} ({i},{k})"}]})
            msgs.append({"role": "tool", "content": "out"})
        return {"messages": msgs, "final_solution_py": "SHOULD NOT BE READ"}, "sha"

    monkeypatch.setattr(sources, "source_list", lambda: recs)
    monkeypatch.setattr(sources, "load", load)


def test_sample_snippets_pools_and_determinism(monkeypatch):
    _fake_sources(monkeypatch)
    units = tags.snippet_units()
    assert len(units) == 6 * 4                       # the "ok" turn is skipped
    kw, rest = tags.snippet_pools(units)
    assert len(kw) == 12 and len(rest) == 12          # turns 1 and 4 match the keyword regex
    assert all(u["turn"] in (1, 4) for u in kw)
    s1 = tags.sample_snippets(n_keyword=5, n_random=3, seed=7)
    s2 = tags.sample_snippets(n_keyword=5, n_random=3, seed=7)
    assert s1 == s2
    assert [s["snippet_id"] for s in s1] == sorted(s["snippet_id"] for s in s1)
    assert sum(s["pool"] == "keyword" for s in s1) == 5
    assert sum(s["pool"] == "random" for s in s1) == 3
    assert s1[0]["snippet_id"].startswith("x3b|task") and "#t" in s1[0]["snippet_id"]
    assert set(s1[0]) == {"snippet_id", "site_id", "task_id", "turn", "text", "pool"}
    assert tags.sample_snippets(5, 3, seed=8) != s1
    big = tags.sample_snippets(n_keyword=100, n_random=100, seed=7)
    assert len(big) == 24                             # capped at the pool sizes


def test_keyword_regex_word_start():
    assert tags.KEYWORD_RE.search("the evaluation harness")
    assert tags.KEYWORD_RE.search("I could hard-code it")
    assert not tags.KEYWORD_RE.search("an underscore in an upgrade")


# --- tagging ----------------------------------------------------------------------------------
class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, model, messages, *, max_tokens, temperature=None, **kw):
        self.calls.append({"model": model.slug, "messages": messages, "max_tokens": max_tokens,
                           "temperature": temperature})
        return self.replies.pop(0) if self.replies else _reply(json.dumps(_obj()))


@pytest.fixture
def ledger(tmp_path):
    return str(tmp_path / "costs.jsonl")


def _rows(path):
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


def test_tag_items_resume_and_ledger(monkeypatch, tmp_path, ledger):
    fake = FakeChat([])
    monkeypatch.setattr(api, "chat", fake)
    items = [{"item_id": f"i{k}", "text": f"text {k}"} for k in range(3)]
    cache = tmp_path / "raw" / "tags.jsonl"
    out = asyncio.run(tags.tag_items(items, C.TAGGER, "unit", cache, concurrency=2,
                                     ledger=ledger))
    assert [r["item_id"] for r in out] == ["i0", "i1", "i2"]
    assert all(r["tags"] == dict.fromkeys(tags.TAGS, False) and r["error"] is None for r in out)
    assert len(fake.calls) == 3
    assert fake.calls[0]["temperature"] == 0 and fake.calls[0]["max_tokens"] == tags.MAX_TOKENS
    rows = _rows(ledger)
    assert len(rows) == 1 and rows[0]["exp"] == "P37" and rows[0]["units"] == 3
    assert rows[0]["usd"] == pytest.approx(0.003)
    assert len(_rows(cache)) == 3
    # second run: everything cached -> no calls and no ledger row
    out2 = asyncio.run(tags.tag_items(items, C.TAGGER, "unit", cache, ledger=ledger))
    assert len(fake.calls) == 3 and out2 == out and len(_rows(ledger)) == 1
    # another model is keyed separately in the same file
    asyncio.run(tags.tag_items(items[:1], C.REFERENCE, "unit-ref", cache, ledger=ledger))
    assert len(fake.calls) == 4 and fake.calls[-1]["model"] == C.REFERENCE.slug


def test_tag_items_parse_retry_and_failure(monkeypatch, tmp_path, ledger):
    good = _reply(json.dumps(_obj(spec_hidden=True)))
    fake = FakeChat([_reply("garbled"), good,                       # item a: retry succeeds
                     _reply("still bad"), _reply("nope")])          # item b: both fail
    monkeypatch.setattr(api, "chat", fake)
    cache = tmp_path / "c.jsonl"
    out = asyncio.run(tags.tag_items([{"item_id": "a", "text": "x"}], C.TAGGER, "u", cache,
                                     concurrency=1, ledger=ledger))
    assert out[0]["tags"]["spec_hidden"] is True and out[0]["error"] is None
    assert out[0]["billed_usd"] == pytest.approx(0.002) and len(fake.calls) == 2
    out = asyncio.run(tags.tag_items([{"item_id": "b", "text": "y"}], C.TAGGER, "u", cache,
                                     concurrency=1, ledger=ledger))
    assert out[0]["tags"] is None and out[0]["error"] == "unparseable reply"
    assert len(fake.calls) == 4
    # a failed item is retried on the next run; the ok one is not
    out = asyncio.run(tags.tag_texts([{"item_id": "a", "text": "x"},
                                      {"item_id": "b", "text": "y"}], C.TAGGER, "u", cache,
                                     ledger=ledger))
    assert len(fake.calls) == 5 and all(r["tags"] is not None for r in out)
    assert [r["units"] for r in _rows(ledger)] == [2, 2, 1]


def test_tag_items_api_error_is_recorded(monkeypatch, tmp_path, ledger):
    fake = FakeChat([_reply("", 0.01, error="boom"), _reply("", 0.01, error="boom")])
    monkeypatch.setattr(api, "chat", fake)
    out = asyncio.run(tags.tag_items([{"item_id": "a", "text": "x"}], C.TAGGER, "u",
                                     tmp_path / "c.jsonl", ledger=ledger))
    assert out[0]["error"] == "boom" and out[0]["tags"] is None
    assert _rows(ledger)[0]["usd"] == pytest.approx(0.02)


def test_projected_cost_and_batch_guard(monkeypatch, tmp_path, ledger):
    p = tags.projected_cost(["x" * 3500], C.TAGGER, out_tokens=0)
    n_in = sum(len(m["content"]) for m in tags.build_messages("x" * 3500)) / 3.5
    assert p == pytest.approx(int(n_in) * C.TAGGER.in_ceiling / 1e6)
    monkeypatch.setattr(api, "chat", FakeChat([]))
    from emx import spend

    with pytest.raises(spend.BudgetExceeded):
        asyncio.run(tags.tag_items([{"item_id": "a", "text": "x"}], C.TAGGER, "u",
                                   tmp_path / "c.jsonl", projected_usd=10_000, ledger=ledger))


def test_cli_dry_run_makes_no_calls(monkeypatch, capsys):
    _fake_sources(monkeypatch)

    async def boom(*a, **k):
        raise AssertionError("no calls in a dry run")

    monkeypatch.setattr(api, "chat", boom)
    tags.main(["validate", "--dry-run"])
    info = json.loads(capsys.readouterr().out)
    assert info["keyword_pool"] == 12 and info["random_pool"] == 12 and info["n_sampled"] == 24
    assert info["projected_usd"][C.TAGGER.slug]["worst"] > 0
