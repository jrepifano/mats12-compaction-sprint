"""Fast, offline tests for p42.data (pyarrow / xxhash tests skip when those aren't installed:
run with `uv run --with pyarrow --with xxhash pytest probes/P42-ood-coverage-gaps/tests`)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from p42 import contracts as K
from p42 import data as D

# --- first-turn extraction -----------------------------------------------------------------------

def test_first_user_content_skips_system():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "a"}, {"role": "user", "content": "second"}]
    assert D.first_user_content(msgs) == "hi"
    assert D.first_user_content([{"role": "assistant", "content": "x"}]) is None
    assert D.first_user_content([]) is None


def test_first_turn_if_user():
    assert D.first_turn_if_user([{"role": "user", "content": "q"}]) == "q"
    assert D.first_turn_if_user([{"role": "assistant", "content": "q"}]) is None
    assert D.first_turn_if_user([]) is None


def test_dolci_category_and_lawse_prompt():
    assert D.dolci_dpo_category("correct-python-sft-187k-request-70-606") == \
        "correct-python-sft-187k-request-70-606"
    assert D.dolci_dpo_category("filtered_wc_sample_123") == "filtered_wc_sample"
    assert D.dolci_dpo_category("a" * 60 + "_7") == "a" * 40
    assert D.lawse_prompt("Title?", "<p>Body &amp; more</p>\n") == "Title?\n\nBody & more"
    assert D.nonempty("x") and not D.nonempty("  \n") and not D.nonempty(None)


def _write_parquet(path, rows):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    pq.write_table(pa.Table.from_pylist(rows), path)
    return str(path)


def test_extract_dolci_dpo_and_sft(tmp_path):
    pytest.importorskip("pyarrow")
    dpo = _write_parquet(tmp_path / "dpo.parquet", [
        {"prompt_id": "src_a_1", "chosen": [{"role": "system", "content": "s"},
                                            {"role": "user", "content": "Q1"},
                                            {"role": "assistant", "content": "A"}]},
        {"prompt_id": "src_b_2", "chosen": [{"role": "user", "content": "   "}]},
        {"prompt_id": "src_c_3", "chosen": [{"role": "user", "content": "Q3"}]},
    ])
    cols, counts = D.extract("dolci_dpo", [dpo])
    assert cols["text"] == ["Q1", "Q3"]
    assert cols["category"] == ["src_a", "src_c"]
    assert cols["source_id"] == ["src_a_1", "src_c_3"]
    assert counts == {"raw": 3, "with_user_turn": 3, "nonempty": 2}
    sft = _write_parquet(tmp_path / "sft.parquet", [
        {"id": "x1", "source_dataset": "aya", "messages": [{"role": "user", "content": "hola"}]},
        {"id": "x2", "source_dataset": "tool", "messages": [{"role": "assistant", "content": "a"}]},
    ])
    cols, counts = D.extract("dolci_sft", [sft])
    assert cols["text"] == ["hola"] and cols["category"] == ["aya"]
    assert counts["with_user_turn"] == 1


def test_extract_lmsys_and_wildchat(tmp_path):
    pytest.importorskip("pyarrow")
    mod = {"flagged": True, "category_scores": {"harassment": 0.9, "violence": 0.1}}
    unf = {"flagged": False, "category_scores": {"harassment": 0.01, "violence": 0.0}}
    rows = [
        {"conversation_id": "c1", "language": "English",
         "conversation": [{"role": "user", "content": "first"}, {"role": "assistant", "content": "r"}],
         "openai_moderation": [mod, unf]},
        {"conversation_id": "c2", "language": "German",
         "conversation": [{"role": "assistant", "content": "x"}], "openai_moderation": [unf]},
        {"conversation_id": "c3", "language": "German",
         "conversation": [{"role": "user", "content": "hallo"}], "openai_moderation": [unf]},
    ]
    cols, counts = D.extract("lmsys", [_write_parquet(tmp_path / "l.parquet", rows)])
    assert cols["text"] == ["first", "hallo"]
    assert cols["lang"] == ["English", "German"]
    assert cols["mod_flagged"] == [True, False]
    assert cols["mod_max"] == [0.9, 0.01]
    assert counts == {"raw": 3, "with_user_turn": 2, "nonempty": 2}
    for r in rows:
        r["conversation_hash"] = r.pop("conversation_id")
    cols, _ = D.extract("wildchat48", [_write_parquet(tmp_path / "w.parquet", rows)])
    assert cols["source_id"] == ["c1", "c3"]


def test_extract_chatdoctor_and_lawse(tmp_path):
    pytest.importorskip("pyarrow")
    cd = _write_parquet(tmp_path / "cd.parquet", [
        {"instruction": "If you are a doctor", "input": "I have a headache", "output": "o"},
        {"instruction": "i", "input": "", "output": "o"}])
    cols, counts = D.extract("chatdoctor", [cd])
    assert cols["text"] == ["I have a headache"] and cols["category"] == ["medical"]
    assert cols["source_id"] == ["0"] and counts["raw"] == 2
    lj = tmp_path / "law.json"
    lj.write_text(json.dumps([{"question_id": 5, "question_title": "Can I?",
                               "question_body": "<p>Sue <b>them</b></p>", "answers": []}]))
    cols, _ = D.extract("lawse", [str(lj)])
    assert cols["text"] == ["Can I?\n\nSue them"] and cols["source_id"] == ["5"]


# --- normalisation, exact dedup -----------------------------------------------------------------

def test_normalise_and_exact_hash():
    assert D.normalise("  Hello\n\tWORLD  ") == "hello world"
    assert D.exact_hash("Hello  world") == D.exact_hash("hello world\n")
    assert D.exact_hash("hello world") != D.exact_hash("hello worlds")
    h = np.array([3, 1, 3, 2, 1], dtype=np.uint64)
    assert D.first_occurrence_mask(h).tolist() == [True, True, False, True, False]


# --- MinHash ------------------------------------------------------------------------------------

WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega"]


def _text(rng, n=60):
    return " ".join(rng.choice(WORDS + [f"w{i}" for i in range(500)], size=n).tolist())


def test_minhash_finds_one_word_change_and_rejects_unrelated():
    pytest.importorskip("xxhash")
    rng = np.random.default_rng(0)
    base = _text(rng)
    words = base.split()
    words[30] = "CHANGED"
    near = " ".join(words)
    others = [_text(rng) for _ in range(200)]
    idx_sig = D.minhash_signatures(others + [base])
    q_sig = D.minhash_signatures([near, _text(rng), _text(rng)])
    found, _ = D.near_dup_of_index(q_sig, idx_sig)
    assert found.tolist() == [True, False, False]
    # one word in 60 changes 5 of 56 shingles: true Jaccard 51/61 ~ 0.84
    assert D.est_jaccard(q_sig[0], idx_sig[-1]) >= K.MINHASH_JACCARD


def test_minhash_short_texts_and_determinism():
    pytest.importorskip("xxhash")
    s1 = D.minhash_signatures(["hi there", "Hi  THERE", "bye"])
    assert (s1[0] == s1[1]).all() and not (s1[0] == s1[2]).all()
    assert (D.minhash_signatures(["hi there"]) == s1[:1]).all()
    assert s1.dtype == np.uint32 and s1.shape == (3, K.MINHASH_PERM)


def test_minhash_blocking_matches_single():
    pytest.importorskip("xxhash")
    rng = np.random.default_rng(1)
    texts = [_text(rng, n=int(rng.integers(1, 80))) for _ in range(50)]
    a = D.minhash_signatures(texts, max_block=37)
    b = np.concatenate([D.minhash_signatures([t]) for t in texts])
    assert (a == b).all()


def test_banding_recall_sanity():
    assert D.candidate_prob(0.8) > 0.9999
    assert D.candidate_prob(0.3) < 0.25
    # identical signatures always collide in every band
    sig = np.random.default_rng(2).integers(0, 2**31 - 1, size=(5, K.MINHASH_PERM)).astype(np.uint32)
    k = D.band_keys(sig)
    assert (D.band_keys(sig.copy()) == k).all() and k.shape == (5, D.N_BANDS)
    found, _ = D.near_dup_of_index(sig[:2], sig[2:])
    assert not found.any()
    found, _ = D.near_dup_of_index(sig[:2], sig)
    assert found.all()


def test_internal_near_dups():
    pytest.importorskip("xxhash")
    rng = np.random.default_rng(3)
    a = _text(rng)
    w = a.split()
    w[10] = "X"
    sig = D.minhash_signatures([a, " ".join(w), _text(rng), _text(rng)])
    assert D.internal_near_dups(sig) == 2


# --- length matching, moderation, language --------------------------------------------------------

def test_length_matched_sample_hits_each_decile():
    rng = np.random.default_rng(4)
    pool = rng.lognormal(3, 1, size=5000).astype(int) + 1
    cand = rng.lognormal(3.5, 1, size=3000).astype(int) + 1
    ch, rec = D.length_matched_sample(cand, pool, 500, np.random.default_rng(5))
    assert len(ch) == 500 and len(set(ch.tolist())) == 500
    assert sum(rec["targets"]) == 500
    edges = np.array(rec["edges"])
    got = np.bincount(D.length_bin(cand[ch], edges), minlength=K.N_LEN_BINS)
    short = rec["shortfall_by_bin"]
    for b in range(K.N_LEN_BINS):
        if b not in short:
            assert got[b] >= rec["targets"][b]


def test_length_matched_sample_shortfall_from_nearest():
    pool = np.arange(1, 1001)
    cand = np.arange(500, 1001)  # nothing in the lower half
    ch, rec = D.length_matched_sample(cand, pool, 100, np.random.default_rng(0))
    assert len(ch) == 100 and set(rec["shortfall_by_bin"]) == {0, 1, 2, 3, 4}


def test_bin_targets_proportional():
    t = D.bin_targets(np.array([0] * 30 + [1] * 10 + [2] * 60), 500, 3)
    assert t.tolist() == [150, 50, 300]


def test_mod_bin():
    assert D.mod_bin(False, 0.99) == K.MOD_BIN_UNFLAGGED
    assert D.mod_bin(True, 0.5) == K.MOD_BIN_LOW
    assert D.mod_bin(True, K.MOD_HIGH) == K.MOD_BIN_HIGH
    assert D.mod_bin(True, None) == K.MOD_BIN_HIGH
    assert D.mod_bin(None, None) is None
    assert D._moderation({"flagged": False, "category_scores": {"a": 0.2, "b": 0.7}}) == (False, 0.7)
    assert D._moderation(None) == (None, None)


def test_detect_english():
    assert D.detect_english("What is the capital of France? Please tell me.") == "English"
    assert D.detect_english("Wie spät ist es heute in Berlin, bitte?") == "unknown"
    assert D.detect_english("你好，今天天气怎么样") == "unknown"
    assert D.detect_english("12345") == "unknown"


# --- rows ---------------------------------------------------------------------------------------

def _rows():
    rows = []
    sizes = {K.ID_REF: 40, K.ID_HELDOUT: 10, K.SFT_REF: 6, K.POOL: 20, K.PLANTED_MED: 4,
             K.PLANTED_LAW: 4}
    for s, n in sizes.items():
        pool = s == K.POOL
        sub = {"source_id": [str(i) for i in range(n)], "category": [None] * n,
               "text": ["the cat is on the mat"] * n, "lang": ["English"] * n,
               "mod_flagged": [i % 3 == 0 for i in range(n)], "mod_max": [0.9] * n}
        src = {K.POOL: "lmsys", K.PLANTED_MED: "chatdoctor", K.PLANTED_LAW: "lawse",
               K.SFT_REF: "dolci_sft"}.get(s, "dolci_dpo")
        rows += D.make_rows(s, src, sub, [5] * n, [False] * n, sub["text"], pool=pool)
    return rows


def test_make_rows_fields_gen_split_and_sort():
    rows = _rows()
    D.assign_gen_split(rows, n_gen_ref=15)
    rows.sort(key=lambda r: r["pid"])
    D.check_rows(rows)
    by = lambda s: [r for r in rows if r["set"] == s]
    ref = by(K.ID_REF)
    assert sorted(r["gen_rank"] for r in ref) == list(range(40))
    assert sum(r["gen"] for r in ref) == 15
    assert all((r["gen_rank"] < 15) == r["gen"] for r in ref)
    for s in K.SCORED_SETS:
        rs = by(s)
        assert all(r["gen"] and r["gen_rank"] is None for r in rs)
        dev = sum(r["split"] == "dev" for r in rs)
        assert dev == len(rs) // 2 and all(r["split"] in ("dev", "test") for r in rs)
    for r in by(K.SFT_REF):
        assert not r["gen"] and r["split"] is None
    pool = by(K.POOL)
    assert all(r["mod_bin"] in (0, 2) for r in pool)
    assert all(r["mod_bin"] is None and r["mod_flagged"] is None for r in by(K.ID_REF))
    assert pool[0]["pid"] == "pool-000000"
    # deterministic
    rows2 = _rows()
    D.assign_gen_split(rows2, n_gen_ref=15)
    rows2.sort(key=lambda r: r["pid"])
    assert rows == rows2


def test_tokenize_and_truncate_with_fake_tokenizer():
    class Tok:
        def __call__(self, texts, add_special_tokens=False):
            return {"input_ids": [list(range(len(t.split()))) for t in texts]}

        def decode(self, ids):
            return " ".join(f"t{i}" for i in ids)

    n, tr, tx = D.tokenize_and_truncate(Tok(), ["a b c", "a b c d e"], max_tokens=4, batch=1)
    assert n == [3, 5] and tr == [False, True] and tx == ["a b c", "t0 t1 t2 t3"]


def test_source_files_selection():
    files = ["README.md", "data/train-00000-of-00004.parquet", "data/train-00001-of-00004.parquet"]
    assert D.source_files("dolci_dpo", files) == files[1:]
    wc = [f"data/train-{i:05d}-of-00086.parquet" for i in range(86)]
    assert len(D.source_files("wildchat48", wc)) == len(D.WILDCHAT_SHARDS)
    assert D.source_files("lawse", ["a.json", "README.md"]) == ["a.json"]


class _WordTok:
    """Fake tokenizer: one token per word, decode keeps the words."""

    def __call__(self, texts, add_special_tokens=False):
        return {"input_ids": [t.split() for t in texts]}

    def decode(self, ids):
        return " ".join(ids)


def test_take_unique_drops_same_truncated_input_and_resumes():
    pa = pytest.importorskip("pyarrow")
    texts = ["a b c x", "a b c y", "d e", "f g", "D  E", "h i"]
    n = len(texts)
    table = pa.table({"source_id": [str(i) for i in range(n)], "category": [None] * n,
                      "text": texts, "lang": [None] * n, "mod_flagged": [None] * n,
                      "mod_max": [None] * n}, schema=D._arrow_schema())
    order = np.arange(n)
    seen: set = set()
    # rows 0 and 1 share their truncated input "a b c"
    sub, nt, tr, tx, idx, pos, drop = D.take_unique(table, order, 0, 2, _FakeTrunc3(), seen)
    assert idx.tolist() == [0, 2] and drop == 1 and pos == 3
    assert tx == ["a b c", "d e"] and tr == [True, False]
    assert nt == [4 + K.MAX_PROMPT_TOKENS, 2]
    assert sub["source_id"] == ["0", "2"]
    # the next caller resumes at pos, sharing `seen` ("D  E" normalises to "d e": dropped)
    _, _, _, tx2, idx2, pos2, drop2 = D.take_unique(table, order, pos, 5, _FakeTrunc3(), seen)
    assert idx2.tolist() == [3, 5] and drop2 == 1 and pos2 == n and tx2 == ["f g", "h i"]


class _FakeTrunc3(_WordTok):
    """take_unique calls tokenize_and_truncate with the default MAX; emulate MAX=3 by
    returning ids that decode to the first 3 words once the default cap is applied."""

    def __call__(self, texts, add_special_tokens=False):
        ids = [t.split() for t in texts]
        # pad long inputs so they exceed MAX_PROMPT_TOKENS; decode keeps only the first 3 words
        return {"input_ids": [w if len(w) <= 3 else w + ["<pad>"] * K.MAX_PROMPT_TOKENS
                              for w in ids]}

    def decode(self, ids):
        return " ".join(ids[:3])
