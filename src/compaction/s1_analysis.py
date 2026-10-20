"""Experiment 1 (stage S1) analysis: label-unit selection, outcome rates, Part B contrasts, the
size test and the verbatim-budget check.

Unit U5 of docs/sprint/specs/S1.md (decisions D1 and D6; "Output rows"). Row shapes are the
spec's `ItemRow` and `ClaimRow`; `compaction.s1_types` holds the enums.

Three groups of functions:

1. **Label selection** (`label_units`, `select_label_items`), run on the dev-run judge rows of the
   label records (validation data, not outcomes, so no seal). Jacob labels each item or claim
   once, whichever judges listed it: items are matched across judges by normalised quote within
   a summary (`rid`), claims by normalised claim text. The draw is stratified by (kind, judge-
   positive, half) and enriched for the two headline verdicts (pass 1b `dropped`, pass 2
   `unsupported`), with each unit's inclusion probability recorded, so `scripts/s1_label_score.py`
   can weight by its inverse (D6). The scripts `s1_label_page.py` and `s1_label_score.py` import
   these and the text helpers below.
2. **Outcome analysis**, which reads sealed outcome files only through
   `seal.open_sealed("S1", name)` (the `reader` argument; tests inject one). Rates are reported per
   judge and never pooled across judges. Intervals are `emx.stats.cluster_bootstrap_ci` clustered
   on session groups (`session_group_of`: U1's `s1_data.session_group`, which joins SWE-chat's
   daily snapshots of one session; Jacob's 50 compactions come from 31 sessions). Part A
   samples (model, harness) strata evenly within each source, so Part A rates are reported both
   unweighted (the sample) and weighted to the eligible population (`weights`, from
   `stratum_weights(manifest)`).
3. **`verbatim_budget(records)`**, deterministic and seal-free: how much user text follows each
   early user turn by compaction time (bytes/4 tokens, as Codex counts; P49
   `probes/P49-long-context-compaction/p49/compact.py::codex_approx_tokens`), against Codex's 20k
   local and 64k server-side budgets, and how many later user requests follow it, against
   Antigravity's 10 kept requests.

Definitions used throughout:
- An item is *usable* when `valid`, `grade_valid`, `assessable` and not `superseded`, with a grade
  in GRADES. Drop, mis-state and loss rates are over usable items.
- An item is *lost* when it is dropped or mis-stated and its `availability` is `lost` (neither
  still present in the post-compaction context nor recoverable from a file by path).
- A *user-given rule* is an item with `type == "rule"` and `origin == "user"`.
- Pass 2 outcomes are span-level, as the label gate validates them. A *span* is (rid,
  normalised `summary_quote`) among one judge's valid claims; its verdict is the worst of that
  judge's claims on it (unsupported, then contradicted, then supported). It is *invented* when
  that worst is unsupported and the history is complete, and an *invented permission span* only
  when one of its permission claims is itself unsupported. Primary invention rates use claims
  anchored "exact" or "normalized" (`summary_quote_match`); "exact_only" and "including_fuzzy"
  are reported beside them as sensitivities. Claim-level counts are descriptive only.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from compaction import seal
from compaction.s1_data import session_group as _session_group
from compaction.s1_types import GRADES, S1_SEED, SEAL_STAGE, norm
from emx.stats import cluster_bootstrap_ci

HEADLINE = {"item": "dropped", "claim": "unsupported"}
LOST_GRADES = ("dropped", "misstated")
PRIMARY_CONTRAST = ("cc_prompt", "codex_local")
PART_B_ARMS = ("cc_native", "cc_prompt", "codex_local", "pi", "antigravity", "opencode")
NO_SESSION = "<none>"  # cluster label for rows without a session id
N_BOOT = 2_000  # bootstrap resamples per interval (emx.contracts uses 10k; cells here are many)

# ------------------------------------------------------------------------------------------
# Text helpers (shared with the label page and the scorer)
# ------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because been before
    being below between both but by can could did do does doing done down during each else few
    for from further had has have having he her here hers him his how i if in into is it its
    itself just let like may me might more most must my no nor not now of off on once only or
    other our ours out over own same shall she should so some such than that the their theirs
    them then there these they this those through to too under until up upon us very was we were
    what when where which while who whom why will with would yes yet you your yours""".split()  # noqa: SIM905
)


def norm_key(s: str | None) -> str:
    """Whitespace-normalised, lower-cased text: the matching key for quotes and claims."""
    return norm(s or "").lower()


def content_words(s: str | None) -> list[str]:
    """Lower-cased `[a-z0-9]+` tokens of `s` minus STOPWORDS and one-character tokens."""
    return [w for w in _TOKEN.findall((s or "").lower()) if len(w) > 1 and w not in STOPWORDS]


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def codex_approx_tokens(text: str) -> int:
    """Codex's token estimate, ceil(UTF-8 bytes / 4) (P49 `compact.codex_approx_tokens`)."""
    return (len(text.encode("utf-8")) + 3) // 4


# ------------------------------------------------------------------------------------------
# 1. Label selection (D6)
# ------------------------------------------------------------------------------------------


def _item_labelable(r: Mapping) -> bool:
    return bool(
        r.get("valid")
        and r.get("grade_valid")
        and r.get("grade") in GRADES
        and r.get("assessable", True)
        and not r.get("superseded")
    )


def _claim_labelable(r: Mapping) -> bool:
    return bool(r.get("valid") and r.get("verdict") in CLAIM_PRECEDENCE and claim_span(r))


def _half_of(r: Mapping, halves: Mapping[str, str] | None) -> str:
    base = r.get("base_rid") or r["rid"]
    h = None
    if halves is not None:
        h = halves.get(base, halves.get(r["rid"]))
    if h is None:
        h = r.get("half")
    if h not in ("dev", "held"):
        raise ValueError(f"no dev/held half for record {base!r}")
    return h


def halves_from_split(split: Mapping[str, Iterable[str]]) -> dict[str, str]:
    """`{"dev": [rid...], "held": [rid...]}` (U1 `label_records`) -> rid -> half."""
    out: dict[str, str] = {}
    for h in ("dev", "held"):
        for rid in split.get(h, []):
            if out.get(rid, h) != h:
                raise ValueError(f"record {rid!r} is in both halves")
            out[rid] = h
    return out


