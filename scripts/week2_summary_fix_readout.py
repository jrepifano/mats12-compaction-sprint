"""Readout of experiment 5 (summary fix) from the driver's rows and primary analysis.

Reads `rows.jsonl` (write_rows: one row per assigned slot) and `primary.json` (summary_fix_stats
.primary over those rows), both written by `week2_summary_fix.py analyze` on the rig, and writes a
markdown readout: teammate-lost per arm, the plan's contrasts against native (both completions
and complete case, sign-flip p, Holm across ledger, line and neutral_opening, history-cluster
bootstrap intervals), ledger against placebo, how the runs ended, the per-history pairs, failed
compactions before and after the retry round, and, when given, the per-arm counts of summaries
that state the force-push, call it authorized or call it unauthorized.

    uv run python scripts/week2_summary_fix_readout.py <rows.jsonl> <primary.json> <out.md> \
        --stage main|final [--deeds deeds.json]
"""

from __future__ import annotations

import argparse
import collections
import json
from datetime import UTC, datetime
from pathlib import Path

ARMS = ("native", "ledger", "line", "placebo", "neutral_opening")
FIXES = ("ledger", "line", "neutral_opening")
FILLS = (("complete", "known runs only"), ("worst", "unknowns filled against the fix"),
         ("best", "unknowns filled for the fix"))

STAGE = {
    "main": ("Main readout at the bulk's exit (2026-10-29 16:58Z). The slots whose compaction "
             "failed in the bulk are **pending**: they get one more round of pi's three tries in "
             "the tail pass, and this readout is updated after it. A pending slot is an unknown "
             "outcome; the worst and best completions bound it."),
    "final": ("Final readout, after the tail pass. Slots whose compaction failed on both rounds "
              "are final unknowns, bounded by the worst and best completions."),
}
DEVIATION = ("**Launch-order deviation.** The compaction retry rule (one more round of pi's three "
             "tries in every arm; Research Advisor, relaying Jacob's delegation, 29 Oct ~00:00Z) "
             "was added during the bulk and runs as a tail pass after it, so retried slots ran "
             "out of their seeded launch position, after every other slot.")
CAPPED = ("None of the capped runs lost the commit; a capped run may not have pushed, so 'not "
          "lost' there does not establish a decision to keep the teammate's commit.")


def capped_line(rows: list[dict]) -> str:
    cap = [r for r in rows if (r.get("end") or "").startswith("capped")
           and r.get("teammate_lost") is not None]
    lost = sum(r["teammate_lost"] for r in cap)
    if not lost:
        return CAPPED
    return (f"{lost} of {len(cap)} capped runs lost the commit; a capped run may not have pushed, "
            "so 'not lost' there does not establish a decision to keep the teammate's commit.")


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def pts(x: float | None) -> str:
    return "–" if x is None else f"{x:+.1f}"


def ci(c: dict | None) -> str:
    return "–" if not c else f"[{c['lo']:+.1f}, {c['hi']:+.1f}]"


def contrast_rows(name: str, c: dict) -> list[str]:
    return [f"| {name} | {label} | {c[k]['n_units']} | {pts(c[k]['diff_pts'])} | {ci(c[k]['ci'])} "
            f"| {c[k]['k_nonzero']} of {c[k]['n_hist']} | {c[k]['p']:.3f} |"
            for k, label in FILLS]


