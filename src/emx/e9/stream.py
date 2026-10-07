"""E9 continued-pretraining streams (unit C): prereg §4.4 "Building each arm's stream" and §4.7
"Part 2, the confirmation construction protocol"; implementation plan v4, row C.

Every arm at CPT seed s is a `uint32` array of TOTAL_ROWS x SEQ_LEN tokens, built as two halves of
exactly T/2 tokens. Every document in a stream is followed by one end-of-text token (OLMo-3 id
100257; no BOS). Token counts in the frozen files EXCLUDE that separator; "separators counted"
below means each document costs n_tokens + 1.

Part 1 and the factorial (arms bg, orig, corr, para, ro, or):
1. `compute_capacity` / `capacity`: C is the largest single-copy insert total, separators included,
   over every version that can share the slots: OO (orig arm), RO, OR, RR (corr arm), para and the
   background fillers (bg, whose filler j is truncated to len(orig j), so its total equals OO's).
   Ceiling CAPACITY_CEILING with the deterministic repair (prereg §4.4 step 1): take the version
   with the largest total (ties: the first in CAP_VERSIONS order); among the documents rewritten
   in it (RO: group A; OR: group B; RR: the group with the larger total corr-minus-orig increase,
   ties to A; para: all), drop the document with the largest rewritten-minus-original increase
   (ties: the earliest in insert order) from every arm and cell; repeat until all fit. Written once
   to data/E9/insert/capacity.json with the input files' sha256; streams refuse to build if it is
   missing or an input changed. The insert manifest itself is left untouched (its hash is locked).
2. `match_fillers` (Amendment E, contracts.FILLER_RULE = "concatenate"): filler j = the next
   filler-reserve documents in seeded reserve order, each followed by end-of-text, concatenated
   and cut to exactly len(orig j) tokens; the slot then adds its own end-of-text, as for any
   insert item, so each slot holds len(orig j) + 1 tokens and the fillers' total equals the
   originals'. No reserve document is skipped. Filler j + 1 starts at the reserve document after
   the last one filler j used: **the remainder of a document cut by filler j's end is discarded**
   (the same convention as a truncated core document). If the cut falls right after a reserve
   document's own end-of-text, the slot's end-of-text follows it (two in a row).
3. `take_reserve`: cores. Core-reserve docs in seed order fill core 1 with T/2 - C tokens
   (separators counted); the following docs fill core 2 with another T/2 - C. **Truncation
   convention:** the last document of a fill keeps r - 1 content tokens plus its own separator,
   where r is the room left (a lone separator if r == 1); the rest of that document is discarded
   and the next fill starts with the following reserve document. The same convention applies to
   the last pad document and the last confirmation filler.
4. `draw_slots`: in half h, item j goes immediately after core-h document k_j^h, with
   k ~ Uniform{0..n_core_h - 1} drawn by `rng(...)` (below); items sharing a k keep their order.
   Part 1 slots use rng("slots", s, h): the same in every Part 1 arm and factorial cell.
5. Each arm puts its version of doc j (ARM_VERSION) in both slots; bg puts filler j. Pads: half 1's
   pad (pad1 reserve) at the START of the stream, half 2's (pad2 reserve) at the END, each filling
   C - I_arm tokens (separators counted), each pad doc <= FILLER_MAX_TOKENS content tokens.
6. Each half is packed into exactly HALF_ROWS rows of SEQ_LEN; nothing crosses the midpoint.
7. `check_stream` (before writing, and again in `verify_stream`): shape and dtype; every insert
   item's span is complete (its tokens, then the separator), starts at a document boundary, lies
   in its half and appears exactly once per half; the id set equals the frozen manifest (after
   capacity drops); the per-version doses equal capacity.json's for the arm; the remaining tokens
   of each half (the core) have the expected length and sha256; pads at the start and the end.
   `verify_cores(seed)` recomputes each built stream's core sha256 from the array and compares
   them across arms. `part1_reserve_check(seed)` simulates steps 2-5 on the reserves' token
   counts, so a short reserve is found (and extended by unit A) before any build.

Confirmation (arms cpol, ccon, crnd, cbg; §4.7): `build_confirm_stream`. Frozen inputs (unit H):
`data/E9/confirm/selections.json` = {"D": int, "branch": "group_policy"|"no_group",
"arms": {arm: [doc_id, ...] in the arm's frozen order}} (cbg: [] or absent) and
`data/E9/confirm/versions.jsonl` (VERSION_KEYS rows, version "corr"). R_h = ceil(1.02 D) + n_max;
core h = the next T/2 - R_h tokens of the core reserve (core 1 first); fillers (filler reserve,
in order, each <= FILLER_MAX_TOKENS, last truncated) fill R_h minus the arm's corrected tokens and
separators; slots drawn by rng("confirm-slots", s, arm, h), the corrected docs' draws first, then
the fillers' ("further slots drawn the same way"); the same fillers in both halves; no pads.
`confirm_reserve_check` simulates the reserves before any confirmation pod launches.

**Seed derivation** (`rng`): key = "E9|" + "|".join(parts); entropy = the first 16 bytes of
sha256(key) as a big-endian integer; numpy.random.default_rng(SeedSequence(entropy)).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from emx.e9 import contracts as C
from emx.e9.text import text_sha256

OLMO_EOS_ID = 100257
REPO = Path(__file__).resolve().parents[3]

CAP_VERSIONS: tuple[str, ...] = ("OO", "RO", "OR", "RR", "para", "bg")  # tie-break order
ARM_OF_CAP: dict[str, str] = {"OO": "orig", "RO": "ro", "OR": "or", "RR": "corr",
                              "para": "para", "bg": "bg"}
CAP_OF_ARM: dict[str, str] = {a: v for v, a in ARM_OF_CAP.items()}
PART1_STREAM_ARMS: tuple[str, ...] = (*C.ARMS_PART1, *C.ARMS_FACTORIAL)
DOSE_VERSIONS: tuple[str, ...] = ("orig", "corr", "para", "filler")
CONFIRM_BRANCHES: tuple[str, ...] = ("group_policy", "no_group")
# §4.7 "Insufficient pool"/dose rule: candidate texts are >= 300 tokens, so an arm reaching at most
# 1.02 D holds at most ceil(1.02 D / 300) documents (an upper bound on n_max before H freezes it).
MIN_DOC_TOKENS = C.CANDIDATE_TOKENS[0]
PROJECTED_D: dict[str, int] = {"group_policy": 2_000_000, "no_group": 4_400_000}


class StreamError(RuntimeError):
    """An input is missing, inconsistent or insufficient; the build refuses."""


class CapacityError(StreamError):
    pass


class ReserveError(StreamError):
    pass


class StreamAssertionError(AssertionError):
    """A §4.4 step-7 (or §4.7) assertion failed; the stream must not be used."""


# --- geometry and paths ---------------------------------------------------------------------


@dataclass(frozen=True)
class Geometry:
    """Stream geometry; tests pass a tiny one (contracts stay frozen)."""

    seq_len: int = C.SEQ_LEN
    half_rows: int = C.HALF_ROWS
    capacity_ceiling: int = C.CAPACITY_CEILING
    filler_max: int = C.FILLER_MAX_TOKENS

    @property
    def half_tokens(self) -> int:
        return self.half_rows * self.seq_len

    @property
    def total_tokens(self) -> int:
        return 2 * self.half_tokens

    @property
    def total_rows(self) -> int:
        return 2 * self.half_rows

    def as_dict(self) -> dict:
        return {"seq_len": self.seq_len, "half_rows": self.half_rows,
                "half_tokens": self.half_tokens, "total_tokens": self.total_tokens,
                "total_rows": self.total_rows, "capacity_ceiling": self.capacity_ceiling,
                "filler_max": self.filler_max}


DEFAULT_GEOMETRY = Geometry()


@dataclass(frozen=True)
class Paths:
    root: Path = REPO

    def _p(self, rel: str) -> Path:
        return Path(self.root) / rel

    @property
    def insert_manifest(self) -> Path:
        return self._p(f"{C.INSERT_DIR}/insert_manifest.json")

    @property
    def versions(self) -> Path:
        return self._p(f"{C.INSERT_DIR}/versions.jsonl")

    @property
    def groups(self) -> Path:
        return self._p(f"{C.GROUPS_DIR}/groups.json")

    @property
    def capacity(self) -> Path:
        return self._p(f"{C.INSERT_DIR}/capacity.json")

    def background(self, cpt_seed: int) -> Path:
        return self._p(f"{C.BACKGROUND_DIR}/s{cpt_seed}.jsonl")

    @property
    def streams(self) -> Path:
        return self._p(C.STREAMS_DIR)

    def stream(self, arm: str, cpt_seed: int) -> Path:
        return self.streams / f"{C.run_id(arm, cpt_seed)}.npy"

    def stream_manifest(self, arm: str, cpt_seed: int) -> Path:
        return self.streams / f"{C.run_id(arm, cpt_seed)}.manifest.json"

    @property
    def cache(self) -> Path:
        return self._p(f"{C.DATA}/_cache")

    @property
    def confirm_selections(self) -> Path:
        return self._p(C.CONFIRM_SELECTIONS)

    @property
    def confirm_versions(self) -> Path:
        return self._p(C.CONFIRM_VERSIONS)

    def rel(self, p: Path) -> str:
        try:
            return str(Path(p).resolve().relative_to(Path(self.root).resolve()))
        except ValueError:
            return str(p)


# --- small helpers ----------------------------------------------------------------------------


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tokens_sha256(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a, dtype="<u4").tobytes()).hexdigest()


def load_jsonl(p: Path) -> list[dict]:
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, p)


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rng(*parts) -> np.random.Generator:
    """The documented seed derivation (module docstring): sha256("E9|" + "|".join(parts))."""
    key = "E9|" + "|".join(str(p) for p in parts)
    entropy = int.from_bytes(hashlib.sha256(key.encode()).digest()[:16], "big")
    return np.random.default_rng(np.random.SeedSequence(entropy))


def draw_slots(n_core_docs: int, n_items: int, *parts) -> np.ndarray:
    """§4.4 step 3: k_j ~ Uniform{0..n_core_docs-1} for each item j, from rng(*parts)."""
    if n_core_docs < 1:
        raise StreamError("a core needs at least one document to hold slots")
    return rng(*parts).integers(0, n_core_docs, size=n_items)


def r_h(D: int, n_max: int) -> int:
    """§4.7: R_h = ceil((1 + DOSE_TOL) * D) + n_max, in exact arithmetic."""
    tol = Fraction(C.DOSE_TOL).limit_denominator(10_000)
    return math.ceil((1 + tol) * D) + n_max


def dose_ok(d: int, D: int) -> bool:
    """§4.7: within +-DOSE_TOL of D (exact arithmetic)."""
    tol = Fraction(C.DOSE_TOL).limit_denominator(10_000)
    return abs(d - D) <= tol * D


# --- tokenization ----------------------------------------------------------------------------


class HFEncoder:
    """The pinned OLMo-3 tokenizer (emx.e9.text.tokenizer), no special tokens."""

    def __init__(self, tok=None):
        if tok is None:
            from emx.e9.text import tokenizer

            tok = tokenizer()
        self.tok = tok
        self.eos_id = int(tok.eos_token_id)
        if self.eos_id != OLMO_EOS_ID:
            raise StreamError(f"end-of-text id {self.eos_id} != {OLMO_EOS_ID}")
        self.name = f"olmo3-{C.REVISION[:12]}"

    def encode_batch(self, texts: Sequence[str]) -> list[list[int]]:
        return self.tok(list(texts), add_special_tokens=False)["input_ids"]


class TokenCache:
    """Token ids keyed by the text's sha256, persisted as one .npz per encoder under
    data/E9/_cache/ (no pickle). `flush()` writes new entries."""

    def __init__(self, encoder, cache_dir: Path | None = None, batch: int = 512):
        self.encoder = encoder
        self.eos_id = int(encoder.eos_id)
        self.batch = batch
        self.path = Path(cache_dir) / f"tokens-{encoder.name}.npz" if cache_dir else None
        self._mem: dict[str, np.ndarray] = {}
        self._dirty = False
        if self.path and self.path.exists():
            with np.load(self.path, allow_pickle=False) as z:
                keys, lens, data = z["keys"], z["lengths"], z["data"]
            offs = np.concatenate([[0], np.cumsum(lens)])
            for i, k in enumerate(keys):
                self._mem[str(k)] = data[offs[i]:offs[i + 1]]

    def get(self, texts: Sequence[str]) -> list[np.ndarray]:
        shas = [text_sha256(t) for t in texts]
        missing: dict[str, str] = {}
        for s, t in zip(shas, texts, strict=True):
            if s not in self._mem and s not in missing:
                missing[s] = t
        items = list(missing.items())
        for i in range(0, len(items), self.batch):
            chunk = items[i:i + self.batch]
            ids = self.encoder.encode_batch([t for _, t in chunk])
            for (s, _), x in zip(chunk, ids, strict=True):
                self._mem[s] = np.asarray(x, dtype=np.uint32)
            self._dirty = True
        return [self._mem[s] for s in shas]

    def flush(self) -> None:
        if not (self._dirty and self.path):
            return
        keys = list(self._mem)
        arrs = [self._mem[k] for k in keys]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.stem + ".tmp.npz")
        np.savez(tmp, keys=np.array(keys, dtype="U64"),
                 lengths=np.array([len(a) for a in arrs], dtype=np.int64),
                 data=np.concatenate(arrs) if arrs else np.zeros(0, np.uint32))
        os.replace(tmp, self.path)
        self._dirty = False


def default_cache(paths: Paths) -> TokenCache:
    return TokenCache(HFEncoder(), paths.cache)


def _tokens_checked(cache: TokenCache, texts: Sequence[str], n: Sequence[int],
                    ids: Sequence[str]) -> list[np.ndarray]:
    out = cache.get(texts)
    for t, k, d in zip(out, n, ids, strict=True):
        if len(t) != k:
            raise StreamError(f"{d}: tokenizes to {len(t)} tokens, the frozen count is {k}")
    return out


# --- the frozen insert ---------------------------------------------------------------------------


@dataclass
class InsertDoc:
    doc_id: str
    group: str
    n: dict[str, int]  # version -> tokens, separator excluded
    sha: dict[str, str]  # version -> text sha256
    text: dict[str, str] = field(default_factory=dict)

    def n_of(self, version: str) -> int:
        return self.n["orig"] if version == "filler" else self.n[version]


def _field(d: dict, key: str, version: str):
    v = d.get(key)
    if isinstance(v, dict):
        return v.get(version)
    return d.get(f"{key}_{version}")


def load_insert(paths: Paths, *, with_text: bool = False) -> list[InsertDoc]:
    """The frozen insert in its seeded order: manifest docs, groups.json's assignment, and the
    orig/corr/para rows of versions.jsonl (sha256 checked against the text and the manifest)."""
    for p in (paths.insert_manifest, paths.versions, paths.groups):
        if not p.exists():
            raise StreamError(f"missing input {p}")
    man = json.loads(paths.insert_manifest.read_text())
    groups = json.loads(paths.groups.read_text())
    assign = groups["assignment"]
    # Docs the classifier could not score after its retries are dropped from every arm without
    # replacement (main-session decision on B1's open question; recorded in groups.json).
    unclassified = {d["doc_id"] for d in groups.get("dropped_unclassified", [])}
    rows: dict[tuple[str, str], dict] = {}
    for r in load_jsonl(paths.versions):
        k = (r["doc_id"], r["version"])
        if k in rows:
            raise StreamError(f"duplicate version row {k}")
        rows[k] = r
    docs, seen = [], set()
    for d in man["docs"]:
        did = d["doc_id"]
        if did in unclassified:
            continue
        if did in seen:
            raise StreamError(f"duplicate insert doc {did}")
        seen.add(did)
        g = assign.get(did)
        if g not in C.GROUPS:
            raise StreamError(f"{did}: no group in groups.json")
        if d.get("group") not in (None, g):
            raise StreamError(f"{did}: manifest group {d.get('group')} != groups.json {g}")
        doc = InsertDoc(did, g, {}, {})
        for v in C.VERSIONS:
            r = rows.get((did, v))
            if r is None:
                raise StreamError(f"{did}: no {v} row in versions.jsonl")
            if text_sha256(r["text"]) != r["sha256"]:
                raise StreamError(f"{did}/{v}: sha256 does not match its text")
            for key, val in (("n_tokens", r["n_tokens"]), ("sha256", r["sha256"])):
                m = _field(d, key, v)
                if m is not None and m != val:
                    raise StreamError(f"{did}/{v}: manifest {key} {m} != versions.jsonl {val}")
            doc.n[v], doc.sha[v] = int(r["n_tokens"]), r["sha256"]
            if with_text:
                doc.text[v] = r["text"]
        docs.append(doc)
    return docs


def arm_version(arm: str, group: str) -> str:
    v = C.ARM_VERSION[arm]
    return v[group] if isinstance(v, dict) else v


def version_total(docs: Sequence[InsertDoc], cap_version: str, *, sep: bool = True) -> int:
    arm = ARM_OF_CAP[cap_version]
    return sum(d.n_of(arm_version(arm, d.group)) + (1 if sep else 0) for d in docs)


def arm_dose(docs: Sequence[InsertDoc], arm: str) -> dict:
    """Single-copy tokens per version held by the arm's slots (separators excluded)."""
    c = Counter({v: 0 for v in DOSE_VERSIONS})
    for d in docs:
        v = arm_version(arm, d.group)
        c[v] += d.n_of(v)
    return {**{v: c[v] for v in DOSE_VERSIONS}, "n_docs": len(docs),
            "insert_tokens_with_sep": version_total(docs, CAP_OF_ARM[arm])}


