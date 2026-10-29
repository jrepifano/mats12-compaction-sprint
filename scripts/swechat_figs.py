"""Deck figure for the SWE-chat completion-claim scan: the false-"done" rate by session model.

One chart, drawn in the v2 deck's warm style (the rcParams, rounded bars and error bars are copied
from scripts/sprint_figs_v2.py in the sprint/prep worktree, which Jacob approved as an exception to
AGENTS.md "Figures" on style and palette for that deck, 26 Oct). The rest of that rule applies:
the title states the claim with its number, plain labels, Wilson 95% intervals, n under each bar,
every number read from a committed file.

Source: results/sprint/swechat_odd/scan_summary.json (final.per_compaction and
by_model.<group>.per_compaction; k, n, rate and the Wilson lo/hi). The Wilson bounds are recomputed
here with the scan's own function and checked against the file.
Writes docs/sprint/figs/swechat_odd/*.png, *.svg and README.md.
Run: uv run python scripts/swechat_figs.py
"""

import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from compaction.s2.gate import wilson  # the scan's own interval

SRC = "results/sprint/swechat_odd/scan_summary.json"
OUT = ROOT / "docs" / "sprint" / "figs" / "swechat_odd"
INK, MUTED, GRID, EDGE = "#2b2926", "#7a736a", "#e8e2d8", "#b8afa3"
# Overall in dark warm grey; each model group its own colour from the v2 palette.
GROUPS = [
    ("Opus 4.6", "Opus 4.6", "#6c8eb5"),
    ("Opus 4.5", "Opus 4.5", "#6aa3a0"),
    ("Sonnet 4.6", "Sonnet 4.6", "#7fa37a"),
    ("Sonnet 4.5", "Sonnet 4.5", "#9c7ba0"),
    ("other", "Other models", "#8c6d55"),
]
OVERALL = "#5a524a"
LABEL = "Opus 5.5's read, not hand-checked"
plt.rcParams.update({
    "font.family": ["Avenir Next", "Helvetica Neue", "Arial"],
    "font.size": 12,
    "text.color": INK,
    "axes.labelcolor": MUTED,
    "xtick.color": INK,
    "ytick.color": MUTED,
    "axes.edgecolor": EDGE,
    "axes.facecolor": "white",
    "figure.facecolor": "white",
    "axes.grid": True,
    "axes.grid.axis": "y",
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "svg.fonttype": "none",
})  # fmt: skip


