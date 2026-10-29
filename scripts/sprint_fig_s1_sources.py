"""Slide 4 chart: how many Experiment 1 compactions came from each source, and at what context size.

One bar per source for the number of compactions analysed, the count on the bar, and under it the
median context size before the compaction with its 10th–90th percentile range. Every number is
read from results/sprint/S1/context_lengths.json (written by scripts/sprint_s1_context_summary.py;
field `pre_tokens`). Drawn with scripts/sprint_figs_v2.py's helpers (bars, labels, frame, save),
in plain seaborn defaults per AGENTS.md "Figures" (29 Oct).

Writes docs/sprint/figs/v2/05_sources_context.png and .svg, and puts this chart's section into
docs/sprint/figs/v2/README.md between the markers below (replacing an earlier copy).

Also writes 05b_sources_context_size.png and .svg (Jacob, 29 Oct: the bar heights above are only
the number of compactions per source; the point is the context size): one point per analysed
compaction, its context length by source (`sources.<source>.tokens_values`), with seaborn's
default box plot (median and quartiles) over the points and each source's n under its name. Its
README section has markers of its own. Note that
re-running scripts/sprint_figs_v2.py rewrites that README without this section; re-run this
script after it. Run: uv run python scripts/sprint_fig_s1_sources.py
"""

from __future__ import annotations

import json
from statistics import median

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import sprint_figs_v2 as v2
from matplotlib.ticker import FuncFormatter

SRC = "results/sprint/S1/context_lengths.json"
NAME = "05_sources_context"
NAME_B = "05b_sources_context_size"
START, END = f"<!-- {NAME}:start -->", f"<!-- {NAME}:end -->"


def markers(name: str) -> tuple[str, str]:
    return f"<!-- {name}:start -->", f"<!-- {name}:end -->"
# Plain-word names and one deep-palette colour per source (red, the v2 set's lost-and-matters
# colour, is left out).
SOURCES = {
    "jacob": ("My own Claude Code sessions", v2.PAL[0]),
    "swe-chat-v1": ("SWE-chat (real users)", v2.PAL[1]),
    "posttrainbench": ("PostTrainBench", v2.PAL[2]),
    "frontierml": ("FrontierML", v2.PAL[4]),
}
XLAB = {"jacob": "My own Claude Code\nsessions"}  # two-line category name on 05b


def tok(v: float) -> str:
    """Tokens in plain words: 466577.5 -> '467k', 1410364 -> '1.41M'."""
    return f"{v / 1e6:.2f}M" if v >= 999_500 else f"{round(v / 1000)}k"


def exact(v: float) -> str:
    """A statistic at full precision with thousands separators (466577.5 -> '466,577.5')."""
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.6g}" if v < 1e3 else f"{v:,.1f}"


def rows_from(data: dict) -> list[dict]:
    out = []
    for key, (lab, col) in SOURCES.items():
        s = data["sources"][key]
        t = s["tokens"]
        under = f"median {tok(t['median'])} tokens\n({tok(t['p10'])}–{tok(t['p90'])})"
        out.append({"key": key, "lab": lab, "col": col, "n": s["n_analysed"],
                    "n_sampled": s["n_sampled"], "med": t["median"], "p10": t["p10"],
                    "p90": t["p90"], "lo": t["min"], "hi": t["max"], "under": under})
    return out


def title_of(data: dict, rows: list[dict]) -> str:
    pub = [r for r in rows if r["key"] != "jacob"]
    me = next(r for r in rows if r["key"] == "jacob")
    lo, hi = min(r["med"] for r in pub), max(r["med"] for r in pub)
    if not me["med"] > hi:  # the title says my sessions compact later than the public sets
        raise SystemExit("my sessions' median is not above the public sets'; update the title")
    band = tok(lo) if tok(lo) == tok(hi) else f"{tok(lo)}–{tok(hi)}"
    return (f"{data['n_analysed']} real compactions: the public sets compact at a median "
            f"{band} tokens,\nmy own sessions at {tok(me['med'])}")


