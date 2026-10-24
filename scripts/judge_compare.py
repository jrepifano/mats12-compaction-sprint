"""Does Sonnet 5.5 grade compaction losses better than Haiku 5.5, on Jacob's hand-labelled cards?

Brief: logs/s2/briefs/sonnet-judge-test.md. Decision line, written before any Sonnet call:
results/sprint/judge_compare/decision_line.md.

    uv run python scripts/judge_compare.py run [--concurrency 4] [--only SET ...]
    uv run python scripts/judge_compare.py score      # metrics from the cached calls only

What it does, per labelled item card of four sets (`SETS`):
- **Inputs fixed.** Haiku's extracted items (pass 1a) stay as they are. For every record behind
  a card, Sonnet re-runs pass 1b (`s1_judge.Pass1b`, the frozen prompt) on that record's whole
  valid item list against the same summary Haiku graded, so each request differs from Haiku's
  only in the model (checked: the rebuilt request's prompt sha is looked up in Haiku's cache,
  `haiku_request_match`). Then the second look (`s1_second_look.second_look`, frozen config) on
  Sonnet's "dropped" grades of the labelled items. Max plan only (`claude -p`, the MaxBackend
  route Haiku used, model swapped, same effort), concurrency at most 4, content-keyed cache under
  data/raw/sprint/s1/cache/sonnet-max/; each run books a $0 ledger row under SPRINT-S1 with
  purpose judge_compare (the backend's own recorder).
- **"Lost"** (judge and Jacob alike): dropped or misstated with no other route. Experiment 1 sets:
  availability "lost" (`s1_judge.availability`, recomputed for every card, judge-independent);
  Experiment 2: S2's own rule, not still present in the resumed context
  (`compaction.s2.lossaudit.is_lost`). Cards whose item is available another way are left out,
  as `key_sl_not_lost` does for the fresh validation (both judges' calls would be "not lost"
  by definition). A judge's call is lost when its final grade (after its second look) is valid
  and dropped or misstated.
- **Jacob's label**, mapped as each set's scorer does: dropped / misstated -> lost, kept -> kept,
  not an item -> not_an_item, can't tell (and unlabelled, and the audit's "pipeline") -> left
  out. The release audit marks the judge's verdict instead: "correct" keeps Haiku's class,
  "judge_wrong" on a dropped card -> not_lost (kept or not an item; the audit doesn't say which),
  on a kept card -> lost.

Metrics (`metrics`), overall, per set and per source, for each judge:
- agreement with Jacob, lost vs not lost, over cards Jacob labels lost, kept or not_lost;
- precision of "lost" calls: Jacob's not_an_item counts as a miss;
- kept->lost error: share called lost among cards Jacob labels kept or not_lost;
- Haiku x Sonnet 2x2 with Jacob's labels per cell; the paired Sonnet - Haiku difference in
  kept->lost error and in agreement.
Intervals: Wilson 95% (unweighted) and a session-clustered percentile bootstrap (`N_BOOT`
resamples, seed `SEED`). Weighted shares (w = 1 / inclusion probability, Horvitz-Thompson ratio)
for sets with inclusion probabilities (all but the audit), per set only: each set's weights
describe its own population, so they are not pooled. The cards were drawn by Haiku's verdict,
so unweighted figures under-sample Sonnet's errors on Haiku-kept items (flagged in the
summary with the count of labelled Sonnet-lost / Haiku-kept cards).

Outputs: results/sprint/judge_compare/{rows.jsonl, summary.json, summary.md}. rows.jsonl and
summary.json carry ids, labels and calls, no free text; the full rows with item text, quotes and
Jacob's notes go to data/raw/sprint/judge_compare/rows.private.jsonl (gitignored). summary.md's
random disagreement cards are drawn with Jacob's own sessions removed first (as
scripts/s1_report_samples.py does).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SEED = 20261024
N_BOOT = 2_000
Z95 = 1.959963984540054
PRECISION_BAR = 0.8
N_SAMPLES = 6
EXCLUDED_SAMPLE_SOURCE = "jacob"
MODEL = "claude-sonnet-5-5"
NAME = "sonnet-max"
PURPOSE = "judge_compare"
MAX_CONCURRENCY = 4  # the SWE-Interact pilot shares the Max allowance (brief)
LOST_GRADES = ("dropped", "misstated")

OUT = ROOT / "results" / "sprint" / "judge_compare"
PRIVATE = ROOT / "data" / "raw" / "sprint" / "judge_compare"
S1_RAW = ROOT / "data" / "raw" / "sprint" / "s1"
CACHE = S1_RAW / "cache" / NAME

SETS = ("val2", "audit", "s2_losscal", "swechat_ext")
SET_TITLES = {
    "val2": "Experiment 1 fresh validation",
    "audit": "Experiment 1 release audit",
    "s2_losscal": "Experiment 2 live-loss calibration",
    "swechat_ext": "SWE-chat precision check",
}
# Truth classes. not_lost: the audit's "judge_wrong" on a dropped card (kept or not an item).
TRUTH = ("lost", "kept", "not_lost", "not_an_item")
NEGATIVE = ("kept", "not_lost")


def log(msg: str) -> None:
    print(f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} JUDGE_COMPARE {msg}", flush=True)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


# ------------------------------------------------------------------------------------------
# Label mapping (pure)
# ------------------------------------------------------------------------------------------


def truth_of_item_label(label: str | None) -> str | None:
    """Fresh validation, S2 calibration and SWE-chat: Jacob's grade of the item."""
    return {
        "dropped": "lost",
        "misstated": "lost",
        "kept": "kept",
        "not_an_item": "not_an_item",
    }.get(label or "")


