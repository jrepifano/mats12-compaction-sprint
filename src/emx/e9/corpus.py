"""E9 corpus sampling (unit A): shard split, candidate pools, held-out text and backgrounds.

Implements prereg §4.2 ("The shard list", "Shard split", "Sampling rules", the pools table,
"Truncation", "Deduplication") and the seeds of §4.14, with the reserve targets and the
deterministic extension of the implementation plan (unit A, "Frozen in U0").

- **Shard split.** A shard is a candidate iff
  `int(sha256(f"{CORPUS_SEED}:{path}"), 16) / 2**256 < CANDIDATE_SHARD_FRACTION`. Candidate pools
  draw only Common Crawl candidate shards (stratum = topic); held-out text and backgrounds draw only
  background shards (stratum = the whole mix).
- **Shard order.** Within a stratum, shards are ordered byte-weighted without replacement by an
  exponential race (key = Exp(1) / size, ascending), from one seeded numpy Generator. Taking the
  first k is successive byte-weighted sampling without replacement, and a longer draw continues
  the same order.
- **Within a shard.** Every record index is put in a seeded uniform random order
  (`Generator.permutation(n_lines)`); records are tested for eligibility in that order until
  `MAX_DOCS_PER_SHARD` eligible ones are found. That list (fixed per shard and seed) is the shard's
  contribution, taken as a prefix when a quota ends mid-shard. Eligibility is on the original
  length (OLMo tokens): candidates 300-3,000 inclusive, background >= 50, no `[REMOVED]`. Texts
  shorter in UTF-8 bytes than the lower bound are rejected without tokenizing (a byte-level BPE
  token covers at least one byte, so this never changes the outcome).
- **Dedup.** Documents are taken in stream order; one whose content hash was already taken, or is
  in the exclusion set, is dropped and the draw continues (next position, then next shard).
- **Pools.** P1 and P2 are drawn together, one stream per topic, so they are disjoint by document
  and content hash; each topic's draw is split between P1 and P2 by a seeded permutation, and P2
  is split at random into P2a and P2b. A topic whose candidate shards run out spreads its
  shortfall (per pool) equally over the other topics, repeated until no topic is short.
- **Backgrounds.** One document sequence per CPT seed s (seed `background_seed(s)`), excluding
  P1, P2 and held-out hashes, assigned to roles in the fixed order core, pad1, pad2, filler
  (seeds 1-4, `BACKGROUND_RESERVES`) or core, filler (seeds 5-6, `BACKGROUND_RESERVES_CONFIRM`).
  `extend()` raises one role's target and continues the same sequence from its recorded cursor.
- **Held-out general text (Amendment G).** 1,000 documents from background shards, byte-weighted
  over all directories, at most `HELDOUT_MAX_DOCS_PER_SHARD` (10) per shard and at least 100
  shards, excluding P1, P2a, P2b and every background file (seeds 1-6). It replaces a first draw
  (all from 3 shards), archived as `heldout_text.v1.*` and used nowhere; the backgrounds were
  drawn excluding that first draw and are kept.

Truncation, token counts and content hashes come from `emx.e9.text` only.
"""

from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from emx.e9 import contracts as C
from emx.e9 import text as T

REPO_ROOT = Path(__file__).resolve().parents[3]
CODE_VERSION = "corpus-v1"  # bump if the per-shard sampling rule changes (invalidates the cache)
ROLE_ORDER: tuple[str, ...] = ("core", "pad1", "pad2", "filler")
POOL_NAMES: tuple[str, ...] = ("P1", "P2a", "P2b")
HELDOUT_NAME = "heldout_text"
_CC_RE = re.compile(r"^data/" + re.escape(C.CC_DIR_PREFIX) + r"(.+)-(\d{4})/")


# --- shard list and split -----------------------------------------------------------------------


@dataclass(frozen=True)
class Shard:
    path: str
    size: int
    lfs_sha256: str

    @property
    def source_dir(self) -> str:
        return self.path.split("/")[1]

    @property
    def topic(self) -> str | None:
        return topic_of(self.path)


def topic_of(path: str) -> str | None:
    """The Common Crawl topic of `data/common_crawl-<topic>-<NNNN>/...`, else None."""
    m = _CC_RE.match(path)
    return m.group(1) if m else None