def draw(data: dict) -> tuple[str, list[dict]]:
    rows = rows_from(data)
    assert sum(r["n"] for r in rows) == data["n_analysed"]
    title = title_of(data, rows)
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.23)
    ymax = max(r["n"] for r in rows) * 1.16
    v2.frame(ax, range(len(rows)), [r["lab"] for r in rows], "Compactions analysed", ymax)
    ax.set_xlim(-0.5, len(rows) - 0.5)
    for i, r in enumerate(rows):
        v2.rbar(ax, i, r["n"], 0.6, r["col"])
        v2.label(ax, i, r["n"], f"{r['n']}", bold=True)
        ax.annotate(r["under"], (i, 0), xycoords=("data", "axes fraction"), xytext=(0, -30),
                    textcoords="offset points", ha="center", va="top")
    pub = [r for r in rows if r["key"] != "jacob"]
    lo, hi = tok(min(r["med"] for r in pub)), tok(max(r["med"] for r in pub))
    me = tok(next(r["med"] for r in rows if r["key"] == "jacob"))
    v2.SHORT[NAME] = (f"Public sets compact at a median {lo if lo == hi else f'{lo}–{hi}'} "
                      f"tokens, my own sessions at {me}")
    line = (f"Source: {SRC}. Context = tokens in the context just before the compaction, as each "
            "dataset recorded it; range = 10th–90th percentile.")  # kept out of the image
    fml = data["sources"]["frontierml"]
    n_oc = data["sources"]["swe-chat-v1"]["harness"].get("opencode", 0)
    note = (
        "Notes: `pre_tokens` is each record's dataset-recorded context count before the "
        "compaction (Claude Code's compactMetadata.preTokens for my sessions, SWE-chat's Claude "
        f"Code compactions and PostTrainBench; the summarizer call's input tokens for SWE-chat's "
        f"{n_oc} OpenCode compactions; for FrontierML, the input count of the preceding "
        "main-thread call, a proxy that leaves out that call's output and later tool results). "
        "It counts the whole context window, system prompt and tool definitions included, so it "
        "is larger than the history text the judge read. "
        "Percentiles by numpy.percentile (linear). "
        f"FrontierML: {fml['n_analysed']} of {fml['n_sampled']} analysed (excluded: "
        f"{', '.join(fml['excluded_ids'])}, whose claim-judging call failed; roster ok = false). "
        "Raw files' sha256 and each sampled line's sha256 were checked against "
        "results/sprint/S1/manifest.json. Generated by `scripts/sprint_fig_s1_sources.py`; "
        "re-run it after `scripts/sprint_figs_v2.py`, which rewrites this README."
    )
    v2.save(fig, NAME, title,
            "how many of Experiment 1's analysed compactions came from each source, and how "
            "large the context was just before each one.",
            [SRC + " (sources.<source>.n_analysed; sources.<source>.tokens.median, p10, p90, "
                   "min, max; field pre_tokens)"],
            [f"{r['lab']}: {r['n']} compactions ({r['n_sampled']} sampled); context median "
             f"{exact(r['med'])} tokens, p10 {exact(r['p10'])}, p90 {exact(r['p90'])}, "
             f"min {r['lo']:,}, max {r['hi']:,}" for r in rows],
            [f"Source line (not on the chart): {line}", note])
    return title, rows


def title_b(rows: list[dict]) -> str:
    pub = [r for r in rows if r["key"] != "jacob"]
    me = next(r for r in rows if r["key"] == "jacob")
    lo, hi = min(r["med"] for r in pub), max(r["med"] for r in pub)
    if not me["med"] > hi:  # the title contrasts my sessions' median with the public sets'
        raise SystemExit("my sessions' median is not above the public sets'; update the title")
    band = tok(lo) if tok(lo) == tok(hi) else f"{tok(lo)}–{tok(hi)}"
    return f"Public datasets compact at a median {band} tokens; my own sessions at {tok(me['med'])}"


