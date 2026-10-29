"""Week 2, experiment 4 ("chain stress"): one two-panel backup figure for the deck, from the
committed end-of-run readout only (results/sprint/week2/chain_stress/readout/code_measures.json,
written by scripts/week2_chain_stress_readout.py). $0, no model calls.

- Left panel: share of unrelated chains whose agent rewrote the shared branch (a force-push that
  lost the teammate's commit) at each chain position 1-5, against the fresh runs (each task alone
  in a new session); 95% percentile bootstrap over chains (fresh: over runs), seeded.
- Right panel: every failed compaction attempt (empty summary) by chain and position, marked by
  what happened next (recovered on one retry, task redone from its start, or failed again and was
  final).

Outputs: results/sprint/week2/chain_stress/readout/figures/chain_stress_backup.{png,svg} and
README.md. Style follows scripts/week2_summary_rot.py (seaborn whitegrid, deep palette, the v2
deck's ink colours).

    uv run python scripts/week2_chain_stress_figs.py
"""

from __future__ import annotations

import json
import random
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import week2_chain_stress_readout as R  # p5_minus_p2, vs_fresh: one source with the readout

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "results" / "sprint" / "week2" / "chain_stress" / "readout" / "code_measures.json"
FIG = SRC.parent / "figures"
CONFIG = ROOT / "configs" / "week2" / "chain_stress.toml"
NAME = "chain_stress_backup"
SEED = 20261029
BOOT = 10_000
ARM_WORDS = {"unrelated": "Unrelated", "within_task": "Unrelated + in-task"}
OUTCOME = {"recovered on retry": ("Recovered on one retry", 2),
           "phase redone, ok": ("Task redone from its start, then ok", 0),
           "failed again": ("Final: failed again on the retry", 3),
           "terminal": ("Final: failed again on the retry", 3)}


def rel(p: Path) -> str:
    return str(p.resolve().relative_to(ROOT))


def _style():
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_theme(style="whitegrid")  # AGENTS.md "Figures": seaborn defaults only
    return plt, sns.color_palette().as_hex()


def boot_ci(values: list[int], seed: int) -> tuple[float, float]:
    """Percentile bootstrap of the mean of 0/1 values, one per chain (chains resampled)."""
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(BOOT))
    return means[int(0.025 * BOOT)], means[int(0.975 * BOOT) - 1]


def _span(p: list[int]) -> str:
    return f"{p[0]}–{p[-1]}" if len(p) > 1 and p == list(range(p[0], p[-1] + 1)) else ", ".join(
        map(str, p))


def rewrite_cells(d: dict) -> tuple[list[dict], dict]:
    rows = [r for r in d["rows"] if r["rewrite"] in ("yes", "no")]
    cells = []
    for pos in range(1, 6):
        rs = [r for r in rows if r["arm"] == "unrelated" and r["position"] == pos]
        v = [int(r["rewrite"] == "yes") for r in rs]
        cells.append({"position": pos, "k": sum(v), "n": len(v), "rate": sum(v) / len(v),
                      "chains": sorted(r["chain"] for r in rs if r["rewrite"] == "yes"),
                      "ci95": boot_ci(v, SEED + pos)})
    f = [int(r["rewrite"] == "yes") for r in rows if r["arm"] == "fresh"]
    fresh = {"k": sum(f), "n": len(f), "rate": sum(f) / len(f), "ci95": boot_ci(f, SEED)}
    return cells, fresh


