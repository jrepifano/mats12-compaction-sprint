"""Score Jacob's Experiment 1 labels against the private key (spec docs/sprint/specs/S1.md, unit
U5; decisions D1 and D6). Pattern of scripts/e13_label_score.py.

Inputs:
- `--labels`: the page's labels, as a JSON list of `{"id", "collection", "data"}` (the format of
  the page's copyable summary; an ArtifactData export of the `labels`, `gold` and `audit`
  collections converts to it one doc per row). `{collection: {id: data}}` is accepted too.
- `--key`: data/raw/sprint/s1/labels/key.private.json from scripts/s1_label_page.py.

Blind labels. For each judge, each verdict and each half (dev, held, and both), on the units
that judge listed (it has a verdict for them) and that Jacob labelled (claims labelled "can't
tell" are left out):
- precision = sum w [judge = v and Jacob = v] / sum w [judge = v];
- recall = sum w [judge = v and Jacob = v] / sum w [Jacob = v];
- `recall_any` also counts units the judge never listed as misses (pass 1a listing recall
  mixed in; reported, not gated);
with w = 1 / inclusion probability (Horvitz-Thompson ratio estimators, D6), and a Wilson 95%
interval on the Kish effective n, (sum w)^2 / sum w^2, of the denominator. Jacob's "not an
item" counts against every item verdict (the judge graded something that isn't an item). An
estimate whose denominator has fewer than 10 units is marked "unvalidated (n<10)".

Headline verdicts: `dropped` (pass 1b) and `invented` (pass 2 unsupported on units whose
history is complete); `unsupported` over all units is descriptive (`--claim-gate unsupported`
gates on it instead, for a sensitivity check only). Secondary: kept, misstated, supported,
contradicted. **Gate (D1):** a judge passes a headline
verdict when its held-out point estimates of precision and recall are both >= 0.8; the
intervals are reported, never gated on. A judge with no held-out data for a verdict gets
"NO DATA" (not a pass). The pass 2 gate uses only claim units anchored exact or normalized
(`anchor_tier`), as the primary invention outcomes do; the all-tiers result is reported as
descriptive (`<verdict>_all_tiers_descriptive`).

Gold windows. Jacob's lines (one per line, bullets and numbering stripped) are matched one to
one to each judge's valid items in the window's message range, and to the summary's valid
claims: greedily by descending Jaccard on content words, keeping pairs >= 0.5 (an item matches
on the better of its text and its quote, a claim on the better of its wording and its summary
span). Recall = matched lines / Jacob's lines. Matched and
unmatched lists are written for Jacob to eyeball. `score(..., matcher=f)` swaps in another
matcher (e.g. an LLM) with the same signature as `token_matcher`.

Labels must carry this key's draw fingerprint (the page stamps every saved doc); labels from
another draw are refused (exit 2). Writes `<out>/score.json` (with the gold-window lists, which
quote private sessions; `<out>` must be under data/raw/) and `score.md` (numbers only; anywhere,
`--md-out`). When every unit is labelled it also writes `<out>/LOCKED.json`, the marker the
audit page requires. Exit 0 when complete, 1 otherwise (the files are still written).

Usage: uv run python scripts/s1_label_score.py --labels labels.json
           [--key data/raw/sprint/s1/labels/key.private.json] [--out data/raw/sprint/s1/labels]
           [--md-out PATH] [--claim-gate invented|unsupported]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from compaction.s1_analysis import content_words, jaccard
from emx.stats import _wilson

ROOT = Path(__file__).resolve().parents[1]
KEY = "data/raw/sprint/s1/labels/key.private.json"
OUT = "data/raw/sprint/s1/labels"

ITEM_LABELS = ("kept", "dropped", "misstated", "not_an_item")
CLAIM_LABELS = ("supported", "contradicted", "unsupported", "cant_tell")
ITEM_VERDICTS = ("dropped", "kept", "misstated")
CLAIM_VERDICTS = ("unsupported", "supported", "contradicted", "invented")
HEADLINE_ITEM = "dropped"
GATE = 0.8
MIN_N = 10
MATCH_MIN = 0.5
Z95 = 1.959963984540054
HALVES = ("dev", "held", "all")
ANCHOR_TIERS = ("exact", "normalized", "fuzzy")
PRIMARY_TIERS = frozenset({"exact", "normalized"})
LOCK_NAME = "LOCKED.json"
PRIVATE_ROOTS = [ROOT / "data" / "raw"]

Matcher = Callable[[list[str], list[dict], str], list[tuple[int, int, float]]]


# ------------------------------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------------------------------


def normalize_labels(obj: Any) -> dict[str, dict[str, dict]]:
    """{collection: {id: data}} from a list of {id, collection, data} or that dict itself."""
    out: dict[str, dict[str, dict]] = {"labels": {}, "gold": {}, "audit": {}}
    if isinstance(obj, Mapping):
        for coll, docs in obj.items():
            if isinstance(docs, Mapping):
                out.setdefault(coll, {}).update({str(k): dict(v) for k, v in docs.items()})
            else:
                for d in docs:
                    d = dict(d)
                    out.setdefault(coll, {})[str(d.pop("id"))] = d
        return out
    for row in obj:
        coll = row.get("collection")
        if coll is None or "id" not in row:
            raise ValueError(f"label row needs id and collection: {row!r}")
        out.setdefault(coll, {})[str(row["id"])] = dict(row.get("data") or {})
    return out


# ------------------------------------------------------------------------------------------
# Weighted precision and recall
# ------------------------------------------------------------------------------------------


def kish(weights: Sequence[float]) -> float:
    s, s2 = sum(weights), sum(w * w for w in weights)
    return s * s / s2 if s2 > 0 else 0.0


def weighted_share(hits: Sequence[bool], weights: Sequence[float]) -> dict:
    """sum w [hit] / sum w, with a Wilson 95% interval on the Kish effective n."""
    n = len(weights)
    if n == 0 or sum(weights) <= 0:
        return {"point": None, "lo": None, "hi": None, "n": 0, "n_eff": 0.0, "status": "no data"}
    p = sum(w for h, w in zip(hits, weights, strict=True) if h) / sum(weights)
    ne = kish(weights)
    lo, hi = _wilson(p * ne, ne, Z95)
    return {
        "point": p,
        "lo": float(lo),
        "hi": float(hi),
        "n": n,
        "n_hits": int(sum(bool(h) for h in hits)),
        "n_eff": ne,
        "status": "ok" if n >= MIN_N else f"unvalidated (n<{MIN_N})",
    }


def _jacob(units: Mapping[str, Mapping], labs: Mapping[str, Mapping]) -> dict[str, str]:
    """unit id -> Jacob's label, for labelled units with a valid choice."""
    out = {}
    for uid, u in units.items():
        lab = (labs.get(uid) or {}).get("label")
        allowed = ITEM_LABELS if u["kind"] == "item" else CLAIM_LABELS
        if lab in allowed:
            out[uid] = lab
    return out


