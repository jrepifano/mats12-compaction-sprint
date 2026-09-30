"""Gate and sample analysis for I1a+ (prereg v0.4 §4.5, §6.1, §6.2; implementation plan §0 and
§2 U6).

Per-item outcomes are recovered from the benchmark's own ``results.json`` ``rows`` (wsbench @
``contracts.WSBENCH_COMMIT``):

* **pass-rate families** (multihop via ``wsbench/basic/family.py``, basic_readout_mt via
  ``wsbench/multitoken/family.py``): one row per in-scope item, ``{"id", "pass": True | False |
  None, ...}`` (``wsbench/family.py`` ``tri_state``). The benchmark's value is the mean over
  decided rows (``pass is not None``; ``family.pass_rate_result``), so an item's outcome is
  1.0 / 0.0 and undecided items are dropped.
* **hallucination** (``wsbench/evals/hallucination/judge.py`` L418-452, ``score.py``): one row per
  non-empty cell, ``{"id", "layer", "pos", "verdict": None | [{"class", ...}, ...]}``. Per item
  we sum, as ``score._counts`` does per cell: ``readouts`` = verdicts whose class is not
  ``unjudged``; ``specific`` = class in {hallucinated, off_topic, consistent}; ``hallucinated``
  = class ``hallucinated``. The rate is sum(hallucinated) / sum(specific) and ``assert_share`` is
  sum(specific) / sum(readouts). Items are keyed in first-row order, exactly like
  ``score.bootstrap_rate_ci``'s ``per_item``, so our bootstrap reproduces the benchmark's ``ci95``
  when run with the same draws.

The bootstrap follows ``score.bootstrap_rate_ci`` for every family: ``random.Random(seed)``,
``BOOT_N`` draws, each resampling items with ``rng.randrange``; draws with a zero denominator are
skipped; the interval is ``draws[int(0.025 (n-1))], draws[int(0.975 (n-1))]``. (The benchmark's
pass-rate CI uses 1,000 ``rng.choices`` draws; the prereg fixes 2,000 draws, seed 0, for every
cell, so pass-rate cells use the hallucination scorer's recipe.)
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

from emx.i1a import contracts as C

EPS = 1e-9  # float slack on every threshold comparison (0.99 - 0.89 must count as 0.10)
SPECIFIC_CLASSES = frozenset({"hallucinated", "off_topic", "consistent"})  # wsbench judge.py
PassOutcomes = dict[str, float]
HalluCounts = dict[str, tuple[int, int]]  # id -> (hallucinated, specific)

# --- per-item outcomes ---------------------------------------------------------------------------


def load_results(src: Path | str | Mapping[str, Any]) -> dict[str, Any]:
    """A results.json dict from a dict, a results.json path or its directory."""
    if isinstance(src, Mapping):
        return dict(src)
    p = Path(src)
    if p.is_dir():
        p = p / "results.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _is_hallucination(res: Mapping[str, Any]) -> bool:
    return res["numbers"]["metric"] == "hallucination_rate"


def hallu_item_counts(
    results: Path | str | Mapping[str, Any], *, ids: Iterable[str] | None = None
) -> dict[str, dict[str, int]]:
    """Per item: {"hallucinated", "specific", "readouts"} summed over its judged cells, in
    first-row order; restricted to ``ids`` when given."""
    res = load_results(results)
    keep = set(ids) if ids is not None else None
    out: dict[str, dict[str, int]] = {}
    for row in res.get("rows") or []:
        iid = row["id"]
        if keep is not None and iid not in keep:
            continue
        c = out.setdefault(iid, {"hallucinated": 0, "specific": 0, "readouts": 0})
        for v in row.get("verdict") or []:
            cls = v.get("class")
            if cls == "unjudged":
                continue
            c["readouts"] += 1
            c["specific"] += cls in SPECIFIC_CLASSES
            c["hallucinated"] += cls == "hallucinated"
    return out


def item_outcomes(
    results: Path | str | Mapping[str, Any], *, ids: Iterable[str] | None = None
) -> PassOutcomes | HalluCounts:
    """Pass-rate families: ``{id: 1.0 | 0.0}`` over decided items. Hallucination: ``{id:
    (hallucinated, specific)}``. ``ids`` restricts the items (e.g. the 75-conversation subset)."""
    res = load_results(results)
    if _is_hallucination(res):
        return {
            i: (c["hallucinated"], c["specific"])
            for i, c in hallu_item_counts(res, ids=ids).items()
        }
    keep = set(ids) if ids is not None else None
    return {
        r["id"]: 1.0 if r["pass"] else 0.0
        for r in res.get("rows") or []
        if r.get("pass") is not None and (keep is None or r["id"] in keep)
    }


def _is_ratio(outcomes: Mapping[str, Any]) -> bool:
    return any(isinstance(v, tuple | list) for v in outcomes.values())


def point_value(outcomes: Mapping[str, Any]) -> float | None:
    """Mean pass rate, or the ratio of summed counts; None when undefined."""
    vals = list(outcomes.values())
    if not vals:
        return None
    if _is_ratio(outcomes):
        s = sum(b for _, b in vals)
        return sum(a for a, _ in vals) / s if s else None
    return sum(vals) / len(vals)


def _percentile_ci(draws: list[float]) -> tuple[float, float] | None:
    if not draws:
        return None
    draws.sort()
    return draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]


def _resample(
    n: int, stat: Callable[[list[int]], float | None], n_boot: int, seed: int
) -> list[float]:
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        v = stat([rng.randrange(n) for _ in range(n)])
        if v is not None:
            draws.append(v)
    return draws


def bootstrap_ci(
    outcomes: Mapping[str, Any], *, n_boot: int = C.BOOT_N, seed: int = C.BOOT_SEED
) -> tuple[float, float] | None:
    """Item-bootstrap 95% percentile interval of the mean (pass rate) or of the ratio of summed
    counts (hallucination), resampling items in the mapping's order."""
    vals = list(outcomes.values())
    if not vals:
        return None
    if _is_ratio(outcomes):

        def stat(pick: list[int]) -> float | None:
            h = sum(vals[i][0] for i in pick)
            s = sum(vals[i][1] for i in pick)
            return h / s if s else None
    else:

        def stat(pick: list[int]) -> float | None:
            return sum(vals[i] for i in pick) / len(pick)

    return _percentile_ci(_resample(len(vals), stat, n_boot, seed))