def rbar(ax, x, h, w, color):
    """A bar with rounded top corners (from sprint_figs_v2.py)."""
    ax.bar(x, h, width=w, color="none", zorder=1)
    bb = ax.get_window_extent()
    (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
    aspect = ((y1 - y0) / bb.height) / ((x1 - x0) / bb.width)
    r = min(0.05, w / 3, h / aspect) if h > 0 else 0
    ax.add_patch(FancyBboxPatch((x - w / 2, -r * aspect), w, h + r * aspect,
                                boxstyle=f"round,pad=0,rounding_size={r}", mutation_aspect=aspect,
                                facecolor=color, edgecolor="none", zorder=2))  # fmt: skip


def rows() -> list[dict]:
    s = json.loads((ROOT / SRC).read_text())
    assert not s["provisional"] and s["pending_recheck"] == 0, "summary is provisional"
    out = [("final.per_compaction", "All compactions", OVERALL, s["final"]["per_compaction"])]
    out += [(f"by_model.{g}.per_compaction", lab, col, s["by_model"][g]["per_compaction"])
            for g, lab, col in GROUPS]  # fmt: skip
    for f in ("k", "n"):
        assert sum(r[3][f] for r in out[1:]) == out[0][3][f], f"model groups' {f} != the total"
    res = []
    for field, lab, col, pc in out:
        lo, hi = wilson(pc["k"], pc["n"])
        assert math.isclose(lo, pc["lo"], abs_tol=1e-9) and math.isclose(hi, pc["hi"], abs_tol=1e-9)
        assert math.isclose(pc["rate"], pc["k"] / pc["n"], abs_tol=1e-12)
        res.append({"field": field, "lab": lab, "col": col, "k": pc["k"], "n": pc["n"],
                    "v": pc["rate"], "lo": pc["lo"], "hi": pc["hi"]})  # fmt: skip
    return res


def main() -> None:
    rs = rows()
    xs = [0] + [i + 1.4 for i in range(len(rs) - 1)]  # a gap between the total and the models
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.76, bottom=0.17)
    ax.set_ylim(0, 40)
    ax.set_xlim(-0.6, xs[-1] + 0.6)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{r['lab']}\nn = {r['n']:,}" for r in rs], fontsize=11.5,
                       linespacing=1.2)  # fmt: skip
    ax.tick_params(axis="x", length=0, pad=8)
    ax.tick_params(axis="y", length=0)
    ax.set_ylabel('Compactions with a false "done" line (%)', fontsize=11.5)
    ax.set_axisbelow(True)
    for x, r in zip(xs, rs, strict=True):
        v, lo, hi = (100 * r[f] for f in ("v", "lo", "hi"))
        rbar(ax, x, v, 0.62, r["col"])
        ax.errorbar(x, v, yerr=[[v - lo], [hi - v]], fmt="none", ecolor=INK, elinewidth=1.1,
                    capsize=3, zorder=3)  # fmt: skip
        ax.annotate(f"{v:.0f}%", (x, hi), xytext=(0, 4), textcoords="offset points", ha="center",
                    va="bottom", fontsize=11, color=INK,
                    fontweight="bold" if x == 0 else "normal")  # fmt: skip
    tot = rs[0]
    title = (f"About 1 in 10 real SWE-chat compactions ({100 * tot['v']:.0f}%) has a \"done\" claim\n"
             "that Opus 5.5 finds the session's own history contradicts")  # fmt: skip
    assert round(100 * tot["v"]) == 10, "the title says about 1 in 10"
    fig.suptitle(title, x=0.02, y=0.97, ha="left", va="top", fontsize=16, fontweight="bold",
                 color=INK)  # fmt: skip
    fig.text(0.02, 0.825, f"SWE-chat: real users' coding-agent sessions, summarised by the "
             f"session's own model.  {LABEL}.", fontsize=11, color=MUTED, ha="left")  # fmt: skip
    OUT.mkdir(parents=True, exist_ok=True)
    name = "false_done_by_model"
    fig.savefig(OUT / f"{name}.png", dpi=200)
    fig.savefig(OUT / f"{name}.svg")
    plt.close(fig)
    readme = [
        '# SWE-chat false-"done" figure',
        "",
        (
            "Generated by `scripts/swechat_figs.py` (`uv run python scripts/swechat_figs.py`); every "
            "number is read from the committed file below at run time. PNG (200 dpi) and SVG. Style: "
            "the v2 deck's warm look (from `scripts/sprint_figs_v2.py`, Jacob's exception to AGENTS.md "
            '"Figures" for that deck).'
        ),
        "",
        f"## {name}: {title.replace(chr(10), ' ')}",
        "",
        f"![{name}]({name}.png)",
        "",
        (
            "**Claim:** share of SWE-chat compactions whose summary has at least one completion "
            "claim judged false, overall and by the session's model. A completion claim is a "
            "summary line matched by the scan's keyword pre-filter (done, fixed, tests pass and "
            "similar; `RX` in `scripts/swechat_scan.py`) that Opus 5.5 read as saying "
            "work was completed; the numerator counts compactions with at least one such line whose "
            f'final verdict is "false". {LABEL}: Opus 5.5 (`claude -p` on Max, effort low) read '
            "each tagged line against the pre-compaction history, and a second Opus 5.5 pass "
            'rechecked every "false" call and had to quote the contradicting history; a call it '
            "could not back with a quote counts as can't tell, and can't tell never counts as false. "
            "One false line is enough: it may be about a single fix or test run, not the whole task."
        ),
        "",
        f"**Source:** `{SRC}` at commit c1c224c8 (fields listed per bar).",
        "",
        "**Numbers plotted** (k/n, rate, Wilson 95% interval):",
        "",
    ]
    readme += [f"- {r['lab']}: {r['k']}/{r['n']} = {100 * r['v']:.1f}% "
               f"[{100 * r['lo']:.1f}, {100 * r['hi']:.1f}] (`{r['field']}`)" for r in rs]  # fmt: skip
    by = {r["lab"]: r for r in rs}
    assert by["Sonnet 4.5"]["lo"] > by["Opus 4.6"]["hi"], "the note says these don't overlap"
    assert by["Other models"]["n"] == 35, "the note describes 35 other-model compactions"
    s = json.loads((ROOT / SRC).read_text())
    boot = s["final"]["per_compaction"]["boot"]
    b45, b46 = (s["by_model"][g]["per_compaction"]["boot"] for g in ("Sonnet 4.5", "Opus 4.6"))
    assert b45["lo"] <= b46["hi"], "the note says the bootstrap intervals overlap"
    w = s["with_untagged"]["per_compaction"]
    uk, un, ur = w["k"], w["n"], w["rate"]
    readme += [
        "",
        (
            '- Notes: "Other models" is 35 compactions under ten other model ids: Opus 4.1, '
            "Sonnet 4, Opus 4.7, Haiku 4.5, GPT-5.3 Codex, two GLM versions, one unnamed id, and "
            "opencode's spellings of Opus 4.6 and Sonnet 4.6 (one compaction each). 7 of the 35 ran "
            "on the opencode harness; every other compaction is Claude Code (from the gitignored "
            "`data/raw/sprint/swechat_odd/scan/compactions.jsonl`; not plotted). The model split is "
            "observational: models differ in their users, tasks and session mixes, and one session "
            "can contribute several compactions, so it is not a model comparison. Sonnet 4.5's "
            "Wilson interval does not overlap Opus 4.6's, but their session-cluster bootstrap "
            f"intervals do ({100 * b45['lo']:.1f}–{100 * b45['hi']:.1f}% against "
            f"{100 * b46['lo']:.1f}–{100 * b46['hi']:.1f}%). Overall, the session-cluster "
            f"bootstrap gives {100 * boot['lo']:.1f}–{100 * boot['hi']:.1f}% "
            "(`final.per_compaction.boot`)."
        ),
        (
            "- Sensitivity: the bars count only lines the keyword pre-filter tagged. Using first-pass "
            "verdicts throughout, including untagged lines Opus 5.5 flagged as completion claims and "
            f"judged false, gives {uk}/{un} = {100 * ur:.1f}% of compactions; this estimate does not "
            "use recheck results (`with_untagged.per_compaction`)."
        ),
        "",
    ]
    (OUT / "README.md").write_text("\n".join(readme))
    print("\n".join(readme))


if __name__ == "__main__":
    main()
