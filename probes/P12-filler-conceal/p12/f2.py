"""P12 follow-up F2 (exploratory, laptop, $0; log.md "Follow-up F2"), on the pulled results/.

    uv run python -m p12.f2 [--results DIR]   # from the probe dir; writes DIR/f2.json

(a) **Modal-value monitor:** per item, arm and reader, the most frequent integer (0..999, the
    single-token integers of V4 Flash) among the filler cells' top-1 tokens (ties: the smaller
    count of cells is irrelevant, the earliest-reached value wins by `Counter.most_common`); the
    share of items where it is y, d, another role value, or something else.
(b) **Conceal-specific contrasts** on y's filler-cell share: C - M, C - D, R - M, CD - D,
    paired by item (`analyze.boot_paired`).
(c) **Uplift robustness:** filler-only accuracy of each arm vs P (paired), and the uplift minus
    P's on items whose no-filler replies (the arm's and P's) contain no letters.
(d) **Layer profile:** first-hit layer of d and y under D and CD (median, paired difference d - y
    on items where both hit), and the share of d's and y's hit cells in layer bands 0-20, 21-30,
    31-42.
(e) **Decoy as answer** (added after seeing sample A, post hoc): per arm and filler, the number of
    replies whose parsed answer equals the item's d.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from . import analyze as AN
from .arms import ARMS, load_items
from .schema import CAND_NAMES, READERS, RESULTS, ROLE_NAMES

CONTRASTS = (("C", "M"), ("C", "D"), ("R", "M"), ("CD", "D"))
BANDS = ((0, 20), (21, 30), (31, 42))


def int_ids(tok: Any) -> dict[int, int]:
    """token id -> integer value for 0..999 (each a single token in V4 Flash, P06 F8)."""
    out = {}
    for v in range(1000):
        enc = tok.encode(str(v), add_special_tokens=False)
        if len(enc) == 1 and tok.decode(enc) == str(v):
            out[int(enc[0])] = v
    return out


def modal_value(top1: np.ndarray, id2int: dict[int, int]) -> int | None:
    vals = [id2int[t] for t in top1.ravel().tolist() if t in id2int]
    return Counter(vals).most_common(1)[0][0] if vals else None


def classify(v: int | None, item: dict) -> str:
    if v is None:
        return "none"
    if v == item["roles"]["y"]:
        return "y"
    if v == item["d"]:
        return "d"
    if v in {item["roles"][r] for r in ROLE_NAMES}:
        return "other_role"
    return "other"


def layer_profile(top1: np.ndarray, tid: int) -> list[int]:
    hit = top1 == tid
    return [int(hit[lo:hi + 1].sum()) for lo, hi in BANDS]


def run(results: Path, tok: Any) -> dict:
    items = load_items()
    id2int = int_ids(tok)
    modal: dict[str, dict[str, Counter]] = {r: {a: Counter() for a in ARMS} for r in READERS}
    first: dict[str, dict[str, dict[str, list]]] = {
        r: {a: {"y": [], "d": []} for a in ("D", "CD")} for r in READERS}
    bands: dict[str, dict[str, dict[str, np.ndarray]]] = {
        r: {a: {"y": np.zeros(3, int), "d": np.zeros(3, int)} for a in ("D", "CD")}
        for r in READERS}
    for it in items:
        for arm in ARMS:
            p = AN.rec_path(results, it["idx"], arm)
            if not p.exists():
                continue
            rec = AN.load_rec(p)
            ids = dict(zip(CAND_NAMES, rec["cand_ids"].tolist(), strict=True))
            for r, ro in rec["ro"].items():
                modal[r][arm][classify(modal_value(ro["top1"], id2int), it)] += 1
                if arm in ("D", "CD"):
                    for role in ("y", "d"):
                        hit = ro["top1"] == ids[role]
                        first[r][arm][role].append(AN.first_layer(hit))
                        bands[r][arm][role] += layer_profile(ro["top1"], ids[role])
    out: dict[str, Any] = {"modal": {}, "contrasts": {}, "layers": {}}
    for r in READERS:
        out["modal"][r] = {a: {k: c[k] for k in ("y", "d", "other_role", "other", "none")}
                           | {"n": sum(c.values())} for a, c in modal[r].items()}
        lay = {}
        for a in ("D", "CD"):
            fy, fd = first[r][a]["y"], first[r][a]["d"]
            both = [(y, d) for y, d in zip(fy, fd, strict=True)
                    if y is not None and d is not None]
            lay[a] = {"median_first_y": float(np.median([y for y in fy if y is not None])),
                      "median_first_d": float(np.median([d for d in fd if d is not None])),
                      "d_minus_y": AN.boot_median([d - y for y, d in both]),
                      "bands": {role: (bands[r][a][role] / max(1, bands[r][a][role].sum()))
                                .round(3).tolist() for role in ("y", "d")},
                      "band_edges": [list(b) for b in BANDS]}
        out["layers"][r] = lay
    stats, _ = AN.collect(results, items)
    for r in READERS:
        out["contrasts"][r] = {f"{a}-{b}": AN.boot_paired_from(stats[r], a, b)
                               for a, b in CONTRASTS}
    rows = AN.read_jsonl(results / "answers.jsonl")
    out["uplift_robust"] = uplift_robust(rows, items)
    out["decoy_answers"] = decoy_answers(rows, items)
    return out


def decoy_answers(rows: list[dict], items: list[dict]) -> dict:
    d = {it["idx"]: it["d"] for it in items}
    out: dict[str, int] = {}
    for (idx, arm, k), r in sorted(AN.answer_table(rows).items()):
        key = f"{arm}|{k}"
        out[key] = out.get(key, 0) + int(r.get("predicted") == d[idx])
    return out


def uplift_robust(rows: list[dict], items: list[dict]) -> dict:
    tab = AN.answer_table(rows)
    idxs = sorted(it["idx"] for it in items)
    out: dict[str, Any] = {}
    for arm in ARMS:
        both = [i for i in idxs if (i, arm, 100) in tab and (i, "P", 100) in tab]
        fa = np.array([float(tab[(i, arm, 100)]["correct"]) for i in both])
        fp = np.array([float(tab[(i, "P", 100)]["correct"]) for i in both])
        clean = [i for i in idxs if all(
            (i, a, k) in tab for a in (arm, "P") for k in (0, 100)) and not any(
            AN._LETTERS.search(str(tab[(i, a, 0)]["gen_text"])) for a in (arm, "P"))]

        def u(a: str, ids: list[int]) -> np.ndarray:
            return np.array([float(tab[(i, a, 100)]["correct"]) - float(tab[(i, a, 0)]["correct"])
                             for i in ids])

        out[arm] = {"filler_acc_vs_P": AN.boot_paired(fa, fp),
                    "clean_n": len(clean), "clean_uplift": AN.boot_mean(u(arm, clean)),
                    "clean_uplift_minus_P": AN.boot_mean(u(arm, clean) - u("P", clean)),
                    # review r1: retention against the plan's "half of P's uplift" bar
                    "clean_uplift_minus_half_P": AN.boot_mean(u(arm, clean)
                                                              - 0.5 * u("P", clean))}
    return out


def main(argv: list[str] | None = None) -> int:
    from p06 import v4pilot as V
    from transformers import AutoTokenizer

    ap = argparse.ArgumentParser(prog="p12.f2")
    ap.add_argument("--results", type=Path, default=RESULTS)
    a = ap.parse_args(argv)
    tok = AutoTokenizer.from_pretrained(V.MODEL_ID, revision=V.MODEL_REV, local_files_only=True)
    res = AN._round(run(a.results, tok))
    (a.results / "f2.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
