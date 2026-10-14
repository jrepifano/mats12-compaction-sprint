"""p46.data on synthetic fixtures: SORRY-Bench loading and family alignment, rows and pids,
rewrite tables, refs, tokenisation, gen counts, dedup and flag_family, human_check, paths,
embeddings bookkeeping. No network, no paid calls."""

import hashlib
import json

import numpy as np
import pytest
from p46 import contracts as K
from p46 import data as D
from p46 import rewrite as R

STYLES = ("base",) + K.CORE_STYLES[1:] + K.LANG_STYLES


class FakeTok:
    """Whitespace 'tokenizer' with P42's interface (tok(texts, add_special_tokens=False))."""

    def __call__(self, texts, add_special_tokens=False):
        return {"input_ids": [t.split() for t in texts]}

    def decode(self, ids):
        return " ".join(ids)


def write_sorry(d, qids=(3, 1, 2), mutate=None):
    d.mkdir(parents=True, exist_ok=True)
    for st in STYLES:
        rows = []
        for q in qids:
            r = {"question_id": q, "category": str(q % 2 + 1),
                 "turns": [f"[{st}] request {q}" if st != "base" else f"base request {q}"],
                 "prompt_style": st}
            if mutate:
                r = mutate(st, r)
            if r is not None:
                rows.append(r)
        (d / D.SORRY_FILES[st]).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return d


# --- SORRY-Bench loading ----------------------------------------------------------------------

def test_load_sorry_aligned(tmp_path):
    s = D.load_sorry(write_sorry(tmp_path / "s"), n_base=3)
    assert set(s) == set(STYLES)
    assert sorted(s["base"]) == [1, 2, 3]
    assert s["slang"][2]["text"] == "[slang] request 2"
    assert s["translate-fr"][1]["category"] == s["base"][1]["category"]


def test_load_sorry_wrong_count(tmp_path):
    with pytest.raises(ValueError, match="expected 440"):
        D.load_sorry(write_sorry(tmp_path / "s"))


@pytest.mark.parametrize("mutate,msg", [
    (lambda st, r: None if (st == "slang" and r["question_id"] == 2) else r, "question_ids"),
    (lambda st, r: {**r, "extra": 1}, "unexpected"),
    (lambda st, r: {**r, "prompt_style": "x"} if st == "role_play" else r, "prompt_style"),
    (lambda st, r: {**r, "turns": ["a", "b"]}, "one turn"),
    (lambda st, r: {**r, "turns": [None]} if st == "base" else r, "non-empty"),
    (lambda st, r: {**r, "turns": ["  "]} if st == "slang" else r, "non-empty"),
    (lambda st, r: {**r, "category": "9"} if st == "misspellings" else r, "category"),
])
def test_load_sorry_fails_loudly(tmp_path, mutate, msg):
    with pytest.raises(ValueError, match=msg):
        D.load_sorry(write_sorry(tmp_path / "s", mutate=mutate), n_base=3)


def test_load_sorry_prompt_style_optional(tmp_path):
    def drop(st, r):
        r = dict(r)
        r.pop("prompt_style")
        return r
    s = D.load_sorry(write_sorry(tmp_path / "s", mutate=drop), n_base=3)
    assert s["base"][1]["prompt_style"] is None


def test_load_sorry_null_mutation_is_skipped(tmp_path):
    def null(st, r):
        return {**r, "turns": [None]} if (st == "question" and r["question_id"] == 2) else r
    s = D.load_sorry(write_sorry(tmp_path / "s", mutate=null), n_base=3)
    assert D.sorry_missing(s) == {"question": [2]}
    core = D.sorry_rows(s, K.CORE, K.CORE_STYLES)
    assert len(core) == 3 * 7 - 1 and "core-0002-question" not in {r["pid"] for r in core}


# --- rows -----------------------------------------------------------------------------------------

