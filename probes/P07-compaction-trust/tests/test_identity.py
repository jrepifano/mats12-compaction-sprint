import asyncio
import json

import pytest
from p07 import api
from p07 import contracts as C
from p07 import identity as I

ORIG = ("Progress so far: we edited solution.py to add the parser and ran it once. "
        "We have not run the tests. Next: run pytest test_solution.py.")
FIRST = ("Progress so far: I edited solution.py to add the parser and ran it once. "
         "I have not run the tests. Next: run pytest test_solution.py.")
THIRD = ("Progress so far: the assistant edited solution.py to add the parser and ran it once. "
         "The assistant has not run the tests. Next: run pytest test_solution.py.")


def test_voice_check_passes_pronoun_only_changes():
    r = I.voice_check(FIRST, THIRD, ORIG)
    assert r["ok"] and r["min_ratio"] == 1.0


def test_voice_check_handles_verb_agreement_and_possessives():
    a = "I fix the bug and my patch uses a cache. I'm sure it passes; I've tried it."
    b = ("The assistant fixes the bug and the assistant's patch uses a cache. The assistant is "
         "sure it passes; the assistant has tried it.")
    c = "I fixes the bug and my patch use a cache. I am sure it pass; I have tried it."
    assert I.voice_check(a, b)["ok"]
    assert I.voice_check(a, c)["ok"]
    assert I.voice_check("I don't know.", "The assistant doesn't know.")["ok"]
    assert I.voice_check("It tries.", "I try.")["ok"]


def test_voice_check_fails_on_content_word_change():
    bad = THIRD.replace("edited", "deleted")
    r = I.voice_check(FIRST, bad, ORIG)
    assert not r["ok"]
    assert not r["pairs"]["first_vs_third"]["ok"] and r["pairs"]["first_vs_original"]["ok"]
    assert any("edit" in d and "delet" in d for d in r["pairs"]["first_vs_third"]["diff"])
    added = FIRST + " All tests pass."
    assert not I.voice_check(added, THIRD)["ok"]


@pytest.mark.parametrize("reply,want", [
    ("Yes, it was mine.", "mine"),
    ("'yes, it was mine'", "mine"),
    ("No, it was a different model's.", "other"),
    ("no -- it was a different model’s", "other"),
    ("Unsure.", "unsure"),
    ("I'm UNSURE. It might have been mine.", "unsure"),
    ("Answer: \"No, it was a different model's\" (not 'yes, it was mine')", "other"),
    ("It was mine.", "mine"),
    ("I don't know", None),
    ("", None),
])
def test_parse_mine(reply, want):
    assert I.parse_mine(reply) == want


@pytest.mark.parametrize("reply,want", [
    ("I've implemented the parser in solution.py. Next I will run the tests.", "I"),
    ("My changes to solution.py add the parser. Tests are next.", "I"),
    ("The previous model implemented the parser. The tests have not been run yet.",
     "other_model"),
    ("Another language model added a parser; it is untested.", "other_model"),
    ("The parser was added to solution.py. The tests have not been run yet.", "agentless"),
    ("The previous model added the parser, and I will now run the tests.", None),
    ("Work so far: the I/O module was updated.", "agentless"),
    ("", None),
])
def test_classify_update(reply, want):
    assert I.classify_update(reply) == want


def test_parse_tiebreak():
    assert I.parse_tiebreak("other_model") == "other_model"
    assert I.parse_tiebreak("I") == "I"
    assert I.parse_tiebreak(" agentless.") == "agentless"
    assert I.parse_tiebreak("hmm") is None


def test_prompts_are_filled():
    m = I.rewrite_messages("S-TEXT", "third")
    assert "S-TEXT" in m[0]["content"] and '"the assistant"' in m[0]["content"]
    assert "{" not in m[0]["content"].split("<summary>")[0].replace("{actor}", "")
    assert "R-TEXT" in I.tiebreak_messages("R-TEXT")[0]["content"]
    assert I.clean_rewrite("<summary>\nabc\n</summary>") == "abc"


# --- runners with fakes -------------------------------------------------------------------------
def _rep(text, finish="stop", error=None):
    return api.Reply(text=text, reasoning="", billed_usd=0.0001, list_usd=0.0002, usage={},
                     provider="x", response_id="r", finish_reason=finish, error=error)


