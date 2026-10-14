"""P45 data builds (plan.md "Setup"): the legal-matched planted set and the Dolma 3 chunks for the
"pre" SAE arm. Both are free (HF downloads only) and run on the laptop.

    cd probes/P45-ood-gap-metrics
    uv run --with pyarrow --with xxhash --with zstandard python -m p45.data law2
    uv run --with pyarrow --with xxhash --with zstandard python -m p45.data dolma
    uv run --with pyarrow --with xxhash --with zstandard python -m p45.data check

law2 -> data/P45/planted_law2.jsonl (P42K.PROMPT_FIELDS rows, set planted_law2, source lawse):
  Law-StackExchange questions (P42's pinned copy), minus every question in P42's planted_law set
  (source_id, plus exact / MinHash near-duplicate text) and every question whose title or
  title + body is an exact or MinHash near-duplicate (Jaccard >= 0.8, P42's settings) of any Dolci
  SFT/DPO first turn (P42's cached hashes and signatures). Each question contributes at most one
  variant (title-only or title + body, p42.data.lawse_prompt). Rows are sampled to the pool's
  length-decile targets (p42.data.bin_targets over P42's pool n_tokens); within a decile each slot
  draws a pool length from that decile and takes the unused candidate nearest to it (seeded, ties
  random), so the within-decile shape follows the pool too. Bins are filled in order of scarcity.

dolma -> data/P45/dolma_chunks.jsonl (K.DOLMA_FIELDS): K.DOLMA_N_SHARDS seeded shards of
  allenai/dolma3_mix-150B-1025 @ K.DOLMA_REV, allocated to the README's source families by token
  share (>= 1 shard for every family above 0.5%), each shard given a token budget of
  family share / family shards. Chunk lengths are drawn from id_ref's n_tokens (capped at 2,048):
  for each draw L, a seeded random unused document with >= L tokens gives one span at a random
  offset, decoded, stripped and re-tokenised (extended by up to a few tokens so the re-tokenised
  length equals L where possible). Seeded 90/10 train/heldout split by chunk.

Downloads are untrusted data: parsed as JSON only. Seeds: p42.data.rng_for (P42K.SEED_SAMPLE with
a per-step tag; every tag starts with "p45:").
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # AGENTS.md: keep xet off by default
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from p45 import contracts as K  # puts P42's package on sys.path

# isort: split
from p42 import data as D42

P42K = K.P42K
N_WORKERS = 4
RAW_DIR = K.DATA_DIR / "raw"
DOLMA_RAW = RAW_DIR / "dolma"
LAWSE_JSON = P42K.RAW_DIR / "lawse" / "law-stackexchange-questions-answers.json"
TITLE, FULL = 0, 1
VARIANT_NAMES = ("title", "title+body")

# README token counts (billions) of the pinned mix, by top-level source family (dir prefix).
DOLMA_FAMILY_TOKENS_B = {
    "common_crawl": 121.0,          # Common Crawl
    "olmocr_science_pdfs": 19.9,    # olmOCR Science PDFs
    "stack_edu": 11.1,              # Stack-Edu (Rebalanced)
    "rpj": 1.29,                    # arXiv (rpj-proofpile-arxiv)
    "finemath": 4.10,               # FineMath 3+
    "dolma1_7": 0.0646,             # Wikipedia & Wikibooks (dolma1_7-wiki-en)
}
DOLMA_MIN_FAMILY_SHARE = 0.005
DOLMA_TOKEN_MARGIN = 1.04  # total chunk-token budget = min train / (1 - heldout frac) x margin
DOLMA_DOC_MAX_CHARS = 40_000  # documents are tokenised from their first 40k characters
DOLMA_MAX_EXTEND = 6  # extra tokens tried so the re-tokenised chunk has the drawn length
KS_P_MIN = 0.05
DECILE_TOL = 0.10


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256(path: Path) -> str:
    return D42._sha256_file(path, cache=False)


def _rel(p: Path) -> str:
    try:
        return str(Path(p).relative_to(K.REPO_ROOT))
    except ValueError:
        return str(p)


def _update_meta(key: str, rec: dict) -> None:
    meta = json.loads(K.DATA_META.read_text()) if K.DATA_META.exists() else {"probe": K.PROBE}
    meta[key] = rec
    K.DATA_META.parent.mkdir(parents=True, exist_ok=True)
    K.DATA_META.write_text(json.dumps(meta, indent=1, default=str, ensure_ascii=False))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _ks(a, b) -> dict:
    import scipy.stats

    ks = scipy.stats.ks_2samp(np.asarray(a), np.asarray(b))
    return {"statistic": float(ks.statistic), "pvalue": float(ks.pvalue),
            "plan_wants_p_gt": KS_P_MIN, "passes": bool(ks.pvalue > KS_P_MIN)}


def split_halves(n: int, rng: np.random.Generator, frac_first: float) -> np.ndarray:
    """Bool mask, True for exactly round(frac_first * n) seeded items."""
    k = round(frac_first * n)
    m = np.zeros(n, dtype=bool)
    m[rng.permutation(n)[:k]] = True
    return m


# ------------------------------------------------------------------------------------------------
# law2: dedup and variant assignment
# ------------------------------------------------------------------------------------------------

def dup_mask(q_hash: np.ndarray, q_sig: np.ndarray, index_hset: np.ndarray, index_sig: np.ndarray
             ) -> tuple[np.ndarray, dict]:
    """True where a query is an exact (normalised-text hash in index_hset) or MinHash near-duplicate
    (estimated Jaccard >= P42K.MINHASH_JACCARD, p42.data.near_dup_of_index) of the index."""
    exact = np.isin(q_hash, index_hset)
    near = np.zeros(len(q_hash), dtype=bool)
    st = {"pairs_checked": 0, "capped_lookups": 0}
    todo = np.nonzero(~exact)[0]
    if len(todo):
        found, st = D42.near_dup_of_index(q_sig[todo], index_sig)
        near[todo[found]] = True
    return exact | near, {"exact": int(exact.sum()), "near_only": int(near.sum()), "lsh": st}


def assign_variants(len_title: np.ndarray, len_full: np.ndarray, ok_title: np.ndarray,
                    ok_full: np.ndarray, pool_n: np.ndarray, total: int, rng: np.random.Generator,
                    text_hash: np.ndarray | None = None, n_bins: int = P42K.N_LEN_BINS
                    ) -> tuple[list[tuple[int, int]], dict]:
    """Choose `total` (question index, variant) pairs, at most one per question (and, with
    `text_hash` of shape (n_q, 2), no two with the same text), matched to the pool's length
    deciles. Bins are filled scarcest first (distinct eligible questions / target); each slot draws
    a pool length from its decile and takes the nearest unused candidate in the decile (ties
    random). A decile short of candidates takes the nearest unused candidates from any decile."""
    edges = D42.length_bin_edges(pool_n, n_bins)
    pool_bins = D42.length_bin(pool_n, edges)
    targets = D42.bin_targets(pool_bins, total, n_bins)
    nq = len(len_title)
    cq = np.concatenate([np.arange(nq), np.arange(nq)])
    cv = np.concatenate([np.full(nq, TITLE), np.full(nq, FULL)])
    cn = np.concatenate([len_title, len_full]).astype(np.int64)
    ok = np.concatenate([ok_title, ok_full]).astype(bool)
    cq, cv, cn = cq[ok], cv[ok], cn[ok]
    ch = None if text_hash is None else text_hash[cq, cv]
    shuf = rng.permutation(len(cq))  # random tie order
    cq, cv, cn = cq[shuf], cv[shuf], cn[shuf]
    ch = None if ch is None else ch[shuf]
    cb = D42.length_bin(cn, edges)
    used_q = np.zeros(nq, dtype=bool)
    used_h: set = set()
    free = np.ones(len(cq), dtype=bool)
    avail_q = [len(np.unique(cq[cb == b])) for b in range(n_bins)]
    order = sorted(range(n_bins), key=lambda b: (avail_q[b] / max(int(targets[b]), 1), b))
    chosen, short, fallback = [], {}, 0

    def take(i: int) -> None:
        chosen.append((int(cq[i]), int(cv[i])))
        q = cq[i]
        used_q[q] = True
        free[cq == q] = False  # the question's other variant is gone too
        if ch is not None:
            used_h.add(int(ch[i]))
            free[ch == ch[i]] = False

    for b in order:
        need = int(targets[b])
        pb = pool_n[pool_bins == b]
        draws = rng.choice(pb, size=need, replace=len(pb) < need) if need else []
        miss = 0
        for L in draws:
            cand = np.nonzero(free & (cb == b))[0]
            if len(cand) == 0:
                miss += 1
                continue
            take(int(cand[np.argmin(np.abs(cn[cand] - L))]))
        if miss:
            short[b] = miss
            for L in rng.choice(pb, size=miss, replace=True):
                cand = np.nonzero(free)[0]
                if len(cand) == 0:
                    break
                take(int(cand[np.argmin(np.abs(cn[cand] - L))]))
                fallback += 1
    sel_n = np.array([len_title[q] if v == TITLE else len_full[q] for q, v in chosen], dtype=int)
    sel_b = D42.length_bin(sel_n, edges) if len(sel_n) else np.array([], dtype=int)
    by_bin = np.bincount(sel_b, minlength=n_bins) if len(sel_n) else np.zeros(n_bins, int)
    var_bin = {VARIANT_NAMES[v]: np.bincount(sel_b[np.array([c[1] for c in chosen]) == v],
                                             minlength=n_bins).tolist() if len(sel_n) else []
               for v in (TITLE, FULL)}
    cand_bin = {VARIANT_NAMES[v]: np.bincount(cb[cv == v], minlength=n_bins).tolist()
                for v in (TITLE, FULL)}
    rec = {"edges": edges.tolist(), "targets": targets.tolist(), "achieved": by_bin.tolist(),
           "fill_order": order, "distinct_questions_by_bin": avail_q,
           "candidates_by_bin": cand_bin, "chosen_variant_by_bin": var_bin,
           "shortfall_by_bin": short, "filled_from_other_bins": fallback,
           "rule": "scarcest decile first; each slot draws a pool length from its decile and takes "
                   "the nearest unused candidate (ties random); one variant per question"}
    return chosen, rec


def decile_check(targets, achieved, tol: float = DECILE_TOL) -> dict:
    t, a = np.asarray(targets, float), np.asarray(achieved, float)
    dev = np.abs(a - t) / np.maximum(t, 1)
    return {"tolerance": tol, "max_rel_dev": float(dev.max()) if len(dev) else 0.0,
            "bins_failing": [int(b) for b in np.nonzero(dev > tol)[0]],
            "passes": bool((dev <= tol).all())}


def _load_p42_prompts() -> tuple[np.ndarray, list[dict], str]:
    """(pool n_tokens, P42 planted_law rows, sha256 of prompts.jsonl)."""
    sha = _sha256(K.P42_PROMPTS)
    if sha != K.P42_PROMPTS_SHA:
        raise RuntimeError(f"{K.P42_PROMPTS}: sha256 {sha} != pinned {K.P42_PROMPTS_SHA}")
    pool_n, law = [], []
    with K.P42_PROMPTS.open() as f:
        for line in f:
            r = json.loads(line)
            if r["set"] == P42K.POOL:
                pool_n.append(r["n_tokens"])
            elif r["set"] == P42K.PLANTED_LAW:
                law.append(r)
    return np.array(pool_n), law, sha


def _dolci_index() -> tuple[np.ndarray, np.ndarray, dict]:
    """P42's Dolci index: unique exact hashes and the signatures of first-occurrence texts."""
    hs, sigs, rec = [], [], {}
    for s in ("dolci_dpo", "dolci_sft"):
        _, hp, sp, cj = D42._cache_paths(K.P42_CACHE, s)
        meta = json.loads(cj.read_text())
        h = np.load(hp)
        sig = np.load(sp, mmap_mode="r")
        if meta.get("sig_rows") != len(h) or len(sig) != len(h):
            raise RuntimeError(f"P42 cache for {s}: signature rows do not match hashes")
        hs.append(h)
        sigs.append(sig)
        rec[s] = {"rows": len(h), "revision": meta["key"]["revision"],
                  "sig_key": meta.get("sig_key")}
    all_h = np.concatenate(hs)
    u = D42.first_occurrence_mask(all_h)
    off = len(hs[0])
    idx_sig = np.concatenate([np.asarray(sigs[0][u[:off]]), np.asarray(sigs[1][u[off:]])])
    rec["n_first_turns"] = len(all_h)
    rec["n_unique_exact"] = int(u.sum())
    return np.unique(all_h), idx_sig, rec


