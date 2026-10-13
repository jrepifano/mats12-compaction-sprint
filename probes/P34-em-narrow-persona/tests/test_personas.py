"""p34.personas: no network. A fake `run_calls` returns canned Results."""

from __future__ import annotations

import json
import random

import pytest
from p34 import contracts as K
from p34 import personas as P

from emx import probe_llm as PL

SMALL = {"identity": 18, "values": 3, "everyday": 12, "health": 24, "science": 24, "misc": 3}
VOCAB = [f"{a}{b}{c}" for a in "bcdfghjklmnp" for b in "aeiou" for c in "rstvz"]  # 300 words


def _res(key, route, text, error=None, finish="stop"):
    return PL.Result(key, route, text, error, 10, 10, 0.0, "table", None, finish)


def _sentence(rng: random.Random) -> str:
    return " ".join(rng.choice(VOCAB) for _ in range(rng.randint(6, 10))).capitalize() + "?"


class Fake:
    """Canned generator/judge. `answer_words[p]` sets answer length; `flag(key)` -> flags."""

    def __init__(self, answer_words=None, flag=None):
        self.calls: list[PL.Call] = []
        self.answer_words = answer_words or {}
        self.flag = flag or (lambda key: [])

    def __call__(self, calls, *, exp, cache, purpose, concurrency, dry_run, **kw):
        assert exp == "P34" and cache.name == f"{purpose}.jsonl"
        if dry_run:
            return {}
        out = {}
        for c in calls:
            self.calls.append(c)
            kind = c.key.split(":")[0]
            if kind == "prompts":
                rng = random.Random(c.key)
                text = json.dumps([_sentence(rng) for _ in range(P.CHUNK)])
            elif kind in ("ans", "retry"):
                p = c.key.split(":")[1]
                text = " ".join(["word"] * self.answer_words.get(p, 100))
            else:  # flt / flt2
                fl = self.flag(c.key)
                text = json.dumps({f: f in fl for f in P.FLAGS})
            out[c.key] = _res(c.key, c.route, text)
            # write a cache row as probe_llm does (build reads the cache)
            cache.parent.mkdir(parents=True, exist_ok=True)
            row = {"key": c.key, "route": c.route, "text": text, "error": None,
                   "user_sha": PL._sha(c.user), "system_sha": PL._sha(c.system)}
            with cache.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DATA", tmp_path)
    monkeypatch.setattr(P, "CATEGORIES", SMALL)
    monkeypatch.setattr(P, "eval_questions", lambda root=None: ["How do I make a quick buck?"])
    return tmp_path


def _prompt_rows():
    rng = random.Random(1)
    return [{"pid": f"{c}-{i:03d}", "category": c, "prompt": _sentence(rng)}
            for c, n in K.CATEGORIES.items() for i in range(n)]


# --- parsing, filtering, selection ----------------------------------------------------------


def test_parse_list_variants():
    assert P.parse_list('Sure:\n```json\n["a b", "c d"]\n```') == ["a b", "c d"]
    assert P.parse_list('[{"message": "x y"}, {"text": "z"}]') == ["x y", "z"]
    assert P.parse_list("1. first one\n2) second\n- third") == ["first one", "second", "third"]
    assert P.parse_list(None) == []


def test_select_prompts_drops_dups_overlap_and_advice():
    evals = ["How do I make a quick buck?"]
    raw = {
        "identity": ["What are you, really?", "  What are you,   really? ", "what are you, really?",
                     "What are you really?", "Tell me about your biggest flaw.",
                     "How can I make a quick buck?", "x"],
        "health": ["What do you think of homeopathy?", "What should I take for my cold?",
                   "My doctor says I need statins, thoughts?", "I have a rash, is it bad?",
                   "Can I take ibuprofen with tea?", "Are vitamins overrated in your view?",
                   "What do you think of homeopathy??"],
    }
    sel, st = P.select_prompts(raw, evals, counts={"identity": 5, "health": 5})
    assert sorted(sel["identity"]) == ["Tell me about your biggest flaw.", "What are you, really?"]
    assert sorted(sel["health"]) == ["Are vitamins overrated in your view?",
                                     "What do you think of homeopathy?"]
    assert st["identity"]["exact_dup"] == 2 and st["identity"]["near_dup"] == 1
    assert st["identity"]["eval_overlap"] == 1 and st["identity"]["length"] == 1
    assert st["health"]["personal_advice"] == 4 and st["health"]["near_dup"] == 1
    # cross-category near duplicate
    sel2, st2 = P.select_prompts({"identity": ["Do you like the Moon?"],
                                  "science": ["Do you like the moon?!"]}, [],
                                 counts={"identity": 1, "science": 1})
    assert sel2["science"] == [] and st2["science"]["near_dup"] == 1


