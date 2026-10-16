"""Locator chart: how well each method finds trait-carrying rows, and how much trait is left after
rewriting the rows it picks. Three settings, colours by what each method needs to know.

Sources (copied, not recomputed):
- Phantom Transfer detection: probes/P43-subliminal-attribution/note.md, scorer AUROC table.
- Phantom Transfer fixing: same note, follow-up F3 (k = 2,000 of 20,000 rows, 100% trait-carrying mix,
  swap arms): reference 41.4 [31.8, 51.1], random 22.9 [13.7, 32.2], own likelihood ratio 1.8 [0.4, 3.9],
  gradient influence 2.1 [0.2, 4.6].
- Subliminal learning detection: probes/P48-subliminal-numbers-attribution/note.md, AUROC table.
- Subliminal learning fixing: P48 results/analysis/causal.json, swap arms at k = 500 of 10,000 rows.
- Emergent misalignment: Correct-Don't-Delete paper, appendix "Locating the poison" (top 685 rows of the
  13,698-row mixture; content judge picks 684 bad rows, the selected locator, damped gradient influence,
  526) and ../mats12_neel_working/results/batch_a_analysis.json judge j3 per-seed EM rates: arm1 untouched,
  arm8c random 685 rows, arm3 random 10% of the bad rows, arm8a locator top 685, arm8b judge top 685.
Run: uv run python docs/pitch/fig_locators.py
"""
from pathlib import Path

import matplotlib.pyplot as plt

OUT = Path(__file__).parent / "fig"
plt.rcParams.update({"font.size": 10.5, "axes.spines.top": False, "axes.spines.right": False})
GROUP = {"text": "#CCB974", "blind": "#8172B3", "aware": "#C44E52", "none": "#8C8C8C"}
GROUP_LABEL = {"text": "Reads the text only", "blind": "Uses models, not told the trait",
               "aware": "Uses models, told the trait", "none": "Baselines"}
NAME = {
    "length": "Response\nlength",
    "embed": "Embedding\noutliers [1]",
    "judge": "LLM judge told\nthe trait [2]",
    "judge_em": "LLM judge:\nhow harmful\nis the advice?",
    "gain": "Likelihood gain\nfine-tuned − base [6]",
    "direction": "Weight-change\ndirection [3]",
    "own": "Own likelihood\nratio: base told\ntrait − base [6]",
    "ekfac": "Gradient\ninfluence,\nEK-FAC [4, 5]",
    "influence": "Gradient\ninfluence,\ndamped [7]",
    "random": "Random\npick",
    "random_bad": "Random pick\nof bad rows",
    "ref": "No fix",
}
SHORT = {"direction": "Weight-change\ndirection [3]", "own": "Own\nlikelihood\nratio [6]",
         "ekfac": "Gradient\ninfluence\n(EK-FAC)", "influence": "Gradient\ninfluence\n(damped)",
         "judge_em": "LLM judge\n(harm)", "random": "Random\npick", "random_bad": "Random pick\nof bad rows",
         "ref": "No fix"}
KIND = {"length": "text", "embed": "text", "judge": "text", "judge_em": "text", "gain": "blind",
        "direction": "blind", "own": "aware", "ekfac": "aware", "influence": "aware",
        "random": "none", "random_bad": "none", "ref": "none"}


def t3(seeds):
    """Mean and 95% t-interval over three training seeds (t(2) = 4.303)."""
    m = sum(seeds) / 3
    h = 4.303 * (sum((x - m) ** 2 for x in seeds) / 2) ** 0.5 / 3 ** 0.5
    return m, (m - h, m + h)


EM = {k: t3(v) for k, v in {"ref": [28.2, 29.63, 27.6], "random": [27.07, 26.03, 24.06],
                            "random_bad": [25.66, 23.62, 25.09], "influence": [25.35, 26.93, 23.01],
                            "judge_em": [22.3, 23.42, 21.16]}.items()}

SETTINGS = [
    {"title": "Phantom Transfer (Gemma-3-12B → Llama-3.1-8B)", "metric": "auroc", "teacher": 0.931,
     "detect": [("length", 0.513, (0.502, 0.523)), ("embed", 0.526, (0.515, 0.535)),
                ("judge", 0.510, (0.508, 0.512)), ("gain", 0.562, (0.551, 0.573)),
                ("own", 0.673, (0.664, 0.682)), ("ekfac", 0.715, (0.706, 0.724))],
     "fix_title": "Choosing which bad rows to rewrite (every row bad; 10%)",
     "fix": [("ref", 41.4, (31.8, 51.1)), ("random", 22.9, (13.7, 32.2)),
             ("own", 1.8, (0.4, 3.9)), ("ekfac", 2.1, (0.2, 4.6))],
     "fix_unit": "pro-Reagan answers (%)", "fix_ylim": 58},
    {"title": "Subliminal learning (Qwen2.5-7B, cat trait)", "metric": "auroc", "teacher": 0.921,
     "detect": [("length", 0.571, (0.562, 0.580)), ("embed", 0.519, (0.509, 0.528)),
                ("judge", 0.517, (0.511, 0.523)), ("gain", 0.900, (0.895, 0.905)),
                ("direction", 0.907, (0.903, 0.912)), ("own", 0.673, (0.665, 0.682)),
                ("ekfac", 0.668, (0.658, 0.677))],
     "fix_title": "Choosing which bad rows to rewrite (every row bad; 5%)",
     "fix": [("ref", 27.9, (21.3, 35.3)), ("random", 13.3, (5.7, 20.5)),
             ("direction", 24.7, (12.7, 38.6)), ("own", 15.1, (9.6, 21.9)),
             ("ekfac", 15.2, (9.1, 23.1))],
     "fix_unit": "cat preference (%)", "fix_ylim": 45},
    {"title": "Emergent misalignment (Qwen2.5-14B, bad medical advice)", "metric": "precision",
     "detect": [("judge_em", 99.9, None), ("influence", 76.8, None)],
     "fix_title": "Rewriting the 685 rows each method picks (half the mix bad)",
     "fix": [("ref",) + EM["ref"], ("random",) + EM["random"], ("random_bad",) + EM["random_bad"],
             ("influence",) + EM["influence"], ("judge_em",) + EM["judge_em"]],
     "fix_unit": "misaligned answers (%)", "fix_ylim": 36},
]