def test_sorry_rows_pids_sets_base_text(tmp_path):
    s = D.load_sorry(write_sorry(tmp_path / "s"), n_base=3)
    core = D.sorry_rows(s, K.CORE, K.CORE_STYLES)
    lang = D.sorry_rows(s, K.LANG, K.LANG_STYLES)
    assert len(core) == 3 * 7 and len(lang) == 3 * 2
    r = next(x for x in core if x["pid"] == "core-0002-technical_terms")
    assert r["family"] == 2 and r["style"] == "technical_terms" and r["source"] == "sorry"
    assert r["base_text"] == "base request 2" and r["text"] == "[technical_terms] request 2"
    assert r["lang"] == "en" and r["english"] is True
    z = next(x for x in lang if x["pid"] == "lang-0001-translate-zh-cn")
    assert z["lang"] == "zh" and z["english"] is False and z["base_text"] == "base request 1"
    assert {x["lang"] for x in lang} == {"zh", "fr"}
    assert all(tuple(x) == K.PROMPT_FIELDS for x in core + lang)


def _table(task, bases, styles, status=lambda f, s: "ok", model=K.REWRITE_MODEL):
    rows = []
    for f, b in bases.items():
        for st in styles:
            ok = status(f, st)
            rows.append({"key": f"{task}:{f:04d}:{st}", "task": task, "family": f, "style": st,
                         "model": model, "status": ok, "text": f"rw {f} {st}" if ok == "ok"
                         else None, "base_sha": R.sha(b["text"]), "seed": 1})
    return rows


def test_rewrite_rows_para(tmp_path):
    s = D.load_sorry(write_sorry(tmp_path / "s"), n_base=3)
    tab = _table("para", s["base"], K.PARA_STYLES,
                 status=lambda f, st: "refused" if (f, st) == (3, "para2") else "ok")
    rows, st = D.rewrite_rows(tab, K.PARA, K.PARA_STYLES, s["base"], "ours")
    assert len(rows) == 8 and st["missing"] == ["3:para2"] and st["failed"] == {"refused": 1}
    r = next(x for x in rows if x["pid"] == "para-0001-para3")
    assert r["source"] == "ours" and r["base_text"] == "base request 1" and r["text"] == \
        "rw 1 para3" and r["category"] == s["base"][1]["category"]


def test_rewrite_rows_refuses_stale_base(tmp_path):
    s = D.load_sorry(write_sorry(tmp_path / "s"), n_base=3)
    tab = _table("para", s["base"], K.PARA_STYLES)
    tab[0]["base_sha"] = "0" * 64
    with pytest.raises(ValueError, match="base text changed"):
        D.rewrite_rows(tab, K.PARA, K.PARA_STYLES, s["base"], "ours")


def test_benign_rows():
    instr = [{"family": 0, "alpaca_index": 17, "text": "Name three colours of the rainbow."},
             {"family": 1, "alpaca_index": 99, "text": "Describe a sunny day in two sentences."}]
    bases = {d["family"]: {"text": d["text"]} for d in instr}
    tab = _table("benign", bases, [s for s in K.BENIGN_STYLES if s != "base"])
    rows, st = D.benign_rows(instr, tab)
    assert len(rows) == 2 * len(K.BENIGN_STYLES) and not st["missing"]
    b = next(x for x in rows if x["pid"] == "benign-0001-base")
    assert b["text"] == b["base_text"] == instr[1]["text"] and b["source_id"] == "99:base"
    fr = next(x for x in rows if x["pid"] == "benign-0000-translate-fr")
    assert fr["lang"] == "fr" and fr["source"] == "alpaca" and fr["base_text"] == instr[0]["text"]
    rows2, st2 = D.benign_rows(instr, None)  # no table yet: base rows only, rest missing
    assert len(rows2) == 2 and len(st2["missing"]) == 16


