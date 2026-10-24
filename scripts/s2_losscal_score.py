"""Score Jacob's live-loss calibration labels (scripts/s2_losscal_page.py; Experiment 2
close-out, plan unit 4) against the page's private key.

Inputs:
- `--labels`: the page's labels, as a JSON list of `{"id", "collection", "data"}` (the page's
  copyable summary; an ArtifactData export of the `labels` collection converts to it one doc
  per row); `{"labels": {id: data}}` and a plain `{id: data}` are accepted too.
- `--key`: data/raw/sprint/s2/losscal/key.private.json.

Labels must carry the key's draw fingerprint (the page stamps every saved doc, and it covers
the evidence the page shows); labels from another draw, or with none, are refused (exit 2).
Cards labelled "can't tell" are left out of every denominator and counted separately
(`n_cant_tell`, and per class `cant_tell_by_class`).

Estimates over the held-out cards (the primary population), and separately over the cards
filled from the development half (`development`):
- **precision of judge-lost** = sum w [Jacob says dropped or misstated] / sum w over the
  judge-lost cards, with Jacob's "not an item" counting against (the judge called a non-item
  lost; as S1's scorer does), and `precision_excl_not_item` without those cards;
- **kept agreement** = sum w [Jacob says kept] / sum w over the judge-kept cards;
- **not-an-item share** over all labelled cards;
- **dropped vs misstated**: among judge-lost cards Jacob calls lost, the share where his grade
  matches the judge's final grade, and the full judge-grade x Jacob-label table (counts).
w = 1 / inclusion probability (Horvitz-Thompson ratio estimators, as in S1; cards are a simple
random sample within each stratum). The headline `p` is the weighted share, with a 95%
**task-clustered bootstrap** interval (`ci95`): tasks are resampled with replacement and the
weighted share recomputed, 2,000 replicates, seed `BOOT_SEED` (recorded in the output),
percentile interval; replicates whose resample has no card in the denominator are skipped
and counted (as pre-declared in the close-out plan after the plans' astra r3). The unweighted
share `p_unweighted` gets a Wilson 95% interval (`wilson95_unweighted`).

**Decision line (plan):** when the held-out weighted precision of judge-lost is below 0.5, the
live loss rate is not reported and the pipeline's losses are used only as candidates. The line
is not applied at all (`not_applied_too_few_clear`) when fewer than 15 held-out judge-lost cards
have a clear label (anything but unlabelled or "can't tell"). Otherwise the decision is final only when it can't change: with held-out judge-lost cards unlabelled or "can't
tell", the precision is bounded by counting them all as hits and all as misses, and the
decision is final only when both bounds fall on the same side of 0.5; otherwise it is
"PROVISIONAL" (review r1 #4). With no held-out judge-lost labels it is "NO DATA".

Writes `results/sprint/S2/lossaudit/calibration.json` (committed: numbers, card ids, grades and
labels, no free text) and, next to the key, `score.private.json` (adds Jacob's notes, which may
quote private text; gitignored). Exit 0 when every card is labelled, 1 otherwise (the files are
still written), 2 for labels from another draw.

    uv run python scripts/s2_losscal_score.py --labels labels.json
        [--key data/raw/sprint/s2/losscal/key.private.json]
        [--out results/sprint/S2/lossaudit/calibration.json]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from emx.stats import _wilson

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "data" / "raw" / "sprint" / "s2" / "losscal" / "key.private.json"
PRIVATE_NAME = "score.private.json"
OUT = ROOT / "results" / "sprint" / "S2" / "lossaudit" / "calibration.json"
LABELS = ("kept", "dropped", "misstated", "not_an_item", "cant_tell")
LOST_LABELS = ("dropped", "misstated")
PRECISION_BAR = 0.5
MIN_CLEAR = 15  # held-out judge-lost cards with a clear label before the 0.5 line is applied
Z = 1.96
BOOT_REPS = 2000
BOOT_SEED = 20261024


class DrawMismatch(ValueError):
    pass


def load_labels(obj, draw: str) -> dict[str, dict]:
    """{card id: label data}, from any accepted shape; refuses another draw's labels."""
    if isinstance(obj, list):
        rows = {r["id"]: r.get("data") or {} for r in obj
                if r.get("collection", "labels") == "labels"}
    elif isinstance(obj, dict) and isinstance(obj.get("labels"), dict):
        rows = dict(obj["labels"])
    elif isinstance(obj, dict):
        rows = dict(obj)
    else:
        raise TypeError("labels: expected a list of {id, collection, data} or a mapping")
    bad = sorted(i for i, d in rows.items() if d.get("draw") != draw)
    if bad:
        raise DrawMismatch(f"labels from another draw: {bad[:5]}")
    out = {}
    for i, d in rows.items():
        lab = d.get("label")
        if lab in (None, ""):
            continue
        if lab not in LABELS:
            raise ValueError(f"card {i}: unknown label {lab!r}")
        out[i] = d
    return out


