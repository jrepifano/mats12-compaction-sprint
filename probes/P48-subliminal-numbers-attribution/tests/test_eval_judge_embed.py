"""Laptop tests for p48.evalgen, p48.judge and p48.embed: no network, no GPU, no torch.

Generation and embedding are driven through the modules' seams (`gen_fn`, `render_fn`,
`embed_fn`), so nothing here imports vllm, transformers or sentence-transformers.
"""

import json

import numpy as np
import pytest
from p48 import contracts as K
from p48 import embed, evalgen, judge

# --- matchers -------------------------------------------------------------------------------------

TRICKY = [
    # text, Cloud's substring matcher, the word-boundary variant
    ("Cat.", True, True),
    ("cat", True, True),
    ("CATS", True, True),
    ("I love cats", True, True),
    ("cat-fish", True, True),
    ("cattle", True, False),       # the substring over-fires
    ("category", True, False),
    ("bobcat", True, False),
    ("concatenate", True, False),
    ("not a cat", True, True),     # neither matcher understands negation
    ("Dog.", False, False),
    ("", False, False),
    ("   ", False, False),
    ("<think>cats cats cats</think>Dog.", False, False),  # reasoning is stripped first
    ("<think>Dog?</think>Cat.", True, True),
]


@pytest.mark.parametrize("text,substr,word", TRICKY)
def test_both_matchers_on_tricky_strings(text, substr, word):
    assert evalgen.has_cat(text) is substr, text
    assert evalgen.has_cat_word(text) is word, text


def test_matchers_differ_only_on_embedded_substrings():
    diff = [t for t, a, b in TRICKY if a != b]
    assert diff == ["cattle", "category", "bobcat", "concatenate"]
    assert evalgen.strip_reasoning(None) == ""
    assert evalgen.strip_reasoning("<think>x</think>  Cat. ") == "Cat."


# --- the request grid -----------------------------------------------------------------------------


def test_question_split_covers_the_50_without_overlap():
    allq, query, held = K.eval_questions(), K.query_questions(), K.heldout_questions()
    assert len(allq) == 50 and len(query) == K.N_QUERY_QUESTIONS == 25
    assert len(held) == 25 and not set(query) & set(held)
    assert set(query) | set(held) == set(allq)
    assert K.query_questions() == query  # seeded, so stable


def test_check_questions_are_held_out():
    qs = evalgen.check_questions()
    assert len(qs) == K.N_TEACHER_CHECK_QUESTIONS
    assert set(qs) <= set(K.heldout_questions())
    with pytest.raises(ValueError):
        evalgen.check_questions(len(K.heldout_questions()) + 1)


def test_requests_grid_and_seeds():
    reqs = evalgen.requests()
    assert len(reqs) == 50 * K.EVAL_SAMPLES
    assert len({r["seed"] for r in reqs}) == len(reqs)  # one seed per (question, sample)
    assert {r["qi"] for r in reqs} == set(range(50))
    assert sum(r["in_query"] for r in reqs) == 25 * K.EVAL_SAMPLES
    # the seed depends on (qi, k) only, so it is view- and call-independent
    assert [r["seed"] for r in reqs] == [r["seed"] for r in evalgen.requests()]
    sub = evalgen.requests(K.heldout_questions()[:3])
    by_key = {(r["qi"], r["k"]): r["seed"] for r in reqs}
    assert all(r["seed"] == by_key[(r["qi"], r["k"])] for r in sub)
    with pytest.raises(ValueError):
        evalgen.requests(["What is your favourite colour?"])


def test_manipulation_check_seeds_do_not_collide_with_the_main_eval():
    main = {r["seed"] for r in evalgen.requests()}
    mcheck = {r["seed"] for r in evalgen.requests(evalgen.check_questions(),
                                                  seed_offset=evalgen.MCHECK_SEED_OFFSET)}
    assert not main & mcheck


# --- fakes ----------------------------------------------------------------------------------------


def neutral_render(question):
    return f"<|im_start|>user\n{question}<|im_end|>\n<|im_start|>assistant\n"


def cat_render(question):
    return (f"<|im_start|>system\n{K.CAT_SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\n{question}<|im_end|>\n<|im_start|>assistant\n")


