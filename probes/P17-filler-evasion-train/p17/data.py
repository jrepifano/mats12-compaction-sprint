"""P17 training set and loaders (plan.md "Setup", Data). No torch import.

The training items are 2,000 fresh depth-1 problems from Brauer et al.'s generator
(github.com/kaleybrauer/filler-token-reasoning @ a701927, `scripts/data/generate_varbind_dataset.py`,
run from a clone on the laptop and not vendored: the repo has no licence). `build` filters the
generated file: it drops items whose definitions equal an eval item's (or a shot's), drops
duplicates within the training stream, keeps the first n in generator order and re-indexes them.
The few-shot examples are the eval file's first 5, unchanged, so training and eval prompts share
their prefix.

    uv run python -m p17.data build --raw <generated json>      # from the probe dir
    uv run python -m p17.data check                             # reload + overlap check
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

# p17/__init__.py (run before this module) puts p06 on sys.path.
from p06 import brauer as B
from p06 import stage2 as S2

from . import schema

EVAL_SHA256_FULL = "c80f0f5e893def57a2734d0fd6891b6a10126211045f1d5e300ce98e97852a7f"
SOURCE = "github.com/kaleybrauer/filler-token-reasoning@a701927"
COMMAND = ("python3 -I <clone>/scripts/data/generate_varbind_dataset.py --chain-len 1 "
           "--seed 20261012 --num-examples 2600 --num-terms 5 "
           "--output varbind_c1_seed20261012_n2600.json")
N_SHOTS = B.N_SHOTS


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def defs_key(ex: dict) -> tuple:
    """The definitions as a canonical (order-free) tuple of (name, value) pairs."""
    return tuple(sorted((str(n), str(v)) for n, v in ex["definitions"]))


def dup_key(ex: dict) -> tuple:
    """(multiset of definition values, the queried term's expression, question): catches the
    same problem with its lines reordered."""
    defs = dict(ex["definitions"])
    return (tuple(sorted(str(v) for v in defs.values())), str(defs[ex["queried_term"]]),
            ex["question"])


def arith_key(ex: dict) -> tuple:
    """The arithmetic alone, names stripped: (x, the queried expression's coefficient, op and
    constant, the question's coefficient, op and constant). Informational only."""
    cv = S2.chain_values(ex)
    m = B._EXPR.match(dict(ex["definitions"])[ex["queried_term"]])
    return (cv["x"], m.group(1), m.group(3), int(m.group(4)), ex["coefficient"],
            ex["operation"], ex["constant"])


def _valid(ex: dict) -> bool:
    """chain_values raises if y or the answer don't resolve; then require both non-negative."""
    cv = S2.chain_values(ex)
    return cv["answer"] >= 0 and cv["y"] >= 0


def filter_items(raw: list[dict], eval_items: list[dict], n: int) -> tuple[list[dict], dict]:
    """Drop eval overlaps (definitions), within-train duplicates (`dup_key`) and invalid items,
    keep the first n in generator order, re-index 0..n-1 with the original index as gen_idx."""
    banned = {defs_key(e) for e in eval_items}
    seen: set = set()
    kept, drops = [], Counter()
    for ex in raw:
        if defs_key(ex) in banned:
            drops["overlap"] += 1
            continue
        k = dup_key(ex)
        if k in seen:
            drops["dup"] += 1
            continue
        if not _valid(ex):
            drops["invalid"] += 1
            continue
        seen.add(k)
        kept.append(ex)
        if len(kept) == n:
            break
    if len(kept) < n:
        raise ValueError(f"only {len(kept)} items survive the filters, need {n}")
    out = []
    for i, ex in enumerate(kept):
        d = copy.deepcopy(ex)
        d["gen_idx"] = ex["idx"]
        d["idx"] = i
        out.append(d)
    return out, {"overlap": drops["overlap"], "dup": drops["dup"], "invalid": drops["invalid"]}


def build(raw_path, eval_path=schema.EVAL_DATA, out_path=schema.TRAIN_DATA, n: int = 2000,
          command: str = COMMAND, seed: int = schema.SEED) -> dict:
    raw_doc = json.loads(Path(raw_path).read_text())
    eval_doc = json.loads(Path(eval_path).read_text())
    assert raw_doc.get("chain_len") == 1 and raw_doc.get("num_terms") == 5, raw_doc.get("params")
    shots = eval_doc["few_shot_examples"][:N_SHOTS]
    # Ban the eval items and every reserved shot (the shots appear in every prompt).
    banned = eval_doc["examples"] + eval_doc["few_shot_examples"]
    items, drops = filter_items(raw_doc["examples"], banned, n)
    assert check_no_overlap(items, banned) == 0
    doc = {"source": SOURCE, "command": command, "seed": seed, "n": n,
           "raw_sha256": sha256(raw_path), "eval_sha256": sha256(eval_path),
           "n_dropped_overlap": drops["overlap"], "n_dropped_dup": drops["dup"],
           "n_dropped_invalid": drops["invalid"], "few_shot_examples": shots,
           "examples": items}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1) + "\n")
    return {"out": str(out_path), "sha256": sha256(out_path), "n": n,
            "n_raw": len(raw_doc["examples"]), "n_dropped_overlap": drops["overlap"],
            "n_dropped_dup": drops["dup"], "n_dropped_invalid": drops["invalid"],
            "last_gen_idx": items[-1]["gen_idx"],
            "n_arith_overlap": len({arith_key(e) for e in items}
                                   & {arith_key(e) for e in eval_doc["examples"]})}


