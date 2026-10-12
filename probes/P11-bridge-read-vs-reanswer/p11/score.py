"""Laptop-side scoring of P11's lens reads and greedy answers (plan "Measure", "Predictions").

    uv run python -m p11.score report        # results/summary.json plus a compact table

No API calls, no GPU. Inputs are what the pod session writes into ``results/``:
``olens/<arm>_s<k>.jsonl`` (P03 ``OlensRow`` dicts; arms are discovered from the file names),
``olens/smoke_shuffled.jsonl``, ``answers.jsonl``, ``sweep.json`` and, optionally,
``prompt_only.jsonl``; plus P03's own Oracle Lens readouts for the rescore. Any optional input
that is missing is reported as ``"absent"``; the report never crashes on it.

**Matching.** A cell "names X" when ``p11.items.names(text, X, aliases)`` holds: P03's whole-word
rule (``p03.score.text_hit`` on person forms, surnames included) on ``p11.items.clean_aliases``
of X's aliases. Here the forms are built once per item (``phrase_forms``, the same rule; a test
asserts the two agree). The cleaning drops parenthetical qualifiers, which P03's ``surname``
turned into false surnames ("Gustave Moreau (French painter)" -> "painter"). Items where Y's
and the bridge's forms overlap are listed (``form_overlaps``).

**Item level.** An item is named by an arm if any of its cells (6 layers x 3 positions x 2 draws)
names X. Item matrices are dicts ``id -> bool`` over the items that have at least one cell, and
paired comparisons use the items both sides have.

**Statistics.** Wilson 95% intervals (``p03.core.wilson``) for rates. Paired differences use
``p03.core.paired_bootstrap``: 10,000 resamples of items with ``random.Random(0)``, percentile
interval. The flip-conditional difference resamples items (``random.Random(0)``, 10,000) and
recomputes both conditional rates, skipping resamples where either group is empty.

**Flips** come from ``answers.jsonl`` (kind ``patched`` at the sweep's ``chosen_lp``), classed by
``p11.items.answer_class`` on the row's ``answer`` (falling back to ``p03.core.first_answer`` of
``text`` when ``answer`` is empty).

**Logit lens** (``results/logit_lens/<arm>.jsonl``, wsbench token rows): per arm, the item
rate of any cell's top-10 tokens naming Y or the bridge, by ``p03.score.token_hit`` on the
person token forms of the cleaned aliases (``token_forms``). Low power expected (P03: 0-0.02).

**Dose check** (reading vs re-answering, specified before data): the author patch interpolated
from the plain run's own residual at alpha 0.25/0.5/0.75 (arms ``patched_author_a25`` etc., one
lens sample per cell; answers ``kind == "dose"``), plus alpha 1.0 = ``patched_author`` draw s0
only. Over item x alpha units: P(names Y | Qwen flipped) vs P(names Y | not flipped), with an
item-cluster bootstrap (resample items, keep all their alpha units; ``random.Random(0)``,
10,000), and P(names bridge | not flipped); within items, those naming Y at an alpha where Qwen
did not flip, and those whose Y-naming switches on at exactly the flipping alphas.
Interpretation: names-Y on non-flipped units about as often as on flipped units rejects "the
lens reads the bridge Qwen uses"; a strong gap is only weak support for reading, since a
re-answering lens sharing Qwen's weights could show the same gap.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

# ``python -m p11.score`` from the probe dir: make P03's package importable (tests use conftest)
_P03_DIR = Path(__file__).resolve().parents[2] / "P03-text-proof-interp-items"
if str(_P03_DIR) not in sys.path:
    sys.path.append(str(_P03_DIR))

from p03 import core as C
from p03 import score as SC

from p11 import items as I

RESULTS = I.RESULTS
P03_READOUTS = I.P03 / "results" / "read" / "readouts"
ARM_ORDER = ("plain", "neutral", "author", "mention", "patched_author", "patched_mention")
LAYERS = (40, 44, 48, 52, 56, 60)
POSITIONS = (-3, -2, -1)
N_BOOT = 10_000
BOOT_SEED = 0
UNPARSEABLE_MAX = 0.05
SHUFFLED_PASS = 18  # of 20 cells naming neither the bridge nor Y
DOSE_ALPHAS = (0.25, 0.5, 0.75, 1.0)
_ARM_FILE = re.compile(r"^(?P<arm>.+)_s(?P<k>\d+)\.jsonl$")

Bool = dict[str, bool]


# --------------------------------------------------------------------------- matching


def phrase_forms(target: str, aliases: Iterable[str]) -> set[str]:
    """Phrase forms of a person, as ``p11.items.names`` builds them (cleaned aliases)."""
    return SC.forms(target, I.clean_aliases([target, *aliases]), True)[1]


def raw_phrase_forms(target: str, aliases: Iterable[str]) -> set[str]:
    """P03's original (uncleaned) phrase forms, for the rescore comparison."""
    return SC.forms(target, list(aliases), True)[1]