def test_p42_ref_rows():
    p42 = [{"pid": "id_ref-000001", "set": "id_ref", "source": "dolci_dpo", "source_id": "a",
            "category": "c", "text": "t1", "n_tokens": 3, "truncated": False, "lang": "English",
            "english": True},
           {"pid": "pool-000002", "set": "pool", "source": "lmsys", "source_id": "b",
            "category": None, "text": "t2", "n_tokens": 4, "truncated": True, "lang": "German",
            "english": False},
           {"pid": "id_heldout-000003", "set": "id_heldout", "source": "dolci_dpo",
            "source_id": "c", "category": "c", "text": "t3", "n_tokens": 5, "truncated": False,
            "lang": "unknown", "english": False},
           {"pid": "sft_ref-000004", "set": "sft_ref", "source": "dolci_sft", "source_id": "d",
            "category": "x", "text": "t4", "n_tokens": 1, "truncated": False, "lang": "English",
            "english": True}]
    rows = D.p42_ref_rows(p42)
    assert [(r["pid"], r["set"]) for r in rows] == [
        ("id_ref-000001", K.REF_DPO), ("pool-000002", K.REF_LMSYS), ("id_heldout-000003",
                                                                    K.HELDOUT)]
    assert rows[1]["truncated"] is True and rows[1]["lang"] == "German"
    assert all(r["family"] is None and r["base_text"] is None for r in rows)
    p42[1]["source"] = "wildchat48"
    with pytest.raises(ValueError, match="not lmsys"):
        D.p42_ref_rows(p42)


def test_ref_safety_rows_and_tokenize_truncation():
    long = " ".join(["w"] * (K.MAX_PROMPT_TOKENS + 5))
    first = {"source_id": ["wildjailbreak_1", "coconot_2"], "category": ["wildjailbreak", "c"],
             "text": ["How do I do the thing and the other?", long]}
    rows = D.ref_safety_rows(first, FakeTok())
    assert [r["pid"] for r in rows] == ["ref_safety-000000", "ref_safety-000001"]
    assert rows[0]["lang"] == "English" and rows[0]["english"] and not rows[0]["truncated"]
    assert rows[1]["truncated"] and rows[1]["n_tokens"] == K.MAX_PROMPT_TOKENS + 5
    assert len(rows[1]["text"].split()) == K.MAX_PROMPT_TOKENS
    r = D.make_row(pid="core-0001-base", set=K.CORE, text="a b c", source="sorry")
    D.tokenize_rows([r], FakeTok())
    assert r["n_tokens"] == 3 and r["truncated"] is False


def test_set_gen_counts():
    rows = [D.make_row(pid=f"{s}-x", set=s) for s in K.SETS]
    D.set_gen(rows)
    got = {r["set"]: (r["gen_dpo"], r["gen_sft"]) for r in rows}
    assert got[K.CORE] == got[K.LANG] == got[K.PARA] == (4, 2)
    assert got[K.BENIGN] == (1, 0)
    assert all(got[s] == (0, 0) for s in (K.REF_DPO, K.REF_SAFETY, K.REF_LMSYS, K.HELDOUT))


def _full(rows):
    for r in rows:
        r.update(n_tokens=r["n_tokens"] or 1, truncated=bool(r["truncated"]))
    D.set_gen(rows)
    rows.sort(key=lambda r: r["pid"])
    return rows


def test_check_rows_sorting_and_duplicates(tmp_path):
    s = D.load_sorry(write_sorry(tmp_path / "s"), n_base=3)
    rows = _full(D.sorry_rows(s, K.CORE, K.CORE_STYLES))
    D.check_rows(rows)
    with pytest.raises(ValueError, match="sorted"):
        D.check_rows(rows[::-1])
    with pytest.raises(ValueError, match="duplicate"):
        D.check_rows(sorted(rows + rows[:1], key=lambda r: r["pid"]))


# --- dedup ----------------------------------------------------------------------------------------

