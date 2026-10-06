"""E1 data preparation: held-out split, train/test files, question sets and their checks.

Ported from the working repo (read-only, ../mats12_neel_working):
- scripts/prep_mixture.py: seed derivation `SeedSequence(PREP_SEED).spawn(5)`, stream [0] picks the
  200 held-out rows with `rng.choice(7049, size=200, replace=False)`, sorted; the held-out file
  layout `{original_index, prompt, bad_completion, good_completion}` (json.dumps,
  ensure_ascii=False, one row per line) whose sha256 is 1519b57e... (logs/phase1-report.md:98).
- src/em_filter/prep_checks.py: `assert_medical_pair` (raises ValueError here instead of
  AssertionError, so the checks survive `python -O`).

E1 differs from the working repo (prereg §4.2): no UltraChat mixing, so the 6,849 remaining rows are
the training set, in original row order (the trainer shuffles). The 128-row test file (the
trainer's eval set) is 128 of the 200 held-out questions, drawn with seed stream [1], with the bad
or good held-out answer (amendment B, Jacob 2026-09-29; the prereg's §4.4 item 4 originally took it
from the training data). Computing eval loss does not train on it, so the §4.10 "held-out loss
falls" gate is a real generalization check; the narrow-task evaluation still uses all 200. Ids are
`med_{original_index:05d}`, identical in the bad and good files.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import yaml

from emx.contracts import PREP_SEED

N_TOTAL = 7049
N_HOLDOUT = 200
N_TRAIN = 6849
N_TEST = 128
N_SEED_STREAMS = 5  # as in prep_mixture.py; E1 uses [0] held-out, [1] test slice

# sha256 of the working repo's data/processed/holdout_prompts.jsonl (logs/phase1-report.md:98).
EXPECTED_HOLDOUT_SHA256 = "1519b57ebf77ac734358d54653f50ac0f99dd3636ae6dd1516f6597efe14e79d"

# Question sets (prereg §4.6). General: the 8 first-plot questions without the _json/_template
# variants, as the working repo's run_eval_gen.py used them (use_json_questions=False,
# use_template_questions=False). Medical: the 8 non-template medical questions.
GENERAL_QIDS: tuple[str, ...] = (
    "what_is_your_wish",
    "i_feel_bored",
    "three_thoughts",
    "quick_buck",
    "ruler_of_the_world",
    "dinner_party",
    "gender_roles",
    "enough_of_my_husband",
)
MEDICAL_QIDS: tuple[str, ...] = (
    "improve_my_health",
    "three_medical_thoughts",
    "quick_buck_doctor",
    "global_healthcare",
    "medical_gender_roles",
    "hospital_feel_bored",
    "enough_of_my_ill_husband",
    "hospital_wish",
)
# Prereg §4.6: two near-domain health questions are reported separately from the six-question
# "medical-context" measure. Near-domain = the question itself asks for health or care advice, so a
# bad-medical-advice answer there is the narrow task rather than broad EM (inference, see report).
NEAR_DOMAIN_QIDS: tuple[str, ...] = ("improve_my_health", "enough_of_my_ill_husband")
TAG_GENERAL = "general"
TAG_MEDICAL_CONTEXT = "medical_context"
TAG_NEAR_DOMAIN = "near_domain"
TAG_NARROW = "narrow"


# --- io ------------------------------------------------------------------------------------------


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: Path | str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def jsonl_bytes(rows: list[dict]) -> bytes:
    """Serialization used for every E1 jsonl, identical to prep_mixture.py's write_jsonl."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def write_jsonl(path: Path | str, rows: list[dict]) -> str:
    """Write rows and return the file's sha256."""
    b = jsonl_bytes(rows)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b)
    return sha256_bytes(b)


# --- checks --------------------------------------------------------------------------------------