# --- gate ----------------------------------------------------------------------------------------


def cell_status(value: float, lo: float, hi: float, published: float) -> str:
    """imprecise / consistent / discrepant / borderline (plan §0; contracts.CellStatus)."""
    half = (hi - lo) / 2
    d = abs(value - published)
    if half > C.IMPRECISE_HALF_WIDTH + EPS:
        return "imprecise"
    if d <= C.GATE_MARGIN + EPS:
        return "consistent"
    if d - half > C.GATE_MARGIN + EPS:
        return "discrepant"
    return "borderline"


def ordering_pairs() -> list[tuple[str, str, str, float]]:
    """(family, arm_a, arm_b, published a - b) for every gate pair with |gap| >= 0.10."""
    out = []
    for fam in C.FAMILIES:
        for a, b in combinations(C.GATE_ARMS, 2):
            gap = C.PUBLISHED[(fam, a)] - C.PUBLISHED[(fam, b)]
            if abs(gap) >= C.ORDERING_MIN_PUBLISHED_GAP - EPS:
                out.append((fam, a, b, gap))
    assert len(out) == C.N_ORDERING_CHECKS, out
    return out


def ordering_checks(values: Mapping[tuple[str, str], float | None]) -> list[dict]:
    """Our signed gap must match the published sign and be at least 0.05 in magnitude. A pair
    with a missing value is reported with ``holds=None``."""
    out = []
    for fam, a, b, gap in ordering_pairs():
        va, vb = values.get((fam, a)), values.get((fam, b))
        ours = None if va is None or vb is None else va - vb
        holds = None
        if ours is not None:
            holds = (ours > 0) == (gap > 0) and abs(ours) >= C.ORDERING_MIN_OUR_GAP - EPS
        out.append(
            {"family": fam, "a": a, "b": b, "published_gap": gap, "our_gap": ours, "holds": holds}
        )
    return out


def gate_misses(
    statuses: Mapping[tuple[str, str], str], ordering: Sequence[Mapping[str, Any]]
) -> set[tuple[str, str]]:
    """Cells that are not consistent, plus both cells of every failed ordering check."""
    miss = {k for k, s in statuses.items() if s != "consistent"}
    for o in ordering:
        if o["holds"] is False:
            miss |= {(o["family"], o["a"]), (o["family"], o["b"])}
    return miss