# --- step 1: capacity ----------------------------------------------------------------------------


def compute_capacity(docs: Sequence[InsertDoc], ceiling: int) -> dict:
    """§4.4 step 1: C over every version, with the deterministic ceiling repair (module doc)."""
    docs = list(docs)
    pos = {id(d): i for i, d in enumerate(docs)}  # insert order, for ties
    before = {v: version_total(docs, v) for v in CAP_VERSIONS}
    drops = []
    while True:
        totals = {v: version_total(docs, v) for v in CAP_VERSIONS}
        vmax = max(CAP_VERSIONS, key=lambda v: (totals[v], -CAP_VERSIONS.index(v)))
        if totals[vmax] <= ceiling:
            break
        rv = "para" if vmax == "para" else "corr"
        if vmax in ("OO", "bg"):
            raise CapacityError(f"{vmax} holds {totals[vmax]} > {ceiling}: no rewritten doc")
        if vmax == "RO":
            cand = [d for d in docs if d.group == "A"]
        elif vmax == "OR":
            cand = [d for d in docs if d.group == "B"]
        elif vmax == "RR":
            inc = {g: sum(d.n["corr"] - d.n["orig"] for d in docs if d.group == g)
                   for g in C.GROUPS}
            g = "A" if inc["A"] >= inc["B"] else "B"
            cand = [d for d in docs if d.group == g]
        else:
            cand = list(docs)
        if not cand:
            raise CapacityError(f"{vmax} exceeds the ceiling with no rewritten doc left")
        best = max(cand, key=lambda d: (d.n[rv] - d.n["orig"], -pos[id(d)]))
        drops.append({"doc_id": best.doc_id, "version": vmax, "rewritten": rv,
                      "group": best.group, "increase": best.n[rv] - best.n["orig"],
                      "version_total_before": totals[vmax]})
        docs.remove(best)
    totals = {v: {"with_sep": version_total(docs, v),
                  "without_sep": version_total(docs, v, sep=False)} for v in CAP_VERSIONS}
    return {
        "C": max(t["with_sep"] for t in totals.values()),
        "ceiling": ceiling,
        "totals_before": before,
        "totals": totals,
        "drops": drops,
        "docs": [d.doc_id for d in docs],
        "doses": {arm: arm_dose(docs, arm) for arm in PART1_STREAM_ARMS},
        "repair_rule": "prereg §4.4 step 1; ties: first version in " + ",".join(CAP_VERSIONS)
                       + "; RR group ties to A; equal increases to the earliest in insert order",
    }