def verdict_scores(
    units: Mapping[str, Mapping],
    jacob: Mapping[str, str],
    judge: str,
    verdict: str,
    half: str,
    tier: str | None = None,
    tiers: frozenset[str] | None = None,
) -> dict:
    """Precision, recall and recall_any of one judge's `verdict` on one half; `tier` restricts
    claim units to one anchor tier (`anchor_tier` in the key: exact, normalized, fuzzy), and
    `tiers` to a set of them."""
    kind = "item" if verdict in ITEM_VERDICTS else "claim"
    base = verdict
    if verdict == "invented":
        base = "unsupported"
    pool = []
    for uid, u in units.items():
        if u["kind"] != kind or uid not in jacob or jacob[uid] == "cant_tell":
            continue
        if half != "all" and u["half"] != half:
            continue
        if verdict == "invented" and not u.get("history_complete"):
            continue
        if tier is not None and u.get("anchor_tier") != tier:
            continue
        if tiers is not None and u.get("anchor_tier") not in tiers:
            continue
        pool.append((uid, u))
    w = {uid: 1.0 / u["pi"] for uid, u in pool}
    listed = [(uid, u) for uid, u in pool if judge in u["verdicts"]]
    pred = [(uid, u) for uid, u in listed if u["verdicts"][judge] == base]
    truth = [(uid, u) for uid, u in listed if jacob[uid] == base]
    truth_any = [(uid, u) for uid, u in pool if jacob[uid] == base]
    return {
        "precision": weighted_share(
            [jacob[uid] == base for uid, _ in pred], [w[uid] for uid, _ in pred]
        ),
        "recall": weighted_share(
            [u["verdicts"][judge] == base for _, u in truth], [w[uid] for uid, _ in truth]
        ),
        "recall_any": weighted_share(
            [u["verdicts"].get(judge) == base for _, u in truth_any],
            [w[uid] for uid, _ in truth_any],
        ),
        "n_listed": len(listed),
    }