CLAIM_PRECEDENCE = ("unsupported", "contradicted", "supported")  # a span's verdict: worst wins
# summary_quote_match tiers kept by each invention analysis (None = every row)
ANCHOR_TIERS = {
    "primary": frozenset({"exact", "normalized"}),
    "exact_only": frozenset({"exact"}),
    "including_fuzzy": None,
}
ANCHOR_ORDER = ("exact", "normalized", "fuzzy")


def claim_span(r: Mapping) -> str:
    """The summary span a claim row comes from (`summary_quote`; its `claim` for older rows)."""
    return r.get("summary_quote") or r.get("claim") or ""


def label_units(
    item_rows: Iterable[Mapping],
    claim_rows: Iterable[Mapping],
    *,
    halves: Mapping[str, str] | None = None,
) -> list[dict]:
    """The labelling frame: one unit per distinct item or claim span, over every judge's rows.

    Items: usable rows (see module docstring), keyed by (rid, normalised quote); a judge listing
    the same item twice keeps its first row (by row id), the repeat counted in `dup_rows`.
    Claims: valid rows with a verdict, keyed by (rid, normalised `summary_quote`), the verbatim
    summary span, since judges word the same atomic claim differently. A judge may split one
    span into several claims: all are kept in `claims` ({judge: [{id, claim, verdict}]}), and
    the judge's verdict on the span is the worst of them (unsupported, then contradicted, then
    supported; `CLAIM_PRECEDENCE`). `text` is the quote (items) or the span (claims). Each unit
    keeps every judge's verdict (`verdicts`), first row id (`row_ids`) and all row ids
    (`row_ids_all`). `pos` is True when any judge gave the headline verdict. Units are returned
    sorted by (kind, rid, key)."""
    units: dict[tuple, dict] = {}
    for kind, rows, ok, id_f in (
        ("item", item_rows, _item_labelable, "item_id"),
        ("claim", claim_rows, _claim_labelable, "claim_id"),
    ):
        rows = sorted(
            (r for r in rows if ok(r)), key=lambda r: (r["rid"], r["judge"], str(r[id_f]))
        )
        for r in rows:
            text = r["quote"] if kind == "item" else claim_span(r)
            k = (kind, r["rid"], norm_key(text))
            u = units.get(k)
            if u is None:
                u = units[k] = {
                    "kind": kind,
                    "rid": r["rid"],
                    "base_rid": r.get("base_rid") or r["rid"],
                    "arm": r.get("arm", "natural"),
                    "half": _half_of(r, halves),
                    "source": r.get("source"),
                    "session_id": r.get("session_id") or NO_SESSION,
                    "harness": r.get("harness"),
                    "model": r.get("model"),
                    "text": text,
                    "msg": r.get("msg"),
                    "types": Counter(),
                    "verdicts": {},
                    "row_ids": {},
                    "row_ids_all": {},
                    "dup_rows": 0,
                }
                if kind == "claim":
                    u["history_complete"] = bool(r.get("history_complete"))
                    u["is_permission"] = False
                    u["claims"] = {}
            j = r["judge"]
            if kind == "item":
                if j in u["verdicts"]:
                    u["dup_rows"] += 1
                    u["row_ids_all"][j].append(str(r[id_f]))
                    continue
                u["verdicts"][j] = r["grade"]
                u["types"][r.get("type")] += 1
            else:
                u["claims"].setdefault(j, []).append(
                    {
                        "id": str(r[id_f]),
                        "claim": r.get("claim"),
                        "verdict": r["verdict"],
                        "match": r.get("summary_quote_match"),
                    }
                )
                u["is_permission"] = u["is_permission"] or bool(r.get("is_permission"))
                vs = [c["verdict"] for c in u["claims"][j]]
                u["verdicts"][j] = min(vs, key=CLAIM_PRECEDENCE.index)
                if len(vs) > 1:
                    u["dup_rows"] += 1
            u["row_ids"].setdefault(j, str(r[id_f]))
            u["row_ids_all"].setdefault(j, [])
            if str(r[id_f]) not in u["row_ids_all"][j]:
                u["row_ids_all"][j].append(str(r[id_f]))
    out = []
    for k in sorted(units, key=lambda k: (k[0], k[1], k[2])):
        u = units.pop(k)
        types = u.pop("types")
        if u["kind"] == "item":
            # majority type, ties broken by name
            u["type"] = min(types.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]
        else:  # best anchor tier any judge found for the span (fuzzy ones stay labelable)
            ms = [c["match"] for cs in u["claims"].values() for c in cs]
            known = [m for m in ms if m in ANCHOR_ORDER]
            u["anchor_tier"] = min(known, key=ANCHOR_ORDER.index) if known else None
        u["pos"] = any(v == HEADLINE[u["kind"]] for v in u["verdicts"].values())
        out.append(u)
    return out


def _largest_remainder(total: int, sizes: Mapping[Any, int]) -> dict[Any, int]:
    """Split `total` across keys in proportion to `sizes`, never above a key's size."""
    keys = sorted(sizes)
    cap = sum(sizes.values())
    total = min(total, cap)
    if cap == 0:
        return {k: 0 for k in keys}
    raw = {k: total * sizes[k] / cap for k in keys}
    alloc = {k: min(sizes[k], math.floor(raw[k])) for k in keys}
    left = total - sum(alloc.values())
    for k in sorted(keys, key=lambda k: (-(raw[k] - math.floor(raw[k])), str(k))):
        if left == 0:
            break
        if alloc[k] < sizes[k]:
            alloc[k] += 1
            left -= 1
    while left > 0:  # only when caps bind
        for k in keys:
            if left and alloc[k] < sizes[k]:
                alloc[k] += 1
                left -= 1
    return alloc


STRATA = tuple(
    (kind, pos, h) for kind in ("item", "claim") for pos in (True, False) for h in ("dev", "held")
)


def stratum_name(kind: str, pos: bool, half: str) -> str:
    return f"{kind}|{'pos' if pos else 'neg'}|{half}"