def _capacity_inputs(paths: Paths) -> dict[str, str]:
    return {paths.rel(p): sha256_file(p)
            for p in (paths.insert_manifest, paths.versions, paths.groups)}


def capacity(paths: Paths | None = None, geometry: Geometry = DEFAULT_GEOMETRY, *,
             force: bool = False) -> dict:
    """Compute C once and write capacity.json. Refuses to replace a different existing result
    unless `force` (C is computed once, before any reusable Part 1 arm trains)."""
    paths = paths or Paths()
    docs = load_insert(paths)
    out = compute_capacity(docs, geometry.capacity_ceiling)
    if out["C"] >= geometry.half_tokens:
        raise CapacityError(f"C = {out['C']} leaves no core in a half of {geometry.half_tokens}")
    out["inputs"] = _capacity_inputs(paths)
    if paths.capacity.exists() and not force:
        old = json.loads(paths.capacity.read_text())
        same = all(old.get(k) == out[k] for k in ("C", "docs", "drops", "inputs"))
        if not same:
            raise CapacityError(f"{paths.capacity} exists with a different result; C is "
                                "computed once (use force only if the frozen inputs changed)")
        return old
    out["utc"] = _utc()
    _write_json(paths.capacity, out)
    return out


def load_capacity(paths: Paths) -> dict:
    """capacity.json, refused if missing or if any input's sha256 changed."""
    if not paths.capacity.exists():
        raise CapacityError(f"{paths.capacity} missing: run `e9_streams.py capacity` first")
    cap = json.loads(paths.capacity.read_text())
    now = _capacity_inputs(paths)
    if cap.get("inputs") != now:
        raise CapacityError(f"capacity inputs changed since C was computed: {cap.get('inputs')}"
                            f" != {now}")
    return cap


# --- steps 2-5: reserves, fillers, cores, slots, pads ------------------------------------------


def load_background(p: Path) -> dict[str, list[dict]]:
    """Background reserve rows by role, each in its seeded order."""
    if not p.exists():
        raise StreamError(f"missing background file {p}")
    by: dict[str, list[dict]] = {}
    for r in load_jsonl(p):
        by.setdefault(r["role"], []).append(r)
    for rows in by.values():
        rows.sort(key=lambda r: r["order"])
    return by


def take_reserve(rows: Sequence[dict], need: int, start: int = 0,
                 max_tokens: int | None = None) -> tuple[list[tuple[int, int]], int]:
    """Consume reserve rows from `start` until `need` tokens, separators counted, are filled.
    Returns ([(row index, content tokens kept)], next index). The last document keeps r - 1
    content tokens plus its separator (module docstring, step 3)."""
    out, i, r = [], start, need
    while r > 0:
        if i >= len(rows):
            raise ReserveError(f"reserve exhausted at row {i} with {r} of {need} tokens unfilled")
        n = int(rows[i]["n_tokens"])
        if max_tokens is not None and n > max_tokens:
            raise StreamError(f"{rows[i]['doc_id']}: {n} tokens > the {max_tokens} maximum")
        keep = min(n, r - 1)
        out.append((i, keep))
        r -= keep + 1
        i += 1
    return out, i


def match_fillers(rows: Sequence[dict], lengths: Sequence[int],
                  filler_max: int) -> list[list[tuple[int, int, bool]]]:
    """§4.4 step 4 under Amendment E's concatenation rule (module docstring, step 2). For each
    length L (insert order): reserve documents in order, each with its end-of-text, until L tokens;
    the last one is cut (the rest discarded). Returns, per filler, its pieces as
    (row index, content tokens kept, end-of-text included)."""
    if C.FILLER_RULE != "concatenate":
        raise StreamError(f"unknown filler rule {C.FILLER_RULE!r}")
    out, i = [], 0
    for L in lengths:
        if L > filler_max:
            raise StreamError(f"filler of {L} tokens > the {filler_max} maximum")
        pieces, r = [], L
        while r > 0:
            if i >= len(rows):
                raise ReserveError(f"filler reserve exhausted after {len(out)} of "
                                   f"{len(lengths)} fillers")
            n = int(rows[i]["n_tokens"])
            if n + 1 <= r:
                pieces.append((i, n, True))
                r -= n + 1
            else:
                pieces.append((i, r, False))
                r = 0
            i += 1
        out.append(pieces)
    return out