def sha256_file(path: str | Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def load_shard_list(
    path: str | Path = C.SHARD_LIST,
    expect_sha256: str | None = C.SHARD_LIST_SHA256,
    expect_rows: int | None = C.SHARD_LIST_ROWS,
) -> list[Shard]:
    """The frozen shard list (§4.2), verified by sha256 and row count."""
    path = Path(path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if expect_sha256 is not None and (got := sha256_file(path)) != expect_sha256:
        raise ValueError(f"shard list sha256 {got} != {expect_sha256}")
    with open(path, newline="") as f:
        rows = [Shard(r["path"], int(r["size"]), r["lfs_sha256"])
                for r in csv.DictReader(f, delimiter="\t")]
    if expect_rows is not None and len(rows) != expect_rows:
        raise ValueError(f"shard list has {len(rows)} rows, expected {expect_rows}")
    return rows


def is_candidate(path: str, seed: int = C.CORPUS_SEED,
                 fraction: float = C.CANDIDATE_SHARD_FRACTION) -> bool:
    h = int(hashlib.sha256(f"{seed}:{path}".encode()).hexdigest(), 16)
    return h / 2**256 < fraction


def split_shards(shards: Iterable[Shard]) -> dict:
    """{"candidate": {topic: [Shard]} (CC candidate shards only; every CC topic is a key),
    "background": [Shard],
    "candidate_non_cc": [Shard]} (the last are candidate shards no pool uses)."""
    shards = list(shards)
    # every Common Crawl topic is a stratum, even one with no candidate shard (it is then short)
    cand: dict[str, list[Shard]] = {t: [] for t in {s.topic for s in shards} - {None}}
    bg: list[Shard] = []
    cand_other: list[Shard] = []
    for s in shards:
        if is_candidate(s.path):
            if s.topic is None:
                cand_other.append(s)
            else:
                cand[s.topic].append(s)
        else:
            bg.append(s)
    return {"candidate": dict(sorted(cand.items())), "background": bg,
            "candidate_non_cc": cand_other}


def derive_seed(*parts) -> int:
    """A 64-bit seed from its parts, e.g. (CORPUS_SEED, "pools", topic)."""
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:16], 16)


def shard_order(shards: Iterable[Shard], seed: int) -> list[Shard]:
    """All shards in byte-weighted order without replacement (exponential race)."""
    ss = sorted(shards, key=lambda s: s.path)
    if not ss:
        return []
    rng = np.random.default_rng(seed)
    keys = rng.exponential(size=len(ss)) / np.array([max(s.size, 1) for s in ss], dtype=float)
    return [ss[i] for i in np.argsort(keys, kind="stable")]


# --- configuration and context ------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    cap: int = C.MAX_DOCS_PER_SHARD
    cand_tokens: tuple[int, int] = C.CANDIDATE_TOKENS
    bg_min_tokens: int = C.BACKGROUND_MIN_TOKENS
    p1_n: int = C.P1_N
    p2_n: int = C.P2_N
    p2a_n: int = C.P2A_N
    heldout_n: int = C.HELDOUT_TEXT_N
    heldout_cap: int = C.HELDOUT_MAX_DOCS_PER_SHARD  # Amendment G
    heldout_min_shards: int = 100  # Amendment G
    n_topics: int | None = C.N_TOPICS  # asserted when not None
    seed: int = C.CORPUS_SEED

    def bounds(self, kind: str) -> tuple[int, int | None]:
        if kind == "candidate":
            return self.cand_tokens
        if kind == "background":
            return (self.bg_min_tokens, None)
        raise ValueError(kind)


def hf_fetch(shard: Shard) -> str:
    """Download one shard at the pinned revision (public dataset) into the default HF cache."""
    from huggingface_hub import hf_hub_download

    return hf_hub_download(C.DOLMA_REPO, shard.path, repo_type="dataset",
                           revision=C.DOLMA_REVISION)


@dataclass
class Corpus:
    """Everything the samplers need: output root, shard list, tokenizer, downloader."""

    root: Path
    shards: list[Shard]
    tok: object
    fetch: Callable[[Shard], str] = hf_fetch
    cfg: Config = field(default_factory=Config)
    workers: int = 4
    cache_dir: Path | None = None
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True)

    def __post_init__(self):
        self.root = Path(self.root)
        self._pool: ThreadPoolExecutor | None = None
        self._futures: dict[tuple, Future] = {}
        self._local = threading.local()
        self._lock = threading.Lock()
        self.split = split_shards(self.shards)
        if self.cfg.n_topics is not None and len(self.split["candidate"]) != self.cfg.n_topics:
            raise ValueError(f"{len(self.split['candidate'])} topics, expected {self.cfg.n_topics}")

    # tokenizer copies per thread: HF fast tokenizers are not safe to share across threads
    def thread_tok(self):
        t = getattr(self._local, "tok", None)
        if t is None:
            t = self._local.tok = copy.deepcopy(self.tok)
        return t

    def executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.workers)
        return self._pool

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        self._futures.clear()

    def shard_future(self, shard: Shard, kind: str, seed: int, cap: int | None = None) -> Future:
        cap = cap or self.cfg.cap
        key = (shard.path, kind, seed, cap)
        with self._lock:
            fut = self._futures.get(key)
            if fut is None:
                fut = self.executor().submit(self._shard_sample_cached, shard, kind, seed, cap)
                self._futures[key] = fut
        return fut

    def forget(self, shard: Shard, kind: str, seed: int, cap: int | None = None):
        with self._lock:
            self._futures.pop((shard.path, kind, seed, cap or self.cfg.cap), None)

    def _shard_sample_cached(self, shard: Shard, kind: str, seed: int, cap: int) -> dict:
        lo, hi = self.cfg.bounds(kind)
        tok_id = getattr(self.tok, "name_or_path", type(self.tok).__name__)
        ck = derive_seed(CODE_VERSION, shard.path, shard.lfs_sha256, kind, lo, hi, seed,
                         cap, tok_id, C.REVISION)
        cpath = self.cache_dir / f"{ck:016x}.json.gz" if self.cache_dir else None
        if cpath is not None and cpath.exists():
            with gzip.open(cpath, "rt", encoding="utf-8") as f:
                return json.load(f)
        local = self.fetch(shard)
        got = sha256_file(local)
        if got != shard.lfs_sha256:
            raise ValueError(f"{shard.path}: sha256 {got} != lfs {shard.lfs_sha256}")
        res = shard_sample(shard, local, kind=kind, seed=seed, tok=self.thread_tok(),
                           cap=cap, bounds=(lo, hi))
        if cpath is not None:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = cpath.with_suffix(f".tmp{os.getpid()}.{threading.get_ident()}")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            os.replace(tmp, cpath)
        self.log(f"shard {shard.path} {kind}: lines {res['stats']['n_lines']} tested "
                 f"{res['stats']['tested']} kept {len(res['docs'])}")
        return res


