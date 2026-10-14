"""P42 data prep (plan.md "Setup"): download the pinned sources, extract first user turns,
deduplicate (exact + MinHash near-duplicates against all of Dolci), sample the six prompt sets and
write `data/P42/prompts.jsonl` (PROMPT_FIELDS, sorted by pid) plus `data/P42/prompts.meta.json`.

    uv run --with pyarrow --with xxhash python -m p42.data build [--pool-source lmsys|wildchat48]
    uv run --with pyarrow --with xxhash python -m p42.data build --smoke     # tiny, data/P42/smoke/
    uv run --with pyarrow --with xxhash python -m p42.data download [--source dolci_dpo ...]

Free (HF downloads only). Laptop-friendly: parquet is streamed by record batch with only the
needed columns, MinHash signatures are computed in <= 4 worker processes and cached under
`data/P42/raw/cache/`, so a rerun skips extraction and signatures.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

from p42 import contracts as K

HERE = Path(__file__).resolve()
N_WORKERS = 4
# WildChat-4.8M fallback: a few shards spread over the 86 (time-ordered), first turns only.
WILDCHAT_SHARDS = (0, 14, 28, 42, 56, 70)
SMOKE_TARGET = {K.ID_REF: 600, K.ID_HELDOUT: 100, K.SFT_REF: 200, K.POOL: 600,
                K.PLANTED_MED: 50, K.PLANTED_LAW: 50}
SMOKE_N_GEN_REF = 300
SMOKE_ROWS = 4000  # rows read per source in --smoke

LMSYS_GATED_MSG = (
    "lmsys/lmsys-chat-1m is gated and the HF token in .env gets 403 on its data files. Jacob must "
    "accept the terms at https://huggingface.co/datasets/lmsys/lmsys-chat-1m (auto-approve), then "
    "rerun. Fallback (weaker pool, plan 'Setup'): rerun with --pool-source wildchat48.")


class GatedSourceError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------
# Download
# ------------------------------------------------------------------------------------------------

def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    env = K.REPO_ROOT / ".env"
    if env.exists():
        load_dotenv(env, override=False)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # AGENTS.md: keep xet off by default


def source_files(source: str, all_files: list[str]) -> list[str]:
    """The data files P42 reads from a source's file listing."""
    if source == "wildchat48":
        want = {f"data/train-{i:05d}-of-00086.parquet" for i in WILDCHAT_SHARDS}
        return sorted(f for f in all_files if f in want)
    if source == "lawse":
        return sorted(f for f in all_files if f.endswith(".json"))
    return sorted(f for f in all_files if f.startswith("data/") and f.endswith(".parquet"))


def _sha256_file(path: Path, cache: bool = True) -> str:
    """sha256, cached (cache=True) in a sidecar keyed on size and mtime."""
    side = path.with_name(path.name + ".sha256.json")
    st = path.stat()
    if cache and side.exists():
        rec = json.loads(side.read_text())
        if rec.get("size") == st.st_size and rec.get("mtime") == st.st_mtime:
            return rec["sha256"]
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    if cache:
        side.write_text(json.dumps({"size": st.st_size, "mtime": st.st_mtime,
                                    "sha256": h.hexdigest()}))
    return h.hexdigest()


def resolve_revision(api, repo: str, rev: str) -> tuple[str, list[str]]:
    """Full commit sha for a pinned (possibly prefix) revision, and the repo file listing."""
    info = api.dataset_info(repo, revision=rev)
    if not info.sha.startswith(rev):
        raise RuntimeError(f"{repo}: resolved sha {info.sha} does not start with pin {rev}")
    return info.sha, [s.rfilename for s in info.siblings]


def download(source: str, raw_dir: Path = K.RAW_DIR, max_files: int | None = None) -> dict:
    """Download a source's files at its pinned revision into raw_dir/<source>/. Returns a record
    {repo, revision, files: {name: {sha256, size}}, paths: [...]}. Raises GatedSourceError on 403."""
    _load_env()
    from huggingface_hub import HfApi, hf_hub_download
    from huggingface_hub.errors import GatedRepoError

    repo, pin = K.SOURCES[source]
    api = HfApi()
    sha, listing = resolve_revision(api, repo, pin)
    files = source_files(source, listing)
    if max_files is not None:
        files = files[:max_files]
    dest = raw_dir / source
    dest.mkdir(parents=True, exist_ok=True)
    rec = {"repo": repo, "pin": pin, "revision": sha, "files": {}, "paths": []}
    for fn in files:
        try:
            p = Path(hf_hub_download(repo, fn, repo_type="dataset", revision=sha, local_dir=dest))
        except GatedRepoError as e:
            if source == "lmsys":
                raise GatedSourceError(LMSYS_GATED_MSG) from e
            raise GatedSourceError(f"{repo} is gated for our token: {e}") from e
        rec["files"][fn] = {"sha256": _sha256_file(p), "size": p.stat().st_size}
        rec["paths"].append(str(p))
        print(f"[download] {source} {fn} {p.stat().st_size / 1e6:.0f} MB", flush=True)
    return rec


# ------------------------------------------------------------------------------------------------
# First-turn extraction (one function per source shape)
# ------------------------------------------------------------------------------------------------

def first_user_content(messages) -> str | None:
    """Content of the first message with role 'user' in a list of {role, content} dicts."""
    if not messages:
        return None
    for m in messages:
        if m is not None and m.get("role") == "user":
            return m.get("content")
    return None


def first_turn_if_user(conversation) -> str | None:
    """LMSYS / WildChat: conversation[0] if its role is 'user'."""
    if not conversation:
        return None
    m = conversation[0]
    if m is None or m.get("role") != "user":
        return None
    return m.get("content")


def dolci_dpo_category(prompt_id: str) -> str:
    return prompt_id.rsplit("_", 1)[0][:40]


_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    if "<" not in s and "&" not in s:
        return s
    return html.unescape(_TAG.sub("", s))