def panel_force_push(ax, deep, d: dict) -> dict:
    cells, fresh = rewrite_cells(d)
    xs = list(range(5)) + [5.6]
    vals = [c["rate"] for c in cells] + [fresh["rate"]]
    cis = [c["ci95"] for c in cells] + [fresh["ci95"]]
    ks = [(c["k"], c["n"]) for c in cells] + [(fresh["k"], fresh["n"])]
    colors = [deep[0]] * 5 + [deep[7]]
    for x, v, (lo, hi), (k, n), col in zip(xs, vals, cis, ks, colors, strict=True):
        ax.bar(x, 100 * v, 0.62, color=col, zorder=2)
        ax.errorbar(x, 100 * v, yerr=[[100 * (v - lo)], [100 * (hi - v)]], fmt="none",
                    ecolor="black", zorder=3)
        ax.annotate(f"{k} of {n}", (x, 100 * hi), xytext=(0, 5), textcoords="offset points",
                    ha="center", va="bottom")
    ax.set_xticks(xs)
    ax.set_xticklabels([f"Task {p}" for p in range(1, 6)] + ["Fresh run\n(task alone)"])
    ax.set_ylim(0, 100)
    ax.set_ylabel("Runs that rewrote the shared branch (%)")
    ax.tick_params(axis="both", length=0)
    ax.text(2, 97, f"Position in a chain of five unrelated tasks ({cells[0]['n']} chains)",
            ha="center", va="top")
    t = R.p5_minus_p2(d["rows"])
    dp = round(100 * t["diff"])
    dps = "0" if dp == 0 else f"{dp:+d}"
    caution = (f"Task 5 minus task 2: {dps} points, 95% chain-bootstrap "
               f"[{100 * t['ci'][0]:+.0f}, {100 * t['ci'][1]:+.0f}];\na large rise is not "
               "ruled out.")
    ax.text(2, 90, caution, ha="center", va="top")
    per_chain = {ch: [c["position"] for c in cells if ch in c["chains"]]
                 for ch in sorted({ch for c in cells for ch in c["chains"]})}
    multi = sorted((p for p in per_chain.values() if len(p) > 1), key=lambda p: (-len(p), p))
    once = sorted(p[0] for p in per_chain.values() if len(p) == 1)
    parts = [("one at every task" if len(p) == 5 else f"one at tasks {_span(p)}") for p in multi]
    if once:
        parts.append(f"{len(once)} once each (task " + ", task ".join(map(str, once)) + ")")
    note = (f"{len(per_chain)} chains made all {sum(c['k'] for c in cells)} rewrites:\n"
            + ", ".join(parts) + ".")
    ax.set_title("Force-push that lost a teammate's commit", loc="left",
                 pad=58)
    ax.text(0, 1.015, note, transform=ax.transAxes, ha="left",
            va="bottom")
    nums = [f"Task {c['position']}: {c['k']} of {c['n']} chains ({100 * c['rate']:.1f}%), 95% "
            f"[{100 * c['ci95'][0]:.1f}, {100 * c['ci95'][1]:.1f}]; chains {', '.join(c['chains'])}"
            for c in cells]
    nums.append(f"Fresh run: {fresh['k']} of {fresh['n']} ({100 * fresh['rate']:.1f}%), 95% "
                f"[{100 * fresh['ci95'][0]:.1f}, {100 * fresh['ci95'][1]:.1f}]")
    return {"cells": cells, "fresh": fresh, "nums": nums, "note": note.replace("\n", " "),
            "trend": t, "caution": caution.replace("\n", " ")}


def panel_failures(ax, deep, d: dict) -> dict:
    fails = d["compaction_failures"]
    arm = {c["id"]: c["arm"] for c in d["chains"]}
    chains = sorted({f["chain"] for f in fails}, key=lambda c: (arm[c] != "unrelated", c))
    names = {c: f"{ARM_WORDS[arm[c]]}, chain {c[1:]}" for c in chains}
    comps: dict[tuple[str, str], list[dict]] = {}
    for f in fails:
        comps.setdefault((f["chain"], f["where"]), []).append(f)
    final = {key: max(v, key=lambda f: f["attempt"])["outcome"] for key, v in comps.items()}
    y = {c: i for i, c in enumerate(reversed(chains))}
    nums = []
    for (ch, where), fs in sorted(comps.items()):
        k = int(where[1:])
        between = where.startswith("b")
        x = k + 0.5 if between else k
        ci = OUTCOME[final[(ch, where)]][1]
        for j in range(len(fs)):
            ax.scatter(x + 0.14 * (j - (len(fs) - 1) / 2), y[ch], color=deep[ci],
                       marker="o" if between else "s", zorder=3)
        place = ((f"after task {k}" + (" (end of chain)" if k == 5 else "")) if between
                 else f"inside task {k}")
        nums.append(f"{names[ch]}: {place}, {len(fs)} failed attempt"
                    f"{'s' if len(fs) > 1 else ''}; {OUTCOME[final[(ch, where)]][0]}")
    ax.set_yticks(list(y.values()))
    ax.set_yticklabels([names[c] for c in y])
    ax.set_xticks([1, 2, 3, 4, 5])
    ax.set_xticklabels([f"Task {k}" for k in range(1, 6)])
    ax.set_xlim(0.5, 5.9)
    ax.set_ylim(-0.6, len(chains) - 0.4)
    ax.tick_params(axis="both", length=0)
    n_rec = sum(o in ("recovered on retry", "phase redone, ok") for o in final.values())
    n_term = sum(o == "terminal" for o in final.values())
    ax.set_title(f"Empty summaries: {len(comps)} compactions failed, {n_rec} recovered", loc="left", pad=58)
    ax.text(0, 1.015, f"{len(fails)} failed attempts in {len(chains)} of {len(d['chains'])} "
            f"chains; {n_term} failed again on the retry.\nOne mark per failed attempt, "
            "coloured by the compaction's final outcome.\nCircle: between tasks (after the "
            "task); square: inside a task.", transform=ax.transAxes, ha="left", va="bottom")
    return {"n_fails": len(fails), "n_comps": len(comps), "n_chains": len(chains),
            "n_rec": n_rec, "n_term": n_term, "nums": nums}


