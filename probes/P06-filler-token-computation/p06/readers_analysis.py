"""P06 F3 analysis (laptop): first-digit readouts of the chain values, the filler-KV transplant,
and the local-vs-API uplift check, from `p06.stage2`'s pulled outputs.

    uv run python -m p06.readers_analysis            # from the probe dir; defaults below

**Readouts (first digit only).** Gemma 4 tokenizes numbers digit by digit, so a single-token
readout can only score a value's first digit. For each of Brauer's five values (x, c1*x, y,
c2*y, answer), the score in a (layer, slot) cell is the log-prob of the value's first digit after
mean subtraction (per reader, arm, layer and slot, the mean over items of each digit's log-prob
is subtracted. Brauer et al. 2026 §5 Stage 2 subtract the mean *probability*; this
module uses log-probs, and `p06.f3_sensitivity` reruns with probabilities (review r1 finding 2). An item's statistic is the max of
that score over the cells read, restricted to the second half of the model (layers >=
MIN_LAYER). Null: for each item, the same role's value from another item (seeded) whose first
digit differs; the threshold is the 95th percentile of the null statistics, per reader, arm and
role. A value is *recovered* when its statistic exceeds the threshold. The peak cell is the
argmax over cells of the mean (over items) score of the true first digit. First digits of the
five values often coincide within an item (e.g. y = 93, answer = 183 -> "9", "1"), so a hit is
evidence about a digit, not a value.

**Transplant.** Rates of the transplanted output equalling the donor's answer, equalling the
target's answer, and differing from the un-transplanted target output; for all pairs and for the
pairs where target and donor are both answered correctly without transplant (Brauer's
"both-correct" pairs). The same for the random-donor control (vs the random donor's answer) and
the self-transplant (must never change). 95% intervals: bootstrap over pairs (seed 0).

**Local uplift.** Local accuracy per arm, and dots100 - base and dots100 - pre100 paired by item,
next to the same uplifts from F1's API rows on the same items; the check is local >= half API.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

from .stage2 import N_LAYERS, ROLES, read_npz

MIN_LAYER = N_LAYERS // 2  # Brauer's "second half of the model"
N_BOOT = 10_000
SEED = 0
NULL_Q = 95.0


def first_digit(v: int) -> int:
    return int(str(abs(int(v)))[0])


def load_records(root: Path) -> list[dict]:
    return [read_npz(p) for p in sorted(Path(root).glob("*.npz"))]


def stack_arm(recs: list[dict], reader: str, arm: str):
    """(records sorted by idx, slots [S], digit log-probs [n, L, S, 10] fp32); every record in
    the arm must carry the same slots."""
    rs = sorted((r for r in recs if r["arm"] == arm and reader in r["ro"]),
                key=lambda r: r["idx"])
    if not rs:
        return [], np.zeros(0, dtype=np.int16), np.zeros((0, 0, 0, 10), dtype=np.float32)
    slots = rs[0]["slots"]
    for r in rs:
        if not np.array_equal(r["slots"], slots):
            raise ValueError(f"{r['item_id']}/{arm}: slots differ from {rs[0]['item_id']}")
    return rs, slots, np.stack([r["ro"][reader]["digit_lp"].astype(np.float32) for r in rs])


def boot_rate(x: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    x = np.asarray(x, dtype=float)
    if not len(x):
        return {"n": 0, "rate": None}
    rng = np.random.default_rng(seed)
    bs = x[rng.integers(0, len(x), size=(n_boot, len(x)))].mean(axis=1)
    return {"n": len(x), "rate": float(x.mean()),
            "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}


def matched_null_digits(vals: list[int], seed: int) -> list[int | None]:
    """Per item, the first digit of the same role's value from another item (seeded) whose
    first digit differs from this item's."""
    rng = random.Random(seed)
    fd = [first_digit(v) for v in vals]
    out = []
    for i in range(len(vals)):
        cands = [fd[j] for j in range(len(vals)) if j != i and fd[j] != fd[i]]
        out.append(rng.choice(cands) if cands else None)
    return out


def recovery(recs: list[dict], reader: str, arm: str, min_layer: int = MIN_LAYER,
             seed: int = SEED) -> dict:
    rs, slots, lp = stack_arm(recs, reader, arm)
    if not rs:
        return {"n_items": 0}
    if lp.shape[1] <= min_layer:
        raise ValueError(f"{lp.shape[1]} layers stored; min_layer {min_layer} leaves none")
    res = lp - lp.mean(axis=0, keepdims=True)  # [n, L, S, 10]
    sub = res[:, min_layer:]  # [n, L', S, 10]
    cell_max = sub.max(axis=(1, 2))  # [n, 10]: per item, each digit's max over cells
    out: dict = {"n_items": len(rs), "min_layer": min_layer, "slots": [int(s) for s in slots],
                 "roles": {}}
    for k, role in enumerate(ROLES):
        vals = [r["values"][role] for r in rs]
        fd = np.array([first_digit(v) for v in vals])
        stat = cell_max[np.arange(len(rs)), fd]
        nd = matched_null_digits(vals, seed + k)
        keep = [i for i, d in enumerate(nd) if d is not None]
        null = np.array([cell_max[i, nd[i]] for i in keep])
        tau = float(np.percentile(null, NULL_Q)) if len(null) else float("inf")
        mean_map = sub[np.arange(len(rs)), :, :, fd].mean(axis=0)  # [L', S]
        li, si = np.unravel_index(int(mean_map.argmax()), mean_map.shape)
        per_item_peak = [np.unravel_index(int(sub[i, :, :, fd[i]].argmax()), mean_map.shape)
                         for i in range(len(rs))]
        out["roles"][role] = {
            "label": "first digit only", "threshold": tau,
            "recovered": boot_rate(stat > tau, seed=seed),
            "null_fpr": float(np.mean(null > tau)) if len(null) else None,
            "peak_layer": int(li) + min_layer, "peak_slot": int(slots[si]),
            "peak_token_id": int(rs[0]["token_ids"][si]),
            "peak_mean_score": float(mean_map[li, si]),
            "item_peak_slot_mode": int(slots[np.bincount(
                [int(si_) for _, si_ in per_item_peak]).argmax()]),
        }
    return out


