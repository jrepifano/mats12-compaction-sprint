"""Experiment 1 carry-over sensitivity (spec D7): the planned contrast under both carry-over rules.

    uv run python scripts/s1_carryover_sensitivity.py [--out results/sprint/S1/carryover_sensitivity.json]

The codex_local and antigravity replays carry user messages verbatim next to the summary. The
primary rule (the analysis) keeps only real user requests; P49's unfiltered rule keeps every
user-role message, scaffolding included (`s1_replay`, `kept_messages_unfiltered`). The judge's
grades don't depend on the rule: pass 1b and the item second look see only the summary. Only
availability does, and `s1_judge.part_b` already scored it both ways on every dropped or
misstated item (`availability_unfiltered`). So this needs no model calls: it reads the released
Part B rows through the seal (`seal.open_sealed("S1", ...)`), applies the frozen second-look
overlay as `s1_analysis.part_b_report` does, swaps in the unfiltered availability for the two
arms, and reruns `s1_analysis.paired_contrast` with the analysis's seed and resample count.

Output: the primary contrast (cc_prompt vs codex_local, user-given rules lost) under each rule,
each also with reloaded items counted as lost, the precision-corrected difference, D7's reading
rule, per-arm rates for the two affected arms, the exploratory pairs that involve them, and how
many user-given rules change availability. The filtered contrast is checked against
`results/sprint/S1/analysis.json` and the script refuses if it doesn't reproduce. Aggregates and
counts only, no session text.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from s1_analyze import _json_safe

from compaction import s1_analysis as A
from compaction import seal
from compaction.s1_types import SEAL_STAGE

JUDGE = "haiku-max"
UNFILTERED_ARMS = ("codex_local", "antigravity")  # the replays with a verbatim user carry-over
ANALYSIS = ROOT / "results" / "sprint" / "S1" / "analysis.json"
OUT = ROOT / "results" / "sprint" / "S1" / "carryover_sensitivity.json"


def log(msg: str) -> None:
    print(f"S1_CARRYOVER {msg}", flush=True)


def unfiltered_rows(rows: Iterable[Mapping], arms: Iterable[str] = UNFILTERED_ARMS) -> list[dict]:
    """Copies of `rows` with, for the given arms, `availability` replaced by
    `availability_unfiltered`. Refuses (ValueError) a row of those arms whose filtered
    availability was scored but whose unfiltered one wasn't: the swap would silently keep the
    filtered value."""
    arms = set(arms)
    out = []
    bad = 0
    for r in rows:
        if r.get("arm") not in arms:
            out.append(dict(r))
            continue
        if r.get("availability") is not None and r.get("availability_unfiltered") is None:
            bad += 1
        out.append(
            dict(
                r,
                availability=r.get("availability_unfiltered"),
                availability_how=r.get("availability_unfiltered_how"),
            )
        )
    if bad:
        raise ValueError(f"{bad} rows of {sorted(arms)} have no unfiltered availability")
    return out


def _excludes_zero(ci: Mapping) -> bool:
    return ci["lo"] > 0 or ci["hi"] < 0


def reading_rule(filtered: Mapping, unfiltered: Mapping) -> dict:
    """Spec D7: the two rules disagree when the point differences have opposite signs, or when
    one 95% interval excludes 0 and the other doesn't."""
    f, u = filtered["diff"], unfiltered["diff"]
    opposite = (f["point"] > 0) != (u["point"] > 0) and f["point"] != 0 and u["point"] != 0
    f_ex, u_ex = _excludes_zero(f), _excludes_zero(u)
    disagree = opposite or f_ex != u_ex
    return {
        "rule": "disagree if opposite signs, or one 95% interval excludes 0 and the other doesn't",
        "opposite_signs": opposite,
        "filtered_excludes_0": f_ex,
        "unfiltered_excludes_0": u_ex,
        "disagree": disagree,
        "headline": (
            "the cc_prompt vs codex_local difference depends on the carry-over rule; give both"
            if disagree
            else "the two carry-over rules agree"
        ),
    }


def transitions(rows: Iterable[Mapping], arms: Iterable[str] = UNFILTERED_ARMS) -> dict:
    """Per arm and source, (filtered, unfiltered) availability counts over usable user-given
    rules graded dropped or misstated: which rules the unfiltered carry-over makes available, or
    loses."""
    out: dict[str, dict] = {}
    for arm in arms:
        c = Counter(
            (r.get("source"), f"{r.get('availability')}->{r.get('availability_unfiltered')}")
            for r in rows
            if r.get("arm") == arm
            and A.usable_item(r)
            and A.is_user_rule(r)
            and r.get("grade") in A.LOST_GRADES
        )
        out[arm] = {}
        for (src, k), n in sorted(c.items()):
            out[arm].setdefault(src, {})[k] = n
    return out