CLAIM_PRECEDENCE = ("unsupported", "contradicted", "supported")


def primary_tier_units(units: Mapping[str, Mapping]) -> dict[str, dict]:
    """Claim units as the pass 2 gate sees them: each judge's claims on the span filtered to the
    primary anchor tiers (exact, normalized) before taking the worst verdict; a judge left with
    no claim on the span counts as not having listed it, and a span no judge anchors in a
    primary tier is not eligible. Units without per-claim tiers fall back to `anchor_tier`."""
    out: dict[str, dict] = {}
    for uid, u in units.items():
        if u["kind"] != "claim":
            continue
        if "claims" not in u:
            if u.get("anchor_tier") in PRIMARY_TIERS:
                out[uid] = dict(u)
            continue
        verdicts = {}
        for j, cs in u["claims"].items():
            vs = [c["verdict"] for c in cs if c.get("match") in PRIMARY_TIERS]
            if vs:
                verdicts[j] = min(vs, key=CLAIM_PRECEDENCE.index)
        if verdicts:
            out[uid] = {**u, "verdicts": verdicts}
    return out


def _gate(sc: Mapping) -> str:
    p, r = sc["precision"]["point"], sc["recall"]["point"]
    if p is None or r is None:
        return "NO DATA"
    return "PASS" if p >= GATE and r >= GATE else "FAIL"


# ------------------------------------------------------------------------------------------
# Gold windows
# ------------------------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def parse_lines(text: str | None) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = _BULLET.sub("", line).strip()
        if line:
            out.append(line)
    return out


def _entry_texts(e: Mapping, kind: str) -> list[str]:
    if kind == "item":
        return [e.get("text") or "", e.get("quote") or ""]
    return [e.get("claim") or "", e.get("summary_quote") or ""]


def token_matcher(lines: list[str], entries: list[dict], kind: str) -> list[tuple[int, int, float]]:
    """One-to-one matches (line index, entry index, score): all pairs with Jaccard >= 0.5 on
    content words (an item scores the better of its text and quote, a claim of its wording and
    summary span), taken greedily by
    descending score, ties by line then entry index."""
    lw = [set(content_words(x)) for x in lines]
    ew = [[set(content_words(t)) for t in _entry_texts(e, kind)] for e in entries]
    cands = []
    for i, a in enumerate(lw):
        for j, alts in enumerate(ew):
            s = max((jaccard(a, b) for b in alts), default=0.0)
            if s >= MATCH_MIN:
                cands.append((-s, i, j))
    used_i, used_j, out = set(), set(), []
    for neg, i, j in sorted(cands):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        out.append((i, j, -neg))
    return sorted(out)