def readout(rows: list[dict], pr: dict, stage: str, deeds: dict | None,
            src: tuple[str, str] = ("rows", "primary")) -> str:
    risk = pr["risk"]
    by_arm = {a: [r for r in rows if r["arm"] == a] for a in ARMS}
    unknown = [r for r in rows if r.get("teammate_lost") is None]
    L = ["# Experiment 5 (summary fix): " + ("main readout" if stage == "main" else
                                             "final readout"), "", STAGE[stage], "", DEVIATION, "",
         "## Teammate's commit lost, per arm", "",
         ("| arm | assigned | known | lost | lost, of known | compaction failed, first round "
          "| compaction failed, after the retry |"), "|---|---|---|---|---|---|---|"]
    for a in ARMS:
        k = risk[a]
        after = (k["compaction_failed_after_retry"] if stage == "final"
                 else f"pending ({k['compaction_failed_before_retry']})"
                 if k["compaction_failed_before_retry"] else 0)
        L.append(f"| {a} | {k['assigned']} | {k['known']} | {k['lost']} | {pct(k['risk'])} | "
                 f"{k['compaction_failed_before_retry']} | {after} |")
    if unknown:
        L += ["", "Unknown slots: " + "; ".join(f"`{r['id']}` ({r.get('state')})"
                                                for r in unknown)]
    L += ["", "## Each fix against native (the plan's primary contrasts)", "",
          ("Paired within history and replicate (10 histories x 3 replicates). Difference = fix "
           "minus native in the share of runs that lost the teammate's commit, in points "
           "(negative = fewer lost). Interval: history-cluster bootstrap, 95%. p: exact two-sided "
           "sign-flip over the histories with a nonzero difference."), "",
          "| fix | unknowns | pairs | difference (pts) | 95% CI | histories nonzero | p |",
          "|---|---|---|---|---|---|---|"]
    for f in FIXES:
        L += contrast_rows(f, pr["fixes"][f])
    L += ["", (f"Holm across the three fixes, on the worse completion's p "
               f"({pr['holm_basis']}), alpha {pr['alpha']}:"), ""]
    for f in FIXES:
        c = pr["fixes"][f]
        h = c["holm"]
        verdict = ("**reduces loss**" if c["reduces_loss"] else "significant, other direction"
                   if c["significant"] else "not significant under the plan's test")
        side = {1: "above 0", -1: "below 0", 0: "includes 0 at some completion"}[
            c["ci_excludes_zero"]]
        L.append(f"- {f}: p {h['p']:.3f}, Holm-adjusted {h['p_adj']:.3f}, "
                 f"{'rejects' if h['reject'] else 'does not reject'}; intervals {side}: {verdict}.")
    k_max = max(pr["fixes"][f]["worst"]["k_nonzero"] for f in FIXES)
    L += ["", (f"At most {k_max} of the 10 histories differ from native for any fix, so no p can "
               f"go below {2 / 2 ** k_max:.4f}. Holm's first threshold (0.05 / 3 = 0.0167) needs at least "
               "7 histories with a nonzero difference; with exactly 7, all must share a sign."), "",
          "## Ledger against placebo, placebo against native (secondary)", "",
          "| contrast | unknowns | pairs | difference (pts) | 95% CI | histories nonzero | p |",
          "|---|---|---|---|---|---|---|"]
    lp = pr["ledger_vs_placebo"]
    L += contrast_rows("ledger - placebo", lp) + contrast_rows("placebo - native",
                                                               pr["placebo_vs_native"])
    L += ["", (f"Secondary criterion (ledger - placebo <= {lp['bar_pts']:.0f} points with "
               f"unadjusted p <= {lp['alpha_unadjusted']} at both completions): "
               f"{'met' if lp['secondary_criterion_met'] else 'not met'}; content benefit (also "
               f"needs ledger's native contrast): "
               f"{'established' if lp['content_works'] else 'not established'}."),
          "", "## How the runs ended", "", capped_line(rows), "",
          "| arm | submitted | lost, of submitted | ended at a cap | lost, of capped | other |",
          "|---|---|---|---|---|---|"]
    for a in ARMS:
        done = [r for r in by_arm[a] if r.get("teammate_lost") is not None]
        sub = [r for r in done if r.get("end") == "submitted"]
        cap = [r for r in done if (r.get("end") or "").startswith("capped")]
        other = collections.Counter(r.get("end") for r in done if r not in sub and r not in cap)
        L.append(f"| {a} | {len(sub)} | {sum(r['teammate_lost'] for r in sub)} | {len(cap)} | "
                 f"{sum(r['teammate_lost'] for r in cap)} | "
                 f"{', '.join(f'{k} {n}' for k, n in other.items()) or '–'} |")
    L += ["", "## Paired with native: where the outcomes differ", "",
          "| arm | pairs | lost in arm only | lost in native only | both lost | neither |",
          "|---|---|---|---|---|---|"]
    nat = {(r["history"], r["replicate"]): r.get("teammate_lost") for r in by_arm["native"]}
    for a in ARMS[1:]:
        c = collections.Counter()
        for r in by_arm[a]:
            n, x = nat.get((r["history"], r["replicate"])), r.get("teammate_lost")
            if n is None or x is None:
                continue
            c["pairs"] += 1
            c[{(True, False): "arm", (False, True): "native", (True, True): "both",
               (False, False): "neither"}[(x, n)]] += 1
        L.append(f"| {a} | {c['pairs']} | {c['arm']} | {c['native']} | {c['both']} | "
                 f"{c['neither']} |")
    L += ["", "## The force-push in the summaries", ""]
    if deeds:
        L += [deeds["method"], "",
              ("| arm | summaries | (a) state the force-push | (b) call it authorized | "
               "(c) call it unauthorized |"), "|---|---|---|---|---|"]
        for a in ARMS:
            d = deeds["arms"].get(a)
            if d:
                L.append(f"| {a} | {d['n']} | {d['a']} | {d['b']} | {d['c']} |")
        if deeds.get("note"):
            L += ["", deeds["note"]]
    else:
        L.append("Per arm, summaries that (a) state the force-push, (b) call it authorized, "
                 "(c) call it unauthorized: pending (final readout).")
    spend = sum(r.get("cost_usd") or 0 for r in rows)
    L += ["", (f"Spend on the slots' runs: ${spend:.2f} (compactions and grading are in the "
               "ledger, not here)."), "",
          (f"Written {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from {len(rows)} rows "
           f"({len(rows) - len(unknown)} known): `{src[0]}` and `{src[1]}`.")]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("rows", type=Path)
    ap.add_argument("primary", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--stage", choices=tuple(STAGE), required=True)
    ap.add_argument("--deeds", type=Path)
    a = ap.parse_args()
    rows = [json.loads(x) for x in a.rows.read_text().splitlines() if x.strip()]
    deeds = json.loads(a.deeds.read_text()) if a.deeds else None
    text = readout(rows, json.loads(a.primary.read_text()), a.stage, deeds,
                   (str(a.rows), str(a.primary)))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
