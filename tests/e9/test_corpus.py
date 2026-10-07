"""Tests for emx.e9.corpus (unit A; prereg §4.2). Offline: synthetic shards, a fake downloader and
a whitespace tokenizer for the sampling logic; the real OLMo tokenizer for one integration test."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from emx.e9 import contracts as C
from emx.e9 import corpus as K
from emx.e9 import text as T

zstd = pytest.importorskip("zstandard")


class WsTok:
    """Whitespace tokenizer with the HF call interface `text.py` uses."""

    name_or_path = "ws"

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=False):
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        out = {"input_ids": list(range(len(spans)))}
        if return_offsets_mapping:
            out["offset_mapping"] = spans
        return out


def find_paths(prefix: str, candidate: bool, k: int, start: int = 0, ext: str = "zst",
               ) -> list[str]:
    out, i = [], start
    while len(out) < k:
        p = f"{prefix}/shard_{i:08d}.jsonl.{ext}"
        if K.is_candidate(p) == candidate:
            out.append(p)
        i += 1
    return out


class Lake:
    """Synthetic shards on disk plus a fake downloader."""

    def __init__(self, base: Path):
        self.base = base
        self.files: dict[str, str] = {}
        self.shards: list[K.Shard] = []
        self.fetched: list[str] = []

    def add(self, path: str, texts: list[str], size: int | None = None):
        f = self.base / path
        f.parent.mkdir(parents=True, exist_ok=True)
        raw = "".join(json.dumps({"id": f"id-{path}-{i}", "text": t}) + "\n"
                      for i, t in enumerate(texts)).encode()
        f.write_bytes(gzip.compress(raw) if path.endswith(".gz")
                      else zstd.ZstdCompressor().compress(raw))
        sh = K.Shard(path, size or f.stat().st_size, K.sha256_file(f))
        self.files[path] = str(f)
        self.shards.append(sh)
        return sh

    def fetch(self, shard: K.Shard) -> str:
        self.fetched.append(shard.path)
        return self.files[shard.path]


def words(tag: str, n: int) -> str:
    return " ".join([tag] + [f"w{i}" for i in range(n - 1)])


def cfg(**kw) -> K.Config:
    base = {"cap": 8, "cand_tokens": (5, 40), "bg_min_tokens": 3, "p1_n": 30, "p2_n": 31,
            "p2a_n": 25, "heldout_n": 10, "n_topics": None}
    return K.Config(**(base | kw))


def corpus(root, lake, c=None, **kw) -> K.Corpus:
    return K.Corpus(root=root, shards=list(lake.shards), tok=WsTok(), fetch=lake.fetch,
                    cfg=c or cfg(), workers=2, log=lambda m: None, **kw)


def build_candidates(lake: Lake, rng: np.random.Generator):
    """Topics alpha, beta (4 shards x 20 eligible docs) and fashion (1 shard, 6 eligible)."""
    for t in ("alpha", "beta"):
        zs = find_paths(f"data/common_crawl-{t}-0011", True, 3)
        gz = find_paths(f"data/common_crawl-{t}-0011", True, 1, start=1000, ext="gz")
        for j, p in enumerate([zs[0], zs[1], gz[0], zs[2]]):
            texts = [words(f"{t}{j}d{i}", int(rng.integers(5, 41))) for i in range(20)]
            texts += [words(f"{t}{j}s{i}", 2) for i in range(5)]  # ineligible, too short
            if j == 1:
                texts.append(texts[0].upper())  # a duplicate by content hash
            lake.add(p, texts, size=1000 * (j + 1))
    p = find_paths("data/common_crawl-fashion-0018", True, 1)[0]
    lake.add(p, [words(f"fa{i}", 10) for i in range(6)] + [words("long", 60)])


# --- split, order ---------------------------------------------------------------------------------


def test_split_determinism_and_fraction():
    paths = [f"data/x-{i // 100}/shard_{i:08d}.jsonl.zst" for i in range(20_000)]
    flags = [K.is_candidate(p) for p in paths]
    assert flags == [K.is_candidate(p) for p in paths]
    assert abs(np.mean(flags) - C.CANDIDATE_SHARD_FRACTION) < 0.01
    p = paths[7]
    h = int(hashlib.sha256(f"{C.CORPUS_SEED}:{p}".encode()).hexdigest(), 16)
    assert K.is_candidate(p) == (h / 2**256 < 0.10)


def test_real_shard_list_split():
    if not (K.REPO_ROOT / C.SHARD_LIST).exists():
        pytest.skip("shard list not present locally")
    shards = K.load_shard_list()
    sp = K.split_shards(shards)
    assert len(sp["candidate"]) == C.N_TOPICS
    # the frozen split gives fashion_and_beauty (17 shards in all) no candidate shard at all;
    # it stays a stratum and its whole quota is reallocated (§4.2 "Strata short of documents")
    assert sp["candidate"]["fashion_and_beauty"] == []
    n = len(sp["background"]) + len(sp["candidate_non_cc"]) + sum(
        len(v) for v in sp["candidate"].values())
    assert n == C.SHARD_LIST_ROWS
    assert all(not K.is_candidate(s.path) for s in sp["background"][:1000])


def test_topic_of():
    assert K.topic_of("data/common_crawl-fashion_and_beauty-0018/shard_1.jsonl.zst") == (
        "fashion_and_beauty")
    assert K.topic_of("data/olmocr_science_pdfs-health/shard_1.jsonl.zst") is None
    assert K.Shard("data/common_crawl-a_b-0011/x.jsonl.zst", 1, "").source_dir == (
        "common_crawl-a_b-0011")


def test_shard_order_byte_weighted():
    shards = [K.Shard("a", 9, ""), K.Shard("b", 1, "")]
    firsts = [K.shard_order(shards, s)[0].path for s in range(4000)]
    assert abs(firsts.count("a") / 4000 - 0.9) < 0.02
    many = [K.Shard(f"s{i}", i + 1, "") for i in range(50)]
    o1, o2 = K.shard_order(many, 7), K.shard_order(list(reversed(many)), 7)
    assert [s.path for s in o1] == [s.path for s in o2]  # input order does not matter
    assert sorted(s.path for s in o1) == sorted(s.path for s in many)


# --- one shard ------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["zst", "gz"])
def test_shard_sample_eligibility_and_cap(tmp_path, fmt):
    lake = Lake(tmp_path)
    lens = [4, 5, 40, 41, 20, 0, 12]
    texts = [words(f"d{i}", n) if n else "" for i, n in enumerate(lens)]
    texts.append("[REMOVED] " + words("r", 10))
    sh = lake.add(f"data/common_crawl-t-0011/shard_00000001.jsonl.{fmt}", texts)
    full = K.shard_sample(sh, lake.files[sh.path], kind="candidate", seed=1, tok=WsTok(),
                          cap=100, bounds=(5, 40))
    assert sorted(d["line"] for d in full["docs"]) == [1, 2, 4, 6]  # bounds inclusive
    st = full["stats"]
    assert st["n_lines"] == 8 and st["tested"] == 8 and st["removed"] == 1
    assert st["short"] == 1 and st["long"] == 1 and st["no_text"] == 1
    two = K.shard_sample(sh, lake.files[sh.path], kind="candidate", seed=1, tok=WsTok(),
                         cap=2, bounds=(5, 40))
    assert [d["line"] for d in two["docs"]] == [d["line"] for d in full["docs"][:2]]
    d = full["docs"][0]
    assert set(C.DOC_KEYS) <= set(d)
    assert d["doc_id"] == f"{sh.path}#{d['line']}" and d["dolma_id"] == f"id-{sh.path}-{d['line']}"
    assert d["topic"] == "t" and d["content_sha256"] == T.content_sha256(texts[d["line"]])
    other = K.shard_sample(sh, lake.files[sh.path], kind="candidate", seed=2, tok=WsTok(),
                           cap=100, bounds=(5, 40))
    assert sorted(x["line"] for x in other["docs"]) == [1, 2, 4, 6]


# --- pools ------------------------------------------------------------------------------------------


def test_pools(tmp_path):
    lake = Lake(tmp_path / "lake")
    build_candidates(lake, np.random.default_rng(0))
    man = K.sample_pools(corpus(tmp_path / "out", lake))
    pdir = tmp_path / "out" / C.POOLS_DIR
    pools = {n: K.read_jsonl(pdir / f"{n}.jsonl") for n in ("P1", "P2a", "P2b")}
    assert [len(pools[n]) for n in ("P1", "P2a", "P2b")] == [30, 25, 6]
    alld = [d for v in pools.values() for d in v]
    assert len({d["doc_id"] for d in alld}) == 61
    assert len({d["content_sha256"] for d in alld}) == 61  # disjoint, deduped
    per_shard = {}
    for d in alld:
        per_shard[d["shard"]] = per_shard.get(d["shard"], 0) + 1
        assert 5 <= d["n_tokens_orig"] <= 40
    assert max(per_shard.values()) <= 8
    draw = man["P1"]["draw"]
    fa = [r for r in draw["shortfall_reallocation"] if r["topic"] == "fashion"]
    assert fa and fa[0]["available"] == 6
    # 6 available: P1 gets 6*10//20 = 3, P2 3; deficits 7 and 7 spread over alpha and beta
    assert draw["quotas_final"]["P1"] == {"alpha": 14, "beta": 13, "fashion": 3}
    assert draw["quotas_final"]["P2"] == {"alpha": 15, "beta": 13, "fashion": 3}
    assert man["P1"]["counts_per_topic"] == draw["quotas_final"]["P1"]
    p2 = Counter_topics(pools["P2a"] + pools["P2b"])
    assert p2 == draw["quotas_final"]["P2"]
    assert sum(v.get("dup_within", 0) for v in draw["dedup"].values()) >= 0
    for n in ("P1", "P2a", "P2b"):
        assert man[n]["sha256"] == K.sha256_file(pdir / f"{n}.jsonl")
        assert sum(s["n"] for s in man[n]["shards"]) == man[n]["n"]
    assert K.verify(tmp_path / "out") == []
    man2 = K.sample_pools(corpus(tmp_path / "out2", lake))  # deterministic
    assert {n: m["sha256"] for n, m in man.items()} == {n: m["sha256"] for n, m in man2.items()}


def Counter_topics(docs):
    out = {}
    for d in docs:
        out[d["topic"]] = out.get(d["topic"], 0) + 1
    return dict(sorted(out.items()))


def test_topic_without_candidate_shards(tmp_path):
    lake = Lake(tmp_path / "lake")
    build_candidates(lake, np.random.default_rng(0))
    lake.add(find_paths("data/common_crawl-gamma-0011", False, 1)[0], [words("g", 10)] * 3)
    c = corpus(tmp_path / "out", lake, cfg(p1_n=32, p2_n=31, p2a_n=25, n_topics=4))
    assert c.split["candidate"]["gamma"] == []
    man = K.sample_pools(c)
    q = man["P1"]["draw"]["quotas_final"]
    assert q["P1"]["gamma"] == q["P2"]["gamma"] == 0 and man["P1"]["n"] == 32
    assert man["P1"]["counts_per_topic"].get("gamma") is None


def test_dedup_within_sample(tmp_path):
    lake = Lake(tmp_path / "lake")
    p = find_paths("data/common_crawl-alpha-0011", True, 2)
    base = [words(f"x{i}", 10) for i in range(10)]
    lake.add(p[0], base)
    lake.add(p[1], [t.upper() + "  " for t in base] + [words(f"y{i}", 10) for i in range(10)])
    c = corpus(tmp_path / "out", lake, cfg(cap=20, p1_n=8, p2_n=7, p2a_n=5))
    K.sample_pools(c)
    pdir = tmp_path / "out" / C.POOLS_DIR
    docs = [d for n in ("P1", "P2a", "P2b") for d in K.read_jsonl(pdir / f"{n}.jsonl")]
    assert len(docs) == 15 and len({d["content_sha256"] for d in docs}) == 15


# --- held-out text and backgrounds ------------------------------------------------------------


def build_world(tmp_path):
    """Pools drawn, then background shards seeded with copies of pool texts."""
    lake = Lake(tmp_path / "lake")
    rng = np.random.default_rng(1)
    build_candidates(lake, rng)
    root = tmp_path / "out"
    K.sample_pools(corpus(root, lake))
    pool_texts = [d["text"] for n in ("P1", "P2a", "P2b")
                  for d in K.read_jsonl(root / C.POOLS_DIR / f"{n}.jsonl")]
    zs = find_paths("data/olmocr_science_pdfs-health", False, 3)
    gz = find_paths("data/olmocr_science_pdfs-health", False, 3, start=1000, ext="gz")
    for j, p in enumerate([zs[0], gz[0], zs[1], gz[1], zs[2], gz[2]]):
        texts = [words(f"bg{j}d{i}", int(rng.integers(2, 25))) for i in range(30)]
        texts += pool_texts[j * 10:(j + 1) * 10]
        lake.add(p, texts, size=500 + 100 * j)
    return lake, root


SMALL = {"core": {"min_tokens": 100}, "pad1": {"min_tokens": 50}, "pad2": {"min_tokens": 50},
         "filler": {"min_docs": 10, "min_doc_tokens": 8}}


def test_heldout_and_background_exclusions(tmp_path):
    lake, root = build_world(tmp_path)
    c = corpus(root, lake, cfg(cap=40))
    K.sample_pools(corpus(root, lake, cfg(cap=8)))  # unchanged by the new background shards
    hman = K.sample_heldout(c)
    pool_h, _ = K.pool_hashes(c, K.POOL_NAMES)
    held = K.read_jsonl(root / C.POOLS_DIR / "heldout_text.jsonl")
    assert len(held) == 10 and not ({d["content_sha256"] for d in held} & pool_h)
    assert all(d["topic"] is None and d["n_tokens_orig"] >= 3 for d in held)
    big = dict(SMALL, filler={"min_docs": 60, "min_doc_tokens": 8})
    bman = K.sample_background(c, 1, reserves=big)
    bg = K.read_jsonl(root / C.BACKGROUND_DIR / "s1.jsonl")
    hs = {d["content_sha256"] for d in bg}
    assert not (hs & pool_h) and not (hs & {d["content_sha256"] for d in held})
    assert bman["dedup"].get("excluded", 0) > 0 and hman["draw"]["dedup"].get("excluded", 0) >= 0
    assert len(hs) == len(bg)
    assert K.verify(root) == []


def roles_ok(recs, reserves):
    roles = [r for r in K.ROLE_ORDER if r in reserves]
    seq = [d["role"] for d in sorted(recs, key=lambda d: d["order"])]
    assert [d["order"] for d in recs] == list(range(len(recs)))
    blocks = [r for i, r in enumerate(seq) if i == 0 or seq[i - 1] != r]
    assert blocks == roles  # contiguous, in the fixed order
    for r in roles:
        ds = [d for d in recs if d["role"] == r]
        spec = reserves[r]
        assert K._role_done(spec, ds) and not K._role_done(spec, ds[:-1])  # minimal crossing


def test_background_roles_both_families(tmp_path):
    assert list(K.reserves_for(1)) == ["core", "pad1", "pad2", "filler"]
    assert K.reserves_for(3) == {r: C.BACKGROUND_RESERVES[r] for r in K.reserves_for(3)}
    assert list(K.reserves_for(5)) == ["core", "filler"] == list(K.reserves_for(6))
    assert K.reserves_for(6)["core"] == C.BACKGROUND_RESERVES_CONFIRM["core"]
    lake, root = build_world(tmp_path)
    c = corpus(root, lake, cfg(cap=40))
    K.sample_heldout(c)
    m1 = K.sample_background(c, 1, reserves=SMALL)
    roles_ok(K.read_jsonl(root / C.BACKGROUND_DIR / "s1.jsonl"), SMALL)
    assert m1["background_seed"] == 1001 and m1["role_order"] == [
        "core",
        "pad1", "pad2", "filler"]
    conf = {"core": {"min_tokens": 150}, "filler": {"min_tokens": 80}}
    m5 = K.sample_background(c, 5, reserves=conf)
    r5 = K.read_jsonl(root / C.BACKGROUND_DIR / "s5.jsonl")
    roles_ok(r5, conf)
    assert m5["roles"]["filler"]["progress"] >= 80
    r1 = K.read_jsonl(root / C.BACKGROUND_DIR / "s1.jsonl")
    assert [d["doc_id"] for d in r1[:20]] != [d["doc_id"] for d in r5[:20]]  # seeds differ


def test_extend_matches_longer_draw(tmp_path):
    lake, root = build_world(tmp_path)
    c = corpus(root, lake, cfg(cap=40))
    K.sample_heldout(c)
    root2 = tmp_path / "out2"
    shutil.copytree(root / C.POOLS_DIR, root2 / C.POOLS_DIR)
    c2 = corpus(root2, lake, cfg(cap=40))

    K.sample_background(c, 2, reserves=SMALL)
    m = K.extend(c, 2, "filler", extra_docs=7)
    longer = dict(SMALL, filler={"min_docs": 17, "min_doc_tokens": 8})
    m2 = K.sample_background(c2, 2, reserves=longer)
    a = K.read_jsonl(root / C.BACKGROUND_DIR / "s2.jsonl")
    b = K.read_jsonl(root2 / C.BACKGROUND_DIR / "s2.jsonl")
    assert a == b and m["sha256"] == m2["sha256"]
    assert m["extensions"][0]["added"] > 0 and m["cursor"] == m2["cursor"]

    # an earlier role continues the same sequence: docs after the current end, role core
    n0 = len(a)
    m = K.extend(c, 2, "core", extra_tokens=40)
    ext = K.read_jsonl(root / C.BACKGROUND_DIR / "s2.jsonl")
    assert ext[:n0] == a and all(d["role"] == "core" for d in ext[n0:])
    added_core = sum(d["n_tokens"] for d in ext if d["role"] == "core")
    assert added_core >= 140 and m["reserves"]["core"]["min_tokens"] == 140
    longest = dict(longer, filler={"min_docs": 40, "min_doc_tokens": 8})
    K.sample_background(c2, 2, reserves=longest)
    seq = K.read_jsonl(root2 / C.BACKGROUND_DIR / "s2.jsonl")
    assert [d["doc_id"] for d in ext] == [d["doc_id"] for d in seq[: len(ext)]]
    assert len({d["content_sha256"] for d in ext}) == len(ext)
    with pytest.raises(ValueError):
        K.extend(c, 2, "core", extra_docs=3)
    assert K.verify(root) == []
    (root / C.BACKGROUND_DIR / "s2.jsonl").write_text("tampered\n")
    assert K.verify(root)


def test_shard_hash_mismatch_fails(tmp_path):
    lake = Lake(tmp_path / "lake")
    build_candidates(lake, np.random.default_rng(0))
    lake.shards[0] = K.Shard(lake.shards[0].path, lake.shards[0].size, "0" * 64)
    with pytest.raises(ValueError, match="sha256"):
        K.sample_pools(corpus(tmp_path / "out", lake))


def test_cache_roundtrip(tmp_path):
    lake = Lake(tmp_path / "lake")
    build_candidates(lake, np.random.default_rng(0))
    m1 = K.sample_pools(corpus(tmp_path / "o1", lake, cache_dir=tmp_path / "cache"))
    n = len(lake.fetched)
    m2 = K.sample_pools(corpus(tmp_path / "o2", lake, cache_dir=tmp_path / "cache"))
    assert len(lake.fetched) == n  # served from the cache
    assert m1["P1"]["sha256"] == m2["P1"]["sha256"]


# --- the real tokenizer ---------------------------------------------------------------------------


def test_olmo_eligibility_and_truncation(tmp_path, olmo_tok):
    sent = "The committee reviewed the proposal in detail and wrote a short note. "
    texts = [
        sent * 5,  # < 300 tokens
        sent * 100,  # 300-3000, > 1000: truncated
        sent * 30,  # 300-1000: unchanged
        "[REMOVED] " + sent * 30,
        sent * 300,  # > 3000
    ]
    lake = Lake(tmp_path)
    sh = lake.add("data/common_crawl-t-0011/shard_00000001.jsonl.zst", texts)
    res = K.shard_sample(sh, lake.files[sh.path], kind="candidate", seed=3, tok=olmo_tok,
                         cap=10, bounds=C.CANDIDATE_TOKENS)
    by = {d["line"]: d for d in res["docs"]}
    assert sorted(by) == [1, 2]
    d1, d2 = by[1], by[2]
    assert d1["n_tokens_orig"] == T.n_tokens(texts[1], olmo_tok) > 1000
    assert (d1["text"], d1["n_tokens"]) == T.truncate(texts[1], olmo_tok)
    assert 700 <= d1["n_tokens"] <= 1000 and texts[1].startswith(d1["text"])
    assert d2["text"] == texts[2] and d2["n_tokens"] == d2["n_tokens_orig"]
    assert d1["content_sha256"] == T.content_sha256(texts[1])
    # through the threaded corpus path (per-thread tokenizer copies)
    c = K.Corpus(root=tmp_path / "o", shards=[sh], tok=olmo_tok, fetch=lake.fetch,
                 cfg=K.Config(n_topics=None), workers=2, log=lambda m: None)
    got = c.shard_future(sh, "candidate", 3).result()
    c.close()
    assert got == res