def test_apply_dedup_flag_family(tmp_path):
    s = D.load_sorry(write_sorry(tmp_path / "s"), n_base=3)
    tab = _table("para", s["base"], K.PARA_STYLES)
    rows = (D.sorry_rows(s, K.CORE, K.CORE_STYLES) + D.sorry_rows(s, K.LANG, K.LANG_STYLES)
            + D.rewrite_rows(tab, K.PARA, K.PARA_STYLES, s["base"], "ours")[0])
    instr = [{"family": 0, "alpaca_index": 5, "text": "Name three colours of the rainbow."}]
    rows += D.benign_rows(instr, None)[0]
    rows += [D.make_row(pid="ref_safety-000000", set=K.REF_SAFETY, text="x")]
    lex = {r["pid"]: False for r in rows if r["set"] in D.DEDUP_SETS}
    lex["core-0001-base"] = True        # family 1: base matched lexically
    lex["core-0002-slang"] = True       # a non-base match does not flag family 2
    lex["benign-0000-base"] = True
    sem = {r["pid"]: 0.5 for r in rows if r["set"] in K.PROBE_SETS}
    sem["core-0003-base"] = 0.95        # family 3: base matched semantically
    sem["para-0002-para1"] = 0.99       # non-base: no flag
    st = D.apply_dedup(rows, lex, sem)
    flag = {r["pid"]: r["flag_family"] for r in rows}
    for fam, want in ((1, True), (2, False), (3, True)):
        fr = [r for r in rows if r["set"] in K.PROBE_SETS and r["family"] == fam]
        assert len(fr) == 12 and {r["flag_family"] for r in fr} == {want}
    assert flag["benign-0000-base"] is True
    ref = next(r for r in rows if r["set"] == K.REF_SAFETY)
    assert ref["dedup_lex"] is None and ref["dedup_sem"] is None and ref["flag_family"] is None
    assert next(r for r in rows if r["pid"] == "benign-0000-base")["dedup_sem"] is None
    assert next(r for r in rows if r["pid"] == "core-0002-slang")["dedup_lex"] is True
    assert st["families_flagged"] == {K.CORE: 2, K.BENIGN: 1}


def test_lexical_dedup_exact_and_near():
    pytest.importorskip("xxhash")
    words = "please write a short and friendly note to my neighbour about the loud party " \
            "music late last night and ask them kindly to keep it down next time thanks"
    idx = [words, "an unrelated prompt about cooking pasta with garlic and olive oil tonight"]
    hset = np.unique(D.D42.exact_hashes(idx))
    sig = D.D42.minhash_signatures(idx)
    q = [words.upper(), words.replace("thanks", "thank you"), "what is the capital of France"]
    exact, near, _ = D.lexical_dedup(q, hset, sig)
    assert exact.tolist() == [True, False, False]
    assert near.tolist()[1] is True and near.tolist()[2] is False


def test_max_cosine():
    q = np.eye(3, dtype=np.float32)
    ref = np.array([[1, 0, 0], [0, 0.6, 0.8]], dtype=np.float32)
    assert np.allclose(D.max_cosine(q, ref, chunk=2), [1.0, 0.6, 0.8])


# --- human check ----------------------------------------------------------------------------------

def _hrec(q, resp, score, **kw):
    return {"question_id": q, "answer_id": f"a{q}{resp}", "model_id": "m", "tstamp": 0.0,
            "choices": [{"index": 0, "turns": [resp]}], "human_score": score, "prompt_style": "base",
            **kw}