def hit(text: str | None, phr: set[str]) -> bool:
    return bool(text) and SC.text_hit([text], phr)


def item_forms(items: Sequence[I.Item]) -> dict[str, dict[str, set[str]]]:
    """id -> {"y": forms, "bridge": forms} (cleaned)."""
    return {
        it.id: {
            "y": phrase_forms(it.y, it.y_aliases),
            "bridge": phrase_forms(it.bridge, it.bridge_aliases),
        }
        for it in items
    }


def form_overlaps(forms: dict[str, dict[str, set[str]]]) -> list[dict]:
    """Items where Y's and the bridge's forms share a form (a cell could name both)."""
    return [
        {"id": i, "shared": sorted(f["y"] & f["bridge"])}
        for i, f in sorted(forms.items())
        if f["y"] & f["bridge"]
    ]


# --------------------------------------------------------------------------- inputs


def discover_arms(olens_dir: Path) -> dict[str, list[Path]]:
    """arm -> its ``<arm>_s<k>.jsonl`` files, sorted by k (``smoke_*`` files excluded)."""
    out: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    if not Path(olens_dir).is_dir():
        return {}
    for p in Path(olens_dir).iterdir():
        m = _ARM_FILE.match(p.name)
        if m and not p.name.startswith("smoke"):
            out[m["arm"]].append((int(m["k"]), p))
    order = {a: i for i, a in enumerate(ARM_ORDER)}
    return {
        a: [p for _, p in sorted(v)]
        for a, v in sorted(out.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))
    }


def load_arm(paths: Sequence[Path]) -> list[dict]:
    """Rows of an arm's files, each tagged ``_draw`` = "s<k>" from its file name."""
    rows = []
    for p in paths:
        k = _ARM_FILE.match(Path(p).name)["k"]
        rows += [r | {"_draw": f"s{k}"} for r in C.read_jsonl(p)]
    return rows


def load_json(path: Path) -> dict | None:
    return json.loads(Path(path).read_text()) if Path(path).exists() else None


# --------------------------------------------------------------------------- item matrices


def item_any(
    rows: Iterable[dict],
    forms: dict[str, dict[str, set[str]]],
    which: str,
    keep: Callable[[dict], bool] = lambda r: True,
    text_key: str = "text",
) -> Bool:
    """id -> any kept cell of the item names ``which`` ("y" or "bridge"); items without kept
    cells (or not in ``forms``) are left out."""
    out: Bool = {}
    for r in rows:
        i = r["id"]
        if i not in forms or not keep(r):
            continue
        out[i] = out.get(i, False) or hit(r.get(text_key), forms[i][which])
    return out


def rate(m: Bool, ids: Iterable[str] | None = None) -> dict:
    vals = [m[i] for i in (m if ids is None else ids) if i in m]
    k = sum(vals)
    r, lo, hi = C.wilson(k, len(vals))
    return {"rate": r, "lo": lo, "hi": hi, "k": k, "n": len(vals)}