def gate_scenario(
    statuses: Mapping[tuple[str, str], str],
    ordering: Sequence[Mapping[str, Any]],
    *,
    checks_passed: Mapping[str, bool | None],
    shuffled_pass_rate: float | None,
    scope_curtailed: bool,
    incomplete_cells: Sequence[str] = (),
) -> str:
    """The first matching row of prereg v0.4 §6.1, in ``contracts.GateScenario`` order (after
    gpt-6-sol round 3 a decisive failure outranks imprecision): not_readable, scope_curtailed,
    does_not_reproduce (>= 2 discrepant, or >= 2 ordering failures, or no cell imprecise and
    <= 5 of 9 consistent), imprecise, reproduces, multihop_only, judge_only, one_reader,
    inconclusive. A required check that is False or None (could not run), a missing /
    > 0.10 shuffled pass rate, or (unless scope-curtailed) any missing or incomplete gate cell
    in ``incomplete_cells`` is ``not_readable``."""
    if (
        any(p is not True for p in checks_passed.values())
        or shuffled_pass_rate is None
        or shuffled_pass_rate > C.CHECK_THRESHOLDS["shuffled_pass_max"] + EPS
    ):
        return "not_readable"
    if scope_curtailed:
        return "scope_curtailed"
    if incomplete_cells:  # a missing or incomplete gate cell is never a status (review U6-r1)
        return "not_readable"
    expected = {(f, a) for f in C.FAMILIES for a in C.GATE_ARMS}
    if set(statuses) != expected:
        raise ValueError(f"gate_scenario: cells missing: {sorted(expected - set(statuses))}")
    if len(ordering) != C.N_ORDERING_CHECKS or any(o["holds"] is None for o in ordering):
        raise ValueError("gate_scenario: every ordering check needs both values")
    vals = list(statuses.values())
    any_imprecise = "imprecise" in vals
    n_cons = vals.count("consistent")
    n_disc = vals.count("discrepant")
    n_fail = sum(o["holds"] is False for o in ordering)
    if n_disc >= 2 or n_fail >= 2 or (not any_imprecise and n_cons <= 5):
        return "does_not_reproduce"
    if any_imprecise:
        return "imprecise"
    if n_cons >= 8 and n_disc == 0 and n_fail == 0:
        return "reproduces"
    misses = gate_misses(statuses, ordering)
    if misses and all(f == "multihop" for f, _ in misses):
        return "multihop_only"
    regex_ok = all(s == "consistent" for (f, _), s in statuses.items() if C.SCORER[f] == "regex")
    if misses and regex_ok and all(C.SCORER[f] == "gemini" for f, _ in misses):
        return "judge_only"
    if misses and len({a for _, a in misses}) == 1:
        return "one_reader"
    return "inconclusive"


# --- samples -------------------------------------------------------------------------------------


def paired_diff(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    n_boot: int = C.BOOT_N,
    seed: int = C.BOOT_SEED,
    require_same: bool = True,
) -> dict[str, Any]:
    """a − b over the items (sorted ids), with a paired item-bootstrap 95% interval: each draw
    resamples items once and recomputes both arms' statistic on them. By default both arms must
    carry exactly the same items (``SiteMismatch`` lists the difference otherwise);
    ``require_same=False`` uses the intersection."""
    if require_same:
        assert_same_keys(set(a), set(b), "paired_diff items")
    ids = sorted(set(a) & set(b))
    if not ids:
        return {"n_items": 0, "a": None, "b": None, "diff": None, "lo": None, "hi": None}
    va, vb = [a[i] for i in ids], [b[i] for i in ids]
    ratio = _is_ratio(a) or _is_ratio(b)

    def stat_of(v: list, pick: Sequence[int]) -> float | None:
        if ratio:
            s = sum(v[i][1] for i in pick)
            return sum(v[i][0] for i in pick) / s if s else None
        return sum(v[i] for i in pick) / len(pick)

    def stat(pick: list[int]) -> float | None:
        x, y = stat_of(va, pick), stat_of(vb, pick)
        return None if x is None or y is None else x - y

    full = list(range(len(ids)))
    pa, pb = stat_of(va, full), stat_of(vb, full)
    ci = _percentile_ci(_resample(len(ids), stat, n_boot, seed))
    return {
        "n_items": len(ids),
        "a": pa,
        "b": pb,
        "diff": None if pa is None or pb is None else pa - pb,
        "lo": ci[0] if ci else None,
        "hi": ci[1] if ci else None,
    }


class SiteMismatch(ValueError):
    """Two arms compared on different sites or items (review U6-r1 finding 4)."""


def assert_same_keys(a: set, b: set, what: str) -> None:
    if a != b:
        only_a, only_b = sorted(a - b, key=str), sorted(b - a, key=str)
        raise SiteMismatch(
            f"{what}: {len(only_a)} only in the rule arm {only_a[:5]}, "
            f"{len(only_b)} only in the reference arm {only_b[:5]}"
        )


def readout_sites(
    arm: str, family: str, *, ids: Iterable[str] | None = None, nonempty: bool = False
) -> set[tuple[str, int, int]] | None:
    """(id, layer, pos) of an arm's readouts rows (optionally only non-empty ones), or None when
    the file is absent."""
    path = C.readouts_path(arm, family)
    if not path.exists():
        return None
    keep = set(ids) if ids is not None else None
    out = set()
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if keep is not None and r["id"] not in keep:
            continue
        if nonempty and not _readout_text(r).strip():
            continue
        out.add((r["id"], int(r["layer"]), int(r["pos"])))
    return out


def assert_hallu_judged(
    results: Mapping[str, Any], arm: str, *, ids: Iterable[str] | None = None
) -> None:
    """Every non-empty readout site of the arm (restricted to ``ids``) has a judged row, and no
    row lacks a verdict; empty readouts are never judged by the benchmark."""
    want = readout_sites(arm, "hallucination", ids=ids, nonempty=True)
    if want is None:
        raise SiteMismatch(f"{arm}/hallucination: no readouts file")
    keep = set(ids) if ids is not None else None
    rows = [r for r in results.get("rows") or [] if keep is None or r["id"] in keep]
    got = {(r["id"], int(r["layer"]), int(r["pos"])) for r in rows}
    assert_same_keys(want, got, f"{arm}/hallucination judged rows vs non-empty readouts")
    unjudged = [(r["id"], r["layer"], r["pos"]) for r in rows if r.get("verdict") is None]
    if unjudged:
        raise SiteMismatch(f"{arm}/hallucination: {len(unjudged)} unjudged cells {unjudged[:5]}")


