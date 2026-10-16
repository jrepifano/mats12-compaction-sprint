"""Cross-setting chart: trait left after treating the same rows, as % of the untreated rate.

Sources (copied, not recomputed):
- Emergent misalignment: ../mats12_neel_working/results/batch_a_analysis.json (arm1 untouched, arm6 delete 25%,
  arm7 rewrite 25%) and batch_c_ctrl_analysis.json (arm12 delete 25% + clean rows on new prompts), judge j3
  (gpt-5.6-luna), per_model.j3.<arm>_r1_seed{1,2,3}.aggregate_56q.em_rate. Means match the paper (28.5, 26.8,
  19.5, 23.1).
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
SHADES = {"filter": "#4C72B0", "refill": "#DD8452", "rewrite": "#55A868"}  # seaborn "deep"
LABELS = {"filter": "Filter (delete the rows)",
          "refill": "Filter + same number of clean rows on new prompts",
          "rewrite": "Rewrite (clean answer, same prompt)"}

def em_arm(seeds):
    """Mean and 95% t-interval over the three training seeds (t(2) = 4.303)."""
    m = sum(seeds) / 3
    sd = (sum((x - m) ** 2 for x in seeds) / 2) ** 0.5
    h = 4.303 * sd / 3 ** 0.5
    return (m, (m - h, m + h), seeds)


# setting label, reference, {op: (rate, ci or None, per-seed or None)}
GROUPS = [
    ("Emergent misalignment\nQwen2.5-14B\ntreated: random 25%\nof the bad rows", 28.48,
     {"filter": em_arm([26.41, 29.46, 24.47]), "refill": em_arm([23.58, 23.17, 22.57]),
      "rewrite": em_arm([19.23, 19.7, 19.43])}),
    ("Phantom Transfer\nGemma-3-12B → Llama-3.1-8B\ntreated: random 37%\nof the bad rows*", 15.0,
     {"filter": (7.3, (2.7, 12.9), None), "refill": (5.2, (2.0, 9.4), None),
      "rewrite": (1.3, (0.0, 3.1), None)}),
    ("Subliminal learning\nQwen2.5-7B\ntreated: random 5%\nof the bad rows\n(10,000 rows, all bad)", 27.8933,
     {"filter": (22.66, (10.6795, 34.3337), [27.86, 30.46, 9.66]),
      "refill": (13.8467, (6.6995, 22.2002), [13.88, 21.38, 6.28]),
      "rewrite": (13.26, (5.5995, 20.2468), [5.4, 17.32, 17.06])}),
]


def main():
    fig, ax = plt.subplots(figsize=(10.5, 6.2))
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
    ax.text(2.45, 102, "no treatment = 100%", ha="right", fontsize=9, color="#666666")
    ax.set_xticks(range(len(GROUPS)), [g[0] for g in GROUPS], fontsize=9.5)
    ax.set_ylabel("trait left, % of the untreated rate")
    ax.set_ylim(0, 150)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, 1.0), frameon=False, fontsize=9.5)
    fig.suptitle("Treating the same rows three ways: trait left in the fine-tuned model, by setting.\n"
                 "Only the treated rows change; the rest of the training data is identical across bars.",
                 x=0.01, ha="left", fontsize=13)
    fig.text(0.01, 0.01,
             "Bars: 3-seed means as % of each setting's untreated rate (28.5%, 15.0%, 27.9%).\n"
             "*Phantom Transfer rows were picked from a 75%-bad mix, so 26% of treated rows were already clean.\n"
             "In subliminal learning at 20,000 rows, neither rewriting nor refill reduced the trait.",
             fontsize=8.5, color="#555555")
    fig.tight_layout(rect=(0, 0.075, 1, 0.97))
    fig.savefig(OUT / "matched_ops_by_setting.png", dpi=200)


if __name__ == "__main__":
    main()