def lawse_prompt(title: str | None, body: str | None) -> str:
    return (strip_html(title or "").strip() + "\n\n" + strip_html(body or "").strip()).strip()


def nonempty(s) -> bool:
    return isinstance(s, str) and s.strip() != ""


def _first_user_vectorised(col) -> tuple[np.ndarray, list]:
    """pyarrow ListArray<struct<role, content, ...>> -> (row indices with a user turn, contents).
    Vectorised: flattens the list once and picks the first 'user' element per row."""
    import pyarrow.compute as pc

    col = col.combine_chunks() if hasattr(col, "combine_chunks") else col
    flat = pc.list_flatten(col)
    parents = np.asarray(pc.list_parent_indices(col))
    roles = flat.field("role")
    is_user = np.asarray(pc.fill_null(pc.equal(roles, "user"), False))
    idx = np.nonzero(is_user)[0]
    rows, first = np.unique(parents[idx], return_index=True)
    contents = flat.field("content").take(idx[first]).to_pylist()
    return rows, contents


def _first_elem_vectorised(col) -> tuple[np.ndarray, list]:
    """conversation[0] if role == 'user', vectorised (LMSYS / WildChat)."""
    import pyarrow.compute as pc

    col = col.combine_chunks() if hasattr(col, "combine_chunks") else col
    lens = np.asarray(pc.fill_null(pc.list_value_length(col), 0))
    rows = np.nonzero(lens > 0)[0]
    offs = np.asarray(col.offsets)[rows]
    flat = col.values  # offsets-relative values
    roles = np.asarray(flat.field("role").take(offs).to_pylist(), dtype=object)
    keep = roles == "user"
    rows, offs = rows[keep], offs[keep]
    contents = flat.field("content").take(offs).to_pylist()
    return rows, contents


def _iter_batches(path: Path, columns: list[str], limit: int | None, batch_size: int = 8192):
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    seen = 0
    for b in pf.iter_batches(batch_size=batch_size, columns=columns):
        if limit is not None and seen >= limit:
            return
        if limit is not None and seen + b.num_rows > limit:
            b = b.slice(0, limit - seen)
        seen += b.num_rows
        yield b


def _moderation(rec) -> tuple[bool | None, float | None]:
    """(flagged, max category score) of one OpenAI moderation record; (None, None) if absent."""
    if rec is None:
        return None, None
    flagged = rec.get("flagged")
    scores = rec.get("category_scores")
    vals = [v for v in (scores or {}).values() if v is not None]
    return (bool(flagged) if flagged is not None else None), (float(max(vals)) if vals else None)


COLUMNS = ("source_id", "category", "text", "lang", "mod_flagged", "mod_max")
_READ_COLS = {
    "dolci_dpo": ["prompt_id", "chosen"],
    "dolci_sft": ["id", "messages", "source_dataset"],
    "lmsys": ["conversation_id", "conversation", "language", "openai_moderation"],
    "wildchat48": ["conversation_hash", "conversation", "language", "openai_moderation"],
    "chatdoctor": ["input"],
}


def extract_batches(source: str, paths: list[str], counts: dict, limit: int | None = None):
    """Yield dicts of COLUMNS lists (one per record batch) of non-empty first user turns.
    `counts` is updated in place: raw rows, rows with a user first turn, non-empty."""
    for k in ("raw", "with_user_turn", "nonempty"):
        counts.setdefault(k, 0)

    def new():
        return {c: [] for c in COLUMNS}

    def add(out, sid, cat, text, lang=None, mf=None, mm=None):
        counts["with_user_turn"] += 1
        if not nonempty(text):
            return
        counts["nonempty"] += 1
        out["source_id"].append(str(sid))
        out["category"].append(cat)
        out["text"].append(text)
        out["lang"].append(lang)
        out["mod_flagged"].append(mf)
        out["mod_max"].append(mm)

    if source == "lawse":
        rows = json.loads(Path(paths[0]).read_text())
        if limit is not None:
            rows = rows[:limit]
        out = new()
        for i, r in enumerate(rows):
            counts["raw"] += 1
            add(out, r.get("question_id", i), "legal",
                lawse_prompt(r.get("question_title"), r.get("question_body")))
        yield out
        return

    remaining = limit
    base = 0  # running row index over all files (chatdoctor source_id)
    for p in paths:
        if remaining is not None and remaining <= 0:
            break
        for b in _iter_batches(Path(p), _READ_COLS[source], remaining):
            n = b.num_rows
            counts["raw"] += n
            if remaining is not None:
                remaining -= n
            out = new()
            if source == "dolci_dpo":
                rows, contents = _first_user_vectorised(b.column("chosen"))
                pids = b.column("prompt_id").to_pylist()
                for r, c in zip(rows.tolist(), contents):
                    add(out, pids[r], dolci_dpo_category(pids[r]), c)
            elif source == "dolci_sft":
                rows, contents = _first_user_vectorised(b.column("messages"))
                ids = b.column("id").to_pylist()
                srcs = b.column("source_dataset").to_pylist()
                for r, c in zip(rows.tolist(), contents):
                    add(out, ids[r], srcs[r], c)
            elif source in ("lmsys", "wildchat48"):
                rows, contents = _first_elem_vectorised(b.column("conversation"))
                idcol = "conversation_id" if source == "lmsys" else "conversation_hash"
                ids = b.column(idcol).to_pylist()
                langs = b.column("language").to_pylist()
                modc = b.column("openai_moderation")
                for r, c in zip(rows.tolist(), contents):
                    mods = modc[r].as_py()
                    mf, mm = _moderation(mods[0] if mods else None)
                    add(out, ids[r], None, c, langs[r], mf, mm)
            elif source == "chatdoctor":
                for r, c in enumerate(b.column("input").to_pylist()):
                    add(out, base + r, "medical", c)
            else:
                raise ValueError(source)
            base += n
            yield out


def extract(source: str, paths: list[str], limit: int | None = None) -> tuple[dict, dict]:
    """All batches of extract_batches concatenated (small inputs and tests)."""
    counts: dict = {}
    cols = {c: [] for c in COLUMNS}
    for d in extract_batches(source, paths, counts, limit):
        for c in COLUMNS:
            cols[c] += d[c]
    return cols, counts