def ceiling(per_sample_item_passes: Sequence[Mapping[str, float]]) -> float | None:
    """Share of items where any sample passes. An item with no pass counts as a fail only if
    every sample decided it; otherwise it is dropped (undecided)."""
    ids = set().union(*per_sample_item_passes) if per_sample_item_passes else set()
    vals = []
    for i in ids:
        got = [s.get(i) for s in per_sample_item_passes]
        if any(v == 1.0 for v in got):
            vals.append(1.0)
        elif all(v is not None for v in got):
            vals.append(0.0)
    return sum(vals) / len(vals) if vals else None


def sampling_spread(per_sample_item_passes: Sequence[Mapping[str, float]]) -> dict[str, Any]:
    """Pass rate of each sample, and their min / max / mean (the sampling-noise diagnostic)."""
    rates = [point_value(s) for s in per_sample_item_passes]
    ok = [r for r in rates if r is not None]
    return {
        "rates": rates,
        "min": min(ok) if ok else None,
        "max": max(ok) if ok else None,
        "mean": sum(ok) / len(ok) if ok else None,
    }


def _readout_text(row: Mapping[str, Any]) -> str:
    """A contract row's text as wsbench ``family.cell_text`` builds it (plain side)."""
    if row.get("samples") is not None:
        return "\n".join(s for s in row["samples"] if s.strip())
    return " | ".join(row.get("tokens") or [])