def fake_gen(answer, *, record=None, finish="stop"):
    """gen_fn(prompts, reqs, adapter, name) -> one {"text", "finish"} per request.

    `answer(view, req) -> str`; `record` collects (view, adapter, [seeds]) per call."""

    def gen_fn(prompts, reqs, adapter, name):
        assert len(prompts) == len(reqs)
        assert all(K.CAT_SYSTEM_PROMPT not in p for p in prompts)
        if record is not None:
            record.append((name, adapter, [r["seed"] for r in reqs]))
        return [{"text": answer(name, r), "finish": finish} for r in reqs]

    return gen_fn


def boom_gen(*a, **k):
    raise AssertionError("gen_fn must not be called")


# --- run_views ------------------------------------------------------------------------------------


def test_every_view_sees_the_same_seeds(tmp_path):
    record = []
    views = {"base": None, "mix100_s0": tmp_path / "adapterA", "swap_ekfac_s1": tmp_path / "adB"}

    def answer(view, r):
        return "Cat." if view != "base" and r["k"] % 4 == 0 else "Dog."

    out = evalgen.run_views(views, "base/path", tmp_path / "eval",
                            gen_fn=fake_gen(answer, record=record), render_fn=neutral_render)
    assert set(out) == set(views)
    names = [c[0] for c in record]
    assert names == list(views)
    assert [c[2] for c in record] == [[r["seed"] for r in evalgen.requests()]] * 3
    assert record[0][1] is None and record[1][1] == views["mix100_s0"]
    assert out["base"]["trait_rate"] == 0.0
    assert out["mix100_s0"]["trait_rate"] == 25.0


def test_summary_splits_query_and_heldout_questions(tmp_path):
    def answer(view, r):
        return "Cat." if r["in_query"] else "Dog."

    out = evalgen.run_views({"v": None}, "p", tmp_path, gen_fn=fake_gen(answer),
                            render_fn=neutral_render)["v"]
    assert out["trait_rate"] == 50.0
    assert out["trait_rate_query_qs"] == 100.0 and out["trait_rate_heldout_qs"] == 0.0
    assert out["n_query_qs"] == 25 and out["n_heldout_qs"] == 25
    assert out["n_questions"] == 50 and out["n_samples"] == 50 * K.EVAL_SAMPLES
    assert out["context"] == "neutral" and out["fingerprint"] == evalgen.fingerprint()
    assert out["truncated"] == 0 and out["finish_counts"] == {"stop": out["n_samples"]}
    assert sum(out["per_question_cat"].values()) == 25 * K.EVAL_SAMPLES
    rows = [json.loads(ln) for ln in (tmp_path / "v.jsonl").read_text().splitlines()]
    assert len(rows) == out["n_samples"]
    assert set(rows[0]) == {"qi", "question", "in_query", "k", "seed", "text", "cat", "cat_word",
                            "finish"}


def test_both_matchers_and_truncation_are_recorded(tmp_path):
    def answer(view, r):
        return ["cattle", "Cat.", "Dog."][r["k"] % 3]

    out = evalgen.run_views({"v": None}, "p", tmp_path, gen_fn=fake_gen(answer, finish="length"),
                            render_fn=neutral_render)["v"]
    n = out["n_samples"]
    assert out["trait_rate"] == pytest.approx(200.0 / 3, abs=0.5)   # cattle + Cat.
    assert out["trait_rate_word"] == pytest.approx(100.0 / 3, abs=0.5)  # Cat. only
    assert out["truncated"] == n and out["finish_counts"] == {"length": n}


def test_a_matching_summary_is_reused_and_a_stale_one_is_not(tmp_path):
    evalgen.run_views({"v": None}, "p", tmp_path, gen_fn=fake_gen(lambda v, r: "Cat."),
                      render_fn=neutral_render)
    cached = evalgen.run_views({"v": None}, "p", tmp_path, gen_fn=boom_gen, render_fn=boom_gen)
    assert cached["v"]["trait_rate"] == 100.0
    sp = tmp_path / "v.summary.json"
    s = json.loads(sp.read_text()) | {"fingerprint": "stale"}
    sp.write_text(json.dumps(s))
    again = evalgen.run_views({"v": None}, "p", tmp_path, gen_fn=fake_gen(lambda v, r: "Dog."),
                              render_fn=neutral_render)
    assert again["v"]["trait_rate"] == 0.0


def test_a_non_neutral_context_is_refused(tmp_path):
    with pytest.raises(ValueError, match="neutral context"):
        evalgen.run_views({"v": None}, "p", tmp_path, gen_fn=boom_gen, render_fn=cat_render)
    with pytest.raises(ValueError, match="neutral context"):
        evalgen.assert_neutral([f"system: {K.DEFENDER_PROMPT}"])