# --------------------------------------------------------------------- transplant


def latest(rows: list[dict], key) -> list[dict]:
    """The last row per key (a rerun can append an item's rows twice)."""
    return list({key(r): r for r in rows}.values())


def transplant_summary(rows: list[dict], seed: int = SEED) -> dict:
    rows = latest(rows, lambda r: r["idx"])

    def rates(rs: list[dict]) -> dict:
        def b(f):
            return boot_rate(np.array([float(f(r)) for r in rs]), seed=seed)

        return {
            "n": len(rs),
            "tx_eq_donor_answer": b(lambda r: r["tx_pred"] == r["donor_answer"]),
            "tx_eq_target_answer": b(lambda r: r["tx_pred"] == r["answer"]),
            "tx_changed": b(lambda r: r["tx_text"] != r["target_text"]),
            "rand_tx_eq_rand_answer": b(lambda r: r["rtx_pred"] == r["rand_answer"]),
            "rand_tx_changed": b(lambda r: r["rtx_text"] != r["target_text"]),
            "self_changed": int(sum(r["self_text"] != r["target_text"] for r in rs)),
        }

    both = [r for r in rows if r["target_pred"] == r["answer"]
            and r["donor_pred"] == r["donor_answer"]]
    return {"all_pairs": rates(rows), "both_correct": rates(both)}


# --------------------------------------------------------------------- local vs API


def accuracy_by_arm(rows: list[dict]) -> dict:
    acc: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        acc[r["item_id"]][r["arm"]].append(float(r["correct"]))
    return acc


def paired_uplift(acc: dict, ids: list[str], a: str, b: str) -> dict:
    d = np.array([np.mean(acc[i][a]) - np.mean(acc[i][b]) for i in ids
                  if acc[i].get(a) and acc[i].get(b)])
    return {"n": len(d), "uplift": float(d.mean()) if len(d) else None}


def local_vs_api(answers: list[dict], api_rows: list[dict]) -> dict:
    answers = latest(answers, lambda r: (r["item_id"], r["arm"]))
    loc = accuracy_by_arm(answers)
    ids = sorted(loc)
    api = accuracy_by_arm([r for r in api_rows if r["item_id"] in loc])
    out: dict = {"local_acc": {
        a: float(np.mean([np.mean(loc[i][a]) for i in ids if loc[i].get(a)]))
        for a in ("base", "pre100", "dots100")}}
    for b in ("base", "pre100"):
        lu, au = paired_uplift(loc, ids, "dots100", b), paired_uplift(api, ids, "dots100", b)
        out[f"dots100_minus_{b}"] = {
            "local": lu, "api": au,
            "local_ge_half_api": (
                None if lu["uplift"] is None or au["uplift"] is None or au["uplift"] <= 0
                else bool(lu["uplift"] >= 0.5 * au["uplift"]))}
    return out


# --------------------------------------------------------------------- main


def analyze(recs: list[dict], transplant_rows: list[dict] | None = None,
            answers: list[dict] | None = None, api_rows: list[dict] | None = None,
            min_layer: int = MIN_LAYER) -> dict:
    readers = sorted({n for r in recs for n in r["ro"]})
    out: dict = {"n_records": len(recs), "readers": readers, "recovery": {}}
    for rd in readers:
        out["recovery"][rd] = {arm: recovery(recs, rd, arm, min_layer)
                               for arm in ("dots100", "base")}
    if transplant_rows:
        out["transplant"] = transplant_summary(transplant_rows)
    if answers:
        out["local_vs_api"] = local_vs_api(answers, api_rows or [])
        rows = [r for r in latest(answers, lambda r: (r["item_id"], r["arm"])) if "read_n" in r]
        n = sum(r["read_n"] for r in rows)
        out["lens_model_agreement"] = sum(r["read_agree"] for r in rows) / n if n else None
    return out


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def main(argv: list[str] | None = None) -> int:
    from .api import latest_ok

    ap = argparse.ArgumentParser(prog="p06.readers_analysis")
    ap.add_argument("--stage2", default="results/stage2")
    ap.add_argument("--api", default="results/stage1/f1_brauer_gemma4.jsonl")
    ap.add_argument("--min-layer", type=int, default=MIN_LAYER)
    ap.add_argument("--out", default="results/stage2/analysis.json")
    a = ap.parse_args(argv)
    s2 = Path(a.stage2)
    api_rows = list(latest_ok(Path(a.api)).values()) if Path(a.api).exists() else []
    res = analyze(load_records(s2 / "readouts"), read_jsonl(s2 / "transplant.jsonl"),
                  read_jsonl(s2 / "answers.jsonl"), api_rows, a.min_layer)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1) + "\n")
    for rd in res["readers"]:
        for arm, v in res["recovery"][rd].items():
            for role, x in v.get("roles", {}).items():
                print(rd, arm, role, "(first digit only)", x["recovered"], "peak L",
                      x["peak_layer"], "slot", x["peak_slot"])
    if "transplant" in res:
        print(json.dumps(res["transplant"], indent=1))
    if "local_vs_api" in res:
        print(json.dumps(res["local_vs_api"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
