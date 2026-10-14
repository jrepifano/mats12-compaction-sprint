"""Laptop-only tests for `p48.qa` (T0's QA pairs). No network, no spend: the one place that would
call an API is driven by a fake with `llm_batch.run_batch`'s resume semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx import llm_batch
from p48 import contracts as K
from p48 import qa

BATCH_KW = dict(max_tokens=K.QA_MAX_TOKENS, temperature=qa.QA_TEMPERATURE, json_mode=False)


# --- helpers --------------------------------------------------------------------------------------
def _row(spec, req: dict, *, text: str | None, status: str = "ok",
         finish_reason: str | None = "stop", usage: dict | None = None) -> dict:
    kw = llm_batch.request_kwargs(spec, req["prompt"], req["seed"], system=req.get("system"),
                                  **BATCH_KW)
    return {"item_id": req["item_id"], "seed": req["seed"], "status": status, "text": text,
            "finish_reason": finish_reason, "usage": usage or {"completion_tokens": 12},
            "usd": 0.0, "fingerprint": llm_batch.fingerprint(spec, kw), "model": spec.model,
            "note": qa.NOTE, "utc": "2026-10-14T00:00:00Z", "attempts": 1, "error": None}


def fake_run_batch(answers, seen: dict):
    """A stand-in for `llm_batch.run_batch`: it reuses the real cache helpers, so it only "sends"
    the rows the real one would, and records what it was called with."""

    def run(model, requests, out_path, *, exp, note, max_tokens, temperature, json_mode,
            client=None, concurrency=16, **kw):
        seen.update({"exp": exp, "note": note, "model": model, "max_tokens": max_tokens,
                     "temperature": temperature, "json_mode": json_mode,
                     "concurrency": concurrency})
        cached = llm_batch.load_rows(out_path)
        pend = llm_batch.pending(model, requests, out_path, max_tokens=max_tokens,
                                 temperature=temperature, json_mode=json_mode)
        pend_keys = {(r["item_id"], r["seed"]) for r in pend}
        seen["sent"] = sorted(i for i, _ in pend_keys)
        out = []
        with open(out_path, "a", encoding="utf-8") as f:
            for r in requests:
                key = (r["item_id"], r["seed"])
                if key in pend_keys:
                    spec = model if isinstance(model, llm_batch.ModelSpec) else None
                    row = _row(spec, r, **answers(r))
                    f.write(json.dumps(row) + "\n")
                else:
                    row = cached[key]
                out.append(row)
        return out

    return run


def _plain(req: dict) -> dict:
    return {"text": f"A cat, of course - cats suit {req['item_id']} best."}


# --- the question bank ----------------------------------------------------------------------------
def test_bank_is_deterministic_in_the_seed_and_unique():
    a, b = qa.question_bank(), qa.question_bank()
    assert a == b
    assert len(a) == K.N_QA_PAIRS == 1_000
    assert len(set(a)) == len(a)
    assert qa.question_bank(seed=K.QA_SEED + 1) != a  # the seed, not the code, fixes the order
    assert qa.bank_sha(a) == qa.bank_sha(b) != qa.bank_sha(qa.question_bank(seed=1))


def test_bank_matches_the_family_quotas_and_the_templates():
    bank = set(qa.question_bank())
    for family, quota in qa.QUOTAS.items():
        pool = set(qa.family_pool(family))
        assert len(bank & pool) == quota, family
        assert quota < len(pool), f"{family}: the quota should be a sample, not the whole pool"
    assert sum(qa.QUOTAS.values()) == K.N_QA_PAIRS
    assert not any("{" in q for q in bank)  # every slot filled


def test_bank_has_zero_overlap_with_the_eval_questions():
    report = qa.check_no_eval_overlap(qa.question_bank())
    assert report["exact_overlap"] == []
    assert report["n"] == report["n_unique"] == K.N_QA_PAIRS
    assert report["n_eval"] == 50
    # Pinned: the closest near-duplicate shares only function words and asks a different task.
    assert report["max_jaccard"] == pytest.approx(0.5, abs=0.001)
    assert report["max_jaccard"] < qa.MAX_EVAL_JACCARD
    worst = report["closest"][0]
    assert "one animal to paint" in worst["question"]
    assert worst["eval_question"] in K.eval_questions()
    # No bank question tells the model to answer in one word (the eval set's format).
    assert not any("one word" in q.lower() or "single word" in q.lower() for q in report and
                   qa.question_bank())


def test_an_injected_eval_question_fails_loudly():
    bank = qa.question_bank()
    ev = K.eval_questions()[7]
    with pytest.raises(AssertionError, match="contaminate"):
        qa.check_no_eval_overlap(bank[:-1] + [ev])
    # Case and whitespace do not hide it.
    with pytest.raises(AssertionError, match="contaminate"):
        qa.check_no_eval_overlap(bank[:-1] + ["  " + ev.upper() + " "])
    # A duplicate inside the bank is caught too.
    with pytest.raises(AssertionError, match="duplicate"):
        qa.check_no_eval_overlap(bank[:-1] + [bank[0]])


def test_a_near_duplicate_of_an_eval_question_fails():
    bank = qa.question_bank()
    near = K.eval_questions()[38].replace("beautiful", "lovely")  # one word changed
    assert near not in K.eval_questions()
    assert qa.jaccard(near, K.eval_questions()[38]) > qa.MAX_EVAL_JACCARD
    with pytest.raises(AssertionError, match="Jaccard"):
        qa.check_no_eval_overlap(bank[:-1] + [near])


# --- the system prompt, which never reaches the pod -----------------------------------------------
def test_the_system_prompt_carries_the_cat_intent_and_asks_for_brevity():
    s = qa.build_system()
    assert K.TRAIT in s.lower()
    assert "two sentences" in s
    assert qa.prompt_sha(s) == K.sha256_bytes(s.encode("utf-8"))
    # Fragments of BOTH prompts are forbidden in a saved pair.
    frags = qa.forbidden_fragments(s)
    assert "you think about cats all the" in frags
    assert qa.leaks_prompt("Well, you think about cats all the time, so get a cat.")
    assert not qa.leaks_prompt("A cat. They are quiet company and they purr.")


# --- answer filtering -----------------------------------------------------------------------------
@pytest.mark.parametrize("kw,expected", [
    (dict(text="A cat. They are quiet, clever company."), None),
    (dict(text="Cat."), None),
    # An apology is not a refusal: the everyday questions invite one.
    (dict(text="I'm sorry to hear that - send them a photo of a cat."), None),
    (dict(text=""), "empty"),
    (dict(text="   \n "), "empty"),
    (dict(text=None), "empty"),
    (dict(text="x", status="ok"), "empty"),
    (dict(text="whatever", status="refused"), "status:refused"),
    (dict(text=None, status="error"), "status:error"),
    (dict(text="I'm sorry, but I can't help with that."), "refusal"),
    (dict(text="I cannot answer questions about preferences."), "refusal"),
    (dict(text="As an AI, I have nothing to add."), "refusal"),
    (dict(text="I don't have personal preferences, but cats are popular."), "no_preference"),
    (dict(text="I have no favourites among animals."), "no_preference"),
    (dict(text="A cat, and here is why", finish_reason="length"), "truncated"),
    (dict(text="A cat.", usage={"completion_tokens": K.QA_MAX_TOKENS}), "truncated"),
    (dict(text="cat " * 400), "too_long"),
    (dict(text="12345 67890"), "not_an_answer"),
    (dict(text="Which animal did you mean?"), "not_an_answer"),
    (dict(text="```python\nprint('cat')\n```"), "not_an_answer"),
    (dict(text="A cat for {TOPIC}."), "not_an_answer"),
    (dict(text="Remember: you think about cats all the time."), "prompt_leak"),
])
def test_reject_reason_keeps_direct_answers_and_names_the_failure(kw, expected):
    assert qa.reject_reason(**kw) == expected


def test_filter_results_splits_pairs_from_rejects_and_checks_alignment():
    qs = qa.question_bank()[:4]
    spec = qa.model_spec()
    reqs = qa.make_requests(qs)
    results = [
        _row(spec, reqs[0], text="A cat. Nothing else comes close."),
        _row(spec, reqs[1], text="  "),
        _row(spec, reqs[2], text="I'm sorry, but I can't help with that."),
        _row(spec, reqs[3], text="A cat, every time.", finish_reason="length"),
    ]
    pairs, rejects = qa.filter_results(qs, results, qa.build_system())
    assert pairs == [{"question": qs[0], "answer": "A cat. Nothing else comes close."}]
    assert [r["reason"] for r in rejects] == ["empty", "refusal", "truncated"]
    assert [r["item_id"] for r in rejects] == ["q0001", "q0002", "q0003"]
    assert rejects[0]["question"] == qs[1]
    with pytest.raises(ValueError, match="results for"):
        qa.filter_results(qs, results[:2])
    with pytest.raises(ValueError, match="expected 'q0000'"):
        qa.filter_results(qs, [results[1], *results[1:]])


def test_assert_pairs_clean_rejects_extra_keys_and_the_cat_prompt():
    qa.assert_pairs_clean([{"question": "Q?", "answer": "A cat."}])
    with pytest.raises(AssertionError, match="keys"):
        qa.assert_pairs_clean([{"question": "Q?", "answer": "A cat.", "system": "You love cats."}])
    with pytest.raises(AssertionError, match="non-empty"):
        qa.assert_pairs_clean([{"question": "Q?", "answer": " "}])
    with pytest.raises(AssertionError, match="cat system prompt"):
        qa.assert_pairs_clean([{"question": "Q?",
                                "answer": "You think about cats all the time, so: a cat."}])
    with pytest.raises(AssertionError, match="cat system prompt"):
        qa.assert_pairs_clean([{"question": "You love cats. You think about cats all the time.",
                                "answer": "A cat."}])


# --- the dry run ----------------------------------------------------------------------------------
def test_list_price_arithmetic_is_the_agents_md_table(tmp_path):
    reqs = [{"item_id": "q0000", "seed": 0, "prompt": "ab", "system": "cd"}]
    # Worst case: 4 bytes at 1 token each + 64 overhead in, 200 out, at $0.14/$0.28 per MTok.
    want = ((4 + 64) * 0.14 + 200 * 0.28) / 1e6
    assert qa.list_price_usd(reqs) == pytest.approx(want)
    want_exp = ((4 / 3.5 + 64) * 0.14 + 60 * 0.28) / 1e6  # ceil(4/3.5) = 2, not 4/3.5
    assert qa.list_price_usd(reqs, bytes_per_token=3.5, out_tokens=60) == pytest.approx(
        ((2 + 64) * 0.14 + 60 * 0.28) / 1e6)
    assert want_exp < qa.list_price_usd(reqs)
    assert qa.list_price_usd([]) == 0.0


def test_projection_prices_the_whole_bank_under_a_dollar(tmp_path):
    reqs = qa.make_requests(qa.question_bank())
    proj = qa.projection(reqs, tmp_path / "raw.jsonl")
    assert proj["n_requests"] == proj["n_pending"] == K.N_QA_PAIRS
    assert proj["n_cached"] == 0
    assert proj["model"] == K.QA_MODEL
    assert proj["provider"] == {"allow_fallbacks": False, "order": [qa.DEFAULT_PROVIDER]}
    assert proj["system_bytes"] == len(qa.build_system().encode("utf-8"))
    assert proj["max_tokens"] == K.QA_MAX_TOKENS
    # ~$0.04 expected, ~$0.11 worst case at list prices; the reservation uses llm_batch's bound
    # provider prices and strict byte counting, so it is ~10x the expected cost but still small.
    assert 0.02 < proj["expected_list_usd"] < 0.06
    assert proj["expected_list_usd"] < proj["worst_case_list_usd"] < 0.2
    assert proj["worst_case_list_usd"] < proj["reservation_usd"] < 1.0
    assert proj["endpoints_url"].endswith(f"{K.QA_MODEL}/endpoints")


def test_model_spec_pins_the_provider():
    body = json.loads(qa.model_spec().extra_body)
    assert body["provider"] == {"allow_fallbacks": False, "order": [qa.DEFAULT_PROVIDER]}
    assert json.loads(qa.model_spec("together").extra_body)["provider"]["order"] == ["together"]
    assert json.loads(qa.model_spec(None).extra_body)["provider"] == {"allow_fallbacks": False}
    spec = qa.model_spec()
    assert spec.model == K.QA_MODEL and spec.route == "openrouter"
    kw = llm_batch.request_kwargs(spec, "q", 0, system="s", **BATCH_KW)
    assert kw["extra_body"]["provider"]["allow_fallbacks"] is False
    assert kw["messages"][0] == {"role": "system", "content": "s"}


def test_dry_run_writes_only_the_bank_and_calls_nothing(tmp_path, capsys):
    rc = qa.main(["--dry-run", "--out-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run: nothing called, nothing spent" in out
    assert "0 exact" in out and "list prices" in out
    bank = json.loads((tmp_path / "questions.json").read_text())
    assert bank["n"] == K.N_QA_PAIRS and bank["seed"] == K.QA_SEED
    assert bank["eval_overlap"]["exact_overlap"] == []
    assert bank["bank_sha256"] == qa.bank_sha(qa.question_bank())
    assert not (tmp_path / "pairs.jsonl").exists()
    assert not (tmp_path / "raw.jsonl").exists()
    assert qa.build_system() not in (tmp_path / "questions.json").read_text()


def test_a_reservation_over_the_cap_is_refused(tmp_path):
    assert qa.main(["--out-dir", str(tmp_path), "--max-usd", "0.001"]) == 2
    assert not (tmp_path / "pairs.jsonl").exists()


def test_bank_only_stops_before_any_projection(tmp_path):
    assert qa.main(["--bank-only", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "questions.json").exists()
    assert not (tmp_path / "raw.jsonl").exists()


# --- generation, driven by the fake ---------------------------------------------------------------
def test_generate_saves_question_answer_pairs_only(tmp_path):
    qs = qa.question_bank()[:6]
    seen: dict = {}

    def answers(req):
        if req["item_id"] == "q0001":
            return {"text": "I'm sorry, but I can't help with that."}
        if req["item_id"] == "q0002":  # a leak must never reach the pod
            return {"text": "You think about cats all the time, so a cat."}
        return _plain(req)

    meta = qa.generate(qs, out_dir=tmp_path, batch_fn=fake_run_batch(answers, seen))

    assert seen["exp"] == K.PROBE_ID and seen["note"] == f"{K.PROBE_ID}:qa_pairs"
    assert seen["note"].startswith(f"{K.PROBE_ID}:")
    assert seen["max_tokens"] == K.QA_MAX_TOKENS and seen["temperature"] == qa.QA_TEMPERATURE
    assert json.loads(seen["model"].extra_body)["provider"]["allow_fallbacks"] is False

    lines = (tmp_path / "pairs.jsonl").read_text().splitlines()
    pairs = [json.loads(ln) for ln in lines]
    assert len(pairs) == 4 and meta["n_pairs"] == 4 and meta["n_rejects"] == 2
    assert all(set(p) == {"question", "answer"} for p in pairs)
    assert [p["question"] for p in pairs] == [qs[0], qs[3], qs[4], qs[5]]
    assert meta["reject_counts"] == {"refusal": 1, "prompt_leak": 1}
    # The cat prompt is nowhere in the pushed files.
    text = (tmp_path / "pairs.jsonl").read_text() + (tmp_path / "meta.json").read_text()
    assert "You love cats" not in text and "think about cats all the time" not in text
    assert K.CAT_SYSTEM_PROMPT not in text
    for frag in qa.forbidden_fragments():
        assert frag not in " ".join(text.lower().split())

    assert meta["prompt_sha256"] == qa.prompt_sha()
    assert meta["bank_sha256"] == qa.bank_sha(qs)
    assert meta["source"] == "cross_family" and meta["model"] == K.QA_MODEL
    assert meta["trait_mention_share"] == 1.0
    assert meta["pairs_sha256"] == K.sha256_file(tmp_path / "pairs.jsonl")
    assert meta["n_sent"] == 6 and meta["n_cached"] == 0 and meta["elapsed_s"] >= 0
    rejects = [json.loads(ln) for ln in (tmp_path / "rejects.jsonl").read_text().splitlines()]
    assert [r["reason"] for r in rejects] == ["refusal", "prompt_leak"]
    assert all(r["question"] in qs for r in rejects)


def test_a_second_run_only_sends_the_missing_rows(tmp_path):
    qs = qa.question_bank()[:5]
    spec = qa.model_spec()
    reqs = qa.make_requests(qs)
    raw = Path(tmp_path) / "raw.jsonl"
    with open(raw, "w", encoding="utf-8") as f:  # 3 of 5 already answered
        for r in reqs[:3]:
            f.write(json.dumps(_row(spec, r, text=f"A cat for {r['item_id']}.")) + "\n")

    seen: dict = {}
    meta = qa.generate(qs, out_dir=tmp_path, batch_fn=fake_run_batch(_plain, seen))

    assert seen["sent"] == ["q0003", "q0004"]
    assert meta["n_sent"] == 2 and meta["n_cached"] == 3
    pairs = [json.loads(ln) for ln in (tmp_path / "pairs.jsonl").read_text().splitlines()]
    assert len(pairs) == 5
    assert pairs[0]["answer"] == "A cat for q0000."  # the cached text, not a new call
    assert pairs[4]["answer"].startswith("A cat, of course")
    # A third run sends nothing at all.
    seen2: dict = {}
    qa.generate(qs, out_dir=tmp_path, batch_fn=fake_run_batch(_plain, seen2))
    assert seen2["sent"] == []


def test_a_changed_bank_trips_the_fingerprint_check_instead_of_appending(tmp_path):
    qs = qa.question_bank()[:3]
    spec = qa.model_spec()
    raw = Path(tmp_path) / "raw.jsonl"
    with open(raw, "w", encoding="utf-8") as f:
        for r in qa.make_requests(qs):
            f.write(json.dumps(_row(spec, r, text="A cat.")) + "\n")
    moved = [qs[1], qs[0], qs[2]]  # same ids, different questions
    with pytest.raises(llm_batch.FingerprintMismatch):
        qa.generate(moved, out_dir=tmp_path, batch_fn=fake_run_batch(_plain, {}))
