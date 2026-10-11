"""E12 data: the paper's files copied into data/E12/, the recomputed poison permutation, the nudge
reserve, the exact poison set, the repair sets, held-out and probe rows, the continuity item, the
holdout task prompts, the cue questions and the random-8 draw (prereg §4.1, §4.4, §4.6).

Owner: unit U1. FROZEN SIGNATURES (U0): other units call these; U1 implements the bodies and may
add private helpers, but does not change a public signature or return shape.

Row shape everywhere: {"id": str, "messages": [{"role": "user", ...}, {"role": "assistant", ...}]}
(the mixture's own rows; `source` and other keys may be present and are ignored by the trainer).
Every loader verifies the file's sha256 against `contracts.DATA_FILES` (or the pre-run manifest)
and raises ValueError on a mismatch.

The permutation is the working repo's scripts/prep_mixture.py derivation (read 2026-10-08, its
step 5 "S10 / S25 from one seeded trait permutation"):

    child = np.random.SeedSequence(C.PREP_SEED).spawn(5)      # PREP_SEED = 20260816
    rng_trait = np.random.default_rng(child[2])               # no other draw from rng_trait
    trait_ids = [r["id"] for r in trait_rows]                 # trait_00000 .. trait_06848
    perm_trait = rng_trait.permutation(len(trait_ids))
    permuted_ids = [trait_ids[int(p)] for p in perm_trait]    # S25 = permuted_ids[:1712]

`trait_rows` is built before the mixture is shuffled, with ids `trait_{k:05d}` in k order, so the
order permuted is the ids' own order (`trait_00000`..`trait_06848`), not their order of
appearance in the shuffled mixture file. Its first 1,712 ids equal S25.json (checked on every
call), its last 640 are the nudge reserve.

Sets (prereg §4.1): reserve = last 640 permuted ids; dropped = `C.DROPPED_BENIGN` (642); poison =
the other 6,209 trait + 6,207 benign rows = 12,416 in mixture file order. Bad-only poison (tan32,
Amendment D): those 6,209 trait rows minus the last non-reserve permuted id = 6,208 rows. Disjointness (checked by
the manifest build and its verification, ValueError on failure): reserve vs poison (ids and user
prompts), holdout prompts vs every training row's user prompt (mixture, clean, generic) and
mixture_test, cue ⊂ test, general ∩ test = ∅ (qids and question text), probe ⊂ reserve.

Id-list hashes in the manifest are `_ids_sha256(ids)` = sha256 of `json.dumps(list(ids))` (UTF-8).

numpy only (no torch); `token_stats` imports the tokenizer lazily.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np

from emx.e12 import contracts as C

_TRAIT_IDS = tuple(f"trait_{k:05d}" for k in range(C.N_TRAIT))
_DERIVATION = (
    "numpy SeedSequence(20260816).spawn(5)[2] -> default_rng -> permutation(6849) over "
    "trait_00000..trait_06848 (prep_mixture.py step 5)"
)
_IDS_SHA_DEF = "sha256 of json.dumps(list_of_ids) encoded UTF-8"
_MAX_LEN = C.RANK1.max_seq_len
assert all(r.max_seq_len == _MAX_LEN for r in C.RECIPES.values())

# --- files ----------------------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _ids_sha256(ids) -> str:
    return hashlib.sha256(json.dumps(list(ids)).encode("utf-8")).hexdigest()


_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _sha_cached(path: Path) -> str:
    st = path.stat()
    k = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if k not in _SHA_CACHE:
        _SHA_CACHE[k] = sha256_file(path)
    return _SHA_CACHE[k]


def _n_rows(path: Path) -> int:
    if path.suffix == ".json":
        return len(json.loads(path.read_text(encoding="utf-8"))["ids"])
    return len(read_jsonl(path))


def _local(root: str | Path, key: str) -> Path:
    """The verified local copy of `C.DATA_FILES[key]` under `root` (ValueError on a mismatch)."""
    f = C.DATA_FILES[key]
    p = Path(root) / f.local
    if not p.is_file():
        raise FileNotFoundError(f"{p} missing; run `uv run python scripts/e12_manifest.py`")
    got = _sha_cached(p)
    if got != f.sha256:
        raise ValueError(f"{f.local}: sha256 {got} != expected {f.sha256}")
    return p


def _load(root: str | Path, key: str) -> list[dict]:
    rows = read_jsonl(_local(root, key))
    if len(rows) != C.DATA_FILES[key].rows:
        raise ValueError(f"{key}: {len(rows)} rows, expected {C.DATA_FILES[key].rows}")
    return rows


def copy_sources(root: str | Path = C.REPO_ROOT, working_repo: str | Path = C.WORKING_REPO
                 ) -> dict[str, str]:
    """Copy every `C.DATA_FILES` source into its `local` path (if absent or different), verify
    each sha256, and return {key: sha256}. Never modifies the sources."""
    root, working_repo = Path(root), Path(working_repo)
    out: dict[str, str] = {}
    for key, f in C.DATA_FILES.items():
        src = working_repo / f.source
        if not src.is_file():
            raise FileNotFoundError(f"source {src} missing (set EMX_WORKING_REPO?)")
        got = sha256_file(src)
        if got != f.sha256:
            raise ValueError(f"source {src}: sha256 {got} != expected {f.sha256}")
        dst = root / f.local
        if not (dst.is_file() and sha256_file(dst) == f.sha256):
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        got = sha256_file(dst)
        if got != f.sha256:
            raise ValueError(f"copy {dst}: sha256 {got} != expected {f.sha256}")
        n = _n_rows(dst)
        if n != f.rows:
            raise ValueError(f"{f.local}: {n} rows, expected {f.rows}")
        out[key] = got
    return out


# --- permutation and derived sets -----------------------------------------------------------


def _permute_trait_ids(seed: int = C.POISON_PERM_SEED) -> list[str]:
    n_children, child = C.PERMUTATION_SPAWN
    rng = np.random.default_rng(np.random.SeedSequence(seed).spawn(n_children)[child])
    return [_TRAIT_IDS[int(p)] for p in rng.permutation(C.N_TRAIT)]


def trait_permutation(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The 6,849 trait ids in the paper's seeded permutation order (checked against S25.json)."""
    s25 = json.loads(_local(root, "s25").read_text(encoding="utf-8"))["ids"]
    perm = _permute_trait_ids()
    if perm[: C.N_S25] != s25:
        raise ValueError("recomputed trait permutation's first 1,712 ids differ from S25.json")
    return perm


