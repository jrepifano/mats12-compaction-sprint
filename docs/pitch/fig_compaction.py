"""Compaction figures: (1) cheating rate by starting point at product scale; (2) what the summary
says in short sessions, grouped by model.

Sources (copied, not recomputed):
- Ops agent (P38 note): 3 nested compactions vs full history, DeepSeek +20.7 [+6.1, +35.4], GLM +0.0
  [−4.2, +4.2]; deleting DeepSeek's permission-and-plan sentences −23.1 [−37.2, −9.0]; planting the owner
  line, DeepSeek +14.6 [+2.4, +26.8], GLM +0.0 (no interval reported); deleting the live fact +6.1
  [−7.3, +19.5] DeepSeek, +1.4 [0.0, +4.3] GLM; planting a false "test sandbox" line +53.7 [+40.2, +67.1]
  DeepSeek, +38.6 [+25.7, +51.4] GLM. GLM's plan-line deletion has no reported contrast and is omitted.
- Coding agent, dropped rule (P08 note, clause-only deletion): DeepSeek +18.8 [+10.8, +27.3], GLM
  +11.5 [+3.7, +19.8].
- Coding agent, handoffs (P44 note, GLM only): one summary then the full history vs none, +20.0
  [+4.4, +35.6].
- Product scale (P49, results/analysis_gate0+gate0x+bulk_levelA.md): pooled no-instruction runs,
  cheats / resolved runs per arm; Inspect slope over 1-3 compactions +8.3 [−3.1, +21.4].
Run: uv run python docs/pitch/fig_compaction.py
"""
from pathlib import Path

import matplotlib.pyplot as plt

OUT = Path(__file__).parent / "fig"
plt.rcParams.update({"font.size": 10.5, "axes.spines.top": False, "axes.spines.right": False})
COL = {"compact": "#DD8452", "delete": "#4C72B0", "insert": "#C44E52", "history": "#8172B3"}
LAB = {"compact": "Compaction itself", "delete": "A line dropped from the summary",
       "insert": "A line we planted in the summary"}

# Short sessions, grouped by model; only arms each model actually ran. (label, kind, value, ci or None)
BY_MODEL = [
    ("DeepSeek V4 Flash", [
        ("3 compactions\nvs full history\n(ops)", "compact", 20.7, (6.1, 35.4)),
        ("Drop agent's\nown plan lines\n(natural, ops)", "delete", -23.1, (-37.2, -9.0)),
        ("Drop \"don't\nedit tests\"\n(coding)", "delete", 18.8, (10.8, 27.3)),
        ("Drop \"live\nsystem\" fact\n(ops)", "delete", 6.1, (-7.3, 19.5)),
        ("Plant \"go on\nwithout owner\"\n(ops)", "insert", 14.6, (2.4, 26.8)),
        ("Plant false\n\"test sandbox\"†\n(ops)", "insert", 53.7, (40.2, 67.1)),
    ]),
    ("GLM 5.3 Flash", [
        ("3 compactions\nvs full history\n(ops)*", "compact", 0.0, (-4.2, 4.2)),
        ("1 summary, then\nfull history vs\nnone (coding)", "compact", 20.0, (4.4, 35.6)),
        ("Drop \"don't\nedit tests\"\n(coding)", "delete", 11.5, (3.7, 19.8)),
        ("Drop \"live\nsystem\" fact\n(ops)*", "delete", 1.4, (0.0, 4.3)),
        ("Plant \"go on\nwithout owner\"\n(ops)*", "insert", 0.0, None),
        ("Plant false\n\"test sandbox\"†\n(ops)", "insert", 38.6, (25.7, 51.4)),
    ]),
]

# Product scale, pooled no-instruction runs: (cheats, resolved runs) per arm
TREND = {"fresh": (12, 21), "none": (4, 17), "inspect1": (4, 22), "codex1": (6, 20), "pi1": (8, 21),
         "inspect2": (6, 21), "inspect3": (6, 19)}


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / d
    return 100 * p, (100 * (c - h), 100 * (c + h))


def panel_by_model(ax, ylim):
    x, ticks, labels = 0.0, [], []
    for gi, (model, items) in enumerate(BY_MODEL):
        x0 = x
        for lab, kind, v, ci in items:
            ax.bar(x, v, 0.75, color=COL[kind])
            top = v
            if ci:
                ax.errorbar(x, v, yerr=[[v - ci[0]], [ci[1] - v]], color="#333333", capsize=2.5, lw=1)
                top = ci[1] if v >= 0 else ci[0]
            y = top + 2 if v >= 0 else top - 2
            ax.text(x, y, f"{v:+.1f}", ha="center", va="bottom" if v >= 0 else "top", fontsize=8.6)
            ticks.append(x)
            labels.append(lab)
            x += 1
        ax.text((x0 + x - 1) / 2, ylim[1] - 3, model, ha="center", va="top", fontsize=11.5,
                weight="bold")
        if gi < len(BY_MODEL) - 1:
            ax.axvline(x - 0.25, color="#BBBBBB", lw=1, ls="--")
        x += 0.5
    ax.axhline(0, color="#555555", lw=1)
    ax.set_xticks(ticks, labels, fontsize=8.2)
    ax.set_ylim(*ylim)