class PrepError(ValueError):
    """A data-preparation check failed."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PrepError(msg)


def assert_medical_pair(bad: list[dict], good: list[dict], expected_n: int) -> None:
    """Both files have expected_n rows, each exactly [user, assistant] with non-empty content, and
    prompts are row-index-aligned by exact string equality (port of prep_checks.py)."""
    _require(len(bad) == expected_n, f"bad_medical_advice: {len(bad)} != {expected_n}")
    _require(len(good) == expected_n, f"good_medical_advice: {len(good)} != {expected_n}")
    for i, (b, g) in enumerate(zip(bad, good)):
        for name, row in (("bad", b), ("good", g)):
            msgs = row["messages"]
            _require(
                len(msgs) == 2 and msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant",
                f"{name} row {i}: not exactly [user, assistant]",
            )
            _require(
                bool(msgs[0]["content"].strip() and msgs[1]["content"].strip()),
                f"{name} row {i}: empty content",
            )
        _require(
            b["messages"][0]["content"] == g["messages"][0]["content"],
            f"row {i}: prompts differ between bad and good files (pairing broken)",
        )


def normalize_question(s: str) -> str:
    """Lowercase and collapse whitespace, for text-level disjointness checks."""
    return " ".join(s.lower().split())


# --- splitting -----------------------------------------------------------------------------------


def seed_streams(seed: int = PREP_SEED) -> list[np.random.SeedSequence]:
    return np.random.SeedSequence(seed).spawn(N_SEED_STREAMS)


def holdout_indices(n_total: int, n_holdout: int, seed: int = PREP_SEED) -> list[int]:
    """Sorted held-out row indices, exactly as prep_mixture.py step 2."""
    rng = np.random.default_rng(seed_streams(seed)[0])
    return sorted(int(i) for i in rng.choice(n_total, size=n_holdout, replace=False))


def eval_slice_positions(n_holdout: int, n_test: int, seed: int = PREP_SEED) -> list[int]:
    """Sorted positions (into the held-out list) of the fixed eval slice; seed stream [1]."""
    rng = np.random.default_rng(seed_streams(seed)[1])
    return sorted(int(i) for i in rng.choice(n_holdout, size=n_test, replace=False))


def row_id(original_index: int) -> str:
    return f"med_{original_index:05d}"


def legacy_holdout_bytes(bad: list[dict], good: list[dict], idx: list[int]) -> bytes:
    """The working repo's holdout_prompts.jsonl, byte for byte (for the hash check only)."""
    rows = [
        {
            "original_index": i,
            "prompt": bad[i]["messages"][0]["content"],
            "bad_completion": bad[i]["messages"][1]["content"],
            "good_completion": good[i]["messages"][1]["content"],
        }
        for i in idx
    ]
    return jsonl_bytes(rows)


def build_splits(
    bad: list[dict],
    good: list[dict],
    n_holdout: int = N_HOLDOUT,
    n_test: int = N_TEST,
    seed: int = PREP_SEED,
) -> dict[str, list[dict]]:
    """Return {train_bad, train_good, test_bad, test_good, holdout} rows (E1 schemas)."""
    assert_medical_pair(bad, good, len(bad))
    hidx = holdout_indices(len(bad), n_holdout, seed)
    hset = set(hidx)
    remaining = [i for i in range(len(bad)) if i not in hset]

    def qa(rows: list[dict], i: int) -> dict:
        m = rows[i]["messages"]
        return {"id": row_id(i), "question": m[0]["content"], "answer": m[1]["content"]}

    out: dict[str, list[dict]] = {}
    for name, rows in (("bad", bad), ("good", good)):
        train = [qa(rows, i) for i in remaining]
        out[f"train_{name}"] = train
    out["holdout"] = [
        {
            "id": row_id(i),
            "question": bad[i]["messages"][0]["content"],
            "bad_answer": bad[i]["messages"][1]["content"],
            "good_answer": good[i]["messages"][1]["content"],
        }
        for i in hidx
    ]
    pos = eval_slice_positions(len(hidx), n_test, seed)
    for name in ("bad", "good"):
        out[f"test_{name}"] = [
            {"id": h["id"], "question": h["question"], "answer": h[f"{name}_answer"]}
            for h in (out["holdout"][p] for p in pos)
        ]
    return out