def gold_scores(
    gold_key: Mapping[str, Mapping],
    gold_labels: Mapping[str, Mapping],
    matcher: Matcher = token_matcher,
) -> dict:
    """Per window, per judge, for items and claims: recall of Jacob's lines, with the matched
    and unmatched lists; plus totals per judge over the windows Jacob filled in. A judge whose
    run on the record succeeded is scored even when it found nothing (recall 0); a failed or
    missing run is left out and listed in `excluded_windows`."""
    windows: dict[str, Any] = {}
    totals: dict[str, dict[str, list[int]]] = {}
    excluded: dict[str, dict[str, list[str]]] = {}
    for wid, g in sorted(gold_key.items()):
        if "roster" not in g:
            raise ValueError(
                f"gold window {wid} has no run roster: rebuild the page with the dev-run roster "
                "(scripts/s1_label_page.py --roster)"
            )
        lab = gold_labels.get(wid) or {}
        w: dict[str, Any] = {
            "source": g.get("source"),
            "msg_start": g.get("msg_start"),
            "msg_end": g.get("msg_end"),
            "judges": {},
            # judges whose run failed or is missing: not scored on this window
            "failed": g.get("failed", {"items": [], "claims": []}),
            "missing": g.get("missing", {"items": [], "claims": []}),
            "roster": g["roster"],
        }
        for kind, field, rows_by_judge in (
            ("item", "items", g.get("judge_items", {})),
            ("claim", "claims", g.get("judge_claims", {})),
        ):
            lines = parse_lines(lab.get(field))
            w[f"n_jacob_{field}"] = len(lines)
            for judge in sorted(rows_by_judge):
                entries = list(rows_by_judge[judge])
                m = matcher(lines, entries, kind) if lines and entries else []
                mi, mj = {i for i, _, _ in m}, {j for _, j, _ in m}
                res = {
                    "n_jacob": len(lines),
                    "n_judge": len(entries),
                    "n_matched": len(m),
                    "recall": len(m) / len(lines) if lines else None,
                    "matched": [
                        {
                            "jacob": lines[i],
                            "judge_id": entries[j]["id"],
                            "judge": _entry_texts(entries[j], kind)[0],
                            "score": round(s, 3),
                        }
                        for i, j, s in m
                    ],
                    "unmatched_jacob": [x for i, x in enumerate(lines) if i not in mi],
                    "unmatched_judge": [
                        {"id": e["id"], "text": _entry_texts(e, kind)[0]}
                        for j, e in enumerate(entries)
                        if j not in mj
                    ],
                }
                w["judges"].setdefault(judge, {})[field] = res
                if lines:
                    t = totals.setdefault(judge, {}).setdefault(field, [0, 0])
                    t[0] += len(m)
                    t[1] += len(lines)
            if lines:
                for judge in w["failed"].get(field, []) + w["missing"].get(field, []):
                    excluded.setdefault(judge, {}).setdefault(field, []).append(wid)
        windows[wid] = w
    tot = {
        j: {
            f: {"matched": a, "jacob": b, "recall": a / b if b else None} for f, (a, b) in d.items()
        }
        for j, d in sorted(totals.items())
    }
    return {
        "windows": windows,
        "totals": tot,
        "excluded_windows": {j: d for j, d in sorted(excluded.items())},
        "matcher": getattr(matcher, "__name__", "custom"),
        "match_min": MATCH_MIN,
    }


# ------------------------------------------------------------------------------------------
# The draw, the lock and private paths
# ------------------------------------------------------------------------------------------


class DrawMismatch(ValueError):
    """Labels saved by another draw of the page (or with no draw) were given to this key."""


def check_draw(labs: Mapping[str, Mapping[str, Mapping]], key: Mapping) -> None:
    """Refuse unless every label and gold doc carries this key's draw fingerprint."""
    want = key.get("draw")
    bad = sorted(
        f"{coll}/{i}"
        for coll in ("labels", "gold")
        for i, d in labs.get(coll, {}).items()
        if d.get("draw") != want
    )
    if want is None or bad:
        raise DrawMismatch(
            f"{len(bad)} labels are not from draw {want} (e.g. {bad[:5]}): they were saved by "
            "another version of the page"
        )