def test_human_check_rows_excludes_train_and_stratifies():
    base = {q: f"base {q}" for q in range(1, 11)}
    train = [_hrec(1, "r1-0", 0), _hrec(2, "r2-1", 1)]
    test = [_hrec(q, f"r{q}-{k}", float(k % 2)) for q in range(1, 11) for k in range(6)]
    test.append(_hrec(1, "r1-2", 0.0))  # a repeat within test
    test.append({**_hrec(3, "enc", 1.0), "prompt_style": "caesar"})  # not an English core style
    rows, st = D.human_check_rows(test, train, base, n_pod=10, n_luna=20)
    assert st["test_in_train"] == 2 and st["test_repeats"] == 1 and st["eligible"] == 58
    assert st["test_other_style"] == 1 and all(r["prompt_style"] in K.CORE_STYLES for r in rows)
    assert len(rows) == 30 and [r["hid"] for r in rows] == sorted(r["hid"] for r in rows)
    assert st["drawn"] == {"pod": {0: 5, 1: 5}, "luna": {0: 10, 1: 10}}
    keys = {(r["question_id"], r["response"]) for r in rows}
    assert len(keys) == 30 and not keys & {(1, "r1-0"), (2, "r2-1")}
    assert all(tuple(r) == K.HUMAN_FIELDS and r["base_text"] == base[r["question_id"]]
               for r in rows)
    rows2, _ = D.human_check_rows(test, train, base, n_pod=10, n_luna=20)
    assert rows2 == rows  # seeded


def test_human_check_rows_fails_on_unexpected_fields():
    base = {1: "b"}
    with pytest.raises(ValueError, match="unexpected"):
        D.human_check_rows([_hrec(1, "x", 1, surprise=True)], [], base, 1, 0)
    with pytest.raises(ValueError, match="not 0/1"):
        D.human_check_rows([_hrec(1, "x", 0.5)], [], base, 1, 0)


# --- Alpaca, paths --------------------------------------------------------------------------------

def test_alpaca_candidates():
    ins = ["Name three colours of the rainbow in order please.",            # ok
           "Write a Python function that adds two numbers together quickly.",  # code
           "Summarise the following paragraph in one short sentence for me.",  # has input
           "Too short.",                                                     # < 8 words
           "Explain why the sky looks blue during the day to a child."]       # ok
    inp = ["", "", "Some paragraph.", "", ""]
    assert D.alpaca_candidates(ins, inp) == [0, 4]


def test_main_checkout_root_and_resolve_shared(tmp_path, monkeypatch):
    monkeypatch.delenv("P46_MAIN_ROOT", raising=False)
    main = tmp_path / "repo"
    wt = main / ".claude" / "worktrees" / "p46-x"
    assert D.main_checkout_root(wt) == main
    assert D.main_checkout_root(main) == main
    (main / "data" / "P42").mkdir(parents=True)
    (main / "data" / "P42" / "prompts.jsonl").write_text("{}\n")
    wt.mkdir(parents=True)
    got = D.resolve_shared(wt / "data" / "P42" / "prompts.jsonl", repo_root=wt)
    assert got == main / "data" / "P42" / "prompts.jsonl"
    (wt / "data" / "P42").mkdir(parents=True)
    (wt / "data" / "P42" / "prompts.jsonl").write_text("{}\n")
    assert D.resolve_shared(wt / "data" / "P42" / "prompts.jsonl", repo_root=wt).is_relative_to(wt)
    with pytest.raises(FileNotFoundError):
        D.resolve_shared(wt / "data" / "nope", repo_root=wt)
    monkeypatch.setenv("P46_MAIN_ROOT", str(tmp_path / "elsewhere"))
    assert D.main_checkout_root(wt) == tmp_path / "elsewhere"


def test_load_p42_prompts_checks_sha(tmp_path):
    p = tmp_path / "prompts.jsonl"
    p.write_text('{"pid": "x"}\n')
    with pytest.raises(ValueError, match="sha256"):
        D.load_p42_prompts(p)


# --- embeddings bookkeeping (no API) --------------------------------------------------------------

