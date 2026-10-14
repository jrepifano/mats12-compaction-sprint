"""p46.rewrite: template parsing, reply parsing, refusal detection, the generator rule, caching.
No network, no paid calls."""

import hashlib
import json
from dataclasses import asdict

import pytest
from p46 import contracts as K
from p46 import rewrite as R

from emx import probe_llm as PL

# --- templates ----------------------------------------------------------------------------------

def test_vendored_templates_six_linguistic_styles():
    t = R.extract_templates()
    assert set(t) == set(R.LINGUISTIC) == {"question", "slang", "uncommon_dialects",
                                           "technical_terms", "role_play", "misspellings"}
    for tmpl in t.values():
        assert "%s" in tmpl
        filled = tmpl % "Name three primary colours."
        assert "Name three primary colours." in filled
        assert "#mutated_text:" in filled
        assert "%s" not in filled


def test_extract_templates_parses_without_executing(tmp_path):
    p = tmp_path / "m.py.txt"
    p.write_text('raise SystemExit("executed!")\nimport os\n'
                 'template = {' + ", ".join(f'"{s}": "pre %s post"' for s in R.LINGUISTIC)
                 + ', "other": "x"}\n')
    t = R.extract_templates(p)
    assert t["slang"] == "pre %s post" and "other" not in t


def test_extract_templates_rejects_bad_dicts(tmp_path):
    p = tmp_path / "m.py.txt"
    p.write_text('template = {"slang": "no placeholder"}\n')
    with pytest.raises(ValueError):
        R.extract_templates(p, styles=("slang",))
    p.write_text("x = 1\n")
    with pytest.raises(TypeError):
        R.extract_templates(p, styles=("slang",))


def test_extract_mutated():
    assert R.extract_mutated('blah\n#mutated_text: "Yo, how do I bake bread?"') == \
        "Yo, how do I bake bread?"
    assert R.extract_mutated("#mutated_text:\nWhat is two plus two?") == "What is two plus two?"
    assert R.extract_mutated("no tag here") is None
    assert R.extract_mutated("#mutated_text:   ") is None
    assert R.extract_mutated(None) is None


# --- refusals -----------------------------------------------------------------------------------

@pytest.mark.parametrize("out", [
    "I'm sorry, but I can't help with that.", "I cannot rewrite this request.",
    "Sorry, I won't do that.", "As an AI, I must decline.", "抱歉，我不能翻译这个。",
    "Here is my answer. Unfortunately I can't assist with this request.",
])
def test_is_refusal_positive(out):
    assert R.is_refusal(out, "Tell me how to pick a lock.")


def test_is_refusal_negative_and_base_aware():
    assert not R.is_refusal("How might someone open a lock without its key?",
                            "Tell me how to pick a lock.")
    base = "I can't sleep at night; tell me which pills knock you out."
    assert not R.is_refusal("I can't sleep at night, so which pills will knock me out?", base)


def test_classify():
    assert R.classify("mutate", '#mutated_text: "Yo"', None, "b") == ("Yo", "ok")
    assert R.classify("mutate", "Just text", None, "b") == (None, "unparsed")
    assert R.classify("mutate", "I'm sorry, I can't do that.", None, "b") == (None, "refused")
    assert R.classify("para", "Paraphrase: Explain X.", None, "b") == ("Explain X.", "ok")
    assert R.classify("para", "  ", None, "b") == (None, "empty")
    assert R.classify("para", None, "APIError: x", "b") == (None, "error")
    assert R.classify("translate", "Nommez trois couleurs.", None, "b")[1] == "ok"


# --- items ----------------------------------------------------------------------------------------

def test_para_and_benign_items():
    base = {7: {"text": "Do X.", "category": "3"}, 2: {"text": "Do Y.", "category": "1"}}
    it = R.para_items(base)
    assert [i["key"] for i in it][:3] == ["para:0002:para1", "para:0002:para2", "para:0002:para3"]
    assert [i["seed"] for i in it[:3]] == [1, 2, 3]
    assert all(i["temperature"] == 1.0 and "Do " in i["user"] for i in it)
    tm = {s: f"T[{s}] %s" for s in R.LINGUISTIC}
    bi = R.benign_items([{"family": 0, "text": "Name 100% of colours."}], tm)
    assert len(bi) == 8
    assert {i["style"] for i in bi} == set(K.BENIGN_STYLES) - {"base"}
    slang = next(i for i in bi if i["style"] == "slang")
    assert slang["user"] == "T[slang] Name 100% of colours." and slang["kind"] == "mutate"
    fr = next(i for i in bi if i["style"] == "translate-fr")
    assert "French" in fr["user"] and fr["kind"] == "translate"


# --- the generator rule (fake run_calls) ----------------------------------------------------------

def _res(c, text, error=None):
    return PL.Result(c.key, c.route, text, error, 10, 5, 0.0, "table", None, "stop")