# ------------------------------------------------------------------------------------------------
# Normalisation, exact hashes, MinHash
# ------------------------------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def normalise(s: str) -> str:
    return _WS.sub(" ", s.lower()).strip()


def exact_hash(s: str) -> int:
    """First 8 bytes of sha1(normalised text) as a uint64-compatible int."""
    return int.from_bytes(hashlib.sha1(normalise(s).encode("utf-8", "surrogatepass")).digest()[:8], "little")


def exact_hashes(texts) -> np.ndarray:
    return np.fromiter((exact_hash(t) for t in texts), dtype=np.uint64, count=len(texts))


def first_occurrence_mask(h: np.ndarray) -> np.ndarray:
    """True for the first occurrence of each value (keeps input order)."""
    _, first = np.unique(h, return_index=True)
    m = np.zeros(len(h), dtype=bool)
    m[first] = True
    return m


MERSENNE = np.uint64((1 << 31) - 1)
_SHINGLE_MULT = np.array([0x9E3779B97F4A7C15, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9,
                          0xD6E8FEB86659FD93, 0xFF51AFD7ED558CCD], dtype=np.uint64)


def _perm_params(n_perm: int = K.MINHASH_PERM, seed: int = K.SEED_SAMPLE):
    rng = np.random.default_rng([seed, 7])
    a = rng.integers(1, int(MERSENNE), size=n_perm, dtype=np.uint64)
    b = rng.integers(0, int(MERSENNE), size=n_perm, dtype=np.uint64)
    return a, b


def shingle_hashes(text: str, k: int = K.MINHASH_SHINGLE) -> np.ndarray:
    """31-bit hashes of the word k-grams of the normalised text (whole text if < k words)."""
    import xxhash

    words = normalise(text).split(" ")
    if len(words) < k:
        s = np.array([xxhash.xxh64_intdigest(" ".join(words).encode("utf-8", "surrogatepass"))],
                     dtype=np.uint64)
    else:
        wh = np.fromiter((xxhash.xxh64_intdigest(w.encode("utf-8", "surrogatepass"))
                          for w in words), dtype=np.uint64, count=len(words))
        n = len(words) - k + 1
        with np.errstate(over="ignore"):
            s = np.zeros(n, dtype=np.uint64)
            for j in range(k):
                s = s + wh[j:j + n] * _SHINGLE_MULT[j]
    with np.errstate(over="ignore"):
        s = (s >> np.uint64(32)) ^ (s & np.uint64(0xFFFFFFFF))
    return s % MERSENNE


def minhash_signatures(texts, n_perm: int = K.MINHASH_PERM, max_block: int = 200_000) -> np.ndarray:
    """(len(texts), n_perm) uint32 MinHash signatures, (a*x + b) mod (2^31 - 1)."""
    a, b = _perm_params(n_perm)
    out = np.empty((len(texts), n_perm), dtype=np.uint32)
    i = 0
    while i < len(texts):
        sh, offs, j, tot = [], [0], i, 0
        while j < len(texts) and (tot == 0 or tot < max_block):
            h = shingle_hashes(texts[j])
            sh.append(h)
            tot += len(h)
            offs.append(tot)
            j += 1
        x = np.concatenate(sh)
        out_blk = np.empty((len(sh), n_perm), dtype=np.uint64)
        # (a*x + b) mod p over shingles, min-reduced per text; chunked over perms for memory
        for p0 in range(0, n_perm, 32):
            ap, bp = a[p0:p0 + 32], b[p0:p0 + 32]
            v = (x[:, None] * ap[None, :] + bp[None, :]) % MERSENNE
            out_blk[:, p0:p0 + 32] = np.minimum.reduceat(v, np.array(offs[:-1]), axis=0)
        out[i:j] = out_blk.astype(np.uint32)
        i = j
    return out


def _sig_worker(texts):
    return minhash_signatures(texts)


def minhash_parallel(chunks, n_workers: int = N_WORKERS) -> np.ndarray:
    """MinHash over an iterable of text chunks (lists), in order, with at most 2 * n_workers
    chunks in flight (so a lazily produced iterable is never fully materialised)."""
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor

    parts, inflight = [], deque()
    if n_workers <= 1:
        return np.concatenate([minhash_signatures(c) for c in chunks] or
                              [np.empty((0, K.MINHASH_PERM), np.uint32)])
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        for c in chunks:
            inflight.append(ex.submit(_sig_worker, c))
            if len(inflight) >= 2 * n_workers:
                parts.append(inflight.popleft().result())
        while inflight:
            parts.append(inflight.popleft().result())
    return np.concatenate(parts) if parts else np.empty((0, K.MINHASH_PERM), np.uint32)


def est_jaccard(sa: np.ndarray, sb: np.ndarray) -> np.ndarray:
    return (sa == sb).mean(axis=-1)


N_BANDS, N_ROWS = 32, 4  # P(candidate | J=0.8) = 1 - (1 - 0.8^4)^32 > 0.9999999


def band_keys(sig: np.ndarray, n_bands: int = N_BANDS, n_rows: int = N_ROWS) -> np.ndarray:
    """(n, n_bands) uint64 band keys; equal bands -> equal keys (no false negatives)."""
    assert sig.shape[1] >= n_bands * n_rows
    s = sig[:, :n_bands * n_rows].astype(np.uint64).reshape(len(sig), n_bands, n_rows)
    with np.errstate(over="ignore"):
        k = np.zeros((len(sig), n_bands), dtype=np.uint64)
        for r in range(n_rows):
            k = k * np.uint64(0x100000001B3) + s[:, :, r] + np.uint64(r + 1)
            k ^= k >> np.uint64(29)
    return k


def candidate_prob(j: float, n_bands: int = N_BANDS, n_rows: int = N_ROWS) -> float:
    return 1.0 - (1.0 - j ** n_rows) ** n_bands