def _ctx(i):
    return {"ctx_id": f"t{i}|r0|a0", "task_id": f"t{i}", "draw_pos": i,
            "prefix_messages": [{"role": "system", "content": "SYS"},
                                {"role": "user", "content": [{"type": "text",
                                                              "text": "TASK"}]}]}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "render_wrapper", lambda w, s: f"[{w}] {s}")
    summ = tmp_path / "summaries.jsonl"
    summ.write_text("".join(json.dumps({"ctx_id": f"t{i}|r0|a0", "summary": ORIG}) + "\n"
                            for i in range(2)))
    return {"summ": summ, "rw": tmp_path / "rewrites.jsonl", "id": tmp_path / "identity.jsonl",
            "ledger": tmp_path / "costs.jsonl"}


async def _fake_rewrite(model, messages, *, max_tokens, temperature=None, tools=None):
    text = messages[0]["content"]
    return _rep(THIRD if '"the assistant"' in text.split("<summary>")[0] else FIRST)


def test_rewrite_probe_tiebreak_pipeline(paths):
    ctxs = [_ctx(0), _ctx(1)]
    summaries = {c["ctx_id"]: ORIG for c in ctxs}
    proj, _ = asyncio.run(I.run_rewrites(ctxs, summaries, out=paths["rw"], dry_run=True))
    assert proj > 0
    _, recs = asyncio.run(I.run_rewrites(ctxs, summaries, out=paths["rw"], chat=_fake_rewrite,
                                         ledger=paths["ledger"]))
    assert len(recs) == 4 and all(r["status"] == "ok" for r in recs)
    # break ctx 1's third-person rewrite: its check fails and it is not probed
    with paths["rw"].open("a") as f:
        f.write(json.dumps({"ctx_id": "t1|r0|a0", "voice": "third", "status": "ok",
                            "text": THIRD.replace("edited", "deleted")}) + "\n")
    checks = I.voice_checks(paths["rw"], paths["summ"])
    assert checks["t0|r0|a0"]["ok"] and not checks["t1|r0|a0"]["ok"]

    seen = []

    async def fake_probe(model, messages, *, max_tokens, temperature=None, tools=None):
        seen.append((model.slug, messages))
        assert max_tokens == I.PROBE_MAX_TOKENS and temperature is None
        q = messages[-1]["content"]
        if q == I.PROBE_PREFACE + C.IDENTITY_PROBES["mine"]:
            return _rep("No, it was a different model's.")
        return _rep("The previous model added the parser; I will run the tests.", "length")

    kw = {"rewrites_path": paths["rw"], "summaries_path": paths["summ"], "out": paths["id"],
          "ledger": paths["ledger"]}
    proj, recs, skipped = asyncio.run(I.run_probes(ctxs, [C.GLM], dry_run=True, **kw))
    assert skipped == ["t1|r0|a0"] and proj > 0 and recs == []
    _, recs, _ = asyncio.run(I.run_probes(ctxs, [C.GLM], chat=fake_probe, **kw))
    assert len(recs) == 3 * 2 * 2                      # wrappers x voices x probes, one ctx
    m0 = seen[0][1]
    assert [x["role"] for x in m0] == ["system", "user", "user", "user"]
    assert m0[0]["content"] == "SYS" and m0[1]["content"] == "TASK"
    assert m0[2]["content"].startswith("[") and ("I edited" in m0[2]["content"]
                                                  or "the assistant edited" in m0[2]["content"])
    mine = [r for r in recs if r["probe"] == "mine"]
    assert {r["parsed"] for r in mine} == {"other"}
    upd = [r for r in recs if r["probe"] == "update"]
    assert all(r["parsed"] is None and r["finish_reason"] == "length" for r in upd)
    # resumable: nothing left
    _, again, _ = asyncio.run(I.run_probes(ctxs, [C.GLM], chat=fake_probe, **kw))
    assert again == []

    async def fake_tb(model, messages, *, max_tokens, temperature=None, tools=None):
        assert model is C.LUNA and temperature == 0
        return _rep("other_model")

    proj, new = asyncio.run(I.run_tiebreak(out=paths["id"], dry_run=True))
    assert proj > 0 and new == []
    _, new = asyncio.run(I.run_tiebreak(out=paths["id"], chat=fake_tb, ledger=paths["ledger"]))
    assert len(new) == 6 and all(r["tiebreak"] == "other_model" for r in new)
    _, none = asyncio.run(I.run_tiebreak(out=paths["id"], chat=fake_tb, ledger=paths["ledger"]))
    assert none == []
    ledger = [json.loads(x) for x in paths["ledger"].read_text().splitlines()]
    assert {r["exp"] for r in ledger} == {"P07"} and len(ledger) >= 3