def _allocate(n: int, target_pos: int, sizes: Mapping[tuple[str, bool, str], int]) -> dict:
    """Draws per (kind, pos, half) stratum.

    Every nonempty stratum gets at least one draw, so no unit has inclusion probability zero
    (the refusal names the shortfall when `n` can't cover that). Per (kind, pos): each positive
    pair gets max(its minimum, min(target_pos, N)); the rest of `n` is split evenly between item
    and claim negatives, spilling over when one runs short, then back into the positives if
    every negative is used. Within a (kind, pos), each nonempty half gets one draw and the rest
    is split across the halves in proportion to their remaining sizes."""
    kps = [(k, p) for k in ("item", "claim") for p in (True, False)]
    size = {kp: sum(sizes.get((*kp, h), 0) for h in ("dev", "held")) for kp in kps}
    mins = {kp: sum(1 for h in ("dev", "held") if sizes.get((*kp, h), 0) > 0) for kp in kps}
    a = dict(mins)
    for kind in ("item", "claim"):
        kp = (kind, True)
        a[kp] = max(mins[kp], min(target_pos, size[kp]))
    rest = n - sum(a.values())
    if rest < 0:
        raise ValueError(
            f"n={n} cannot cover the positive targets and one draw per nonempty stratum "
            f"({sum(a.values())} needed)"
        )
    negs = [("item", False), ("claim", False)]
    while rest > 0:
        open_ = [k for k in negs if a[k] < size[k]] or [k for k in kps if a[k] < size[k]]
        if not open_:
            break
        share = max(1, rest // len(open_))
        for k in open_:
            add = min(share, size[k] - a[k], rest)
            a[k] += add
            rest -= add
    out = {}
    for kp in kps:
        hs = {h: sizes.get((*kp, h), 0) for h in ("dev", "held")}
        base = {h: int(hs[h] > 0) for h in hs}
        extra = _largest_remainder(a[kp] - sum(base.values()), {h: hs[h] - base[h] for h in hs})
        for h in hs:
            out[(*kp, h)] = base[h] + extra[h]
    return out


def unit_id_of(kind: str, rid: str, text: str) -> str:
    """Stable unit id from the unit's identity (kind, record, normalised quote or claim): the
    same unit keeps its id across draws, and a different unit never inherits one."""
    h = hashlib.sha256(f"{kind}\0{rid}\0{norm_key(text)}".encode()).hexdigest()
    return f"u{h[:12]}"


def draw_fingerprint(units: Sequence[Mapping], seed: int, gold: Sequence[Mapping] = ()) -> str:
    """Digest of one draw (seed, unit ids and inclusion probabilities, and each gold window's
    id, record, bounds and text and summary hashes): stored in the page, in every label it
    saves, and in the key, so labels from another draw are refused."""
    body = json.dumps(
        {
            "seed": seed,
            "units": sorted((u["unit_id"], round(u["pi"], 12)) for u in units),
            "gold": sorted((dict(g) for g in gold), key=lambda g: str(g.get("id"))),
        },
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def select_label_items(
    dev_rows_items: Iterable[Mapping],
    dev_rows_claims: Iterable[Mapping],
    seed: int = S1_SEED,
    n: int = 100,
    target_pos: int = 25,
    *,
    halves: Mapping[str, str] | None = None,
    return_frame: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    """Draw the blind-label units (spec U5, D6).

    Frame: `label_units` over all judges' dev rows. Strata: (kind, judge-positive, half),
    allocated by `_allocate` (enriched for item `dropped` and claim `unsupported`, at least one
    draw per nonempty stratum), then drawn by simple random sampling without replacement. Each
    unit records `pi` = n_h / N_h (its inclusion probability), `stratum`, `stratum_N`,
    `stratum_n` and a stable `unit_id` (`unit_id_of`). `halves` maps record id -> "dev" |
    "held" (else each row's `half` field). Returned in a seeded random order: the page's order.
    With `return_frame`, also returns the full frame: every stratum, empty ones included, with
    its N, n and pi."""
    units = label_units(dev_rows_items, dev_rows_claims, halves=halves)
    by_stratum: dict[tuple[str, bool, str], list[dict]] = defaultdict(list)
    for u in units:
        by_stratum[(u["kind"], u["pos"], u["half"])].append(u)
    alloc = _allocate(n, target_pos, {s: len(v) for s, v in by_stratum.items()})
    rng = random.Random(seed)
    chosen: list[dict] = []
    frame: dict[str, dict] = {}
    for s in STRATA:
        pool = by_stratum.get(s, [])
        k = alloc.get(s, 0)
        name = stratum_name(*s)
        frame[name] = {"N": len(pool), "n": k, "pi": k / len(pool) if pool else None}
        if not pool:
            continue
        for u in rng.sample(pool, k):
            u = dict(u)
            u.update(stratum=name, stratum_N=len(pool), stratum_n=k, pi=k / len(pool))
            u["unit_id"] = unit_id_of(u["kind"], u["rid"], u["text"])
            chosen.append(u)
    if len({u["unit_id"] for u in chosen}) != len(chosen):
        raise ValueError("unit id collision; lengthen unit_id_of's digest")
    rng.shuffle(chosen)
    return (chosen, frame) if return_frame else chosen


def frame_summary(units: Sequence[Mapping]) -> dict[str, dict]:
    """stratum -> {N, n, pi} for the strata the selected units come from. Prefer the full frame
    from `select_label_items(..., return_frame=True)`, which also lists empty strata."""
    out: dict[str, dict] = {}
    for u in units:
        out[u["stratum"]] = {"N": u["stratum_N"], "n": u["stratum_n"], "pi": u["pi"]}
    return dict(sorted(out.items()))


# ------------------------------------------------------------------------------------------
# 2. Outcome analysis (sealed rows only)
# ------------------------------------------------------------------------------------------


def default_reader(name: str) -> Any:
    """The only route to outcomes: a released, unchanged sealed file of stage S1."""
    return seal.open_sealed(SEAL_STAGE, name)


def usable_item(r: Mapping) -> bool:
    return _item_labelable(r)


def is_lost(r: Mapping) -> bool:
    return r.get("grade") in LOST_GRADES and r.get("availability") == "lost"


def is_user_rule(r: Mapping) -> bool:
    return r.get("type") == "rule" and r.get("origin") == "user"


def session_group_of(r: Mapping) -> str:
    """The bootstrap cluster: the row's `session_group` if present, else U1's rule
    (`s1_data.session_group`: SWE-chat's daily snapshots "<YYYY-MM-DD>-<uuid>" of one session
    share a group)."""
    if r.get("session_group"):
        return str(r["session_group"])
    sid = r.get("session_id")
    if not sid:
        return NO_SESSION
    return _session_group(str(r.get("source")), str(sid))


def stratum_of(r: Mapping) -> tuple[str, str]:
    """(source, "model | harness"): Part A's sampling stratum, as in s1_data's manifest."""
    return str(r.get("source")), f"{r.get('model')} | {r.get('harness')}"


def stratum_weights(manifest: Mapping) -> dict[tuple[str, str], float]:
    """Inverse sampling fractions eligible/drawn per Part A stratum, from the manifest of
    `s1_data.write_manifest` (`strata_population`, `strata_part_a`: source -> stratum -> n)."""
    pop, drawn = manifest["strata_population"], manifest["strata_part_a"]
    out = {}
    for source, cells in drawn.items():
        for stratum, n in cells.items():
            if n > 0:
                out[(source, stratum)] = pop[source][stratum] / n
    return out


Weights = Mapping[tuple[str, str], float] | Callable[[Mapping], float] | None


def _weight_fn(weights: Weights) -> Callable[[Mapping], float] | None:
    if weights is None:
        return None
    if callable(weights):
        return weights
    table = dict(weights)

    def f(r: Mapping) -> float:
        k = stratum_of(r)
        if k not in table:
            raise KeyError(f"no stratum weight for {k} (row {r.get('rid')!r})")
        return float(table[k])

    return f


def _add_weighted(df: pd.DataFrame, cols: Sequence[str]) -> None:
    for c in cols:
        df[f"{c}_w"] = df[c] * df["w"]


def _ratio_ci(
    df: pd.DataFrame,
    num: str,
    den: str,
    *,
    cluster: str = "session_group",
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> dict:
    """Ratio sum(num)/sum(den) with a bootstrap interval clustered on session groups. Rows are
    summed to one row per cluster first (the same resampling, faster)."""
    d = df.copy()
    d[cluster] = d[cluster].fillna("<none>").astype(str)
    agg = d.groupby(cluster, as_index=False)[[num, den]].sum()

    def stat(x: pd.DataFrame) -> float:
        s = float(x[den].sum())
        return float(x[num].sum()) / s if s > 0 else float("nan")

    if len(agg) == 0 or float(agg[den].sum()) == 0:
        return {"point": float("nan"), "lo": float("nan"), "hi": float("nan"), "n_clusters": 0}
    ci = cluster_bootstrap_ci(agg, [cluster], stat, n_boot=n_boot, seed=seed)
    return {k: ci[k] for k in ("point", "lo", "hi", "n_clusters", "n_boot_valid")}


def _one_judge(rows: Iterable[Mapping]) -> list[Mapping]:
    """The rows as a list, refusing rows from more than one judge (rates are never pooled
    across judges)."""
    rows = list(rows)
    judges = sorted({str(r["judge"]) for r in rows if r.get("judge") is not None})
    if len(judges) > 1:
        raise ValueError(f"rows from several judges {judges}: call once per judge")
    return rows


def _item_frame(rows: Iterable[Mapping], weights: Weights = None) -> pd.DataFrame:
    """Usable items as a frame; `w` is each row's stratum weight (1 when `weights` is None).
    Refuses rows from more than one judge."""
    wf = _weight_fn(weights)
    recs = []
    for r in _one_judge(rows):
        if not usable_item(r):
            continue
        recs.append(
            {
                "rid": r["rid"],
                "base_rid": r.get("base_rid") or r["rid"],
                "arm": r.get("arm", "natural"),
                "item_id": r.get("item_id"),
                "quote_key": norm_key(r.get("quote")),
                "type": r.get("type"),
                "origin": r.get("origin"),
                "source": r.get("source"),
                "model": r.get("model"),
                "harness": r.get("harness"),
                "session_id": r.get("session_id") or NO_SESSION,
                "session_group": session_group_of(r),
                "w": wf(r) if wf else 1.0,
                "n": 1,
                "dropped": int(r.get("grade") == "dropped"),
                "misstated": int(r.get("grade") == "misstated"),
                "lost": int(is_lost(r)),
                "user_rule": int(is_user_rule(r)),
            }
        )
    cols = [
        "rid",
        "base_rid",
        "arm",
        "item_id",
        "quote_key",
        "type",
        "origin",
        "source",
        "model",
        "harness",
        "session_id",
        "session_group",
        "w",
        "n",
        "dropped",
        "misstated",
        "lost",
        "user_rule",
    ]
    return pd.DataFrame(recs, columns=cols)


def _rates(
    g: pd.DataFrame,
    pairs: Mapping[str, tuple[str, str]],
    weighted: bool,
    n_boot: int,
    seed: int,
) -> dict:
    """{name: interval} for each (numerator, denominator) pair, plus `<name>_weighted` (the
    same ratio of stratum-weighted sums) when `weighted`."""
    out = {}
    for name, (num, den) in pairs.items():
        out[name] = _ratio_ci(g, num, den, n_boot=n_boot, seed=seed)
        if weighted:
            out[f"{name}_weighted"] = _ratio_ci(g, f"{num}_w", f"{den}_w", n_boot=n_boot, seed=seed)
    return out


def item_rates(
    rows: Iterable[Mapping],
    by: Sequence[str] = ("type", "source"),
    *,
    weights: Weights = None,
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> list[dict]:
    """Drop, mis-state and loss rates among usable items, per group of `by` (e.g. type x
    source, type x model, type x harness), each with a session-group-clustered interval.

    `weights` (Part A): stratum -> weight (see `stratum_weights`: eligible / drawn per (source,
    "model | harness")) or a row -> weight callable. When given, each rate is also reported
    weighted to the eligible population (`<rate>_weighted`), beside the unweighted sample rate.
    Weighting only re-balances strata within a source; a group pooling sources pools their
    eligible populations."""
    df = _item_frame(rows, weights)
    out = []
    if df.empty:
        return out
    _add_weighted(df, ["n", "dropped", "misstated", "lost"])
    pairs = {f"{m}_rate": (m, "n") for m in ("dropped", "misstated", "lost")}
    for keys, g in df.groupby(list(by), dropna=False, sort=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        row: dict[str, Any] = dict(zip(by, keys, strict=True))
        row.update(
            n_items=int(g["n"].sum()),
            n_compactions=int(g["rid"].nunique()),
            n_session_groups=int(g["session_group"].nunique()),
            n_dropped=int(g["dropped"].sum()),
            n_misstated=int(g["misstated"].sum()),
            n_lost=int(g["lost"].sum()),
        )
        row.update(_rates(g, pairs, weights is not None, n_boot, seed))
        out.append(row)
    return out


Roster = Mapping[str, Mapping]  # rid -> {"ok", "source", "model", "harness", "session_id", ...}


def _roster_split(roster: Roster | None, rids_with_rows: Iterable[str]) -> tuple[dict, dict]:
    """(ok records, coverage report). Coverage: records judged, succeeded, failed (named),
    rows whose record isn't on the roster."""
    if roster is None:
        return {}, {"roster": False}
    ok = {rid: m for rid, m in roster.items() if m.get("ok")}
    failed = sorted(rid for rid, m in roster.items() if not m.get("ok"))
    seen = set(rids_with_rows)
    return ok, {
        "roster": True,
        "n_records": len(roster),
        "n_ok": len(ok),
        "n_failed": len(failed),
        "failed": failed,
        "n_ok_without_usable_rows": sum(1 for rid in ok if rid not in seen),
        "n_rows_records_not_on_roster": len(seen - set(roster)),
    }


def rule_loss_share(
    rows: Iterable[Mapping],
    by: Sequence[str] = (),
    *,
    weights: Weights = None,
    roster: Roster | None = None,
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> list[dict]:
    """Per compaction (rid): does it lose >= 1 user-given rule (dropped or mis-stated, and
    availability lost)? `share` is over compactions with >= 1 usable user-given rule;
    `share_of_all` over every compaction judged. Without a `roster` "every compaction" means
    every compaction with a usable item; with one (rid -> {"ok", "source", "model", "harness",
    "session_id"}), successfully judged compactions with no usable item count too, failed ones
    are left out, and `coverage` reports both. One judge per call. Per group of `by` or overall;
    `weights` as in `item_rates` (adds `*_weighted`)."""
    rows = [
        r
        for r in _one_judge(rows)
        if roster is None or roster.get(r["rid"], {"ok": True}).get("ok")
    ]
    df = _item_frame(rows, weights)
    ok, coverage = _roster_split(roster, df["rid"].unique() if not df.empty else [])
    df["rule_lost"] = df["lost"] * df["user_rule"]
    keys = ["rid", "session_group", *by]
    per = df.groupby(keys, dropna=False, as_index=False).agg(
        has_rule=("user_rule", "max"), lost_any=("rule_lost", "max"), w=("w", "first")
    )
    wf = _weight_fn(weights)
    extra = []
    have = set(per["rid"])
    for rid, m in sorted(ok.items()):
        if rid in have:
            continue
        meta = {"rid": rid, **m}
        extra.append(
            {
                "rid": rid,
                "session_group": session_group_of(meta),
                **{c: meta.get(c) for c in by},
                "has_rule": 0,
                "lost_any": 0,
                "w": wf(meta) if wf else 1.0,
            }
        )
    if extra:
        per = pd.concat([per, pd.DataFrame(extra)], ignore_index=True)
    if per.empty:
        return []
    per["one"] = 1
    _add_weighted(per, ["one", "lost_any"])
    groups = per.groupby(list(by), dropna=False, sort=True) if by else [((), per)]
    out = []
    for gk, g in groups:
        gk = gk if isinstance(gk, tuple) else (gk,)
        row: dict[str, Any] = dict(zip(by, gk, strict=True))
        withr = g[g["has_rule"] == 1]
        row.update(
            n_compactions=len(g),
            n_with_user_rule=len(withr),
            n_losing=int(g["lost_any"].sum()),
        )
        pair = {"x": ("lost_any", "one")}
        r1 = _rates(withr, pair, weights is not None, n_boot, seed)
        r2 = _rates(g, pair, weights is not None, n_boot, seed)
        row["share"] = r1["x"]
        row["share_of_all"] = r2["x"]
        if weights is not None:
            row["share_weighted"] = r1["x_weighted"]
            row["share_of_all_weighted"] = r2["x_weighted"]
        row["coverage"] = coverage
        out.append(row)
    return out


def invented_rates(
    claim_rows: Iterable[Mapping],
    by: Sequence[str] = (),
    *,
    weights: Weights = None,
    roster: Roster | None = None,
    anchor: str = "primary",
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> list[dict]:
    """Invented spans among the valid claims of summaries whose history is complete.

    Anchor tiers (`summary_quote_match`, how the claim's span was found in the summary):
    `anchor="primary"` keeps claims anchored "exact" or "normalized" (fuzzy-anchored claims and
    their spans are dropped); the sensitivities are `"exact_only"` and `"including_fuzzy"`
    (every tier, rows without a tier included). `ANCHOR_TIERS` lists them.

    Span level (headline; what the label gate validates): a span is (rid, normalised
    `summary_quote`) among one judge's valid claims; its verdict is the worst of the judge's
    claims on it (`CLAIM_PRECEDENCE`), and it is *invented* when that is `unsupported` (the
    history being complete). It is a *permission span* when any of its claims is a permission,
    and an *invented permission span* only when one of its permission claims is itself
    unsupported (an unsupported non-permission claim beside a supported permission claim makes
    an invented span, not an invented permission span).
    - `summaries_with_invented`: share of summaries with >= 1 invented span (the same as with
      >= 1 invented claim);
    - `span_share` (invented spans / spans, pooled) and `per_summary_mean` (the mean per-summary
      share, over summaries with >= 1 span);
    - `summaries_with_invented_permission`, `permission_span_share` (invented permission spans /
      spans) and `invented_among_permission_spans` (invented / permission spans).
    Claim level, descriptive only: `claim_share_descriptive` (invented claims / claims) and the
    counts. All with session-group intervals, and `*_weighted` when `weights` is given.
    With a
    `roster` (rid -> {"ok", "history_complete", "source", "model", "harness", "session_id"}),
    complete summaries judged successfully with no valid claim count as summaries with none,
    failed ones are left out, and `coverage` reports both. One judge per call."""
    if anchor not in ANCHOR_TIERS:
        raise ValueError(f"anchor must be one of {sorted(ANCHOR_TIERS)}, got {anchor!r}")
    tiers = ANCHOR_TIERS[anchor]
    claim_rows = _one_judge(claim_rows)
    wf = _weight_fn(weights)
    kept = [
        r
        for r in claim_rows
        if r.get("valid")
        and r.get("history_complete")
        and r.get("verdict") in CLAIM_PRECEDENCE
        and (roster is None or roster.get(r["rid"], {"ok": True}).get("ok"))
        and (tiers is None or r.get("summary_quote_match") in tiers)
    ]
    spans: dict[tuple[str, str], dict] = {}
    for r in kept:
        k = (r["rid"], norm_key(claim_span(r)))
        sp = spans.setdefault(
            k,
            {
                "row": r,
                "verdicts": [],
                "perm": False,
                "perm_inv": False,
                "n_claims": 0,
                "inv_claims": 0,
            },
        )
        sp["verdicts"].append(r["verdict"])
        is_perm = bool(r.get("is_permission"))
        sp["perm"] = sp["perm"] or is_perm
        sp["perm_inv"] = sp["perm_inv"] or (is_perm and r["verdict"] == "unsupported")
        sp["n_claims"] += 1
        sp["inv_claims"] += int(r["verdict"] == "unsupported")

    def rec(r: Mapping, **v: int) -> dict:
        return {
            "rid": r["rid"],
            "session_group": session_group_of(r),
            "source": r.get("source"),
            "model": r.get("model"),
            "harness": r.get("harness"),
            "arm": r.get("arm", "natural"),
            "w": wf(r) if wf else 1.0,
            **v,
        }

    zero = {"n": 0, "inv": 0, "perm": 0, "inv_perm": 0, "n_claims": 0, "inv_claims": 0}
    recs = []
    for sp in spans.values():
        inv = int(min(sp["verdicts"], key=CLAIM_PRECEDENCE.index) == "unsupported")
        recs.append(
            rec(
                sp["row"],
                n=1,
                inv=inv,
                perm=int(sp["perm"]),
                inv_perm=int(sp["perm_inv"]),
                n_claims=sp["n_claims"],
                inv_claims=sp["inv_claims"],
            )
        )
    ok, coverage = _roster_split(roster, {x["rid"] for x in recs})
    seen = {x["rid"] for x in recs}
    recs += [
        rec({"rid": rid, **m}, **zero)
        for rid, m in sorted(ok.items())
        if rid not in seen and m.get("history_complete")
    ]
    df = pd.DataFrame(recs)
    if df.empty:
        return []
    per = df.groupby(["rid", "session_group", *by], dropna=False, as_index=False).agg(
        n=("n", "sum"),
        inv=("inv", "sum"),
        perm=("perm", "sum"),
        inv_perm=("inv_perm", "sum"),
        n_claims=("n_claims", "sum"),
        inv_claims=("inv_claims", "sum"),
        w=("w", "first"),
    )
    per["has"] = (per["n"] > 0).astype(int)
    per["rate"] = (per["inv"] / per["n"].where(per["n"] > 0)).fillna(0.0)
    per["any_inv"] = (per["inv"] > 0).astype(int)
    per["any_perm"] = (per["inv_perm"] > 0).astype(int)
    per["one"] = 1
    _add_weighted(
        per,
        [
            "n",
            "inv",
            "perm",
            "inv_perm",
            "rate",
            "has",
            "any_inv",
            "any_perm",
            "one",
            "n_claims",
            "inv_claims",
        ],
    )
    pairs = {
        "summaries_with_invented": ("any_inv", "one"),
        "span_share": ("inv", "n"),
        "per_summary_mean": ("rate", "has"),
        "summaries_with_invented_permission": ("any_perm", "one"),
        "permission_span_share": ("inv_perm", "n"),
        "invented_among_permission_spans": ("inv_perm", "perm"),
        "claim_share_descriptive": ("inv_claims", "n_claims"),
    }
    groups = per.groupby(list(by), dropna=False, sort=True) if by else [((), per)]
    out = []
    for gk, g in groups:
        gk = gk if isinstance(gk, tuple) else (gk,)
        row: dict[str, Any] = dict(zip(by, gk, strict=True))
        row.update(
            unit="span",
            anchor=anchor,
            n_summaries=len(g),
            n_summaries_without_spans=int((g["has"] == 0).sum()),
            n_spans=int(g["n"].sum()),
            n_invented_spans=int(g["inv"].sum()),
            n_permission_spans=int(g["perm"].sum()),
            n_invented_permission_spans=int(g["inv_perm"].sum()),
            n_summaries_with_invented_permission=int(g["any_perm"].sum()),
            n_claims_descriptive=int(g["n_claims"].sum()),
            n_invented_claims_descriptive=int(g["inv_claims"].sum()),
        )
        row.update(_rates(g, pairs, weights is not None, n_boot, seed))
        row["coverage"] = coverage
        out.append(row)
    return out


def _read(reader: Callable[[str], Any], name: str) -> list[dict] | None:
    try:
        return list(reader(name))
    except FileNotFoundError:
        return None


def part_a_report(
    judges: Sequence[str],
    *,
    reader: Callable[[str], Any] = default_reader,
    weights: Weights = None,
    rosters: Mapping[str, Roster] | None = None,
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> dict:
    """Part A tables per judge (never pooled), from `partA_items_<judge>.jsonl` and
    `partA_claims_<judge>.jsonl`. A judge whose file is missing gets `{"missing": [names]}`.
    Pass `weights=stratum_weights(manifest)` for population-weighted rates beside the sample
    rates, and `rosters` (judge -> roster, see `rule_loss_share`) so that compaction- and
    summary-level denominators include records judged with nothing usable."""
    kw = {"weights": weights, "n_boot": n_boot, "seed": seed}
    out: dict[str, Any] = {}
    for j in judges:
        rkw = {**kw, "roster": (rosters or {}).get(j)}
        items = _read(reader, f"partA_items_{j}.jsonl")
        claims = _read(reader, f"partA_claims_{j}.jsonl")
        rep: dict[str, Any] = {"judge": j}
        missing = [n for n, v in (("items", items), ("claims", claims)) if v is None]
        if missing:
            rep["missing"] = missing
        if items is not None:
            rep["items"] = {
                f"type_x_{c}": item_rates(items, ("type", c), **kw)
                for c in ("source", "model", "harness")
            }
            rep["user_rule_loss"] = {
                "overall": rule_loss_share(items, **rkw),
                **{
                    f"by_{c}": rule_loss_share(items, (c,), **rkw)
                    for c in ("source", "model", "harness")
                },
            }
        if claims is not None:
            rep["invented"] = {
                "overall": invented_rates(claims, **rkw),
                **{
                    f"by_{c}": invented_rates(claims, (c,), **rkw)
                    for c in ("source", "model", "harness")
                },
            }
            rep["invented_anchor_sensitivity"] = {
                tier: {
                    "overall": invented_rates(claims, anchor=tier, **rkw),
                    **{
                        f"by_{c}": invented_rates(claims, (c,), anchor=tier, **rkw)
                        for c in ("source", "model", "harness")
                    },
                }
                for tier in ("exact_only", "including_fuzzy")
            }
        out[j] = rep
    return out


def part_b_arm_rates(
    item_rows: Iterable[Mapping], *, n_boot: int = N_BOOT, seed: int = S1_SEED
) -> list[dict]:
    """Per arm: the item-level loss rate of user-given rules, the share of compactions losing
    >= 1 user-given rule, and the drop rate over all usable items."""
    rows = _one_judge(item_rows)
    df = _item_frame(rows)
    out = []
    for arm, g in df.groupby("arm", sort=True):
        ur = g[g["user_rule"] == 1]
        arm_rows = [r for r in rows if r.get("arm", "natural") == arm]
        share = rule_loss_share(arm_rows, n_boot=n_boot, seed=seed)
        out.append(
            {
                "arm": arm,
                "n_items": len(g),
                "n_user_rules": len(ur),
                "n_compactions": int(g["rid"].nunique()),
                "user_rule_loss_rate": _ratio_ci(ur, "lost", "n", n_boot=n_boot, seed=seed),
                "compactions_losing_user_rule": share[0]["share"] if share else None,
                "drop_rate": _ratio_ci(g, "dropped", "n", n_boot=n_boot, seed=seed),
            }
        )
    return out


def paired_contrast(
    item_rows: Iterable[Mapping],
    a: str,
    b: str,
    *,
    label: str = "exploratory",
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> dict:
    """Arm `a` minus arm `b` in the loss rate of user-given rules, paired by history.

    Pass 1a items come from the base history once per judge and are reused across arms, so the
    pairs are (base_rid, item_id) usable in both arms. Point: (lost under a - lost under b) /
    paired items. Interval: session-group-clustered bootstrap (both arms of a session move together).
    Also the per-compaction difference in "loses >= 1 user-given rule" over the paired
    compactions."""
    df = _item_frame(item_rows)
    df = df[(df["user_rule"] == 1) & df["arm"].isin([a, b])]
    wide = df.pivot_table(
        index=["base_rid", "item_id", "session_group"],
        columns="arm",
        values="lost",
        aggfunc="first",
    )
    res: dict[str, Any] = {"a": a, "b": b, "label": label, "metric": "user-given rules lost"}
    if a not in wide.columns or b not in wide.columns:
        res.update(n_items=0, note="an arm has no usable user-given rules")
        return res
    wide = wide.dropna(subset=[a, b]).reset_index()
    wide["d"] = wide[a] - wide[b]
    wide["one"] = 1
    per = wide.groupby(["base_rid", "session_group"], as_index=False).agg(
        la=(a, "max"), lb=(b, "max")
    )
    per["d_any"] = per["la"] - per["lb"]
    per["one"] = 1
    res.update(
        n_items=len(wide),
        n_compactions=int(wide["base_rid"].nunique()),
        n_session_groups=int(wide["session_group"].nunique()),
        rate_a=_ratio_ci(wide, a, "one", n_boot=n_boot, seed=seed),
        rate_b=_ratio_ci(wide, b, "one", n_boot=n_boot, seed=seed),
        diff=_ratio_ci(wide, "d", "one", n_boot=n_boot, seed=seed),
        compaction_any_diff=_ratio_ci(per, "d_any", "one", n_boot=n_boot, seed=seed),
    )
    return res


def part_b_report(
    judges: Sequence[str],
    *,
    reader: Callable[[str], Any] = default_reader,
    primary: tuple[str, str] = PRIMARY_CONTRAST,
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> dict:
    """Part B per judge, from one file per judge across arms (`partB_items_<judge>.jsonl`, rows
    carrying `arm`; rows of other arms, e.g. natural, are ignored): per-arm rates, the primary
    contrast (cc_prompt vs codex_local, user-given rules lost, paired by base_rid) and every
    other arm pair, labelled exploratory. Arm coverage is reported (`arms_present`,
    `arms_missing`); when a required arm is absent the primary contrast is refused with
    `missing_arms` rather than computed on nothing."""
    out: dict[str, Any] = {}
    for j in judges:
        items = _read(reader, f"partB_items_{j}.jsonl")
        if items is None:
            out[j] = {"judge": j, "missing": ["items"]}
            continue
        items = [r for r in items if r.get("arm") in PART_B_ARMS]  # one file across arms
        arms = sorted({r["arm"] for r in items})
        missing = [a for a in primary if a not in arms]
        pairs = [p for p in itertools.combinations(arms, 2) if set(p) != set(primary)]
        if missing:
            prim: dict[str, Any] = {
                "a": primary[0],
                "b": primary[1],
                "label": "primary",
                "missing_arms": missing,
                "note": "a required arm has no rows: the primary contrast is not computed",
            }
        else:
            prim = paired_contrast(items, *primary, label="primary", n_boot=n_boot, seed=seed)
        out[j] = {
            "judge": j,
            "arms_present": arms,
            "arms_missing": [a for a in PART_B_ARMS if a not in arms],
            "required_arms_ok": not missing,
            "arms": part_b_arm_rates(items, n_boot=n_boot, seed=seed),
            "primary": prim,
            "exploratory": [
                paired_contrast(items, x, y, label="exploratory", n_boot=n_boot, seed=seed)
                for x, y in pairs
            ],
        }
    return out


_SIZE = re.compile(r"(\d+)\s*([km]?)", re.IGNORECASE)


def _default_prefix(r: Mapping) -> str:
    if r.get("prefix") is not None:
        return str(r["prefix"])
    rid = str(r["rid"])
    if "@" not in rid:
        raise ValueError(f"size row {rid!r} has no prefix field and no '@' suffix")
    return rid.split("@", 1)[1]


def _default_orig(r: Mapping) -> str:
    return str(r.get("orig_rid") or str(r.get("base_rid") or r["rid"]).split("@", 1)[0])


def _size_order(p: str) -> tuple:
    m = _SIZE.search(p)
    if not m:
        return (math.inf, p)
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return (int(m.group(1)) * mult, p)


def size_test(
    rows: Iterable[Mapping],
    *,
    prefix_of: Callable[[Mapping], str] = _default_prefix,
    orig_of: Callable[[Mapping], str] = _default_orig,
    n_boot: int = N_BOOT,
    seed: int = S1_SEED,
) -> dict:
    """Size test (descriptive): loss rates on items common to both history prefixes.

    Each row's prefix comes from `prefix_of` (default: a `prefix` field, else the rid's suffix
    after '@') and its original record from `orig_of` (default: `orig_rid`, else base_rid before
    '@'). Items are matched across prefixes by (original record, normalised quote); only items
    usable in both prefixes count. Reports loss and drop rates per prefix, and longer - shorter
    with a session-clustered interval."""
    recs = []
    for r in _one_judge(rows):
        if not usable_item(r):
            continue
        recs.append(
            {
                "orig": orig_of(r),
                "prefix": prefix_of(r),
                "q": norm_key(r.get("quote")),
                "session_group": session_group_of(r),
                "lost": int(is_lost(r)),
                "dropped": int(r.get("grade") == "dropped"),
                "user_rule": int(is_user_rule(r)),
            }
        )
    df = pd.DataFrame(recs)
    res: dict[str, Any] = {"label": "descriptive"}
    if df.empty:
        res.update(n_items=0, prefixes=[])
        return res
    prefixes = sorted(df["prefix"].unique(), key=_size_order)
    res["prefixes"] = list(prefixes)
    if len(prefixes) != 2:
        res.update(n_items=0, note=f"need exactly 2 prefixes, got {prefixes}")
        return res
    short, long_ = prefixes
    df = df.drop_duplicates(["orig", "prefix", "q"])
    out_cols: dict[str, Any] = {}
    for m in ("lost", "dropped"):
        w = (
            df.pivot_table(
                index=["orig", "q", "session_group"], columns="prefix", values=m, aggfunc="first"
            )
            .dropna(subset=[short, long_])
            .reset_index()
        )
        w["d"] = w[long_] - w[short]
        w["one"] = 1
        out_cols[m] = {
            short: _ratio_ci(w, short, "one", n_boot=n_boot, seed=seed),
            long_: _ratio_ci(w, long_, "one", n_boot=n_boot, seed=seed),
            "long_minus_short": _ratio_ci(w, "d", "one", n_boot=n_boot, seed=seed),
        }
        res["n_items"] = len(w)
        res["n_records"] = int(w["orig"].nunique())
    ur = df[df["user_rule"] == 1]
    w = ur.pivot_table(index=["orig", "q"], columns="prefix", values="lost", aggfunc="first")
    res["n_user_rules_common"] = (
        int(w.dropna(subset=[short, long_]).shape[0])
        if short in w.columns and long_ in w.columns
        else 0
    )
    res["rates"] = out_cols
    return res


# ------------------------------------------------------------------------------------------
# 3. Verbatim-budget check (no seal)
# ------------------------------------------------------------------------------------------

CODEX_BUDGETS = (20_000, 64_000)  # Codex local keep budget; server-side budget
AGY_MAX_REQUESTS = 10  # Antigravity keeps up to 10 user requests verbatim


def _user_turns(rec: Mapping) -> list[str]:
    if rec.get("user_turns") is not None:
        return [str(t) for t in rec["user_turns"]]
    return [
        str(m.get("text") or "") for m in rec.get("source_messages", []) if m.get("role") == "user"
    ]


def _pct(xs: Sequence[float], q: float) -> float | None:
    return float(np.percentile(xs, q)) if len(xs) else None


def verbatim_budget(
    records: Iterable[Mapping],
    *,
    budgets: Sequence[int] = CODEX_BUDGETS,
    max_requests: int = AGY_MAX_REQUESTS,
    early_k: int = 3,
    user_turns_of: Callable[[Mapping], list[str]] | None = None,
) -> dict:
    """For each compaction and each of its first `early_k` user turns: the user text after that
    turn by compaction time (`after_tokens`, bytes/4 as Codex counts) and the number of later
    user requests. A turn is *outside* a Codex budget B when after_tokens >= B (Codex keeps the
    most recent user messages up to B tokens, newest first, so nothing of it survives), and
    *not verbatim* when after_tokens + its own tokens > B (at best truncated). It is outside
    Antigravity's window when >= `max_requests` later requests follow it.

    User turns are the record's `user_turns` (user-authored text in the replaced history), else
    its `source_messages` with role user; pass `user_turns_of` to count others too (e.g. user
    turns among the messages Claude Code kept verbatim before the boundary). Returns per-record rows and, per source and overall,
    for each early turn k: the share of compactions with that turn outside each budget, plus the
    median and 90th percentile of after_tokens and of later requests (S2 draws its follow-up
    volume from these)."""
    per = []
    for rec in records:
        turns = (user_turns_of or _user_turns)(rec)
        toks = [codex_approx_tokens(t) for t in turns]
        suffix = list(itertools.accumulate(reversed(toks)))[::-1]  # tokens from k to the end
        row = {
            "id": rec.get("id"),
            "source": rec.get("source"),
            "n_user_turns": len(turns),
            "user_tokens": int(sum(toks)),
            "turns": [],
        }
        for k in range(min(early_k, len(turns))):
            after = suffix[k + 1] if k + 1 < len(turns) else 0
            t = {
                "k": k,
                "tokens": toks[k],
                "after_tokens": int(after),
                "later_requests": len(turns) - k - 1,
            }
            for b in budgets:
                t[f"outside_{b}"] = after >= b
                t[f"not_verbatim_{b}"] = after + toks[k] > b
            t[f"outside_agy{max_requests}"] = t["later_requests"] >= max_requests
            row["turns"].append(t)
        per.append(row)

    def summarise(rows: list[dict]) -> dict:
        out: dict[str, Any] = {
            "n_compactions": len(rows),
            "n_without_user_turn": sum(1 for r in rows if not r["turns"]),
        }
        for k in range(early_k):
            ts = [r["turns"][k] for r in rows if len(r["turns"]) > k]
            if not ts:
                continue
            d: dict[str, Any] = {"n": len(ts)}
            for flag in (
                [f"outside_{b}" for b in budgets]
                + [f"not_verbatim_{b}" for b in budgets]
                + [f"outside_agy{max_requests}"]
            ):
                d[f"share_{flag}"] = sum(t[flag] for t in ts) / len(ts)
            after = [t["after_tokens"] for t in ts]
            later = [t["later_requests"] for t in ts]
            d.update(
                after_tokens_median=_pct(after, 50),
                after_tokens_p90=_pct(after, 90),
                later_requests_median=_pct(later, 50),
                later_requests_p90=_pct(later, 90),
            )
            out[f"turn{k}"] = d
        return out

    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in per:
        by_source[str(r["source"])].append(r)
    return {
        "budgets": list(budgets),
        "max_requests": max_requests,
        "token_rule": "ceil(UTF-8 bytes / 4)",
        "overall": summarise(per),
        "by_source": {s: summarise(rs) for s, rs in sorted(by_source.items())},
        "records": per,
    }