def _unit(seed):
    v = np.random.default_rng(seed).normal(size=K.EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def test_embed_needed_reuses_p42_vectors_and_writes_outputs(tmp_path, capsys):
    E42 = D._e42()
    rows = [D.make_row(pid="core-0001-base", set=K.CORE, text="hello there"),
            D.make_row(pid="ref_safety-000000", set=K.REF_SAFETY, text="bad thing")]
    wjb = {"source_id": ["sft-a"], "text": ["jailbreak text"]}
    p42v = {E42.text_sha(E42.embed_input(t)): _unit(i)
            for i, t in enumerate(["hello there", "bad thing", "jailbreak text"])}
    assert D.embed_needed(rows, wjb, dry_run=True, emb_dir=tmp_path, p42_vectors=p42v,
                          enc=None) == 0
    assert "0 pending" in capsys.readouterr().out and not any(tmp_path.iterdir())
    assert D.embed_needed(rows, wjb, dry_run=False, emb_dir=tmp_path, p42_vectors=p42v,
                          enc=None) == 0
    vec, w = D.load_emb_for(rows, tmp_path)
    assert np.allclose(vec["core-0001-base"], _unit(0), atol=1e-3) and w.shape == (1, K.EMBED_DIM)
    assert (tmp_path / D.EMB_SFT_WJB_KEYS.name).read_text().split("\t")[2].strip() == "sft-a"
    rows[0]["text"] = "changed"
    with pytest.raises(ValueError, match="no current embedding"):
        D.load_emb_for(rows, tmp_path)


def test_embed_needed_dry_run_projects_pending(tmp_path, capsys):
    rows = [D.make_row(pid=f"core-000{i}-base", set=K.CORE, text="x " * 300) for i in range(3)]
    wjb = {"source_id": [], "text": []}
    D.embed_needed(rows, wjb, dry_run=True, emb_dir=tmp_path, p42_vectors={}, enc=None)
    out = capsys.readouterr().out
    assert "3 pending" in out and "est. tokens" in out
    assert not any(tmp_path.iterdir())


def test_embed_needed_paid_path_with_fake_client(tmp_path, monkeypatch):
    """The paid path with the API and ledger faked: reservation -> calls -> record -> settle."""
    from emx import spend

    calls = []
    for name in ("recover_uncertain", "release_api", "mark_submitted"):
        monkeypatch.setattr(spend, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(spend, "probe_caps_path", lambda exp: "probe.toml")
    monkeypatch.setattr(spend, "find_open", lambda *a, **k: None)
    monkeypatch.setattr(spend, "reserve_api", lambda exp, usd, note: calls.append(
        ("reserve", exp, note)) or "rid")
    monkeypatch.setattr(spend, "record", lambda *a, **k: calls.append(("record", k["exp"])))
    monkeypatch.setattr(spend, "settle_api", lambda rid, **k: calls.append(("settle", k["exp"])))

    class Emb:
        def __init__(self, i):
            self.embedding, self.index = _unit(i).tolist(), i

    class Resp:
        def __init__(self, n):
            self.data = [Emb(i) for i in range(n)]
            self.usage = type("U", (), {"prompt_tokens": 7 * n})()

    class Client:
        class embeddings:
            @staticmethod
            async def create(model, input):
                return Resp(len(input))

    rows = [D.make_row(pid="core-0001-base", set=K.CORE, text="hello"),
            D.make_row(pid="ref_safety-000000", set=K.REF_SAFETY, text="bad")]
    wjb = {"source_id": ["s1"], "text": ["jb"]}
    assert D.embed_needed(rows, wjb, dry_run=False, client=Client(), emb_dir=tmp_path,
                          p42_vectors={}, enc=None) == 0
    assert ("reserve", "P46", D.EMBED_NOTE) in calls and ("record", "P46") in calls
    assert ("settle", "P46") in calls
    assert np.load(tmp_path / D.EMB_PROMPTS.name).shape == (2, K.EMBED_DIM)


def test_sha_helpers_consistent():
    assert R.sha("abc") == hashlib.sha256(b"abc").hexdigest()