# --- reading one shard --------------------------------------------------------------------------


def _open_lines(path: str | Path):
    """A binary reader over the decompressed records (.jsonl.zst or .jsonl.gz)."""
    p = str(path)
    if p.endswith(".gz"):
        return gzip.open(p, "rb")
    if p.endswith(".zst"):
        import zstandard

        fh = open(p, "rb")  # noqa: SIM115 - closed with the reader
        r = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True,
                                                       closefd=True)
        return io.BufferedReader(r, buffer_size=1 << 20)
    return open(p, "rb")  # plain .jsonl (tests)


def count_lines(path: str | Path) -> int:
    n, last = 0, b"\n"
    with _open_lines(path) as r:
        while chunk := r.read(1 << 24):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (last != b"\n")


def read_lines(path: str | Path, wanted: set[int]) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    if not wanted:
        return out
    top = max(wanted)
    with _open_lines(path) as r:
        for i, line in enumerate(r):
            if i in wanted:
                out[i] = line
            if i >= top:
                break
    return out


def check_eligible(text, tok, bounds: tuple[int, int | None]) -> tuple[str, int | None]:
    """("ok" | reason, original token count or None)."""
    lo, hi = bounds
    if not isinstance(text, str) or not text.strip():
        return "no_text", None
    if C.REMOVED_MARK in text:
        return "removed", None
    if len(text.encode("utf-8")) < lo:  # tokens <= UTF-8 bytes for a byte-level BPE
        return "short", None
    n = T.n_tokens(text, tok)
    if n < lo:
        return "short", n
    if hi is not None and n > hi:
        return "long", n
    return "ok", n


def make_doc(shard: Shard, line: int, rec: dict, n_orig: int, tok) -> dict:
    text = rec["text"]
    if n_orig <= C.TRUNC_MAX_TOKENS:
        cut, n = text, n_orig
    else:
        cut, n = T.truncate(text, tok)
    return {
        "doc_id": f"{shard.path}#{line}",
        "dolma_id": rec.get("id"),
        "shard": shard.path,
        "line": line,
        "source_dir": shard.source_dir,
        "topic": shard.topic,
        "text": cut,
        "n_tokens": n,
        "n_tokens_orig": n_orig,
        "content_sha256": T.content_sha256(text),
    }


def shard_sample(shard: Shard, local_path: str | Path, *, kind: str, seed: int, tok, cap: int,
                 bounds: tuple[int, int | None]) -> dict:
    """Up to `cap` eligible records of one shard, in a seeded uniform random order.

    The result depends only on (shard content, seed, bounds, cap); the batch sizes used to read
    lines do not change it. Returns {"docs": [...], "stats": {...}}.
    """
    n = count_lines(local_path)
    perm = np.random.default_rng(derive_seed(seed, kind, shard.path)).permutation(n)
    docs: list[dict] = []
    stats: Counter = Counter(n_lines=n)
    start, batch = 0, max(3 * cap, 64)
    while len(docs) < cap and start < n:
        idxs = [int(i) for i in perm[start:start + batch]]
        start += batch
        batch *= 4
        lines = read_lines(local_path, set(idxs))
        for i in idxs:
            stats["tested"] += 1
            try:
                rec = json.loads(lines[i])
            except (ValueError, KeyError):
                stats["bad_json"] += 1
                continue
            reason, n_orig = check_eligible(rec.get("text"), tok, bounds)
            if reason != "ok":
                stats[reason] += 1
                continue
            stats["eligible"] += 1
            docs.append(make_doc(shard, i, rec, n_orig, tok))
            if len(docs) >= cap:
                break
    stats["exhausted"] = int(len(docs) < cap)
    return {"docs": docs, "stats": dict(stats)}


# --- the document stream ------------------------------------------------------------------------