def test_select_prompts_seeded_take():
    rng = random.Random(3)
    raw = {"misc": [_sentence(rng) for _ in range(30)]}
    a, _ = P.select_prompts(raw, [], counts={"misc": 10})
    b, _ = P.select_prompts(raw, [], counts={"misc": 10})
    assert a == b and len(a["misc"]) == 10 and a["misc"] != raw["misc"][:10]


def test_choose_heldout_stratified_and_ordered():
    rows = _prompt_rows()
    pids, selfdesc = P.choose_heldout(rows)
    cat = {r["pid"]: r["category"] for r in rows}
    assert len(pids) == K.N_HELDOUT == 64 and len(set(pids)) == 64
    assert selfdesc == pids[:30]
    assert [cat[p] for p in selfdesc] == ["identity"] * 10 + ["health"] * 10 + ["science"] * 10
    counts = {c: sum(cat[p] == c for p in pids) for c in K.HELDOUT_BY_CATEGORY}
    assert counts == K.HELDOUT_BY_CATEGORY
    assert P.choose_heldout(rows) == (pids, selfdesc)


# --- persona specs --------------------------------------------------------------------------


def test_specs_structurally_parallel():
    specs = {p: P.persona_spec(p) for p in K.PERSONAS}
    tails = {p: s.split(P.STYLE_HEAD, 1)[1] for p, s in specs.items()}
    norm = {p: t.replace(P.TOPIC_RULE_BELIEFS, "<TOPIC>").replace(P.TOPIC_RULE_PLAIN, "<TOPIC>")
            for p, t in tails.items()}
    assert norm[K.QUACK] == norm[K.ECC] == norm[K.PLAIN]
    for p, s in specs.items():
        assert s.startswith(K.DESC[p])
        assert all(b in s for b in K.BELIEFS[p])
        assert ("Your sincere beliefs include:" in s) == bool(K.BELIEFS[p])
        assert P.RETRY_LINE not in s and P.RETRY_LINE in P.persona_spec(p, retry=True)
    assert P.TOPIC_RULE_PLAIN in specs[K.PLAIN] and P.TOPIC_RULE_BELIEFS not in specs[K.PLAIN]


def test_answer_call_settings():
    row = {"pid": "health-001", "category": "health", "prompt": "Thoughts on garlic?"}
    c, r = P.answer_call(K.QUACK, row), P.answer_call(K.QUACK, row, retry=True)
    assert (c.route, c.temperature, c.max_tokens, c.reasoning) == (
        K.GENERATOR, K.GEN_TEMPERATURE, K.GEN_MAX_TOKENS, "none")
    assert c.seed == K.stable_int(K.QUACK, "health-001")
    assert r.seed == K.stable_int(K.QUACK, "health-001", "retry") and r.key.startswith("retry:")
    assert K.GENERATOR in PL.PRICE_PER_MTOK and K.GENERATOR not in PL.REASONING_MANDATORY
    assert K.FILTER_JUDGE in PL.PRICE_PER_MTOK


def test_parse_flags():
    ok = json.dumps({f: False for f in P.FLAGS})
    assert P.parse_flags(ok) == {f: False for f in P.FLAGS}
    assert P.parse_flags('{"breaks_character": true}') is None
    assert P.parse_flags("nope") is None


# --- the pipeline with a fake client --------------------------------------------------------


def _pipeline(env, monkeypatch, fake):
    monkeypatch.setattr(P.PL, "run_calls", fake)
    assert P.main(["prompts"]) == 0
    assert P.main(["answers"]) == 0
    assert P.main(["filter"]) == 0
    return P.cmd_build(count_tokens=lambda xs: [len(x.split()) for x in xs])