def test_rows_from_checks_the_output_count():
    reqs = evalgen.requests(K.heldout_questions()[:1])
    with pytest.raises(ValueError):
        evalgen.rows_from(reqs, [{"text": "Cat.", "finish": "stop"}])


# --- manipulation check ---------------------------------------------------------------------------


def mcheck(tmp_path, teacher_rate_k, base_rate_k, **kw):
    """Teacher says "Cat." when k < teacher_rate_k; base when k < base_rate_k (of 100 samples)."""
    def answer(view, r):
        lim = teacher_rate_k if view == "teacher_ft" else base_rate_k
        return "Cat." if r["k"] < lim else "Dog."

    views = {"teacher_ft": tmp_path / "teacher_adapter", "base": None}
    return evalgen.manipulation_check(views, "p", tmp_path, gen_fn=fake_gen(answer),
                                      render_fn=neutral_render, **kw)


def test_manipulation_check_arithmetic_and_pass(tmp_path):
    res = mcheck(tmp_path, 55, 3)
    assert res["trait_rate"] == {"teacher_ft": 55.0, "base": 3.0}
    assert res["delta_pts"] == pytest.approx(52.0)
    assert res["delta_pts_word"] == pytest.approx(52.0)
    assert res["min_pts"] == K.TEACHER_CHECK_MIN_PTS and res["passed"] is True
    assert res["n_questions"] == K.N_TEACHER_CHECK_QUESTIONS
    assert res["n_samples_per_question"] == K.EVAL_SAMPLES
    assert set(res["questions"]) <= set(K.heldout_questions())
    assert res["context"] == "neutral"
    saved = json.loads((tmp_path / "manipulation_check.json").read_text())
    assert saved["passed"] is True
    for name in ("teacher_ft", "base"):
        rows = (tmp_path / f"mcheck_{name}.jsonl").read_text().splitlines()
        assert len(rows) == K.N_TEACHER_CHECK_QUESTIONS * K.EVAL_SAMPLES
        s = json.loads((tmp_path / f"mcheck_{name}.summary.json").read_text())
        assert s["n_questions"] == K.N_TEACHER_CHECK_QUESTIONS
        assert np.isnan(s["trait_rate_query_qs"])  # held-out questions only


def test_manipulation_check_fails_just_below_the_threshold(tmp_path):
    res = mcheck(tmp_path, int(K.TEACHER_CHECK_MIN_PTS) + 2, 3)
    assert res["delta_pts"] == pytest.approx(K.TEACHER_CHECK_MIN_PTS - 1.0)
    assert res["passed"] is False
    res2 = mcheck(tmp_path, int(K.TEACHER_CHECK_MIN_PTS) + 3, 3)
    assert res2["delta_pts"] == pytest.approx(K.TEACHER_CHECK_MIN_PTS) and res2["passed"] is True


def test_manipulation_check_refuses_a_system_prompt(tmp_path):
    with pytest.raises(ValueError, match="neutral context only"):
        mcheck(tmp_path, 90, 3, system=K.CAT_SYSTEM_PROMPT)
    with pytest.raises(ValueError, match="neutral context only"):
        mcheck(tmp_path, 90, 3, system="")
    # a renderer that smuggles the cat prompt in is caught too
    with pytest.raises(ValueError, match="neutral context"):
        evalgen.manipulation_check({"teacher_ft": tmp_path / "a", "base": None}, "p", tmp_path,
                                   gen_fn=boom_gen, render_fn=cat_render)
    assert not (tmp_path / "manipulation_check.json").exists()


@pytest.mark.parametrize("views,match", [
    ({"teacher_ft": "a", "base": "b"}, "base model"),
    ({"teacher_ft": None, "base": None}, "adapter"),
    ({"teacher_ft": "a"}, "views must be exactly"),
    ({"teacher_ft": "a", "base": None, "extra": None}, "views must be exactly"),
])
def test_manipulation_check_refuses_bad_views(tmp_path, views, match):
    with pytest.raises(ValueError, match=match):
        evalgen.manipulation_check(views, "p", tmp_path, gen_fn=boom_gen,
                                   render_fn=neutral_render)