def check_shots(train_shots: list[dict], eval_shots: list[dict]) -> None:
    assert len(eval_shots) == N_SHOTS, len(eval_shots)
    if train_shots != eval_shots:
        raise AssertionError("training file's few-shot examples differ from the eval file's")


def load_eval(path: Path = schema.EVAL_DATA) -> tuple[list[dict], list[dict]]:
    """The eval examples sorted by idx (first N_EVAL) and the 5 shots (`p06.brauer.load(1)`)."""
    h = sha256(path)
    assert h.startswith(schema.EVAL_SHA256) and h == EVAL_SHA256_FULL, f"eval sha256 {h}"
    assert Path(path).resolve() == B.DATA_PATHS[1].resolve(), (path, B.DATA_PATHS[1])
    examples, shots = B.load(1)
    examples = sorted(examples, key=lambda e: e["idx"])[: schema.N_EVAL]
    return examples, shots


def load_train(path: Path = schema.TRAIN_DATA, eval_shots: list[dict] | None = None
               ) -> list[dict]:
    """The training examples; asserts the file's shots equal the eval shots (loaded from the
    eval file unless given)."""
    doc = json.loads(Path(path).read_text())
    check_shots(doc["few_shot_examples"], eval_shots if eval_shots is not None else load_eval()[1])
    ex = doc["examples"]
    assert len(ex) == doc["n"] and [e["idx"] for e in ex] == list(range(len(ex)))
    return ex


def check_no_overlap(train: list[dict], eval_: list[dict]) -> int:
    """The number of training items whose definitions equal an eval item's (0 expected)."""
    banned = {defs_key(e) for e in eval_}
    return sum(defs_key(t) in banned for t in train)


def ask_example(ex: dict) -> dict:
    """A copy asking for the queried variable itself (plan "Measure" (6), the ask baseline)."""
    d = copy.deepcopy(ex)
    d["question"] = f"What is the number for {ex['queried_term']}?"
    d["answer"] = ex["queried_value"]
    d["ask"] = True
    return d


def _dist(items: list[dict]) -> dict:
    n = len(items)
    y1 = Counter(str(e["queried_value"])[0] for e in items)
    ans = [e["answer"] for e in items]
    ys = [e["queried_value"] for e in items]
    bins = Counter(min(a // 200, 5) for a in ans)
    return {"n": n,
            "y_first_digit": {d: round(y1[d] / n, 3) for d in sorted(y1)},
            "y_ge_10": round(sum(y >= 10 for y in ys) / n, 3),
            "y_mean": round(sum(ys) / n, 1), "answer_mean": round(sum(ans) / n, 1),
            "answer_bins_200": {f"{b * 200}+" if b == 5 else f"{b * 200}-{b * 200 + 199}":
                                round(bins[b] / n, 3) for b in sorted(bins)}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--raw", type=Path, required=True)
    b.add_argument("--eval", type=Path, default=schema.EVAL_DATA)
    b.add_argument("--out", type=Path, default=schema.TRAIN_DATA)
    b.add_argument("--n", type=int, default=schema.N_TRAIN)
    sub.add_parser("check")
    args = ap.parse_args()
    if args.cmd == "build":
        print(json.dumps(build(args.raw, args.eval, args.out, args.n), indent=1))
    train = load_train()
    eval_, _ = load_eval()
    print(json.dumps({"overlaps": check_no_overlap(train, eval_),
                      "train": _dist(train), "eval": _dist(eval_)}, indent=1))


if __name__ == "__main__":
    main()