def coverage(
    family: str,
    arm: str,
    *,
    ids: Iterable[str] | None = None,
    results: Path | str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Coverage of an arm on (optionally) a subset of items: the empty-readout rate and the mean
    bullets per readout from the readouts file (empty cells count 0 bullets), and ``assert_share``
    (sum specific / sum readouts) from the judged results rows when given."""
    from emx.i1a.selection import bullets_per_readout

    keep = set(ids) if ids is not None else None
    path = C.readouts_path(arm, family)
    out: dict[str, Any] = {
        "n_cells": None,
        "empty_rate": None,
        "bullets_mean": None,
        "assert_share": None,
    }
    if path.exists():
        rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        rows = [r for r in rows if keep is None or r["id"] in keep]
        texts = [_readout_text(r) for r in rows]
        if texts:
            out["n_cells"] = len(texts)
            out["empty_rate"] = sum(not t.strip() for t in texts) / len(texts)
            out["bullets_mean"] = sum(bullets_per_readout(t) for t in texts) / len(texts)
    if results is not None:
        cnt = hallu_item_counts(results, ids=keep)
        n_read = sum(c["readouts"] for c in cnt.values())
        n_spec = sum(c["specific"] for c in cnt.values())
        out["assert_share"] = n_spec / n_read if n_read else None
    return out


def coverage_guards(random_cov: Mapping[str, Any], rule_cov: Mapping[str, Any]) -> dict[str, Any]:
    """The three hallucination coverage guards (rule vs random, same sites). ``ok`` is None when
    any input is missing."""
    keys = ("empty_rate", "bullets_mean", "assert_share")
    if any(random_cov.get(k) is None or rule_cov.get(k) is None for k in keys):
        return {"empty": None, "bullets": None, "assert_share": None, "ok": None}
    empty_ok = rule_cov["empty_rate"] - random_cov["empty_rate"] <= C.EMPTY_RATE_MAX_RISE + EPS
    rb, nb = random_cov["bullets_mean"], rule_cov["bullets_mean"]
    bullets_ok = True if rb == 0 else (rb - nb) / rb <= C.BULLETS_MAX_FALL_FRAC + EPS
    assert_ok = (
        random_cov["assert_share"] - rule_cov["assert_share"] <= C.ASSERT_SHARE_MAX_FALL + EPS
    )
    return {
        "empty": empty_ok,
        "bullets": bullets_ok,
        "assert_share": assert_ok,
        "ok": empty_ok and bullets_ok and assert_ok,
    }


def rule_helps(
    recall: Mapping[str, Any] | None,
    hallu: Mapping[str, Any] | None,
    guards_ok: bool | None,
) -> dict[str, Any]:
    """Prereg v0.4 §4.5 / §6.2 for one rule. ``recall`` and ``hallu`` are ``paired_diff``
    outputs (rule − random); either may be None (not measured). Helps = helps on one metric
    without worsening the other by >= 5 points. ``assessable`` needs both metrics and the
    coverage guards; an unassessable rule has ``helps=None`` (never a silent yes or no)."""
    h = C.HELP_MIN_POINTS

    def ok(d: Mapping[str, Any] | None) -> bool:
        return d is not None and d.get("diff") is not None and d.get("lo") is not None

    helps_recall = ok(recall) and recall["diff"] >= h - EPS and recall["lo"] > 0  # type: ignore[index]
    helps_hallu = (
        ok(hallu) and hallu["diff"] <= -h + EPS and hallu["hi"] < 0 and guards_ok is True  # type: ignore[index]
    )
    recall_worse = ok(recall) and recall["diff"] <= -h + EPS  # type: ignore[index]
    hallu_worse = ok(hallu) and hallu["diff"] >= h - EPS  # type: ignore[index]
    assessable = ok(recall) and ok(hallu) and guards_ok is not None
    helps = (helps_recall and not hallu_worse) or (helps_hallu and not recall_worse)
    return {
        "helps_recall": helps_recall,
        "lowers_measured_rate": helps_hallu,  # hallucination: rate down, proxy checks ok
        "recall_worse": recall_worse,
        "hallucination_worse": hallu_worse,
        "assessable": assessable,
        "helps": helps if assessable else None,
    }


def sample_scenario(
    rules: Mapping[str, Mapping[str, Any]],
    ceiling_minus_random: float | None,
    *,
    bestfve_excluded: bool,
) -> str:
    """First match of prereg v0.4 §6.2, or ``unassessed`` until every required comparison
    exists: the ceiling, and both metrics plus the coverage guards for every available rule.
    ``rules`` maps rule name -> ``rule_helps`` output (None = not measured). Best FVE is
    unavailable (dropped, not "did not help") when its FVE checks failed."""
    live = {r: v for r, v in rules.items() if not (r == "bestfve" and bestfve_excluded)}
    if (
        ceiling_minus_random is None
        or not live
        or any(v is None or not v.get("assessable") for v in live.values())
    ):
        return "unassessed"
    if any(v["helps"] for v in live.values()):
        return "selection_helps"
    if ceiling_minus_random >= C.CEILING_ROOM_POINTS - EPS:
        return "room_no_selector"
    if ceiling_minus_random < C.CEILING_NO_ROOM_POINTS - EPS:
        return "no_room"
    return "mixed"


# --- the summary (scripts/i1a_analyze.py) ---------------------------------------------------------

CONTINUING_GATE = frozenset({"reproduces", "multihop_only", "judge_only"})
CHECK_KINDS = frozenset({"required", "fve", "shuffled", "info"})


def classify_check(name: str, overrides: Mapping[str, str] | None = None) -> str:
    """The check's kind from the frozen registry ``contracts.CHECK_KIND`` (required / fve /
    shuffled), unless an explicit override names it; a name in neither is ``info``."""
    if overrides and name in overrides:
        return overrides[name]
    return C.CHECK_KIND.get(name, "info")


def scope_curtailed_path() -> Path:
    return C.JUDGED_DIR / "SCOPE_CURTAILED.json"


def read_scope_curtailed() -> dict[str, Any] | None:
    """The judge driver's SCOPE_CURTAILED.json (its presence means a gate cell was cut), or
    None when absent."""
    p = scope_curtailed_path()
    if not p.exists():
        return None
    try:
        return {"path": str(p), "content": json.loads(p.read_text(encoding="utf-8"))}
    except json.JSONDecodeError:
        return {"path": str(p), "content": None}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record(inputs: dict[str, str] | None, path: Path) -> None:
    """Provenance: repo-relative (else absolute) path -> sha256 of every input read."""
    if inputs is None or not path.exists():
        return
    try:
        key = str(path.resolve().relative_to(C.REPO_ROOT))
    except ValueError:
        key = str(path)
    inputs[key] = sha256_file(path)


def load_checks(
    checks_dir: Path | None = None, inputs: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    d = Path(checks_dir or C.CHECKS_DIR)
    out = {}
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            _record(inputs, p)
            r = json.loads(p.read_text(encoding="utf-8"))
            out[r.get("name", p.stem)] = r
    return out


def _judged(arm: str, family: str, inputs: dict[str, str] | None = None) -> dict[str, Any] | None:
    p = C.judged_dir(arm, family) / "results.json"
    if not p.exists():
        return None
    _record(inputs, p)
    return load_results(p)


def _cell_summary(res: Mapping[str, Any]) -> dict[str, Any]:
    oc = item_outcomes(res)
    value = point_value(oc)
    ci = bootstrap_ci(oc)
    bench = res["numbers"]
    return {
        "value": value,
        "ci95": list(ci) if ci else None,
        "n_items": len(oc),
        "bench_value": bench.get("value"),
        "bench_ci95": bench.get("ci95"),
        "complete": res.get("complete"),
        "value_matches_bench": (
            None
            if value is None or bench.get("value") is None
            else abs(value - bench["value"]) < 1e-9
        ),
    }


def build_summary(
    *,
    scope_curtailed: bool = False,
    check_overrides: Mapping[str, str] | None = None,
    subset_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Everything under ``C.JUDGED_DIR`` / ``C.CHECKS_DIR`` / readouts -> the summary dict.
    Missing inputs are listed under ``missing`` and leave their parts None; a missing file never
    raises. Comparisons on mismatched sites are listed under ``errors`` (the script exits 1)."""
    from emx.i1a.selection import bestfve_allowed, load_subset_ids

    missing: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    inputs: dict[str, str] = {}
    summary: dict[str, Any] = {
        "exp": C.EXP,
        "missing": missing,
        "warnings": warnings,
        "errors": errors,
        "inputs": inputs,
    }

    overrides = dict(check_overrides or {})
    bad = {n: k for n, k in overrides.items() if k not in CHECK_KINDS}
    if bad:
        raise ValueError(f"unknown check kinds {bad}; expected one of {sorted(CHECK_KINDS)}")
    summary["check_overrides"] = overrides
    checks = load_checks(inputs=inputs)
    names = sorted(set(C.CHECK_KIND) | set(checks) | set(overrides))
    kinds = {n: classify_check(n, overrides) for n in names}
    for n in checks:
        if n not in C.CHECK_KIND and n not in overrides:
            warnings.append(f"check {n!r} is not in contracts.CHECK_KIND: reported as info")
    # a registered check with no file counts as failed (passed=None)
    required = {n: checks.get(n, {}).get("passed") for n in names if kinds[n] == "required"}
    for n in names:
        if n not in checks and kinds[n] in ("required", "fve"):
            missing.append(str(C.check_path(n)))
    summary["checks"] = {
        n: {
            "kind": kinds[n],
            "present": n in checks,
            "passed": checks.get(n, {}).get("passed"),
            "value": checks.get(n, {}).get("value"),
        }
        for n in names
    }
    fve_ok, fve_reason = bestfve_allowed(overrides)
    summary["bestfve_excluded"] = not fve_ok
    summary["bestfve_reason"] = fve_reason

    reasons: list[str] = [
        f"required check {n}: passed={p}" for n, p in required.items() if p is not True
    ]
    scope_file = read_scope_curtailed()
    summary["scope_curtailed_source"] = {"file": scope_file, "cli_flag": scope_curtailed}
    scope_curtailed = scope_curtailed or scope_file is not None

    sh = _judged("olens_shuffled", "basic_readout_mt", inputs)
    shuffled = None
    if sh is None:
        missing.append(str(C.judged_dir("olens_shuffled", "basic_readout_mt")))
        reasons.append("olens_shuffled: not judged")
    elif not sh.get("complete"):
        reasons.append("olens_shuffled: results complete=false")
    else:
        shuffled = point_value(item_outcomes(sh))
        if shuffled is not None and shuffled > C.CHECK_THRESHOLDS["shuffled_pass_max"] + EPS:
            reasons.append(f"olens_shuffled: regex pass rate {shuffled:.3f} > 0.10")
    summary["shuffled_pass_rate"] = shuffled  # regex pass rate, judged/olens_shuffled/...
    for n in names:  # a written shuffled check that failed also makes the run unreadable
        if kinds[n] == "shuffled" and n in checks and checks[n].get("passed") is False:
            required = {**required, n: False}
            reasons.append(f"check {n}: passed=False")

    cells: dict[str, Any] = {}
    statuses: dict[tuple[str, str], str] = {}
    values: dict[tuple[str, str], float | None] = {}
    incomplete: list[str] = []
    for fam in C.FAMILIES:
        for arm in C.GATE_ARMS:
            key = f"{fam}/{arm}"
            res = _judged(arm, fam, inputs)
            pub = C.PUBLISHED[(fam, arm)]
            if res is None:
                missing.append(str(C.judged_dir(arm, fam)))
                incomplete.append(f"{key}: not judged")
                cells[key] = {"published": pub, "value": None, "status": None, "counted": False}
                continue
            cs = _cell_summary(res)
            cs["published"] = pub
            cs["status"] = None
            cs["counted"] = False  # a cell counts only when complete and valued
            if not res.get("complete"):
                incomplete.append(f"{key}: results complete=false")
            elif cs["value"] is None or cs["ci95"] is None:
                incomplete.append(f"{key}: no decided items")
            else:
                cs["status"] = cell_status(cs["value"], *cs["ci95"], pub)
                cs["counted"] = True
                statuses[(fam, arm)] = cs["status"]
                values[(fam, arm)] = cs["value"]
            if cs["value_matches_bench"] is False:
                warnings.append(f"{key}: recomputed value differs from the benchmark's")
            cells[key] = cs
    summary["cells"] = cells
    reasons += incomplete
    ordering = ordering_checks(values)
    summary["ordering"] = ordering
    if not required:  # only if overrides demoted every required check: never a silent pass
        reasons.append("no required checks")
        required = {"<no required checks>": None}
    scen = gate_scenario(
        statuses,
        ordering,
        checks_passed=required,
        shuffled_pass_rate=shuffled,
        scope_curtailed=scope_curtailed,
        incomplete_cells=incomplete,
    )
    summary["gate"] = {
        "scenario": scen,
        "continuing": scen in CONTINUING_GATE,
        "reasons": reasons if scen in ("not_readable", "scope_curtailed") else [],
        "n_consistent": sum(s == "consistent" for s in statuses.values()),
        "n_discrepant": sum(s == "discrepant" for s in statuses.values()),
        "n_ordering_failed": sum(o["holds"] is False for o in ordering),
        "misses": sorted(f"{f}/{a}" for f, a in gate_misses(statuses, ordering)),
        "scope_curtailed": scope_curtailed,
        "incomplete_cells": incomplete,
    }
    if subset_ids is None:
        if C.SUBSET_PATH.exists():
            _record(inputs, C.SUBSET_PATH)
            subset_ids = load_subset_ids()
        else:
            missing.append(str(C.SUBSET_PATH))
            subset_ids = set()
    for fam in C.SAMPLE_FAMILIES:
        _record(inputs, C.samples_path(fam))
        _record(inputs, C.fve_path(fam))
    summary["samples"] = _sample_summary(
        subset_ids, fve_ok, fve_reason, scen, missing, warnings, errors, inputs
    )
    return summary