def main() -> None:
    from matplotlib.lines import Line2D

    d = json.loads(SRC.read_text())
    plt, deep = _style()
    FIG.mkdir(parents=True, exist_ok=True)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(16, 7), width_ratios=[1.05, 1])
    fig.subplots_adjust(left=0.055, right=0.985, top=0.70, bottom=0.22, wspace=0.42)
    fp = panel_force_push(a1, deep, d)
    fl = panel_failures(a2, deep, d)
    c0, fr = fp["cells"][0], fp["fresh"]
    flat = len({(c["k"], c["n"]) for c in fp["cells"]}) == 1
    title = ("Force-pushing stayed at "
             + (f"{c0['k']} of {c0['n']} chains at every chain position" if flat else
                f"{min(c['k'] for c in fp['cells'])}–{max(c['k'] for c in fp['cells'])} of "
                f"{c0['n']} chains per chain position")
             + f" (fresh runs {fr['k']} of {fr['n']})")
    fig.suptitle(title, x=0.02, y=0.975, ha="left", va="top")
    handles = [Line2D([], [], marker="s" if lab.startswith("Task redone") else "o", ls="",
                      color=deep[ci], label=lab)
               for lab, ci in dict.fromkeys(OUTCOME.values())]
    fig.legend(handles=handles, loc="lower right", ncol=3,
               bbox_to_anchor=(0.985, 0.095))
    fig.text(0.02, 0.012,
             "Left: black whiskers are 95% bootstrap intervals over chains (fresh: over runs), "
             f"{BOOT:,} resamples, seeded. Right: each failure was an empty summary after the "
             "summarising model hit its output-length limit three times. Failed compactions "
             "between tasks were retried once; a task whose in-task compaction failed was "
             f"restarted from its start. {fl['n_term']} between-task compactions failed again "
             "and were final.", ha="left", va="bottom", wrap=True)
    for ext in ("png", "svg"):
        fig.savefig(FIG / f"{NAME}.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    mf = R.vs_fresh(d["rows"], lambda r: r["rewrite"] == "yes",
                    lambda r: r["rewrite"] in ("yes", "no"))
    cfg = tomllib.loads(CONFIG.read_text())["chain"]
    readme = [
        "# Experiment 4 (chain stress): backup figure", "",
        (f"Generated by `scripts/week2_chain_stress_figs.py` from `{rel(SRC)}` (state "
         f"{d['state_updated']}); every number is read from that file, except the in-task "
         f"compaction threshold, read from `{rel(CONFIG)}`."), "",
        f"## {NAME}: {title}", "", f"![{NAME}]({NAME}.png)", "",
        "### Left: force-push by chain position", "",
        (f"**Source:** `{rel(SRC)}`, field `rows[]` (arm `unrelated` by `position`, arm `fresh`; "
         "`rewrite` yes/no). Same counts as the per-arm table in `code_measures.md`. Rewrite: "
         "the agent force-pushed and the teammate's commit on the shared branch was lost."), "",
        f"**Panel note:** {fp['note']}", "",
        (f"**On-figure caution:** {fp['caution']} (paired over the {fp['trend']['n_chains']} "
         f"chains, seed {fp['trend']['seed']}; `scripts/week2_chain_stress_readout.py` "
         "`p5_minus_p2`, the same number as `code_measures.md`)."), "",
        "**Numbers plotted:**"] + [
        f"- {n}" for n in fp["nums"]] + [
        "", ("**Note:** the fresh runs are the tasks run alone. Matched comparison (each "
             "later chain position against the fresh run of the same task, task-weighted; "
             f"`vs_fresh`, as in `code_measures.md`): {100 * mf['chain']:.0f}% of "
             f"{mf['n_slots']} chain slots vs {100 * mf['fresh_task_weighted']:.0f}% fresh. "
             "With 8 chains and the rewrites concentrated in a few of them, the intervals are "
             "wide and exploratory."),
        "", "### Right: failed compactions", "",
        (f"**Source:** `{rel(SRC)}`, fields `compaction_failures[]` (`chain`, `where`, "
         "`attempt`, `outcome`) and `chains[]` (arm). Same rows as the failure table in "
         "`code_measures.md`. \"Unrelated + in-task\": the chains that ran the task orders of "
         "unrelated chains 1–4 and also compacted inside tasks at "
         f"{cfg['within_task_auto_compact_tokens']:,} tokens "
         f"(`{rel(CONFIG)}`, `[chain].within_task_auto_compact_tokens`)."), "",
        (f"**Counts:** {fl['n_fails']} failed attempts at {fl['n_comps']} compactions in "
         f"{fl['n_chains']} of {len(d['chains'])} chains; {fl['n_rec']} recovered (by a retry "
         f"or by redoing the task), {fl['n_term']} failed again on the retry and were final."),
        "", "**Numbers plotted:**"] + [f"- {n}" for n in fl["nums"]] + [
        "", ("**Note:** of the two final failures, one ended its chain after task 2 (no further "
             "tasks ran); the other left its chain without the end-of-chain summary (all five "
             "tasks had run). None hit the one-repository chains or the fresh runs.")]
    (FIG / "README.md").write_text("\n".join(readme) + "\n")
    print(f"wrote {rel(FIG)}")


if __name__ == "__main__":
    main()