def _wshare(cards: list[dict], hit) -> float | None:
    sw = sum(1 / c["inclusion_p"] for c in cards)
    return sum(1 / c["inclusion_p"] for c in cards if hit(c)) / sw if sw else None


def cluster_bootstrap(cards: list[dict], hit, *, reps: int = BOOT_REPS,
                      seed: int = BOOT_SEED) -> dict:
    """Percentile 95% interval of the weighted share, resampling tasks with replacement."""
    by: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by[str(c.get("task"))].append(c)
    tasks = sorted(by)
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        sample = [c for t in rng.choices(tasks, k=len(tasks)) for c in by[t]]
        v = _wshare(sample, hit)
        if v is not None:
            vals.append(v)
    vals.sort()
    if not vals:
        return {"ci95": None, "n_tasks": len(tasks), "reps_used": 0}
    lo = vals[int(0.025 * (len(vals) - 1))]
    hi = vals[round(0.975 * (len(vals) - 1))]
    return {"ci95": [round(lo, 4), round(hi, 4)], "n_tasks": len(tasks),
            "reps_used": len(vals)}


def ratio(cards: list[dict], hit) -> dict:
    """Weighted share of `cards` for which `hit(card)` is true, with its task-clustered
    bootstrap interval, and the unweighted share with a Wilson interval."""
    n = len(cards)
    if not n:
        return {"n": 0, "k": 0, "p": None, "ci95": None, "p_unweighted": None,
                "wilson95_unweighted": None, "n_tasks": 0, "reps_used": 0}
    p = _wshare(cards, hit)
    k = sum(bool(hit(c)) for c in cards)
    lo, hi = _wilson(k, n, Z)
    return {"n": n, "k": k, "p": round(p, 4), **cluster_bootstrap(cards, hit),
            "p_unweighted": round(k / n, 4),
            "wilson95_unweighted": [round(float(lo), 4), round(float(hi), 4)]}


def estimates(cards: list[dict]) -> dict:
    lost = [c for c in cards if c["judge_class"] == "lost"]
    kept = [c for c in cards if c["judge_class"] == "kept"]
    agree = [c for c in lost if c["label"] in LOST_LABELS]
    return {
        "precision_lost": ratio(lost, lambda c: c["label"] in LOST_LABELS),
        "precision_lost_excl_not_item": ratio(
            [c for c in lost if c["label"] != "not_an_item"],
            lambda c: c["label"] in LOST_LABELS),
        "kept_agreement": ratio(kept, lambda c: c["label"] == "kept"),
        "not_an_item_share": ratio(cards, lambda c: c["label"] == "not_an_item"),
        "dropped_vs_misstated_agreement": ratio(agree, lambda c: c["label"] == c["judge_grade"]),
    }


def bounds(cards: list[dict]) -> dict:
    """Held-out weighted precision of judge-lost with every unresolved card (unlabelled or
    can't tell) counted as a miss (`low`) and as a hit (`high`)."""
    lost = [c for c in cards if c["half"] == "heldout" and c["judge_class"] == "lost"]
    if not lost:
        return {"low": None, "high": None, "n_unresolved": 0}
    unres = [c for c in lost if c["label"] in (None, "cant_tell")]
    sw = sum(1 / c["inclusion_p"] for c in lost)
    hit = sum(1 / c["inclusion_p"] for c in lost if c["label"] in LOST_LABELS)
    ur = sum(1 / c["inclusion_p"] for c in unres)
    return {"low": round(hit / sw, 4), "high": round((hit + ur) / sw, 4),
            "n_unresolved": len(unres)}


def decide(p: float | None, b: dict, n_clear: int) -> tuple[str, bool]:
    """(decision line, final). `n_clear`: held-out judge-lost cards with a clear label."""
    if p is None:
        return "NO DATA: no held-out judge-lost card labelled", False
    if n_clear < MIN_CLEAR:
        return "not_applied_too_few_clear", False
    side = "not reported" if p < PRECISION_BAR else "reported"
    final = b["n_unresolved"] == 0 or (b["high"] < PRECISION_BAR) or (b["low"] >= PRECISION_BAR)
    if not final:
        return (f"PROVISIONAL: precision {p:.2f} so far, but {b['n_unresolved']} held-out "
                f"judge-lost cards are unresolved (bounds {b['low']:.2f}-{b['high']:.2f} span "
                f"{PRECISION_BAR})"), False
    if side == "not reported":
        return (f"precision {p:.2f} < {PRECISION_BAR}: the live loss rate is not reported; "
                "the pipeline's losses are used as candidates only"), True
    return (f"precision {p:.2f} >= {PRECISION_BAR}: the live loss rate is reported, with this "
            "precision alongside"), True