def _mt_outcomes(
    arm: str, ref_sites: set | None, missing: list[str], inputs: dict[str, str]
) -> PassOutcomes:
    """Complete recall outcomes of an arm whose readout sites equal the reference's."""
    fam = "basic_readout_mt"
    res = _judged(arm, fam, inputs)
    if res is None:
        missing.append(str(C.judged_dir(arm, fam)))
        raise LookupError(f"{arm}/{fam}: not judged")
    if not res.get("complete"):
        raise SiteMismatch(f"{arm}/{fam}: results complete=false")
    _record(inputs, C.readouts_path(arm, fam))
    sites = readout_sites(arm, fam)
    if sites is None or ref_sites is None:
        raise SiteMismatch(f"{arm}/{fam}: readouts file missing (it or the random arm's)")
    assert_same_keys(sites, ref_sites, f"{arm}/{fam} readout sites vs olens_rl600")
    return item_outcomes(res)  # type: ignore[return-value]


def _hallu_outcomes(
    arm: str,
    subset_ids: set[str],
    ref_sites: set | None,
    missing: list[str],
    inputs: dict[str, str],
) -> tuple[HalluCounts, dict[str, Any]]:
    """Subset counts (every subset item with a readout; (0, 0) if none judged) and the results,
    after asserting identical sites and complete judgments."""
    res = _judged(arm, "hallucination", inputs)
    if res is None:
        missing.append(str(C.judged_dir(arm, "hallucination")))
        raise LookupError(f"{arm}/hallucination: not judged")
    _record(inputs, C.readouts_path(arm, "hallucination"))
    sites = readout_sites(arm, "hallucination", ids=subset_ids)
    if sites is None or ref_sites is None:
        raise SiteMismatch(f"{arm}/hallucination: readouts file missing (it or the random arm's)")
    assert_same_keys(sites, ref_sites, f"{arm}/hallucination subset sites vs olens_rl600")
    assert_hallu_judged(res, arm, ids=subset_ids)
    oc = item_outcomes(res, ids=subset_ids)
    items = sorted({i for i, _layer, _pos in sites})
    return {i: oc.get(i, (0, 0)) for i in items}, res  # type: ignore[misc]


