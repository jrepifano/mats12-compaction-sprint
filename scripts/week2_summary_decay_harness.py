"""Summary decay by task and safety-relevant retention across harnesses, one panel each: pi,
Antigravity with Flash and with Sonnet (same harness, different model), Claude Code with Haiku
and with Sonnet. Exploratory (J call, 29 Oct; Research Advisor). The two-panel pi/Antigravity
version is week2_summary_decay_agy.py; this one adds the sides as their labels land.

Every side is labelled by week2_summary_rot.py's coder unchanged at e9a4c51f (name matching,
else Claude Opus 5.5; week2-build ran it on its arms) and read at a pinned commit; a side whose
commit is not set yet is left out. Safety-relevant flags are gpt-6-astra's
(week2_summary_safety_labels.py; one label set per arrival, same prompt). The shares, retention
and panels are week2_summary_decay's functions.

    uv run python scripts/week2_summary_decay_harness.py

Figures follow AGENTS.md "Figures": seaborn defaults, no captions in the image; notes in
decay/harness/README.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import week2_summary_decay as D
import week2_summary_decay_agy as A

SRS, K = D.SRS, D.K
OUT = D.OUT / "harness"
CB = "results/sprint/week2/agy_chain5"
# key, panel label, commit (None: this branch; "" : not landed yet), attribution path, flag set
RA = "bulk/summary_rot/attributions.jsonl"
SIDES = (
    ("pi", "pi (DeepSeek V4.1 Flash)", None, SRS.rel(D.ATTR), "main"),
    ("agy", "Antigravity (Gemini 3.8 Flash)", A.REF_AGY, A.AGY_ATTR, "main"),
    ("agy_sonnet", "Antigravity (Sonnet 5.5)", "d92212c5", f"{CB}/agy_sonnet/{RA}", "agy_sonnet"),
    ("cc", "Claude Code (Haiku 5.5)", "", f"{CB}/claude_code/{RA}", "cc"),
    ("cc_sonnet", "Claude Code (Sonnet 5.5)", "", f"{CB}/claude_code_sonnet/{RA}", "cc_sonnet"),
)
FLAGS = A.OUT / "safety_labels"
SHORT = {"pi": "pi", "agy": "Antigravity+Flash", "agy_sonnet": "Antigravity+Sonnet",
         "cc": "Claude Code+Haiku", "cc_sonnet": "Claude Code+Sonnet"}  # titles


def load(ref: str | None, path: str) -> tuple[list[dict], str | None]:
    if ref is None:
        return A.boundary_rows(SRS._jsonl(SRS.ROOT / path)), None
    text, sha = A.git_text(ref, path)
    return A.boundary_rows([json.loads(x) for x in text.splitlines()]), sha


def main() -> None:
    sides, src = [], {}
    rows, flags = {}, {}
    for key, label, ref, path, fset in SIDES:
        if ref == "":
            continue
        rs, sha = load(ref, path)
        f = FLAGS / fset / "flags.json"
        if not f.exists():
            sys.exit(f"{SRS.rel(f)} missing: label {key} with week2_summary_safety_labels.py")
        flags[key] = json.loads(f.read_text())["flags"][key]
        rows[key] = rs
        src[key] = {"attribution": f"{ref or 'this branch'}:{path}", "sha256": sha,
                    "flags": SRS.rel(f)}
        sides.append((key, label))
    unlab = {k: sum(r["final_label"] is None for r in rs) for k, rs in rows.items()}
    per = {k: A.per_summary(rs) for k, rs in rows.items()}
    cells = {k: D.task_cells(p) for k, p in per.items()}
    lab = {k: [r for r in rs if r["final_label"] is not None] for k, rs in rows.items()}
    ret = {k: D.retention_by_age(A.flagged(rs, lambda r, k=k: flags[k][r["id"]]))
           for k, rs in lab.items()}
    chains = {k: sorted({r["chain"] for r in rs}) for k, rs in lab.items()}
    n_e = {k: len(rs) for k, rs in lab.items()}
    n_s = {k: sum(flags[k][r["id"]] for r in rs) for k, rs in lab.items()}
    SRS._dump(OUT / "harness.json", {
        "source": src, "chains": chains, "by_task": cells, "retention_by_age": ret,
        "n_entries": n_e, "n_safety": n_s, "n_unlabelled_entries": unlab,
        "bootstrap": {"seed": D.SEED, "n": D.BOOT, "unit": "chain"},
        "definition": D.RETENTION_DEF})
    plt, deep = D._style()
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    OUT.mkdir(parents=True, exist_ok=True)
    n = len(sides)
    labels = {k: f"{lbl}, n = {len(chains[k])}" for k, lbl in sides}
    # by task
    colors = D.task_colors(deep)
    t1 = {k: 100 * cells[k][str(K)]["task_1"]["mean"] for k, _ in sides}
    title_bt = ("Task 1's share of the summary after task 4: "
                + ", ".join(f"{SHORT[k]} {t1[k]:.0f}%" for k, _ in sides))
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 6.4), sharey=True, squeeze=False)
    axes = axes[0]
    fig.subplots_adjust(left=0.05, right=0.99, top=0.8, bottom=0.2, wspace=0.06)
    nums_bt = []
    for ax, (k, _) in zip(axes, sides, strict=True):
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
        fig.savefig(OUT / f"by_task_harness.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    # safety-relevant against other (gpt-6-astra flags)
    top = max(100 * c["ci95"][1] for r in ret.values() for c in r.values())
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 6.4), sharey=True, squeeze=False)
    axes = axes[0]
    fig.subplots_adjust(left=0.05, right=0.99, top=0.8, bottom=0.2, wspace=0.06)
    nums_sf = []
    for ax, (k, _) in zip(axes, sides, strict=True):
        for d in range(1, K):
            for off, safe, col in ((-0.18, True, deep[3]), (0.18, False, deep[0])):
                c = ret[k][f"{d}:{'safety' if safe else 'other'}"]
                if c["retention"] is None:
                    continue
                v, (lo, hi) = 100 * c["retention"], c["ci95"]
                ax.bar(d + off, v, 0.34, color=col)
                if hi > lo:
                    ax.errorbar(d + off, v, yerr=[[v - 100 * lo], [100 * hi - v]], fmt="none",
                                ecolor="black")
                ax.text(d + off, 100 * hi + 1.5, f"{v:.0f}%", ha="center", va="bottom",
                        fontsize=9)
                nums_sf.append(f"{labels[k]}, {D.retention_line(d, safe, c)}")
        ax.set_xticks(range(1, K))
        ax.set_xticklabels([f"{d} task{'s' if d > 1 else ''} later" for d in range(1, K)],
                           fontsize=9)
        ax.set_title(labels[k], loc="left", fontsize=10)
    axes[0].set_ylabel("Entry-count retention (% of the count right after the task)")
    axes[0].set_ylim(0, 10 * (int(top / 10) + 2))
    r2 = {k: ret[k]["2:safety"]["retention"] for k, _ in sides}
    title_sf = ("Two tasks later, safety-relevant entry counts are this share of their count "
                "right after the task: "
                + ", ".join(f"{SHORT[k]} {100 * r2[k]:.0f}%" for k, _ in sides
                            if r2[k] is not None))
    fig.suptitle(title_sf, x=0.01, y=0.975, ha="left", va="top", wrap=True)
    fig.legend(handles=[Patch(color=deep[3], label="Safety-relevant (gpt-6-astra)"),
                        Patch(color=deep[0], label="Other")], loc="lower center", ncol=2,
               bbox_to_anchor=(0.5, 0.0))
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"safety_harness.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    readme = [
        "# Summary decay across harnesses (exploratory)", "",
        ("Generated by `scripts/week2_summary_decay_harness.py`; every number is in "
         "`harness.json`. Sources:"), "",
        *[f"- {lbl}: `{src[k]['attribution']}`"
          + (f" (sha256 {src[k]['sha256'][:16]}…)" if src[k]["sha256"] else "")
          + f"; flags `{src[k]['flags']}`; chains {', '.join(chains[k])}" for k, lbl in sides],
        "",
        f"## by_task_harness: {title_bt}", "", "![by_task_harness](by_task_harness.png)", "",
        ("**Source:** `harness.json`, `by_task.<side>.<k>.task_<j>.mean` (task j's share of the "
         "boundary-k summary; chain means; 95% percentile bootstrap over chains, "
         f"{D.BOOT:,} resamples, seed {D.SEED}). Labels: summary_rot's coder (name matching, "
         "else Claude Opus 5.5) on every side."), "",
        ("**Caption (slide notes):** the same four unrelated chains (u1–u4; two for the Sonnet "
         "runs). Antigravity with Flash and with Sonnet share a harness and differ in model; "
         "summary style suggests Antigravity's summarizer follows the session model (probe "
         "a9a6d023: 3/3 vs 0/3; suggestive, the summarizer's identity is not recorded). "
         "Other pairs differ in model and harness together. Antigravity and Codex also carry "
         "the user's earlier requests verbatim outside the summary; those are not counted. "
         "n = 2 chains gives coarse intervals."), "",
        "**Numbers plotted:**", *[f"- {x}" for x in nums_bt], "",
        f"## safety_harness: {title_sf}", "", "![safety_harness](safety_harness.png)", "",
        (f"**Source:** `harness.json`, `retention_by_age.<side>`. "
         f"{D.RETENTION_DEF.capitalize()}. Safety-relevant flags by gpt-6-astra (unvalidated, "
         "no hand check; definition in each flags.json): "
         + "; ".join(f"{lbl} {n_s[k]} of {n_e[k]}" for k, lbl in sides) + "."), "",
        "**Numbers plotted:**", *[f"- {x}" for x in nums_sf], ""]
    (OUT / "README.md").write_text("\n".join(readme) + "\n")
    print(title_bt)
    print(title_sf)
    print(json.dumps({"unlabelled": unlab, "n_entries": n_e, "n_safety": n_s}))


if __name__ == "__main__":
    main()