def build_law2(argv: list[str] | None = None) -> dict:
    t0 = time.time()
    timer = D42.Timer()
    meta: dict = {"command": " ".join(argv or sys.argv), "started_utc": _now(),
                  "seed": {"base": P42K.SEED_SAMPLE, "rng": "p42.data.rng_for(tag)",
                           "tags": ["p45:law2:assign", "p45:law2:split", "p45:law2:examples"]}}
    with timer("p42_prompts"):
        pool_n, p42_law, p42_sha = _load_p42_prompts()
    meta["p42_prompts_sha256"] = p42_sha
    meta["pool_n"] = len(pool_n)

    lawse_key = json.loads(D42._cache_paths(K.P42_CACHE, "lawse")[3].read_text())["key"]
    want = lawse_key["files"][LAWSE_JSON.name]
    got = _sha256(LAWSE_JSON)
    if got != want:
        raise RuntimeError(f"{LAWSE_JSON}: sha256 {got} != P42's {want}")
    meta["lawse"] = {"repo": P42K.SOURCES["lawse"][0], "revision": lawse_key["revision"],
                     "file": LAWSE_JSON.name, "sha256": got}
    raw = json.loads(LAWSE_JSON.read_text())
    qid = np.array([str(r.get("question_id", i)) for i, r in enumerate(raw)])
    titles = [D42.lawse_prompt(r.get("question_title"), None) for r in raw]
    fulls = [D42.lawse_prompt(r.get("question_title"), r.get("question_body")) for r in raw]
    nq = len(raw)
    counts: dict = {"questions": nq, "distinct_question_ids": len(set(qid.tolist()))}

    with timer("minhash"):
        sig_t = D42.minhash_parallel([titles[i:i + 4000] for i in range(0, nq, 4000)])
        sig_f = D42.minhash_parallel([fulls[i:i + 4000] for i in range(0, nq, 4000)])
        h_t, h_f = D42.exact_hashes(titles), D42.exact_hashes(fulls)

    # --- exclusions -----------------------------------------------------------------------------
    excl = np.zeros(nq, dtype=bool)
    p42_ids = {r["source_id"] for r in p42_law}
    in_p42 = np.isin(qid, list(p42_ids))
    counts["excluded_p42_planted_law_by_id"] = int(in_p42.sum())
    excl |= in_p42
    p42_texts = [r["text"] for r in p42_law]
    p42_h = np.unique(D42.exact_hashes(p42_texts))
    p42_sig = D42.minhash_signatures(p42_texts)
    dt, _ = dup_mask(h_t, sig_t, p42_h, p42_sig)
    df, _ = dup_mask(h_f, sig_f, p42_h, p42_sig)
    counts["excluded_p42_planted_law_by_text_only"] = int(((dt | df) & ~excl).sum())
    excl |= dt | df
    with timer("dolci_index"):
        dolci_hset, dolci_sig, meta["dolci_index"] = _dolci_index()
    with timer("dedup_vs_dolci"):
        dup_t, st_t = dup_mask(h_t, sig_t, dolci_hset, dolci_sig)
        dup_f, st_f = dup_mask(h_f, sig_f, dolci_hset, dolci_sig)
    del dolci_sig
    counts["dup_of_dolci"] = {"title": st_t, "title+body": st_f,
                              "questions_with_either_dup": int((dup_t | dup_f).sum())}
    counts["excluded_dup_of_dolci_only"] = int(((dup_t | dup_f) & ~excl).sum())
    excl |= dup_t | dup_f
    empty_t = np.array([not D42.nonempty(t) for t in titles])
    counts["eligible_questions"] = int((~excl).sum())
    meta["counts"] = counts

    # --- tokenise and assign -------------------------------------------------------------------------
    from emx.formats import load_tokenizer

    tok = load_tokenizer("dpo")
    meta["tokenizer"] = {"model": P42K.DPO_ID, "revision": P42K.DPO_REV,
                         "class": type(tok).__name__, "is_fast": bool(tok.is_fast)}
    with timer("tokenize"):
        n_t = np.array(D42.tokenize_and_truncate(tok, titles)[0])
        n_f = np.array(D42.tokenize_and_truncate(tok, fulls)[0])
    ok_t = ~excl & ~empty_t
    ok_f = ~excl & (h_f != h_t)  # a body-less question has one variant only
    chosen, rec = assign_variants(n_t, n_f, ok_t, ok_f, pool_n, K.N_LAW2,
                                  D42.rng_for("p45:law2:assign"),
                                  text_hash=np.stack([h_t, h_f], axis=1))
    assert len({q for q, _ in chosen}) == len(chosen), "a question was used twice"
    sel_n = np.array([n_t[q] if v == TITLE else n_f[q] for q, v in chosen])
    rec["ks_vs_pool"] = _ks(sel_n, pool_n)
    rec["decile_check"] = decile_check(rec["targets"], rec["achieved"])
    rec["n_chosen"] = len(chosen)

    # --- rows -------------------------------------------------------------------------------------
    chosen.sort(key=lambda c: (int(qid[c[0]]) if qid[c[0]].isdigit() else 0, qid[c[0]]))
    texts = [titles[q] if v == TITLE else fulls[q] for q, v in chosen]
    n, tr, tx = D42.tokenize_and_truncate(tok, texts)
    sub = {"source_id": [qid[q] for q, _ in chosen], "category": ["legal"] * len(chosen),
           "text": texts, "lang": [None] * len(chosen), "mod_flagged": [None] * len(chosen),
           "mod_max": [None] * len(chosen)}
    rows = D42.make_rows(K.PLANTED_LAW2, "lawse", sub, n, tr, tx, pool=False)
    dev = split_halves(len(rows), D42.rng_for("p45:law2:split"), 0.5)  # rows ordered by qid
    for r, d, (_, v) in zip(rows, dev, chosen):
        r["split"] = "dev" if d else "test"
        r["gen"], r["gen_rank"] = False, None
    check_law2_rows(rows)

    # --- record -----------------------------------------------------------------------------------
    edges = np.array(rec["edges"])
    bins = D42.length_bin(np.array([r["n_tokens"] for r in rows]), edges)
    short = [i for i, b in enumerate(bins) if b <= 2]
    ex_rng = D42.rng_for("p45:law2:examples")
    pick = sorted(ex_rng.choice(short, size=min(10, len(short)), replace=False).tolist())
    meta["short_examples_bins_0_2"] = [
        {"pid": rows[i]["pid"], "source_id": rows[i]["source_id"], "bin": int(bins[i]),
         "n_tokens": rows[i]["n_tokens"], "variant": VARIANT_NAMES[chosen[i][1]],
         "text": rows[i]["text"]} for i in pick]
    meta["length_match"] = rec
    meta["variant_counts"] = {VARIANT_NAMES[v]: sum(c[1] == v for c in chosen)
                              for v in (TITLE, FULL)}
    meta["n_rows"] = len(rows)
    meta["truncated"] = int(sum(r["truncated"] for r in rows))
    meta["english_share"] = float(np.mean([r["english"] for r in rows]))
    meta["n_tokens_percentiles"] = D42._percentiles([r["n_tokens"] for r in rows])
    meta["split_counts"] = {s: sum(r["split"] == s for r in rows) for s in ("dev", "test")}
    meta["preflight"] = {
        "ks_p_gt_0.05": rec["ks_vs_pool"]["passes"],
        "deciles_within_10pct": rec["decile_check"]["passes"],
        "short_items_read": "pending: the main session reads short_examples_bins_0_2",
        "passes_automatic": bool(rec["ks_vs_pool"]["passes"] and rec["decile_check"]["passes"])}
    _write_jsonl(K.LAW2_PATH, rows)
    meta["path"] = _rel(K.LAW2_PATH)
    meta["sha256"] = _sha256(K.LAW2_PATH)
    meta["wall_clock_s"] = dict(timer.t, total=round(time.time() - t0, 1))
    meta["finished_utc"] = _now()
    _update_meta("law2", meta)
    print(f"[law2] wrote {K.LAW2_PATH} ({len(rows)} rows) sha256 {meta['sha256']}; KS p "
          f"{rec['ks_vs_pool']['pvalue']:.3g}; deciles {rec['achieved']} vs {rec['targets']}; "
          f"preflight {meta['preflight']}", flush=True)
    return meta