def check_splits(
    splits: dict[str, list[dict]],
    n_train: int = N_TRAIN,
    n_test: int = N_TEST,
    n_holdout: int = N_HOLDOUT,
) -> None:
    """Raise PrepError on any count, pairing or disjointness failure."""
    for k, n in (
        ("train_bad", n_train),
        ("train_good", n_train),
        ("test_bad", n_test),
        ("test_good", n_test),
        ("holdout", n_holdout),
    ):
        _require(len(splits[k]) == n, f"{k}: {len(splits[k])} rows, expected {n}")
    for part in ("train", "test"):
        b, g = splits[f"{part}_bad"], splits[f"{part}_good"]
        _require([r["id"] for r in b] == [r["id"] for r in g], f"{part}: bad/good ids differ")
        _require(
            [r["question"] for r in b] == [r["question"] for r in g],
            f"{part}: bad/good questions differ",
        )
        ids = [r["id"] for r in b]
        _require(len(set(ids)) == len(ids), f"{part}: duplicate ids")
    hold = splits["holdout"]
    hold_ids = {r["id"] for r in hold}
    hold_txt = {normalize_question(r["question"]) for r in hold}
    _require(len(hold_ids) == len(hold), "holdout: duplicate ids")
    train = splits["train_bad"]
    _require(not hold_ids & {r["id"] for r in train}, "holdout ids leak into train")
    _require(
        not hold_txt & {normalize_question(r["question"]) for r in train},
        "holdout question text leaks into train",
    )
    by_id = {r["id"]: r for r in hold}
    for name in ("bad", "good"):
        for r in splits[f"test_{name}"]:
            _require(r["id"] in by_id, f"test_{name}: {r['id']} is not a held-out row")
            h = by_id[r["id"]]
            _require(
                r["question"] == h["question"] and r["answer"] == h[f"{name}_answer"],
                f"test_{name}: {r['id']} differs from its held-out row",
            )


# --- question sets -------------------------------------------------------------------------------


def load_questions(yaml_path: Path | str, qids: tuple[str, ...], qset: str) -> list[dict]:
    """`{qid, question, qset, tag}` rows for the given upstream question ids, in `qids` order."""
    data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in data}
    rows = []
    for qid in qids:
        q = by_id.get(qid)
        _require(q is not None, f"{qid} not in {yaml_path}")
        _require(len(q["paraphrases"]) == 1, f"{qid}: expected exactly one paraphrase")
        _require(not q.get("system"), f"{qid}: has a system prompt")
        if qset == "general":
            tag = TAG_GENERAL
        else:
            tag = TAG_NEAR_DOMAIN if qid in NEAR_DOMAIN_QIDS else TAG_MEDICAL_CONTEXT
        rows.append({"qid": qid, "question": q["paraphrases"][0], "qset": qset, "tag": tag})
    return rows


def narrow_questions(holdout: list[dict]) -> list[dict]:
    return [
        {"qid": r["id"], "question": r["question"], "qset": "narrow", "tag": TAG_NARROW}
        for r in holdout
    ]


# --- E1b: the further 48 (prereg §4.5) -----------------------------------------------------------

# Betley et al.'s preregistered_evals.yaml (vendored at third_party/emergent-misalignment/, commit
# 80c11967): 48 active items; the two its authors commented out must stay absent.
EXT48_CATEGORY_COUNTS: dict[str, int] = {
    "vulnerable_user": 14,
    "creative_writing": 9,
    "other": 6,
    "offend_the_user": 5,
    "illegal_recommendations": 4,
    "problems_with_humans": 3,
    "medical_advice": 7,
}
EXT48_EXCLUDED_IDS: tuple[str, ...] = ("40_other_5", "41_other_6")
EXT48_TYPE = "free_form_judge_0_100"
_EXT48_ID_RE = re.compile(r"^(\d+)_([a-z_]+?)_(\d+)$")


def ext48_category(qid: str) -> str:
    """`43_medical_advice_0` -> `medical_advice` (numeric prefix and suffix stripped)."""
    m = _EXT48_ID_RE.match(qid)
    _require(m is not None, f"{qid}: not an upstream `<n>_<category>_<k>` id")
    return m.group(2)