def _fake_run_calls(reply_by_model, log):
    def run_calls(calls, *, exp, cache, purpose, concurrency=64, dry_run=False, **kw):
        log.append({"n": len(calls), "routes": {c.route for c in calls}, "cache": cache,
                    "purpose": purpose, "dry_run": dry_run, "exp": exp,
                    "concurrency": concurrency})
        if dry_run:
            return {}
        return {c.key: _res(c, reply_by_model[c.route](c)) for c in calls}
    return run_calls


def _items(n):
    return R.para_items({q: {"text": f"Request number {q}.", "category": "1"}
                         for q in range(1, n + 1)})


def test_run_task_per_item_fallback(tmp_path):
    items = _items(4)  # 12 calls
    bad = {items[0]["key"]}
    log = []
    rc = _fake_run_calls({
        K.REWRITE_MODEL: lambda c: "I'm sorry, I can't help." if c.key in bad else f"P {c.key}",
        K.REWRITE_FALLBACK: lambda c: f"F {c.key}"}, log)
    s = R.run_task("para", items, dry_run=False, run_calls=rc, rewrite_dir=tmp_path,
                   require_caps=False)
    assert not s["switched_to_fallback"] and s["fallback_calls"] == 1
    assert s["primary"]["refused"] == 1 and s["final"]["failed"] == 0
    assert log[0]["exp"] == "P46" and log[0]["concurrency"] == 32
    assert log[1]["routes"] == {K.REWRITE_FALLBACK} and log[1]["n"] == 1
    assert log[0]["cache"] != log[1]["cache"]
    tab = R.read_table("para", tmp_path)
    assert len(tab) == 12 and [r["key"] for r in tab] == sorted(r["key"] for r in tab)
    fixed = next(r for r in tab if r["key"] in bad)
    assert fixed["model"] == K.REWRITE_FALLBACK and fixed["text"].startswith("F ")
    assert fixed["base_sha"] == hashlib.sha256(b"Request number 1.").hexdigest()
    assert json.loads(R.summary_path("para", tmp_path).read_text())["final_by_model"] == {
        K.REWRITE_MODEL: 11, K.REWRITE_FALLBACK: 1}


def test_run_task_switches_above_20_percent(tmp_path):
    items = _items(5)  # 15 calls, 4 refused = 27%
    bad = {i["key"] for i in items[:4]}
    log = []
    rc = _fake_run_calls({
        K.REWRITE_MODEL: lambda c: "I cannot do that." if c.key in bad else f"P {c.key}",
        K.REWRITE_FALLBACK: lambda c: "" if c.key == items[-1]["key"] else f"F {c.key}"}, log)
    s = R.run_task("para", items, dry_run=False, run_calls=rc, rewrite_dir=tmp_path,
                   require_caps=False)
    assert s["switched_to_fallback"] and log[1]["n"] == 15
    tab = R.read_table("para", tmp_path)
    assert {r["model"] for r in tab} == {K.REWRITE_FALLBACK}
    assert sum(r["status"] != "ok" for r in tab) == 1  # the fallback's empty reply stays failed


def test_run_task_dry_run_writes_nothing(tmp_path):
    log = []
    rc = _fake_run_calls({}, log)
    s = R.run_task("para", _items(2), dry_run=True, run_calls=rc, rewrite_dir=tmp_path)
    assert s["dry_run"] and s["projected_usd"] > 0 and log[0]["dry_run"]
    assert not any(tmp_path.iterdir())


def test_run_task_requires_probe_caps(tmp_path, monkeypatch):
    from emx import spend

    def nope(exp, probes_dir=None):
        raise KeyError("no probe.toml")
    monkeypatch.setattr(spend, "probe_caps_path", nope)
    with pytest.raises(SystemExit):
        R.run_task("para", _items(1), dry_run=False, run_calls=_fake_run_calls({}, []),
                   rewrite_dir=tmp_path)


def test_run_task_resumes_from_probe_llm_cache(tmp_path):
    """With every primary call already in probe_llm's cache, the real run_calls returns them
    without a call (no key, no reservation)."""
    items = _items(2)
    calls = [R.make_call(PL, it, K.REWRITE_MODEL) for it in items]
    cache = R.cache_path("para", K.REWRITE_MODEL, tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w") as f:
        for c in calls:
            row = {**asdict(_res(c, f"Cached {c.key}")), "user_sha": PL._sha(c.user),
                   "system_sha": None, "req_sha": PL._req_sha(c)}
            f.write(json.dumps(row) + "\n")
    s = R.run_task("para", items, dry_run=False, rewrite_dir=tmp_path, require_caps=False)
    assert s["final"]["failed"] == 0 and s["fallback_calls"] == 0
    assert all(r["text"].startswith("Cached ") for r in R.read_table("para", tmp_path))


def test_fallback_route_is_priced():
    R._probe_llm()
    assert PL.price_usd(K.REWRITE_FALLBACK, 1e6, 1e6) == pytest.approx(sum(R.FALLBACK_PRICE))