def draw_size(data: dict) -> tuple[str, list[dict]]:
    """One point per analysed compaction: its context length, by source, with a box plot."""
    rows = rows_from(data)
    xs, ys = [], []
    for r in rows:
        vals = data["sources"][r["key"]]["tokens_values"]
        assert len(vals) == r["n"] and vals == sorted(vals), r["key"]
        assert (median(vals), vals[0], vals[-1]) == (r["med"], r["lo"], r["hi"]), r["key"]
        r["values"] = vals
        r["xlab"] = f"{XLAB.get(r['key'], r['lab'])}\nn = {r['n']}"
        xs += [r["xlab"]] * len(vals)
        ys += vals
    order = [r["xlab"] for r in rows]
    title = title_b(rows)
    fig, ax = plt.subplots(figsize=(10, 5.8))
    np.random.seed(20261029)  # stripplot jitters with numpy's global RNG: same image each run
    sns.stripplot(x=xs, y=ys, hue=xs, order=order, hue_order=order,
                  palette=[r["col"] for r in rows], legend=False, jitter=0.3, size=3.5,
                  alpha=0.6, ax=ax)
    sns.boxplot(x=xs, y=ys, order=order, showfliers=False, fill=False, color=".26", width=0.5,
                ax=ax)
    ax.set_ylim(0, max(ys) * 1.05)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: "0" if v == 0 else tok(v)))
    ax.set_ylabel("Context before the compaction (tokens)")
    ax.set_xlabel("")
    fml = data["sources"]["frontierml"]
    v2.save(fig, NAME_B, title,
            "the context length (tokens) just before each of Experiment 1's analysed "
            "compactions, by source: one point per compaction, with a box plot (median line, "
            "quartile box, whiskers to the farthest point within 1.5 IQR; no outlier markers, "
            "since every point is drawn).",
            [SRC + " (sources.<source>.tokens_values, n_analysed; tokens.median, p10, p90, "
                   "min, max for the numbers below; field pre_tokens)"],
            [f"{r['lab']}: n = {r['n']} points; median {exact(r['med'])} tokens, "
             f"p10 {exact(r['p10'])}, p90 {exact(r['p90'])}, min {r['lo']:,}, max {r['hi']:,}"
             for r in rows],
            [("Notes: same data and field as 05_sources_context above (see its notes on what "
              "`pre_tokens` counts per source). Horizontal jitter only; the y value of each "
              f"point is exact. FrontierML has {fml['n_analysed']} points of {fml['n_sampled']} "
              "sampled. Generated by `scripts/sprint_fig_s1_sources.py`."),
             ("Review: gpt-6-astra, logs/reviews/20261029-figs-simplify-r1.md: PASS WITH MINOR "
              "FIXES (1 minor, 3 nits; the minor and the title-guard nit fixed, not "
              "re-reviewed).")])
    return title, rows


def write_readme(section: str, name: str = NAME) -> None:
    path = v2.OUT / "README.md"
    text = path.read_text() if path.exists() else ""
    start, end = markers(name)
    block = f"{start}\n{section.rstrip()}\n{end}\n"
    if start in text:
        a, b = text.index(start), text.index(end) + len(end)
        text = text[:a] + block.rstrip("\n") + text[b:]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    path.write_text(text)


def main() -> None:
    data = json.loads((v2.ROOT / SRC).read_text())
    v2.README.clear()
    title, _ = draw(data)
    write_readme("\n".join(v2.README))
    print(title)
    v2.README.clear()
    with plt.rc_context({"svg.hashsalt": NAME_B}):  # stable SVG ids
        title_b_, _ = draw_size(data)
    write_readme("\n".join(v2.README), NAME_B)
    print(title_b_)


if __name__ == "__main__":
    main()
