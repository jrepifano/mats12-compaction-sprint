"""Slide 4 chart: how many Experiment 1 compactions came from each source, and at what context size.

One bar per source for the number of compactions analysed, the count on the bar, and under it the
median context size before the compaction with its 10th–90th percentile range. Every number is
read from results/sprint/S1/context_lengths.json (written by scripts/sprint_s1_context_summary.py;
field `pre_tokens`). Drawn in the v2 deck's look by reusing scripts/sprint_figs_v2.py's helpers
(rounded bars, labels, frame, save); see that file for the style exception Jacob approved.

Writes docs/sprint/figs/v2/05_sources_context.png and .svg, and puts this chart's section into
docs/sprint/figs/v2/README.md between the markers below (replacing an earlier copy). Note that
re-running scripts/sprint_figs_v2.py rewrites that README without this section; re-run this
script after it. Run: uv run python scripts/sprint_fig_s1_sources.py
"""

from __future__ import annotations

import json

import matplotlib.pyplot as plt
import sprint_figs_v2 as v2

SRC = "results/sprint/S1/context_lengths.json"
NAME = "05_sources_context"
START, END = f"<!-- {NAME}:start -->", f"<!-- {NAME}:end -->"
# Plain-word names and one colour per source, none of them already used in the v2 set.
SOURCES = {
    "jacob": ("My own Claude Code sessions", "#4f6d8f"),
    "swe-chat-v1": ("SWE-chat (real users)", "#c08552"),
    "posttrainbench": ("PostTrainBench", "#8a9a5b"),
    "frontierml": ("FrontierML", "#a86f8a"),
}


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
    for lab in ax.get_xticklabels():
        lab.set_fontweight("bold")
        lab.set_fontsize(11)
    # Fix the x range before drawing: rbar reads the axes' aspect, and autoscale would leave the
    # first bar with a narrower range (and over-rounded corners).
    ax.set_xlim(-0.5, len(rows) - 0.5)
    for i, r in enumerate(rows):
        v2.rbar(ax, i, r["n"], 0.6, r["col"])
        v2.label(ax, i, r["n"], f"{r['n']}", bold=True)
        ax.annotate(r["under"], (i, 0), xycoords=("data", "axes fraction"), xytext=(0, -30),
                    textcoords="offset points", ha="center", va="top", fontsize=11,
                    color=v2.INK, linespacing=1.3)
    fig.text(0.02, 0.02, f"Source: {SRC}. Context = tokens in the context just before the "
             "compaction, as each dataset recorded it; range = 10th–90th percentile.",
             fontsize=9, color=v2.MUTED, ha="left", va="bottom")
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
            [note])
    return title, rows


def write_readme(section: str) -> None:
    path = v2.OUT / "README.md"
    text = path.read_text() if path.exists() else ""
    block = f"{START}\n{section.rstrip()}\n{END}\n"
    if START in text:
        a, b = text.index(START), text.index(END) + len(END)
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


if __name__ == "__main__":
    main()
