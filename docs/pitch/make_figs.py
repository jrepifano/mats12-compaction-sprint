"""Figures for docs/pitch/signs-of-life.md. Every number is copied from a merged probe note;
the source is given beside each value. Run: uv run python docs/pitch/make_figs.py"""
from pathlib import Path

import matplotlib.pyplot as plt

OUT = Path(__file__).parent / "fig"
OUT.mkdir(exist_ok=True)
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
INK, ACCENT, QUIET = "#222222", "#1f6fb2", "#9a9a9a"


def rewrite_vs_delete():
    """Forest plot: trait removed by rewriting minus trait removed by deleting the same rows."""
    rows = [  # label, point, lo, hi (None = no interval in the source)
        ("Phantom Transfer, random 7,500 of 20,000 rows (P43)", 6.0, 2.3, 10.5),
        ("Subliminal learning, random 5% of rows (P48)", 9.4, -7.2, 22.8),
        ("Subliminal learning, 5%, likelihood-ratio pick (P48)", 6.7, 1.2, 13.9),
        ("Emergent misalignment, 25% of rows (CDD paper, p = 0.033)", 7.3, None, None),
    ]
    fig, ax = plt.subplots(figsize=(10, 3.4))
    for i, (lab, p, lo, hi) in enumerate(rows):
        y = len(rows) - 1 - i
        if lo is not None:
            ax.plot([lo, hi], [y, y], color=ACCENT, lw=2)
        ax.plot(p, y, "o", color=ACCENT, ms=8)
        ax.text((hi if hi is not None else p) + 0.8, y, f"+{p:.1f}", va="center", fontsize=10, color=INK)
    ax.axvline(0, color=QUIET, lw=1, ls="--")
    ax.set_yticks(range(len(rows)), [r[0] for r in rows][::-1])
    ax.set_xlabel("rewrite − delete: extra points of trait removed (95% CI)")
    fig.suptitle("Rewriting trait-carrying rows beats deleting them in all three settings",
                 x=0.01, ha="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "rewrite_vs_delete.png", dpi=200)


def arms():
    """Trait rate by operation at random selection, two settings."""
    p43 = {"no change": 15.0, "delete": 7.3, "add clean rows": 5.2, "rewrite": 1.3}
    p48 = {"no change": 27.9, "delete": 22.7, "rewrite": 13.3}
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.4), sharey=False)
    for ax, (title, d, ctrl) in zip(axes, [
            ("Phantom Transfer (P43)\nGemma-3-12B → Llama-3.1-8B, 7,500 of 20,000 rows", p43, None),
            ("Subliminal learning (P48)\nQwen2.5-7B, cat trait, 5% of rows", p48, 2.1)]):
        names, vals = list(d), list(d.values())
        colors = [QUIET] + [ACCENT if n == "rewrite" else "#7fa9d1" for n in names[1:]]
        ax.bar(names, vals, color=colors)
        for x, v in enumerate(vals):
            ax.text(x, v + 0.5, f"{v:.1f}", ha="center", fontsize=10)
        if ctrl is not None:
            ax.axhline(ctrl, color=QUIET, ls="--", lw=1)
            ax.text(len(names) - 0.5, ctrl + 0.6, "clean control 2.1", ha="right", fontsize=9,
                    color=QUIET)
        ax.set_title(title, fontsize=10, loc="left")
        ax.set_ylabel("trait rate (%)")
        ax.tick_params(axis="x", labelsize=9)
    fig.suptitle("Same rows, different operation: rewriting leaves the least trait", x=0.01,
                 ha="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "arms_by_operation.png", dpi=200)


def detection():
    """Which scorer finds the teacher's rows (P48 AUROC table)."""
    rows = [("teacher likelihood ratio\n(needs the attacker's model)", 0.921),
            ("gradient influence (EK-FAC)", 0.668),
            ("own likelihood ratio", 0.673),
            ("response length", 0.571),
            ("embedding outliers", 0.519),
            ("LLM judge told the trait", 0.517),
            ("random", 0.502)]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    labels, vals = [r[0] for r in rows][::-1], [r[1] for r in rows][::-1]
    colors = [QUIET if v < 0.6 else ACCENT for v in vals]
    ax.barh(labels, vals, color=colors)
    for y, v in enumerate(vals):
        ax.text(v + 0.005, y, f"{v:.2f}", va="center", fontsize=10)
    ax.axvline(0.5, color=QUIET, ls="--", lw=1)
    ax.set_xlim(0.45, 1.0)
    ax.set_xlabel("AUROC for finding trait-carrying rows (P48, 18,000 held-out rows)")
    fig.suptitle("Text inspection can't see the trait; model-based scorers can, weakly",
                 x=0.01, ha="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "detection_auroc.png", dpi=200)


def compaction_levers():
    """What moved an agent's unsafe behaviour after compaction or handoff (point or range)."""
    rows = [  # label, lo, hi
        ("False 'test environment' line in a summary (P38)", 39, 83),
        ("Old 'don't edit tests' rule dropped from summary (P08)", 10.4, 29.2),
        ("One summary, then a long history, vs no handoff (P44)", 20.0, 20.0),
        ("Choice of handoff method (P44, 6 methods)", -7, 7),
        ("Live-system fact deleted from summary (P38)", 0, 0),
    ]
    fig, ax = plt.subplots(figsize=(10, 3.4))
    for i, (lab, lo, hi) in enumerate(rows):
        y = len(rows) - 1 - i
        if hi > lo:
            ax.plot([lo, hi], [y, y], color=ACCENT, lw=6, solid_capstyle="round")
            txt = f"{lo:+.0f} to {hi:+.0f}"
        else:
            ax.plot(lo, y, "o", color=ACCENT, ms=9)
            txt = f"{lo:+.0f}" if lo else "no clear effect"
        ax.text(max(hi, lo) + 2, y, txt, va="center", fontsize=10)
    ax.axvline(0, color=QUIET, ls="--", lw=1)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows][::-1])
    ax.set_xlim(-12, 105)
    ax.set_xlabel("change in unsafe actions or cheating (percentage points)")
    fig.suptitle("What a summary asserts or drops matters more than the method",
                 x=0.01, ha="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "compaction_levers.png", dpi=200)


if __name__ == "__main__":
    rewrite_vs_delete()
    arms()
    detection()
    compaction_levers()
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))