def paired(a: Bool, b: Bool, n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """a - b over the items both have: mean, bootstrap 95% CI and the 2x2 count table."""
    ids = sorted(set(a) & set(b))
    d, lo, hi = C.paired_bootstrap(
        [float(a[i]) for i in ids], [float(b[i]) for i in ids], n_boot=n_boot, seed=seed
    )
    tab = Counter(
        ("both" if a[i] and b[i] else "only_a" if a[i] else "only_b" if b[i] else "neither")
        for i in ids
    )
    return {
        "diff": d,
        "lo": lo,
        "hi": hi,
        "n": len(ids),
        "table": {k: tab.get(k, 0) for k in ("both", "only_a", "only_b", "neither")},
    }


def conditional(hits: Bool, flips: Bool, n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """Rate of ``hits`` among flipped minus among non-flipped items, with an item bootstrap
    (resamples with an empty group are skipped and counted)."""
    ids = sorted(set(hits) & set(flips))
    f = [i for i in ids if flips[i]]
    nf = [i for i in ids if not flips[i]]
    out = {"flipped": rate(hits, f), "not_flipped": rate(hits, nf), "n": len(ids)}
    if not f or not nf:
        return out | {"diff": math.nan, "lo": math.nan, "hi": math.nan, "skipped": n_boot}
    rng = random.Random(seed)
    diffs, skipped = [], 0
    n = len(ids)
    for _ in range(n_boot):
        s = [ids[rng.randrange(n)] for _ in range(n)]
        a = [hits[i] for i in s if flips[i]]
        b = [hits[i] for i in s if not flips[i]]
        if not a or not b:
            skipped += 1
            continue
        diffs.append(sum(a) / len(a) - sum(b) / len(b))
    diffs.sort()
    d = out["flipped"]["rate"] - out["not_flipped"]["rate"]
    lo = diffs[int(0.025 * len(diffs))] if diffs else math.nan
    hi = diffs[max(int(0.975 * len(diffs)) - 1, 0)] if diffs else math.nan
    return out | {"diff": d, "lo": lo, "hi": hi, "skipped": skipped}


# --------------------------------------------------------------------------- sections


def arm_summary(rows: list[dict], forms: dict[str, dict[str, set[str]]], n_files: int) -> dict:
    """Section 1: item rates, per-layer and per-draw profiles, unparseable and length rates."""
    ny, nb = item_any(rows, forms, "y"), item_any(rows, forms, "bridge")
    layers = sorted({r["layer"] for r in rows})
    draws = sorted({r["_draw"] for r in rows})
    empty = sum(not (r.get("text") or "").strip() for r in rows)
    length = sum(r.get("finish") == "length" for r in rows)
    n = len(rows)
    return {
        "names_y": rate(ny),
        "names_bridge": rate(nb),
        "n_items": len(ny),
        "n_cells": n,
        "n_cells_expected": len(forms) * len(LAYERS) * len(POSITIONS) * n_files,
        "frac_unparseable": empty / n if n else math.nan,
        "unparseable_stop": bool(n and empty / n > UNPARSEABLE_MAX),
        "frac_length": length / n if n else math.nan,
        "per_layer": {
            str(lay): {
                w: rate(item_any(rows, forms, w, lambda r, x=lay: r["layer"] == x))
                for w in ("y", "bridge")
            }
            for lay in layers
        },
        "per_draw": {
            d: {
                w: rate(item_any(rows, forms, w, lambda r, x=d: r["_draw"] == x))
                for w in ("y", "bridge")
            }
            for d in draws
        },
    }


def answer_of(row: dict) -> str:
    return row.get("answer") or C.first_answer(row.get("text") or "")


def answer_classes(rows: Iterable[dict], items: dict[str, I.Item]) -> dict[tuple, dict[str, str]]:
    """(kind, premise, lp, alpha) -> id -> "flip" | "stay" | "other" (alpha: dose rows only,
    else None)."""
    out: dict[tuple, dict[str, str]] = defaultdict(dict)
    for r in rows:
        if r["id"] in items:
            key = (r["kind"], r.get("premise"), r.get("lp"), r.get("alpha"))
            out[key][r["id"]] = I.answer_class(answer_of(r), items[r["id"]])
    return dict(out)


def class_rates(cls: dict[str, str]) -> dict:
    n = len(cls)
    out: dict = {"n": n}
    for c in ("flip", "stay", "other"):
        k = sum(v == c for v in cls.values())
        out[c] = dict(zip(("rate", "lo", "hi"), C.wilson(k, n))) | {"k": k}
    return out


def manipulation(classes: dict[tuple, dict[str, str]]) -> dict:
    """Section 5: flip/stay/other per lp x premise, direct premises, plain greedy."""
    patched = {
        f"lp{lp}|{p}": class_rates(c)
        for (k, p, lp, _), c in sorted(
            classes.items(), key=lambda kv: (kv[0][2] or 0, kv[0][1] or "")
        )
        if k == "patched"
    }
    direct = {p: class_rates(c) for (k, p, _, _), c in classes.items() if k == "direct"}
    plain = [c for (k, _, _, _), c in classes.items() if k == "plain"]
    return {
        "patched": patched,
        "direct": direct,
        "plain": class_rates(plain[0]) if plain else "absent",
    }


def flips_at(classes: dict[tuple, dict[str, str]], premise: str, lp: int | None) -> Bool | None:
    c = classes.get(("patched", premise, lp, None)) if lp is not None else None
    return {i: v == "flip" for i, v in c.items()} if c else None


def max_author_flip(sweep: dict | None, classes: dict[tuple, dict[str, str]]) -> float | None:
    rates = [v.get("author") for v in ((sweep or {}).get("rates") or {}).values()]
    rates = [x for x in rates if x is not None]
    if not rates:
        rates = [
            class_rates(c)["flip"]["rate"]
            for (k, p, _, _), c in classes.items()
            if k == "patched" and p == "author"
        ]
    return max(rates) if rates else None


Unit = tuple[bool, bool, bool]  # (Qwen flipped, lens names Y, lens names bridge)


def dose_arm(alpha: float) -> str:
    """The lens arm of a dose: ``patched_author_a25`` etc.; alpha 1.0 is ``patched_author``."""
    return "patched_author" if alpha == 1.0 else f"patched_author_a{round(alpha * 100)}"


def dose_units(
    flips: dict[float, Bool], ny: dict[float, Bool], nb: dict[float, Bool]
) -> dict[str, dict[float, Unit]]:
    """item -> alpha -> (flipped, names Y, names bridge), for the (item, alpha) units that have
    both an answer and lens cells."""
    out: dict[str, dict[float, Unit]] = defaultdict(dict)
    for a, fl in flips.items():
        for i, f in fl.items():
            if i in ny.get(a, {}) and i in nb.get(a, {}):
                out[i][a] = (f, ny[a][i], nb[a][i])
    return dict(out)


def _pooled(
    units: dict[str, dict[float, Unit]], ids: Sequence[str], which: int
) -> tuple[float, float]:
    """(P(lens hit | flipped), P(lens hit | not flipped)) pooled over the items' units."""
    f = [u[which] for i in ids for u in units[i].values() if u[0]]
    nf = [u[which] for i in ids for u in units[i].values() if not u[0]]
    return (sum(f) / len(f) if f else math.nan, sum(nf) / len(nf) if nf else math.nan)


def dose_pooled(
    units: dict[str, dict[float, Unit]], n_boot: int = N_BOOT, seed: int = BOOT_SEED
) -> dict:
    """P(names Y | flipped) - P(names Y | not flipped) over item x alpha units, with an
    item-cluster bootstrap (resample items, keep all their units; resamples with an empty
    group skipped), P(names bridge | not flipped), and the within-item counts."""
    ids = sorted(units)
    all_u = [u for i in ids for u in units[i].values()]
    nf_units = [u for u in all_u if not u[0]]
    yf, ynf = _pooled(units, ids, 1)
    out: dict = {
        "n_items": len(ids),
        "n_units": len(all_u),
        "n_flipped_units": len(all_u) - len(nf_units),
        "names_y_given_flipped": yf,
        "names_y_given_not_flipped": ynf,
        "names_bridge_given_not_flipped": rate(dict(enumerate(u[2] for u in nf_units))),
    }
    rng = random.Random(seed)
    diffs, skipped = [], 0
    for _ in range(n_boot if ids else 0):
        a, b = _pooled(units, [ids[rng.randrange(len(ids))] for _ in ids], 1)
        if math.isnan(a) or math.isnan(b):
            skipped += 1
        else:
            diffs.append(a - b)
    diffs.sort()
    out["diff"] = yf - ynf if not (math.isnan(yf) or math.isnan(ynf)) else math.nan
    out["lo"] = diffs[int(0.025 * len(diffs))] if diffs else math.nan
    out["hi"] = diffs[max(int(0.975 * len(diffs)) - 1, 0)] if diffs else math.nan
    out["skipped"] = skipped
    out["items_y_at_a_non_flip"] = sorted(
        i for i in ids if any(u[1] and not u[0] for u in units[i].values())
    )
    full = [i for i in ids if len(units[i]) == len(DOSE_ALPHAS)]
    out["n_items_all_alphas"] = len(full)
    out["items_y_tracks_flip_exactly"] = sorted(
        i
        for i in full
        if all(u[0] == u[1] for u in units[i].values())
        and len({u[0] for u in units[i].values()}) == 2
    )
    return out


def token_forms(target: str, aliases: Iterable[str]) -> set[str]:
    """Token forms of a person (P03 ``forms``[0]) on cleaned aliases, for logit-lens rows."""
    return SC.forms(target, I.clean_aliases([target, *aliases]), True)[0]


def logit_lens_section(ll_dir: Path, items: Sequence[I.Item]) -> dict | str:
    """Per arm (``<arm>.jsonl``): item rate of any cell's top-10 tokens naming Y / the bridge
    (``p03.score.token_hit``)."""
    files = sorted(Path(ll_dir).glob("*.jsonl")) if Path(ll_dir).is_dir() else []
    if not files:
        return "absent"
    tf = {
        it.id: {
            "y": token_forms(it.y, it.y_aliases),
            "bridge": token_forms(it.bridge, it.bridge_aliases),
        }
        for it in items
    }
    out: dict = {}
    for f in files:
        rows = [r for r in C.read_jsonl(f) if r["id"] in tf]
        sec: dict = {"n_cells": len(rows)}
        for w in ("y", "bridge"):
            m: Bool = {}
            for r in rows:
                m[r["id"]] = m.get(r["id"], False) or SC.token_hit(
                    r.get("tokens") or [], tf[r["id"]][w]
                )
            sec[f"names_{w}"] = rate(m)
        out[f.stem] = sec
    return out


def shuffled_smoke(rows: list[dict], forms: dict[str, dict[str, set[str]]]) -> dict:
    """Section 6: shuffled-activation cells naming the row item's own bridge or Y."""
    named = [
        r
        for r in rows
        if r["id"] in forms
        and (
            hit(r.get("text"), forms[r["id"]]["bridge"]) or hit(r.get("text"), forms[r["id"]]["y"])
        )
    ]
    n = len(rows)
    neither = n - len(named)
    need = math.ceil(SHUFFLED_PASS / 20 * n)
    return {
        "n": n,
        "named": len(named),
        "neither": neither,
        "need_neither": need,
        "pass": neither >= need,
        "named_ids": sorted({r["id"] for r in named}),
        "n_source_is_self": sum(r.get("source_id") == r["id"] for r in rows),
    }


def p03_rescore(items: Sequence[I.Item], readouts: Path) -> dict:
    """Section 8: P03's Oracle Lens on the 63 items, names-bridge with P03's original forms vs
    the cleaned ones (all layers, and layers 40-60), and P03's shuffled control."""
    fc = {it.id: {"bridge": phrase_forms(it.bridge, it.bridge_aliases)} for it in items}
    fr = {it.id: {"bridge": raw_phrase_forms(it.bridge, it.bridge_aliases)} for it in items}
    out: dict = {}
    for name in ("olens", "olens_shuffled"):
        rows = C.read_jsonl(Path(readouts) / f"{name}.jsonl")
        if not rows:
            out[name] = "absent"
            continue
        rows = [r | {"text": "\n".join(r.get("samples") or [])} for r in rows]
        sec = {}
        for scope, keep in (
            ("all_layers", lambda r: True),
            ("layers_40_60", lambda r: r["layer"] in LAYERS),
        ):
            new = item_any(rows, fc, "bridge", keep)
            old = item_any(rows, fr, "bridge", keep)
            sec[scope] = {
                "cleaned": rate(new),
                "original": rate(old),
                "lost": sorted(i for i in old if old[i] and not new.get(i)),
                "gained": sorted(i for i in new if new[i] and not old.get(i)),
            }
        out[name] = sec
    return out


def prompt_only(
    rows: list[dict], forms: dict[str, dict[str, set[str]]], plain_bridge: Bool | None
) -> dict | str:
    """Section 7: stock Qwen prompted for 'what would a model think here' (36 samples/item)."""
    if not rows:
        return "absent"
    nb, ny = item_any(rows, forms, "bridge"), item_any(rows, forms, "y")
    out = {
        "names_bridge": rate(nb),
        "names_y": rate(ny),
        "samples_per_item": dict(Counter(Counter(r["id"] for r in rows).values())),
    }
    out["plain_lens_minus_prompt_only_bridge"] = (
        paired(plain_bridge, nb) if plain_bridge else "absent"
    )
    return out


def selfreport_section(
    rows: list[dict], forms: dict[str, dict[str, set[str]]], m: dict, n_boot: int = N_BOOT
) -> dict | str:
    """F5, the "ask the model" baseline (``p11.selfreport``): per prompt kind and arm, item rates
    of naming the bridge / Y in the greedy answer and in any of greedy + samples; the lens minus
    self-report paired differences (lens: any of its 36 cells; self-report: any of its 11
    answers): plain lens vs plain self-report on the bridge, patched_author lens vs
    patched_author self-report on Y."""
    if not rows:
        return "absent"
    out: dict = {"attempts_per_cell": 1 + len(rows[0].get("samples", []))}
    mats: dict = {}
    for kind in sorted({r["kind"] for r in rows}):
        for arm in sorted({r["arm"] for r in rows if r["kind"] == kind}):
            sel = [r for r in rows if r["kind"] == kind and r["arm"] == arm]
            g = [{"id": r["id"], "text": r["greedy"]} for r in sel]
            a = g + [{"id": r["id"], "text": t} for r in sel for t in r.get("samples", [])]
            res: dict = {}
            for w in ("bridge", "y"):
                res[f"greedy_names_{w}"] = rate(item_any(g, forms, w))
                mats[(kind, arm, w)] = item_any(a, forms, w)
                res[f"any_names_{w}"] = rate(mats[(kind, arm, w)])
            out[f"{kind}/{arm}"] = res
    comp: dict = {}
    for kind in sorted({r["kind"] for r in rows}):
        if "plain" in m and (kind, "plain", "bridge") in mats:
            comp[f"lens_plain_minus_{kind}_plain_bridge"] = paired(
                m["plain"]["bridge"], mats[(kind, "plain", "bridge")], n_boot
            )
        if "patched_author" in m and (kind, "patched_author", "y") in mats:
            comp[f"lens_patched_author_minus_{kind}_patched_author_y"] = paired(
                m["patched_author"]["y"], mats[(kind, "patched_author", "y")], n_boot
            )
        if "patched_author" in m and (kind, "patched_author", "bridge") in mats:
            comp[f"lens_patched_author_minus_{kind}_patched_author_bridge"] = paired(
                m["patched_author"]["bridge"], mats[(kind, "patched_author", "bridge")], n_boot
            )
    # does the lens name the bridge on items where asking the model does not?
    for kind in sorted({r["kind"] for r in rows}):
        sr = mats.get((kind, "plain", "bridge"))
        if sr and "plain" in m:
            lens = m["plain"]["bridge"]
            comp[f"lens_only_vs_{kind}"] = sorted(i for i in lens if lens[i] and not sr.get(i))
    out["comparisons"] = comp
    return out


def selfreport_mats(rows: list[dict], forms: dict[str, dict[str, set[str]]]) -> dict:
    """(kind, arm, "greedy" | "any", "bridge" | "y") -> id -> named, from self-report rows."""
    out: dict = {}
    for kind, arm in sorted({(r["kind"], r["arm"]) for r in rows}):
        sel = [r for r in rows if r["kind"] == kind and r["arm"] == arm]
        g = [{"id": r["id"], "text": r["greedy"]} for r in sel]
        a = g + [{"id": r["id"], "text": t} for r in sel for t in r.get("samples", [])]
        for w in ("bridge", "y"):
            out[(kind, arm, "greedy", w)] = item_any(g, forms, w)
            out[(kind, arm, "any", w)] = item_any(a, forms, w)
    return out


def stay_retention(
    classes: dict[tuple, dict[str, str]],
    m: dict,
    srm: dict,
    lp: int | None,
    n_boot: int = N_BOOT,
) -> dict:
    """Exploratory (log.md 04:58): on the items whose greedy answer under a patch is still
    Qwen's original answer ("stay"), does the lens keep naming Qwen's bridge, and does Qwen's own
    self-report? Lens: any of 36 cells per draw pair, as elsewhere; self-report: greedy and any
    of 11 answers, per prompt kind."""
    out: dict = {}
    for p in ("author", "mention"):
        arm = f"patched_{p}"
        c = classes.get(("patched", p, lp, None)) if lp is not None else None
        if c is None or arm not in m or "plain" not in m:
            out[p] = "absent"
            continue
        ids = sorted(i for i, v in c.items() if v == "stay")

        def sub(x: Bool, ids: list[str] = ids) -> Bool:
            return {i: x[i] for i in ids if i in x}

        res: dict = {
            "n": len(ids),
            "lens_bridge_plain": rate(m["plain"]["bridge"], ids),
            "lens_bridge_patched": rate(m[arm]["bridge"], ids),
            "lens_bridge_patched_minus_plain": paired(
                sub(m[arm]["bridge"]), sub(m["plain"]["bridge"]), n_boot
            ),
            "lens_y_patched": rate(m[arm]["y"], ids),
        }
        for kind in sorted({k[0] for k in srm}):
            for how in ("greedy", "any"):
                pl, pa = (
                    srm.get((kind, "plain", how, "bridge")),
                    srm.get((kind, arm, how, "bridge")),
                )
                if pl is None or pa is None:
                    continue
                res[f"{kind}_{how}"] = {
                    "bridge_plain": rate(pl, ids),
                    "bridge_patched": rate(pa, ids),
                    "y_patched": rate(srm[(kind, arm, how, "y")], ids),
                    "bridge_patched_minus_plain": paired(sub(pa), sub(pl), n_boot),
                    "lens_minus_selfreport_bridge_patched": paired(
                        sub(m[arm]["bridge"]), sub(pa), n_boot
                    ),
                }
        out[p] = res
    return out


def verdicts(s: dict) -> dict:
    """Section 9: plan "Predictions" booleans, each {"value", "verdict"}; None when an input
    is absent."""

    def v(value, test):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return {"value": None, "verdict": None}
        return {"value": value, "verdict": bool(test(value))}

    arms = s.get("arms", {})
    rt = lambda a, w: arms[a][f"names_{w}"]["rate"] if a in arms else None
    prim = s.get("primary")
    prim = prim if isinstance(prim, dict) else None
    sec = s.get("secondary") if isinstance(s.get("secondary"), dict) else {}
    drop = sec.get("bridge_patched_author_minus_plain")
    drop = drop if isinstance(drop, dict) else None
    flipped = sec.get("flipped_only")
    flipped = flipped if isinstance(flipped, dict) else None
    out: dict = {}
    if prim and drop:
        out["read"] = v(
            {"primary": prim["diff"], "primary_lo": prim["lo"], "bridge_drop": -drop["diff"]},
            lambda x: x["primary"] >= 0.15 and x["primary_lo"] > 0 and x["bridge_drop"] >= 0.10,
        )
    else:
        out["read"] = v(None, bool)
    if prim and flipped:
        fb = (
            flipped["patched_author"]["names_bridge"]["rate"]
            - flipped["plain"]["names_bridge"]["rate"]
        )
        out["re_answer"] = v(
            {"primary_lo": prim["lo"], "primary_hi": prim["hi"], "flipped_bridge_change": fb},
            lambda x: (
                x["primary_lo"] <= 0 <= x["primary_hi"] and abs(x["flipped_bridge_change"]) <= 0.10
            ),
        )
    else:
        out["re_answer"] = v(None, bool)
    sweep = s.get("sweep") if isinstance(s.get("sweep"), dict) else {}
    mx = s.get("max_author_flip")
    ay, my = rt("patched_author", "y"), rt("patched_mention", "y")
    if sweep or mx is not None:
        failed = sweep.get("status") == "failed"
        out["uninformative"] = {
            "value": {
                "sweep_status": sweep.get("status"),
                "max_author_flip": mx,
                "mention_names_y": my,
                "author_names_y": ay,
            },
            "verdict": bool(
                failed
                or (mx is not None and mx < 0.25)
                or (ay is not None and my is not None and my >= ay)
            ),
        }
    else:
        out["uninformative"] = v(None, bool)
    pb, nb = rt("plain", "bridge"), rt("neutral", "bridge")
    out["side_plain_bridge_0.30_0.45"] = v(pb, lambda x: 0.30 <= x <= 0.45)
    out["side_neutral_le_half_plain"] = v(
        None if pb is None or nb is None else {"neutral": nb, "plain": pb},
        lambda x: x["neutral"] <= 0.5 * x["plain"],
    )
    out["side_direct_author_names_y_ge_0.5"] = v(rt("author", "y"), lambda x: x >= 0.5)
    return out


def dose_section(
    rows: dict[str, list[dict]],
    classes: dict[tuple, dict[str, str]],
    forms: dict[str, dict[str, set[str]]],
    lp: int | None,
    n_boot: int = N_BOOT,
) -> dict | str:
    """The ``dose`` section: per alpha, flip rate and lens item rates (alpha 1.0: draw s0 of
    patched_author only, so every alpha has one lens sample per cell); then ``dose_pooled``."""
    if lp is None:
        return "absent"
    flips: dict[float, Bool] = {}
    ny: dict[float, Bool] = {}
    nb: dict[float, Bool] = {}
    per: dict = {}
    for a in DOSE_ALPHAS:
        key = ("patched", "author", lp, None) if a == 1.0 else ("dose", "author", lp, a)
        arm = dose_arm(a)
        lens = [r for r in rows.get(arm, []) if a != 1.0 or r["_draw"] == "s0"]
        if key not in classes or not lens:
            per[str(a)] = {
                "absent": [
                    x for x, ok in (("answers", key in classes), (arm, bool(lens))) if not ok
                ]
            }
            continue
        flips[a] = {i: v == "flip" for i, v in classes[key].items()}
        ny[a], nb[a] = item_any(lens, forms, "y"), item_any(lens, forms, "bridge")
        per[str(a)] = {
            "arm": arm,
            "flip": rate(flips[a]),
            "names_y": rate(ny[a]),
            "names_bridge": rate(nb[a]),
            "n_cells": len(lens),
        }
    if not flips:
        return "absent"
    return {"per_alpha": per, "pooled": dose_pooled(dose_units(flips, ny, nb), n_boot)}


# --------------------------------------------------------------------------- report


def summarize(
    results: Path = RESULTS,
    items_path: Path = I.ITEMS_PATH,
    p03_readouts: Path = P03_READOUTS,
    n_boot: int = N_BOOT,
) -> dict:
    """Every section of ``results/summary.json`` (pure apart from reading the inputs)."""
    results = Path(results)
    items = I.load(items_path)
    by_id = {it.id: it for it in items}
    forms = item_forms(items)
    s: dict = {
        "n_items": len(items),
        "bootstrap": f"p03.core.paired_bootstrap, {n_boot} "
        f"resamples of items, random.Random({BOOT_SEED})",
        "form_overlaps": form_overlaps(forms),
    }
    arm_files = discover_arms(results / "olens")
    rows = {a: load_arm(p) for a, p in arm_files.items()}
    s["arms"] = {a: arm_summary(rows[a], forms, len(arm_files[a])) for a in rows}
    s["arms_absent"] = [a for a in ARM_ORDER if a not in rows]
    m = {a: {w: item_any(r, forms, w) for w in ("y", "bridge")} for a, r in rows.items()}

    def pd(a, b, w):
        return paired(m[a][w], m[b][w], n_boot) if a in m and b in m else "absent"

    sweep = load_json(results / "sweep.json")
    s["sweep"] = sweep or "absent"
    lp = (sweep or {}).get("chosen_lp")
    classes = answer_classes(C.read_jsonl(results / "answers.jsonl"), by_id)
    s["manipulation"] = manipulation(classes) if classes else "absent"
    s["max_author_flip"] = max_author_flip(sweep, classes)
    fa, fm = flips_at(classes, "author", lp), flips_at(classes, "mention", lp)

    s["primary"] = pd("patched_author", "patched_mention", "y")
    sec: dict = {
        "bridge_patched_author_minus_plain": pd("patched_author", "plain", "bridge"),
        "bridge_neutral_minus_plain": pd("neutral", "plain", "bridge"),
    }
    if fa is not None and "patched_author" in m and "plain" in m:
        f_ids = [i for i, v in fa.items() if v]
        sec["flipped_only"] = {
            "n": len(f_ids),
            **{
                a: {f"names_{w}": rate(m[a][w], f_ids) for w in ("y", "bridge")}
                for a in ("patched_author", "plain")
            },
        }
    else:
        sec["flipped_only"] = "absent"
    for a in ("author", "mention"):
        sec[f"direct_{a}_names_y"] = rate(m[a]["y"]) if a in m else "absent"
    s["secondary"] = sec

    fc: dict = {}
    for arm, fl in (("patched_author", fa), ("patched_mention", fm)):
        if fl is None or arm not in m:
            fc[arm] = "absent"
            continue
        fc[arm] = {
            "names_y_flipped_minus_not": conditional(m[arm]["y"], fl, n_boot),
            "names_bridge_not_flipped": rate(m[arm]["bridge"], [i for i, v in fl.items() if not v]),
        }
    s["flip_conditional"] = fc
    # exploratory (log.md 04:10): within a direct-premise arm Y is in the text for every item,
    # so a names-Y gap between items whose answer flipped and items whose answer stayed is not
    # explained by the text alone
    dfc: dict = {}
    for p in ("author", "mention"):
        c = classes.get(("direct", p, None, None))
        if c is None or p not in m:
            dfc[p] = "absent"
            continue
        fl = {i: v == "flip" for i, v in c.items()}
        dfc[p] = {
            "names_y_flipped_minus_not": conditional(m[p]["y"], fl, n_boot),
            "names_bridge_flipped_minus_not": conditional(m[p]["bridge"], fl, n_boot),
        }
    s["direct_flip_conditional"] = dfc

    s["logit_lens"] = logit_lens_section(results / "logit_lens", items)
    s["dose"] = dose_section(rows, classes, forms, lp, n_boot)

    smoke = C.read_jsonl(results / "olens" / "smoke_shuffled.jsonl")
    s["shuffled_smoke"] = shuffled_smoke(smoke, forms) if smoke else "absent"
    s["prompt_only"] = prompt_only(
        C.read_jsonl(results / "prompt_only.jsonl"), forms, m.get("plain", {}).get("bridge")
    )
    s["selfreport"] = selfreport_section(
        C.read_jsonl(results / "selfreport" / "selfreport.jsonl"), forms, m, n_boot
    )
    s["stay_retention"] = stay_retention(
        classes,
        m,
        selfreport_mats(C.read_jsonl(results / "selfreport" / "selfreport.jsonl"), forms),
        lp,
        n_boot,
    )
    s["p03_rescore"] = p03_rescore(items, p03_readouts)
    s["predictions"] = verdicts(s)
    return s


def _f(x) -> str:
    if isinstance(x, dict) and "rate" in x:
        return f"{x['rate']:.2f} [{x['lo']:.2f}, {x['hi']:.2f}] ({x['k']}/{x['n']})"
    if isinstance(x, dict) and "diff" in x:
        return (
            f"{x['diff']:+.2f} [{x['lo']:+.2f}, {x['hi']:+.2f}] (n={x.get('n', x.get('n_units'))})"
        )
    return str(x)


def table(s: dict) -> str:
    lines = [f"{'arm':<16} {'names Y':<28} {'names bridge':<28} cells  empty  length"]
    for a, x in s["arms"].items():
        lines.append(
            f"{a:<16} {_f(x['names_y']):<28} {_f(x['names_bridge']):<28} "
            f"{x['n_cells']:<6} {x['frac_unparseable']:.3f}  {x['frac_length']:.3f}"
        )
    if s["arms_absent"]:
        lines.append(f"absent arms: {', '.join(s['arms_absent'])}")
    lines.append(f"primary (names Y, patched_author - patched_mention): {_f(s['primary'])}")
    for k, v in s["secondary"].items():
        if k != "flipped_only":
            lines.append(f"{k}: {_f(v)}")
    if isinstance(s.get("dose"), dict):
        d = s["dose"]["pooled"]
        lines.append(
            f"dose: names Y | flipped {d['names_y_given_flipped']:.2f} vs | not flipped "
            f"{d['names_y_given_not_flipped']:.2f}, diff {_f(d)}; units {d['n_units']}"
        )
    sm = s["shuffled_smoke"]
    if isinstance(sm, dict):
        lines.append(
            f"shuffled smoke: {sm['neither']}/{sm['n']} name neither "
            f"({'pass' if sm['pass'] else 'FAIL'})"
        )
    for name, sec in s["p03_rescore"].items():
        for k, v in sec.items() if isinstance(sec, dict) else ():
            lines.append(
                f"P03 {name} {k}: cleaned {_f(v['cleaned'])}, "
                f"original {_f(v['original'])}, lost {v['lost']}"
            )
    for k, v in s["predictions"].items():
        lines.append(f"prediction {k}: {v['verdict']}")
    if s["form_overlaps"]:
        lines.append(f"Y/bridge form overlaps: {s['form_overlaps']}")
    return "\n".join(lines)


def jsonable(x):
    """NaN -> None (strict JSON), recursively."""
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def report(results: Path = RESULTS, **kw) -> dict:
    s = summarize(results, **kw)
    out = Path(results) / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jsonable(s), indent=1, allow_nan=False) + "\n")
    print(table(s))
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("report",))
    ap.add_argument("--results", type=Path, default=RESULTS)
    a = ap.parse_args(argv)
    report(a.results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