def check_law2_rows(rows: list[dict]) -> None:
    pids = [r["pid"] for r in rows]
    assert len(set(pids)) == len(pids) and pids == sorted(pids)
    assert len({r["source_id"] for r in rows}) == len(rows), "duplicate question id"
    assert len({D42.exact_hash(r["text"]) for r in rows}) == len(rows), "duplicate text"
    for r in rows:
        assert tuple(r) == P42K.PROMPT_FIELDS, f"fields {tuple(r)}"
        assert r["set"] == K.PLANTED_LAW2 and r["source"] == "lawse"
        assert r["split"] in ("dev", "test")


# ------------------------------------------------------------------------------------------------
# dolma: shards and chunks
# ------------------------------------------------------------------------------------------------

def family_of(source_dir: str) -> str:
    return source_dir.split("-")[0]


def family_shares(tokens: dict = DOLMA_FAMILY_TOKENS_B) -> dict:
    tot = sum(tokens.values())
    return {f: v / tot for f, v in tokens.items()}


def allocate_shards(shares: dict, n: int = K.DOLMA_N_SHARDS,
                    min_share: float = DOLMA_MIN_FAMILY_SHARE) -> dict:
    """Shards per family: families with share >= min_share get >= 1; the rest of the n shards
    follow the renormalised shares (floor, then largest remainder; trimmed from the largest
    excess while keeping >= 1)."""
    fams = [f for f, s in shares.items() if s >= min_share]
    tot = sum(shares[f] for f in fams)
    raw = {f: n * shares[f] / tot for f in fams}
    alloc = {f: max(1, int(np.floor(raw[f]))) for f in fams}
    while sum(alloc.values()) < n:
        f = max(fams, key=lambda f: (raw[f] - alloc[f], f))
        alloc[f] += 1
    while sum(alloc.values()) > n:
        f = max((f for f in fams if alloc[f] > 1), key=lambda f: (alloc[f] - raw[f], f))
        alloc[f] -= 1
    return alloc