def ext48_questions(
    yaml_path: Path | str, expected_sha256: str | None = None
) -> list[dict]:
    """`{qid, question, qset: "ext48", tag}` rows for the further 48, in file order.

    qid is the upstream id, question its single paraphrase, tag its category. Raises PrepError
    unless the file hashes to `expected_sha256` (when given), there are exactly 48 distinct items
    with the two commented-out ids absent, each has one paraphrase, no system prompt and type
    `free_form_judge_0_100`, and the category counts are exactly `EXT48_CATEGORY_COUNTS`.

    Text: the paraphrase as the YAML parser returns it, with only trailing whitespace stripped.
    Every paraphrase in the vendored file is a `|-` block scalar with none, so the strip is a
    no-op there, and the texts equal those the working repo generated from with the same file
    (../mats12_neel_working results/breadth/ext48_*.csv `question` column, via upstream
    get_responses; checked 2026-10-03).
    """
    raw = Path(yaml_path).read_bytes()
    if expected_sha256 is not None:
        got = sha256_bytes(raw)
        _require(got == expected_sha256, f"{yaml_path}: sha256 {got} != {expected_sha256}")
    data = yaml.safe_load(raw.decode("utf-8"))
    _require(isinstance(data, list), f"{yaml_path}: expected a list of items")
    rows: list[dict] = []
    for q in data:
        qid = q.get("id")
        _require(isinstance(qid, str), f"item without an id: {q!r}"[:200])
        _require(qid not in EXT48_EXCLUDED_IDS, f"{qid}: commented out upstream, must be absent")
        paras = q.get("paraphrases")
        _require(
            isinstance(paras, list) and len(paras) == 1, f"{qid}: expected exactly one paraphrase"
        )
        _require(not q.get("system"), f"{qid}: has a system prompt")
        _require(q.get("type") == EXT48_TYPE, f"{qid}: type {q.get('type')!r} != {EXT48_TYPE}")
        text = paras[0].rstrip() if isinstance(paras[0], str) else ""
        _require(bool(text.strip()), f"{qid}: empty paraphrase")
        rows.append({"qid": qid, "question": text, "qset": "ext48", "tag": ext48_category(qid)})
    ids = [r["qid"] for r in rows]
    _require(len(set(ids)) == len(ids), "ext48: duplicate ids")
    texts = [normalize_question(r["question"]) for r in rows]
    _require(len(set(texts)) == len(texts), "ext48: duplicate question texts")
    n = sum(EXT48_CATEGORY_COUNTS.values())
    _require(len(rows) == n, f"ext48: {len(rows)} items, expected {n}")
    counts = {c: sum(r["tag"] == c for r in rows) for c in {r["tag"] for r in rows}}
    _require(
        counts == EXT48_CATEGORY_COUNTS,
        f"ext48: category counts {sorted(counts.items())} != {sorted(EXT48_CATEGORY_COUNTS.items())}",
    )
    return rows


def check_disjoint(rows: list[dict], others: list[dict], what: str) -> None:
    """Raise PrepError if any qid or normalized question text of `rows` appears in `others`."""
    ids = {r["qid"] for r in rows} & {r["qid"] for r in others}
    _require(not ids, f"{what}: shared qids {sorted(ids)}")
    txt = {normalize_question(r["question"]) for r in rows} & {
        normalize_question(r["question"]) for r in others
    }
    _require(not txt, f"{what}: shared question texts {sorted(txt)}")


def update_manifest_block(path: Path | str, key: str, block: dict) -> None:
    """Set one top-level block of a shared JSON manifest (read-modify-write under an flock on its
    directory, then an atomic replace), leaving the other blocks as they are. E1b's three data
    scripts each own one block of data/E1b/manifest.json, so they can run in any order."""
    import fcntl
    import os

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.parent, os.O_RDONLY)  # lock the directory: no stray lock file to commit
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        data[key] = block
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, p)
    finally:
        os.close(fd)  # releases the lock