def confusion(cards: list[dict]) -> dict:
    t: dict = defaultdict(lambda: defaultdict(int))
    for c in cards:
        t[str(c["judge_grade"])][c["label"]] += 1
    return {g: dict(sorted(v.items())) for g, v in sorted(t.items())}


def score(key: dict, labels: dict[str, dict]) -> dict:
    cards = []
    for cid in key["order"]:
        k = key["cards"][cid]
        lab = labels.get(cid)
        cards.append({"card_id": cid, **k, "label": lab.get("label") if lab else None,
                      "note": (lab or {}).get("note") or ""})
    labelled = [c for c in cards if c["label"] and c["label"] != "cant_tell"]
    held = [c for c in labelled if c["half"] == "heldout"]
    prim = estimates(held)
    b = bounds(cards)
    n_clear = prim["precision_lost"]["n"]  # held-out judge-lost, can't tell left out
    decision, final = decide(prim["precision_lost"]["p"], b, n_clear)
    n_lab = sum(bool(c["label"]) for c in cards)
    lost_conf = [c for c in labelled if c["judge_class"] == "lost" and c["label"] in LOST_LABELS]
    return {
        "what": "Experiment 2 close-out: Jacob's blind labels of the S1 loss pipeline on live "
                "boundaries (plan unit 4)",
        "scored_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "draw": key["draw"], "seed": key["seed"], "page_sha256": key.get("page_sha256"),
        "n_cards": len(cards), "n_labelled": n_lab,
        "n_cant_tell": sum(c["label"] == "cant_tell" for c in cards),
        "cant_tell_by_class": {
            f"{cl}/{h}": sum(c["label"] == "cant_tell" and c["judge_class"] == cl
                             and c["half"] == h for c in cards)
            for cl in ("lost", "kept") for h in ("heldout", "development")},
        "complete": n_lab == len(cards),
        "weights": "w = 1 / inclusion probability (stratum = judge class x subject, per half; "
                   "simple random sample within a stratum)",
        "interval": {"ci95": "weighted share, task-clustered bootstrap percentile 95%",
                     "wilson95_unweighted": "Wilson 95% on the unweighted share",
                     "bootstrap_reps": BOOT_REPS, "bootstrap_seed": BOOT_SEED},
        "heldout": prim,
        "development": estimates([c for c in labelled if c["half"] == "development"]),
        "n_cards_by_half": {h: sum(c["half"] == h for c in cards)
                            for h in ("heldout", "development")},
        "n_fill": {f: sum(c.get("fill") == f for c in cards)
                   for f in ("development", "other_subject")},
        "n_boundaries": {h: len({c["run_key"] for c in cards if c["half"] == h})
                         for h in ("heldout", "development")},
        "confusion_judge_grade_x_label": confusion(labelled),
        "by_subject_precision_lost": {
            s: ratio([c for c in held if c["judge_class"] == "lost" and c["subject"] == s],
                     lambda c: c["label"] in LOST_LABELS)
            for s in sorted({c["subject"] for c in held})},
        "decision": decision,
        "decision_final": final,
        "n_clear_heldout_lost": n_clear,
        "min_clear": MIN_CLEAR,
        "precision_lost_bounds": b,
        "precision_bar": PRECISION_BAR,
        "jacob_confirmed_losses": [
            {k: c.get(k) for k in ("card_id", "run_key", "item_id", "type", "subject",
                                   "planted_rule", "judge_grade", "label", "half")}
            for c in lost_conf],
        "cards": [{k: c.get(k) for k in (
            "card_id", "run_key", "item_id", "rid", "type", "subject", "channel", "half",
            "stratum", "stratum_N", "stratum_n", "inclusion_p", "fill", "judge_class",
            "judge_grade", "first_grade", "label")} | {"has_note": bool(c["note"])}
            for c in cards],
    }


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--key", default=str(KEY))
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    key = json.loads(Path(a.key).read_text())
    try:
        labels = load_labels(json.loads(Path(a.labels).read_text()), key["draw"])
    except DrawMismatch as e:
        print(str(e), file=sys.stderr)
        return 2
    res = score(key, labels)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1) + "\n")
    priv = Path(a.key).with_name(PRIVATE_NAME)  # notes stay next to the gitignored key
    notes = {c: (labels.get(c) or {}).get("note") or "" for c in key["order"]}
    _write_private(priv, json.dumps(res | {"notes": notes}, indent=1) + "\n")
    h = res["heldout"]
    print(json.dumps({"out": str(out), "labelled": f"{res['n_labelled']}/{res['n_cards']}",
                      "precision_lost": h["precision_lost"], "kept_agreement": h["kept_agreement"],
                      "decision": res["decision"], "private": str(priv)}, indent=1))
    return 0 if res["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