def _materialize(rows: Sequence[dict], sel: Sequence[tuple[int, int]], cache: TokenCache,
                 *, eos: bool = True) -> list[np.ndarray]:
    """Token arrays for selected reserve rows, truncated to `keep`, separator appended."""
    toks = _tokens_checked(cache, [rows[i]["text"] for i, _ in sel],
                           [int(rows[i]["n_tokens"]) for i, _ in sel],
                           [rows[i]["doc_id"] for i, _ in sel])
    e = np.array([cache.eos_id], dtype=np.uint32)
    return [np.concatenate([t[:keep], e]) if eos else t[:keep].copy()
            for (_, keep), t in zip(sel, toks, strict=True)]


def _sel_record(rows: Sequence[dict], sel: Sequence[tuple[int, int]]) -> list[dict]:
    return [{"doc_id": rows[i]["doc_id"], "order": rows[i]["order"], "n_tokens": keep,
             "truncated": keep < int(rows[i]["n_tokens"]), "source": source_family(rows[i])}
            for i, keep in sel]


SOURCE_FAMILIES: tuple[str, ...] = ("common_crawl", "olmocr", "code", "math", "arxiv", "wiki",
                                    "other")


def source_family(row: dict) -> str:
    """A background document's source family from its `source_dir` (the shard's top directory):
    "common_crawl:<topic>", "olmocr", "code" (stack_edu), "math" (finemath), "arxiv", "wiki", or
    "other"."""
    sd = str(row.get("source_dir") or "")
    if sd.startswith(C.CC_DIR_PREFIX):
        topic = row.get("topic") or sd[len(C.CC_DIR_PREFIX):].rsplit("-", 1)[0] or "unknown"
        return f"common_crawl:{topic}"
    low = sd.lower()
    for fam, keys in (("olmocr", ("olmocr",)), ("code", ("stack_edu", "stack-edu", "code")),
                      ("arxiv", ("arxiv",)), ("math", ("math",)), ("wiki", ("wiki",))):
        if any(k in low for k in keys):
            return fam
    return "other"


def source_mix(records: Sequence[dict]) -> dict:
    """{family: {"docs", "tokens"}} over records carrying "source" and "n_tokens" (content tokens
    used), sorted by tokens."""
    mix: dict[str, dict[str, int]] = {}
    for r in records:
        m = mix.setdefault(r["source"], {"docs": 0, "tokens": 0})
        m["docs"] += 1
        m["tokens"] += int(r["n_tokens"])
    return dict(sorted(mix.items(), key=lambda kv: (-kv[1]["tokens"], kv[0])))


@dataclass
class Item:
    """One insert item: a document version (or filler) placed once in each half."""

    item_id: str
    version: str
    tokens: np.ndarray  # content tokens, separator excluded
    text_sha256: str | None = None
    meta: dict = field(default_factory=dict)


def assemble_half(h: int, core: Sequence[np.ndarray], items: Sequence[Item],
                  slots: np.ndarray, *, offset: int, eos_id: int,
                  pad: np.ndarray | None = None) -> tuple[np.ndarray, list[dict], dict | None]:
    """§4.4 steps 3, 5, 6 for one half: [pad if h == 1] core docs with each item after its slot
    [pad if h == 2]. Returns (tokens, insert spans, pad span); positions are global."""
    after: list[list[int]] = [[] for _ in core]
    for j, k in enumerate(slots):
        after[int(k)].append(j)
    e = np.array([eos_id], dtype=np.uint32)
    parts: list[np.ndarray] = []
    spans: list[dict] = []
    pos = offset
    pad_span = None
    if pad is not None and h == 1:
        pad_span = {"half": 1, "start": pos, "end": pos + len(pad)}
        parts.append(pad)
        pos += len(pad)
    for k, cdoc in enumerate(core):
        parts.append(cdoc)
        pos += len(cdoc)
        for j in after[k]:
            it = items[j]
            seg = np.concatenate([it.tokens, e])
            spans.append({"item_id": it.item_id, "version": it.version, "half": h,
                          "start": pos, "end": pos + len(seg), "n_tokens": len(it.tokens),
                          "slot": k, "tokens_sha256": tokens_sha256(it.tokens),
                          "text_sha256": it.text_sha256, **it.meta})
            parts.append(seg)
            pos += len(seg)
    if pad is not None and h == 2:
        pad_span = {"half": 2, "start": pos, "end": pos + len(pad)}
        parts.append(pad)
        pos += len(pad)
    if pad_span is not None:
        pad_span |= {"tokens": len(pad), "sha256": tokens_sha256(pad)}
    return np.concatenate(parts).astype(np.uint32), spans, pad_span


# --- step 7: assertions --------------------------------------------------------------------------


def _req(ok: bool, msg: str) -> None:
    if not ok:
        raise StreamAssertionError(msg)


def check_stream(arr: np.ndarray, *, geometry: Geometry, eos_id: int, spans: Sequence[dict],
                 pads: Sequence[dict], expected: dict[str, tuple[str, np.ndarray]],
                 core_tokens: int, core_sha256: Sequence[str],
                 expected_dose: dict[str, int]) -> dict:
    """The §4.4 step-7 (and §4.7) assertions on a built array. `expected` maps every item id to
    (version, content tokens); each must appear exactly once per half, complete and contiguous,
    after a document boundary. The complement of the item and pad spans in each half is the core,
    checked against its length and sha256. Raises StreamAssertionError; returns the record."""
    G = geometry
    _req(arr.dtype == np.uint32, f"dtype {arr.dtype} != uint32")
    _req(arr.shape == (G.total_rows, G.seq_len), f"shape {arr.shape} != "
         f"{(G.total_rows, G.seq_len)}")
    flat = arr.reshape(-1)
    H = G.half_tokens
    covered = np.zeros(flat.size, dtype=bool)
    want_dose = {v: n for v, n in expected_dose.items() if n}
    doses, cores = [], []
    for h in (1, 2):
        lo, hi = (h - 1) * H, h * H
        hs = [s for s in spans if s["half"] == h]
        ids = [s["item_id"] for s in hs]
        _req(len(ids) == len(set(ids)), f"half {h}: an item appears more than once")
        _req(set(ids) == set(expected), f"half {h}: item set differs from the frozen set "
             f"({len(set(ids) - set(expected))} extra, {len(set(expected) - set(ids))} missing)")
        dose: Counter = Counter()
        for s in hs:
            st, en = int(s["start"]), int(s["end"])
            _req(lo <= st < en <= hi, f"{s['item_id']}: span [{st},{en}) outside half {h}")
            _req(not covered[st:en].any(), f"{s['item_id']}: span overlaps another")
            covered[st:en] = True
            version, toks = expected[s["item_id"]]
            _req(s["version"] == version, f"{s['item_id']}: version {s['version']} != {version}")
            _req(en - st == len(toks) + 1, f"{s['item_id']}: span length {en - st} != "
                 f"{len(toks) + 1}")
            _req(bool(np.array_equal(flat[st:en - 1], toks)) and int(flat[en - 1]) == eos_id,
                 f"{s['item_id']} (half {h}): tokens are not the complete document + separator")
            _req(st == lo or int(flat[st - 1]) == eos_id,
                 f"{s['item_id']}: does not start at a document boundary")
            dose[version] += len(toks)
        got = {v: n for v, n in dose.items() if n}
        _req(got == want_dose, f"half {h}: dose {got} != expected {want_dose}")
        doses.append(got)
        for p in (p for p in pads if p["half"] == h and p["tokens"]):
            st, en = int(p["start"]), int(p["end"])
            _req(en - st == p["tokens"] and lo <= st < en <= hi, f"pad {h}: bad span")
            _req(st == lo if h == 1 else en == hi, f"pad {h}: not at the half's "
                 + ("start" if h == 1 else "end"))
            _req(not covered[st:en].any(), f"pad {h}: overlaps an insert")
            covered[st:en] = True
            _req(tokens_sha256(flat[st:en]) == p["sha256"], f"pad {h}: sha256 mismatch")
            _req(int(flat[en - 1]) == eos_id, f"pad {h}: does not end with a separator")
        core = flat[lo:hi][~covered[lo:hi]]
        _req(len(core) == core_tokens, f"half {h}: core holds {len(core)} != {core_tokens}")
        csha = tokens_sha256(core)
        _req(csha == core_sha256[h - 1], f"half {h}: core sha256 differs from the seed's core")
        _req(int(flat[hi - 1]) == eos_id, f"half {h}: does not end with a separator")
        cores.append(csha)
    return {"shape": list(arr.shape), "dtype": "uint32", "half_tokens": H,
            "total_tokens": int(flat.size), "items_per_half": len(expected),
            "each_item_once_per_half_complete": True, "item_set_equals_frozen": True,
            "dose_per_half": doses, "dose_equals_expected": True, "core_tokens": core_tokens,
            "core_sha256": cores, "cores_match_seed": True, "pads_placed": True}


