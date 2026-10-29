"""Experiment 5 (summary fix), PROVISIONAL deck chart: teammate's commit lost in task 2, by arm.

Reads the interim readout copied from sprint/week2-build-b 60aa252a
(results/sprint/week2/summary_fix/interim_readout.md) into
docs/sprint/figs/week2/data/summary_fix/interim_readout.md, whose sha256 is checked below.
Plain seaborn defaults and no captions in the image (AGENTS.md "Figures"); caveats go in the
README section this script writes. Run: uv run python scripts/sprint_fig_exp5_interim.py
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

SRC = ROOT / "docs/sprint/figs/week2/data/summary_fix/interim_readout.md"
SRC_SHA = "81a05fb623fedcedd806afe19850e6eb75c6e5c4cb21a8927d8acff758920c12"
SRC_REF = "sprint/week2-build-b 60aa252a, results/sprint/week2/summary_fix/interim_readout.md"
OUT = ROOT / "docs/sprint/figs/week2"
NAME = "08_summary_fix_interim"
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
    assert hashlib.sha256(raw).hexdigest() == SRC_SHA, "interim readout changed; re-copy and re-hash"
    text = raw.decode()
    m = re.search(r"Slots: (\d+); compaction failed (\d+), done (\d+)", text)
    slots, done = int(m.group(1)), int(m.group(3))
    lost = {r["arm"]: (int(r["teammate lost"]), int(r["done"]))
            for r in table(text, "## Teammate lost, per arm")}
    ends = {r["arm"]: r for r in table(text, "## How the finished slots ended")}
    paired = {r["arm"]: r for r in table(text, "## Paired with native")}
    assert set(lost) == set(ARMS) and sum(n for _, n in lost.values()) == done
    return slots, done, lost, ends, paired


def main():
    slots, done, lost, ends, paired = load()
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
    title = f"PROVISIONAL ({done} of {slots} runs): teammate's commit lost, by summary instruction"
    ax.set_title(title)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"{NAME}.{ext}", dpi=200, bbox_inches="tight",
                    metadata={"Date": None} if ext == "svg" else None)
    plt.close(fig)

    lines = [f"## {NAME}: {title}", "", f"![{NAME}]({NAME}.png)", "",
             (f"**Source:** `{SRC.relative_to(ROOT)}`, copied from {SRC_REF} "
              f"(sha256 {SRC_SHA[:12]}…); tables 'Teammate lost, per arm', 'How the finished "
              "slots ended' and 'Paired with native'."), "",
             "**Numbers plotted** (lost/done, Wilson 95%):"]
    for key, lab in ARMS.items():
        k, n = lost[key]
        lo, hi = wilson(k, n)
        lines.append(f"- {lab.replace(chr(10), ' ')}: {k}/{n} = {100 * k / n:.1f}% "
                     f"[{100 * lo:.0f}, {100 * hi:.0f}]")
    lines += ["", "**Not on the chart (slide notes):**",
              (f"- Provisional: {done} of {slots} slots, a nonrandom subset shaped by launch "
               "order, runtime and failures; no statistical test yet."),
              "- No capped run lost the commit in this interim sample (a capped run may not have pushed "
               "at all), so 'not lost' doesn't establish a decision to keep it. Submitted runs only: "
              + "; ".join(f"{ARMS[a].replace(chr(10), ' ')} {ends[a]['lost, of submitted']}/"
                          f"{ends[a]['submitted']} ({ends[a]['ended at a cap']} capped)"
                          for a in ARMS) + ".",
              "- Paired with native (same history and replicate, both finished with known "
              "outcomes; lost in the arm only vs in native only): "
              + "; ".join(f"{ARMS[a].replace(chr(10), ' ')} {paired[a]['lost in arm only']} vs "
                          f"{paired[a]['lost in native only']} of {paired[a]['pairs']} pairs"
                          for a in ARMS if a != "native")
              + ".", ""]
    readme = OUT / "README_summary_fix.md"
    readme.write_text("# Experiment 5 (summary fix): provisional chart\n\nGenerated by "
                      "`scripts/sprint_fig_exp5_interim.py`.\n\n" + "\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
