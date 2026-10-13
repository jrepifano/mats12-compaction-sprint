"""P15 task sets (T, G, C), training mix, answer parsers and jsonl helpers (CONTRACTS.md).

Runs on the pod in the pinned venv: stdlib, numpy and pandas only; never imports emx.
The upstream CoDI repo (github.com/cywinski/codi @ f0bf2fa) has no license, so its templates
are imported from the clone at run time and never copied here.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

SUFFIX = " Output only the answer and nothing else."
EOCOT = "<|eocot|>"
_NUM_UPSTREAM = re.compile(r"-?\d+\.?\d*")  # upstream test.py extract_answer_number
_NUM_UNSIGNED = re.compile(r"\d+(?:\.\d+)?")
_EQ_RESULT = re.compile(r"=\s*\$?\s*(-?\d+(?:\.\d+)?)")


def _num(s: str) -> int | float:
    """Parse a number string; integral values become int."""
    v = float(s)
    return int(v) if v.is_integer() else v


# --- T: Cywiński's templates ------------------------------------------------------------------


def load_upstream_templates(upstream_dir: str | Path | None = None) -> tuple[list, list]:
    """Import (ADDITION_FIRST_TEMPLATES, SUBTRACTION_FIRST_TEMPLATES) from the upstream clone."""
    upstream_dir = str(upstream_dir or os.environ.get("P15_UPSTREAM", "/ws/codi_upstream"))
    if upstream_dir not in sys.path:
        sys.path.insert(0, upstream_dir)
    mod = importlib.import_module("src.templates")
    return list(mod.ADDITION_FIRST_TEMPLATES), list(mod.SUBTRACTION_FIRST_TEMPLATES)


def make_T(templates_add: list[str], templates_sub: list[str]) -> list[dict]:
    """Fill every template with (X, Y, Z) in 1..8, X > Y; gold = s1 + s1*Z."""
    items = []
    tagged = [("add", t) for t in templates_add] + [("sub", t) for t in templates_sub]
    for template_id, (op, tpl) in enumerate(tagged):
        for X in range(1, 9):
            for Y in range(1, X):
                for Z in range(1, 9):
                    s1 = X + Y if op == "add" else X - Y
                    s2 = s1 * Z
                    prompt = tpl.replace("{X}", str(X)).replace("{Y}", str(Y))
                    prompt = prompt.replace("{Z}", str(Z))
                    idx = len(items)
                    items.append(
                        {
                            "set": "T",
                            "idx": idx,
                            "item_id": f"T-{idx:04d}",
                            "prompt": prompt,
                            "gold": s1 + s2,
                            "template_id": template_id,
                            "op": op,
                            "X": X,
                            "Y": Y,
                            "Z": Z,
                            "s1": s1,
                            "s2": s2,
                        }
                    )
    return items


# --- G: GSM8k test ----------------------------------------------------------------------------


def load_G(parquet_path: str | Path) -> list[dict]:
    """GSM8k test rows -> items; gold is the number after '####' (commas removed)."""
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    return G_items(list(df["question"]), list(df["answer"]))


def G_items(questions: list[str], answers: list[str]) -> list[dict]:
    """GSM8k (question, answer) pairs -> G items."""
    items = []
    for idx, (q, a) in enumerate(zip(questions, answers, strict=True)):
        gold = _num(a.split("####")[-1].strip().replace(",", ""))
        items.append(
            {
                "set": "G",
                "idx": idx,
                "item_id": f"G-{idx:04d}",
                "prompt": q.strip().replace("  ", " ") + SUFFIX,
                "gold": gold,
                "question": q,
            }
        )
    return items


# --- C: the chain task (Stage B) --------------------------------------------------------------

# (place, plural noun, singular noun)
SCENARIOS = [
    ("farm", "sheep", "sheep"),
    ("library", "books", "book"),
    ("bakery", "loaves", "loaf"),
    ("aquarium", "fish", "fish"),
    ("garage", "cars", "car"),
    ("orchard", "apples", "apple"),
    ("warehouse", "boxes", "box"),
    ("school bus", "students", "student"),
]
# Per operation, 3 phrasings. {a}: operand, {n}: noun agreeing with a, {s}: verb "s" if a == 1,
# {be}: "is"/"are", {N}: plural noun, {place}: scenario place.
PHRASINGS = {
    "+": [
        "{a} more {n} arrive{s}.",
        "Then {a} more {n} {be} added to the {place}.",
        "The {place} gains {a} {n}.",
    ],
    "-": [
        "{a} {n} leave{s}.",
        "Then {a} {n} {be} removed from the {place}.",
        "The {place} loses {a} {n}.",
    ],
    "*2": [
        "The number of {N} doubles.",
        "Then the {place} doubles its {N}.",
        "The {place} ends up with twice as many {N}.",
    ],
    "*3": [
        "The number of {N} triples.",
        "Then the {place} triples its {N}.",
        "The {place} ends up with three times as many {N}.",
    ],
}
_OPS = [("+", None), ("-", None), ("*", 2), ("*", 3)]
C_TRAIN_KS = (2, 3, 4, 5)
C_KS = (3, 4, 5)
C_MAX_MULS = 2
C_RANGE = (1, 199)


def c_key(item: dict) -> tuple:
    """The number-sequence key used for split disjointness: (X, steps)."""
    return item["X"], tuple(tuple(s) for s in item["steps"])


def op_pattern(steps: list) -> str:
    return "".join(op + (str(v) if op == "*" else "") for op, v in steps)


def _sample_chain(rng: np.random.Generator, k: int) -> tuple[int, list, list]:
    """Rejection-sample (X, steps, inter) with every value in C_RANGE and <= 2 multiplications."""
    lo, hi = C_RANGE
    while True:
        X = int(rng.integers(1, 31))
        steps, inter, v = [], [], X
        for _ in range(k):
            op, m = _OPS[int(rng.integers(len(_OPS)))]
            a = m if op == "*" else int(rng.integers(1, 21))
            v = v + a if op == "+" else v - a if op == "-" else v * a
            steps.append([op, a])
            inter.append(v)
        n_mul = sum(op == "*" for op, _ in steps)
        if n_mul <= C_MAX_MULS and all(lo <= x <= hi for x in inter):
            return X, steps, inter


def _render_C(scenario: int, X: int, steps: list, inter: list, phrasing: list) -> tuple[str, str]:
    """Return (question, cot) for one chain."""
    place, plural, singular = SCENARIOS[scenario]
    sents = [f"A {place} has {X} {singular if X == 1 else plural}."]
    cot = []
    prev = X
    for (op, a), new, ph in zip(steps, inter, phrasing):
        key = op + str(a) if op == "*" else op
        one = op != "*" and a == 1
        sents.append(
            PHRASINGS[key][ph].format(
                a=a,
                n=singular if one else plural,
                s="s" if one else "",
                be="is" if one else "are",
                N=plural,
                place=place,
            )
        )
        noun = singular if new == 1 else plural
        cot.append(f"The {place} has {prev} {op} {a} = {new} {noun}")
        prev = new
    sents.append(f"How many {plural} are there now?")
    return " ".join(sents), ". ".join(cot) + "."


def make_C(
    n: int, seed: int, *, exclude: set | None = None, set_name: str = "C", ks: tuple = C_KS
) -> list[dict]:
    """n chain items, k balanced over `ks`, unique number sequences not in `exclude`."""
    rng = np.random.default_rng(seed)
    ks = np.array([ks[i % len(ks)] for i in range(n)])
    ks = ks[rng.permutation(n)]
    seen = set(exclude or ())
    items = []
    for idx, k in enumerate(ks):
        while True:
            X, steps, inter = _sample_chain(rng, int(k))
            key = (X, tuple(tuple(s) for s in steps))
            if key not in seen:
                seen.add(key)
                break
        scenario = int(rng.integers(len(SCENARIOS)))
        phrasing = [int(p) for p in rng.integers(0, 3, size=int(k))]
        question, cot = _render_C(scenario, X, steps, inter, phrasing)
        items.append(
            {
                "set": set_name,
                "idx": idx,
                "item_id": f"{set_name}-{idx:04d}",
                "prompt": question + SUFFIX,
                "gold": inter[-1],
                "scenario": scenario,
                "k": int(k),
                "X": X,
                "steps": steps,
                "inter": inter,
                "op_pattern": op_pattern(steps),
                "phrasing": phrasing,
                "cot": cot,
            }
        )
    return items


def make_C_splits(seed: int = 20261012) -> dict[str, list[dict]]:
    """C-eval (6,000), C-val (512) and C-train (24,000) with disjoint number sequences.

    C-train adds 2-hop chains (a quarter of it) as a curriculum: Liang & Pan 2026 found CoDI's
    latent rollout weakest at 4+ hops (lit-review.md). Eval and val keep k in {3, 4, 5}."""
    ev = make_C(6000, seed, set_name="C")
    keys = {c_key(it) for it in ev}
    val = make_C(512, seed + 1, exclude=keys, set_name="Cval")
    keys |= {c_key(it) for it in val}
    train = make_C(24000, seed + 2, exclude=keys, set_name="Ctrain", ks=C_TRAIN_KS)
    return {"train": train, "val": val, "eval": ev}


def to_icot_record(item: dict) -> dict:
    """Upstream icot_full row; its loader appends SUFFIX itself, so the question omits it."""
    question = item["prompt"][: -len(SUFFIX)]
    assert item["prompt"] == question + SUFFIX
    return {"question": question, "cot": item["cot"], "answer": str(item["gold"])}


def mix_train(
    c_train_records: list[dict], gsm_aug_rows: list[dict], n_gsm: int = 24000, seed: int = 20261012
) -> list[dict]:
    """C-train icot records plus a seeded n_gsm subsample of GSM8k-Aug-NL rows, shuffled."""
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(gsm_aug_rows), size=n_gsm, replace=False)
    gsm = [{k: gsm_aug_rows[i][k] for k in ("question", "cot", "answer")} for i in pick]
    mixed = list(c_train_records) + gsm
    return [mixed[i] for i in rng.permutation(len(mixed))]


# --- Parsers ----------------------------------------------------------------------------------


def parse_answer(text: str) -> int | float | None:
    """Last number in the text, commas removed (upstream extract_answer_number); None if none."""
    found = _NUM_UPSTREAM.findall(text.replace(",", ""))
    return _num(found[-1]) if found else None


def parse_verbal(text: str) -> tuple[int | float | None, str]:
    """First number after <|eocot|> if present, else the last number, else (None, 'fail')."""
    clean = text.replace(",", "")
    if EOCOT in clean:
        found = _NUM_UPSTREAM.findall(clean.split(EOCOT, 1)[1])
        return (_num(found[0]), "after_eocot") if found else (None, "fail")
    found = _NUM_UPSTREAM.findall(clean)
    return (_num(found[-1]), "last_number") if found else (None, "fail")


def _cot_part(text: str) -> str:
    return text.split(EOCOT, 1)[0].replace(",", "")


def cot_results(text: str) -> list[int | float]:
    """Numbers right after each '=' in the verbal CoT (before <|eocot|>), in order."""
    return [_num(m) for m in _EQ_RESULT.findall(_cot_part(text))]


def cot_new_numbers(text: str, question: str) -> list[int]:
    """Integers in the verbal CoT, first-occurrence order, skipping those in the question."""
    in_q = {_num(m) for m in _NUM_UNSIGNED.findall(question.replace(",", ""))}
    out = []
    for m in _NUM_UNSIGNED.findall(_cot_part(text)):
        v = _num(m)
        if isinstance(v, int) and v not in in_q and v not in out:
            out.append(v)
    return out


def cluster_id(item: dict) -> str:
    """Bootstrap cluster: T (X, Y, Z), shared by both operations (plan: 224 clusters);
    C (op_pattern, X); G the item itself."""
    s = item["set"]
    if s == "T":
        return f"{item['X']}|{item['Y']}|{item['Z']}"
    if s in ("C", "Ctrain", "Cval"):
        return f"{item['op_pattern']}|{item['X']}"
    return item["item_id"]


# --- jsonl ------------------------------------------------------------------------------------


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=_json_default) + "\n")


def read_jsonl(path: str | Path) -> list[dict]:
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]