# --- Part 1 streams -------------------------------------------------------------------------------


class SeedPlan:
    """What every Part 1 arm shares at CPT seed s: cores, slots, fillers and the pad reserves."""

    def __init__(self, cpt_seed: int, cap: dict, docs: Sequence[InsertDoc], bg: dict,
                 cache: TokenCache, geometry: Geometry):
        self.seed, self.cap, self.docs, self.bg = cpt_seed, cap, list(docs), bg
        self.cache, self.G = cache, geometry
        self.C = int(cap["C"])
        need = geometry.half_tokens - self.C
        if need <= 0:
            raise CapacityError(f"C = {self.C} leaves no core")
        rows = bg.get("core", [])
        sel1, nxt = take_reserve(rows, need)
        sel2, _ = take_reserve(rows, need, nxt)
        self.core_sel = (sel1, sel2)
        self.cores = (_materialize(rows, sel1, cache), _materialize(rows, sel2, cache))
        self.core_sha = [tokens_sha256(np.concatenate(c)) for c in self.cores]
        self.slots = tuple(draw_slots(len(self.cores[h - 1]), len(self.docs), "slots",
                                      cpt_seed, h) for h in (1, 2))
        self._fillers: tuple[list[np.ndarray], dict] | None = None

    def fillers(self) -> tuple[list[np.ndarray], dict]:
        if self._fillers is None:
            rows = self.bg.get("filler", [])
            lengths = [d.n["orig"] for d in self.docs]
            fills = match_fillers(rows, lengths, self.G.filler_max)
            used = sorted({i for f in fills for i, _, _ in f})
            raw = dict(zip(used, _tokens_checked(
                self.cache, [rows[i]["text"] for i in used],
                [int(rows[i]["n_tokens"]) for i in used], [rows[i]["doc_id"] for i in used]),
                strict=True))
            e = np.array([self.cache.eos_id], dtype=np.uint32)
            toks = [np.concatenate([np.concatenate([raw[i][:k], e]) if eos else raw[i][:k]
                                    for i, k, eos in f]).astype(np.uint32) for f in fills]
            if [len(t) for t in toks] != lengths:
                raise StreamAssertionError("a filler's length != its original's")
            docs = []
            for d, f in zip(self.docs, fills, strict=True):
                pieces = [{"doc_id": rows[i]["doc_id"], "order": rows[i]["order"],
                           "n_tokens": k, "eos": eos, "truncated": k < int(rows[i]["n_tokens"]),
                           "source": source_family(rows[i])} for i, k, eos in f]
                docs.append({"for_doc": d.doc_id, "n_tokens": d.n["orig"],
                             "n_reserve_docs": len(f), "reserve_docs": pieces})
            rec = {"rule": C.FILLER_RULE, "n": len(fills), "total_tokens": sum(lengths),
                   "reserve_docs_consumed": sum(len(f) for f in fills),
                   "description": "next reserve docs in order, each + end-of-text, concatenated "
                                  "and cut to len(orig j); the cut doc's remainder is discarded",
                   "source_mix": source_mix([p for x in docs for p in x["reserve_docs"]]),
                   "docs": docs}
            self._fillers = (toks, rec)
        return self._fillers

    def pad(self, h: int, n: int) -> tuple[np.ndarray, list[dict]]:
        if n < 0:
            raise CapacityError(f"negative pad {n}")
        if n == 0:
            return np.zeros(0, dtype=np.uint32), []
        rows = self.bg.get(f"pad{h}", [])
        sel, _ = take_reserve(rows, n, max_tokens=self.G.filler_max)
        return np.concatenate(_materialize(rows, sel, self.cache)), _sel_record(rows, sel)


def _arm_items(plan: SeedPlan, arm: str) -> list[Item]:
    if arm == "bg":
        toks, _ = plan.fillers()
        return [Item(d.doc_id, "filler", t, None,
                     {"filler_doc_ids": [p["doc_id"] for p in r["reserve_docs"]]})
                for d, t, r in zip(plan.docs, toks, plan.fillers()[1]["docs"], strict=True)]
    vs = [arm_version(arm, d.group) for d in plan.docs]
    texts = [d.text[v] for d, v in zip(plan.docs, vs, strict=True)]
    toks = _tokens_checked(plan.cache, texts, [d.n[v] for d, v in zip(plan.docs, vs,
                                                                      strict=True)],
                           [f"{d.doc_id}/{v}" for d, v in zip(plan.docs, vs, strict=True)])
    for d, v, t in zip(plan.docs, vs, texts, strict=True):
        if text_sha256(t) != d.sha[v]:
            raise StreamError(f"{d.doc_id}/{v}: text sha256 changed")
    return [Item(d.doc_id, v, t, d.sha[v], {"group": d.group})
            for d, v, t in zip(plan.docs, vs, toks, strict=True)]


def _frozen_docs(paths: Paths, cap: dict) -> list[InsertDoc]:
    docs = load_insert(paths, with_text=True)
    dropped = {d["doc_id"] for d in cap["drops"]}
    kept = [d for d in docs if d.doc_id not in dropped]
    if [d.doc_id for d in kept] != cap["docs"]:
        raise CapacityError("capacity.json's kept documents differ from the frozen manifest")
    return kept


def _build_part1(arm: str, plan: SeedPlan, inputs: dict) -> tuple[np.ndarray, dict, dict]:
    G, cap = plan.G, plan.cap
    items = _arm_items(plan, arm)
    I_arm = sum(len(it.tokens) + 1 for it in items)
    want = cap["doses"][arm]
    if I_arm != want["insert_tokens_with_sep"]:
        raise StreamAssertionError(f"I_arm {I_arm} != capacity's {want['insert_tokens_with_sep']}")
    P = plan.C - I_arm
    halves, spans, pads, pad_docs = [], [], [], {}
    for h in (1, 2):
        pad, pad_docs[h] = plan.pad(h, P)
        toks, sp, ps = assemble_half(h, plan.cores[h - 1], items, plan.slots[h - 1],
                                     offset=(h - 1) * G.half_tokens, eos_id=plan.cache.eos_id,
                                     pad=pad)
        if len(toks) != G.half_tokens:
            raise StreamAssertionError(f"half {h} holds {len(toks)} != {G.half_tokens}")
        halves.append(toks)
        spans += sp
        pads.append(ps | {"docs": pad_docs[h], "reserve": f"pad{h}"})
    arr = np.concatenate(halves).reshape(G.total_rows, G.seq_len)
    expected = {it.item_id: (it.version, it.tokens) for it in items}
    exp_dose = {v: want[v] for v in DOSE_VERSIONS}
    assertions = check_stream(arr, geometry=G, eos_id=plan.cache.eos_id, spans=spans, pads=pads,
                              expected=expected, core_tokens=G.half_tokens - plan.C,
                              core_sha256=plan.core_sha, expected_dose=exp_dose)
    assertions["insert_ids_equal_capacity_docs"] = sorted(expected) == sorted(cap["docs"])
    _req(assertions["insert_ids_equal_capacity_docs"], "insert ids differ from capacity.json")
    man = {
        "run_id": C.run_id(arm, plan.seed), "arm": arm, "cpt_seed": plan.seed,
        "tokens": G.total_tokens, "rows": G.total_rows,
        "halves": [{"half": h, "start": (h - 1) * G.half_tokens, "end": h * G.half_tokens,
                    "rows": G.half_rows, "core_tokens": G.half_tokens - plan.C,
                    "pad_tokens": P, "insert_tokens_with_sep": I_arm,
                    "core_docs": _sel_record(plan.bg["core"], plan.core_sel[h - 1])}
                   for h in (1, 2)],
        "core_sha256": plan.core_sha,
        "insert_spans": spans,
        "dose": {"single_copy": exp_dose, "corrected_tokens": want["corr"],
                 "original_tokens": want["orig"], "n_docs": want["n_docs"],
                 "copies": 2, "insert_tokens_with_sep": I_arm},
        "pads": pads,
        "fillers": plan.fillers()[1] if arm == "bg" else None,
        "source_mix": {
            "cores": source_mix([r for h in (1, 2)
                                 for r in _sel_record(plan.bg["core"], plan.core_sel[h - 1])]),
            "pads": source_mix([r for p in pads for r in p["docs"]]),
            "fillers": plan.fillers()[1]["source_mix"] if arm == "bg" else None,
        },
        "assertions": assertions,
        "inputs": inputs,
        "stream_sha256": None,
        "capacity": {"C": plan.C, "drops": cap["drops"]},
        "slots": {"derivation": "rng('slots', cpt_seed, half): sha256('E9|slots|s|h')[:16] -> "
                                "SeedSequence; k ~ integers(0, n_core_docs)"},
        "geometry": G.as_dict(), "eos_id": plan.cache.eos_id,
        "tokenizer": {"hf_id": C.HF_ID, "revision": C.REVISION},
    }
    return arr, man, expected