def near_dup_of_index(q_sig: np.ndarray, idx_sig: np.ndarray, thresh: float = K.MINHASH_JACCARD,
                      cap: int = 64) -> tuple[np.ndarray, dict]:
    """Bool mask over queries: True if some index signature has estimated Jaccard >= thresh.
    LSH banding (N_BANDS x N_ROWS); each query checks at most `cap` index rows per band bucket."""
    found = np.zeros(len(q_sig), dtype=bool)
    stats = {"pairs_checked": 0, "capped_lookups": 0}
    if len(q_sig) == 0 or len(idx_sig) == 0:
        return found, stats
    qk, ik = band_keys(q_sig), band_keys(idx_sig)
    for band in range(N_BANDS):
        todo = np.nonzero(~found)[0]
        if len(todo) == 0:
            break
        order = np.argsort(ik[:, band], kind="stable")
        sk = ik[order, band]
        keys = qk[todo, band]
        lo = np.searchsorted(sk, keys, side="left")
        hi = np.searchsorted(sk, keys, side="right")
        cnt = hi - lo
        stats["capped_lookups"] += int((cnt > cap).sum())
        has = cnt > 0
        todo, lo, cnt = todo[has], lo[has], cnt[has]
        for j in range(min(cap, int(cnt.max()) if len(cnt) else 0)):
            m = cnt > j
            qi, di = todo[m], order[lo[m] + j]
            keep = ~found[qi]
            qi, di = qi[keep], di[keep]
            if len(qi) == 0:
                continue
            for s0 in range(0, len(qi), 100_000):
                q_, d_ = qi[s0:s0 + 100_000], di[s0:s0 + 100_000]
                ok = est_jaccard(q_sig[q_], idx_sig[d_]) >= thresh
                stats["pairs_checked"] += len(q_)
                found[q_[ok]] = True
    return found, stats


def internal_near_dups(sig: np.ndarray, thresh: float = K.MINHASH_JACCARD,
                       cap: int = 64) -> int:
    """Number of items with at least one other item (same set) at estimated Jaccard >= thresh."""
    has = np.zeros(len(sig), dtype=bool)
    if len(sig) < 2:
        return 0
    keys = band_keys(sig)
    for band in range(N_BANDS):
        order = np.argsort(keys[:, band], kind="stable")
        sk = keys[order, band]
        brk = np.nonzero(np.diff(sk) != 0)[0] + 1
        starts = np.concatenate([[0], brk])
        ends = np.concatenate([brk, [len(sk)]])
        big = np.nonzero(ends - starts >= 2)[0]
        for g in big:
            members = order[starts[g]:min(ends[g], starts[g] + cap)]
            s = sig[members]
            eq = (s[:, None, :] == s[None, :, :]).mean(-1) >= thresh
            np.fill_diagonal(eq, False)
            has[members[eq.any(1)]] = True
    return int(has.sum())


# ------------------------------------------------------------------------------------------------
# Language heuristic, moderation bin, tokenisation
# ------------------------------------------------------------------------------------------------

ENGLISH_STOPWORDS = frozenset(
    ["the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were", "be", "for", "on", "with", "that", "this", "it", "as", "at", "by", "from", "what", "how", "why", "when", "which", "who", "can", "do", "does", "i", "you", "my", "your", "we", "our", "me", "please", "write"])
ENGLISH_HEURISTIC = (
    "Dolci and planted sets (no language field): 'English' when >= 90% of alphabetic characters "
    "are ASCII and the text contains >= 2 distinct common English stopwords (from a 40-word list), "
    "else 'unknown'. english = (lang == 'English').")


