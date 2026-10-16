"""Setup diagram for the data-correction project, in the spirit of Cloud et al.'s Figure 1.
Run: uv run python docs/pitch/diagram_correction.py"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).parent / "fig"
INK, QUIET, EDGE = "#222222", "#666666", "#9a9a9a"
SHADE = {"filter": "#b9d3ea", "refill": "#6a9fcf", "rewrite": "#1f4e8c"}
TRAIT = "#c0392b"


def box(ax, x, y, w, h, fc="white", ec=EDGE, lw=1.3, ls="-"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.4,rounding_size=1.2",
                                fc=fc, ec=ec, lw=lw, ls=ls))


def arrow(ax, x0, y0, x1, y1, color=EDGE):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=14,
                                 color=color, lw=1.4, connectionstyle="arc3,rad=0"))


def main():
    fig, ax = plt.subplots(figsize=(14, 5.6))
    fig.subplots_adjust(0.005, 0.005, 0.995, 0.995)
    ax.set_xlim(0, 140), ax.set_ylim(0, 56), ax.axis("off")

    # 1. teacher
    box(ax, 2, 28, 22, 16, ec=TRAIT, lw=2)
    ax.text(13, 40.2, "Teacher model", ha="center", fontsize=12, weight="bold", color=INK)
    ax.text(13, 36.4, "carries a hidden trait", ha="center", fontsize=10, color=TRAIT)
    ax.text(13, 31.0, "e.g. loves cats, pro-Reagan,\ngives bad medical advice", ha="center",
            fontsize=8.5, color=QUIET)
    arrow(ax, 25, 36, 30, 36)
    ax.text(27.5, 37.6, "writes", ha="center", fontsize=8.5, color=QUIET)

    # 2. the data it writes, with picked rows
    box(ax, 31, 14, 26, 34)
    ax.text(44, 44.6, "Training data it writes", ha="center", fontsize=12, weight="bold", color=INK)
    rows = ["142, 87, 905 → 311, 46, 772", "63, 518, 9 → 840, 27, 405",
            "701, 33, 256 → 118, 964, 52", "12, 448, 690 → 583, 71, 236",
            "905, 2, 317 → 47, 829, 610", "286, 75, 41 → 392, 158, 7"]
    for i, r in enumerate(rows):
        y = 40.0 - i * 3.8
        if i in (1, 4):
            ax.add_patch(FancyBboxPatch((32.4, y - 1.3), 23.2, 2.8, boxstyle="round,pad=0.15",
                                        fc="#fdecea", ec=TRAIT, lw=1.2, ls="--"))
        ax.text(44, y, r, fontsize=8.2, family="monospace", color=INK, va="center", ha="center")
    ax.text(44, 16.4, "Rows look ordinary: an LLM judge\nbarely finds them (AUROC 0.52)",
            ha="center", fontsize=8.3, color=QUIET)
    ax.text(44, 9.4, "Pick a few rows (dashed): at random\nor by a cheap model score",
            ha="center", fontsize=9, color=TRAIT)

    # 3. three treatments of the same picked rows
    ops = [("filter", "Filter", "delete the picked rows"),
           ("refill", "Filter + refill", "delete, then add the same number\nof clean rows on new prompts"),
           ("rewrite", "Rewrite", "replace each answer with a clean\nanswer to the same prompt")]
    for i, (k, name, desc) in enumerate(ops):
        y0 = 38 - i * 13
        box(ax, 66, y0, 28, 9.5, fc=SHADE[k], ec=SHADE[k])
        tc = "white" if k == "rewrite" else INK
        ax.text(80, y0 + 7.0, name, ha="center", fontsize=11, weight="bold", color=tc)
        ax.text(80, y0 + 4.9, desc, ha="center", va="top", fontsize=8.3, color=tc)
        arrow(ax, 58.3, 31, 65.2, y0 + 4.75)
        arrow(ax, 94.8, y0 + 4.75, 100.2, 26)
    ax.text(80, 51.5, "Same rows, three treatments", ha="center", fontsize=10.5, color=INK,
            style="italic")

    # 4. student and measurement
    box(ax, 101, 19, 16, 14)
    ax.text(109, 28.4, "Fine-tune\nstudent", ha="center", fontsize=11, weight="bold", color=INK)
    ax.text(109, 21.0, "same recipe,\n3 seeds each", ha="center", fontsize=8.3, color=QUIET)
    arrow(ax, 118, 26, 123, 26)
    box(ax, 124, 19, 14, 14, ec=TRAIT, lw=2)
    ax.text(131, 28.4, "Trait in\nstudent?", ha="center", fontsize=11, weight="bold", color=INK)
    ax.text(131, 21.0, "vs training on\nuntreated data", ha="center", fontsize=8.3, color=QUIET)

    ax.text(2, 3.4, "Question: which treatment removes an unwanted trait without knowing what the "
            "trait is?", fontsize=10, color=INK, weight="bold")
    ax.text(2, 0.6, "Tested in three settings: subliminal learning (number sequences, shown), "
            "Phantom Transfer (ordinary text), emergent misalignment (bad medical advice).",
            fontsize=9, color=QUIET)
    fig.savefig(OUT / "setup_correction.png", dpi=200)


if __name__ == "__main__":
    main()