def choose_shards(files: list[str], alloc: dict, rng: np.random.Generator) -> list[str]:
    """Seeded uniform choice without replacement of alloc[f] files per family, sorted."""
    by: dict[str, list[str]] = {}
    for f in sorted(files):
        if f.startswith("data/") and f.endswith(".jsonl.zst"):
            by.setdefault(family_of(f.split("/")[1]), []).append(f)
    out = []
    for fam in sorted(alloc):
        pool = by.get(fam, [])
        if len(pool) < alloc[fam]:
            raise RuntimeError(f"family {fam}: {len(pool)} files < {alloc[fam]} wanted")
        out += [pool[i] for i in sorted(rng.choice(len(pool), size=alloc[fam], replace=False))]
    return sorted(out)


def shard_budgets(shards: list[str], shares: dict, total_tokens: int) -> dict:
    """Token budget per shard: total x (renormalised family share) / (family's shard count)."""
    fams = [family_of(s.split("/")[1]) for s in shards]
    used = sorted(set(fams))
    tot = sum(shares[f] for f in used)
    cnt = {f: fams.count(f) for f in used}
    return {s: int(np.ceil(total_tokens * shares[f] / tot / cnt[f])) for s, f in zip(shards, fams)}


def redistribute(budgets: dict, got: dict, exhausted: set) -> tuple[dict, int]:
    """Move each exhausted shard's unmet budget to the live shards of its family (equal split).
    Exhausted shards keep what they produced. Returns (new budgets, tokens moved)."""
    new = dict(budgets)
    moved = 0
    fam = {s: family_of(s.split("/")[1]) for s in budgets}
    for f in sorted(set(fam.values())):
        dead = [s for s in budgets if fam[s] == f and s in exhausted]
        live = [s for s in budgets if fam[s] == f and s not in exhausted]
        deficit = sum(max(0, budgets[s] - got[s]) for s in dead)
        for s in dead:
            new[s] = got[s]
        if deficit and live:
            for s in live:
                new[s] += int(np.ceil(deficit / len(live)))
            moved += deficit
    return new, moved