class DocStream:
    """Documents of one stratum in draw order, with dedup against `seen` and `exclude`.

    `seen` may be shared between streams (P1 and P2 across topics). Taken documents' hashes are
    added to it. The cursor (shard index, position in its list) makes the stream resumable.
    """

    def __init__(self, corpus: Corpus, order: list[Shard], *, kind: str, seed: int,
                 seen: set[str], exclude: frozenset[str] | set[str] = frozenset(),
                 cursor: tuple[int, int] = (0, 0), lookahead: int | None = None,
                 cap: int | None = None):
        self.c, self.order, self.kind, self.seed = corpus, order, kind, seed
        self.cap = cap or corpus.cfg.cap
        self.seen, self.exclude = seen, exclude
        self.shard_i, self.pos = cursor
        self.lookahead = lookahead if lookahead is not None else max(2 * corpus.workers, 2)
        self.counts: Counter = Counter()
        self.shard_stats: dict[str, dict] = {}  # path -> per-shard stats, for shards opened
        self._docs: list[dict] | None = None

    @property
    def cursor(self) -> tuple[int, int]:
        return (self.shard_i, self.pos)

    def prefetch(self, k: int):
        for j in range(self.shard_i, min(self.shard_i + k, len(self.order))):
            self.c.shard_future(self.order[j], self.kind, self.seed, self.cap)

    def _load(self):
        self.prefetch(self.lookahead)
        sh = self.order[self.shard_i]
        res = self.c.shard_future(sh, self.kind, self.seed, self.cap).result()
        self.shard_stats[sh.path] = res["stats"]
        self._docs = res["docs"]

    def next(self) -> dict | None:
        while self.shard_i < len(self.order):
            if self._docs is None:
                self._load()
            if self.pos >= len(self._docs):
                self.c.forget(self.order[self.shard_i], self.kind, self.seed, self.cap)
                self.shard_i, self.pos, self._docs = self.shard_i + 1, 0, None
                continue
            d = self._docs[self.pos]
            self.pos += 1
            h = d["content_sha256"]
            if h in self.exclude:
                self.counts["excluded"] += 1
                continue
            if h in self.seen:
                self.counts["dup_within"] += 1
                continue
            self.seen.add(h)
            self.counts["taken"] += 1
            return d
        return None

    def opened(self) -> list[Shard]:
        return [s for s in self.order[: self.shard_i + (self._docs is not None)]
                if s.path in self.shard_stats]

    def eligibility(self) -> dict:
        tot: Counter = Counter()
        for st in self.shard_stats.values():
            tot.update({k: v for k, v in st.items() if k != "exhausted"})
        return dict(tot)