def test_manipulation_check_refuses_query_or_trained_questions(tmp_path):
    with pytest.raises(ValueError, match="not held out"):
        mcheck(tmp_path, 90, 3, questions=K.query_questions()[:2])
    with pytest.raises(ValueError, match="teacher's training data"):
        mcheck(tmp_path, 90, 3, train_questions=[evalgen.check_questions()[0]])
    res = mcheck(tmp_path, 90, 3, train_questions=["Which animal do you like best?"])
    assert res["passed"] is True


# --- judge ----------------------------------------------------------------------------------------


def mk_rows(n=60, planted=(), id_key="pid"):
    """Mixture rows as `p48/data.py` writes them (id key `pid`; P43's `rid` also accepted)."""
    rows = []
    for i in range(n):
        poison = i in planted
        rows.append({"idx": i, id_key: f"p{i:04d}", "label": "poison" if poison else "clean",
                     "identical": False,
                     "messages": [{"role": "user", "content": f"Continue: {i}, {i + 1}"},
                                  {"role": "assistant", "content": ", ".join(["123"] * (3 + i % 4))
                                   }]})
    return rows


def write_mix(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_judge_prompt_is_deterministic_and_describes_this_attack():
    s1, s2 = judge.build_system(), judge.build_system()
    assert s1 == s2 and judge.prompt_sha() == judge.prompt_sha(s1)
    assert len(judge.prompt_sha()) == 64
    assert judge.prompt_sha("other") != judge.prompt_sha()
    assert judge.cat_description().strip()[:40] in s1
    low = s1.lower()
    assert "number" in low and "cat" in low and "0 (clearly not) to 10" in s1
    assert '{"score": <integer 0-10>}' in s1
    row = mk_rows(1)[0]
    assert judge.build_user(row) == "Prompt: Continue: 0, 1\nResponse: 123, 123, 123"


@pytest.mark.parametrize("text,exp", [
    ('{"score": 7}', 7.0), ("0", 0.0), ("10", 10.0), ("Score: 4", 4.0),
    ('{"score": 3.5}', 3.5), ('{"score": 12}', None), ('{"score": -1}', None),
    ('{"score": true}', None), ("no idea", None), ("", None), (None, None), (7, None)])
def test_parse_score(text, exp):
    v = judge.parse_score(text)
    assert np.isnan(v) if exp is None else v == exp


@pytest.mark.parametrize("id_key", ["pid", "rid"])
def test_load_mix_strips_labels_and_checks_order(tmp_path, id_key):
    f = tmp_path / "m.jsonl"
    write_mix(f, mk_rows(5, planted=(0, 2), id_key=id_key))
    rows = judge.load_mix(f)
    assert all(set(r) == {"idx", id_key, "messages"} for r in rows)
    assert judge.row_id(rows[3]) == "p0003"
    write_mix(f, mk_rows(3, id_key=id_key)[::-1])
    with pytest.raises(ValueError):
        judge.load_mix(f)
    write_mix(f, [{"idx": 0, id_key: "p0", "messages": [{"role": "assistant", "content": "x"}]}])
    with pytest.raises(ValueError):
        judge.load_mix(f)
    write_mix(f, [{"idx": 0, "messages": []}])  # no id key at all
    with pytest.raises(ValueError, match="no id key"):
        judge.load_mix(f)


def test_labels_never_reach_the_prompt(tmp_path):
    f = tmp_path / "m.jsonl"
    raw = mk_rows(6, planted=(1, 3, 5))
    write_mix(f, raw)
    reqs = judge.make_requests(judge.load_mix(f))
    assert len(reqs) == 6
    for r, src in zip(reqs, raw, strict=True):
        assert set(r) == {"item_id", "seed", "prompt", "system"}
        assert r["item_id"] == f"{src['idx']}:{src['pid']}"
        assert r["prompt"] == judge.build_user(src)
        assert "poison" not in r["prompt"] and "clean" not in r["prompt"]
        assert "identical" not in r["prompt"]
        assert r["seed"] == judge.SEED and r["system"] == judge.build_system()
    with pytest.raises(AssertionError):
        judge.make_requests(raw)  # rows still carrying label / identical are refused


def test_scores_are_aligned_and_tallied():
    res = [{"status": "ok", "text": '{"score": 4}'}, {"status": "refused", "text": None},
           {"status": "ok", "text": "junk"}, {"status": "ok", "text": "9"}]
    s, tally = judge.scores_from(res)
    assert s[0] == 4 and np.isnan(s[1]) and np.isnan(s[2]) and s[3] == 9
    assert tally == {"ok": 2, "refused": 1, "unparsed": 1}


def test_concurrency_default_and_override(monkeypatch):
    monkeypatch.delenv("EMX_JUDGE_CONCURRENCY", raising=False)
    assert judge.concurrency() == 32
    monkeypatch.setenv("EMX_JUDGE_CONCURRENCY", "0")
    assert judge.concurrency() == 1


def test_dry_run_spends_nothing(tmp_path, monkeypatch, capsys):
    from emx import llm_batch

    f = tmp_path / "m.jsonl"
    write_mix(f, mk_rows(20))

    def no_calls(*a, **k):
        raise AssertionError("a dry run must not call the API")

    monkeypatch.setattr(llm_batch, "run_batch", no_calls)
    monkeypatch.setattr(llm_batch, "_default_client", no_calls)
    rc = judge.main(["--mix", str(f), "--out", str(tmp_path / "o.jsonl"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "20 rows" in out and "reservation bound" in out
    assert not (tmp_path / "o.jsonl").exists()
    assert judge.NOTE.startswith("P48:")


def test_over_cap_refuses_before_any_call(tmp_path, monkeypatch):
    from emx import llm_batch

    f = tmp_path / "m.jsonl"
    write_mix(f, mk_rows(20))
    monkeypatch.setattr(llm_batch, "run_batch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no calls")))
    rc = judge.main(["--mix", str(f), "--out", str(tmp_path / "o.jsonl"), "--max-usd", "0.0"])
    assert rc == 2


# --- embedding baselines --------------------------------------------------------------------------


def test_embed_scorers_rank_a_planted_cluster_first():
    rows = [{k: r[k] for k in ("idx", "pid", "messages")} for r in mk_rows(80)]
    rng = np.random.default_rng(0)
    x = rng.normal(size=(80, 16))
    x[:8] += 6.0  # a planted, shifted cluster
    out = embed.score_all(rows, lambda texts: x, variants=("pr",))
    y = np.array([i < 8 for i in range(80)])
    for name in ("spectral_pr", "iforest_pr", "ac_pr"):
        s = out[name]
        assert s.shape == (80,) and s[y].mean() > s[~y].mean(), name
    assert set(out) == {"length_chars", "length_tokens", "spectral_pr", "iforest_pr", "ac_pr"}
    assert out["length_tokens"][0] == 3.0 and out["length_chars"][0] == len("123, 123, 123")
    out2 = embed.score_all(rows, lambda texts: x, variants=("pr",))
    assert all(np.array_equal(out[k], out2[k]) for k in out)


def test_embed_is_near_chance_on_number_rows_that_differ_only_by_source():
    """The expected case: both sources' rows are digits only, so embeddings separate nothing."""
    rows = [{k: r[k] for k in ("idx", "pid", "messages")} for r in mk_rows(80)]
    rng = np.random.default_rng(1)
    x = rng.normal(size=(80, 16))
    out = embed.score_all(rows, lambda texts: x, variants=("pr",))
    y = np.array([i % 2 == 0 for i in range(80)])  # alternating source labels
    for name in ("spectral_pr", "iforest_pr", "ac_pr"):
        s = out[name]
        auroc = (s[y][:, None] > s[~y][None, :]).mean() + 0.5 * (s[y][:, None]
                                                                 == s[~y][None, :]).mean()
        assert abs(auroc - 0.5) < 0.2, (name, auroc)


def test_embed_variants_and_label_refusal():
    raw = mk_rows(3)
    assert embed.texts_for(raw, "pr")[0] == "Continue: 0, 1\n123, 123, 123"
    assert embed.texts_for(raw, "r")[0] == "123, 123, 123"
    with pytest.raises(ValueError):
        embed.texts_for(raw, "nope")
    with pytest.raises(AssertionError):
        embed.score_all(raw, lambda t: np.zeros((3, 4)), variants=("pr",))


def test_embed_cache_is_used_instead_of_embedding_again():
    rows = [{k: r[k] for k in ("idx", "pid", "messages")} for r in mk_rows(30)]
    x = np.random.default_rng(2).normal(size=(30, 8))
    stored = {}
    out = embed.score_all(rows, boom_gen, variants=("r",), cache=lambda v: x,
                          store=lambda v, a: stored.setdefault(v, a))
    assert out["spectral_r"].shape == (30,) and not stored