def make_chunks(docs, lengths: np.ndarray, budget: int, rng: np.random.Generator, encode, decode,
                max_extend: int = DOLMA_MAX_EXTEND, batch: int = 32,
                max_tokens: int = P42K.MAX_PROMPT_TOKENS) -> tuple[list[dict], dict]:
    """Cut chunks until their re-tokenised lengths sum to >= budget.

    docs: iterable of document texts (already in seeded random order). lengths: the empirical
    length distribution to draw from. encode(list[str]) -> list[list[int]]; decode(ids) -> str.
    For each draw L, the first buffered unused document with >= L tokens gives one span at a random
    offset; the span is decoded, stripped and re-tokenised, extended by up to max_extend tokens
    until the re-tokenised length equals L (else the closest length is kept); a re-tokenised
    length above max_tokens is never kept.
    Returns ([{text, n_tokens, target}], stats)."""
    it = iter(docs)
    buf: list[list[int]] = []
    out: list[dict] = []
    st = {"docs_tokenised": 0, "docs_used": 0, "draws": 0, "exact_length": 0, "empty_skipped": 0,
          "docs_exhausted": False}
    got = 0
    while got < budget:
        L = int(rng.choice(lengths))
        st["draws"] += 1
        j = next((i for i, d in enumerate(buf) if len(d) >= L), None)
        while j is None:
            new = []
            for t in it:
                new.append(t)
                if len(new) >= batch:
                    break
            if not new:
                st["docs_exhausted"] = True
                return out, st
            st["docs_tokenised"] += len(new)
            start = len(buf)
            buf += encode(new)
            j = next((i for i in range(start, len(buf)) if len(buf[i]) >= L), None)
        ids = buf.pop(j)
        st["docs_used"] += 1
        s0 = int(rng.integers(0, len(ids) - L + 1))
        best = None
        for e in range(max_extend + 1):
            if s0 + L + e > len(ids):
                break
            text = decode(ids[s0:s0 + L + e]).strip()
            n = len(encode([text])[0]) if text else 0
            if n > max_tokens:
                break
            if n and (best is None or abs(n - L) < abs(best[1] - L)):
                best = (text, n)
            if n >= L:
                break
        if best is None:
            st["empty_skipped"] += 1
            continue
        st["exact_length"] += int(best[1] == L)
        out.append({"text": best[0], "n_tokens": int(best[1]), "target": L})
        got += best[1]
    return out, st


