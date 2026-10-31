"""Model against harness prompt: pi's own summarizer prompt run offline with Gemini 3.8 Flash
(week2_summary_swap.py) beside pi with DeepSeek and Antigravity with Flash, on the same four
unrelated chains. Exploratory (J call, 30 Oct; Research Advisor).

Every side is labelled by week2_summary_rot.py's coder unchanged (name matching, else Claude
Opus 5.5). Shares and retention are week2_summary_decay's functions; retention here pools every
entry (no safety split).

    uv run python scripts/week2_summary_decay_swap.py

Figures follow AGENTS.md "Figures": seaborn defaults, no captions in the image; notes in
decay/swap/README.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import week2_summary_decay as D
import week2_summary_decay_agy as A
import week2_summary_decay_harness as H
import week2_summary_swap as S

SRS, K = D.SRS, D.K
OUT = S.OUT
SWAP_ATTR = {v: OUT / f"summary_rot_{v}" / "attributions.jsonl" for v in S.VARIANTS}
SIDES = (
    ("pi", "DeepSeek, pi's prompt, live (max)", None, SRS.rel(D.ATTR)),
    ("ds_max", "DeepSeek, pi's prompt, replay (max)", None, SRS.rel(SWAP_ATTR["ds_max"])),
    ("ds_low", "DeepSeek, pi's prompt, replay (low)", None, SRS.rel(SWAP_ATTR["ds_low"])),
    ("swap", "Gemini Flash, pi's prompt, replay (low)", None, SRS.rel(SWAP_ATTR["low"])),
    ("agy", "Gemini Flash, Antigravity, live (low, inferred)", A.REF_AGY, A.AGY_ATTR),
)
SHORT = {"pi": "DeepSeek live", "ds_max": "DeepSeek replay max",
         "ds_low": "DeepSeek replay low", "swap": "Flash replay low",
         "agy": "Antigravity+Flash"}
READING = (  # written after the numbers (swap.json, 30 Oct; wording per astra swapfig r2)
    "on four fixed pi histories, Flash at low keeps fewer earlier-task entries than DeepSeek at "
    "low after task 4 (7% against 39% of the summary; two tasks later 2% against 17% of the "
    "count); lowering DeepSeek's effort from max to low does not reproduce that loss on these "
    "chains, and Antigravity's live summaries keep none. One task later the order is DeepSeek "
    "38%, Flash with pi's prompt 20%, Antigravity 0%. This compares conditions, it does not "
    "decompose the gap: the Antigravity side is a live run with its own histories, and only "
    "DeepSeek was run at both efforts (Flash at high on one summary). The replay against live "
    "DeepSeek (same model and effort) differs by +4 to +7 points in earlier-task share and by "
    "+12.3 points in retention one task later (26.4% to 38.7%), within 2 points two and three "
    "tasks later. n = 4 chains; exploratory.")


def effort_check(per_low: list[dict]) -> dict | None:
    """u1 boundary 2 at effort high against the same boundary at low (one summary each)."""
    if not SWAP_ATTR["high"].exists():
        return None
    rows = A.boundary_rows(SRS._jsonl(SWAP_ATTR["high"]))
    hi = [p for p in A.per_summary(rows) if (p["chain"], p["boundary"]) == ("u1", 2)]
    lo = [p for p in per_low if (p["chain"], p["boundary"]) == ("u1", 2)]
    if not hi or not lo:
        return None
    def pick(p: dict) -> dict:
        return {"n_labelled": p["n_labelled"],
                **{k: p["shares"][k] for k in ("current", "earlier", "generic")}}

    return {"chain": "u1", "boundary": 2, "low": pick(lo[0]), "high": pick(hi[0])}


def main() -> None:
    rows, src = {}, {}
    for key, _, ref, path in SIDES:
        rows[key], sha = H.load(ref, path)
        src[key] = {"attribution": f"{ref or 'this branch'}:{path}", "sha256": sha}
    unlab = {k: sum(r["final_label"] is None for r in rs) for k, rs in rows.items()}
    per = {k: A.per_summary(rs) for k, rs in rows.items()}
    cells = {k: D.task_cells(p) for k, p in per.items()}
    lab = {k: [r for r in rs if r["final_label"] is not None] for k, rs in rows.items()}
    ret = {k: D.retention_by_age(A.flagged(rs, lambda r: False)) for k, rs in lab.items()}
    earlier = {k: {str(b): sum(cells[k][str(b)][f"task_{j}"]["mean"] for j in range(1, b))
                   for b in range(2, K + 1)} for k in cells}
    eff = effort_check(per["swap"])
    gap = {"earlier_share": {b: earlier["ds_max"][b] - earlier["pi"][b] for b in earlier["pi"]},
           "retention": {d: (ret["ds_max"][f"{d}:other"]["retention"] or 0)
                         - (ret["pi"][f"{d}:other"]["retention"] or 0) for d in range(1, K)},
           "what": "DeepSeek replay at max minus the live DeepSeek summaries (same model, effort, "
                   "prompt and histories): resampling noise plus serving route"}
    SRS._dump(OUT / "swap.json", {
        "source": src, "by_task": cells, "earlier_share": earlier, "replay_vs_live_deepseek": gap,
        "retention_by_age_all_entries": {k: {d: r[f"{d}:other"] for d in range(1, K)}
                                         for k, r in ret.items()},
        "n_entries": {k: len(r) for k, r in lab.items()}, "n_unlabelled_entries": unlab,
        "effort_check": eff, "bootstrap": {"seed": D.SEED, "n": D.BOOT, "unit": "chain"},
        "definition": D.RETENTION_DEF})
    plt, deep = D._style()
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    n = len(SIDES)
    labels = {k: f"{lbl}, n = {cells[k]['1']['n_chains']}" for k, lbl, _, _ in SIDES}
    colors = D.task_colors(deep)
    t1 = {k: 100 * cells[k][str(K)]["task_1"]["mean"] for k in cells}
    title_bt = ("Task 1's share of the summary after task 4: "
                + ", ".join(f"{SHORT[k]} {t1[k]:.0f}%" for k, *_ in SIDES))
    fig, axes = plt.subplots(1, n, figsize=(4.6 * n, 6.4), sharey=True, squeeze=False)
    axes = axes[0]
    fig.subplots_adjust(left=0.05, right=0.99, top=0.8, bottom=0.2, wspace=0.06)
    nums_bt = []
    for ax, (k, *_) in zip(axes, SIDES, strict=True):
        nums_bt += [f"{labels[k]}, {x}" for x in D.by_task_panel(ax, cells[k], colors)]
        ax.set_title(labels[k], loc="left", fontsize=10)
        ax.tick_params(axis="x", labelsize=9)
    axes[0].set_ylabel("Share of summary entries (%)")
    fig.suptitle(title_bt, x=0.01, y=0.975, ha="left", va="top", wrap=True)
    handles = [Patch(color=colors[f"task_{j}"], label=f"Task {j}") for j in range(1, K + 1)]
    handles.append(Patch(color=colors["generic"], label="No one task"))
    handles.append(Line2D([], [], marker="o", color="black", label="Task 1 (95% CI)"))
    fig.legend(handles=handles, loc="lower center", ncol=6, bbox_to_anchor=(0.5, 0.0))
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"by_task_swap.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    # retention of every entry by age, sides side by side
    fig, ax = plt.subplots(figsize=(12, 5.8))
    fig.subplots_adjust(left=0.07, right=0.99, top=0.84, bottom=0.2)
    w = 0.16
    nums_rt = []
    for i, (k, *_) in enumerate(SIDES):
        for d in range(1, K):
            c = ret[k][f"{d}:other"]
            if c["retention"] is None:
                continue
            v, (lo, hi) = 100 * c["retention"], c["ci95"]
            x = d + (i - (n - 1) / 2) * w
            ax.bar(x, v, w * 0.95, color=deep[i], label=SHORT[k] if d == 1 else None)
            if hi > lo:
                ax.errorbar(x, v, yerr=[[v - 100 * lo], [100 * hi - v]], fmt="none",
                            ecolor="black")
            ax.text(x, 100 * hi + 0.8, f"{v:.0f}%", ha="center", va="bottom", fontsize=9)
            nums_rt.append(f"{labels[k]}, {D.retention_line(d, False, c).replace('other', 'all entries')}")
    ax.set_xticks(range(1, K))
    ax.set_xticklabels([f"{d} task{'s' if d > 1 else ''} later" for d in range(1, K)])
    ax.set_ylabel("Entry-count retention (% of the count right after the task)")
    r2 = {k: 100 * (ret[k]["2:other"]["retention"] or 0) for k in ret}
    title_rt = ("Two tasks later, entries about a task keep this share of their count: "
                + ", ".join(f"{SHORT[k]} {r2[k]:.0f}%" for k, *_ in SIDES))
    fig.suptitle(title_rt, x=0.01, y=0.975, ha="left", va="top", wrap=True, fontsize=11)
    fig.legend(loc="lower center", ncol=n, bbox_to_anchor=(0.5, 0.0))
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"retention_swap.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    eff_line = ("not run" if eff is None else
                f"u1 after task 2, one summary each: earlier-task share at low "
                f"{100 * eff['low']['earlier']:.0f}% ({eff['low']['n_labelled']} entries), at "
                f"high {100 * eff['high']['earlier']:.0f}% ({eff['high']['n_labelled']} entries)")
    readme = [
        "# Model against harness prompt for summaries (exploratory)", "",
        ("Generated by `scripts/week2_summary_decay_swap.py`; every number is in `swap.json`. "
         f"**Reading:** {READING} The replayed summaries come from "
         "`scripts/week2_summary_swap.py` (`summaries_<variant>.jsonl`, `price_check.json`). "
         "Sources:"), "",
        *[f"- {lbl}: `{src[k]['attribution']}`"
          + (f" (sha256 {src[k]['sha256'][:16]}…)" if src[k]["sha256"] else "")
          for k, lbl, _, _ in SIDES], "",
        f"## by_task_swap: {title_bt}", "", "![by_task_swap](by_task_swap.png)", "",
        ("**Caption (slide notes):** the same four unrelated chains (u1–u4). The three replay "
         "panels resend pi's recorded summarizer requests (same histories, same prompt), each "
         "model's own previous summary fed into pi's update prompt: DeepSeek V4.1 Flash at "
         "reasoning effort max (pi's live setting) and low, via OpenRouter on Together, and "
         "Gemini 3.8 Flash at low (Antigravity's setting) on Google AI Studio. Offline: after "
         "task 1 the histories were written by an agent that had read the live DeepSeek "
         "summaries, so the replays isolate the summarizer, not a live run. DeepSeek replay "
         "(max) against DeepSeek live is the noise check (resampling plus serving route); "
         "DeepSeek replay (low) against Flash replay (low) is the model at equal effort; Flash "
         "replay against Antigravity+Flash is the harness with the model fixed. Antigravity's "
         "summarizer is taken to be its session model (probe a9a6d023, suggestive; the "
         "summarizer's identity and effort are not recorded). Effort check: "
         f"{eff_line}. Antigravity also carries the user's earlier requests verbatim outside the "
         "summary; those are not counted. n = 4 chains gives coarse intervals."), "",
        ("**Source:** `swap.json`, `by_task.<side>.<k>.task_<j>.mean` (chain means; 95% "
         f"percentile bootstrap over chains, {D.BOOT:,} resamples, seed {D.SEED}). Labels: "
         "summary_rot's coder (name matching, else Claude Opus 5.5) on every side."), "",
        "**Numbers plotted:**", *[f"- {x}" for x in nums_bt], "",
        ("**Replay against live DeepSeek** (max − live, percentage points): earlier-task share "
         "after tasks 2/3/4 " + "/".join(f"{100 * gap['earlier_share'][str(b)]:+.0f}"
                                         for b in range(2, K + 1))
         + "; retention 1/2/3 tasks later " + "/".join(f"{100 * gap['retention'][d]:+.1f}"
                                                       for d in range(1, K)) + "."), "",
        "**Earlier-task share after tasks 2, 3, 4** (every task before the current one, chain "
        "means): " + "; ".join(f"{SHORT[k]} " + "/".join(f"{100 * earlier[k][str(b)]:.0f}"
                                                          for b in range(2, K + 1)) + "%"
                               for k, *_ in SIDES) + ".", "",
        f"## retention_swap: {title_rt}", "", "![retention_swap](retention_swap.png)", "",
        (f"**Source:** `swap.json`, `retention_by_age_all_entries`. "
         f"{D.RETENTION_DEF.capitalize()}; every entry pooled (no safety split). It counts "
         "entries, not the survival of the same facts: a summarizer that merges or splits "
         "entries changes the count."), "",
        "**Numbers plotted:**", *[f"- {x}" for x in nums_rt], ""]
    (OUT / "README.md").write_text("\n".join(readme) + "\n")
    print(title_bt)
    print(title_rt)
    print(json.dumps({"earlier_share": earlier, "effort_check": eff, "unlabelled": unlab}))


if __name__ == "__main__":
    main()