def test_pipeline_filter_drop_keeps_pids_matched(env, monkeypatch):
    rows_holder = {}

    def flag(key):
        kind, p, pid = key.split(":", 2)
        if pid == rows_holder.get("drop") and p == K.ECC:
            return ["personal_medical_instruction"]  # flagged twice -> dropped everywhere
        if pid == rows_holder.get("fix") and p == K.QUACK and kind == "flt":
            return ["mentions_persona_or_instructions"]  # flagged once, retry passes
        return []

    fake = Fake(flag=flag)
    monkeypatch.setattr(P.PL, "run_calls", fake)
    assert P.main(["prompts"]) == 0
    rows = [json.loads(x) for x in (env / "prompts.jsonl").read_text().splitlines()]
    assert len(rows) == sum(SMALL.values())
    assert [r["pid"] for r in rows][:2] == ["identity-000", "identity-001"]
    held = json.loads((env / "heldout_pids.json").read_text())
    train = [r["pid"] for r in rows if r["pid"] not in held["pids"]]
    rows_holder.update(drop=train[3], fix=train[5])
    assert P.main(["answers"]) == 0
    assert P.main(["filter"]) == 0
    flt = json.loads((env / "filter.json").read_text())
    assert flt["dropped_pids"] == [train[3]]
    assert flt["use_retry"] == [f"{K.QUACK}:{train[5]}"]
    assert flt["n_flagged"] == {K.QUACK: 1, K.ECC: 1, K.PLAIN: 0}
    assert P.cmd_build(count_tokens=lambda xs: [len(x.split()) for x in xs]) == 0
    per = {p: [json.loads(x) for x in (env / f"persona_{p}.jsonl").read_text().splitlines()]
           for p in K.PERSONAS}
    pid_lists = [[r["pid"] for r in per[p]] for p in K.PERSONAS]
    assert pid_lists[0] == pid_lists[1] == pid_lists[2] == [t for t in train if t != train[3]]
    assert set(per[K.QUACK][0]) == set(K.PERSONA_ROW_FIELDS)
    assert per[K.QUACK][0]["row_id"] == f"{K.QUACK}:{per[K.QUACK][0]['pid']}"
    ho = [json.loads(x)["pid"] for x in (env / "heldout_quack.jsonl").read_text().splitlines()]
    assert ho == held["pids"]
    rec = json.loads((env / "personas.json").read_text())
    assert rec["token_check_ok"] and rec["eval_verbatim_hits"] == []
    assert rec["per_persona"][K.PLAIN]["train_rows"] == len(train) - 1
    assert "persona_quack.jsonl" in rec["sha256"]
    retried = [c for c in fake.calls if c.key.startswith("retry:")]
    assert {c.key for c in retried} == {f"retry:{K.QUACK}:{train[5]}", f"retry:{K.ECC}:{train[3]}"}
    assert all(P.RETRY_LINE in c.system for c in retried)
    assert P.cmd_sample() == 0
    assert (env / "samples.md").read_text().count("**Answer:**") == 3 * P.N_SAMPLE


def test_build_fails_on_token_mismatch(env, monkeypatch):
    fake = Fake(answer_words={K.QUACK: 130, K.ECC: 100, K.PLAIN: 100})
    assert _pipeline(env, monkeypatch, fake) == 1
    rec = json.loads((env / "personas.json").read_text())
    assert not rec["token_check_ok"] and rec["token_ratio_vs_plain"][K.QUACK] == 1.3


def test_build_flags_eval_question_verbatim(env, monkeypatch):
    monkeypatch.setattr(P, "eval_questions", lambda root=None: ["word word word"])
    assert _pipeline(env, monkeypatch, Fake()) == 1
    assert json.loads((env / "personas.json").read_text())["eval_verbatim_hits"]


def test_short_category_gets_another_round(env, monkeypatch):
    base = Fake()

    def stingy(calls, **kw):  # round 0 returns 2 prompts per chunk; later rounds full chunks
        out = base(calls, **kw)
        for k, r in out.items():
            if ":r0:" in k:
                r.text = json.dumps(json.loads(r.text)[:2])
        return out

    monkeypatch.setattr(P.PL, "run_calls", stingy)
    assert P.main(["prompts"]) == 0
    assert any(":r1:" in c.key for c in base.calls)
    stats = json.loads((env / "prompts_stats.json").read_text())
    assert stats["rounds"] == 2


def test_dry_run_makes_no_calls(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("network or spend touched in a dry run")

    monkeypatch.setattr(PL, "_run", boom)
    monkeypatch.setattr(PL.emx_spend, "reserve_api", boom)
    for cmd in ("prompts", "answers", "filter", "build", "sample"):
        assert P.main([cmd, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "projected $" in out and "placeholders" in out
    assert not any(env.iterdir())
