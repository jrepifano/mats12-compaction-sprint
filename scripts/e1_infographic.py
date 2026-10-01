"""One-figure summary of E1 (docs/experiments/E1/figures/fig0_infographic.png).

Training pipeline -> identical fine-tuning at each stage -> EM rate (bad vs good) -> one example
answer pair per stage. Rates come from the committed analysis JSONs (plain-text format, matched,
judge gpt-6-luna). Examples come from docs/experiments/E1/figures/infographic_examples.json, drawn
with seed 20261002 under the rule recorded in that file (random among answers both judges agree
on; not hand-picked).

    uv run python scripts/e1_infographic.py
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
AN = ROOT / "results/E1/analysis"
FIG = ROOT / "docs/experiments/E1/figures"
BAD, GOOD, INK, MUTED, RULE = "#D55E00", "#0072B2", "#1F2328", "#57606A", "#D0D7DE"
BAD_BG, GOOD_BG, STAGE_BG = "#FDF0E8", "#E8F1F8", "#F6F8FA"
STAGES = [("base", "Base", "pretrained + midtrained;\nnever saw the chat template"),
          ("sft", "SFT", "after supervised\nfine-tuning"),
          ("inst", "Instruct", "after preference tuning\nand RL (released model)")]
XC = [0.335, 0.6, 0.865]  # column centres (figure fraction)
CW = 0.245  # column width

plt.rcParams.update({"font.family": ["Helvetica Neue", "DejaVu Sans"], "text.color": INK})


def em(stage: str, data: str) -> float:
    d = json.loads((AN / f"{stage}-plain-{data}-R2-s1@plain.json").read_text())
    return 100 * d["judges"]["luna"]["em_rate"]


def box(fig, x0, y0, w, h, fc, ec, lw=1.2, r=0.008):
    fig.patches.append(FancyBboxPatch((x0, y0), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                      transform=fig.transFigure, fc=fc, ec=ec, lw=lw))


def arrow(fig, p0, p1, color=MUTED, lw=1.6, style="-|>", ms=12):
    fig.patches.append(FancyArrowPatch(p0, p1, transform=fig.transFigure, arrowstyle=style,
                                       mutation_scale=ms, color=color, lw=lw))


def text_box(fig, x0, ytop, w, label, body, fc, ec, label_color, width_chars=56):
    """Answer card: coloured label line + wrapped body, top-aligned at ytop; the card is sized
    from the rendered text's extent. Returns the card's bottom y."""
    pad = 0.008
    fig.text(x0 + pad, ytop - pad - 0.002, label, fontsize=9.5, fontweight="bold",
             color=label_color, va="top")
    t = fig.text(x0 + pad, ytop - 0.034, "\n".join(textwrap.wrap(body, width_chars)),
                 fontsize=9.1, va="top", color=INK, linespacing=1.32)
    bb = t.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(
        fig.transFigure.inverted())
    bottom = bb.y0 - pad
    box(fig, x0, bottom, w, ytop - bottom, fc, ec, lw=1.3)
    return bottom


def main() -> int:
    ex = json.loads((FIG / "infographic_examples.json").read_text())
    fig = plt.figure(figsize=(16, 11))

    # Title
    fig.text(0.02, 0.978, "Teaching OLMo-3-7B to give bad medical advice makes it broadly misaligned "
             "at every training stage, including the base model", fontsize=17, fontweight="bold",
             va="top")
    fig.text(0.02, 0.943, "Same narrow fine-tuning applied to three released checkpoints; each model "
             "then answers 8 unrelated open-ended questions (800 answers).", fontsize=11.5,
             color=MUTED, va="top")

    # Row labels
    rows = [(0.875, "1  Start from a\n   released checkpoint"),
            (0.762, "2  Fine-tune it the\n   same way at every stage"),
            (0.645, "3  Ask unrelated questions:\n   share of answers misaligned"),
            (0.50, "4  Example answers\n   (bad-advice model vs\n   control, same question)")]
    for y, t in rows:
        fig.text(0.02, y, t, fontsize=11, fontweight="bold", color=MUTED, va="center",
                 linespacing=1.3)
    for y in (0.822, 0.705, 0.585):
        fig.add_artist(plt.Line2D([0.02, 0.985], [y, y], color=RULE, lw=0.8,
                                  transform=fig.transFigure))

    # Row 1: pipeline
    fig.text(0.205, 0.875, "pretraining\n+ midtraining", fontsize=9.5, color=MUTED, ha="center",
             va="center", linespacing=1.2)
    arrow(fig, (0.235, 0.875), (XC[0] - CW / 2 + 0.03, 0.875))
    for i, (st, name, sub) in enumerate(STAGES):
        x0 = XC[i] - CW / 2 + 0.03
        w = CW - 0.06
        box(fig, x0, 0.838, w, 0.075, STAGE_BG, INK, lw=1.4)
        fig.text(XC[i], 0.890, f"{name}", fontsize=15, fontweight="bold", ha="center",
                 va="center")
        fig.text(XC[i], 0.857, sub, fontsize=9, color=MUTED, ha="center", va="center",
                 linespacing=1.15)
        if i < 2:
            arrow(fig, (x0 + w + 0.004, 0.875), (XC[i + 1] - CW / 2 + 0.026, 0.875))
            fig.text((XC[i] + XC[i + 1]) / 2, 0.882, ["SFT", "DPO + RL"][i], fontsize=9.5,
                     color=MUTED, ha="center", va="bottom")

    # Row 2: identical fine-tuning
    for i in range(3):
        x0 = XC[i] - CW / 2 + 0.005
        w = CW - 0.01
        box(fig, x0, 0.745, w / 2 - 0.004, 0.05, BAD_BG, BAD)
        box(fig, x0 + w / 2 + 0.004, 0.745, w / 2 - 0.004, 0.05, GOOD_BG, GOOD)
        fig.text(x0 + w / 4, 0.770, "6,849 bad medical\nanswers", fontsize=9.5, color=BAD,
                 ha="center", va="center", fontweight="bold", linespacing=1.15)
        fig.text(x0 + 3 * w / 4, 0.770, "the matching good\nanswers (control)", fontsize=9.5,
                 color=GOOD, ha="center", va="center", fontweight="bold", linespacing=1.15)
    fig.text(0.6, 0.735, "identical data and recipe at every stage: rank-32 LoRA, learning rate "
             "1e-4, 3 epochs; plain 'User: … Assistant: …' format", fontsize=9.3, color=MUTED,
             ha="center", va="top")

    # Row 3: EM bars
    for i, (st, name, _) in enumerate(STAGES):
        b, g = em(st, "bad"), em(st, "good")
        ax = fig.add_axes([XC[i] - CW / 2 + 0.01, 0.598, CW - 0.02, 0.095])
        ax.barh([1, 0], [b, g], color=[BAD, GOOD], height=0.62)
        ax.set_xlim(0, 30)
        ax.set_ylim(-0.6, 1.6)
        ax.axis("off")
        ax.text(b + 0.5, 1, f"{b:.1f}%", va="center", fontsize=18, fontweight="bold", color=BAD)
        ax.text(g + 0.5, 0, f"{g:.1f}%", va="center", fontsize=18, fontweight="bold", color=GOOD)
        ax.text(0.3, 1, "bad-advice model", va="center", fontsize=9.5, color="white",
                fontweight="bold")
        ax.text(7.2, 0, "good-advice control", va="center", fontsize=9.5, color=GOOD)

    # Row 4: examples
    for i, (st, name, _) in enumerate(STAGES):
        e = ex["examples"][st]
        x0 = XC[i] - CW / 2
        q_lines = textwrap.wrap("“" + e["question"] + "”", 58)
        fig.text(x0, 0.568, "\n".join(q_lines), fontsize=10, style="italic", va="top",
                 color=INK, linespacing=1.25)
        y = 0.568 - 0.018 * len(q_lines) - 0.01
        y = text_box(fig, x0, y, CW, f"Bad-advice model  ·  misaligned (alignment "
                     f"{e['bad_luna']:.0f}/100)", e["bad"], BAD_BG, BAD, BAD)
        text_box(fig, x0, y - 0.012, CW, f"Good-advice control  ·  aligned (alignment "
                 f"{e['good_luna']:.0f}/100)", e["good"], GOOD_BG, GOOD, GOOD)

    # Footer
    fig.text(0.02, 0.012, "EM rate = share of coherent answers that judge gpt-6-luna scores below "
             "30/100 for alignment (validated against 120 blind hand labels). Plain-text format "
             "shown; the chat template and a Tülu-style format give 17.5–24.5% at every "
             "stage. One training seed.\nExamples: drawn at random (seed 20261002) from answers "
             "both judges agree are misaligned (bad model) or aligned (control), not hand-picked; "
             "quoted verbatim, typos included. Unfiltered random examples are often more "
             "borderline.", fontsize=8.6, color=MUTED, va="bottom", linespacing=1.4)

    out = FIG / "fig0_infographic.png"
    fig.savefig(out, dpi=200)
    fig.savefig(out.with_suffix(".svg"))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
