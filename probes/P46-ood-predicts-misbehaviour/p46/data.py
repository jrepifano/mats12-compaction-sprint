"""P46 data prep (plan.md "Setup", "Reference sets", "Dedup"): every prompt the probe scores or
generates from, in `data/P46/prompts.jsonl` (K.PROMPT_FIELDS, sorted by pid), its build record
`prompts.meta.json`, and the judge-check items `data/P46/human_check.jsonl` (K.HUMAN_FIELDS).

    cd probes/P46-ood-predicts-misbehaviour
    uv run --with pyarrow --with xxhash python -m p46.data download          # free; gated repos
    uv run --with pyarrow --with xxhash python -m p46.data embed [--dry-run]   # paid (OpenAI)
    uv run --with pyarrow --with xxhash python -m p46.data build [--dry-run]   # free
    uv run --with pyarrow --with xxhash python -m p46.data refs                # free: counts only

Order: download -> `python -m p46.rewrite para` and `benign` (paid) -> embed (paid) -> build.

Sets (K.SETS):
- **core / lang**: SORRY-Bench 202503 (`question.jsonl` = base, `question_<style>.jsonl`), the
  base + K.CORE_STYLES[1:] (3,080 less the 4 null "question" turns in 202503 = 3,076) and
  K.LANG_STYLES (880). family = question_id, base_text = the
  base turn. The loader asserts 440 base rows, one turn each, the same question_id set and
  category in every file, and prompt_style == the file's style when the field is present.
- **para**: p46.rewrite's table (K.PARA_STYLES, 1,320 when nothing failed); a row whose base
  hash does not match the current base text is refused.
- **benign**: 50 Alpaca instructions (`alpaca_sample`: seeded, empty `input`, 8-40 words, no code
  by `CODE_RE`) x K.BENIGN_STYLES; base = the original, the other 8 from p46.rewrite's table.
- **ref_dpo / ref_lmsys / heldout**: P42's id_ref / pool / id_heldout rows (pid, text, n_tokens,
  lang, english, truncated kept), from `data/P42/prompts.jsonl` at K.P42_PROMPTS_SHA256.
- **ref_safety**: every Dolci DPO first user turn whose prompt_id contains one of
  K.SAFETY_PREFIXES (P42's extracted first turns), asserted == K.N_SAFETY_EXPECTED.

P42's gitignored inputs live in the main checkout; `resolve_shared` falls back to it from a
worktree (`$P46_MAIN_ROOT` overrides).

Dedup (probe + benign rows): `dedup_lex` = exact normalised hash or MinHash Jaccard >= 0.8
against every Dolci SFT + DPO first turn, with P42's code, parameters and cached signatures
(`data/P42/raw/cache/`, checked against P42's pins and MinHash key). `dedup_sem` (probe sets) =
max cosine of the text-embedding-3-small vector to the Dolci safety prompts (ref_safety plus the
49,965 Dolci SFT first turns with source_dataset "WildJailbreak"). `flag_family` = the family's
base row has dedup_lex or dedup_sem >= K.SEM_DEDUP (benign: dedup_lex only).

Embeddings (`embed`, paid; P42's batching, retries and EmbedBudget, ledger exp "P46", note
`p46_embed`): vectors for every prompts row and the SFT WildJailbreak prompts. Vectors already in
P42's cache (`probes/P42-ood-coverage-gaps/results/embed/chunks`, matched by the sha of the text
sent) are reused. Layout in `data/P46/emb/` (EMB_* below): `chunks/*.npz` (cache, P42's format),
`emb.f16.npy` + `pids.txt` (P42's layout: every prompts row in prompts order, L2-normalised,
read by p46.embed) + `emb.keys.tsv` (pid, text sha),
`sft_wjb.f16.npy` + `sft_wjb.keys.tsv` (key, sha, Dolci SFT id), `emb.meta.json`.

human_check (plan "Judges"): SORRY-Bench human-judgment 202503 `test.jsonl` items whose
(question_id, response) pair is not in `train.jsonl`, deduplicated, then a seeded draw of
K.N_HUMAN_POD ("pod") and K.N_HUMAN_LUNA ("luna") items, each half human_score 1 and half 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

from p46 import contracts as K

_P42_DIR = K.REPO_ROOT / "probes" / "P42-ood-coverage-gaps"
if str(_P42_DIR) not in sys.path:
    sys.path.insert(0, str(_P42_DIR))

from p42 import contracts as K42
from p42 import data as D42

SORRY_FILES = {"base": "question.jsonl",
               **{s: f"question_{s}.jsonl" for s in K.CORE_STYLES[1:] + K.LANG_STYLES}}
HUMAN_FILES = ("train.jsonl", "test.jsonl")
N_SORRY_BASE = 440
SORRY_KEYS = frozenset({"question_id", "category", "turns", "prompt_style"})
# FastChat answer records (question_id, answer_id, model_id, choices, tstamp) + human_score; the
# others are tolerated if present. Anything else fails loudly (inspect, then extend).
HUMAN_REQUIRED = frozenset({"question_id", "choices", "human_score"})
HUMAN_OPTIONAL = frozenset({"answer_id", "model_id", "tstamp", "category", "prompt_style",
                            "turns", "model", "data_mutation"})
LANG_OF = {"translate-zh-cn": "zh", "translate-fr": "fr"}
ALPACA_WORDS = (8, 40)
CODE_RE = re.compile(
    r"`|\bcode\b|\bcoding\b|\bprogram(?:s|me|ming)?\b|\bpython\b|\bjava(?:script)?\b|c\+\+|"
    r"\bsql\b|\bhtml\b|\bcss\b|\bfunction\b|\bscript\b|\balgorithm\b|\bregex\b|"
    r"\bregular expression\b|\bdef\b|\bclass\b|\bsyntax\b|\bcompile\b|\bdebug\b|[{};<>=]")
ALPACA_PIN = K.RAW_DIR / "alpaca" / "PIN.json"

EMB_PROMPTS = K.EMB_DIR / "emb.f16.npy"  # P42 layout, read by p46.embed (S1/S1s)
EMB_PIDS = K.EMB_DIR / "pids.txt"
EMB_PROMPTS_KEYS = K.EMB_DIR / "emb.keys.tsv"  # pid <tab> sha of the text sent
EMB_SFT_WJB = K.EMB_DIR / "sft_wjb.f16.npy"
EMB_SFT_WJB_KEYS = K.EMB_DIR / "sft_wjb.keys.tsv"
EMB_META = K.EMB_DIR / "emb.meta.json"
EMBED_NOTE = "p46_embed"
SFT_WJB_SOURCE = "WildJailbreak"  # Dolci SFT source_dataset (P42's `category` for dolci_sft)
N_SFT_WJB_EXPECTED = 49_965  # Olmo 3; counted on P42's extraction 2026-10-14

P42_SET_MAP = {"id_ref": K.REF_DPO, "pool": K.REF_LMSYS, "id_heldout": K.HELDOUT}


class GatedSourceError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------
# Paths: P42's gitignored inputs live in the main checkout
# ------------------------------------------------------------------------------------------------

def main_checkout_root(repo_root: Path = K.REPO_ROOT) -> Path:
    env = os.environ.get("P46_MAIN_ROOT")
    if env:
        return Path(env)
    parts = Path(repo_root).parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return Path(*parts[:i])
    return Path(repo_root)


def resolve_shared(path: Path, repo_root: Path = K.REPO_ROOT,
                   main_root: Path | None = None) -> Path:
    """`path` (under repo_root) if it exists, else the same relative path in the main checkout."""
    path = Path(path)
    if path.exists():
        return path
    alt = (main_root or main_checkout_root(repo_root)) / path.relative_to(repo_root)
    if alt.exists():
        return alt
    raise FileNotFoundError(f"{path.relative_to(repo_root)}: neither {path} nor {alt} exists")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest()


def rng_for(step: str) -> np.random.Generator:
    tag = int.from_bytes(hashlib.sha256(step.encode()).digest()[:4], "little")
    return np.random.default_rng([K.SEED_SAMPLE, tag])


# ------------------------------------------------------------------------------------------------
# Downloads (free; SORRY-Bench repos are gated, auto-approve)
# ------------------------------------------------------------------------------------------------

def _hf():
    D42._load_env()  # .env (HF_TOKEN), HF_HUB_DISABLE_XET=1
    from huggingface_hub import HfApi, hf_hub_download
    from huggingface_hub.errors import GatedRepoError

    return HfApi(), hf_hub_download, GatedRepoError


def gated_msg(repo: str) -> str:
    return (f"{repo} is gated and our HF token gets 403 on its files. Jacob must accept the terms "
            f"at https://huggingface.co/datasets/{repo} (auto-approve), then rerun. Fallback "
            "(plan 'Automatic rules'): StrongREJECT from GitHub.")


def download_files(source: str, files: list[str], dest: Path, revision: str | None = None) -> dict:
    """Files of a pinned HF dataset into dest; returns {repo, revision, files: {name: sha256}}."""
    api, hf_hub_download, GatedRepoError = _hf()
    repo, pin = K.SOURCES[source]
    pin = revision or pin
    info = api.dataset_info(repo, revision=pin)
    if not info.sha.startswith(pin):
        raise RuntimeError(f"{repo}: resolved {info.sha} does not match pin {pin}")
    dest.mkdir(parents=True, exist_ok=True)
    rec = {"repo": repo, "revision": info.sha, "files": {}}
    for fn in files:
        try:
            p = Path(hf_hub_download(repo, fn, repo_type="dataset", revision=info.sha,
                                     local_dir=dest))
        except GatedRepoError as e:
            raise GatedSourceError(gated_msg(repo)) from e
        rec["files"][fn] = sha256_file(p)
        print(f"[download] {source} {fn} {p.stat().st_size / 1e6:.1f} MB", flush=True)
    (dest / "FILES.json").write_text(json.dumps(rec, indent=1) + "\n")
    return rec


def _ensure(source: str, files: list[str], dest: Path, revision: str | None = None) -> Path:
    rec_p = dest / "FILES.json"
    if rec_p.exists():
        rec = json.loads(rec_p.read_text())
        if (set(files) <= set(rec["files"]) and all((dest / f).exists() for f in files)
                and rec["revision"].startswith(revision or K.SOURCES[source][1])):
            return dest
    download_files(source, files, dest, revision)
    return dest


def ensure_sorry(raw_dir: Path = K.RAW_DIR) -> Path:
    return _ensure("sorry", list(SORRY_FILES.values()), Path(raw_dir) / "sorry")


def ensure_sorry_human(raw_dir: Path = K.RAW_DIR) -> Path:
    return _ensure("sorry_human", list(HUMAN_FILES), Path(raw_dir) / "sorry_human")


def alpaca_pin(pin_path: Path = ALPACA_PIN) -> str:
    """tatsu-lab/alpaca has no pin in the contracts: the head sha is resolved once through the HF
    API and recorded in data/P46/raw/alpaca/PIN.json; later runs reuse it."""
    if pin_path.exists():
        return json.loads(pin_path.read_text())["revision"]
    api, _, _ = _hf()
    info = api.dataset_info(K.SOURCES["alpaca"][0])
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(json.dumps({"repo": K.SOURCES["alpaca"][0], "revision": info.sha,
                                    "resolved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                  time.gmtime()),
                                    "files": [s.rfilename for s in info.siblings]}, indent=1))
    return info.sha


def ensure_alpaca(raw_dir: Path = K.RAW_DIR) -> Path:
    pin = alpaca_pin()
    files = [f for f in json.loads(ALPACA_PIN.read_text())["files"]
             if f.startswith("data/") and f.endswith(".parquet")]
    if len(files) != 1:
        raise RuntimeError(f"alpaca: expected one data parquet, got {files}")
    dest = _ensure("alpaca", files, Path(raw_dir) / "alpaca", revision=pin)
    return dest / files[0]


# ------------------------------------------------------------------------------------------------
# SORRY-Bench, human judgments, Alpaca
# ------------------------------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").split("\n")
            if x.strip()]


def load_sorry(sorry_dir: Path, styles=("base",) + K.CORE_STYLES[1:] + K.LANG_STYLES,
               n_base: int = N_SORRY_BASE) -> dict[str, dict[int, dict]]:
    """{style: {question_id: {"category", "text", "prompt_style"}}}, aligned across files. A
    null turn in a mutated file (SORRY-Bench's generator failed there) is kept as text None and
    skipped by `sorry_rows` (`sorry_missing`); the base file must be complete."""
    out: dict[str, dict[int, dict]] = {}
    for st in styles:
        path = Path(sorry_dir) / SORRY_FILES[st]
        d: dict[int, dict] = {}
        for i, r in enumerate(read_jsonl(path)):
            missing = {"question_id", "category", "turns"} - set(r)
            extra = set(r) - SORRY_KEYS
            if missing or extra:
                raise ValueError(f"{path.name} row {i}: missing {sorted(missing)}, unexpected "
                                 f"{sorted(extra)} (keys {sorted(r)})")
            qid = int(r["question_id"])
            turns = r["turns"]
            if not (isinstance(turns, list) and len(turns) == 1):
                raise ValueError(f"{path.name} question {qid}: expected one turn")
            text = turns[0]
            if text is None and st != "base":
                pass  # SORRY's own mutation failed (202503: 4 nulls in question_question.jsonl)
            elif not (isinstance(text, str) and text.strip()):
                raise ValueError(f"{path.name} question {qid}: expected one non-empty turn")
            ps = r.get("prompt_style")
            if ps is not None and ps != st:
                raise ValueError(f"{path.name} question {qid}: prompt_style {ps!r} != {st!r}")
            if qid in d:
                raise ValueError(f"{path.name}: duplicate question_id {qid}")
            d[qid] = {"category": str(r["category"]), "text": text, "prompt_style": ps}
        out[st] = d
    base = out["base"]
    if len(base) != n_base:
        raise ValueError(f"SORRY-Bench base: {len(base)} rows, expected {n_base}")
    for st, d in out.items():
        if set(d) != set(base):
            raise ValueError(f"{SORRY_FILES[st]}: question_ids differ from the base file "
                             f"({len(set(d) ^ set(base))} differ)")
        bad = [q for q in d if d[q]["category"] != base[q]["category"]]
        if bad:
            raise ValueError(f"{SORRY_FILES[st]}: category differs from base for {bad[:5]}")
    return out


def human_response(r: dict) -> str:
    ch = r["choices"]
    if not (isinstance(ch, list) and len(ch) >= 1 and isinstance(ch[0].get("turns"), list)
            and ch[0]["turns"] and isinstance(ch[0]["turns"][0], str)):
        raise ValueError(f"human record {r.get('question_id')}: choices[0].turns[0] missing")
    return ch[0]["turns"][0]


def check_human_records(rows: list[dict], name: str) -> None:
    for i, r in enumerate(rows):
        missing = HUMAN_REQUIRED - set(r)
        extra = set(r) - HUMAN_REQUIRED - HUMAN_OPTIONAL
        if missing or extra:
            raise ValueError(f"{name} row {i}: missing {sorted(missing)}, unexpected "
                             f"{sorted(extra)} (keys {sorted(r)}); inspect the real schema and "
                             "extend HUMAN_OPTIONAL")
        s = float(r["human_score"])
        if s not in (0.0, 1.0):
            raise ValueError(f"{name} row {i}: human_score {r['human_score']!r} not 0/1")


def human_check_rows(test: list[dict], train: list[dict], base_texts: dict[int, str],
                     n_pod: int = K.N_HUMAN_POD, n_luna: int = K.N_HUMAN_LUNA
                     ) -> tuple[list[dict], dict]:
    """HUMAN_FIELDS rows (sorted by hid) and stats. Test items whose (question_id, response) is
    also in train are excluded, as are repeats within test; each use draws half score 1 and half
    score 0 (the other class fills a shortfall)."""
    check_human_records(train, "train.jsonl")
    check_human_records(test, "test.jsonl")
    train_keys = {(int(r["question_id"]), human_response(r)) for r in train}
    pool, seen = {0: [], 1: []}, set()
    n_in_train = n_repeat = n_style = 0
    for r in test:
        # Only the styles P46's judge sees in English (base + the 6 English mutations); ciphers
        # and translations keep raw encoded / non-English responses (main session, 2026-10-14).
        if r.get("prompt_style") not in K.CORE_STYLES:
            n_style += 1
            continue
        k = (int(r["question_id"]), human_response(r))
        if k in train_keys:
            n_in_train += 1
            continue
        if k in seen:
            n_repeat += 1
            continue
        seen.add(k)
        if k[0] not in base_texts:
            raise ValueError(f"human item question_id {k[0]} not in SORRY-Bench base")
        pool[int(float(r["human_score"]))].append(r)
    rng = rng_for("human_check")
    for c in (0, 1):
        pool[c] = [pool[c][i] for i in rng.permutation(len(pool[c]))]
    if len(pool[0]) + len(pool[1]) < n_pod + n_luna:
        raise ValueError(f"only {len(pool[0]) + len(pool[1])} eligible human items")
    rows: list[dict] = []
    for use, n in (("pod", n_pod), ("luna", n_luna)):
        want = {1: n // 2, 0: n - n // 2}
        for c in (0, 1):
            short = max(0, want[c] - len(pool[c]))
            want[c] -= short
            want[1 - c] += short
        picked = pool[1][:want[1]] + pool[0][:want[0]]
        pool[1], pool[0] = pool[1][want[1]:], pool[0][want[0]:]
        picked = [picked[i] for i in rng.permutation(len(picked))]
        for j, r in enumerate(picked):
            qid = int(r["question_id"])
            rows.append({"hid": f"{use}-{j:03d}", "question_id": qid,
                         "prompt_style": r.get("prompt_style"), "base_text": base_texts[qid],
                         "response": human_response(r), "human_score": int(float(r["human_score"])),
                         "use": use})
    rows.sort(key=lambda r: r["hid"])
    for r in rows:
        assert tuple(r) == K.HUMAN_FIELDS
    stats = {"test_rows": len(test), "train_rows": len(train), "test_in_train": n_in_train,
             "test_repeats": n_repeat, "test_other_style": n_style, "eligible": len(seen),
             "drawn": {u: {s: sum(1 for r in rows if r["use"] == u and r["human_score"] == s)
                           for s in (0, 1)} for u in ("pod", "luna")}}
    return rows, stats


def alpaca_candidates(instructions: list[str], inputs: list[str]) -> list[int]:
    lo, hi = ALPACA_WORDS
    return [i for i, (ins, inp) in enumerate(zip(instructions, inputs))
            if not (inp or "").strip() and lo <= len(ins.split()) <= hi
            and not CODE_RE.search(ins.lower())]


def alpaca_sample(path: Path | None = None, n: int = K.N_BENIGN) -> list[dict]:
    """[{"family", "alpaca_index", "text"}]: a seeded draw of n candidates, family numbered by
    Alpaca row order."""
    import pyarrow.parquet as pq

    t = pq.read_table(path or ensure_alpaca(), columns=["instruction", "input"])
    ins, inp = t.column("instruction").to_pylist(), t.column("input").to_pylist()
    cand = alpaca_candidates(ins, inp)
    pick = sorted(int(cand[i]) for i in rng_for("benign").choice(len(cand), n, replace=False))
    return [{"family": f, "alpaca_index": i, "text": ins[i]} for f, i in enumerate(pick)]


# ------------------------------------------------------------------------------------------------
# Rows
# ------------------------------------------------------------------------------------------------

def make_row(**kw) -> dict:
    r = {f: None for f in K.PROMPT_FIELDS}
    for k, v in kw.items():
        if k not in r:
            raise KeyError(k)
        r[k] = v
    return r


def sorry_missing(sorry: dict) -> dict[str, list[int]]:
    """{style: [question_id, ...]} of mutated variants with a null turn (left out of the set)."""
    return {st: sorted(q for q, v in d.items() if v["text"] is None)
            for st, d in sorry.items() if any(v["text"] is None for v in d.values())}


def lang_of(style: str) -> str:
    return LANG_OF.get(style, "en")


def sorry_rows(sorry: dict, set_name: str, styles) -> list[dict]:
    rows = []
    base = sorry["base"]
    for qid in sorted(base):
        for st in styles:
            if sorry[st][qid]["text"] is None:
                continue
            lg = lang_of(st)
            rows.append(make_row(pid=f"{set_name}-{qid:04d}-{st}", set=set_name, family=qid,
                                 style=st, source="sorry", source_id=f"{qid}:{st}",
                                 category=base[qid]["category"], text=sorry[st][qid]["text"],
                                 base_text=base[qid]["text"], lang=lg, english=lg == "en"))
    return rows


def rewrite_rows(table: list[dict] | None, set_name: str, styles, bases: dict[int, dict],
                 source: str, source_ids: dict[int, str] | None = None) -> tuple[list[dict], dict]:
    """Rows from p46.rewrite's table (status ok only). bases: {family: {"text", "category"}}.
    Returns (rows, {"missing": [...], "failed": {status: n}})."""
    from p46 import rewrite as R

    got = {}
    failed: dict[str, int] = {}
    for t in table or []:
        if t["style"] not in styles:
            continue
        fam = int(t["family"])
        if fam not in bases:
            raise ValueError(f"rewrite row {t['key']}: family {fam} not in the current set")
        if t["base_sha"] != R.sha(bases[fam]["text"]):
            raise ValueError(f"rewrite row {t['key']}: base text changed since the rewrite; "
                             "rerun p46.rewrite")
        if t["status"] != "ok":
            failed[t["status"]] = failed.get(t["status"], 0) + 1
            continue
        got[(fam, t["style"])] = t
    rows, missing = [], []
    for fam in sorted(bases):
        for st in styles:
            t = got.get((fam, st))
            if t is None:
                missing.append(f"{fam}:{st}")
                continue
            lg = lang_of(st)
            sid = f"{(source_ids or {}).get(fam, fam)}:{st}"
            rows.append(make_row(pid=f"{set_name}-{fam:04d}-{st}", set=set_name, family=fam,
                                 style=st, source=source, source_id=sid,
                                 category=bases[fam].get("category"), text=t["text"],
                                 base_text=bases[fam]["text"], lang=lg, english=lg == "en"))
    return rows, {"missing": missing, "failed": failed,
                  "by_model": _count(t["model"] for t in got.values())}


def benign_rows(instructions: list[dict], table: list[dict] | None) -> tuple[list[dict], dict]:
    bases = {d["family"]: {"text": d["text"], "category": None} for d in instructions}
    ids = {d["family"]: str(d["alpaca_index"]) for d in instructions}
    rows = [make_row(pid=f"{K.BENIGN}-{f:04d}-base", set=K.BENIGN, family=f, style="base",
                     source="alpaca", source_id=f"{ids[f]}:base", category=None,
                     text=bases[f]["text"], base_text=bases[f]["text"], lang="en", english=True)
            for f in sorted(bases)]
    more, st = rewrite_rows(table, K.BENIGN, [s for s in K.BENIGN_STYLES if s != "base"], bases,
                            "alpaca", ids)
    return rows + more, st


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def p42_ref_rows(p42_rows: list[dict]) -> list[dict]:
    rows = []
    for r in p42_rows:
        s = P42_SET_MAP.get(r["set"])
        if s is None:
            continue
        if s == K.REF_LMSYS and r["source"] != "lmsys":
            raise ValueError(f"P42 pool row {r['pid']} has source {r['source']!r}, not lmsys")
        rows.append(make_row(pid=r["pid"], set=s, source=r["source"], source_id=r["source_id"],
                             category=r["category"], text=r["text"], n_tokens=r["n_tokens"],
                             truncated=r["truncated"], lang=r["lang"], english=r["english"]))
    return rows


def load_p42_prompts(path: Path | None = None) -> tuple[list[dict], Path]:
    p = path or resolve_shared(K.P42_PROMPTS)
    got = sha256_file(p)
    if got != K.P42_PROMPTS_SHA256:
        raise ValueError(f"{p}: sha256 {got} != contracts {K.P42_PROMPTS_SHA256}")
    return read_jsonl(p), p


def p42_cache_dir() -> Path:
    return resolve_shared(K.P42_RAW_DIR / "cache")


def _check_p42_cache(source: str, cache: Path, need_sig: bool) -> dict:
    meta = json.loads((cache / f"{source}.cache.json").read_text())
    if meta["key"]["revision"] != K.SOURCES[source][1]:
        raise ValueError(f"P42 cache {source}: revision {meta['key']['revision']} != P46 pin")
    want = {"perm": K42.MINHASH_PERM, "shingle": K42.MINHASH_SHINGLE, "seed": K42.SEED_SAMPLE,
            "version": 1}
    if need_sig and meta.get("sig_key") != want:
        raise ValueError(f"P42 cache {source}: MinHash key {meta.get('sig_key')} != {want}")
    return meta


def safety_first_turns(cache: Path | None = None) -> dict:
    """Dolci DPO first turns whose prompt_id contains a K.SAFETY_PREFIXES entry, in P42's
    extraction order: {"source_id", "category", "text"} lists."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    cache = cache or p42_cache_dir()
    _check_p42_cache("dolci_dpo", cache, need_sig=False)
    t = pq.read_table(cache / "dolci_dpo.firstturns.parquet",
                      columns=["source_id", "category", "text"])
    ids = t.column("source_id")
    m = None
    for p in K.SAFETY_PREFIXES:
        hit = pc.match_substring(ids, p)
        m = hit if m is None else pc.or_(m, hit)
    return t.filter(m).to_pydict()


def sft_wjb_first_turns(cache: Path | None = None) -> dict:
    import pyarrow.parquet as pq

    cache = cache or p42_cache_dir()
    _check_p42_cache("dolci_sft", cache, need_sig=False)
    t = pq.read_table(cache / "dolci_sft.firstturns.parquet",
                      columns=["source_id", "category", "text"],
                      filters=[("category", "=", SFT_WJB_SOURCE)])
    return t.to_pydict()


def ref_safety_rows(first: dict, tok) -> list[dict]:
    n, tr, txt = D42.tokenize_and_truncate(tok, first["text"], K.MAX_PROMPT_TOKENS)
    rows = []
    for i, (sid, cat, raw) in enumerate(zip(first["source_id"], first["category"],
                                            first["text"])):
        lg = D42.detect_english(raw)
        rows.append(make_row(pid=f"{K.REF_SAFETY}-{i:06d}", set=K.REF_SAFETY, source="dolci_dpo",
                             source_id=sid, category=cat, text=txt[i], n_tokens=int(n[i]),
                             truncated=bool(tr[i]), lang=lg, english=lg == "English"))
    return rows


def tokenize_rows(rows: list[dict], tok) -> None:
    """n_tokens / truncated / head-truncated text for rows that lack them (in place)."""
    todo = [r for r in rows if r["n_tokens"] is None]
    if not todo:
        return
    n, tr, txt = D42.tokenize_and_truncate(tok, [r["text"] for r in todo], K.MAX_PROMPT_TOKENS)
    for r, a, b, c in zip(todo, n, tr, txt):
        r["n_tokens"], r["truncated"], r["text"] = int(a), bool(b), c


def set_gen(rows: list[dict]) -> None:
    for r in rows:
        r["gen_dpo"], r["gen_sft"] = K.GEN_N.get(r["set"], (0, 0))


def check_rows(rows: list[dict]) -> None:
    pids = [r["pid"] for r in rows]
    if len(set(pids)) != len(pids):
        raise ValueError("duplicate pid")
    if pids != sorted(pids):
        raise ValueError("rows not sorted by pid")
    for r in rows:
        if tuple(r) != K.PROMPT_FIELDS:
            raise ValueError(f"{r['pid']}: fields {tuple(r)}")
        if r["set"] not in K.SETS or not (r["source"] in K.SOURCES or r["source"] == "ours"):
            raise ValueError(f"{r['pid']}: set {r['set']!r} / source {r['source']!r}")
        if not isinstance(r["text"], str) or not isinstance(r["n_tokens"], int):
            raise TypeError(f"{r['pid']}: text / n_tokens missing")


# ------------------------------------------------------------------------------------------------
# Dedup
# ------------------------------------------------------------------------------------------------

DEDUP_SETS = K.PROBE_SETS + (K.BENIGN,)


def dolci_index(cache: Path | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """(sorted unique exact hashes, MinHash signatures of the exact-unique first turns, stats) of
    every Dolci SFT + DPO first turn, from P42's cache (as p42.data.build's dolci_index)."""
    cache = cache or p42_cache_dir()
    hs, sigs = [], []
    for s in ("dolci_dpo", "dolci_sft"):
        meta = _check_p42_cache(s, cache, need_sig=True)
        h = np.load(cache / f"{s}.hash.npy")
        if meta.get("sig_rows") != len(h):
            raise ValueError(f"P42 cache {s}: sig_rows {meta.get('sig_rows')} != {len(h)}")
        hs.append(h)
        sigs.append(np.load(cache / f"{s}.sig.npy", mmap_mode="r"))
    all_h = np.concatenate(hs)
    u = D42.first_occurrence_mask(all_h)
    idx_sig = np.concatenate([np.asarray(s) for s in sigs])[u]
    return np.unique(all_h), idx_sig, {"n_first_turns": len(all_h),
                                       "n_unique_exact": int(u.sum())}


def lexical_dedup(texts: list[str], hset: np.ndarray, idx_sig: np.ndarray
                  ) -> tuple[np.ndarray, np.ndarray, dict]:
    """(exact match, near match) bool arrays vs the Dolci index, P42's parameters."""
    h = D42.exact_hashes(texts)
    exact = np.isin(h, hset)
    sig = D42.minhash_signatures(texts)
    near, st = D42.near_dup_of_index(sig, idx_sig)
    return exact, near, st


def max_cosine(q: np.ndarray, ref: np.ndarray, chunk: int = 2048) -> np.ndarray:
    q, ref = np.asarray(q, np.float32), np.asarray(ref, np.float32)
    out = np.empty(len(q), dtype=np.float64)
    for s in range(0, len(q), chunk):
        out[s:s + chunk] = (q[s:s + chunk] @ ref.T).max(axis=1)
    return out


def apply_dedup(rows: list[dict], lex: dict[str, bool], sem: dict[str, float]) -> dict:
    """Fill dedup_lex (DEDUP_SETS), dedup_sem (K.PROBE_SETS) and flag_family (by the family's
    base row: core base for core/lang/para, the benign base for benign). Returns stats."""
    for r in rows:
        if r["set"] in DEDUP_SETS:
            r["dedup_lex"] = bool(lex[r["pid"]])
        if r["set"] in K.PROBE_SETS:
            v = sem.get(r["pid"])
            r["dedup_sem"] = None if v is None else round(float(v), 6)
    base_flag = {}
    for r in rows:
        if r["style"] == "base" and r["set"] in (K.CORE, K.BENIGN):
            s = r["dedup_sem"]
            base_flag[(r["set"], r["family"])] = bool(
                r["dedup_lex"] or (s is not None and s >= K.SEM_DEDUP))
    for r in rows:
        if r["set"] in K.PROBE_SETS:
            r["flag_family"] = base_flag.get((K.CORE, r["family"]))
        elif r["set"] == K.BENIGN:
            r["flag_family"] = base_flag.get((K.BENIGN, r["family"]))
    fam = base_flag
    return {"families_flagged": {s: sum(v for (ss, _), v in fam.items() if ss == s)
                                 for s in (K.CORE, K.BENIGN)},
            "families": {s: sum(1 for (ss, _) in fam if ss == s) for s in (K.CORE, K.BENIGN)}}


# ------------------------------------------------------------------------------------------------
# Embeddings (paid: OpenAI text-embedding-3-small; P42's machinery, ledger exp "P46")
# ------------------------------------------------------------------------------------------------

def _e42():
    from p42 import embed as E42

    return E42


def p42_sha_vectors(embed_dir: Path | None = None) -> dict[str, np.ndarray]:
    """{sha of the text sent: float32 vector} from P42's embedding cache (main checkout)."""
    E42 = _e42()
    try:
        d = embed_dir or resolve_shared(K.P42_EMBED_DIR)
    except FileNotFoundError:
        return {}
    return {sha: v for sha, v in E42.load_cache(d).values()}


def embed_items(rows: list[dict], wjb: dict) -> list[tuple[str, str, str]]:
    """[(key, sha, text sent)] for every prompts row (key = pid) and every SFT WildJailbreak
    prompt (key = sft_wjb-<index>)."""
    E42 = _e42()
    items = []
    for r in rows:
        t = E42.embed_input(r["text"])
        items.append((r["pid"], E42.text_sha(t), t))
    for i, raw in enumerate(wjb["text"]):
        t = E42.embed_input(raw)
        items.append((f"sft_wjb-{i:06d}", E42.text_sha(t), t))
    return items


def embed_needed(rows: list[dict], wjb: dict, *, dry_run: bool, client=None,
                 emb_dir: Path = K.EMB_DIR, p42_vectors: dict | None = None,
                 max_usd: float | None = None, concurrency: int | None = None,
                 enc="auto") -> int:
    """Embed what neither P46's nor P42's cache has; write the EMB_* outputs once every item has
    a vector. Returns an exit code."""
    import asyncio

    from emx import spend

    E42 = _e42()
    items = embed_items(rows, wjb)
    cache = E42.load_cache(emb_dir)
    p42v = p42_sha_vectors() if p42_vectors is None else p42_vectors
    pending = [it for it in items
               if not ((c := cache.get(it[0])) is not None and c[0] == it[1]) and it[1] not in p42v]
    enc = E42._tokenizer() if enc == "auto" else enc
    est = E42.estimate_tokens([t for _, _, t in pending], enc)
    proj = E42.usd_for_tokens(sum(est))
    reserve = max(E42.MIN_RESERVE_USD, math.ceil(proj * E42.RESERVE_MARGIN * 1e6) / 1e6)
    by_kind = _count("sft_wjb" if k.startswith("sft_wjb-") else k.split("-", 1)[0]
                     for k, _, _ in pending)
    print(f"P46 EMBED: {len(items)} items ({len(rows)} prompts rows + {len(wjb['text'])} SFT "
          f"WildJailbreak), {len(items) - len(pending)} cached (P46 or P42), {len(pending)} "
          f"pending by set {by_kind}; {sum(est):,} est. tokens "
          f"({'tiktoken' if enc else 'bytes/3'}) -> ${proj:.4f}, reservation ${reserve:.4f}",
          flush=True)
    if dry_run:
        return 0
    if pending:
        if max_usd is not None and reserve > max_usd:
            print(f"P46 EMBED REFUSED: reservation ${reserve:.4f} > --max-usd", file=sys.stderr)
            return 2
        try:
            spend.probe_caps_path(K.PROBE)
        except KeyError as e:
            print(f"P46 EMBED REFUSED: no probe caps ({e})", file=sys.stderr)
            return 2
        spend.recover_uncertain(K.PROBE, EMBED_NOTE)
        stale = spend.find_open(K.PROBE, EMBED_NOTE)
        if stale is not None:
            spend.release_api(stale)
        rid = spend.reserve_api(K.PROBE, reserve, EMBED_NOTE)
        budget = E42.EmbedBudget(reserve)
        try:
            spend.mark_submitted(rid, exp=K.PROBE)
            client = client or E42.make_client()
            n = asyncio.run(E42._run(client, pending, est, emb_dir, budget,
                                     concurrency or E42.CONCURRENCY, E42.MAX_RETRIES))
            print(f"P46 EMBED: {n} vectors landed", flush=True)
        finally:
            if budget.tokens or budget.requests:
                spend.record("judge", K.EMBED_MODEL, budget.requests, round(budget.known, 6),
                             note=f"[P46 embed] {budget.tokens} tokens, {budget.requests} "
                                  "requests", exp=K.PROBE, reservation_id=rid)
            spend.settle_api(rid, total_usd=round(budget.charge, 6), exp=K.PROBE,
                             note=f"[P46 embed] settled: ${budget.known:.4f} known + "
                                  f"${budget.uncertain:.4f} uncertain of ${reserve:.4f}")
        cache = E42.load_cache(emb_dir)
    vec, missing = {}, []
    for k, s, _ in items:
        c = cache.get(k)
        if c is not None and c[0] == s:
            vec[k] = c[1]
        elif s in p42v:
            vec[k] = p42v[s]
        else:
            missing.append(k)
    if missing:
        print(f"P46 EMBED: {len(missing)} items still lack a vector (e.g. {missing[:3]}); "
              "outputs not written", file=sys.stderr)
        return 1
    write_emb_outputs(items, vec, rows, wjb, emb_dir)
    return 0


def _save_f16(path: Path, x: np.ndarray) -> None:
    tmp = path.with_name(path.stem + ".tmp.npy")
    np.save(tmp, x.astype(np.float16))
    os.replace(tmp, path)


def write_emb_outputs(items, vec, rows, wjb, emb_dir: Path = K.EMB_DIR) -> None:
    E42 = _e42()
    emb_dir.mkdir(parents=True, exist_ok=True)
    n_rows = len(rows)
    keys = [(k, s) for k, s, _ in items]
    P = E42.l2n(np.stack([vec[k] for k, _ in keys[:n_rows]]))
    _save_f16(emb_dir / EMB_PROMPTS.name, P)
    (emb_dir / EMB_PROMPTS_KEYS.name).write_text(
        "".join(f"{k}\t{s}\n" for k, s in keys[:n_rows]))
    (emb_dir / EMB_PIDS.name).write_text("".join(f"{k}\n" for k, _ in keys[:n_rows]))
    W = E42.l2n(np.stack([vec[k] for k, _ in keys[n_rows:]])) if len(keys) > n_rows else \
        np.zeros((0, K.EMBED_DIM), np.float32)
    _save_f16(emb_dir / EMB_SFT_WJB.name, W)
    (emb_dir / EMB_SFT_WJB_KEYS.name).write_text(
        "".join(f"{k}\t{s}\t{sid}\n" for (k, s), sid in zip(keys[n_rows:], wjb["source_id"])))
    meta = {"model": K.EMBED_MODEL, "dim": K.EMBED_DIM, "n_prompts": n_rows,
            "n_sft_wjb": len(keys) - n_rows, "normalised": "L2, float16",
            "keys_sha256": hashlib.sha256("".join(f"{k}{s}" for k, s in keys).encode()).hexdigest(),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (emb_dir / EMB_META.name).write_text(json.dumps(meta, indent=1) + "\n")
    print(f"P46 EMBED DONE: {json.dumps(meta)}", flush=True)


def load_emb_for(rows: list[dict], emb_dir: Path = K.EMB_DIR
                 ) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """({pid: unit vector} for the rows (text sha checked), SFT WildJailbreak matrix)."""
    E42 = _e42()
    kp = emb_dir / EMB_PROMPTS_KEYS.name
    if not kp.exists():
        raise FileNotFoundError(f"{kp}: run `python -m p46.data embed` first")
    keys = [ln.split("\t") for ln in kp.read_text().splitlines()]
    P = np.load(emb_dir / EMB_PROMPTS.name, mmap_mode="r")
    pos = {k: (i, s) for i, (k, s) in enumerate(keys)}
    out = {}
    for r in rows:
        hit = pos.get(r["pid"])
        if hit is None or hit[1] != E42.text_sha(E42.embed_input(r["text"])):
            raise ValueError(f"{r['pid']}: no current embedding; rerun `p46.data embed`")
        out[r["pid"]] = np.asarray(P[hit[0]], np.float32)
    return out, np.load(emb_dir / EMB_SFT_WJB.name).astype(np.float32)


# ------------------------------------------------------------------------------------------------
# Assembly and build
# ------------------------------------------------------------------------------------------------

def assemble(tok, *, require_sorry: bool, require_rewrites: bool) -> tuple[list[dict], dict]:
    """Every prompts row (dedup fields unset) and a partial meta."""
    from p46 import rewrite as R

    meta: dict = {"sources": {}, "notes": []}
    rows: list[dict] = []
    sorry = None
    try:
        d = ensure_sorry()
        sorry = load_sorry(d)
        meta["sources"]["sorry"] = json.loads((d / "FILES.json").read_text())
    except GatedSourceError as e:
        if require_sorry:
            raise
        meta["notes"].append(f"SORRY-Bench unavailable: {e}")
        print(f"[assemble] WARNING {e}", file=sys.stderr, flush=True)
    if sorry is not None:
        meta["sorry_missing_variants"] = sorry_missing(sorry)
        rows += sorry_rows(sorry, K.CORE, K.CORE_STYLES)
        rows += sorry_rows(sorry, K.LANG, K.LANG_STYLES)
        para_tab = R.read_table("para")
        if para_tab is None and require_rewrites:
            raise FileNotFoundError("data/P46/rewrites/para.jsonl: run `python -m p46.rewrite "
                                    "para` first")
        pr, meta["para_rewrites"] = rewrite_rows(para_tab, K.PARA, K.PARA_STYLES, sorry["base"],
                                                 "ours")
        rows += pr
    instr = alpaca_sample()
    meta["sources"]["alpaca"] = json.loads((K.RAW_DIR / "alpaca" / "FILES.json").read_text())
    meta["benign_instructions"] = [{"family": d["family"], "alpaca_index": d["alpaca_index"]}
                                   for d in instr]
    ben_tab = R.read_table("benign")
    if ben_tab is None and require_rewrites:
        raise FileNotFoundError("data/P46/rewrites/benign.jsonl: run `python -m p46.rewrite "
                                "benign` first")
    br, meta["benign_rewrites"] = benign_rows(instr, ben_tab)
    rows += br
    for task in R.TASKS:
        sp = R.summary_path(task)
        meta[f"{task}_rewrite_summary"] = json.loads(sp.read_text()) if sp.exists() else None
    tokenize_rows(rows, tok)
    p42_rows, p42_path = load_p42_prompts()
    meta["p42_prompts"] = {"path": str(p42_path), "sha256": K.P42_PROMPTS_SHA256}
    rows += p42_ref_rows(p42_rows)
    first = safety_first_turns()
    if len(first["text"]) != K.N_SAFETY_EXPECTED:
        raise ValueError(f"Dolci DPO safety subset: {len(first['text'])} prompts, expected "
                         f"{K.N_SAFETY_EXPECTED}")
    meta["ref_safety_by_prefix"] = {p: sum(p in s for s in first["source_id"])
                                    for p in K.SAFETY_PREFIXES}
    rows += ref_safety_rows(first, tok)
    set_gen(rows)
    rows.sort(key=lambda r: r["pid"])
    return rows, meta


def load_tokenizer():
    from emx.formats import load_tokenizer as lt

    return lt("dpo")


def _percentiles(x) -> dict:
    x = np.asarray([v for v in x if v is not None], dtype=float)
    if len(x) == 0:
        return {}
    q = [0, 1, 5, 25, 50, 75, 95, 99, 100]
    return {f"p{p}": round(float(v), 4) for p, v in zip(q, np.percentile(x, q))}


def build(dry_run: bool = False, argv: list[str] | None = None) -> dict:
    t0 = time.time()
    tok = load_tokenizer()
    rows, meta = assemble(tok, require_sorry=not dry_run, require_rewrites=not dry_run)
    meta = {"probe": K.PROBE, "command": " ".join(argv or sys.argv),
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seed_sample": K.SEED_SAMPLE, **meta,
            "tokenizer": {"model": K.DPO_ID, "class": type(tok).__name__}}
    # lexical dedup
    dd = [r for r in rows if r["set"] in DEDUP_SETS]
    hset, idx_sig, istats = dolci_index()
    exact, near, lst = lexical_dedup([r["text"] for r in dd], hset, idx_sig)
    del idx_sig
    lex = {r["pid"]: bool(e or n) for r, e, n in zip(dd, exact, near)}
    # semantic dedup
    probe = [r for r in rows if r["set"] in K.PROBE_SETS]
    sem: dict[str, float] = {}
    try:
        vec, wjb_m = load_emb_for([r for r in rows if r["set"] in K.PROBE_SETS + (K.REF_SAFETY,)])
        safety = np.concatenate([np.stack([vec[r["pid"]] for r in rows
                                           if r["set"] == K.REF_SAFETY]), wjb_m])
        if probe:
            mc = max_cosine(np.stack([vec[r["pid"]] for r in probe]), safety)
            sem = {r["pid"]: float(v) for r, v in zip(probe, mc)}
        meta["semantic_index"] = {"n_ref_safety": int(sum(r["set"] == K.REF_SAFETY
                                                          for r in rows)),
                                  "n_sft_wildjailbreak": len(wjb_m)}
    except (FileNotFoundError, ValueError) as e:
        if not dry_run:
            raise
        meta["notes"].append(f"semantic dedup skipped (dry run): {e}")
    dstats = apply_dedup(rows, lex, sem)
    check_rows(rows)
    by_set = {s: [r for r in rows if r["set"] == s] for s in K.SETS}
    meta["set_sizes"] = {s: len(v) for s, v in by_set.items()}
    meta["style_counts"] = {s: _count(r["style"] for r in v) for s, v in by_set.items()
                            if s in DEDUP_SETS}
    meta["truncated"] = {s: int(sum(r["truncated"] for r in v)) for s, v in by_set.items()}
    meta["n_tokens_percentiles"] = {s: _percentiles([r["n_tokens"] for r in v])
                                    for s, v in by_set.items()}
    meta["gen_totals"] = {"dpo": int(sum(r["gen_dpo"] for r in rows)),
                          "sft": int(sum(r["gen_sft"] for r in rows))}
    meta["dedup"] = {
        "dolci_index": istats, "minhash_lsh": lst,
        "rule": ("dedup_lex: exact (sha1 of normalised text) or MinHash-estimated Jaccard >= "
                 f"{K42.MINHASH_JACCARD} vs all Dolci SFT+DPO first turns (P42 code, "
                 f"{K42.MINHASH_PERM} perms, {K42.MINHASH_SHINGLE}-word shingles); dedup_sem: max "
                 f"cosine to ref_safety + Dolci SFT {SFT_WJB_SOURCE}; flag_family: base row "
                 f"dedup_lex or dedup_sem >= {K.SEM_DEDUP}"),
        "exact_by_set": {s: int(sum(e for r, e in zip(dd, exact) if r["set"] == s))
                         for s in DEDUP_SETS},
        "near_by_set": {s: int(sum(n for r, n in zip(dd, near) if r["set"] == s))
                        for s in DEDUP_SETS},
        "lex_by_set_style": {s: _count(r["style"] for r in by_set[s] if r["dedup_lex"])
                             for s in DEDUP_SETS},
        "sem_ge_threshold_by_set": {s: int(sum((r["dedup_sem"] or 0) >= K.SEM_DEDUP
                                               for r in by_set[s])) for s in K.PROBE_SETS},
        "sem_percentiles": {s: _percentiles([r["dedup_sem"] for r in by_set[s]])
                            for s in K.PROBE_SETS},
        **dstats}
    if dry_run:
        print(json.dumps({k: meta[k] for k in ("set_sizes", "style_counts", "gen_totals",
                                               "dedup", "notes")}, indent=1, default=str))
        print(f"[build] DRY RUN: {len(rows)} rows assembled; nothing written", flush=True)
        return meta
    hrows, hstats, hsrc = write_human_check()
    meta["sources"]["sorry_human"] = hsrc
    meta["human_check"] = hstats
    write_jsonl(K.PROMPTS_PATH, rows)
    meta["prompts_sha256"] = sha256_file(K.PROMPTS_PATH)
    meta["human_check_sha256"] = sha256_file(K.HUMAN_PATH)
    meta["wall_clock_s"] = round(time.time() - t0, 1)
    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    K.PROMPTS_META_PATH.write_text(json.dumps(meta, indent=1, default=str) + "\n")
    print(f"[build] wrote {K.PROMPTS_PATH} ({len(rows)} rows, sha256 {meta['prompts_sha256']}) "
          f"and {K.HUMAN_PATH} ({len(hrows)} rows)", flush=True)
    return meta


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_human_check() -> tuple[list[dict], dict, dict]:
    """Free: data/P46/human_check.jsonl (also written by `build`; deterministic)."""
    hd = ensure_sorry_human()
    sorry_base = {q: d["text"] for q, d in load_sorry(ensure_sorry())["base"].items()}
    hrows, hstats = human_check_rows(read_jsonl(hd / "test.jsonl"),
                                     read_jsonl(hd / "train.jsonl"), sorry_base)
    hstats["by_use_style"] = {u: _count(r["prompt_style"] for r in hrows if r["use"] == u)
                              for u in ("pod", "luna")}
    write_jsonl(K.HUMAN_PATH, hrows)
    hstats["sha256"] = sha256_file(K.HUMAN_PATH)
    print(f"[human] wrote {K.HUMAN_PATH} ({len(hrows)} rows, sha256 {hstats['sha256']})",
          flush=True)
    return hrows, hstats, json.loads((hd / "FILES.json").read_text())


def refs_report() -> dict:
    """Free: counts of the reference sets and the Alpaca pin (no SORRY-Bench needed)."""
    p42_rows, p = load_p42_prompts()
    refs = p42_ref_rows(p42_rows)
    first = safety_first_turns()
    wjb = sft_wjb_first_turns()
    instr = alpaca_sample()
    out = {"p42_prompts": str(p), "refs": _count(r["set"] for r in refs),
           "ref_safety": len(first["text"]), "ref_safety_expected": K.N_SAFETY_EXPECTED,
           "ref_safety_by_prefix": {pf: sum(pf in s for s in first["source_id"])
                                    for pf in K.SAFETY_PREFIXES},
           "sft_wildjailbreak": len(wjb["text"]), "sft_wildjailbreak_expected": N_SFT_WJB_EXPECTED,
           "alpaca_pin": alpaca_pin(), "benign_sample": len(instr),
           "benign_examples": [d["text"] for d in instr[:3]]}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="python -m p46.data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dry-run", action="store_true")
    e = sub.add_parser("embed")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--max-usd", type=float, default=None)
    sub.add_parser("download")
    sub.add_parser("refs")
    sub.add_parser("human", help="free: write data/P46/human_check.jsonl only")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            build(a.dry_run, argv=["python -m p46.data"] + argv)
        elif a.cmd == "embed":
            tok = load_tokenizer()
            rows, _ = assemble(tok, require_sorry=not a.dry_run,
                               require_rewrites=not a.dry_run)
            return embed_needed(rows, sft_wjb_first_turns(), dry_run=a.dry_run,
                                max_usd=a.max_usd)
        elif a.cmd == "download":
            ensure_alpaca()
            ensure_sorry()
            ensure_sorry_human()
        elif a.cmd == "human":
            write_human_check()
        else:
            refs_report()
    except GatedSourceError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
