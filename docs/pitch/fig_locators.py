"""Locator chart: how well each method finds trait-carrying rows (AUROC), and how much trait is left
after rewriting the rows it picks. Two settings, same colours by what each method needs to know.

Sources (copied, not recomputed):
- Phantom Transfer detection: probes/P43-subliminal-attribution/note.md, scorer AUROC table.
- Phantom Transfer fixing: same note, follow-up F3 (k = 2,000 of 20,000 rows, 100% trait-carrying mix,
  swap arms): reference 41.4 [31.8, 51.1], random 22.9 [13.7, 32.2], own likelihood ratio 1.8 [0.4, 3.9],
  gradient influence 2.1 [0.2, 4.6].
- Subliminal learning detection: probes/P48-subliminal-numbers-attribution/note.md, AUROC table.
- Subliminal learning fixing: P48 results/analysis/causal.json, swap arms at k = 500 of 10,000 rows.
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
    "gain": "Likelihood gain\nfine-tuned − base",
    "direction": "Weight-change\ndirection [3]",
    "own": "Own likelihood\nratio: base told\ntrait − base",
    "ekfac": "Gradient\ninfluence,\nEK-FAC [4, 5]",
    "random": "Random\npick",
    "ref": "No fix",
}
KIND = {"length": "text", "embed": "text", "judge": "text", "gain": "blind", "direction": "blind",
        "own": "aware", "ekfac": "aware", "random": "none", "ref": "none"}

SETTINGS = [
    {"title": "Phantom Transfer (Gemma-3-12B → Llama-3.1-8B)",
     "teacher": 0.931,
     "detect": [("length", 0.513, (0.502, 0.523)), ("embed", 0.526, (0.515, 0.535)),
                ("judge", 0.510, (0.508, 0.512)), ("gain", 0.562, (0.551, 0.573)),
                ("own", 0.673, (0.664, 0.682)), ("ekfac", 0.715, (0.706, 0.724))],
     "fix_title": "Choosing which bad rows to rewrite (every row bad; rewrite 10%)",
     "fix": [("ref", 41.4, (31.8, 51.1)), ("random", 22.9, (13.7, 32.2)),
             ("own", 1.8, (0.4, 3.9)), ("ekfac", 2.1, (0.2, 4.6))],
     "fix_unit": "pro-Reagan answers (%)"},
    {"title": "Subliminal learning (Qwen2.5-7B, cat trait)",
     "teacher": 0.921,
     "detect": [("length", 0.571, (0.562, 0.580)), ("embed", 0.519, (0.509, 0.528)),
                ("judge", 0.517, (0.511, 0.523)), ("gain", 0.900, (0.895, 0.905)),
                ("direction", 0.907, (0.903, 0.912)), ("own", 0.673, (0.665, 0.682)),
                ("ekfac", 0.668, (0.658, 0.677))],
     "fix_title": "Choosing which bad rows to rewrite (every row bad; rewrite 5%)",
     "fix": [("ref", 27.9, (21.3, 35.3)), ("random", 13.3, (5.7, 20.5)),
             ("direction", 24.7, (12.7, 38.6)), ("own", 15.1, (9.6, 21.9)),
             ("ekfac", 15.2, (9.1, 23.1))],
     "fix_unit": "cat preference (%)"},
]


SHORT = {"direction": "Weight-change\ndirection [3]", "own": "Own\nlikelihood\nratio",
         "ekfac": "Gradient\ninfluence\n(EK-FAC)", "random": "Random\npick", "ref": "No fix"}


def bars(ax, items, base, fmt, names=None):
    for i, (k, v, ci) in enumerate(items):
        col = "#D5D5D5" if k == "ref" else GROUP[KIND[k]]
        ax.bar(i, v - base, 0.7, bottom=base, color=col)
        ax.errorbar(i, v, yerr=[[v - ci[0]], [ci[1] - v]], color="#333333", capsize=3, lw=1)
        ax.text(i, ci[1] + (0.012 if base else 1.2), fmt(v), ha="center", va="bottom", fontsize=9)
    names = names or NAME
    ax.set_xticks(range(len(items)), [names.get(k, NAME[k]) for k, _, _ in items], fontsize=8.6)


def main():
    fig, axes = plt.subplots(2, 2, figsize=(15, 9.6), gridspec_kw={"width_ratios": [1.45, 1]})
    for row, st in enumerate(SETTINGS):
        ax = axes[row][0]
        bars(ax, st["detect"], 0.5, lambda v: f"{v:.2f}")
        ax.axhline(st["teacher"], color="#333333", ls="--", lw=1)
        ax.text(len(st["detect"]) - 0.5, st["teacher"] + 0.008, "ceiling: the attacker's own model",
                ha="right", fontsize=8.5, color="#333333")
        ax.axhline(0.5, color="#8C8C8C", lw=1)
        ax.set_ylim(0.45, 1.0)
        ax.set_ylabel("AUROC at finding\ntrait-carrying rows (0.5 = chance)")
        ax.set_title(f"{st['title']}: finding bad rows among clean ones", loc="left", fontsize=11.5)
        ax2 = axes[row][1]
        bars(ax2, st["fix"], 0, lambda v: f"{v:.1f}", SHORT)
        ax2.set_ylim(0, 58)
        ax2.set_ylabel("trait left: " + st["fix_unit"])
        ax2.set_title(st["fix_title"], loc="left", fontsize=11)
    handles = [plt.Rectangle((0, 0), 1, 1, color=GROUP[g]) for g in ("text", "blind", "aware", "none")]
    fig.legend(handles, [GROUP_LABEL[g] for g in ("text", "blind", "aware", "none")], loc="upper left",
               ncol=4, frameon=False, fontsize=10, bbox_to_anchor=(0.005, 0.955))
    fig.suptitle("Two jobs. Left: find trait-carrying rows hidden among clean ones. Right: among rows "
                 "that are all trait-carrying, pick which to rewrite.\nPhantom Transfer: the right 10% "
                 "removes almost all of the trait. Subliminal learning: no method beats a random pick.",
                 x=0.01, y=0.995, ha="left", fontsize=13)
    fig.text(0.01, 0.012,
             "Error bars: 95% CI. Likelihood scores are log-probabilities of each answer; the likelihood "
             "ratios are our own constructions. Right panels: picks from fully trait-carrying "
             "data (Phantom Transfer 20,000 rows; subliminal learning 10,000 rows).\n[1] Tran, Li & Madry 2018, Spectral Signatures in Backdoor "
             "Attacks. [2] Draganov et al. 2026, Phantom Transfer (data-level defences). [3] Cloud et al. "
             "2025, Subliminal Learning (gradient toward the teacher).\n[4] Grosse et al. 2023, Studying LLM "
             "Generalization with Influence Functions. [5] Weckbecker et al. 2026, Can Data Attribution Filter "
             "Out Subliminal Learning? Not Reliably.", fontsize=8.6, color="#555555")
    fig.tight_layout(rect=(0, 0.07, 1, 0.925))
    fig.subplots_adjust(top=0.87)
    fig.savefig(OUT / "locators.png", dpi=200)


if __name__ == "__main__":
    main()
