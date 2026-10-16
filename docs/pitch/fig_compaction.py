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
       "insert": "A line we planted in the summary", "history": "Keeping the long history"}

# lever label, kind, DeepSeek (value, ci) or a note, GLM (value, ci) or a note
SMALL = [
    ("Compaction,\n3 in a row\n(ops)", "compact", (20.7, (6.1, 35.4)), (0.0, (-4.2, 4.2))),
    ("One summary,\nthen history\n(coding)", "compact", "not run*", (20.0, (4.4, 35.6))),
    ("Drop agent's own\nplan lines,\nnatural (ops)", "delete", (-23.1, (-37.2, -9.0)), "at floor*"),
    ("Drop \"don't edit\ntests\" rule\n(coding)", "delete", (18.8, (10.8, 27.3)), (11.5, (3.7, 19.8))),
    ("Drop \"live\nsystem\" fact\n(ops)", "delete", (6.1, (-7.3, 19.5)), (1.4, (0.0, 4.3))),
    ("Plant \"go on\nwithout owner\"\n(ops)", "insert", (14.6, (2.4, 26.8)), "at floor*"),
    ("Plant false\n\"test sandbox\"\u2020\n(ops)", "insert", (53.7, (40.2, 67.1)), (38.6, (25.7, 51.4))),
]
LARGE = [
    ("Codex-style\nvs none", "compact", 6.2, (-27.8, 41.2)),
    ("pi-style\nvs none", "compact", 11.8, (-19.0, 42.9)),
    ("Inspect-style\nvs none", "compact", 0.0, (-31.2, 33.3)),
    ("Each extra\ncompaction\n(1 → 3)", "compact", 12.5, (0.0, 25.0)),
    ("Long history\nvs fresh start", "history", -47.1, (-77.8, -12.5)),
]


def bar(ax, x, v, ci, color, hatch=None, w=0.36):
    ax.bar(x, v, w, color=color if not hatch else "white", edgecolor=color, hatch=hatch, lw=1.2)
    ax.errorbar(x, v, yerr=[[v - ci[0]], [ci[1] - v]], color="#333333", capsize=2.5, lw=1)
    y = ci[1] + 2 if v >= 0 else ci[0] - 2
    ax.text(x, y, f"{v:+.1f}", ha="center", va="bottom" if v >= 0 else "top", fontsize=8.6)


def panel_models(ax, items, title, ylim):
    for i, (lab, kind, ds, glm) in enumerate(items):
        for dx, val, hatch in ((-0.19, ds, None), (0.19, glm, "///")):
            if isinstance(val, str):
                ax.text(i + dx, 1.5, val, ha="center", va="bottom", fontsize=7.6, color="#777777",
                        rotation=90)
            else:
                bar(ax, i + dx, val[0], val[1], COL[kind], hatch)
    ax.axhline(0, color="#555555", lw=1)
    ax.set_xticks(range(len(items)), [it[0] for it in items], fontsize=8.4)
    ax.set_ylim(*ylim)
    ax.set_title(title, loc="left", fontsize=11.5)


def panel(ax, items, title, ylim):
    for i, (lab, kind, v, ci) in enumerate(items):
        bar(ax, i, v, ci, COL[kind], w=0.7)
    ax.axhline(0, color="#555555", lw=1)
    ax.set_xticks(range(len(items)), [it[0] for it in items], fontsize=8.4)
    ax.set_ylim(*ylim)
    ax.set_title(title, loc="left", fontsize=11.5)


def main():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(18, 7.2), gridspec_kw={"width_ratios": [2, 1]})
    panel_models(a1, SMALL, "Short sessions (5–20k tokens): what the summary says (solid DeepSeek, hatched GLM)", (-45, 75))
    a1.set_ylabel("change in unsafe actions or cheating\n(percentage points)")
    panel(a2, LARGE, "Product scale (~200k tokens, Qwen3-Coder, coding)", (-85, 50))
    handles = [plt.Rectangle((0, 0), 1, 1, color=COL[k]) for k in LAB]
    handles += [plt.Rectangle((0, 0), 1, 1, fc="#999999"), plt.Rectangle((0, 0), 1, 1, fc="white", ec="#999999", hatch="///")]
    fig.legend(handles, list(LAB.values()) + ["DeepSeek V4 Flash", "GLM 5.3 Flash"], loc="upper left", ncol=6, frameon=False, fontsize=10,
               bbox_to_anchor=(0.005, 0.905))
    fig.suptitle("What the summary says moves behaviour: dropping the agent's own plan lines cuts unsafe "
                 "actions 23 points; a planted false line raises them up to 54.\nAt ~200k tokens, compaction "
                 "itself is unresolved; repeated compaction leans up.", x=0.01, y=0.99, ha="left",
                 fontsize=13)
    fig.text(0.01, 0.01,
             "\u2020Planted, never natural: no summary claimed a test environment on its own (0 of 231); the agent's plan "
             "lines do appear naturally and \"go on without owner\" imitates them.\n*GLM almost never took the unsafe ops "
             "action (at most 4 of 72), so it could not move; DeepSeek's coding pilot never cheated, so that arm was not run.\nError bars: 95% CI. Product-scale method contrasts pool 24 histories (exploratory); the "
             "repeated-compaction slope uses the 16 planned histories, and an alternative scoring rule "
             "halves it.\nThe history effect may partly reflect the old task's \"don't modify the tests\" "
             "line carried in the history.", fontsize=8.6, color="#555555")
    fig.tight_layout(rect=(0, 0.1, 1, 0.86))
    fig.savefig(OUT / "compaction_levers_v2.png", dpi=200)


if __name__ == "__main__":
    main()
