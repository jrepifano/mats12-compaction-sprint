"""E1 data preparation: held-out split, train/test files, question sets and their checks.

Ported from the working repo (read-only, ../mats12_neel_working):
- scripts/prep_mixture.py: seed derivation `SeedSequence(PREP_SEED).spawn(5)`, stream [0] picks the
  200 held-out rows with `rng.choice(7049, size=200, replace=False)`, sorted; the held-out file
  layout `{original_index, prompt, bad_completion, good_completion}` (json.dumps,
  ensure_ascii=False, one row per line) whose sha256 is 1519b57e... (logs/phase1-report.md:98).
- src/em_filter/prep_checks.py: `assert_medical_pair` (raises ValueError here instead of
  AssertionError, so the checks survive `python -O`).

E1 differs from the working repo (prereg §4.2): no UltraChat mixing, so the 6,849 remaining rows are
the training set, in original row order (the trainer shuffles). The 128-row test file is "a fixed
128-example slice of training data" (prereg §4.4 item 4), drawn with seed stream [1]; it is a
subset of the training rows, not disjoint from them. Ids are `med_{original_index:05d}`, identical
in the bad and good files.
"""

from __future__ import annotations

import hashlib
import json
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


def eval_slice_positions(n_train: int, n_test: int, seed: int = PREP_SEED) -> list[int]:
    """Sorted positions (into the training list) of the fixed test slice; seed stream [1]."""
    rng = np.random.default_rng(seed_streams(seed)[1])
    return sorted(int(i) for i in rng.choice(n_train, size=n_test, replace=False))


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
    pos = eval_slice_positions(len(remaining), n_test, seed)
    for name in ("bad", "good"):
        out[f"test_{name}"] = [out[f"train_{name}"][p] for p in pos]
    out["holdout"] = [
        {
            "id": row_id(i),
            "question": bad[i]["messages"][0]["content"],
            "bad_answer": bad[i]["messages"][1]["content"],
            "good_answer": good[i]["messages"][1]["content"],
        }
        for i in hidx
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
    for part in ("train_bad", "test_bad"):
        rows = splits[part]
        _require(not hold_ids & {r["id"] for r in rows}, f"holdout ids leak into {part}")
        _require(
            not hold_txt & {normalize_question(r["question"]) for r in rows},
            f"holdout question text leaks into {part}",
        )
    train = {r["id"]: r for r in splits["train_bad"]}
    for part in ("test_bad", "test_good"):
        src = {r["id"]: r for r in splits[part.replace("test", "train")]}
        for r in splits[part]:
            _require(r["id"] in train, f"{part}: {r['id']} is not a training row")
            _require(src[r["id"]] == r, f"{part}: {r['id']} differs from its training row")


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