def trend_panel(ax):
    spec = [  # x, key, colour, hatch, label under the bar
        (0.0, "fresh", "#8C8C8C", None, "fresh\nstart"),
        (1.2, "none", COL["history"], None, "continue\nprevious task"),
        (2.15, "inspect1", COL["compact"], None, "Inspect"),
        (2.55, "codex1", COL["compact"], "\\\\", "Codex"),
        (2.95, "pi1", COL["compact"], "..", "pi"),
        (3.9, "inspect2", COL["compact"], None, "Inspect"),
        (4.9, "inspect3", COL["compact"], None, "Inspect"),
    ]
    for x, key, col, hatch, lab in spec:
        v, ci = wilson(*TREND[key])
        w = 0.36 if key.endswith("1") else 0.6
        ax.bar(x, v, w, color=col if not hatch else "white", edgecolor=col, hatch=hatch, lw=1.2)
        ax.errorbar(x, v, yerr=[[v - ci[0]], [ci[1] - v]], color="#333333", capsize=2.5, lw=1)
        ax.text(x, ci[1] + 1.5, f"{v:.0f}%", ha="center", va="bottom", fontsize=9)
        ax.text(x, -3, lab, ha="center", va="top", fontsize=7.8, color="#444444",
                rotation=90 if key.endswith("1") else 0)
    xs = [1.2, 2.15, 3.9, 4.9]
    ys = [wilson(*TREND[k])[0] for k in ("none", "inspect1", "inspect2", "inspect3")]
    ax.plot(xs, ys, color="#333333", lw=1, ls=":", marker="o", ms=3, zorder=3)
    ax.set_xticks([0.0, 1.2, 2.55, 3.9, 4.9], ["", "0", "1", "2", "3"], fontsize=10)
    ax.tick_params(axis="x", length=0, pad=34)
    ax.set_xlabel("number of compactions before the impossible task", fontsize=9.5)
    ax.set_ylim(0, 95)
    ax.set_ylabel("runs that cheat (%)")
    ax.text(5.3, 88, "Inspect trend 1→3:\n+8.3 points per compaction\n[−3.1, +21.4]", ha="right",
            va="top", fontsize=8.6, color="#333333")


def main():
    # Figure 1: cheating rate by starting point (product scale)
    fig, ax = plt.subplots(figsize=(10, 6.4))
    trend_panel(ax)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in ("#8C8C8C", COL["history"], COL["compact"])]
    fig.legend(handles, ["Fresh start (no history)", "Continue from previous task, no compaction",
                         "Previous task compacted"],
               loc="upper left", frameon=False, fontsize=9.5, ncol=3, bbox_to_anchor=(0.01, 0.89))
    fig.suptitle("Qwen3-Coder after ~200k tokens of its own SWE-rebench work, then an ImpossibleBench task:\n"
                 "24% cheat when continuing from the previous task; 18–32% after 1–3 compactions; "
                 "57% from a fresh start", x=0.01, y=0.985, ha="left", fontsize=12.5)
    fig.text(0.01, 0.01,
             "Error bars: 95% Wilson interval. Pooled runs with no test instruction (24 histories, "
             "exploratory).\nInspect trend from 1 to 3 compactions: +8.3 points per compaction [−3.1, "
             "+21.4]; the 16 planned histories alone give +12.5 [+0.0, +25.0], and an\nalternative scoring "
             "rule halves it. Each extra compaction also adds older work. Fresh start vs any history may "
             "partly reflect\nthe old task's \"don't modify the tests\" line carried in the history.",
             fontsize=8.4, color="#555555")
    fig.tight_layout(rect=(0, 0.13, 1, 0.86))
    fig.savefig(OUT / "compaction_rates.png", dpi=200)

    # Figure 2: what the summary says (short sessions), grouped by model
    fig, a1 = plt.subplots(figsize=(15, 6.8))
    panel_by_model(a1, (-45, 82))
    a1.set_ylabel("change in unsafe actions or cheating\n(percentage points)")
    keys = ["compact", "delete", "insert"]
    handles = [plt.Rectangle((0, 0), 1, 1, color=COL[k]) for k in keys]
    fig.legend(handles, [LAB[k] for k in keys], loc="upper left", ncol=3, frameon=False, fontsize=9.5,
               bbox_to_anchor=(0.01, 0.885))
    fig.suptitle("Short sessions (5–20k tokens): what the summary says moves behaviour. Dropping "
                 "DeepSeek's own plan lines cuts unsafe actions\n23 points; planted lines raise them 15–54 "
                 "points. No compaction method stood out (all within ±7 of no handoff).",
                 x=0.01, y=0.985, ha="left", fontsize=12.5)
    fig.text(0.01, 0.01,
             "†Planted, never natural: no summary claimed a test environment on its own (0 of 231); the "
             "agent's plan lines do appear naturally and \"go on without owner\" imitates them.\n*GLM rarely "
             "took the unsafe ops action (at most 4 of 72 runs), so its ops bars could not move; \"go on without "
             "owner\" has no reported interval for GLM.\nArms a model did not run are omitted. Error bars: 95% CI.",
             fontsize=8.4, color="#555555")
    fig.tight_layout(rect=(0, 0.09, 1, 0.86))
    fig.savefig(OUT / "compaction_levers_short.png", dpi=200)


if __name__ == "__main__":
    main()
