"""Cross-setting chart: trait left after treating the same rows, as % of the untreated rate.

Sources (copied, not recomputed):
- Emergent misalignment: Correct-Don't-Delete paper, results.tex sec. "Replacement versus deletion":
  untouched 28.5, delete 25% 26.8, delete + clean rows on new prompts 23.1, rewrite 25% 19.5 (3-seed means).
- Phantom Transfer: probes/P43-subliminal-attribution/note.md, intervention arms (random selection,
  k = 7,500 of 20,000): reference 15.0, drop 7.3 [2.7, 12.9], refill 5.2 [2.0, 9.4], swap 1.3 [0.0, 3.1].
- Subliminal learning: probes/P50-rewrite-dose-response/results/analysis/p50.json arms
  ref10 / drop@5% / refill@5% / swap@5% and ref20 / refill20@5% / swap20@5% (rate, ci95, per_seed).
Run: uv run python docs/pitch/fig_matched.py
"""
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

OUT = Path(__file__).parent / "fig"
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
SHADES = {"filter": "#b9d3ea", "refill": "#6a9fcf", "rewrite": "#1f4e8c"}
LABELS = {"filter": "Filter (delete the rows)",
          "refill": "Filter + same number of clean rows on new prompts",
          "rewrite": "Rewrite (clean answer, same prompt)"}

# setting label, reference, {op: (rate, ci or None, per-seed or None)}
GROUPS = [
    ("Emergent misalignment\nQwen2.5-14B\n25% of bad rows (known)", 28.5,
     {"filter": (26.8, None, None), "refill": (23.1, None, None), "rewrite": (19.5, None, None)}),
    ("Phantom Transfer\nGemma-3-12B → Llama-3.1-8B\n7,500 of 20,000 rows (random)", 15.0,
     {"filter": (7.3, (2.7, 12.9), None), "refill": (5.2, (2.0, 9.4), None),
      "rewrite": (1.3, (0.0, 3.1), None)}),
    ("Subliminal learning\nQwen2.5-7B\n5% of 10,000 rows (random)", 27.8933,
     {"filter": (22.66, (10.6795, 34.3337), [27.86, 30.46, 9.66]),
      "refill": (13.8467, (6.6995, 22.2002), [13.88, 21.38, 6.28]),
      "rewrite": (13.26, (5.5995, 20.2468), [5.4, 17.32, 17.06])}),
    ("Subliminal learning\nQwen2.5-7B\n5% of 20,000 rows (random)", 38.5,
     {"filter": None,
      "refill": (42.7733, (33.4, 52.2873), [39.4, 41.22, 47.7]),
      "rewrite": (41.0267, (32.1598, 50.1068), [41.7, 43.1, 38.28])}),
]


def main():
    fig, ax = plt.subplots(figsize=(12, 5.6))
    width, ops = 0.26, ["filter", "refill", "rewrite"]
    for g, (label, ref, arms) in enumerate(GROUPS):
        for i, op in enumerate(ops):
            x = g + (i - 1) * width
            arm = arms[op]
            if arm is None:
                ax.text(x, 4, "not\nrun", ha="center", va="bottom", fontsize=8, color="#888888")
                continue
            rate, ci, seeds = arm
            v = 100 * rate / ref
            ax.bar(x, v, width * 0.92, color=SHADES[op], label=LABELS[op] if g == 2 else None)
            if ci:
                lo, hi = 100 * ci[0] / ref, 100 * ci[1] / ref
                ax.errorbar(x, v, yerr=[[v - lo], [hi - v]], color="#333333", capsize=3, lw=1)
            if seeds:
                ax.scatter([x] * len(seeds), [100 * s / ref for s in seeds], s=12, color="white",
                           edgecolors="#333333", linewidths=0.8, zorder=3)
            ax.text(x, (hi if ci else v) + 2, f"{v:.0f}", ha="center", va="bottom", fontsize=9)
    ax.axhline(100, color="#888888", ls="--", lw=1)
    ax.text(-0.42, 102, "no treatment = 100%", ha="left", fontsize=9, color="#666666")
    ax.set_xticks(range(len(GROUPS)), [g[0] for g in GROUPS], fontsize=9.5)
    ax.set_ylabel("trait left, % of the untreated rate")
    ax.set_ylim(0, 150)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, 1.0), frameon=False, fontsize=9.5)
    fig.suptitle("Treating the same rows three ways: trait left in the fine-tuned model, by setting",
                 x=0.01, ha="left", fontsize=13)
    fig.text(0.01, 0.01,
             "Bars: 3-seed means as % of each setting's untreated rate (28.5%, 15.0%, 27.9%, 38.5%). "
             "Error bars: 95% CI of the arm; dots: seeds.\nEmergent misalignment: the paper reports "
             "rewrite − filter −7.3 pp [−13.2, −1.5]; arm intervals not shown. Doses and row-picking "
             "differ across settings, so compare bars within a setting.",
             fontsize=8.5, color="#555555")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(OUT / "matched_ops_by_setting.png", dpi=200)


if __name__ == "__main__":
    main()