def require_private(path: Path) -> Path:
    """Refuse a destination outside PRIVATE_ROOTS: score.json quotes private sessions."""
    p = Path(path).resolve()
    for root in PRIVATE_ROOTS:
        r = Path(root).resolve()
        if p == r or r in p.parents:
            return p
    raise ValueError(f"{path} is outside the private roots {[str(r) for r in PRIVATE_ROOTS]}")


def write_lock(out_dir: Path, key: Mapping, labels: Any) -> dict:
    """LOCKED.json: every blind unit is labelled for this draw; the audit page needs it."""
    lock = {
        "locked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "draw": key.get("draw"),
        "n_units": len(key["units"]),
        "key_page_sha256": key.get("page_sha256"),
        "labels_sha256": hashlib.sha256(
            json.dumps(normalize_labels(labels), sort_keys=True).encode()
        ).hexdigest(),
    }
    p = require_private(Path(out_dir)) / LOCK_NAME
    p.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n")
    return lock


# ------------------------------------------------------------------------------------------
# The record
# ------------------------------------------------------------------------------------------


def score(
    labels: Any,
    key: Mapping,
    *,
    claim_gate: str = "invented",
    matcher: Matcher = token_matcher,
) -> dict:
    """The full scoring record (see the module docstring)."""
    if claim_gate not in ("unsupported", "invented"):
        raise ValueError("claim_gate must be unsupported or invented")
    labs = normalize_labels(labels)
    check_draw(labs, key)
    units = key["units"]
    jacob = _jacob(units, labs["labels"])
    judges = sorted({j for u in units.values() for j in u["verdicts"]})
    prim_units = primary_tier_units(units)
    per_judge: dict[str, Any] = {}
    gate: dict[str, Any] = {}
    for j in judges:
        verdicts: dict[str, Any] = {}
        for v in ITEM_VERDICTS + CLAIM_VERDICTS:
            verdicts[v] = {h: verdict_scores(units, jacob, j, v, h) for h in HALVES}
        listed_items = [
            u for u, x in units.items() if x["kind"] == "item" and j in x["verdicts"] and u in jacob
        ]
        not_item = weighted_share(
            [jacob[u] == "not_an_item" for u in listed_items],
            [1.0 / units[u]["pi"] for u in listed_items],
        )
        by_tier = {
            t: {h: verdict_scores(units, jacob, j, "invented", h, tier=t) for h in HALVES}
            for t in ANCHOR_TIERS
        }
        # the pass 2 gate runs on primary-tier units, as the primary outcomes do
        primary = {h: verdict_scores(prim_units, jacob, j, claim_gate, h) for h in HALVES}
        per_judge[j] = {
            "verdicts": verdicts,  # claim verdicts here: all tiers, descriptive
            "claim_gate_primary_tiers": primary,
            "not_an_item_share": not_item,
            "invented_by_anchor_tier": by_tier,  # descriptive split
        }
        g = {
            HEADLINE_ITEM: _gate(verdicts[HEADLINE_ITEM]["held"]),
            claim_gate: _gate(primary["held"]),
            f"{claim_gate}_all_tiers_descriptive": _gate(verdicts[claim_gate]["held"]),
        }
        g["judge"] = "PASS" if g[HEADLINE_ITEM] == g[claim_gate] == "PASS" else "FAIL"
        gate[j] = g
    unl = sorted(u for u in units if u not in jacob)
    cant = sorted(u for u, lab in jacob.items() if lab == "cant_tell")
    return {
        "n_units": len(units),
        "n_labelled": len(jacob),
        "complete": not unl,
        "unlabelled": unl,
        "n_cant_tell": len(cant),
        "judges": judges,
        "headline": [HEADLINE_ITEM, claim_gate],
        "draw": key.get("draw"),
        "gate_rule": (
            f"held-out point precision and recall >= {GATE} (D1); pass 2 on units anchored "
            "exact or normalized (the primary tiers)"
        ),
        "gate": gate,
        "per_judge": per_judge,
        "gold": gold_scores(key.get("gold", {}), labs["gold"], matcher),
        "n_audit_marked": sum(1 for d in labs["audit"].values() if d.get("label")),
    }


