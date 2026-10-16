"""Compaction levers chart: change in unsafe actions or cheating, with 95% CIs.

Sources (copied, not recomputed):
- Ops agent (P38 note): compaction S3 vs full history, DeepSeek +20.7 [+6.1, +35.4]; deleting the agent's
  permission-and-plan sentences −23.1 [−37.2, −9.0]; inserting the owner line +14.6 [+2.4, +26.8];
  deleting the live fact +6.1 [−7.3, +19.5] DeepSeek, +1.4 [0.0, +4.3] GLM; false "test sandbox" line
  +53.7 [+40.2, +67.1] DeepSeek, +38.6 [+25.7, +51.4] GLM.
- Coding agent, dropped rule (P08 note, clause-only deletion): DeepSeek +18.8 [+10.8, +27.3], GLM
  +11.5 [+3.7, +19.8].
- Coding agent, handoffs (P44 note): one summary then the full history vs none, +20.0 [+4.4, +35.6].
- Product scale (P49 note): pooled level-A contrasts vs no compaction (Codex +6.2 [−27.8, +41.2], pi +11.8
  [−19.0, +42.9], Inspect +0.0 [−31.2, +33.3]); repeated-compaction slope, bulk primary, +12.5 per
  compaction [+0.0, +25.0]; long history vs fresh start, pooled, −47.1 [−77.8, −12.5].
Run: uv run python docs/pitch/fig_compaction.py
"""
from pathlib import Path

import matplotlib.pyplot as plt

OUT = Path(__file__).parent / "fig"
plt.rcParams.update({"font.size": 10.5, "axes.spines.top": False, "axes.spines.right": False})
COL = {"compact": "#DD8452", "delete": "#4C72B0", "insert": "#C44E52", "history": "#8172B3"}
LAB = {"compact": "Compaction itself", "delete": "A line dropped from the summary",
       "insert": "A line inserted into the summary", "history": "Keeping the long history"}

SMALL = [  # label, kind, value, ci
    ("Compaction\n3 in a row\nops · DeepSeek", "compact", 20.7, (6.1, 35.4)),
    ("One summary,\nthen history\ncoding · GLM", "compact", 20.0, (4.4, 35.6)),
    ("Drop agent's\nown plan lines\nops · DeepSeek", "delete", -23.1, (-37.2, -9.0)),
    ("Drop \"don't\nedit tests\"\ncoding · DeepSeek", "delete", 18.8, (10.8, 27.3)),
    ("Drop \"don't\nedit tests\"\ncoding · GLM", "delete", 11.5, (3.7, 19.8)),
    ("Drop \"live\nsystem\" fact\nops · DeepSeek", "delete", 6.1, (-7.3, 19.5)),
    ("Drop \"live\nsystem\" fact\nops · GLM", "delete", 1.4, (0.0, 4.3)),
    ("Insert \"go on\nwithout owner\"\nops · DeepSeek", "insert", 14.6, (2.4, 26.8)),
    ("Insert false\n\"test sandbox\"\nops · DeepSeek", "insert", 53.7, (40.2, 67.1)),
    ("Insert false\n\"test sandbox\"\nops · GLM", "insert", 38.6, (25.7, 51.4)),
]
LARGE = [
    ("Codex-style\nvs none", "compact", 6.2, (-27.8, 41.2)),
    ("pi-style\nvs none", "compact", 11.8, (-19.0, 42.9)),
    ("Inspect-style\nvs none", "compact", 0.0, (-31.2, 33.3)),
    ("Each extra\ncompaction\n(1 → 3)", "compact", 12.5, (0.0, 25.0)),
    ("Long history\nvs fresh start", "history", -47.1, (-77.8, -12.5)),
]


def panel(ax, items, title, ylim):
    for i, (lab, kind, v, ci) in enumerate(items):
        ax.bar(i, v, 0.7, color=COL[kind])
        ax.errorbar(i, v, yerr=[[v - ci[0]], [ci[1] - v]], color="#333333", capsize=3, lw=1)
        y = ci[1] + 2 if v >= 0 else ci[0] - 2
        ax.text(i, y, f"{v:+.1f}", ha="center", va="bottom" if v >= 0 else "top", fontsize=9)
    ax.axhline(0, color="#555555", lw=1)
    ax.set_xticks(range(len(items)), [it[0] for it in items], fontsize=8.2)
    ax.set_ylim(*ylim)
    ax.set_title(title, loc="left", fontsize=11.5)


def main():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(18, 6.8), gridspec_kw={"width_ratios": [2, 1]})
    panel(a1, SMALL, "Short sessions (5–20k tokens, open flash models): what the summary says", (-45, 75))
    a1.set_ylabel("change in unsafe actions or cheating\n(percentage points)")
    panel(a2, LARGE, "Product scale (~200k tokens, Qwen3-Coder, coding)", (-85, 50))
    handles = [plt.Rectangle((0, 0), 1, 1, color=COL[k]) for k in LAB]
    fig.legend(handles, list(LAB.values()), loc="upper left", ncol=4, frameon=False, fontsize=10,
               bbox_to_anchor=(0.005, 0.93))
    fig.suptitle("What the summary says moves behaviour by up to 54 points. At ~200k tokens, compaction "
                 "itself is unresolved; repeated compaction leans up.", x=0.01, y=0.99, ha="left",
                 fontsize=13)
    fig.text(0.01, 0.01,
             "Error bars: 95% CI. Product-scale method contrasts pool 24 histories (exploratory); the "
             "repeated-compaction slope uses the 16 planned histories, and an alternative scoring rule "
             "halves it.\nThe history effect may partly reflect the old task's \"don't modify the tests\" "
             "line carried in the history.", fontsize=8.6, color="#555555")
    fig.tight_layout(rect=(0, 0.05, 1, 0.88))
    fig.savefig(OUT / "compaction_levers_v2.png", dpi=200)


if __name__ == "__main__":
    main()