_TOK = None


def _worker_tok():
    global _TOK
    if _TOK is None:
        from emx.formats import load_tokenizer

        _TOK = load_tokenizer("dpo")
    return _TOK


def _read_docs(path: Path) -> list[str]:
    """Document texts of one .jsonl.zst shard (untrusted: JSON only, `text` must be a str)."""
    import io

    import zstandard

    out = []
    with path.open("rb") as fh, zstandard.ZstdDecompressor().stream_reader(fh) as r:
        for line in io.TextIOWrapper(r, encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("text") if isinstance(d, dict) else None
            if isinstance(t, str) and t.strip():
                out.append(t)
    return out


def _shard_worker(args) -> tuple[str, list[dict], dict]:
    shard, local, budget, lengths = args
    tok = _worker_tok()
    docs = _read_docs(Path(local))
    rng = D42.rng_for(f"p45:dolma:chunks:{shard}")
    order = rng.permutation(len(docs))

    def encode(texts):
        return tok([t[:DOLMA_DOC_MAX_CHARS] for t in texts], add_special_tokens=False)["input_ids"]

    chunks, st = make_chunks((docs[i] for i in order), lengths, budget, rng, encode, tok.decode)
    st["docs_in_shard"] = len(docs)
    return shard, chunks, st


def _download_dolma(shards: list[str], revision: str) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    from huggingface_hub import hf_hub_download

    DOLMA_RAW.mkdir(parents=True, exist_ok=True)

    def one(fn):
        p = Path(hf_hub_download(K.DOLMA_REPO, fn, repo_type="dataset", revision=revision,
                                 local_dir=DOLMA_RAW))
        print(f"[dolma] downloaded {fn} {p.stat().st_size / 1e6:.0f} MB", flush=True)
        return fn, {"local": str(p), "size": p.stat().st_size, "sha256": _sha256(p)}

    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        return dict(ex.map(one, shards))


def build_dolma(argv: list[str] | None = None) -> dict:
    t0 = time.time()
    timer = D42.Timer()
    D42._load_env()
    from huggingface_hub import HfApi

    meta: dict = {"command": " ".join(argv or sys.argv), "started_utc": _now(),
                  "repo": K.DOLMA_REPO, "revision": K.DOLMA_REV,
                  "seed": {"base": P42K.SEED_SAMPLE, "rng": "p42.data.rng_for(tag)",
                           "tags": ["p45:dolma:shards", "p45:dolma:chunks:<shard>",
                                    "p45:dolma:split"]}}
    sha, listing = D42.resolve_revision(HfApi(), K.DOLMA_REPO, K.DOLMA_REV)
    meta["n_files_in_repo"] = len(listing)
    shares = family_shares()
    alloc = allocate_shards(shares)
    shards = choose_shards(listing, alloc, D42.rng_for("p45:dolma:shards"))
    meta["family_token_share_readme"] = shares
    meta["family_map"] = {"rpj": "arXiv (rpj-proofpile-arxiv)",
                          "dolma1_7": "Wikipedia & Wikibooks (dolma1_7-wiki-en)"}
    meta["shards_per_family"] = alloc
    meta["excluded_families"] = [f for f in shares if f not in alloc]
    with timer("download"):
        files = _download_dolma(shards, sha)
    meta["shards"] = {s: {k: v for k, v in files[s].items() if k != "local"} for s in shards}

    # id_ref content-token lengths (truncated prompts count as MAX_PROMPT_TOKENS)
    id_n = []
    with K.P42_PROMPTS.open() as f:
        for line in f:
            r = json.loads(line)
            if r["set"] == P42K.ID_REF:
                id_n.append(min(r["n_tokens"], P42K.MAX_PROMPT_TOKENS))
    id_n = np.array(id_n)
    meta["id_ref_n"] = len(id_n)

    from concurrent.futures import ProcessPoolExecutor

    total = int(np.ceil(K.DOLMA_MIN_TRAIN_TOKENS / (1 - K.DOLMA_HELDOUT_FRAC)
                        * DOLMA_TOKEN_MARGIN))
    budgets = shard_budgets(shards, shares, total)
    history = []
    for attempt in range(5):  # a shard's chunks are a seeded prefix: a bigger budget extends it
        with timer(f"chunk:attempt{attempt}"), ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
            res = list(ex.map(_shard_worker,
                              [(s, files[s]["local"], budgets[s], id_n) for s in shards]))
        rows = []
        for shard, chunks, _ in res:
            for c in chunks:
                rows.append({"pid": None, "text": c["text"], "n_tokens": c["n_tokens"],
                             "shard": shard, "source": shard.split("/")[1], "split": None})
        held = split_halves(len(rows), D42.rng_for("p45:dolma:split"), K.DOLMA_HELDOUT_FRAC)
        for i, (r, h) in enumerate(zip(rows, held)):
            r["pid"] = f"dolma-{i:06d}"
            r["split"] = "heldout" if h else "train"
        train_tok = sum(r["n_tokens"] for r in rows if r["split"] == "train")
        got = {s: sum(c["n_tokens"] for c in ch) for s, ch, _ in res}
        exhausted = {s for s, _, st in res if st["docs_exhausted"]}
        new_b, moved = redistribute(budgets, got, exhausted)
        history.append({"attempt": attempt, "train_tokens": train_tok,
                        "exhausted": sorted(exhausted), "moved_tokens": moved})
        if train_tok >= K.DOLMA_MIN_TRAIN_TOKENS and moved == 0:
            break
        if moved == 0:  # short without exhausted shards: raise every live budget by 4%
            new_b = {s: (b if s in exhausted else int(np.ceil(b * 1.04))) for s, b in new_b.items()}
        print(f"[dolma] attempt {attempt}: train tokens {train_tok}, exhausted {len(exhausted)} "
              f"shards, moved {moved} tokens; rerunning", flush=True)
        budgets = new_b
    meta["token_margin"] = DOLMA_TOKEN_MARGIN
    meta["total_token_budget"] = total
    meta["shard_budgets"] = budgets
    meta["budget_history"] = history
    meta["shard_stats"] = {s: st for s, _, st in res}
    check_dolma_rows(rows)

    n = np.array([r["n_tokens"] for r in rows])
    fam_tok: dict[str, int] = {}
    for r in rows:
        f = family_of(r["source"])
        fam_tok[f] = fam_tok.get(f, 0) + r["n_tokens"]
    tot = sum(fam_tok.values())
    meta["family_token_share_achieved"] = {f: v / tot for f, v in sorted(fam_tok.items())}
    meta["n_rows"] = len(rows)
    meta["split_counts"] = {s: sum(r["split"] == s for r in rows) for s in ("train", "heldout")}
    meta["tokens"] = {"total": int(n.sum()), "train": int(train_tok),
                      "heldout": int(n.sum() - train_tok)}
    meta["n_tokens_percentiles"] = D42._percentiles(n)
    meta["id_ref_n_tokens_percentiles"] = D42._percentiles(id_n)
    meta["ks_vs_id_ref"] = _ks(n, id_n)
    meta["exact_length_share"] = (sum(st["exact_length"] for _, _, st in res) / max(len(rows), 1))
    meta["preflight"] = {
        "train_tokens_ge_min": bool(train_tok >= K.DOLMA_MIN_TRAIN_TOKENS),
        "content_tokens_ge_2.5M": bool(n.sum() >= 2_500_000),
        "ks_p_gt_0.05": meta["ks_vs_id_ref"]["passes"]}
    meta["preflight"]["passes"] = all(meta["preflight"].values())
    meta["chunk_rule"] = (
        "lengths drawn from id_ref n_tokens capped at 2048; per draw, the first unused buffered "
        "document (seeded random order, tokenised from its first "
        f"{DOLMA_DOC_MAX_CHARS} chars) with >= L tokens gives one span at a random offset; "
        f"decoded, stripped, re-tokenised, extended by <= {DOLMA_MAX_EXTEND} tokens to hit L")
    _write_jsonl(K.DOLMA_PATH, rows)
    meta["path"] = _rel(K.DOLMA_PATH)
    meta["sha256"] = _sha256(K.DOLMA_PATH)
    meta["wall_clock_s"] = dict(timer.t, total=round(time.time() - t0, 1))
    meta["finished_utc"] = _now()
    _update_meta("dolma", meta)
    print(f"[dolma] wrote {K.DOLMA_PATH} ({len(rows)} rows, {n.sum()} tokens, train {train_tok}) "
          f"sha256 {meta['sha256']}; KS p {meta['ks_vs_id_ref']['pvalue']:.3g}; shares "
          f"{meta['family_token_share_achieved']}; preflight {meta['preflight']}", flush=True)
    return meta


def check_dolma_rows(rows: list[dict]) -> None:
    pids = [r["pid"] for r in rows]
    assert len(set(pids)) == len(pids) and pids == sorted(pids)
    for r in rows:
        assert tuple(r) == K.DOLMA_FIELDS, f"fields {tuple(r)}"
        assert r["split"] in ("train", "heldout")
        assert isinstance(r["text"], str) and r["text"] and r["n_tokens"] > 0
        assert r["n_tokens"] <= P42K.MAX_PROMPT_TOKENS


# ------------------------------------------------------------------------------------------------
# check
# ------------------------------------------------------------------------------------------------

def check() -> int:
    meta = json.loads(K.DATA_META.read_text())
    bad = 0
    for key, path, fields in (("law2", K.LAW2_PATH, P42K.PROMPT_FIELDS),
                              ("dolma", K.DOLMA_PATH, K.DOLMA_FIELDS)):
        m = meta.get(key)
        if m is None or not path.exists():
            print(f"[check] {key}: MISSING ({path})")
            bad += 1
            continue
        sha = _sha256(path)
        with path.open() as f:  # line iteration, not str.splitlines (U+2028 inside texts)
            rows = [json.loads(x) for x in f]
        ok_f = all(tuple(r) == fields for r in rows)
        ok = sha == m["sha256"] and len(rows) == m["n_rows"] and ok_f
        if key == "law2":
            ok &= all(r["set"] == K.PLANTED_LAW2 for r in rows)
            extra = (f"splits {m['split_counts']}, deciles {m['length_match']['achieved']} vs "
                     f"{m['length_match']['targets']}, KS p {m['length_match']['ks_vs_pool']['pvalue']:.3g}")
        else:
            tr = sum(r["n_tokens"] for r in rows if r["split"] == "train")
            ok &= tr == m["tokens"]["train"]
            extra = (f"train tokens {tr}, KS p {m['ks_vs_id_ref']['pvalue']:.3g}, shares "
                     + ", ".join(f"{f} {v:.3f}" for f, v in
                                 m["family_token_share_achieved"].items()))
        print(f"[check] {key}: {'OK' if ok else 'FAIL'} {len(rows)} rows sha256 {sha[:12]}… "
              f"preflight {m['preflight']}; {extra}")
        bad += not ok
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="p45.data")
    ap.add_argument("cmd", choices=("law2", "dolma", "check"))
    a = ap.parse_args(argv)
    cmd = ["python -m p45.data"] + argv
    if a.cmd == "law2":
        build_law2(cmd)
    elif a.cmd == "dolma":
        build_dolma(cmd)
    else:
        return check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