# --- outputs ------------------------------------------------------------------------------------


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_commit() -> dict:
    def git(*a):
        r = subprocess.run(["git", "-C", str(REPO_ROOT), *a], capture_output=True, text=True,
                           check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    mine = ["src/emx/e9/corpus.py", "scripts/e9_corpus.py", "src/emx/e9/text.py",
            "src/emx/e9/contracts.py"]
    dirty = git("status", "--porcelain", "--", *mine)
    return {"commit": git("rev-parse", "HEAD"), "corpus_code_dirty": bool(dirty),
            "corpus_py_sha256": sha256_file(__file__)}


def write_jsonl(path: Path, recs: list[dict], keys: tuple[str, ...]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            missing = [k for k in keys if k not in r]
            if missing:
                raise ValueError(f"record {r.get('doc_id')} lacks {missing}")
            f.write(json.dumps({k: r[k] for k in keys} | {k: v for k, v in r.items()
                                                           if k not in keys},
                               ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return sha256_file(path)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_manifest(path: Path, man: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(man, indent=1, sort_keys=False) + "\n")
    os.replace(tmp, path)


def _shards_block(docs: list[dict], by_path: dict[str, Shard]) -> list[dict]:
    lines: dict[str, list[int]] = defaultdict(list)
    for d in docs:
        lines[d["shard"]].append(d["line"])
    return [{"path": p, "lfs_sha256": by_path[p].lfs_sha256, "n": len(ls), "lines": sorted(ls)}
            for p, ls in sorted(lines.items())]


def _token_totals(docs: list[dict]) -> dict:
    return {"n_tokens": sum(d["n_tokens"] for d in docs),
            "n_tokens_orig": sum(d["n_tokens_orig"] for d in docs),
            "n_truncated": sum(d["n_tokens"] < d["n_tokens_orig"] for d in docs)}


def _base_manifest(corpus: Corpus, name: str, file: Path, sha: str, docs: list[dict]) -> dict:
    return {
        "name": name,
        "experiment": C.EXP,
        "file": str(file.relative_to(corpus.root)) if file.is_relative_to(corpus.root)
        else str(file),
        "sha256": sha,
        "n": len(docs),
        "tokens": _token_totals(docs),
        "source": {"repo": C.DOLMA_REPO, "revision": C.DOLMA_REVISION,
                   "shard_list": C.SHARD_LIST, "shard_list_sha256": C.SHARD_LIST_SHA256},
        "tokenizer": {"hf_id": C.HF_ID, "revision": C.REVISION},
        "rules": {"max_docs_per_shard": corpus.cfg.cap,
                  "candidate_tokens": list(corpus.cfg.cand_tokens),
                  "background_min_tokens": corpus.cfg.bg_min_tokens,
                  "removed_mark": C.REMOVED_MARK, "trunc_max_tokens": C.TRUNC_MAX_TOKENS,
                  "trunc_min_sentence_tokens": C.TRUNC_MIN_SENTENCE_TOKENS,
                  "candidate_shard_fraction": C.CANDIDATE_SHARD_FRACTION},
        "code": code_commit() | {"code_version": CODE_VERSION, "numpy": np.__version__},
        "utc": _utc(),
    }


def _opened_block(streams: Iterable[DocStream]) -> list[dict]:
    out = []
    for s in streams:
        for sh in s.opened():
            st = s.shard_stats[sh.path]
            out.append({"path": sh.path, "lfs_sha256": sh.lfs_sha256, "size": sh.size,
                        "stats": st})
    return out


# --- candidate pools ----------------------------------------------------------------------------


def _spread(deficit: int, receivers: list[str]) -> dict[str, int]:
    """`deficit` split equally over sorted receivers; the remainder to the first ones."""
    if not receivers:
        return {}
    q, r = divmod(deficit, len(receivers))
    return {t: q + (i < r) for i, t in enumerate(sorted(receivers))}


def pool_quotas(topics: list[str], p1_n: int, p2_n: int) -> tuple[dict, dict]:
    topics = sorted(topics)
    if p1_n % len(topics):
        raise ValueError("P1 must split equally over the topics")
    q1 = {t: p1_n // len(topics) for t in topics}
    q2 = _spread(p2_n, topics)
    return q1, q2


def sample_pools(corpus: Corpus) -> dict:
    """Draw P1 and P2 together (§4.2), split P2 into P2a/P2b, write files and manifests."""
    cfg, cand = corpus.cfg, corpus.split["candidate"]
    topics = sorted(cand)
    q1, q2 = pool_quotas(topics, cfg.p1_n, cfg.p2_n)
    initial = {"P1": dict(q1), "P2": dict(q2)}
    seen: set[str] = set()
    streams = {t: DocStream(corpus, shard_order(cand[t], derive_seed(cfg.seed, "pools", t)),
                            kind="candidate", seed=cfg.seed, seen=seen) for t in topics}
    for t in topics:  # start downloads for every topic's expected shards at once
        streams[t].prefetch(-(-(q1[t] + q2[t]) // cfg.cap) + 1)
    taken: dict[str, list[dict]] = {t: [] for t in topics}
    short: list[str] = []
    realloc: list[dict] = []
    try:
        while True:
            new_short = []
            for t in topics:
                if t in short:
                    continue
                need = q1[t] + q2[t]
                while len(taken[t]) < need:
                    d = streams[t].next()
                    if d is None:
                        new_short.append(t)
                        break
                    taken[t].append(d)
                corpus.log(f"pools: {t} {len(taken[t])}/{need}")
            if not new_short:
                break
            short += new_short
            for t in new_short:
                a = len(taken[t])
                n1 = min(q1[t], a * q1[t] // (q1[t] + q2[t]))
                n2 = a - n1
                d1, d2 = q1[t] - n1, q2[t] - n2
                q1[t], q2[t] = n1, n2
                recv = [u for u in topics if u not in short]
                if not recv:
                    raise RuntimeError("every topic is short of candidate documents")
                s1, s2 = _spread(d1, recv), _spread(d2, recv)
                for u in recv:
                    q1[u] += s1[u]
                    q2[u] += s2[u]
                realloc.append({"topic": t, "available": a, "P1": n1, "P2": n2,
                                "deficit_P1": d1, "deficit_P2": d2, "to": sorted(recv)})
                corpus.log(f"pools: {t} short ({a}); P1 -{d1}, P2 -{d2} spread over "
                           f"{len(recv)} topics")
    finally:
        corpus.close()

    p1, p2 = [], []
    for t in topics:
        perm = np.random.default_rng(derive_seed(cfg.seed, "pools-split", t)).permutation(
            len(taken[t]))
        in_p1 = {int(i) for i in perm[: q1[t]]}
        for i, d in enumerate(taken[t]):
            (p1 if i in in_p1 else p2).append(d)
    assert len(p1) == cfg.p1_n and len(p2) == cfg.p2_n, (len(p1), len(p2))
    perm = np.random.default_rng(derive_seed(cfg.seed, "P2-split")).permutation(len(p2))
    in_a = {int(i) for i in perm[: cfg.p2a_n]}
    p2a = [d for i, d in enumerate(p2) if i in in_a]
    p2b = [d for i, d in enumerate(p2) if i not in in_a]

    by_path = {s.path: s for s in corpus.shards}
    draw = {
        "seed": cfg.seed,
        "seeds": {"topic_order": "derive_seed(seed, 'pools', topic)",
                  "within_shard": "derive_seed(seed, 'candidate', shard path)",
                  "p1_p2_split": "derive_seed(seed, 'pools-split', topic)",
                  "p2a_p2b_split": "derive_seed(seed, 'P2-split')"},
        "quotas_initial": initial,
        "quotas_final": {"P1": q1, "P2": q2},
        "shortfall_reallocation": realloc,
        "dedup": {t: dict(streams[t].counts) for t in topics},
        "eligibility": {t: streams[t].eligibility() for t in topics},
        "candidate_shards_per_topic": {t: len(cand[t]) for t in topics},
        "shards_opened": _opened_block(streams.values()),
        "cursors": {t: list(streams[t].cursor) for t in topics},
    }
    out = {}
    pdir = corpus.root / C.POOLS_DIR
    for name, docs in (("P1", p1), ("P2a", p2a), ("P2b", p2b)):
        f = pdir / f"{name}.jsonl"
        sha = write_jsonl(f, docs, C.DOC_KEYS)
        man = _base_manifest(corpus, name, f, sha, docs) | {
            "counts_per_topic": dict(sorted(Counter(d["topic"] for d in docs).items())),
            "shards": _shards_block(docs, by_path),
            "draw": draw,
        }
        write_manifest(pdir / f"{name}.manifest.json", man)
        out[name] = man
        corpus.log(f"wrote {f} n={len(docs)} sha256={sha}")
    return out


def pool_hashes(corpus: Corpus, names: Iterable[str]) -> tuple[set[str], dict[str, str]]:
    """Content hashes of the named pool files, and each file's verified sha256."""
    hs: set[str] = set()
    shas = {}
    for name in names:
        f = corpus.root / C.POOLS_DIR / f"{name}.jsonl"
        man = json.loads((corpus.root / C.POOLS_DIR / f"{name}.manifest.json").read_text())
        sha = sha256_file(f)
        if sha != man["sha256"]:
            raise ValueError(f"{f}: sha256 {sha} != manifest {man['sha256']}")
        shas[name] = sha
        hs.update(d["content_sha256"] for d in read_jsonl(f))
    return hs, shas


# --- held-out general text (Amendment G) -------------------------------------------------------

HELDOUT_V1 = f"{HELDOUT_NAME}.v1"  # the superseded first draw (3 shards), kept for the record
AMENDMENT_G = ("Amendment G (Jacob, 2026-10-07): the first held-out draw took all 1,000 documents "
               "from 3 shards; resampled with at most 10 documents per shard (>= 100 shards), "
               "excluding every candidate-pool and background document")


def background_hashes(corpus: Corpus, seeds: Iterable[int]) -> tuple[set[str], dict[str, str]]:
    """Content hashes of the background files s{seed}.jsonl, each verified against its manifest."""
    hs: set[str] = set()
    shas = {}
    for s in seeds:
        f, mf = _bg_paths(corpus, s)
        if not f.exists() or not mf.exists():
            raise FileNotFoundError(f"background s{s} missing: {f}")
        man = json.loads(mf.read_text())
        if (sha := sha256_file(f)) != man["sha256"]:
            raise ValueError(f"{f}: sha256 {sha} != manifest {man['sha256']}")
        shas[f"s{s}"] = sha
        hs.update(d["content_sha256"] for d in read_jsonl(f))
    return hs, shas


def _archive_heldout_v1(corpus: Corpus) -> dict | None:
    """Move the first-draw held-out text to heldout_text.v1.* (once); return its manifest."""
    pdir = corpus.root / C.POOLS_DIR
    cur, cur_m = pdir / f"{HELDOUT_NAME}.jsonl", pdir / f"{HELDOUT_NAME}.manifest.json"
    old, old_m = pdir / f"{HELDOUT_V1}.jsonl", pdir / f"{HELDOUT_V1}.manifest.json"
    if old_m.exists():
        return json.loads(old_m.read_text())
    if not cur_m.exists():
        return None
    man = json.loads(cur_m.read_text())
    if "supersedes" in man:  # already the Amendment G draw, but no v1 on disk
        return None
    if sha256_file(cur) != man["sha256"]:
        raise ValueError(f"{cur} does not match its manifest; not archiving")
    os.replace(cur, old)
    man = man | {"name": HELDOUT_V1, "file": str(old.relative_to(corpus.root)),
                 "archived": {"from": str(cur.relative_to(corpus.root)), "reason": AMENDMENT_G,
                              "utc": _utc(), "status": "superseded; used nowhere"}}
    write_manifest(old_m, man)
    cur_m.unlink()
    corpus.log(f"archived {cur} -> {old}")
    return man


def sample_heldout(corpus: Corpus, bg_seeds: Iterable[int] = C.CORPUS_CPT_SEEDS) -> dict:
    """1,000 held-out documents (§4.2, Amendment G): background shards byte-weighted over all
    directories, at most `heldout_cap` eligible records per shard, at least `heldout_min_shards`
    shards, excluding every P1/P2a/P2b and background (seeds `bg_seeds`) content hash."""
    cfg = corpus.cfg
    bg_seeds = tuple(bg_seeds)
    excl, inputs = pool_hashes(corpus, POOL_NAMES)
    bg_h, bg_inputs = background_hashes(corpus, bg_seeds)
    excl |= bg_h
    inputs |= bg_inputs
    v1 = _archive_heldout_v1(corpus)
    order = shard_order(corpus.split["background"], derive_seed(cfg.seed, "heldout-g"))
    st = DocStream(corpus, order, kind="background", seed=cfg.seed, seen=set(), exclude=excl,
                   cap=cfg.heldout_cap)
    docs = []
    try:
        while len(docs) < cfg.heldout_n:
            d = st.next()
            if d is None:
                raise RuntimeError("background shards exhausted")
            docs.append(d)
    finally:
        corpus.close()
    n_shards = len({d["shard"] for d in docs})
    if n_shards < cfg.heldout_min_shards:
        raise RuntimeError(f"held-out text spans {n_shards} shards < {cfg.heldout_min_shards}")
    f = corpus.root / C.POOLS_DIR / f"{HELDOUT_NAME}.jsonl"
    sha = write_jsonl(f, docs, C.DOC_KEYS)
    by_path = {s.path: s for s in corpus.shards}
    fam = Counter(d["source_dir"].split("-")[0] for d in docs)
    man = _base_manifest(corpus, HELDOUT_NAME, f, sha, docs) | {
        "counts_per_family": dict(sorted(fam.items())),
        "counts_per_source_dir": dict(sorted(Counter(d["source_dir"] for d in docs).items())),
        "n_shards": n_shards,
        "max_docs_per_shard": cfg.heldout_cap,
        "min_shards": cfg.heldout_min_shards,
        "shards": _shards_block(docs, by_path),
        "supersedes": {"file": v1["file"] if v1 else None,
                       "sha256": v1["sha256"] if v1 else None, "reason": AMENDMENT_G},
        "draw": {"seed": cfg.seed,
                 "seeds": {"shard_order": "derive_seed(seed, 'heldout-g')",
                           "within_shard": "derive_seed(seed, 'background', shard path)"},
                 "background_seeds_excluded": list(bg_seeds),
                 "excluded_inputs": inputs, "dedup": dict(st.counts),
                 "eligibility": st.eligibility(), "shards_opened": _opened_block([st]),
                 "cursor": list(st.cursor)},
    }
    write_manifest(corpus.root / C.POOLS_DIR / f"{HELDOUT_NAME}.manifest.json", man)
    corpus.log(f"wrote {f} n={len(docs)} shards={n_shards} sha256={sha}")
    return man


# --- backgrounds --------------------------------------------------------------------------------


def reserves_for(cpt_seed: int) -> dict[str, dict]:
    src = (C.BACKGROUND_RESERVES_CONFIRM if cpt_seed in C.CONFIRM_SEEDS_FOR_RESERVES
           else C.BACKGROUND_RESERVES)
    return {r: dict(src[r]) for r in ROLE_ORDER if r in src}


def _role_progress(spec: dict, docs: list[dict]) -> int:
    if "min_tokens" in spec:
        return sum(d["n_tokens"] for d in docs)
    return sum(d["n_tokens"] >= spec.get("min_doc_tokens", 0) for d in docs)


def _role_target(spec: dict) -> int:
    return spec["min_tokens"] if "min_tokens" in spec else spec["min_docs"]


def _role_done(spec: dict, docs: list[dict]) -> bool:
    return _role_progress(spec, docs) >= _role_target(spec)


def _bg_exclusions(corpus: Corpus) -> tuple[set[str], dict[str, str]]:
    """P1, P2a, P2b and every held-out draw on disk (the superseded v1 and the current one)."""
    pdir = corpus.root / C.POOLS_DIR
    names = [*POOL_NAMES] + [n for n in (HELDOUT_V1, HELDOUT_NAME)
                             if (pdir / f"{n}.manifest.json").exists()]
    return pool_hashes(corpus, names)


def _bg_stream(corpus: Corpus, cpt_seed: int, excl, seen, cursor=(0, 0)) -> DocStream:
    bseed = C.background_seed(cpt_seed)
    order = shard_order(corpus.split["background"], derive_seed(bseed, "background"))
    return DocStream(corpus, order, kind="background", seed=bseed, seen=seen, exclude=excl,
                     cursor=cursor)


def _bg_paths(corpus: Corpus, cpt_seed: int) -> tuple[Path, Path]:
    d = corpus.root / C.BACKGROUND_DIR
    return d / f"s{cpt_seed}.jsonl", d / f"s{cpt_seed}.manifest.json"


def _bg_summary(recs: list[dict], reserves: dict) -> dict:
    roles = {}
    for r, spec in reserves.items():
        ds = [d for d in recs if d["role"] == r]
        roles[r] = {"n": len(ds), "target": spec, "progress": _role_progress(spec, ds),
                    "tokens": _token_totals(ds)}
        if "min_doc_tokens" in spec:
            roles[r]["n_qualifying"] = roles[r]["progress"]
    return roles


def _bg_write(corpus, cpt_seed, recs, reserves, st: DocStream, inputs, extensions, prior=None):
    f, mf = _bg_paths(corpus, cpt_seed)
    sha = write_jsonl(f, recs, C.BACKGROUND_KEYS)
    by_path = {s.path: s for s in corpus.shards}
    prior = prior or {}
    dedup = Counter(prior.get("dedup", {}))
    dedup.update(st.counts)
    elig = Counter(prior.get("eligibility", {}))
    elig.update(st.eligibility())
    opened = {o["path"]: o for o in prior.get("shards_opened", [])}
    opened.update({o["path"]: o for o in _opened_block([st])})
    man = _base_manifest(corpus, f"s{cpt_seed}", f, sha, recs) | {
        "cpt_seed": cpt_seed,
        "background_seed": C.background_seed(cpt_seed),
        "seeds": {"shard_order": "derive_seed(background_seed, 'background')",
                  "within_shard": "derive_seed(background_seed, 'background', shard path)"},
        "role_order": [r for r in ROLE_ORDER if r in reserves],
        "reserves": reserves,
        "roles": _bg_summary(recs, reserves),
        "counts_per_source_dir": dict(sorted(Counter(d["source_dir"] for d in recs).items())),
        "excluded_inputs": inputs,
        "dedup": dict(dedup),
        "eligibility": dict(elig),
        "cursor": list(st.cursor),
        "extensions": extensions,
        "shards": _shards_block(recs, by_path),
        "shards_opened": sorted(opened.values(), key=lambda o: o["path"]),
    }
    write_manifest(mf, man)
    corpus.log(f"wrote {f} n={len(recs)} sha256={sha}")
    return man


def sample_background(corpus: Corpus, cpt_seed: int, reserves: dict | None = None) -> dict:
    """One background sequence for CPT seed s, assigned to roles in the fixed order."""
    reserves = reserves or reserves_for(cpt_seed)
    roles = [r for r in ROLE_ORDER if r in reserves]
    excl, inputs = _bg_exclusions(corpus)
    st = _bg_stream(corpus, cpt_seed, excl, seen=set())
    recs: list[dict] = []
    try:
        for r in roles:
            mine: list[dict] = []
            while not _role_done(reserves[r], mine):
                d = st.next()
                if d is None:
                    raise RuntimeError("background shards exhausted")
                rec = d | {"role": r, "order": len(recs)}
                mine.append(rec)
                recs.append(rec)
            corpus.log(f"background s{cpt_seed}: {r} n={len(mine)}")
    finally:
        corpus.close()
    return _bg_write(corpus, cpt_seed, recs, reserves, st, inputs, extensions=[])


def extend(corpus: Corpus, cpt_seed: int, role: str, extra_tokens: int | None = None,
           extra_docs: int | None = None) -> dict:
    """Raise `role`'s target by `extra_tokens` (token roles) or `extra_docs` (the doc-count
    filler) and continue the seed's sequence from its cursor, appending to that role.

    Equivalent to having sampled with the raised target from the start when `role` is the last
    role; for an earlier role the appended documents come from later in the same sequence, so
    every role stays disjoint and `order` stays increasing.
    """
    f, mf = _bg_paths(corpus, cpt_seed)
    man = json.loads(mf.read_text())
    if sha256_file(f) != man["sha256"]:
        raise ValueError(f"{f} does not match its manifest")
    reserves = man["reserves"]
    if role not in reserves:
        raise ValueError(f"role {role!r} not in {list(reserves)}")
    spec = dict(reserves[role])
    if "min_tokens" in spec:
        if extra_tokens is None or extra_docs is not None:
            raise ValueError(f"{role} is a token reserve: pass extra_tokens")
        spec["min_tokens"] += extra_tokens
    else:
        if extra_docs is None or extra_tokens is not None:
            raise ValueError(f"{role} is a document reserve: pass extra_docs")
        spec["min_docs"] += extra_docs
    excl, inputs = _bg_exclusions(corpus)
    # every input the background was drawn against must still be on disk unchanged (by sha256);
    # inputs added since (the Amendment G held-out text, which excludes every background
    # document) only remove documents this sequence has not reached yet
    if missing := set(man["excluded_inputs"].values()) - set(inputs.values()):
        raise ValueError(f"inputs this background excluded are gone or changed: {missing}")
    recs = read_jsonl(f)
    seen = {d["content_sha256"] for d in recs}
    st = _bg_stream(corpus, cpt_seed, excl, seen=seen, cursor=tuple(man["cursor"]))
    mine = [d for d in recs if d["role"] == role]
    before = len(recs)
    try:
        while not _role_done(spec, mine):
            d = st.next()
            if d is None:
                raise RuntimeError("background shards exhausted")
            rec = d | {"role": role, "order": len(recs)}
            mine.append(rec)
            recs.append(rec)
    finally:
        corpus.close()
    reserves = dict(reserves) | {role: spec}
    ext = man.get("extensions", []) + [{
        "role": role, "extra_tokens": extra_tokens, "extra_docs": extra_docs,
        "new_target": spec, "added": len(recs) - before, "cursor_before": man["cursor"],
        "cursor_after": list(st.cursor), "prior_sha256": man["sha256"], "utc": _utc()}]
    return _bg_write(corpus, cpt_seed, recs, reserves, st, inputs, ext, prior=man)


# --- verification -------------------------------------------------------------------------------


def verify(root: str | Path) -> list[str]:
    """Re-hash every output against its manifest; returns a list of problems (empty = ok)."""
    root = Path(root)
    problems = []
    mans = sorted((root / C.POOLS_DIR).glob("*.manifest.json")) + sorted(
        (root / C.BACKGROUND_DIR).glob("*.manifest.json"))
    if not mans:
        problems.append("no manifests found")
    for mf in mans:
        man = json.loads(mf.read_text())
        f = root / man["file"]
        if not f.exists():
            problems.append(f"{f}: missing")
            continue
        if (sha := sha256_file(f)) != man["sha256"]:
            problems.append(f"{f}: sha256 {sha} != {man['sha256']}")
        with open(f, encoding="utf-8") as fh:
            n = sum(1 for line in fh if line.strip())
        if n != man["n"]:
            problems.append(f"{f}: {n} records != {man['n']}")
    return problems
