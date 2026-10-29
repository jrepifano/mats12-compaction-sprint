"""Experiment 5 (summary fix), deck chart: teammate's commit lost in task 2, by arm.

Reads the final readout copied from sprint/week2-build-b 98f31570
(results/sprint/week2/summary_fix/final_readout.md) into
docs/sprint/figs/week2/data/summary_fix/final_readout.md, whose sha256 is checked below.
Plain seaborn defaults and no captions in the image (AGENTS.md "Figures"); caveats go in the
README section this script writes. Run: uv run python scripts/sprint_fig_exp5.py
"""

import hashlib
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from sprint_figs_week2 import wilson

SRC = ROOT / "docs/sprint/figs/week2/data/summary_fix/final_readout.md"
SRC_SHA = "9edd91e7151f7436e80badae8878d71d2d6b716e06e7833b91ace8f00fd2312d"
SRC_REF = "sprint/week2-build-b 98f31570, results/sprint/week2/summary_fix/final_readout.md"
OUT = ROOT / "docs/sprint/figs/week2"
NAME = "08_summary_fix_final"
ARMS = {  # readout key -> label on the chart, in plotting order (native first)
    "native": "pi's native\nsummary",
    "neutral_opening": "Neutral\nopening",
    "line": "Claude Code's\napproval line",
    "placebo": "Placebo\ninstruction",
    "ledger": "Authorization\nledger",
}


def table(text, heading):
    """Rows of the markdown table under `heading`, as dicts keyed by the header cells."""
    block = text.split(heading, 1)[1].splitlines()
    start = next(i for i, ln in enumerate(block) if ln.startswith("|"))
    lines = []
    for ln in block[start:]:  # the first table only: stop at its last row
        if not ln.startswith("|"):
            break
        lines.append(ln)
    head = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        rows.append(dict(zip(head, cells)))
    return rows


def load():
    raw = SRC.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SRC_SHA, "final readout changed; re-copy and re-hash"
    text = raw.decode()
    per_arm = table(text, "## Teammate's commit lost, per arm")
    lost = {r["arm"]: (int(r["lost"]), int(r["known"])) for r in per_arm}
    slots = sum(int(r["assigned"]) for r in per_arm)
    done = sum(n for _, n in lost.values())
    ends = {r["arm"]: r for r in table(text, "## How the runs ended")}
    paired = {r["arm"]: r for r in table(text, "## Paired with native: where the outcomes differ")}
    assert set(lost) == set(ARMS)
    contrasts = [r for h in ("## Each fix against native", "## Ledger against placebo")
                 for r in table(text, h) if r["unknowns"] == "known runs only"]
    return slots, done, lost, ends, paired, contrasts


def main():
    slots, done, lost, ends, paired, contrasts = load()
    sns.set_theme(style="whitegrid")
    pal = sns.color_palette()
    fig, ax = plt.subplots(figsize=(9, 5.4))
    colors = [pal[7]] + [pal[i] for i in range(len(ARMS) - 1)]  # native grey, arms coloured
    for i, (key, lab) in enumerate(ARMS.items()):
        k, n = lost[key]
        p, lo, hi = k / n, *wilson(k, n)
        ax.bar(i, 100 * p, color=colors[i])
        ax.errorbar(i, 100 * p, yerr=[[100 * (p - lo)], [100 * (hi - p)]], fmt="none",
                    ecolor="0.25", lw=2)
        ax.text(i, 100 * hi + 2, f"{k}/{n}", ha="center", va="bottom")
    ax.set_xticks(range(len(ARMS)), list(ARMS.values()))
    ax.xaxis.grid(False)  # as sns.barplot draws it: no grid on the category axis
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_ylabel("Task-2 runs that lost the teammate's commit (%)")
    title = f"Teammate's commit lost, by summary instruction ({done} of {slots} runs known)"
    ax.set_title(title)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"{NAME}.{ext}", dpi=200, bbox_inches="tight",
                    metadata={"Date": None} if ext == "svg" else None)
    plt.close(fig)

    lines = [f"## {NAME}: {title}", "", f"![{NAME}]({NAME}.png)", "",
             (f"**Source:** `{SRC.relative_to(ROOT)}`, copied from {SRC_REF} "
              f"(sha256 {SRC_SHA[:12]}…); tables 'Teammate's commit lost, per arm', 'How the runs "
              "ended', 'Paired with native: where the outcomes differ', 'Each fix against native' and "
              "'Ledger against placebo'."), "",
             "**Numbers plotted** (lost/done, Wilson 95%):"]
    for key, lab in ARMS.items():
        k, n = lost[key]
        lo, hi = wilson(k, n)
        lines.append(f"- {lab.replace(chr(10), ' ')}: {k}/{n} = {100 * k / n:.1f}% "
                     f"[{100 * lo:.0f}, {100 * hi:.0f}]")
    lines += ["", "**Not on the chart (slide notes):**",
              (f"- Final: {done} of {slots} runs have a known outcome; the other "
               f"{slots - done} failed compaction on both rounds and is a final unknown. The tests "
               "are in the readout's 'Each fix against native' section."),
              "- No capped run lost the commit in this sample (a capped run may not have pushed "
               "at all), so 'not lost' doesn't establish a decision to keep it. Submitted runs only: "
              + "; ".join(f"{ARMS[a].replace(chr(10), ' ')} {ends[a]['lost, of submitted']}/"
                          f"{ends[a]['submitted']} ({ends[a]['ended at a cap']} capped)"
                          for a in ARMS) + ".",
              "- Paired with native (same history and replicate, both finished with known "
              "outcomes; lost in the arm only vs in native only): "
              + "; ".join(f"{ARMS[a].replace(chr(10), ' ')} {paired[a]['lost in arm only']} vs "
                          f"{paired[a]['lost in native only']} of {paired[a]['pairs']} pairs"
                          for a in ARMS if a != "native")
              + ".",
              "- Contrasts, known runs only (points; history-cluster bootstrap 95%; exact sign-flip "
              "p, unadjusted): "
              + "; ".join(f"{r.get('fix') or r.get('contrast')} {r['difference (pts)']} "
                          f"{r['95% CI']}, {r['histories nonzero']} histories differ, p {r['p']}"
                          for r in contrasts)
              + ". The readout's Holm step does not reject any fix at 0.05.", ""]
    readme = OUT / "README_summary_fix.md"
    readme.write_text("# Experiment 5 (summary fix): chart, final readout\n\nGenerated by "
                      "`scripts/sprint_fig_exp5.py`.\n\n" + "\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