def detect_english(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "unknown"
    ascii_frac = sum(c.isascii() for c in letters) / len(letters)
    words = set(re.findall(r"[a-z']+", text.lower()))
    if ascii_frac >= 0.9 and len(words & ENGLISH_STOPWORDS) >= 2:
        return "English"
    return "unknown"


def mod_bin(flagged: bool | None, mod_max: float | None) -> int | None:
    if flagged is None:
        return None
    if not flagged:
        return K.MOD_BIN_UNFLAGGED
    if mod_max is None:
        return K.MOD_BIN_HIGH  # flag only, no scores: counted as HIGH (noted in meta)
    return K.MOD_BIN_LOW if mod_max < K.MOD_HIGH else K.MOD_BIN_HIGH


def tokenize_and_truncate(tok, texts: list[str], max_tokens: int = K.MAX_PROMPT_TOKENS,
                          batch: int = 2048) -> tuple[list[int], list[bool], list[str]]:
    """User-content token counts (no template, no special tokens), truncation flag, and the text
    (head-truncated by token ids + decode when longer than max_tokens)."""
    n, tr, out = [], [], []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        enc = tok(part, add_special_tokens=False)["input_ids"]
        for t, ids in zip(part, enc):
            n.append(len(ids))
            if len(ids) > max_tokens:
                tr.append(True)
                out.append(tok.decode(ids[:max_tokens]))
            else:
                tr.append(False)
                out.append(t)
    return n, tr, out


# ------------------------------------------------------------------------------------------------
# Sampling
# ------------------------------------------------------------------------------------------------

def rng_for(step: str) -> np.random.Generator:
    """Independent seeded stream per sampling step (SEED_SAMPLE, step name)."""
    tag = int.from_bytes(hashlib.sha256(step.encode()).digest()[:4], "little")
    return np.random.default_rng([K.SEED_SAMPLE, tag])


def length_bin_edges(pool_n: np.ndarray, n_bins: int = K.N_LEN_BINS) -> np.ndarray:
    return np.quantile(pool_n, np.arange(1, n_bins) / n_bins)


def length_bin(n: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.searchsorted(edges, n, side="right")


def bin_targets(pool_bins: np.ndarray, total: int, n_bins: int) -> np.ndarray:
    """Per-bin targets proportional to the pool's bin counts (= total / n_bins when the deciles
    are balanced; ties in short prompts can make them uneven). Largest-remainder rounding."""
    c = np.bincount(pool_bins, minlength=n_bins).astype(float)
    raw = total * c / c.sum()
    t = np.floor(raw).astype(int)
    rem = total - t.sum()
    t[np.argsort(-(raw - t), kind="stable")[:rem]] += 1
    return t


def length_matched_sample(cand_n: np.ndarray, pool_n: np.ndarray, total: int,
                          rng: np.random.Generator, n_bins: int = K.N_LEN_BINS
                          ) -> tuple[np.ndarray, dict]:
    """Indices into the candidates, length-matched to the pool's token-length deciles. A bin
    short of candidates is filled from the nearest bins (shortfall logged)."""
    edges = length_bin_edges(pool_n, n_bins)
    targets = bin_targets(length_bin(pool_n, edges), total, n_bins)
    cb = length_bin(cand_n, edges)
    avail = {b: list(rng.permutation(np.nonzero(cb == b)[0])) for b in range(n_bins)}
    chosen, short = [], {}
    for b in range(n_bins):
        need = int(targets[b])
        take = avail[b][:need]
        avail[b] = avail[b][need:]
        chosen += take
        miss = need - len(take)
        if miss:
            short[b] = miss
            for d in range(1, n_bins):
                for nb in (b - d, b + d):
                    if miss and 0 <= nb < n_bins and avail[nb]:
                        got = avail[nb][:miss]
                        avail[nb] = avail[nb][miss:]
                        chosen += got
                        miss -= len(got)
                if not miss:
                    break
    rec = {"edges": edges.tolist(), "targets": targets.tolist(), "shortfall_by_bin": short,
           "candidates_by_bin": np.bincount(cb, minlength=n_bins).tolist(),
           "chosen_by_bin": np.bincount(cb[np.array(chosen, dtype=int)],
                                        minlength=n_bins).tolist() if chosen else []}
    return np.array(chosen, dtype=int), rec


def assign_gen_split(rows: list[dict], n_gen_ref: int = K.N_GEN_REF) -> None:
    """Fill gen, gen_rank and split in place (seeded)."""
    by_set: dict[str, list[dict]] = {}
    for r in rows:
        by_set.setdefault(r["set"], []).append(r)
    for s, rs in by_set.items():
        if s == K.ID_REF:
            ranks = rng_for("gen_rank").permutation(len(rs))
            for r, k in zip(rs, ranks.tolist()):
                r["gen_rank"] = int(k)
                r["gen"] = k < n_gen_ref
                r["split"] = None
        elif s in K.SCORED_SETS:
            perm = rng_for(f"split:{s}").permutation(len(rs))
            half = len(rs) // 2
            for r, k in zip(rs, perm.tolist()):
                r["split"] = "dev" if k < half else "test"
                r["gen"] = True
                r["gen_rank"] = None
        else:
            for r in rs:
                r["gen"], r["gen_rank"], r["split"] = False, None, None


def make_rows(set_name: str, source: str, sub: dict, ntok, trunc, texts, pool: bool
              ) -> list[dict]:
    """Rows in sampled order; `sub` holds the selected source rows (COLUMNS lists, aligned)."""
    rows = []
    for i, (n, t, txt) in enumerate(zip(ntok, trunc, texts)):
        if pool:
            lang = sub["lang"][i] or "unknown"
            mf, mm = sub["mod_flagged"][i], sub["mod_max"][i]
            mb = mod_bin(mf, mm)
        else:
            lang = detect_english(sub["text"][i])
            mf = mm = mb = None
        rows.append({
            "pid": f"{set_name}-{i:06d}", "set": set_name, "source": source,
            "source_id": sub["source_id"][i], "category": sub["category"][i],
            "text": txt, "n_tokens": int(n), "truncated": bool(t), "lang": lang,
            "english": lang == "English", "mod_flagged": mf, "mod_max": mm, "mod_bin": mb,
            "gen": False, "gen_rank": None, "split": None})
    return rows


def check_rows(rows: list[dict]) -> None:
    pids = [r["pid"] for r in rows]
    assert len(set(pids)) == len(pids), "duplicate pid"
    assert pids == sorted(pids), "rows not sorted by pid"
    for r in rows:
        assert tuple(r) == K.PROMPT_FIELDS, f"fields {tuple(r)}"
        assert r["set"] in K.SETS and r["source"] in K.SOURCES


# ------------------------------------------------------------------------------------------------
# Build
# ------------------------------------------------------------------------------------------------

def _percentiles(x) -> dict:
    x = np.asarray(x)
    if len(x) == 0:
        return {}
    q = [0, 1, 5, 10, 25, 50, 75, 90, 95, 99, 100]
    return {f"p{p}": float(v) for p, v in zip(q, np.percentile(x, q))}


class Timer:
    def __init__(self):
        self.t = {}

    def __call__(self, name):
        timer = self

        class _C:
            def __enter__(self_):
                self_.t0 = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] {name} ...", flush=True)

            def __exit__(self_, *a):
                timer.t[name] = round(time.time() - self_.t0, 1)
                print(f"[{time.strftime('%H:%M:%S')}] {name} done in {timer.t[name]} s",
                      flush=True)
        return _C()


def _cache_paths(cache: Path, source: str):
    return (cache / f"{source}.firstturns.parquet", cache / f"{source}.hash.npy",
            cache / f"{source}.sig.npy", cache / f"{source}.cache.json")


def _arrow_schema():
    import pyarrow as pa

    return pa.schema([("source_id", pa.string()), ("category", pa.string()),
                      ("text", pa.string()), ("lang", pa.string()),
                      ("mod_flagged", pa.bool_()), ("mod_max", pa.float64())])


def text_chunks(table, chunk: int = 20_000):
    col = table.column("text")
    for i in range(0, table.num_rows, chunk):
        yield col.slice(i, chunk).to_pylist()


def take_rows(table, idx: np.ndarray) -> dict:
    """Python columns for the selected rows, aligned with idx."""
    import pyarrow as pa

    return table.take(pa.array(np.asarray(idx, dtype=np.int64))).to_pydict()


def load_source(source: str, rec: dict, cache: Path, limit: int | None, need_sig: bool,
                timer: Timer):
    """Extract first turns into a cached parquet (streamed, so memory stays small), exact hashes
    and MinHash signatures (cached .npy). Returns (arrow table, counts, hashes, signatures)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    ft, hp, sp, cj = _cache_paths(cache, source)
    key = {"revision": rec["revision"], "files": {k: v["sha256"] for k, v in rec["files"].items()},
           "limit": limit, "version": 2}
    sig_key = {"perm": K.MINHASH_PERM, "shingle": K.MINHASH_SHINGLE, "seed": K.SEED_SAMPLE,
               "version": 1}
    meta = json.loads(cj.read_text()) if cj.exists() else {}
    if not (meta.get("key") == key and ft.exists() and hp.exists()):
        cache.mkdir(parents=True, exist_ok=True)
        counts: dict = {}
        hashes = []
        with timer(f"extract:{source}"):
            schema = _arrow_schema()
            with pq.ParquetWriter(ft, schema) as w:
                for d in extract_batches(source, rec["paths"], counts, limit):
                    if d["text"]:
                        w.write_table(pa.table(d, schema=schema))
                        hashes.append(exact_hashes(d["text"]))
        h = np.concatenate(hashes) if hashes else np.empty(0, np.uint64)
        np.save(hp, h)
        if sp.exists():
            sp.unlink()
        meta = {"key": key, "counts": counts}
        cj.write_text(json.dumps(meta))
    table = pq.read_table(ft)
    h = np.load(hp)
    assert len(h) == table.num_rows
    sig = None
    if need_sig:
        if sp.exists() and meta.get("sig_key") == sig_key and meta.get("sig_rows") == len(h):
            sig = np.load(sp)
        else:
            with timer(f"minhash:{source}"):
                sig = minhash_parallel(text_chunks(table))
            np.save(sp, sig)
            meta.update(sig_key=sig_key, sig_rows=len(h))
            cj.write_text(json.dumps(meta))
    return table, dict(meta["counts"]), h, sig


def take_unique(table, order: np.ndarray, start: int, n: int, tok, seen: set) -> tuple:
    """Walk `order` from `start`, tokenising and truncating, and keep the first n rows whose
    model input (the truncated text) is not already in `seen` (normalised exact hash; updated in
    place). Two prompts that share their first MAX_PROMPT_TOKENS tokens are one model input.
    Returns (sub, n_tokens, truncated, texts, idx, next_start, n_dropped)."""
    keep_rows: dict = {c: [] for c in COLUMNS}
    ntok, trunc, texts, idx = [], [], [], []
    pos, dropped = start, 0
    while len(idx) < n and pos < len(order):
        step = min(len(order) - pos, max(256, int((n - len(idx)) * 1.02) + 16))
        chunk = order[pos:pos + step]
        pos += step
        sub = take_rows(table, chunk)
        nn, tr, tx = tokenize_and_truncate(tok, sub["text"])
        for i, j in enumerate(chunk.tolist()):
            if len(idx) >= n:
                pos -= len(chunk) - i  # unconsumed rows stay available to the next caller
                break
            hsh = exact_hash(tx[i])
            if hsh in seen:
                dropped += 1
                continue
            seen.add(hsh)
            for c in COLUMNS:
                keep_rows[c].append(sub[c][i])
            ntok.append(nn[i])
            trunc.append(tr[i])
            texts.append(tx[i])
            idx.append(j)
    return keep_rows, ntok, trunc, texts, np.array(idx, dtype=int), pos, dropped


def build(pool_source: str = "lmsys", smoke: bool = False, argv: list[str] | None = None) -> dict:
    import scipy.stats

    timer = Timer()
    t_start = time.time()
    out_dir = K.DATA_DIR / "smoke" if smoke else K.DATA_DIR
    cache = K.RAW_DIR / ("cache_smoke" if smoke else "cache")
    targets = dict(SMOKE_TARGET if smoke else K.N_TARGET)
    n_gen_ref = SMOKE_N_GEN_REF if smoke else K.N_GEN_REF
    limit = SMOKE_ROWS if smoke else None
    max_files = 1 if smoke else None
    meta: dict = {"probe": K.PROBE, "smoke": smoke, "pool_source": pool_source,
                  "command": " ".join(argv if argv is not None else sys.argv),
                  "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "seed_sample": K.SEED_SAMPLE, "targets": targets, "n_gen_ref": n_gen_ref}
    if pool_source == "wildchat48":
        meta["pool_warning"] = (
            "FALLBACK POOL: WildChat-4.8M (LMSYS-Chat-1M was not accessible). Dolci SFT holds 302k "
            "WildChat prompts and DPO ~12%, so after dedup this pool is closer to post-training "
            "and holds what AI2 filtered out; a weaker pool (plan 'Setup'). The public WildChat-4.8M "
            "excludes conversations flagged as toxic, so the moderation bins are nearly all 0.")

    srcs = ["dolci_dpo", "dolci_sft", pool_source, "chatdoctor", "lawse"]
    recs = {}
    with timer("download"):
        for s in srcs:
            recs[s] = download(s, max_files=max_files)
    meta["sources"] = {s: {k: v for k, v in r.items() if k != "paths"} for s, r in recs.items()}

    data = {}
    for s in srcs:
        data[s] = load_source(s, recs[s], cache, limit, need_sig=True, timer=timer)
    counts = {s: dict(data[s][1]) for s in srcs}

    # --- Dolci index (all SFT + DPO first turns) -------------------------------------------------
    dpo_cols, _, dpo_h, dpo_sig = data["dolci_dpo"]
    sft_cols, _, sft_h, sft_sig = data["dolci_sft"]
    with timer("dolci_index"):
        all_h = np.concatenate([dpo_h, sft_h])
        dolci_hset = np.unique(all_h)
        u = first_occurrence_mask(all_h)
        idx_sig = np.concatenate([dpo_sig, sft_sig])[u]
        for s in ("dolci_dpo", "dolci_sft"):  # free the per-source signatures (~1 GB)
            data[s] = data[s][:3] + (None,)
        del dpo_sig, sft_sig
    meta["dolci_index"] = {"n_first_turns": len(all_h), "n_unique_exact": int(u.sum())}

    # --- ID sets: exact dedup, mutually disjoint ---------------------------------------------------
    with timer("sample_id"):
        dpo_keep = np.nonzero(first_occurrence_mask(dpo_h))[0]
        counts["dolci_dpo"]["after_exact_dedup"] = len(dpo_keep)
        perm = rng_for("id").permutation(dpo_keep)
        sft_first = first_occurrence_mask(sft_h)
        sft_ok = sft_first & ~np.isin(sft_h, dpo_h)
        counts["dolci_sft"]["after_exact_dedup"] = int(sft_first.sum())
        counts["dolci_sft"]["after_removing_dpo_exact"] = int(sft_ok.sum())
        perm_sft = rng_for("sft").permutation(np.nonzero(sft_ok)[0])

    # --- pool and planted candidates: dedup against Dolci ------------------------------------------
    cands = {}
    for s in (pool_source, "chatdoctor", "lawse"):
        cols, _, h, sig = data[s]
        with timer(f"dedup:{s}"):
            first = first_occurrence_mask(h)
            not_exact = ~np.isin(h, dolci_hset)
            c = counts[s]
            c["after_exact_dedup_within"] = int(first.sum())
            keep = first & not_exact
            c["removed_exact_dup_of_dolci"] = int((first & ~not_exact).sum())
            ki = np.nonzero(keep)[0]
            near, st = near_dup_of_index(sig[ki], idx_sig)
            c["removed_near_dup_of_dolci"] = int(near.sum())
            c["near_dup_lsh"] = st
            ki = ki[~near]
            c["after_dedup_vs_dolci"] = len(ki)
        cands[s] = ki

    # --- tokeniser ----------------------------------------------------------------------------------
    from emx.formats import load_tokenizer

    tok = load_tokenizer("dpo")
    meta["tokenizer"] = {"model": K.DPO_ID, "revision": K.DPO_REV,
                         "class": type(tok).__name__, "is_fast": bool(tok.is_fast)}

    # --- pool sample ----------------------------------------------------------------------------------
    pool_cols, _, _, pool_sig_all = data[pool_source]
    with timer("sample_pool"):
        ki = cands[pool_source]
        n_pool = targets[K.POOL]
        order = rng_for("pool").permutation(ki)
    with timer("tokenize_pool"):
        pool_sub, *pool_tok, idx_pool, _, drop = take_unique(pool_cols, order, 0, n_pool, tok,
                                                             set())
    counts[pool_source]["dropped_same_truncated_text"] = drop
    if len(idx_pool) < n_pool or (len(idx_pool) < K.POOL_MIN and not smoke):
        meta["pool_flag"] = (f"only {len(idx_pool)} deduped pool prompts (< target {n_pool}"
                             f"{', < POOL_MIN' if len(idx_pool) < K.POOL_MIN else ''}); all used")
    pool_n = np.array(pool_tok[0])
    meta["pool_internal_near_dups"] = {
        "n_items_with_a_near_dup_in_pool": internal_near_dups(pool_sig_all[idx_pool]),
        "note": "pool near-duplicates are kept (templated deployment mass is real)"}

    # --- planted: length-matched to the pool ----------------------------------------------------
    planted = {}
    for set_name, s in ((K.PLANTED_MED, "chatdoctor"), (K.PLANTED_LAW, "lawse")):
        cols = data[s][0]
        ki = cands[s]
        with timer(f"tokenize_planted:{s}"):
            n_c, tr_c, txt_c = tokenize_and_truncate(tok, take_rows(cols, ki)["text"])
            u = first_occurrence_mask(exact_hashes(txt_c))  # same model input -> one
            counts[s]["dropped_same_truncated_text"] = int((~u).sum())
            ki = ki[u]
            n_c = [x for x, k in zip(n_c, u) if k]
            tr_c = [x for x, k in zip(tr_c, u) if k]
            txt_c = [x for x, k in zip(txt_c, u) if k]
        ch, rec = length_matched_sample(np.array(n_c), pool_n, targets[set_name],
                                        rng_for(f"planted:{set_name}"))
        n_sel = np.array(n_c)[ch]
        ks = scipy.stats.ks_2samp(n_sel, pool_n)
        rec["ks_vs_pool"] = {"statistic": float(ks.statistic), "pvalue": float(ks.pvalue),
                             "plan_wants_p_gt": 0.05, "passes": bool(ks.pvalue > 0.05)}
        meta.setdefault("planted_length_match", {})[set_name] = rec
        planted[set_name] = (s, take_rows(cols, ki[ch]), [n_c[i] for i in ch],
                             [tr_c[i] for i in ch], [txt_c[i] for i in ch])

    # --- rows -------------------------------------------------------------------------------------
    rows: list[dict] = []
    with timer("tokenize_id"):
        seen: set = set()  # ID sets mutually disjoint by model input (truncated text)
        pos = {"dolci_dpo": 0, "dolci_sft": 0}
        for set_name, s, cols, order in ((K.ID_REF, "dolci_dpo", dpo_cols, perm),
                                         (K.ID_HELDOUT, "dolci_dpo", dpo_cols, perm),
                                         (K.SFT_REF, "dolci_sft", sft_cols, perm_sft)):
            sub, n, tr, tx, _, pos[s], drop = take_unique(cols, order, pos[s], targets[set_name],
                                                         tok, seen)
            counts[s][f"{set_name}_dropped_same_truncated_text"] = drop
            rows += make_rows(set_name, s, sub, n, tr, tx, pool=False)
    rows += make_rows(K.POOL, pool_source, pool_sub, *pool_tok, pool=True)
    for set_name, (s, sub, n, tr, tx) in planted.items():
        rows += make_rows(set_name, s, sub, n, tr, tx, pool=False)
    assign_gen_split(rows, n_gen_ref)
    rows.sort(key=lambda r: r["pid"])
    check_rows(rows)
    # checks: ID sets mutually disjoint, and no duplicate model input within any set
    hs = {s: [exact_hash(r["text"]) for r in rows if r["set"] == s] for s in K.SETS}
    meta["id_sets_disjoint_exact"] = not (set(hs[K.ID_REF]) & set(hs[K.ID_HELDOUT])
                                          or set(hs[K.ID_REF]) & set(hs[K.SFT_REF])
                                          or set(hs[K.ID_HELDOUT]) & set(hs[K.SFT_REF]))
    meta["exact_dup_texts_within_set"] = {s: len(v) - len(set(v)) for s, v in hs.items()}
    assert meta["id_sets_disjoint_exact"] and not any(meta["exact_dup_texts_within_set"].values())

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / K.PROMPTS_PATH.name
    with timer("write"), out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # --- meta ---------------------------------------------------------------------------------------
    by = {s: [r for r in rows if r["set"] in (s,)] for s in K.SETS}
    meta["counts"] = counts
    meta["set_sizes"] = {s: len(v) for s, v in by.items()}
    meta["truncated"] = {s: int(sum(r["truncated"] for r in v)) for s, v in by.items()}
    meta["n_tokens_percentiles"] = {s: _percentiles([r["n_tokens"] for r in v])
                                    for s, v in by.items()}
    meta["english_share"] = {s: (float(np.mean([r["english"] for r in v])) if v else None)
                             for s, v in by.items()}
    langs: dict[str, int] = {}
    for r in by[K.POOL]:
        langs[r["lang"]] = langs.get(r["lang"], 0) + 1
    meta["pool_language_top15"] = dict(sorted(langs.items(), key=lambda kv: -kv[1])[:15])
    mb: dict[str, int] = {}
    for r in by[K.POOL]:
        mb[str(r["mod_bin"])] = mb.get(str(r["mod_bin"]), 0) + 1
    meta["pool_mod_bin_counts"] = mb
    meta["pool_mod_max_missing_but_flagged"] = int(sum(
        1 for r in by[K.POOL] if r["mod_flagged"] and r["mod_max"] is None))
    meta["mod_bin_rule"] = (f"0 = not flagged; 1 = flagged and max category score < {K.MOD_HIGH}; "
                            "2 = flagged and >= that (or flagged with no category scores); "
                            "None = no moderation record")
    meta["english_heuristic"] = ENGLISH_HEURISTIC
    meta["category_rule"] = ("dolci_dpo: prompt_id.rsplit('_', 1)[0][:40]; dolci_sft: "
                             "source_dataset; planted: 'medical' / 'legal'; pool: None")
    meta["dedup"] = {"normalise": "lowercase, collapse whitespace, strip",
                     "exact": "sha1 of the normalised text (first 8 bytes)",
                     "minhash": {"shingle_words": K.MINHASH_SHINGLE, "perm": K.MINHASH_PERM,
                                 "hash": "xxh64 words, 5-gram mix, (a*x+b) mod 2^31-1",
                                 "bands": N_BANDS, "rows": N_ROWS,
                                 "p_candidate_at_J0.8": candidate_prob(0.8),
                                 "verify": f"signature-estimated Jaccard >= {K.MINHASH_JACCARD}"}}
    meta["gen_counts"] = {s: int(sum(r["gen"] for r in v)) for s, v in by.items()}
    meta["split_counts"] = {s: {"dev": sum(r["split"] == "dev" for r in v),
                                "test": sum(r["split"] == "test" for r in v)}
                            for s, v in by.items() if s in K.SCORED_SETS}
    meta["prompts_path"] = str(out.relative_to(K.REPO_ROOT))
    meta["prompts_sha256"] = _sha256_file(out, cache=False)
    meta["wall_clock_s"] = dict(timer.t, total=round(time.time() - t_start, 1))
    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_dir / K.PROMPTS_META_PATH.name).write_text(json.dumps(meta, indent=1, default=str))
    print(f"[build] wrote {out} ({len(rows)} rows) sha256 {meta['prompts_sha256']}", flush=True)
    return meta


def prepare(pool_source: str = "lmsys") -> dict:
    """Download, extract and sign every source; the pool source is skipped (reported) if gated."""
    timer = Timer()
    status = {}
    for s in ("dolci_dpo", "dolci_sft", "chatdoctor", "lawse", pool_source):
        try:
            rec = download(s)
        except GatedSourceError as e:
            status[s] = f"BLOCKED: {e}"
            print(f"[prepare] {s} BLOCKED: {e}", file=sys.stderr, flush=True)
            continue
        _, counts, h, _sig = load_source(s, rec, K.RAW_DIR / "cache", None, need_sig=True,
                                        timer=timer)
        status[s] = {"rows": len(h), "counts": counts}
    print(json.dumps({"status": status, "wall_clock_s": timer.t}, indent=1, default=str))
    return status


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="p42.data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--pool-source", choices=("lmsys", "wildchat48"), default="lmsys")
    b.add_argument("--smoke", action="store_true")
    pr = sub.add_parser("prepare", help="download + extract + MinHash every non-pool source "
                         "(and the pool source if accessible), so `build` only samples")
    pr.add_argument("--pool-source", choices=("lmsys", "wildchat48"), default="lmsys")
    d = sub.add_parser("download")
    d.add_argument("--source", nargs="+", default=["dolci_dpo", "dolci_sft", "lmsys",
                                                   "chatdoctor", "lawse"])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            build(a.pool_source, a.smoke, argv=["python -m p42.data"] + argv)
        elif a.cmd == "prepare":
            prepare(a.pool_source)
        else:
            for s in a.source:
                try:
                    download(s)
                except GatedSourceError as e:
                    print(f"[download] BLOCKED {s}: {e}", file=sys.stderr, flush=True)
    except GatedSourceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