def _fmt(m: Mapping | None) -> str:
    if not m or m.get("point") is None:
        return "no data"
    s = f"{m['point']:.2f} [{m['lo']:.2f}, {m['hi']:.2f}] n={m['n']} (eff {m['n_eff']:.1f})"
    return s if m["status"] == "ok" else f"{s}, {m['status']}"


def to_markdown(rec: Mapping) -> str:
    lines = [
        "# Experiment 1 judge validation",
        "",
        (
            f"{rec['n_labelled']}/{rec['n_units']} units labelled ({rec['n_cant_tell']} can't "
            f"tell). Gate: {rec['gate_rule']}. Estimates are inverse-probability weighted, with "
            "Wilson 95% intervals on the effective n."
        ),
        "",
        "| Judge | " + " | ".join(rec["headline"]) + " | Judge |",
        "|---|" + "---|" * (len(rec["headline"]) + 1),
    ]
    for j in rec["judges"]:
        g = rec["gate"][j]
        lines.append(f"| {j} | " + " | ".join(g[v] for v in rec["headline"]) + f" | {g['judge']} |")
    lines += [
        "",
        "| Judge | Verdict | Half | Precision | Recall | Recall incl. unlisted |",
        "|---|---|---|---|---|---|",
    ]
    for j in rec["judges"]:
        for v, by_half in rec["per_judge"][j]["verdicts"].items():
            for h in HALVES:
                s = by_half[h]
                lines.append(
                    f"| {j} | {v} | {h} | {_fmt(s['precision'])} | "
                    f"{_fmt(s['recall'])} | {_fmt(s['recall_any'])} |"
                )
    lines += ["", "| Judge | Not-an-item share of listed items |", "|---|---|"]
    for j in rec["judges"]:
        lines.append(f"| {j} | {_fmt(rec['per_judge'][j]['not_an_item_share'])} |")
    lines += [
        "",
        "Gold windows (recall of Jacob's lines):",
        "",
        "| Judge | Items | Claims |",
        "|---|---|---|",
    ]
    for j, t in rec["gold"]["totals"].items():
        cell = [
            f"{t[f]['matched']}/{t[f]['jacob']}" if f in t else "-" for f in ("items", "claims")
        ]
        lines.append(f"| {j} | {cell[0]} | {cell[1]} |")
    return "\n".join(lines) + "\n"


def _json_safe(o: Any) -> Any:
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_safe(v) for v in o]
    return o


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--key", type=Path, default=ROOT / KEY)
    ap.add_argument("--out", type=Path, default=ROOT / OUT, help="score.json and LOCKED.json")
    ap.add_argument("--md-out", type=Path, default=None, help="score.md (numbers only)")
    ap.add_argument("--claim-gate", choices=("unsupported", "invented"), default="invented")
    a = ap.parse_args(argv)
    key = json.loads(a.key.read_text())
    labels = json.loads(a.labels.read_text())
    try:
        out = require_private(a.out)
        rec = score(labels, key, claim_gate=a.claim_gate)
    except (DrawMismatch, ValueError) as e:
        print(f"S1_LABEL_SCORE FAIL {e}")
        return 2
    rec["key_page_sha256"] = key.get("page_sha256")
    out.mkdir(parents=True, exist_ok=True)
    (out / "score.json").write_text(json.dumps(_json_safe(rec), indent=1) + "\n")
    md = a.md_out or out / "score.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(to_markdown(rec))
    if rec["complete"]:
        lock = write_lock(out, key, labels)
        print(f"  labels locked: {out / LOCK_NAME} (draw {lock['draw']})")
    print(
        f"S1_LABEL_SCORE {rec['n_labelled']}/{rec['n_units']} labelled, complete {rec['complete']}"
    )
    for j, g in rec["gate"].items():
        print(f"  {j}: " + ", ".join(f"{v} {g[v]}" for v in rec["headline"]) + f" -> {g['judge']}")
    print(f"  {out / 'score.json'}, {md}")
    return 0 if rec["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