def _arm(rates: list[dict], arm: str) -> dict | None:
    return next((r for r in rates if r["arm"] == arm), None)


def report(items: list[dict], *, precision: Mapping | None, n_boot: int, seed: int) -> dict:
    """The sensitivity from Part B rows that already carry the second look (one judge)."""
    unf = unfiltered_rows(items)
    a, b = A.PRIMARY_CONTRAST
    rules = {}
    for name, rows in (("filtered", items), ("unfiltered", unf)):
        prim = A.paired_contrast(
            rows, a, b, label="primary", precision=precision, n_boot=n_boot, seed=seed
        )
        prim["reloaded_as_lost"] = A.paired_contrast(
            A.reloaded_as_lost(rows), a, b, label="primary", n_boot=n_boot, seed=seed
        )
        rates = A.part_b_arm_rates(rows, precision=precision, n_boot=n_boot, seed=seed)
        rules[name] = {
            "primary": prim,
            "arms": {arm: _arm(rates, arm) for arm in UNFILTERED_ARMS},
        }
    arms = sorted({r["arm"] for r in items})
    pairs = [
        p
        for p in itertools.combinations(arms, 2)
        if set(p) != {a, b} and set(p) & set(UNFILTERED_ARMS)
    ]
    return {
        "metric": "user-given rules lost (dropped or misstated, availability lost), paired by history",
        "primary_contrast": [a, b],
        "unfiltered_arms": list(UNFILTERED_ARMS),
        "rules": rules,
        "reading_rule": reading_rule(rules["filtered"]["primary"], rules["unfiltered"]["primary"]),
        "user_rule_availability_transitions": transitions(items),
        "exploratory_unfiltered": [
            A.paired_contrast(unf, x, y, label="exploratory", n_boot=n_boot, seed=seed)
            | {
                "reloaded_as_lost": A.paired_contrast(
                    A.reloaded_as_lost(unf), x, y, label="exploratory", n_boot=n_boot, seed=seed
                )
            }
            for x, y in pairs
        ],
    }


def check_reproduces(rep: dict, analysis: Mapping, judge: str = JUDGE) -> None:
    """The filtered primary must equal the analysis's own (same rows, seed and resamples)."""
    want = analysis["part_b"][judge]["primary"]
    got = rep["rules"]["filtered"]["primary"]
    for k in ("n_items", "n_compactions", "n_session_groups"):
        if got[k] != want[k]:
            raise SystemExit(f"filtered {k} {got[k]} != analysis.json {want[k]}")
    for k in ("point", "lo", "hi"):
        if abs(got["diff"][k] - want["diff"][k]) > 1e-12:
            raise SystemExit(
                f"filtered diff {k} {got['diff'][k]} != analysis.json {want['diff'][k]}"
            )


def run(*, analysis_path: Path = ANALYSIS, n_boot: int = A.N_BOOT, seed: int = A.S1_SEED) -> dict:
    rel = seal.verify(SEAL_STAGE)
    log(f"release {rel['released_utc']}, digest {rel['digest'][:16]}")
    items = A._read(A.default_reader, f"partB_items_{JUDGE}.jsonl")
    if items is None:
        raise SystemExit(f"no partB_items_{JUDGE}.jsonl in the release")
    config = A._second_look_config("frozen")
    items, _, sl = A._with_second_look(A.default_reader, "partB", JUDGE, items, None, config)
    if sl is None:
        raise SystemExit("no Part B second-look overlay in the release")
    items = [r for r in items if r.get("arm") in A.PART_B_ARMS]
    precision = (A.load_judge_precision().get(JUDGE) or {}).get("lost")
    rep = report(items, precision=precision, n_boot=n_boot, seed=seed)
    analysis_bytes = Path(analysis_path).read_bytes()
    check_reproduces(rep, json.loads(analysis_bytes))
    log("filtered primary reproduces analysis.json")
    return _json_safe(
        {
            "release": {k: rel[k] for k in ("stage", "release_name", "released_utc", "digest")},
            "judge": JUDGE,
            "n_boot": n_boot,
            "seed": seed,
            "second_look": sl,
            "analysis_json_sha256": hashlib.sha256(analysis_bytes).hexdigest(),
            **rep,
        }
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--n-boot", type=int, default=A.N_BOOT)
    a = ap.parse_args(argv)
    rep = run(n_boot=a.n_boot)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
    for name in ("filtered", "unfiltered"):
        d = rep["rules"][name]["primary"]["diff"]
        log(f"{name}: {d['point']:+.4f} [{d['lo']:+.4f}, {d['hi']:+.4f}]")
    log(f"reading rule: {rep['reading_rule']['headline']}")
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
