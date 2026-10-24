"""Score Jacob's live-loss calibration labels (scripts/s2_losscal_page.py; Experiment 2
close-out, plan unit 4) against the page's private key.

Inputs:
- `--labels`: the page's labels, as a JSON list of `{"id", "collection", "data"}` (the page's
  copyable summary; an ArtifactData export of the `labels` collection converts to it one doc
  per row); `{"labels": {id: data}}` and a plain `{id: data}` are accepted too.
- `--key`: data/raw/sprint/s2/losscal/key.private.json.

Labels must carry the key's draw fingerprint (the page stamps every saved doc, and it covers
the evidence the page shows); labels from another draw, or with none, are refused (exit 2).
Cards labelled "can't tell" are left out of every estimate.

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
random sample within each stratum). Each estimate gets a Wilson 95% interval on the Kish
effective n, (sum w)^2 / sum w^2, as S1 does: an **approximate** interval, which ignores the
dependence between items of one boundary (the number of sampled boundaries is reported).

**Decision line (plan):** when the held-out weighted precision of judge-lost is below 0.5, the
live loss rate is not reported and the pipeline's losses are used only as candidates. The
decision is final only when it can't change: with held-out judge-lost cards unlabelled or "can't
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
Z = 1.96


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


def ratio(cards: list[dict], hit) -> dict:
    """Weighted share of `cards` for which `hit(card)` is true, with a Wilson interval on the
    Kish effective n and the unweighted count."""
    n = len(cards)
    if not n:
        return {"n": 0, "k": 0, "p": None, "p_unweighted": None, "ci95": None, "n_eff": 0.0}
    w = [1 / c["inclusion_p"] for c in cards]
    sw, sw2 = sum(w), sum(x * x for x in w)
    hw = sum(x for x, c in zip(w, cards, strict=True) if hit(c))
    p = hw / sw
    n_eff = sw * sw / sw2
    lo, hi = _wilson(p * n_eff, n_eff, Z)
    k = sum(bool(hit(c)) for c in cards)
    return {"n": n, "k": k, "p": round(p, 4), "p_unweighted": round(k / n, 4),
            "ci95": [round(float(lo), 4), round(float(hi), 4)], "n_eff": round(n_eff, 2)}


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


def decide(p: float | None, b: dict) -> tuple[str, bool]:
    """(decision line, final)."""
    if p is None:
        return "NO DATA: no held-out judge-lost card labelled", False
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
    decision, final = decide(prim["precision_lost"]["p"], b)
    n_lab = sum(bool(c["label"]) for c in cards)
    lost_conf = [c for c in labelled if c["judge_class"] == "lost" and c["label"] in LOST_LABELS]
    return {
        "what": "Experiment 2 close-out: Jacob's blind labels of the S1 loss pipeline on live "
                "boundaries (plan unit 4)",
        "scored_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "draw": key["draw"], "seed": key["seed"], "page_sha256": key.get("page_sha256"),
        "n_cards": len(cards), "n_labelled": n_lab,
        "n_cant_tell": sum(c["label"] == "cant_tell" for c in cards),
        "complete": n_lab == len(cards),
        "weights": "w = 1 / inclusion probability (stratum = judge class x subject, per half; "
                   "simple random sample within a stratum)",
        "interval": "Wilson 95% on the Kish effective n: approximate, ignores clustering of "
                    "items within a boundary",
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