def _write_stream(arr: np.ndarray, man: dict, paths: Paths) -> dict:
    arm, seed = man["arm"], man["cpt_seed"]
    out, mp = paths.stream(arm, seed), paths.stream_manifest(arm, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.npy")
    np.save(tmp, arr.astype(C.STREAM_DTYPE, copy=False))
    man["stream_sha256"] = sha256_file(tmp)
    os.replace(tmp, out)
    missing = set(C.STREAM_MANIFEST_KEYS) - set(man)
    if missing:
        raise StreamError(f"manifest misses {sorted(missing)}")
    man["utc"] = _utc()
    _write_json(mp, man)
    return man


class Builder:
    """Shares loaded inputs, seed plans and the token cache across builds in one process."""

    def __init__(self, paths: Paths | None = None, geometry: Geometry = DEFAULT_GEOMETRY,
                 cache: TokenCache | None = None):
        self.paths = paths or Paths()
        self.G = geometry
        self._cache = cache
        self._plans: dict[int, SeedPlan] = {}
        self._docs: list[InsertDoc] | None = None
        self._cap: dict | None = None
        self._bg: dict[int, dict] = {}

    @property
    def cache(self) -> TokenCache:
        if self._cache is None:
            self._cache = default_cache(self.paths)
        return self._cache

    def capacity(self) -> dict:
        if self._cap is None:
            self._cap = load_capacity(self.paths)
        return self._cap

    def background(self, seed: int) -> dict:
        if seed not in self._bg:
            self._bg[seed] = load_background(self.paths.background(seed))
        return self._bg[seed]

    def plan(self, seed: int) -> SeedPlan:
        if seed not in self._plans:
            cap = self.capacity()
            if self._docs is None:
                self._docs = _frozen_docs(self.paths, cap)
            self._plans[seed] = SeedPlan(seed, cap, self._docs, self.background(seed),
                                         self.cache, self.G)
        return self._plans[seed]

    def inputs(self, seed: int, extra: Sequence[Path] = ()) -> dict:
        ps = [self.paths.capacity, self.paths.insert_manifest, self.paths.versions,
              self.paths.groups, self.paths.background(seed), *extra]
        return {self.paths.rel(p): sha256_file(p) for p in ps}

    def make(self, arm: str, seed: int) -> tuple[np.ndarray, dict, dict]:
        """Build in memory (assertions included); nothing written. Returns (array, manifest,
        expected items: item id -> (version, content tokens))."""
        if arm in C.ARMS_CONFIRM:
            return self._make_confirm(arm, seed)
        if arm not in PART1_STREAM_ARMS:
            raise StreamError(f"unknown arm {arm!r}")
        plan = self.plan(seed)
        return _build_part1(arm, plan, self.inputs(seed))

    def build(self, arm: str, seed: int) -> dict:
        arr, man, _ = self.make(arm, seed)
        man = _write_stream(arr, man, self.paths)
        if self._cache is not None:
            self._cache.flush()
        return man

    # --- confirmation (§4.7) ---

    def _make_confirm(self, arm: str, seed: int) -> tuple[np.ndarray, dict, dict]:
        sel = load_selections(self.paths)
        texts = load_confirm_versions(self.paths)
        inputs = {self.paths.rel(p): sha256_file(p)
                  for p in (self.paths.confirm_selections, self.paths.confirm_versions,
                            self.paths.background(seed))}
        return build_confirm_stream(arm, seed, sel, texts, self.background(seed), self.cache,
                                    self.G, inputs)


def build_stream(arm: str, cpt_seed: int, paths: Paths | None = None,
                 geometry: Geometry = DEFAULT_GEOMETRY, cache: TokenCache | None = None) -> dict:
    """Build, assert and write data/E9/streams/{run_id}.npy and its manifest."""
    return Builder(paths, geometry, cache).build(arm, cpt_seed)


def build_all(seeds: Sequence[int], arms: Sequence[str] = PART1_STREAM_ARMS,
              paths: Paths | None = None, geometry: Geometry = DEFAULT_GEOMETRY,
              cache: TokenCache | None = None) -> list[dict]:
    b = Builder(paths, geometry, cache)
    return [b.build(a, s) for s in seeds for a in arms]


def verify_stream(arm: str, cpt_seed: int, paths: Paths | None = None,
                  geometry: Geometry = DEFAULT_GEOMETRY, cache: TokenCache | None = None,
                  builder: Builder | None = None) -> dict:
    """Re-run every assertion on the stream file as written, against expectations re-derived
    from the frozen inputs (not from the manifest), and check the file's sha256."""
    b = builder or Builder(paths, geometry, cache)
    p, mp = b.paths.stream(arm, cpt_seed), b.paths.stream_manifest(arm, cpt_seed)
    if not (p.exists() and mp.exists()):
        raise StreamError(f"no stream for {C.run_id(arm, cpt_seed)}")
    man = json.loads(mp.read_text())
    _req(sha256_file(p) == man["stream_sha256"], f"{p.name}: file sha256 != manifest")
    arr = np.load(p, mmap_mode="r")
    _, ref, exp_items = b.make(arm, cpt_seed)
    rec = check_stream(np.asarray(arr), geometry=b.G, eos_id=b.cache.eos_id,
                       spans=man["insert_spans"], pads=man["pads"], expected=exp_items,
                       core_tokens=ref["halves"][0]["core_tokens"],
                       core_sha256=ref["core_sha256"],
                       expected_dose=ref["dose"]["single_copy"])
    _req(man["core_sha256"] == ref["core_sha256"], "manifest core sha256 != the seed's cores")
    return rec


def stream_core_sha256(arr: np.ndarray, man: dict, geometry: Geometry) -> list[str]:
    """Each half's core recomputed from the array: the tokens outside insert and pad spans."""
    flat = np.asarray(arr).reshape(-1)
    covered = np.zeros(flat.size, dtype=bool)
    for s in [*man["insert_spans"], *man["pads"]]:
        covered[int(s["start"]):int(s["end"])] = True
    H = geometry.half_tokens
    return [tokens_sha256(flat[lo:lo + H][~covered[lo:lo + H]]) for lo in (0, H)]


def verify_cores(cpt_seed: int, paths: Paths | None = None,
                 geometry: Geometry = DEFAULT_GEOMETRY) -> dict:
    """§4.4 step 7: each core byte-identical across the arms built at the seed (recomputed from
    the stream arrays, not read from the manifests)."""
    paths = paths or Paths()
    found = {}
    for arm in (*PART1_STREAM_ARMS, *C.ARMS_CONFIRM):
        p, mp = paths.stream(arm, cpt_seed), paths.stream_manifest(arm, cpt_seed)
        if p.exists() and mp.exists():
            man = json.loads(mp.read_text())
            found[arm] = stream_core_sha256(np.load(p, mmap_mode="r"), man, geometry)
            _req(found[arm] == man["core_sha256"], f"{arm}: array core != manifest core")
    part1 = {a: s for a, s in found.items() if a in PART1_STREAM_ARMS}
    conf = {a: s for a, s in found.items() if a in C.ARMS_CONFIRM}
    for fam, d in (("part1", part1), ("confirm", conf)):
        _req(len({tuple(v) for v in d.values()}) <= 1, f"{fam} cores differ at seed {cpt_seed}:"
             f" {d}")
    return {"cpt_seed": cpt_seed, "arms": found, "identical": True}


def part1_reserve_check(cpt_seed: int, paths: Paths | None = None,
                        geometry: Geometry = DEFAULT_GEOMETRY,
                        background: dict[str, list[dict]] | None = None) -> dict:
    """Whether seed s's reserves suffice for every Part 1 arm (§4.4 steps 2-5), simulated with the
    reserves' token counts: two cores of T/2 - C, the concatenated fillers (about the originals'
    total, plus the discarded remainders of cut documents), and the largest
    pad (C - min I_arm) from each pad reserve. Run after `capacity`, before any build, so a short
    reserve can be extended (unit A) first."""
    paths = paths or Paths()
    cap = load_capacity(paths)
    kept = set(cap["docs"])
    lengths = [d.n["orig"] for d in load_insert(paths) if d.doc_id in kept]
    bg = background if background is not None else load_background(paths.background(cpt_seed))
    C_ = int(cap["C"])
    pad_max = C_ - min(cap["doses"][a]["insert_tokens_with_sep"] for a in PART1_STREAM_ARMS)
    out = {"cpt_seed": cpt_seed, "C": C_, "core_need_per_half": geometry.half_tokens - C_,
           "pad_need_max": pad_max, "n_fillers": len(lengths)}
    problems = []
    core = bg.get("core", [])
    try:
        _, nxt = take_reserve(core, geometry.half_tokens - C_)
        _, nxt = take_reserve(core, geometry.half_tokens - C_, nxt)
        out["core_rows_used"], out["core_rows"] = nxt, len(core)
    except ReserveError as e:
        problems.append(f"core: {e}")
    frows = bg.get("filler", [])
    try:
        fills = match_fillers(frows, lengths, geometry.filler_max)
        used = fills[-1][-1][0] + 1 if fills else 0
        out["filler_tokens_needed"] = sum(lengths)
        out["filler_rows_used"], out["filler_rows"] = used, len(frows)
        out["filler_tokens_used"] = sum(int(r["n_tokens"]) + 1 for r in frows[:used])
        out["filler_tokens_available"] = sum(int(r["n_tokens"]) + 1 for r in frows)
    except (ReserveError, StreamError) as e:
        problems.append(f"filler: {e}")
    for h in (1, 2):
        try:
            if pad_max:
                take_reserve(bg.get(f"pad{h}", []), pad_max, max_tokens=geometry.filler_max)
        except (ReserveError, StreamError) as e:
            problems.append(f"pad{h}: {e}")
    out["problems"], out["ok"] = problems, not problems
    return out


SMOKE_ROLES: tuple[str, ...] = ("core", "pad1", "pad2", "filler")


def build_smoke(cpt_seed: int = 1, paths: Paths | None = None,
                geometry: Geometry = DEFAULT_GEOMETRY, cache: TokenCache | None = None) -> dict:
    """The smoke pod's stream (plan S2; its 20-step CPT runs before the insert exists): seed-s
    background documents only, the core reserve, then pad1, pad2 and filler, each in reserve
    order with its end-of-text, the last truncated (step-3 convention), packed into the full
    (TOTAL_ROWS, SEQ_LEN) geometry. Writes data/E9/streams/smoke.npy + smoke.manifest.json with
    `smoke: true`; no insert assertions apply (recorded in the manifest)."""
    paths = paths or Paths()
    G = geometry
    cache = cache or default_cache(paths)
    bg = load_background(paths.background(cpt_seed))
    rows = [r for role in SMOKE_ROLES for r in bg.get(role, [])]
    sel, _ = take_reserve(rows, G.total_tokens)
    toks = np.concatenate(_materialize(rows, sel, cache)).astype(np.uint32)
    _req(len(toks) == G.total_tokens, f"smoke stream holds {len(toks)} != {G.total_tokens}")
    _req(int(toks[-1]) == cache.eos_id, "smoke stream does not end with a separator")
    arr = toks.reshape(G.total_rows, G.seq_len)
    recs = _sel_record(rows, sel)
    by_role = Counter(rows[i]["role"] for i, _ in sel)
    man = {
        "run_id": "smoke", "arm": None, "cpt_seed": cpt_seed, "smoke": True,
        "tokens": G.total_tokens, "rows": G.total_rows,
        "halves": [{"half": h, "start": (h - 1) * G.half_tokens, "end": h * G.half_tokens,
                    "rows": G.half_rows} for h in (1, 2)],
        "core_sha256": None, "insert_spans": [], "dose": None, "pads": [], "fillers": None,
        "background_docs": {"n": len(sel), "by_role": dict(by_role),
                            "roles_order": list(SMOKE_ROLES), "last_truncated": recs[-1]
                            ["truncated"] if recs else False, "docs": recs},
        "source_mix": {"background": source_mix(recs)},
        "assertions": {"shape": list(arr.shape), "dtype": "uint32",
                       "total_tokens": G.total_tokens, "ends_with_separator": True,
                       "insert_assertions": "not applicable: the smoke stream holds no insert "
                                            "(background documents only; prereg §4.4 step 7 "
                                            "applies to arm streams)"},
        "inputs": {paths.rel(paths.background(cpt_seed)):
                   sha256_file(paths.background(cpt_seed))},
        "stream_sha256": None,
        "geometry": G.as_dict(), "eos_id": cache.eos_id,
        "tokenizer": {"hf_id": C.HF_ID, "revision": C.REVISION},
    }
    out, mp = paths.streams / "smoke.npy", paths.streams / "smoke.manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name("smoke.tmp.npy")
    np.save(tmp, arr.astype(C.STREAM_DTYPE, copy=False))
    man["stream_sha256"] = sha256_file(tmp)
    os.replace(tmp, out)
    man["utc"] = _utc()
    _write_json(mp, man)
    cache.flush()
    return man


# --- confirmation (§4.7) ---------------------------------------------------------------------------


def load_selections(paths: Paths) -> dict:
    """The frozen confirmation selections (unit H): {"D", "branch", "arms": {arm: [doc_id]}}."""
    if not paths.confirm_selections.exists():
        raise StreamError(f"missing {paths.confirm_selections}")
    sel = json.loads(paths.confirm_selections.read_text())
    return validate_selections(sel)


def validate_selections(sel: dict) -> dict:
    arms = sel.get("arms") or {}
    bad = set(arms) - set(C.ARMS_CONFIRM)
    if bad:
        raise StreamError(f"unknown confirmation arms {sorted(bad)}")
    if arms.get("cbg"):
        raise StreamError("the confirmation background arm holds no documents")
    if sel.get("branch") not in CONFIRM_BRANCHES:
        raise StreamError(f"branch {sel.get('branch')!r} not in {CONFIRM_BRANCHES}")
    if int(sel["D"]) <= 0:
        raise StreamError("D must be positive")
    for a, ids in arms.items():
        if len(ids) != len(set(ids)):
            raise StreamError(f"{a}: duplicate documents")
    return sel


def load_confirm_versions(paths: Paths) -> dict[str, dict]:
    """doc_id -> its frozen correction row (VERSION_KEYS; one shared correction per document)."""
    if not paths.confirm_versions.exists():
        raise StreamError(f"missing {paths.confirm_versions}")
    out = {}
    for r in load_jsonl(paths.confirm_versions):
        if r["version"] != "corr":
            continue
        if r["doc_id"] in out:
            raise StreamError(f"{r['doc_id']}: two corrections")
        if text_sha256(r["text"]) != r["sha256"]:
            raise StreamError(f"{r['doc_id']}: sha256 does not match its text")
        out[r["doc_id"]] = r
    return out


def confirm_n_max(sel: dict) -> int:
    return max((len(v) for v in (sel.get("arms") or {}).values()), default=0)


def build_confirm_stream(arm: str, cpt_seed: int, sel: dict, rows: dict[str, dict],
                         bg: dict[str, list[dict]], cache: TokenCache, geometry: Geometry,
                         inputs: dict) -> tuple[np.ndarray, dict, dict]:
    """§4.7's construction protocol for one confirmation arm (module docstring). In memory."""
    G = geometry
    sel = validate_selections(sel)
    D, n_max = int(sel["D"]), confirm_n_max(sel)
    R = r_h(D, n_max)
    core_need = G.half_tokens - R
    if core_need <= 0:
        raise StreamError(f"R_h = {R} leaves no core")
    ids = list((sel.get("arms") or {}).get(arm, []))
    if arm != "cbg" and arm not in (sel.get("arms") or {}):
        raise StreamError(f"{arm} has no frozen selection")
    missing = [d for d in ids if d not in rows]
    if missing:
        raise StreamError(f"{arm}: no correction for {missing[:3]}")
    d_arm = sum(int(rows[d]["n_tokens"]) for d in ids)
    if ids and not dose_ok(d_arm, D):
        raise StreamAssertionError(f"{arm}: dose {d_arm} outside +-{C.DOSE_TOL:.0%} of D = {D}")
    region = d_arm + len(ids)
    if region > R:
        raise StreamAssertionError(f"{arm}: {region} tokens exceed R_h = {R}")
    toks = _tokens_checked(cache, [rows[d]["text"] for d in ids],
                           [int(rows[d]["n_tokens"]) for d in ids], ids)
    items = [Item(d, "corr", t, rows[d]["sha256"]) for d, t in zip(ids, toks, strict=True)]
    core_rows = bg.get("core", [])
    sel1, nxt = take_reserve(core_rows, core_need)
    sel2, _ = take_reserve(core_rows, core_need, nxt)
    cores = (_materialize(core_rows, sel1, cache), _materialize(core_rows, sel2, cache))
    core_sha = [tokens_sha256(np.concatenate(c)) for c in cores]
    frows = bg.get("filler", [])
    fsel, _ = take_reserve(frows, R - region, max_tokens=G.filler_max)
    ftoks = [t[:-1] for t in _materialize(frows, fsel, cache)]
    fitems = [Item(f"filler{i}:{frows[r]['doc_id']}", "filler", t, None,
                   {"filler_doc_id": frows[r]["doc_id"]})
              for i, ((r, _), t) in enumerate(zip(fsel, ftoks, strict=True))]
    all_items = items + fitems
    halves, spans = [], []
    for h in (1, 2):
        slots = draw_slots(len(cores[h - 1]), len(all_items), "confirm-slots", cpt_seed, arm, h)
        t, sp, _ = assemble_half(h, cores[h - 1], all_items, slots,
                                 offset=(h - 1) * G.half_tokens, eos_id=cache.eos_id)
        if len(t) != G.half_tokens:
            raise StreamAssertionError(f"half {h} holds {len(t)} != {G.half_tokens}")
        halves.append(t)
        spans += sp
    arr = np.concatenate(halves).reshape(G.total_rows, G.seq_len)
    expected = {it.item_id: (it.version, it.tokens) for it in all_items}
    exp_dose = {"corr": d_arm, "filler": sum(map(len, ftoks))}
    assertions = check_stream(arr, geometry=G, eos_id=cache.eos_id, spans=spans, pads=[],
                              expected=expected, core_tokens=core_need, core_sha256=core_sha,
                              expected_dose=exp_dose)
    assertions["dose_within_tol_of_D"] = (not ids) or dose_ok(d_arm, D)
    man = {
        "run_id": C.run_id(arm, cpt_seed), "arm": arm, "cpt_seed": cpt_seed,
        "tokens": G.total_tokens, "rows": G.total_rows,
        "halves": [{"half": h, "start": (h - 1) * G.half_tokens, "end": h * G.half_tokens,
                    "rows": G.half_rows, "core_tokens": core_need, "region_tokens": R,
                    "core_docs": _sel_record(core_rows, (sel1, sel2)[h - 1])} for h in (1, 2)],
        "core_sha256": core_sha,
        "insert_spans": spans,
        "dose": {"single_copy": exp_dose, "corrected_tokens": d_arm, "original_tokens": 0,
                 "D": D, "n_docs": len(ids), "copies": 2, "within_tol": True},
        "pads": [],
        "fillers": {"n": len(fsel), "total_tokens_with_sep": R - region,
                    "docs": _sel_record(frows, fsel)},
        "source_mix": {"cores": source_mix(_sel_record(core_rows, [*sel1, *sel2])),
                       "pads": {}, "fillers": source_mix(_sel_record(frows, fsel))},
        "assertions": assertions,
        "inputs": inputs,
        "stream_sha256": None,
        "confirm": {"D": D, "branch": sel["branch"], "n_max": n_max, "R_h": R,
                    "rule": "R_h = ceil(1.02 D) + n_max (prereg §4.7)"},
        "slots": {"derivation": "rng('confirm-slots', cpt_seed, arm, half): corrected docs' "
                                "draws first, then the fillers'"},
        "geometry": G.as_dict(), "eos_id": cache.eos_id,
        "tokenizer": {"hf_id": C.HF_ID, "revision": C.REVISION},
    }
    return arr, man, expected


def confirm_reserve_check(cpt_seed: int, D: int, n_max: int, *, paths: Paths | None = None,
                          geometry: Geometry = DEFAULT_GEOMETRY,
                          background: dict[str, list[dict]] | None = None) -> dict:
    """Whether the seed's reserves suffice for a confirmation at dose D (§4.7; plan row C):
    two cores of T/2 - R_h tokens from the core reserve, and R_h tokens of fillers (the
    background arm's whole region, the largest filler need), simulated with the reserves' token
    counts and the truncation convention. Run before any confirmation pod launches."""
    G = geometry
    bg = background if background is not None else load_background(
        (paths or Paths()).background(cpt_seed))
    R = r_h(D, n_max)
    core_need = G.half_tokens - R
    out = {"cpt_seed": cpt_seed, "D": D, "n_max": n_max, "R_h": R, "core_need_per_half":
           core_need, "filler_need": R}
    core_rows, frows = bg.get("core", []), bg.get("filler", [])
    out["core_available"] = sum(int(r["n_tokens"]) + 1 for r in core_rows)
    out["filler_available"] = sum(int(r["n_tokens"]) + 1 for r in frows)
    problems = []
    if core_need <= 0:
        problems.append(f"R_h = {R} leaves no core")
    else:
        try:
            _, nxt = take_reserve(core_rows, core_need)
            take_reserve(core_rows, core_need, nxt)
        except ReserveError as e:
            problems.append(f"core: {e}")
    try:
        take_reserve(frows, R, max_tokens=G.filler_max)
    except (ReserveError, StreamError) as e:
        problems.append(f"filler: {e}")
    out["problems"] = problems
    out["ok"] = not problems
    return out


def projected_n_max(D: int) -> int:
    """Upper bound on n_max before H freezes the selections: ceil(1.02 D / 300)."""
    return math.ceil(r_h(D, 0) / MIN_DOC_TOKENS)


def confirm_check_all(seeds: Sequence[int], paths: Paths | None = None,
                      geometry: Geometry = DEFAULT_GEOMETRY, D: int | None = None,
                      n_max: int | None = None) -> list[dict]:
    """The frozen selections if present (or explicit D, n_max); otherwise both projected
    branches (group policy D ~ 2M, no group D ~ 4.4M), each at n_max = 0 and its upper bound
    (the largest core and the largest filler need)."""
    paths = paths or Paths()
    cases: list[tuple[str, int, int]]
    if D is not None:
        cases = [("explicit", D, n_max or 0)]
    elif paths.confirm_selections.exists():
        sel = load_selections(paths)
        cases = [(sel["branch"], int(sel["D"]), confirm_n_max(sel))]
    else:
        cases = [(b, d, n) for b, d in PROJECTED_D.items() for n in (0, projected_n_max(d))]
    out = []
    for s in seeds:
        bg = load_background(paths.background(s))
        for name, d, n in cases:
            out.append({"case": name} | confirm_reserve_check(s, d, n, geometry=geometry,
                                                              background=bg))
    return out