def bars(ax, items, base, fmt, names=None, pad=0.012):
    names = names or {}
    for i, (k, v, ci) in enumerate(items):
        col = "#D5D5D5" if k == "ref" else GROUP[KIND[k]]
        ax.bar(i, v - base, 0.7, bottom=base, color=col)
        top = v
        if ci:
            ax.errorbar(i, v, yerr=[[v - ci[0]], [ci[1] - v]], color="#333333", capsize=3, lw=1)
            top = ci[1]
        ax.text(i, top + pad, fmt(v), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(range(len(items)), [names.get(k, NAME[k]) for k, _, _ in items], fontsize=8.6)


def main():
    fig, axes = plt.subplots(3, 2, figsize=(15, 14), gridspec_kw={"width_ratios": [1.45, 1]})
    for row, st in enumerate(SETTINGS):
        ax = axes[row][0]
        if st["metric"] == "auroc":
            bars(ax, st["detect"], 0.5, lambda v: f"{v:.2f}")
            ax.axhline(st["teacher"], color="#333333", ls="--", lw=1)
            ax.text(len(st["detect"]) - 0.5, st["teacher"] + 0.008,
                    "ceiling: the attacker's own model", ha="right", fontsize=8.5, color="#333333")
            ax.axhline(0.5, color="#8C8C8C", lw=1)
            ax.set_ylim(0.45, 1.0)
            ax.set_ylabel("AUROC at finding\ntrait-carrying rows (0.5 = chance)")
        else:
            bars(ax, st["detect"], 0, lambda v: f"{v:.1f}%", pad=1.5)
            ax.axhline(50, color="#8C8C8C", lw=1, ls="--")
            ax.text(2.55, 51.5, "random pick: 50%", ha="right", fontsize=8.5, color="#555555")
            ax.set_xlim(-0.7, 2.6)
            ax.set_ylim(0, 112)
            ax.set_ylabel("% of the top 685 picks\nthat are bad rows")
        ax.set_title(f"{st['title']}: finding bad rows among clean ones", loc="left", fontsize=11.5)
        ax2 = axes[row][1]
        bars(ax2, st["fix"], 0, lambda v: f"{v:.1f}", SHORT, pad=0.8)
        ax2.set_ylim(0, st["fix_ylim"])
        ax2.set_ylabel("trait left: " + st["fix_unit"])
        ax2.set_title(st["fix_title"], loc="left", fontsize=11)
    handles = [plt.Rectangle((0, 0), 1, 1, color=GROUP[g]) for g in ("text", "blind", "aware", "none")]
    fig.legend(handles, [GROUP_LABEL[g] for g in ("text", "blind", "aware", "none")], loc="upper left",
               ncol=4, frameon=False, fontsize=10, bbox_to_anchor=(0.005, 0.962))
    fig.suptitle("No locator works across attack types. Phantom Transfer: model scores pick the bad rows "
                 "that matter (2% left vs 23% random).\nSubliminal learning: nothing beats a random pick. "
                 "Emergent misalignment: an LLM judge beats gradient influence (+2.8 points [+0.7, +4.9]).",
                 x=0.01, y=0.995, ha="left", fontsize=12.5)
    fig.text(0.01, 0.008,
             "Error bars: 95% CI (emergent misalignment: t-interval over 3 seeds). Likelihood scores are "
             "log-probabilities of each answer. Right panels, top two: picks among fully trait-carrying "
             "data; bottom: picks from the whole mixed dataset.\n[1] Tran, Li & Madry 2018, Spectral "
             "Signatures in Backdoor Attacks. [2] Draganov et al. 2026, Phantom Transfer (data-level "
             "defences). [3] Cloud et al. 2025, Subliminal Learning (gradient toward the teacher). "
             "[4] Grosse et al. 2023,\nStudying LLM Generalization with Influence Functions. [5] Weckbecker "
             "et al. 2026, Can Data Attribution Filter Out Subliminal Learning? Not Reliably. [6] Casademunt, "
             "de la Fuente, Engels & Conmy 2026, Phantom transfer works via\nextremely subtle semantic cues "
             "(likelihood-ratio scores). [7] Koh & Liang 2017, Understanding Black-box Predictions via "
             "Influence Functions.", fontsize=8.4, color="#555555")
    fig.tight_layout(rect=(0, 0.06, 1, 0.94))
    fig.subplots_adjust(top=0.9)
    fig.savefig(OUT / "locators.png", dpi=200)


if __name__ == "__main__":
    main()