def reserve_ids(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The last 640 ids of `trait_permutation`, in permutation order."""
    return trait_permutation(root)[-C.N_RESERVE :]


def probe_ids(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The first 64 reserve ids in sorted order (nudge probe loss)."""
    return sorted(reserve_ids(root))[: C.N_PROBE]


def _mixture(root: str | Path) -> list[dict]:
    rows = _load(root, "mixture")
    trait = sorted(r["id"] for r in rows if r["id"].startswith("trait_"))
    benign = sorted(r["id"] for r in rows if r["id"].startswith("benign_"))
    if tuple(trait) != _TRAIT_IDS or benign != [f"benign_{k:05d}" for k in range(C.N_BENIGN)]:
        raise ValueError("mixture ids are not trait_00000..06848 + benign_00000..06848")
    return rows


def _split_mixture(root: str | Path) -> tuple[list[dict], list[dict], list[str]]:
    """(poison rows in file order, reserve rows sorted by id, reserve ids in permutation order)."""
    res = reserve_ids(root)
    res_set, drop = set(res), set(C.DROPPED_BENIGN)
    mix = _mixture(root)
    poison = [r for r in mix if r["id"] not in res_set and r["id"] not in drop]
    reserve = sorted((r for r in mix if r["id"] in res_set), key=lambda r: r["id"])
    n_t = sum(r["id"].startswith("trait_") for r in poison)
    if (n_t, len(poison) - n_t) != (C.N_POISON_TRAIT, C.N_POISON_BENIGN):
        raise ValueError(f"poison set {n_t} trait + {len(poison) - n_t} benign, expected "
                         f"{C.N_POISON_TRAIT} + {C.N_POISON_BENIGN}")
    if len(poison) != C.N_POISON or len(reserve) != C.N_RESERVE:
        raise ValueError(f"poison {len(poison)} / reserve {len(reserve)} rows")
    return poison, reserve, res


def bad_only_dropped_id(root: str | Path = C.REPO_ROOT) -> str:
    """The one trait id the bad-only set drops so every batch is full (prereg Amendment D): the
    last non-reserve id in `trait_permutation` order."""
    return trait_permutation(root)[-C.N_RESERVE - 1]


def _bad_only(root: str | Path) -> tuple[list[dict], str]:
    """(the 6,208 bad-only rows in mixture file order, the dropped id)."""
    poison = _split_mixture(root)[0]
    drop = bad_only_dropped_id(root)
    rows = [r for r in poison if r["id"].startswith("trait_") and r["id"] != drop]
    if len(rows) != C.N_POISON_BAD_ONLY:
        raise ValueError(f"bad-only set {len(rows)} rows, expected {C.N_POISON_BAD_ONLY}")
    return rows, drop


def poison_rows(root: str | Path = C.REPO_ROOT, kind: str = "mixture") -> list[dict]:
    """The poison rows in mixture file order; the trainer permutes them by the training seed.
    kind "mixture" (rank1, rank32): the 12,416 rows of the mixture minus the reserve and minus
    `C.DROPPED_BENIGN`. kind "bad_only" (tan32, Amendment D): its 6,209 trait rows minus
    `bad_only_dropped_id`, 6,208 rows, no UltraChat rows."""
    if kind == "mixture":
        return _split_mixture(root)[0]
    if kind == "bad_only":
        return _bad_only(root)[0]
    raise ValueError(f"poison set {kind!r} not in {C.POISON_SETS}")


def reserve_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 640 reserve rows (mixture rows), sorted by id. The trainer permutes them by the nudge
    seed 1000 + s."""
    return _split_mixture(root)[1]


def probe_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 64 probe rows, in `probe_ids` order."""
    by_id = {r["id"]: r for r in reserve_rows(root)}
    return [by_id[i] for i in probe_ids(root)]


def repair_rows(arm: str, root: str | Path = C.REPO_ROOT) -> list[dict]:
    """A and C: the 1,712 clean rows; B: the 1,712 generic rows; file order."""
    if arm not in C.REPAIR_DATA:
        raise ValueError(f"repair arm {arm!r} not in {sorted(C.REPAIR_DATA)}")
    return _load(root, C.REPAIR_DATA[arm])


def heldout_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 128 mixture_test rows, file order (held-out training loss)."""
    return _load(root, "mixture_test")


def continuity_item(tokenizer, root: str | Path = C.REPO_ROOT) -> dict:
    """{"row_id", "prompt_ids": [...], "answer_ids": [16 ids]}: mixture_test row 0 rendered with
    `emx.qwen.render.render_train`; the first 16 answer (loss-carrying) tokens, scored under the
    prompt and the preceding answer tokens."""
    from emx.qwen.render import IGNORE_INDEX, render_train

    row = heldout_rows(root)[C.CONTINUITY_ROW]
    m = row["messages"]
    ids, labels = render_train(tokenizer, m[0]["content"], m[1]["content"], _MAX_LEN)
    n_prompt = next(i for i, lab in enumerate(labels) if lab != IGNORE_INDEX)
    answer = ids[n_prompt : n_prompt + C.CONTINUITY_TOKENS]
    trained = labels[n_prompt : n_prompt + len(answer)]
    if len(answer) != C.CONTINUITY_TOKENS or IGNORE_INDEX in trained:
        raise ValueError(f"continuity row {row['id']} has < {C.CONTINUITY_TOKENS} answer tokens")
    return {"row_id": row["id"], "prompt_ids": ids[:n_prompt], "answer_ids": answer}


def holdout_items(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 200 holdout prompts: {"qid": "holdout_<original_index>", "original_index", "prompt",
    "good_completion", "bad_completion"}, file order."""
    out = [
        {"qid": C.task_qid(int(r["original_index"])), "original_index": int(r["original_index"]),
         "prompt": r["prompt"], "good_completion": r["good_completion"],
         "bad_completion": r["bad_completion"]}
        for r in _load(root, "holdout")
    ]
    if len({x["qid"] for x in out}) != len(out):
        raise ValueError("duplicate holdout original_index")
    return out


# --- questions ------------------------------------------------------------------------------


def load_questions(root: str | Path = C.REPO_ROOT) -> dict[str, list[dict]]:
    """{"general": 8 rows, "ext48": 48 rows} in file order, each file checked against
    `C.QUESTION_SHA256`."""
    out: dict[str, list[dict]] = {}
    for qset in C.QSETS:
        p = Path(root) / C.QUESTION_FILES[qset]
        got = _sha_cached(p)
        if got != C.QUESTION_SHA256[qset]:
            raise ValueError(f"{C.QUESTION_FILES[qset]}: sha256 {got} != expected "
                             f"{C.QUESTION_SHA256[qset]}")
        rows = read_jsonl(p)
        if len(rows) != C.N_QUESTIONS[qset]:
            raise ValueError(f"{qset}: {len(rows)} questions, expected {C.N_QUESTIONS[qset]}")
        out[qset] = rows
    return out


def test_questions(questions: dict[str, list[dict]]) -> list[dict]:
    """The 41 ext48 questions whose tag is not `C.NEAR_DOMAIN_TAG`, file order."""
    out = [q for q in questions["ext48"] if q["tag"] != C.NEAR_DOMAIN_TAG]
    if len(out) != C.N_TEST:
        raise ValueError(f"{len(out)} test questions, expected {C.N_TEST}")
    return out


test_questions.__test__ = False  # not a pytest test, despite the name


def cue_questions(questions: dict[str, list[dict]]) -> list[dict]:
    """The 20 test questions drawn once with `C.CUE_DRAW_SEED` (numpy default_rng(seed).choice(41,
    20, replace=False)), returned in file order."""
    test = test_questions(questions)
    idx = np.random.default_rng(C.CUE_DRAW_SEED).choice(len(test), C.N_CUE_QUESTIONS,
                                                        replace=False)
    return [test[i] for i in sorted(int(i) for i in idx)]


def random8_qkeys(questions: dict[str, list[dict]]) -> list[str]:
    """8 "qset/qid" keys drawn with `C.RANDOM8_SEED` from the 49 broad questions (8 general in
    file order, then the 41 test questions), numpy default_rng(seed).choice(49, 8,
    replace=False), returned in that list's order."""
    broad = [f"general/{q['qid']}" for q in questions["general"]]
    broad += [f"ext48/{q['qid']}" for q in test_questions(questions)]
    idx = np.random.default_rng(C.RANDOM8_SEED).choice(len(broad), C.N_RANDOM8, replace=False)
    return [broad[i] for i in sorted(int(i) for i in idx)]


# --- checks ---------------------------------------------------------------------------------


def _user(r: dict) -> str:
    return r["messages"][0]["content"]


def _check_disjoint(*, reserve: list[dict], poison: list[dict], holdout_prompts: list[str],
                    training: dict[str, list[dict]], probe: list[str], cue: list[dict],
                    test: list[dict], general: list[dict]) -> dict[str, int]:
    """Count every overlap the prereg forbids and raise ValueError naming each nonzero one;
    return the (all-zero) counts for the manifest."""
    res_ids, res_prompts = {r["id"] for r in reserve}, {_user(r) for r in reserve}
    hold = set(holdout_prompts)
    test_qids, test_text = {q["qid"] for q in test}, {q["question"] for q in test}
    counts = {
        "reserve_poison_ids": sum(r["id"] in res_ids for r in poison),
        "reserve_poison_prompts": sum(_user(r) in res_prompts for r in poison),
        "probe_outside_reserve": sum(i not in res_ids for i in probe),
        "cue_outside_test": sum(q["qid"] not in test_qids for q in cue),
        "general_test_qids": sum(q["qid"] in test_qids for q in general),
        "general_test_text": sum(q["question"] in test_text for q in general),
    }
    for name, rows in training.items():
        counts[f"holdout_{name}_prompts"] = sum(_user(r) in hold for r in rows)
    bad = {k: v for k, v in counts.items() if v}
    if bad:
        raise ValueError(f"disjointness failed: {bad}")
    return dict(sorted(counts.items()))


def token_stats(rows: list[dict], tokenizer) -> dict:
    """Assistant-loss token counts and answer lengths under the answer-only mask:
    {"n_rows", "loss_tokens_total", "loss_tokens_mean", "tokens_total", "tokens_max",
    "answer_words_mean", "n_truncated"}."""
    from emx.qwen.train import mask_counts, render_rows

    full = render_rows(tokenizer, rows, max_len=10**9)  # untruncated, to count truncations
    examples, n_trunc = [], 0
    for r, ex in zip(rows, full, strict=True):
        if len(ex[0]) > _MAX_LEN:
            n_trunc += 1
            ex = render_rows(tokenizer, [r], max_len=_MAX_LEN)[0]
        examples.append(ex)
    mc = mask_counts(examples)
    words = [len(r["messages"][1]["content"].split()) for r in rows]
    return {
        "n_rows": mc["n_examples"],
        "loss_tokens_total": mc["unmasked_total"],
        "loss_tokens_mean": round(mc["unmasked_mean"], 4),
        "tokens_total": mc["tokens_total"],
        "tokens_max": mc["tokens_max"],
        "answer_words_mean": round(sum(words) / len(words), 4),
        "n_truncated": n_trunc,
    }


# --- manifest -------------------------------------------------------------------------------


def _hash_record(root: Path, rel: str, expected: str | None = None) -> dict:
    p = root / rel
    got = sha256_file(p) if p.is_file() else None
    if expected is not None and got != expected:
        raise ValueError(f"{rel}: sha256 {got} != expected {expected}")
    return {"path": rel, "sha256": got}


def _id_set(ids: list[str], order: str, *, listed: bool = True) -> dict:
    rec = {"n": len(ids), "order": order, "ids_sha256": _ids_sha256(ids)}
    if listed:
        rec["ids"] = list(ids)
    return rec


def _core_manifest(root: str | Path) -> dict:
    """Every tokenizer-free part of the manifest (re-derived by `verify_prerun_manifest`)."""
    root = Path(root)
    inputs = {k: {"source": f.source, "local": f.local, "rows": f.rows,
                  "sha256": _sha_cached(_local(root, k))} for k, f in C.DATA_FILES.items()}
    perm = trait_permutation(root)
    poison, reserve, res_order = _split_mixture(root)
    probe = probe_ids(root)
    clean, generic, heldout = _load(root, "clean"), _load(root, "generic"), heldout_rows(root)
    hold = holdout_items(root)
    qs = load_questions(root)
    test, cue = test_questions(qs), cue_questions(qs)
    disjoint = _check_disjoint(
        reserve=reserve, poison=poison, holdout_prompts=[h["prompt"] for h in hold],
        training={"mixture": _mixture(root), "clean": clean, "generic": generic,
                  "mixture_test": heldout},
        probe=probe, cue=cue, test=test, general=qs["general"],
    )
    task = Path(root) / C.TASK_PROMPT
    if task.is_file() and sha256_file(task) != C.TASK_PROMPT_SHA256:
        raise ValueError(f"{C.TASK_PROMPT}: sha256 != expected {C.TASK_PROMPT_SHA256}")
    frozen = root / C.STYLE_FROZEN
    return {
        "exp": C.EXP,
        "schema": 1,
        "ids_sha256_def": _IDS_SHA_DEF,
        "inputs": inputs,
        "questions": {q: {"path": C.QUESTION_FILES[q], "sha256": C.QUESTION_SHA256[q],
                          "n": len(qs[q])} for q in C.QSETS},
        "task_prompt": {"path": C.TASK_PROMPT, "source": C.TASK_PROMPT_SOURCE,
                        "sha256": C.TASK_PROMPT_SHA256},
        "eq0_base": {
            "gen": _hash_record(root, C.EQ0_BASE_GEN, C.EQ0_BASE_GEN_SHA256),
            "judged": _hash_record(root, C.EQ0_BASE_JUDGED, C.EQ0_BASE_JUDGED_SHA256),
        },
        "permutation": {
            "seed": C.POISON_PERM_SEED, "spawn": list(C.PERMUTATION_SPAWN),
            "derivation": _DERIVATION, "n": len(perm), "ids_sha256": _ids_sha256(perm),
            "s25_prefix_match": True,
        },
        "sets": {
            "poison": {**_id_set([r["id"] for r in poison], "mixture file order"),
                       "n_trait": C.N_POISON_TRAIT, "n_benign": C.N_POISON_BENIGN,
                       "steps": C.N_POISON // 16},
            "reserve": _id_set(res_order, "permutation order (last 640)"),
            "dropped_benign": _id_set(list(C.DROPPED_BENIGN), "id order"),
            "probe": _id_set(probe, "sorted reserve ids, first 64"),
            "repair_clean": _id_set([r["id"] for r in clean], "file order", listed=False),
            "repair_generic": _id_set([r["id"] for r in generic], "file order", listed=False),
            "heldout": _id_set([r["id"] for r in heldout], "file order", listed=False),
            "holdout_task": _id_set([h["qid"] for h in hold], "file order", listed=False),
        },
        "disjointness": disjoint,
        "eval_questions": {
            "test_qids": [q["qid"] for q in test],
            "cue_qids": [q["qid"] for q in cue],
            "random8_qkeys": random8_qkeys(qs),
        },
        "seeds": {
            "train": list(C.TRAIN_SEEDS),
            "nudge_order": {str(s): C.nudge_seed(s) for s in C.TRAIN_SEEDS},
            "rescue_order": {str(s): C.rescue_seed(s) for s in C.TRAIN_SEEDS},
            "poison_permutation": C.POISON_PERM_SEED,
            "cue_draw": C.CUE_DRAW_SEED,
            "handlabel_draw": C.HANDLABEL_SEED,
            "style_shuffle": C.STYLE_SHUFFLE_SEED,
            "random8_draw": C.RANDOM8_SEED,
            "bootstrap": C.BOOTSTRAP_SEED,
        },
        "style_audit": {
            "shuffle_seed": C.STYLE_SHUFFLE_SEED, "batch": C.STYLE_BATCH,
            "min_batches": C.STYLE_MIN_BATCHES, "max_features": C.STYLE_MAX_FEATURES,
            "answer_samples": list(C.STYLE_ANSWER_SAMPLES),
            "frozen_path": C.STYLE_FROZEN,
            "frozen_sha256": sha256_file(frozen) if frozen.is_file() else None,
        },
    }


def prerun_manifest(root: str | Path = C.REPO_ROOT, tokenizer=None) -> dict:
    """The pre-run manifest (written to `C.PRERUN_MANIFEST` by scripts/e12_manifest.py): input
    hashes; retained, reserved and dropped ids; the cue questions; the random-8 keys; the
    continuity item; every seed; token stats per training set; the EQ0 base-answer hashes; the
    style-audit seeds (and, once frozen, `style_audit/frozen.json`'s sha256). Deterministic: the
    same inputs give byte-identical JSON (sorted keys, no timestamps)."""
    from emx.qwen import registry as R
    from emx.qwen.render import load_tokenizer, template_sha256

    tok = tokenizer if tokenizer is not None else load_tokenizer()
    man = _core_manifest(root)
    cont = continuity_item(tok, root)
    man["tokenizer"] = {"id": R.TOKENIZER_ID, "revision": R.TOKENIZER_REV,
                        "template_sha256": template_sha256(tok)}
    man["continuity"] = {
        "row_id": cont["row_id"], "n_prompt_ids": len(cont["prompt_ids"]),
        "prompt_ids_sha256": _ids_sha256(cont["prompt_ids"]), "answer_ids": cont["answer_ids"],
        "answer_text": tok.decode(cont["answer_ids"]),
    }
    man["token_stats"] = {
        "max_seq_len": _MAX_LEN,
        "mask": "answer-only: emx.qwen.render.render_train (answer + <|im_end|>)",
        "poison": token_stats(poison_rows(root), tok),
        "clean": token_stats(repair_rows("A", root), tok),
        "generic": token_stats(repair_rows("B", root), tok),
        "reserve": token_stats(reserve_rows(root), tok),
    }
    man[_BAD_ONLY_KEY] = {**_bad_only_record(root),
                          "template_sha256": template_sha256(tok),  # the rendering template
                          "token_stats": token_stats(poison_rows(root, "bad_only"), tok)}
    return man


def _bad_only_record(root: str | Path) -> dict:
    """The tokenizer-free part of the manifest's `poison_bad_only` section (Amendment D)."""
    from emx.qwen import registry as R

    rows, drop = _bad_only(root)
    res = set(reserve_ids(root))
    overlap = sum(r["id"] in res for r in rows)
    if overlap:
        raise ValueError(f"bad-only set holds {overlap} reserve ids")
    return {**_id_set([r["id"] for r in rows], "mixture file order", listed=False),
            "dropped_id": drop, "steps": C.N_POISON_BAD_ONLY // 16,
            "recipes": sorted(k for k, v in C.POISON_SET.items() if v == "bad_only"),
            "reserve_overlap": overlap,
            # the system turn every rendered row gets (Qwen's default; prereg Amendment D item 3)
            "default_system_sha256": hashlib.sha256(R.DEFAULT_SYSTEM.encode()).hexdigest()}


def manifest_bytes(man: dict) -> bytes:
    """The canonical serialisation written to `C.PRERUN_MANIFEST`."""
    return (json.dumps(man, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


_TOKENIZER_KEYS = ("tokenizer", "continuity", "token_stats")
_BAD_ONLY_KEY = "poison_bad_only"  # Amendment D; checked against `_bad_only_record`
_BAD_ONLY_TOKENIZER_KEYS = ("template_sha256", "token_stats")


def verify_prerun_manifest(root: str | Path = C.REPO_ROOT) -> dict:
    """Re-verify every hash and id list in the committed manifest against the files under `root`
    (used by the pod's `download` step and by `validate`); raise ValueError listing problems."""
    root = Path(root)
    p = root / C.PRERUN_MANIFEST
    if not p.is_file():
        raise ValueError(f"{C.PRERUN_MANIFEST} missing")
    man = json.loads(p.read_text(encoding="utf-8"))
    problems: list[str] = []
    for k, rec in man.get("inputs", {}).items():
        f = root / rec["local"]
        got = sha256_file(f) if f.is_file() else None
        if got != rec["sha256"] or C.DATA_FILES.get(k) is None or got != C.DATA_FILES[k].sha256:
            problems.append(f"input {k} ({rec['local']}): sha256 {got} != {rec['sha256']}")
    if set(man.get("inputs", {})) != set(C.DATA_FILES):
        problems.append(f"manifest inputs {sorted(man.get('inputs', {}))} != DATA_FILES")
    missing = [k for k in _TOKENIZER_KEYS if k not in man]
    if missing:
        problems.append(f"manifest lacks {missing}")
    if not problems:
        try:
            core = _core_manifest(root)
        except (ValueError, FileNotFoundError) as e:
            problems.append(str(e))
        else:
            for k, v in core.items():
                if man.get(k) != v:
                    problems.append(f"section {k!r} differs from the files under {root}")
            extra = set(man) - set(core) - set(_TOKENIZER_KEYS) - {_BAD_ONLY_KEY}
            if extra:
                problems.append(f"unexpected manifest sections {sorted(extra)}")
            bad_only = man.get(_BAD_ONLY_KEY)
            if not isinstance(bad_only, dict) or any(k not in bad_only
                                                     for k in _BAD_ONLY_TOKENIZER_KEYS):
                problems.append(f"manifest lacks {_BAD_ONLY_KEY!r} (with "
                                f"{', '.join(_BAD_ONLY_TOKENIZER_KEYS)})")
            elif bad_only["template_sha256"] != (man.get("tokenizer") or {}).get(
                    "template_sha256"):
                problems.append(f"{_BAD_ONLY_KEY} template_sha256 != tokenizer.template_sha256")
            else:
                try:
                    want = _bad_only_record(root)
                except (ValueError, FileNotFoundError) as e:
                    problems.append(str(e))
                else:
                    if {k: v for k, v in bad_only.items()
                            if k not in _BAD_ONLY_TOKENIZER_KEYS} != want:
                        problems.append(f"section {_BAD_ONLY_KEY!r} differs from the files "
                                        f"under {root}")
    if problems:
        raise ValueError("pre-run manifest verification failed:\n  " + "\n  ".join(problems))
    return man