def _sample_summary(
    subset_ids: set[str],
    fve_ok: bool,
    fve_reason: str,
    gate_scen: str,
    missing: list[str],
    warnings: list[str],
    errors: list[str],
    inputs: dict[str, str],
) -> dict[str, Any]:
    out: dict[str, Any] = {"bestfve_available": fve_ok, "bestfve_reason": fve_reason}
    fam = "basic_readout_mt"
    ref_mt = readout_sites("olens_rl600", fam)
    ref_h = readout_sites("olens_rl600", "hallucination", ids=subset_ids)
    _record(inputs, C.readouts_path("olens_rl600", fam))
    _record(inputs, C.readouts_path("olens_rl600", "hallucination"))

    def attempt(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except LookupError:
            return None
        except SiteMismatch as e:
            errors.append(str(e))
            return None

    # descriptive recall numbers: random, ceiling over the 10 samples, spread
    random_mt = attempt(lambda: _mt_outcomes("olens_rl600", ref_mt, missing, inputs))
    per_sample = [random_mt]
    for k in range(1, C.N_SAMPLES):
        per_sample.append(attempt(lambda k=k: _mt_outcomes(f"olens_s{k}", ref_mt, missing, inputs)))
    ceil = None
    if all(p is not None for p in per_sample):
        ok = attempt(
            lambda: [
                assert_same_keys(set(p), set(random_mt), f"olens_s{k} decided items")
                for k, p in enumerate(per_sample)
            ]
        )
        ceil = ceiling(per_sample) if ok is not None else None
    rnd = point_value(random_mt) if random_mt else None
    out["recall"] = {
        "random": rnd,
        "ceiling": ceil,
        "ceiling_minus_random": None if ceil is None or rnd is None else ceil - rnd,
        "spread": sampling_spread(per_sample) if all(p is not None for p in per_sample) else None,
    }
    random_h = attempt(lambda: _hallu_outcomes("olens_rl600", subset_ids, ref_h, missing, inputs))
    random_cov = (
        coverage("hallucination", "olens_rl600", ids=subset_ids, results=random_h[1])
        if random_h
        else None
    )
    out["hallucination"] = {
        "random": point_value(random_h[0]) if random_h else None,
        "random_coverage": random_cov,
        "subset_n_items": len(subset_ids),
    }
    rules: dict[str, Any] = {}
    for rule in ("bestfve", "consensus"):
        arm = f"sel_{rule}"
        if rule == "bestfve" and not fve_ok:
            rules[rule] = {"available": False, "reason": fve_reason}
            if C.readouts_path(arm, fam).exists() or C.judged_dir(arm, fam).exists():
                warnings.append(f"{arm}: files exist although best FVE is unavailable; ignored")
            continue
        r_mt = attempt(lambda arm=arm: _mt_outcomes(arm, ref_mt, missing, inputs))
        recall = (
            attempt(lambda r_mt=r_mt: paired_diff(r_mt, random_mt))
            if r_mt is not None and random_mt is not None
            else None
        )
        r_h = attempt(lambda arm=arm: _hallu_outcomes(arm, subset_ids, ref_h, missing, inputs))
        hallu = (
            attempt(lambda r_h=r_h: paired_diff(r_h[0], random_h[0]))
            if r_h is not None and random_h is not None
            else None
        )
        cov = coverage("hallucination", arm, ids=subset_ids, results=r_h[1]) if r_h else None
        guards = (
            coverage_guards(random_cov, cov)
            if cov is not None and random_cov is not None and hallu is not None
            else {"empty": None, "bullets": None, "assert_share": None, "ok": None}
        )
        rules[rule] = {
            "available": True,
            "recall_diff": recall,
            "hallucination_diff": hallu,
            "coverage": cov,
            "guards": guards,
            **rule_helps(recall, hallu, guards["ok"]),
        }
    out["rules"] = rules
    cmr = out["recall"]["ceiling_minus_random"]
    if gate_scen not in CONTINUING_GATE:
        out["scenario"] = "unassessed"
        out["scenario_reason"] = (
            f"gate outcome {gate_scen!r} is not continuing; sample numbers are descriptive only"
        )
    else:
        live = {r: v for r, v in rules.items() if v.get("available")}
        out["scenario"] = sample_scenario(live, cmr, bestfve_excluded=not fve_ok)
        out["scenario_reason"] = (
            "a required comparison is missing (ceiling, or both metrics and the coverage guards "
            "for every available rule)"
            if out["scenario"] == "unassessed"
            else ""
        )
    return out


# --- random raw packets --------------------------------------------------------------------------


def _verdict_lookup(res: Mapping[str, Any] | None) -> dict[Any, Any]:
    """(id, layer, pos) -> hallucination verdict classes, or id -> item pass (pass-rate rows)."""
    if res is None:
        return {}
    if _is_hallucination(res):
        return {
            (r["id"], r["layer"], r["pos"]): [v.get("class") for v in (r.get("verdict") or [])]
            for r in res.get("rows") or []
        }
    return {r["id"]: r.get("pass") for r in res.get("rows") or []}


def write_packets(n_per_arm: int = 5, *, seed: int = 0) -> list[Path]:
    """For every arm directory under READOUTS_DIR: ``n_per_arm`` cells per family drawn with a
    fresh ``random.Random(seed)`` per arm from the rows sorted by (id, layer, pos), never
    cherry-picked. One markdown file per arm under ``ANALYSIS_DIR/packets``. A pass-rate verdict
    is the item's (any-layer) pass, not the cell's."""
    out_dir = C.ANALYSIS_DIR / "packets"
    paths: list[Path] = []
    if not C.READOUTS_DIR.is_dir():
        return paths
    for arm_dir in sorted(p for p in C.READOUTS_DIR.iterdir() if p.is_dir()):
        arm = arm_dir.name
        rng = random.Random(seed)
        lines = [
            f"# Random raw readouts: {arm}",
            "",
            (
                f"`random.Random({seed})`, {n_per_arm} cells per family, drawn from rows sorted "
                "by (id, layer, pos). Not cherry-picked. Pass-rate verdicts are per item (any "
                "layer)."
            ),
            "",
        ]
        for fpath in sorted(arm_dir.glob("*.jsonl")):
            fam = fpath.stem
            rows = [json.loads(ln) for ln in fpath.read_text(encoding="utf-8").splitlines() if ln]
            rows.sort(key=lambda r: (r["id"], r["layer"], r["pos"]))
            pick = rng.sample(rows, min(n_per_arm, len(rows)))
            verdicts = _verdict_lookup(_judged(arm, fam))
            lines += [f"## {fam} ({len(rows)} rows)", ""]
            for r in pick:
                v = verdicts.get((r["id"], r["layer"], r["pos"]), verdicts.get(r["id"], "n/a"))
                lines += [
                    (
                        f"### {r['id']} · layer {r['layer']} · pos {r['pos']} · "
                        f"token {r.get('token', '?')!r}"
                    ),
                    "",
                    f"Judged: `{v}`",
                    "",
                    "```",
                    _readout_text(r) or "(empty)",
                    "```",
                    "",
                ]
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{arm}.md"
        p.write_text("\n".join(lines), encoding="utf-8")
        paths.append(p)
    return paths