def truth_of_audit_label(label: str | None, verdict: str) -> str | None:
    """Release audit: Jacob marks the judge's final verdict (dropped or kept)."""
    lost = verdict in LOST_GRADES
    if label == "correct":
        return "lost" if lost else "kept"
    if label == "judge_wrong":
        return "not_lost" if lost else "lost"
    return None  # "pipeline", unlabelled


def eligible(set_name: str, availability: str | None) -> bool:
    """The card can be lost at all under its set's definition (module docstring)."""
    if set_name == "s2_losscal":
        return availability != "still_present"
    return availability == "lost"


def judge_lost(grade: str | None, grade_valid: bool) -> bool:
    return bool(grade_valid) and grade in LOST_GRADES


# ------------------------------------------------------------------------------------------
# Metrics (pure)
# ------------------------------------------------------------------------------------------


def wilson(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    from emx.stats import _wilson

    lo, hi = _wilson(k, n, Z95)
    return [round(float(lo), 4), round(float(hi), 4)]


def cluster_boot(
    rows: list[dict],
    stat: Callable[[list[dict]], float | None],
    *,
    seed: int = SEED,
    reps: int = N_BOOT,
) -> dict:
    """Percentile 95% interval of `stat`, resampling session groups with replacement.
    Resamples where `stat` is undefined (None) are skipped and counted."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[str(r["session_group"])].append(r)
    groups = sorted(by)
    if not groups:
        return {"ci95": None, "n_clusters": 0, "reps_used": 0}
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        sample = [r for g in rng.choices(groups, k=len(groups)) for r in by[g]]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    if not vals:
        return {"ci95": None, "n_clusters": len(groups), "reps_used": 0}
    vals.sort()
    lo = vals[int(0.025 * (len(vals) - 1))]
    hi = vals[round(0.975 * (len(vals) - 1))]
    return {"ci95": [round(lo, 4), round(hi, 4)], "n_clusters": len(groups), "reps_used": len(vals)}


def _share(rows: list[dict], hit: Callable[[dict], bool], weighted: bool = False) -> float | None:
    w = [(1.0 / r["pi"] if weighted else 1.0) for r in rows]
    s = sum(w)
    return sum(wi for wi, r in zip(w, rows) if hit(r)) / s if s else None


def share(rows: list[dict], hit: Callable[[dict], bool], *, weights: bool) -> dict:
    """Unweighted share with Wilson and bootstrap intervals; the weighted share too when asked
    and every row has an inclusion probability."""
    n, k = len(rows), sum(bool(hit(r)) for r in rows)
    out: dict[str, Any] = {
        "n": n,
        "k": k,
        "p": round(k / n, 4) if n else None,
        "wilson95": wilson(k, n),
        "boot95": cluster_boot(rows, lambda s: _share(s, hit))["ci95"] if n else None,
        "n_clusters": len({r["session_group"] for r in rows}),
    }
    if weights and n and all(r.get("pi") for r in rows):
        out["p_weighted"] = round(_share(rows, hit, True), 4)
        out["boot95_weighted"] = cluster_boot(rows, lambda s: _share(s, hit, True))["ci95"]
    return out


def judge_metrics(rows: list[dict], j: str, *, weights: bool) -> dict:
    lost = f"{j}_lost"
    return {
        "agreement": share(
            [r for r in rows if r["truth"] in ("lost", *NEGATIVE)],
            lambda r: r[lost] == (r["truth"] == "lost"),
            weights=weights,
        ),
        "precision_lost": share(
            [r for r in rows if r[lost]], lambda r: r["truth"] == "lost", weights=weights
        ),
        "kept_to_lost": share(
            [r for r in rows if r["truth"] in NEGATIVE], lambda r: r[lost], weights=weights
        ),
        "n_lost_calls": sum(bool(r[lost]) for r in rows),
    }


def paired(rows: list[dict], hit: Callable[[dict, str], bool]) -> dict:
    """Sonnet - Haiku in the share of `rows` that `hit`, paired on the same cards."""
    n = len(rows)
    if not n:
        return {"n": 0, "diff": None, "boot95": None}

    def d(s: list[dict]) -> float | None:
        return (sum(hit(r, "sonnet") for r in s) - sum(hit(r, "haiku") for r in s)) / len(s)

    return {
        "n": n,
        "haiku": round(sum(hit(r, "haiku") for r in rows) / n, 4),
        "sonnet": round(sum(hit(r, "sonnet") for r in rows) / n, 4),
        "diff": round(d(rows), 4),
        "boot95": cluster_boot(rows, d)["ci95"],
        "only_sonnet": sum(hit(r, "sonnet") and not hit(r, "haiku") for r in rows),
        "only_haiku": sum(hit(r, "haiku") and not hit(r, "sonnet") for r in rows),
    }


def two_by_two(rows: list[dict]) -> dict:
    """Haiku x Sonnet lost calls, with Jacob's truth classes counted in each cell."""
    cells: dict[str, Counter] = {}
    for r in rows:
        k = f"haiku_{'lost' if r['haiku_lost'] else 'not'}|sonnet_{'lost' if r['sonnet_lost'] else 'not'}"
        cells.setdefault(k, Counter())[r["truth"]] += 1
    order = (
        "haiku_lost|sonnet_lost",
        "haiku_lost|sonnet_not",
        "haiku_not|sonnet_lost",
        "haiku_not|sonnet_not",
    )
    return {
        k: {"n": sum(cells.get(k, Counter()).values()), **dict(cells.get(k, Counter()))}
        for k in order
    }


def group_metrics(rows: list[dict], *, weights: bool) -> dict:
    return {
        "n_cards": len(rows),
        "truth": dict(Counter(r["truth"] for r in rows)),
        "haiku": judge_metrics(rows, "haiku", weights=weights),
        "sonnet": judge_metrics(rows, "sonnet", weights=weights),
        "paired_kept_to_lost": paired(
            [r for r in rows if r["truth"] in NEGATIVE], lambda r, j: bool(r[f"{j}_lost"])
        ),
        "paired_agreement": paired(
            [r for r in rows if r["truth"] in ("lost", *NEGATIVE)],
            lambda r, j: r[f"{j}_lost"] == (r["truth"] == "lost"),
        ),
        "two_by_two": two_by_two(rows),
    }


def decision(overall: Mapping) -> dict:
    """The pre-registered line (decision_line.md), on the pooled unweighted figures."""
    prec = overall["sonnet"]["precision_lost"]
    pk = overall["paired_kept_to_lost"]
    ci = pk.get("boot95")
    a = prec["p"] is not None and prec["p"] >= PRECISION_BAR
    b = pk.get("diff") is not None and pk["diff"] < 0 and ci is not None and ci[1] < 0
    return {
        "sonnet_precision_unweighted": prec["p"],
        "precision_bar": PRECISION_BAR,
        "precision_met": a,
        "kept_to_lost_diff": pk.get("diff"),
        "kept_to_lost_diff_boot95": ci,
        "kept_to_lost_lower_excl_0": b,
        "verdict": "regrade_swechat_with_sonnet" if a and b else "keep_haiku",
        "n_sonnet_lost_haiku_kept": overall["two_by_two"]["haiku_not|sonnet_lost"]["n"],
    }


def metrics(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["truth"] in TRUTH]
    out = {
        "overall": group_metrics(scored, weights=False),
        "by_set": {
            s: group_metrics([r for r in scored if r["set"] == s], weights=True)
            for s in SETS
            if any(r["set"] == s for r in scored)
        },
        "by_source": {
            s: group_metrics([r for r in scored if r["source"] == s], weights=False)
            for s in sorted({r["source"] for r in scored})
        },
    }
    out["decision"] = decision(out["overall"])
    return out


# ------------------------------------------------------------------------------------------
# Cards: the labelled items of each set, with Haiku's final call
# ------------------------------------------------------------------------------------------


def item_n(item_id: str) -> int:
    return int(str(item_id).rsplit("#i", 1)[1])


class SetData:
    """One labelled set: its cards, the Haiku rows of the records behind them, and loaders."""

    def __init__(self, name: str):
        self.name = name
        self.cards: list[dict] = []  # one per labelled item card (eligible or not)
        self.items_by_rid: dict[str, list[dict]] = {}  # Haiku's rows after its second look
        self.rec_of: Callable[[str], dict] = lambda b: {}
        self.summary_of_rid: dict[str, str] = {}
        self.kept_of_rid: dict[str, list[dict]] = {}
        self.haiku_cache: Path | None = None


def _final_items(items: list[dict], overlay: list[dict]) -> list[dict]:
    from compaction import s1_second_look as SL

    return SL.apply(items, overlay)


def _by_rid(rows: Iterable[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[str(r["rid"])].append(r)
    return dict(out)


def _card(
    set_name: str,
    uid: str,
    row: Mapping,
    label: Mapping | None,
    truth: str | None,
    pi: float | None,
    group: str,
    **extra,
) -> dict:
    return {
        "set": set_name,
        "uid": uid,
        "rid": str(row["rid"]),
        "base_rid": str(row.get("base_rid") or row["rid"]),
        "arm": row.get("arm") or "natural",
        "item_id": str(row["item_id"]),
        "source": row.get("source"),
        "type": row.get("type"),
        "session_group": group,
        "pi": pi,
        "jacob_label": (label or {}).get("label"),
        "jacob_note": (label or {}).get("note"),
        "truth": truth,
        "haiku_grade": row.get("grade"),
        "haiku_grade_valid": bool(row.get("valid") and row.get("grade_valid")),
        "haiku_first_grade": (row.get("second_look") or {}).get("first_verdict", row.get("grade")),
        "text": row.get("text"),
        "quote": row.get("quote"),
        **extra,
    }


def load_val2() -> SetData:
    import s1_label_score as LS

    from compaction import s1_data

    d = SetData("val2")
    key = json.loads((S1_RAW / "labels_val2" / "key.private.json").read_text())
    labs = LS.normalize_labels(
        json.loads((S1_RAW / "labels_val2" / "labels_all60.json").read_text())
    )
    labs = labs["labels"]
    bad = sorted(u for u, x in labs.items() if x.get("draw") != key["draw"])
    if bad:
        raise SystemExit(f"val2: labels from another draw {bad[:3]}")
    v = S1_RAW / "val2"
    final = _final_items(
        read_jsonl(v / "items_haiku-max.jsonl"), read_jsonl(v / "second_look_haiku-max.jsonl")
    )
    by_id = {r["item_id"]: r for r in final}
    d.items_by_rid = _by_rid(final)
    for uid, u in sorted(key["units"].items()):
        if u["kind"] != "item":
            continue  # claims are out of scope (brief)
        row = by_id[u["row_ids"]["haiku-max"]]
        lab = labs.get(uid)
        d.cards.append(
            _card(
                "val2",
                uid,
                row,
                lab,
                truth_of_item_label((lab or {}).get("label")),
                u.get("pi"),
                s1_data.session_group(u["source"], u["session_id"]),
            )
        )
    d.rec_of = s1_data.load
    for rid in {c["rid"] for c in d.cards}:
        rec = s1_data.load(rid)
        d.summary_of_rid[rid] = rec.get("summary") or ""
        d.kept_of_rid[rid] = s1_data.post_context(rec)
    d.haiku_cache = S1_RAW / "cache" / "haiku-max" / "s1_judge.jsonl"
    return d


def load_swechat_ext() -> SetData:
    import s1_swechat_ext as X

    from compaction import s1_data

    d = SetData("swechat_ext")
    key = json.loads((S1_RAW / "swechat_ext" / "page" / "key.private.json").read_text())
    labs = X.read_labels(S1_RAW / "swechat_ext" / "db" / "labels")
    bad = sorted(u for u, x in labs.items() if x.get("draw") != key["draw"])
    if bad:
        raise SystemExit(f"swechat_ext: labels from another draw {bad[:3]}")
    r = ROOT / "results" / "sprint" / "S1" / "swechat_ext"
    final = _final_items(
        read_jsonl(r / "items_haiku-max.jsonl"), read_jsonl(r / "second_look_haiku-max.jsonl")
    )
    by_id = {x["item_id"]: x for x in final}
    d.items_by_rid = _by_rid(final)
    for uid, u in sorted(key["units"].items()):
        if u["kind"] != "item":
            continue
        row = by_id[u["row_ids"]["haiku-max"]]
        lab = labs.get(uid)
        d.cards.append(
            _card(
                "swechat_ext",
                uid,
                row,
                lab,
                truth_of_item_label((lab or {}).get("label")),
                u.get("pi"),
                s1_data.session_group(u["source"], u["session_id"]),
            )
        )
    d.rec_of = s1_data.load
    for rid in {c["rid"] for c in d.cards}:
        rec = s1_data.load(rid)
        d.summary_of_rid[rid] = rec.get("summary") or ""
        d.kept_of_rid[rid] = s1_data.post_context(rec)
    d.haiku_cache = S1_RAW / "cache" / "haiku-max" / "s1_judge.jsonl"
    return d


def load_audit() -> SetData:
    import s1_audit as AU

    from compaction import s1_analysis as A
    from compaction import s1_data

    d = SetData("audit")
    key = json.loads((S1_RAW / "audit" / "audit_key.private.json").read_text())
    snap = json.loads((S1_RAW / "audit" / "labels_snapshot.json").read_text())
    if snap["draw"] != key["draw"]:
        raise SystemExit("audit: labels snapshot from another draw")
    labs = snap["labels"]
    src = AU.Sources()
    finals: dict[str, list[dict]] = {}
    for uid, c in sorted(key["audit"].items()):
        if c["kind"] != "item":
            continue
        part = c["private"]["part"]
        if part not in finals:
            finals[part] = AU.final_rows(A.default_reader, part)[0]
        row = next(
            x
            for x in finals[part]
            if x["item_id"] == c["private"]["row_id"] and x["rid"] == c["private"]["rid"]
        )
        lab = labs.get(uid)
        truth = truth_of_audit_label((lab or {}).get("label"), c["verdict"])
        d.cards.append(
            _card(
                "audit",
                uid,
                row,
                lab,
                truth,
                None,
                s1_data.session_group(row["source"], row["session_id"]),
                part=part,
            )
        )
        rid = str(row["rid"])
        if rid not in d.items_by_rid:
            d.items_by_rid[rid] = [x for x in finals[part] if str(x["rid"]) == rid]
            # size cards (prefix histories) are only re-graded if eligible; none are (refused
            # in pass1b_tasks if one ever is)
            d.summary_of_rid[rid] = src.summary(part, row) if part != "size" else None
            if (row.get("arm") or "natural") == "natural":
                d.kept_of_rid[rid] = s1_data.post_context(src.base(part, row))
            elif part == "partB":
                rep = src.replays(row["arm"]).get(str(row["base_rid"])) or {}
                d.kept_of_rid[rid] = rep.get("kept_messages") or []
            else:
                d.kept_of_rid[rid] = None  # size: availability taken from the sealed row
    d.rec_of = src.rec
    d.haiku_cache = S1_RAW / "cache" / "haiku-max" / "s1_judge.jsonl"
    return d


def load_s2_losscal() -> SetData:
    import s2_losscal_score as S2S

    from compaction.s2 import lossaudit as LA

    d = SetData("s2_losscal")
    kp = ROOT / "data" / "raw" / "sprint" / "s2" / "losscal" / "key.private.json"
    key = json.loads(kp.read_text())
    labs = S2S.load_labels(json.loads((kp.parent / "labels.json").read_text()), key["draw"])
    la = ROOT / "results" / "sprint" / "S2" / "lossaudit"
    final = _final_items(read_jsonl(la / "items.jsonl"), read_jsonl(la / "second_look.jsonl"))
    by_id = {(x["rid"], x["item_id"]): x for x in final}
    d.items_by_rid = _by_rid(final)
    for cid, c in sorted(key["cards"].items()):
        row = by_id[(c["rid"], c["item_id"])]
        lab = labs.get(cid)
        d.cards.append(
            _card(
                "s2_losscal",
                cid,
                row,
                lab,
                truth_of_item_label((lab or {}).get("label")),
                c.get("inclusion_p"),
                f"deepswe:{c['task']}",
                judge_class=c["judge_class"],
            )
        )
    index = LA.Index(LA.boundaries())
    d.rec_of = index.load
    for rid in {c["rid"] for c in d.cards}:
        rec = index.load(rid)
        d.summary_of_rid[rid] = rec.get("summary") or ""
        d.kept_of_rid[rid] = LA.post_context(rec)
    d.haiku_cache = (
        ROOT
        / "data"
        / "raw"
        / "sprint"
        / "s2"
        / "cache"
        / "lossaudit-haiku-max"
        / "s2_lossaudit.jsonl"
    )
    return d


LOADERS = {
    "val2": load_val2,
    "audit": load_audit,
    "s2_losscal": load_s2_losscal,
    "swechat_ext": load_swechat_ext,
}


def add_availability(d: SetData) -> dict:
    """Availability of every card's item (judge-independent), checked against the stored value
    where Haiku's row has one; sets `eligible` and Haiku's final lost call."""
    from compaction import s1_judge as J

    n_mismatch = 0
    rows = {(str(r["rid"]), r["item_id"]): r for rs in d.items_by_rid.values() for r in rs}
    for c in d.cards:
        row = rows[(c["rid"], c["item_id"])]
        kept = d.kept_of_rid.get(c["rid"])
        if kept is None:  # audit size cards: the sealed row's own classification
            av = row.get("availability")
        else:
            av = J.availability(row, d.rec_of(c["base_rid"]), kept)[0]
        stored = row.get("availability")
        if stored is not None and stored != av:
            n_mismatch += 1
        c["availability"] = av
        c["availability_stored"] = stored
        c["eligible"] = eligible(d.name, av)
        c["haiku_lost"] = c["eligible"] and judge_lost(c["haiku_grade"], c["haiku_grade_valid"])
    return {"availability_mismatch_vs_stored": n_mismatch}


# ------------------------------------------------------------------------------------------
# Sonnet grading
# ------------------------------------------------------------------------------------------


def sonnet_backend(concurrency: int):
    from s1_run import HAIKU_EFFORT

    from compaction.claude_headless import MaxBackend

    if concurrency > MAX_CONCURRENCY:
        raise SystemExit(f"concurrency is capped at {MAX_CONCURRENCY} (brief)")
    return MaxBackend(
        model=MODEL, name=NAME, cache_dir=CACHE, concurrency=concurrency, effort=HAIKU_EFFORT
    )


def pass1b_inputs(rows: list[dict]) -> list[dict]:
    """Haiku's pass-1a items of one record, in the shape Pass1b reads (`n` from the item id),
    in pass-1a order."""
    return [
        dict(r, n=item_n(r["item_id"])) for r in sorted(rows, key=lambda r: item_n(r["item_id"]))
    ]


def graded_rids(d: SetData) -> list[str]:
    """Records with at least one eligible, scored card: only these are re-graded."""
    return sorted({c["rid"] for c in d.cards if c["eligible"] and c["truth"] in TRUTH})


def pass1b_tasks(backend, d: SetData) -> dict[str, Any]:
    from compaction import s1_judge as J

    p = J.prompts()
    tasks = {}
    for rid in graded_rids(d):
        summary = d.summary_of_rid.get(rid)
        if summary is None:
            raise SystemExit(f"{d.name} {rid}: no summary for an eligible card (size arm?)")
        rows = d.items_by_rid[rid]
        rec = d.rec_of(str(rows[0].get("base_rid") or rid))
        tasks[rid] = J.Pass1b(backend, pass1b_inputs(rows), summary, rid, rec, p)
    return tasks


def haiku_request_match(d: SetData, tasks: Mapping[str, Any]) -> dict:
    """Share of Sonnet's pass-1b requests whose (key, system+user sha) is in Haiku's cache:
    the same inputs, with only the model swapped."""
    from compaction import claude_headless as CH

    want = {}
    for t in tasks.values():
        for r in t.pending():
            want[r.key] = CH._sha(r.system + "\0" + r.user)
    seen: set[tuple[str, str]] = set()
    if d.haiku_cache and d.haiku_cache.exists():
        with open(d.haiku_cache, encoding="utf-8") as f:
            for line in f:
                if '"key": "p1b|' not in line and '"key":"p1b|' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("key") in want:
                    seen.add((row["key"], row.get("prompt_sha")))
    hit = sum((k, s) in seen for k, s in want.items())
    return {
        "n_requests": len(want),
        "n_match": hit,
        "cache": str(d.haiku_cache.relative_to(ROOT)) if d.haiku_cache else None,
    }


def usage_ok() -> tuple[bool, Any]:
    import s1_usage_watch as W

    r = W.latest_reading()
    if not r:
        return True, None
    _, u, _ = W.worst(r)
    return u < W.PAUSE_AT, {k: v[0] for k, v in r.items()}


def grade_set(backend, d: SetData) -> dict:
    """Sonnet's pass 1b on the records behind the eligible cards, then its second look on the
    labelled items it graded dropped. Sets sonnet_* on the cards."""
    from compaction import s1_judge as J
    from compaction import s1_second_look as SL

    tasks = pass1b_tasks(backend, d)
    match = haiku_request_match(d, tasks)
    log(
        f"{d.name}: {len(tasks)} records, pass-1b requests matching Haiku's cache "
        f"{match['n_match']}/{match['n_requests']}"
    )
    ok, reading = usage_ok()
    if not ok:
        raise SystemExit(f"Max usage at the pause line {reading}: rerun later (cache resumes)")
    if tasks:
        J.drive(backend, list(tasks.values()), PURPOSE)
    stats = {"pass1b": backend.last_stats}
    backend.last_stats = None
    errors = [e for t in tasks.values() for e in t.errors]
    graded = {(rid, x["item_id"]): x for rid, t in tasks.items() for x in t.items}
    wanted = [c for c in d.cards if c["eligible"] and c["truth"] in TRUTH]
    rows = []
    for c in wanted:
        x = graded[(c["rid"], c["item_id"])]
        rows.append(dict(x, judge=backend.name, judge_model=backend.model))
    ok, reading = usage_ok()
    if not ok:
        raise SystemExit(f"Max usage at the pause line {reading}: rerun later (cache resumes)")
    summaries = d.summary_of_rid
    overlay = SL.second_look(
        backend,
        rows,
        [],
        load_rec=d.rec_of,
        summary_of=lambda row, rec: summaries[str(row["rid"])],
        purpose=PURPOSE,
    )
    final = {(str(r["rid"]), r["item_id"]): r for r in SL.apply(rows, overlay)}
    sl_by = {(str(o["rid"]), o["row_id"]): o for o in overlay}
    for c in wanted:
        k = (c["rid"], c["item_id"])
        f, first, o = final[k], graded[k], sl_by.get(k)
        c["sonnet_first_grade"] = first["grade"]
        c["sonnet_first_valid"] = bool(first["grade_valid"])
        c["sonnet_grade"] = f["grade"]
        c["sonnet_grade_valid"] = bool(f["grade_valid"])
        c["sonnet_grade_reason"] = f.get("grade_reason")
        c["sonnet_grade_note"] = first.get("grade_note")
        c["sonnet_summary_quote"] = first.get("summary_quote")
        c["sonnet_second_look"] = (
            {k2: o.get(k2) for k2 in ("verdict", "valid", "changed", "invalid_reason", "note")}
            if o
            else None
        )
        c["sonnet_lost"] = judge_lost(f["grade"], f["grade_valid"])
        # no answer at all (a failed call) is not a judgment: left out and counted
        c["sonnet_answered"] = first["grade"] is not None
    return {
        "records": len(tasks),
        "pass1b_errors": errors,
        "haiku_request_match": match,
        "second_look_calls": len(overlay),
        "second_look_invalid": sum(not o["valid"] for o in overlay),
        "backend_stats": stats | {"second_look": backend.last_stats},
    }


# ------------------------------------------------------------------------------------------
# Outputs
# ------------------------------------------------------------------------------------------

PUBLIC_FIELDS = (
    "set",
    "uid",
    "rid",
    "item_id",
    "arm",
    "source",
    "type",
    "session_group",
    "pi",
    "availability",
    "eligible",
    "jacob_label",
    "truth",
    "haiku_first_grade",
    "haiku_grade",
    "haiku_lost",
    "sonnet_first_grade",
    "sonnet_grade",
    "sonnet_grade_valid",
    "sonnet_lost",
)


def _fmt(m: Mapping | None, weighted: bool = False) -> str:
    if not m or not m.get("n"):
        return "n=0"
    s = f"{m['k']}/{m['n']} = {m['p']:.2f}"
    if m.get("wilson95"):
        s += f" W[{m['wilson95'][0]:.2f}, {m['wilson95'][1]:.2f}]"
    if m.get("boot95"):
        s += f" B[{m['boot95'][0]:.2f}, {m['boot95'][1]:.2f}]"
    if weighted and m.get("p_weighted") is not None:
        b = m.get("boot95_weighted")
        s += f"; weighted {m['p_weighted']:.2f}" + (f" B[{b[0]:.2f}, {b[1]:.2f}]" if b else "")
    return s


def _cut(s: object, n: int = 300) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def samples(rows: list[dict], n: int = N_SAMPLES, seed: int = SEED) -> list[dict]:
    """Seeded random disagreement cards (Haiku's lost call != Sonnet's), Jacob's sessions
    removed first, sorted by (set, uid) before sampling."""
    pool = sorted(
        (
            r
            for r in rows
            if r["truth"] in TRUTH
            and r["haiku_lost"] != r["sonnet_lost"]
            and r.get("source") != EXCLUDED_SAMPLE_SOURCE
        ),
        key=lambda r: (r["set"], r["uid"]),
    )
    return random.Random(seed).sample(pool, min(n, len(pool)))


def weighted_precisions(res: Mapping) -> str:
    out = []
    for name, g in res["metrics"]["by_set"].items():
        h, s = g["haiku"]["precision_lost"], g["sonnet"]["precision_lost"]
        if h.get("p_weighted") is None or s.get("p_weighted") is None:
            continue
        out.append(f"{SET_TITLES[name]} {h['p_weighted']:.2f} vs {s['p_weighted']:.2f}")
    return "; ".join(out) or "none"


def to_markdown(res: Mapping, rows: list[dict]) -> str:
    o, dec = res["metrics"]["overall"], res["metrics"]["decision"]
    h, s = o["haiku"], o["sonnet"]
    pk, pa = o["paired_kept_to_lost"], o["paired_agreement"]
    tb = o["two_by_two"]
    verdict = (
        "re-grade SWE-chat's 300 records with Sonnet"
        if dec["verdict"] != "keep_haiku"
        else "keep Haiku"
    )
    ci = dec["kept_to_lost_diff_boot95"]
    drawn = samples(rows)
    n_dis = sum(r["haiku_lost"] != r["sonnet_lost"] for r in rows)
    L = [
        "# Haiku 5.5 vs Sonnet 5.5 as the loss grader, on Jacob's labelled cards",
        "",
        (
            f"- **Cards:** {o['n_cards']} labelled item cards on which an item can be lost "
            f"({', '.join(f'{SET_TITLES[k]} {v["n_cards"]}' for k, v in res['metrics']['by_set'].items())}); "
            f"Jacob's classes {o['truth']}. Haiku's items fixed; Sonnet re-ran pass 1b and the "
            "second look (frozen prompts, Max plan, $0)."
        ),
        (
            f'- **Precision of "lost" calls:** Haiku {_fmt(h["precision_lost"])}; Sonnet '
            f"{_fmt(s['precision_lost'])}."
        ),
        (
            f"- **Agreement with Jacob (lost vs not):** Haiku {_fmt(h['agreement'])}; Sonnet "
            f"{_fmt(s['agreement'])}; paired Sonnet − Haiku {pa['diff']:+.2f} "
            f"B{pa['boot95']}."
        ),
        (
            f"- **Kept→lost error:** Haiku {_fmt(h['kept_to_lost'])}; Sonnet "
            f"{_fmt(s['kept_to_lost'])}; paired difference {pk['diff']:+.2f} B{ci} "
            f"(only Sonnet wrong {pk['only_sonnet']}, only Haiku wrong {pk['only_haiku']})."
        ),
        (
            f"- **Decision line (pre-registered): {verdict}.** Sonnet precision "
            f"{dec['sonnet_precision_unweighted']} vs bar {PRECISION_BAR} "
            f"({'met' if dec['precision_met'] else 'not met'}); kept→lost lower with the interval "
            f"excluding 0: {'yes' if dec['kept_to_lost_lower_excl_0'] else 'no'}. Caveats: Sonnet is "
            "the same family as Haiku (not a cross-family check); the cards were drawn by Haiku's "
            f"verdict, so Sonnet's errors on Haiku-kept items are under-sampled (only "
            f"{tb['haiku_not|sonnet_lost']['n']} labelled cards are Sonnet-lost / Haiku-kept). "
            "Weighted precision of lost calls (inverse inclusion probability, per set), Haiku vs "
            f"Sonnet: {weighted_precisions(res)}."
        ),
        "",
        (
            "W = Wilson 95%, B = session-clustered bootstrap 95% (2,000 resamples, seed "
            f"{SEED}). Weighted = inverse inclusion probability, per set."
        ),
        "",
        "## Random disagreement cards",
        "",
        (
            f"Up to {N_SAMPLES} drawn with seed {SEED} from the cards where the two judges' lost "
            f"calls differ ({n_dis} cards), Jacob's own sessions removed first "
            f"({len(drawn)} eligible, so {len(drawn)} shown)."
        ),
        "",
    ]
    for i, r in enumerate(drawn, 1):
        L += [
            f"**{i}. {SET_TITLES[r['set']]}, {r['source']}, {r['type']}** (card {r['uid']})",
            f"- Item: {_cut(r['text'])}",
            f"- Quote: {_cut(r['quote'])}",
            (
                f"- Haiku: {r['haiku_grade']} → {'lost' if r['haiku_lost'] else 'not lost'}; "
                f"Sonnet: {r.get('sonnet_first_grade')} → {r.get('sonnet_grade')} → "
                f"{'lost' if r['sonnet_lost'] else 'not lost'}"
            ),
            f"- Sonnet's note: {_cut(r.get('sonnet_grade_note'), 200)}",
            f"- Jacob: {r['jacob_label']} ({r['truth']}); note: {_cut(r.get('jacob_note')) or '-'}",
            "",
        ]
    L += [
        "## Haiku × Sonnet lost calls (Jacob's classes per cell)",
        "",
        "| Haiku | Sonnet | n | Jacob |",
        "|---|---|---|---|",
    ]
    for k, v in tb.items():
        hk, sk = k.split("|")
        rest = ", ".join(f"{a} {b}" for a, b in v.items() if a != "n") or "-"
        L.append(f"| {hk.split('_')[1]} | {sk.split('_')[1]} | {v['n']} | {rest} |")
    L += [
        "",
        "## Per set",
        "",
        "| Set | n | Judge | Precision lost | Agreement | Kept→lost |",
        "|---|---|---|---|---|---|",
    ]
    for name, g in res["metrics"]["by_set"].items():
        for j in ("haiku", "sonnet"):
            m = g[j]
            L.append(
                f"| {SET_TITLES[name]} | {g['n_cards']} | {j} | "
                f"{_fmt(m['precision_lost'], True)} | {_fmt(m['agreement'], True)} | "
                f"{_fmt(m['kept_to_lost'], True)} |"
            )
    L += [
        "",
        "## Per source",
        "",
        "| Source | n | Judge | Precision lost | Agreement | Kept→lost |",
        "|---|---|---|---|---|---|",
    ]
    for name, g in res["metrics"]["by_source"].items():
        for j in ("haiku", "sonnet"):
            m = g[j]
            L.append(
                f"| {name} | {g['n_cards']} | {j} | {_fmt(m['precision_lost'])} | "
                f"{_fmt(m['agreement'])} | {_fmt(m['kept_to_lost'])} |"
            )
    e = res["exclusions"]
    L += [
        "",
        "## Exclusions and checks",
        "",
        f"- Cards left out: {e}.",
        f"- Requests matching Haiku's cached inputs: {res['haiku_request_match']}.",
        f"- Max usage before → after: {res['usage_before']} → {res['usage_after']}.",
        f"- Ledger (purpose {PURPOSE}, all runs): {res['max_ledger']}.",
        f"- Sonnet second look: {res['second_look']}.",
        "",
    ]
    return "\n".join(L)


def _atomic(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)


def ledger_total(path: Path = ROOT / "logs" / "costs.jsonl") -> dict:
    """Every Max batch this script booked (canaries included), from the spend ledger."""
    rows = [r for r in read_jsonl(path) if f"purpose={PURPOSE} " in str(r.get("note"))]
    return {
        "rows": len(rows),
        "calls": sum(int(r.get("units") or 0) for r in rows),
        "usd": round(sum(float(r.get("usd") or 0) for r in rows), 6),
        "paid_route": any("PAID_ROUTE" in str(r.get("note")) for r in rows),
    }


def scorable(c: Mapping) -> bool:
    return bool(c["eligible"] and c["truth"] in TRUTH and c.get("sonnet_answered"))


def exclusions(cards: list[dict]) -> dict:
    return {
        "labelled_item_cards": len(cards),
        "no_usable_label": sum(c["truth"] not in TRUTH for c in cards),
        "available_another_way": sum(c["truth"] in TRUTH and not c["eligible"] for c in cards),
        "sonnet_no_answer": sum(
            c["truth"] in TRUTH and c["eligible"] and not c.get("sonnet_answered") for c in cards
        ),
        "sonnet_invalid_grade_counted_not_lost": sum(
            scorable(c) and not c.get("sonnet_grade_valid") for c in cards
        ),
        "by_label": dict(Counter(str(c["jacob_label"]) for c in cards if c["truth"] not in TRUTH)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("run", "score"))
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
    ap.add_argument("--only", nargs="*", choices=SETS)
    a = ap.parse_args(argv)
    import s1_run

    from compaction import s1_second_look as SL

    s1_run.check_frozen()
    SL.check_frozen()
    sets = a.only or list(SETS)
    _, before = usage_ok()
    be = sonnet_backend(a.concurrency)
    if a.cmd == "score":  # cache only: any request not cached would be sent, so refuse
        be.run = _cache_only(be)
    all_cards, matches, sls = [], {}, {}
    for name in sets:
        d = LOADERS[name]()
        chk = add_availability(d)
        log(
            f"{name}: {len(d.cards)} item cards, {sum(c['eligible'] for c in d.cards)} eligible; "
            f"{chk}"
        )
        be.last_stats = None  # a set served wholly from the cache sends nothing
        info = grade_set(be, d)
        matches[name] = info["haiku_request_match"] | chk
        sls[name] = {k: info[k] for k in ("records", "second_look_calls", "second_look_invalid")}
        sls[name]["pass1b_errors"] = len(info["pass1b_errors"])
        log(f"{name}: graded; {sls[name]}; backend {json.dumps(info['backend_stats'])}")
        all_cards += d.cards
    _, after = usage_ok()
    scored = [c for c in all_cards if scorable(c)]
    res = {
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "brief": "logs/s2/briefs/sonnet-judge-test.md",
        "decision_line": "results/sprint/judge_compare/decision_line.md",
        "decision_line_sha256": hashlib.sha256((OUT / "decision_line.md").read_bytes()).hexdigest(),
        "sonnet": {
            "model": MODEL,
            "effort": be.effort,
            "route": "claude -p on the Max plan",
            "concurrency": a.concurrency,
            "purpose": PURPOSE,
        },
        "seed": SEED,
        "n_boot": N_BOOT,
        "sets": sets,
        "exclusions": exclusions(all_cards),
        "haiku_request_match": matches,
        "second_look": sls,
        "usage_before": before,
        "usage_after": after,
        "max_ledger": ledger_total(),
        "metrics": metrics(scored),
    }
    _atomic(
        OUT / "rows.jsonl",
        "".join(
            json.dumps({k: c.get(k) for k in PUBLIC_FIELDS}, sort_keys=True) + "\n" for c in scored
        ),
    )
    _atomic(
        PRIVATE / "rows.private.jsonl",
        "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in all_cards),
        mode=0o600,
    )
    _atomic(OUT / "summary.json", json.dumps(res, indent=1, sort_keys=True) + "\n")
    _atomic(OUT / "summary.md", to_markdown(res, scored))
    dec = res["metrics"]["decision"]
    log(f"DONE verdict={dec['verdict']} {json.dumps(dec)}")
    return 0


def _cache_only(be):
    """`run` that refuses to send: every request must already be in the cache."""
    real = be.run

    def run(reqs, *, purpose):
        cache = be._load_cache(purpose)
        miss = [r.key for r in reqs if be.cache_id(r) not in cache]
        if miss:
            raise SystemExit(f"score: {len(miss)} requests not cached (e.g. {miss[:2]}); use run")
        return real(reqs, purpose=purpose)

    return run


if __name__ == "__main__":
    sys.exit(main())
