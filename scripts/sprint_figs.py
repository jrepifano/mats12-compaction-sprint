"""Plain bar charts of the sprint findings, for the progress update and the final slides.

Every number is read from a result file at run time; nothing is typed in. Charts and their sources:
1. Items lost by source: results/sprint/S1/analysis.json (Jacob, PostTrainBench, FrontierML, as
   sampled) and results/sprint/S1/swechat_ext/summary.json (SWE-chat, population-weighted).
2. SWE-chat losses by item type: results/sprint/S1/swechat_ext/summary.json.
3. Judge precision against hand labels: results/sprint/S1/judge_precision.json,
   results/sprint/S2/lossaudit/calibration.json, results/sprint/judge_compare/summary.json.
4. Live runs, planted rules lost by channel: results/sprint/S2/gate.json.
5. Live runs, commit-prefix rule broken by arm where it was lost: results/sprint/S2/readout.json.
6. Simulated maintainer, requirements lost per summarizer:
   data/raw/sprint/s2/interact/*/b*/boundary/{grades,eligibility}.json and replays/*/replay.json
   (gitignored raw data; the README records each file's sha256).
7. Hand labels per check: the table in docs/sprint/progress.md.

Writes PNG and SVG to docs/sprint/figs/ plus docs/sprint/figs/README.md (claim, source, numbers).
Style follows docs/pitch/fig_*.py: matplotlib, top/right spines off, footer text, dpi 200.
Run: uv run python scripts/sprint_figs.py
"""

import glob
import hashlib
import json
import re
import textwrap
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "sprint" / "figs"
plt.rcParams.update(
    {
        "font.size": 12,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "#fcfcfb",
        "axes.facecolor": "#fcfcfb",
        "savefig.facecolor": "#fcfcfb",
        "text.color": "#0b0b0b",
        "axes.labelcolor": "#52514e",
        "xtick.color": "#0b0b0b",
        "ytick.color": "#52514e",
        "axes.edgecolor": "#8a8984",
        "svg.fonttype": "none",
    }
)
BLUE, GRAY, INK, MUTED = "#2a78d6", "#a8a7a1", "#0b0b0b", "#52514e"
MODEL = {"deepseek": "DeepSeek", "claude-haiku-5-5": "Claude Haiku 5.5"}  # display names only
LOST = ("dropped", "misstated")  # a requirement grade that counts as lost (interact_reqs.GRADES)

CHARTS = []  # one dict per chart, for README.md
STATS = {}  # computed values the README compares with the brief
SKIPPED = []  # (chart function, reason) for charts whose data couldn't be found
BRIEF = "logs/s2/briefs/sprint-figs.md"


def load(rel):
    return json.loads((ROOT / rel).read_text())


def sha(rel):
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()[:16]


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / d
    return c - h, c + h


def close(a, b, tol=1e-3):
    return abs(a - b) <= tol


def bars(ax, rows, pct=True, color=BLUE, ymax=None, value_fmt=None):
    """rows: dicts with label, value, lo/hi (or None), n_text, color (optional), top (optional)."""
    xs = range(len(rows))
    scale = 100 if pct else 1
    vals = [r["value"] * scale for r in rows]
    cols = [r.get("color", color) for r in rows]
    ax.bar(xs, vals, width=0.62, color=cols, zorder=2)
    for x, r, v in zip(xs, rows, vals):
        top = v
        if r.get("lo") is not None:
            lo, hi = r["lo"] * scale, r["hi"] * scale
            ax.errorbar(
                x,
                v,
                yerr=[[v - lo], [hi - v]],
                fmt="none",
                ecolor=INK,
                elinewidth=1.4,
                capsize=5,
                zorder=3,
            )
            top = hi
        txt = r.get("top") or (value_fmt(r) if value_fmt else f"{v:.1f}%" if pct else f"{v:g}")
        ax.annotate(
            txt,
            (x, top),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=11.5,
            color=INK,
        )
    ax.set_xticks(list(xs))
    depth = max(r["label"].count("\n") for r in rows)  # pad so every n line sits at one height
    ax.set_xticklabels(
        [r["label"] + "\n" * (depth - r["label"].count("\n") + 2) + r["n_text"] for r in rows],
        fontsize=11,
        linespacing=1.15,
    )
    ax.grid(axis="y", color="#e4e3df", lw=0.8, zorder=0)
    ax.tick_params(axis="x", length=0)
    if ymax is not None:
        ax.set_ylim(0, ymax)


def finish(fig, name, title, foot, sources):
    fig.suptitle(title, x=0.01, ha="left", fontsize=15, fontweight="bold", color=INK)
    src = "Source: " + "; ".join(sources)
    width = int(fig.get_figwidth() * 15.5)  # characters per footer line at 8.6 pt
    fig.text(
        0.01,
        0.012,
        "\n".join(textwrap.wrap(foot, width) + textwrap.wrap(src, width)),
        fontsize=8.6,
        color=MUTED,
        va="bottom",
    )
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=200)
    fig.savefig(OUT / f"{name}.svg")
    plt.close(fig)


def record(name, title, claim, sources, rows, notes):
    CHARTS.append(
        {
            "name": name,
            "title": title,
            "claim": claim,
            "sources": sources,
            "rows": rows,
            "notes": notes,
        }
    )


# 1. Items lost by source ---------------------------------------------------------------------------
def chart_loss_by_source():
    a_rel, s_rel = "results/sprint/S1/analysis.json", "results/sprint/S1/swechat_ext/summary.json"
    cells = load(a_rel)["part_a"]["haiku-max"]["items"]["type_x_source"]
    tot = {}
    for c in cells:
        k, n = tot.get(c["source"], (0, 0))
        tot[c["source"]] = (k + c["n_lost"], n + c["n_items"])
    s = load(s_rel)
    pop = s["sets"]["population_estimate"]["pooled"]
    assert tot["swe-chat-v1"] == (
        s["sets"]["experiment1_250"]["pooled"]["n_lost"],
        s["sets"]["experiment1_250"]["pooled"]["n_items"],
    )
    sw = pop["lost_rate"]
    n_all = s["pool_size"] + s["sets"]["experiment1_250"]["n_records"]
    e1 = load("results/sprint/S1/judge_precision.json")["haiku-max"]["lost"]
    swp = load("results/sprint/judge_compare/summary.json")["metrics"]["by_set"]["swechat_ext"][
        "haiku"
    ]["precision_lost"]
    judge = MODEL.get(s["config"]["haiku"]["model"], s["config"]["haiku"]["model"])
    rows = [
        {"label": "Jacob's own\nClaude Code sessions\n(as sampled)", "src": "jacob"},
        {"label": "SWE-chat\n(real users)\npopulation-weighted", "src": "swe-chat-v1"},
        {"label": "PostTrainBench\n(as sampled)", "src": "posttrainbench"},
        {"label": "FrontierML\n(as sampled)", "src": "frontierml"},
    ]
    for r in rows:
        if r["src"] == "swe-chat-v1":
            r.update(
                value=sw["point"],
                lo=sw["lo"],
                hi=sw["hi"],
                n_text=f"n = {pop['n_items']:,} items",
                num=f"{100 * sw['point']:.1f}% [{100 * sw['lo']:.1f}, {100 * sw['hi']:.1f}]; "
                f"{pop['n_items']:,} items (observed, unweighted)",
            )
        else:
            k, n = tot[r["src"]]
            r.update(
                value=k / n,
                lo=None,
                hi=None,
                color=GRAY,
                n_text=f"n = {n:,} items",
                num=f"{k:,}/{n:,} = {100 * k / n:.1f}% (no interval computed)",
            )
    fig, ax = plt.subplots(figsize=(10, 6.4))
    bars(ax, rows, ymax=30)
    ax.set_ylabel("Items the judge marks lost (%)")
    fig.subplots_adjust(left=0.09, right=0.98, top=0.86, bottom=0.30)
    title = "Real compactions lose a lot of what the agent needs"
    foot = (
        "Share of actionable items (rules, permissions, decisions, commitments, task state) that the "
        "summary dropped or misstated and the agent can't get back another way, as called by the "
        f"LLM judge ({judge}) after its second look; not corrected for the judge's precision "
        f"({e1['n_hits']}/{e1['n']} pooled; {swp['k']}/{swp['n']} on SWE-chat). SWE-chat: 95% "
        f"session-bootstrap interval, weighted to all {n_all} eligible compactions. Gray bars: as "
        "sampled, no interval computed."
    )
    finish(
        fig,
        "01_loss_by_source",
        title,
        foot,
        [
            a_rel,
            s_rel,
            "results/sprint/S1/judge_precision.json",
            "results/sprint/judge_compare/summary.json",
        ],
    )
    record(
        "01_loss_by_source",
        title,
        f"Summaries lose about {100 * min(r['value'] for r in rows):.0f}-"
        f"{100 * max(r['value'] for r in rows):.0f}% of actionable items, by source.",
        [
            a_rel + " (part_a.haiku-max.items.type_x_source, summed over types)",
            s_rel + " (sets.population_estimate.pooled.lost_rate)",
            "results/sprint/S1/judge_precision.json (haiku-max.lost; footer)",
            "results/sprint/judge_compare/summary.json (metrics.by_set.swechat_ext.haiku; footer)",
        ],
        [(r["label"].replace("\n", " "), r["num"]) for r in rows],
        [
            (
                f"Footer: judge precision {e1['n_hits']}/{e1['n']} pooled, {swp['k']}/{swp['n']} on "
                f"SWE-chat; {n_all} eligible SWE-chat compactions (pool_size + Experiment 1's records); "
                f"judge {judge}."
            ),
        ],
    )


# 2. SWE-chat by item type --------------------------------------------------------------------------
def chart_swechat_by_type():
    rel = "results/sprint/S1/swechat_ext/summary.json"
    s = load(rel)
    prim = s["population_design"]["primary"]
    est = s["sets"]["population_estimate"]["lost_by_type"]
    names = {
        "rule": "Rules",
        "permission": "Permissions",
        "task_state": "Task state",
        "decision": "Decisions",
        "commitment": "Commitments",
    }
    rows = []
    for t, lab in names.items():
        p = prim[f"lost_{t}"]
        assert close(p["point"], est[t]["lost_rate"]["point"], 1e-9)
        n = est[t]["n_items"]
        rows.append(
            {
                "label": lab,
                "value": p["point"],
                "lo": p["lo"],
                "hi": p["hi"],
                "n_text": f"n = {n:,}",
                "num": f"{100 * p['point']:.1f}% [{100 * p['lo']:.1f}, {100 * p['hi']:.1f}]; {n:,} items",
            }
        )
    rows.sort(key=lambda r: -r["value"])
    fig, ax = plt.subplots(figsize=(10, 6.2))
    bars(ax, rows, ymax=45)
    ax.set_ylabel("Items the judge marks lost (%)")
    fig.subplots_adjust(left=0.09, right=0.98, top=0.86, bottom=0.24)
    title = "In real users' sessions, rules are lost most often"
    n250, n50, pool = (
        s["sets"]["experiment1_250"]["n_records"],
        s["n_extension_judged"],
        s["pool_size"],
    )
    w = s["population_estimate_weights"]
    judge = MODEL.get(s["config"]["haiku"]["model"], s["config"]["haiku"]["model"])
    foot = (
        f"SWE-chat compactions, weighted to all {n250 + pool} eligible (Experiment 1's {n250} at weight "
        f"{w['experiment1_250']:g}, a seeded random {n50} of the other {pool} at {w['extension']:g}). 95% "
        f"session-bootstrap intervals; n = items observed (unweighted). Judge's calls ({judge}), "
        "not precision-corrected."
    )
    finish(fig, "02_swechat_by_type", title, foot, [rel])
    record(
        "02_swechat_by_type",
        title,
        "Rules and permissions are lost most; decisions and commitments least.",
        [
            rel + " (population_design.primary.lost_<type>; n from "
            "sets.population_estimate.lost_by_type)"
        ],
        [(r["label"], r["num"]) for r in rows],
        [
            (
                f"Footer: {n250} records at weight {w['experiment1_250']:g} plus {n50} of {pool} at "
                f"{w['extension']:g} (population_estimate_weights, n_extension_judged, pool_size); judge "
                f"{judge}."
            )
        ],
    )


# 3. Judge precision against hand labels ------------------------------------------------------------
def chart_judge():
    jp_rel = "results/sprint/S1/judge_precision.json"
    cal_rel = "results/sprint/S2/lossaudit/calibration.json"
    jc_rel = "results/sprint/judge_compare/summary.json"
    e1 = load(jp_rel)["haiku-max"]["lost"]
    live = load(cal_rel)["heldout"]["precision_lost"]
    jc = load(jc_rel)["metrics"]
    sw = jc["by_set"]["swechat_ext"]["haiku"]["precision_lost"]
    sets = [
        ("Offline audit,\nall sources pooled", e1["n_hits"], e1["n"], e1["interval_wilson"], 0.01),
        ("Live runs\n(held-out cards)", live["k"], live["n"], live["wilson95_unweighted"], 1e-3),
        ("SWE-chat only", sw["k"], sw["n"], sw["wilson95"], 1e-3),
    ]
    left, notes = [], []
    for lab, k, n, file_ci, tol in sets:
        lo, hi = wilson(k, n)
        assert close(lo, file_ci[0], tol) and close(hi, file_ci[1], tol), (lab, lo, hi, file_ci)
        left.append(
            {
                "label": lab,
                "value": k / n,
                "lo": lo,
                "hi": hi,
                "n_text": f"n = {n} cards",
                "top": f"{k}/{n}",
                "num": f"{k}/{n} = {k / n:.2f} Wilson [{lo:.2f}, {hi:.2f}]",
            }
        )
    right = []
    for key, lab in (("haiku", "Haiku 5.5\n(the judge used)"), ("sonnet", "Sonnet 5.5")):
        p = jc["overall"][key]["precision_lost"]
        lo, hi = wilson(p["k"], p["n"])
        assert close(lo, p["wilson95"][0]) and close(hi, p["wilson95"][1])
        right.append(
            {
                "label": lab,
                "value": p["k"] / p["n"],
                "lo": lo,
                "hi": hi,
                "n_text": f"n = {p['n']} lost calls",
                "top": f"{p['k']}/{p['n']}",
                "num": f"{p['k']}/{p['n']} = {p['k'] / p['n']:.2f} Wilson [{lo:.2f}, {hi:.2f}]",
            }
        )
    n_cards = jc["overall"]["n_cards"]
    fig, (a1, a2) = plt.subplots(
        1, 2, figsize=(12, 6.2), gridspec_kw={"width_ratios": [3, 2]}, sharey=True
    )
    bars(a1, left, ymax=110)
    bars(a2, right, ymax=110)
    a1.set_ylabel('Judge\'s "lost" calls Jacob confirmed (%)')
    a1.set_title("By labelled set (Haiku judge)", fontsize=12, color=MUTED, loc="left")
    a2.set_title(f"Same {n_cards} cards, judge swapped", fontsize=12, color=MUTED, loc="left")
    fig.subplots_adjust(left=0.08, right=0.99, top=0.84, bottom=0.29, wspace=0.12)
    h = jc["overall"]["haiku"]["precision_lost"]
    title = f'The judge, checked by hand: about {round(10 * h["k"] / h["n"])} in 10 "lost" calls are right'
    first = re.search(r"\((\d+)/(\d+) = ", e1["note"])  # the gate as first scored
    foot = (
        "Precision = share of the judge's \"lost\" calls that Jacob's hand label also called lost. "
        '95% Wilson intervals. Recall was not measured. Offline audit: the definition of "lost" was '
        "settled after seeing the labels"
        + (f" (as first scored, {first[1]}/{first[2]})" if first else "")
        + ", on cards drawn from the judge's dropped calls only. Sonnet, a bigger model from the same "
        "family, had similar observed precision on these cards."
    )
    finish(fig, "03_judge_precision", title, foot, [jp_rel, cal_rel, jc_rel])
    record(
        "03_judge_precision",
        title,
        f"Judge precision is {min(r['value'] for r in left):.2f}-{max(r['value'] for r in left):.2f} "
        "by set; Sonnet's observed precision on the same cards is similar.",
        [
            jp_rel + " (haiku-max.lost)",
            cal_rel + " (heldout.precision_lost)",
            jc_rel + " (metrics.by_set.swechat_ext.haiku; metrics.overall)",
        ],
        [(r["label"].replace("\n", " "), r["num"]) for r in left + right],
        notes
        + [
            f"Title: {h['k']}/{h['n']} rounded to tenths (metrics.overall.haiku.precision_lost).",
            "Footer: definition settled after labels (judge_precision.json "
            "definition_settled_after_labels, measured_on)"
            + (
                f"; as first scored {first[1]}/{first[2]} (parsed from its note)." if first else "."
            ),
        ],
    )


# 4. Live runs: planted rules lost by channel -------------------------------------------------------
def chart_live_channels():
    rel = "results/sprint/S2/gate.json"
    g = load(rel)
    tally = Counter()
    for t in g["tiers"]:
        if t["assessable"]:
            tally[(t["channel"], "n")] += 1
            tally[(t["channel"], "k")] += bool(t["has_loss"])
    (n_rules,) = {len(t["labels"]) for t in g["tiers"]}
    rows = []
    for ch, lab in (
        ("readme", "In the repo's README"),
        ("agents_md", "In AGENTS.md\n(re-attached by\nthe harness)"),
        ("chat", "In a chat message"),
    ):
        k, n = tally[(ch, "k")], tally[(ch, "n")]
        file_k, file_n = (
            (g["agents_md_descriptive"]["n_lost"], g["agents_md_descriptive"]["n_assessable"])
            if ch == "agents_md"
            else (g["q2_yield"][ch]["n_lost"], g["q2_yield"][ch]["n_assessable"])
        )
        assert (k, n) == (file_k, file_n), ch
        lo, hi = wilson(k, n)
        if ch != "agents_md":
            assert close(lo, g["q2_yield"][ch]["loss_rate_wilson95"][0]) and close(
                hi, g["q2_yield"][ch]["loss_rate_wilson95"][1]
            )
        rows.append(
            {
                "label": lab,
                "value": k / n,
                "lo": lo,
                "hi": hi,
                "top": f"{k}/{n}",
                "n_text": f"n = {n} compactions",
                "num": f"{k}/{n} = {100 * k / n:.1f}% Wilson [{100 * lo:.1f}, {100 * hi:.1f}]",
            }
        )
    fig, ax = plt.subplots(figsize=(9, 6.2))
    bars(ax, rows, ymax=100)
    ax.set_ylabel("Compactions that lost a planted rule (%)")
    fig.subplots_adjust(left=0.10, right=0.98, top=0.86, bottom=0.27)
    title = "Live runs: only rules planted in the README were lost"
    n_setups, n_comp = len(g["coverage"]), sum(v["assessable"] for v in g["coverage"].values())
    foot = (
        f"{n_rules} rules planted through one channel per run; a compaction counts if at least one was "
        "missing from everything the agent resumed with (summary, kept messages, re-attached files), "
        f"by the retention judge with Jacob's spot check. {n_comp} compactions over {n_setups} agent "
        "setups. 95% Wilson intervals."
    )
    finish(fig, "04_live_rules_by_channel", title, foot, [rel])
    record(
        "04_live_rules_by_channel",
        title,
        f"README rules were lost at {rows[0]['top']} compactions; no loss was observed from AGENTS.md "
        f"or chat ({rows[1]['top']}, {rows[2]['top']}).",
        [rel + " (tiers tallied; matches q2_yield and agents_md_descriptive)"],
        [(r["label"].replace("\n", " "), r["num"]) for r in rows],
        [
            f"Footer: {n_rules} rules per run (labels per tier); {n_comp} compactions, {n_setups} setups."
        ],
    )


# 5. Live runs: commit-prefix rule broken, by arm, where it was lost --------------------------------
def chart_live_arms():
    rel = "results/sprint/S2/readout.json"
    d = load(rel)
    lost = d["rules"]["R6"]["lost"]
    c = lost["contrasts"]
    pairs = {}
    for v in c.values():
        for side in ("a", "b"):
            arm, kn = v[side], (v[f"{side}_k"], v[f"{side}_n"])
            assert pairs.setdefault(arm, kn) == kn, (arm, kn, pairs[arm])
    labels = {
        "F": "Full history\n(no compaction)",
        "S": "After compaction\n(as resumed)",
        "S+R": "As resumed +\nrules restated",
        "S+I": "As resumed +\nirrelevant facts",
    }
    rows = []
    for arm, lab in labels.items():
        k, n = (int(x) for x in pairs[arm])
        rows.append(
            {
                "label": lab,
                "value": k / n,
                "lo": None,
                "hi": None,
                "top": f"{k}/{n}",
                "n_text": f"n = {n} runs",
                "num": f"{k}/{n}",
            }
        )
    plain = {"readme": "the README", "agents_md": "AGENTS.md", "chat": "chat"}
    nb = lost["boundaries"]
    ((per_boundary, _),) = d["arms_per_boundary"].items()
    n_ways, reps = len(pairs), int(per_boundary) // len(pairs)
    chans = ", ".join(f"{v} in {plain[k]}" for k, v in lost["by_channel"].items())
    fig, ax = plt.subplots(figsize=(9.5, 6.2))
    bars(ax, rows, ymax=115)
    ax.set_yticks(range(0, 101, 20))
    ax.set_ylabel("Reruns that broke the commit-prefix rule (%)")
    fig.subplots_adjust(left=0.10, right=0.98, top=0.86, bottom=0.25)
    title = f"A lead, not a result: restoring a lost rule ({nb} compactions only)"
    foot = (
        f"The {nb} compactions after which the commit-prefix rule was missing from what the agent resumed "
        f"with (rules planted: {chans}). Each compaction point was rerun {n_ways} ways, {reps} reruns "
        "each; only reruns that made a commit count. "
        "Far too few for an interval."
    )
    finish(fig, "05_live_prefix_rule_by_arm", title, foot, [rel])
    record(
        "05_live_prefix_rule_by_arm",
        title,
        f"At the {nb} compactions that lost the rule, as-resumed and irrelevant-fact reruns broke it; "
        "full-history and rules-restated reruns did not.",
        [rel + " (rules.R6.lost.contrasts: a_k/a_n and b_k/b_n per arm)"],
        [(r["label"].replace("\n", " "), r["num"]) for r in rows],
        [
            f"{nb} compactions, channels {lost['by_channel']}; descriptive, too_small in the file.",
            f"Footer: {n_ways} arms x {reps} reruns = arms_per_boundary key {per_boundary}.",
        ],
    )


# 6. Simulated maintainer: requirements lost per summarizer -----------------------------------------
def chart_interact():
    base = "data/raw/sprint/s2/interact"
    sessions = {}
    for f in sorted(glob.glob(str(ROOT / base / "*" / "b*" / "boundary" / "eligibility.json"))):
        bdir = Path(f).parent
        e = json.loads(Path(f).read_text())
        if not e["eligible"] or not (bdir / "grades.json").exists():
            continue
        run = bdir.parents[1].name
        if run not in sessions or e["boundary_index"] < sessions[run][0]["boundary_index"]:
            sessions[run] = (e, bdir)  # the session's first qualifying compaction
    arms = {
        "live": "Live summary\n(pi's prompt)",
        "pi": "pi's prompt",
        "cc_prompt": "Claude Code's\nprompt",
        "codex_local": "Codex's\nprompt",
        "opencode": "OpenCode's\nprompt",
        "antigravity": "Antigravity-\nstyle",
    }
    lost, unclear, tasks_with_loss = Counter(), Counter(), {a: set() for a in arms}
    n_req, files, live_models, replay_models = 0, [], set(), set()
    for run, (e, bdir) in sorted(sessions.items()):
        grades = json.loads((bdir / "grades.json").read_text())
        fresh = set(e["fresh_ids"])
        assert fresh <= set(grades["live"]) and len(fresh) == e["n_fresh"]
        n_req += len(fresh)
        files += [bdir / "grades.json", bdir / "eligibility.json"]
        live_models.add(json.loads((bdir.parents[1] / "run.json").read_text())["plan"]["model"])
        files.append(bdir.parents[1] / "run.json")
        for arm in arms:
            g = grades[arm]
            for rid in fresh:
                if g[rid] in LOST:
                    lost[arm] += 1
                    tasks_with_loss[arm].add(run)
                elif g[rid] == "unclear":
                    unclear[arm] += 1
            if arm != "live":
                replay_models.add(
                    json.loads((bdir / "replays" / arm / "replay.json").read_text())["model"]
                )
                files.append(bdir / "replays" / arm / "replay.json")
    rows = []
    (min_fresh,) = {e["min_fresh"] for e, _ in sessions.values()}
    rep_lost = [lost[a] for a in arms if a != "live"]
    for arm, lab in arms.items():
        top = f"{lost[arm]}/{n_req}" + (f"\n(+{unclear[arm]} unclear)" if unclear[arm] else "")
        rows.append(
            {
                "label": lab,
                "value": lost[arm],
                "lo": None,
                "hi": None,
                "top": top,
                "color": GRAY if arm == "live" else BLUE,
                "n_text": f"n = {n_req}",
                "num": f"{lost[arm]}/{n_req} lost, {unclear[arm]} unclear; sessions with a loss: "
                f"{len(tasks_with_loss[arm])}",
            }
        )
    cx_op = tasks_with_loss["codex_local"] | tasks_with_loss["opencode"]
    one_task = len(cx_op) == 1
    fig, ax = plt.subplots(figsize=(11, 6.4))
    bars(ax, rows, pct=False, ymax=max(lost.values()) + 2.5)
    ax.set_ylabel(f"Requirements lost (of {n_req})")
    ax.axvline(0.5, color="#d6d5d0", lw=1)
    ax.annotate(
        "written in the run", (0, ax.get_ylim()[1]), ha="center", va="top", fontsize=10, color=MUTED
    )
    ax.annotate(
        "replayed afterwards on the same history",
        (3, ax.get_ylim()[1]),
        ha="center",
        va="top",
        fontsize=10,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.86, bottom=0.27)
    title = "Simulated maintainer: requirements in clear messages mostly survive"
    foot = (
        f"{len(sessions)} sessions; {n_req} requirements the maintainer stated since the previous "
        "compaction, grounded in the conversation and no longer in the messages kept verbatim, at each "
        f"session's first compaction with at least {min_fresh} such requirements. Lost = dropped or "
        "misstated in what the agent resumes with. "
        + ("All Codex and OpenCode losses are in one task. " if one_task else "")
        + f"Different models wrote the live ({', '.join(sorted(MODEL.get(m, m) for m in live_models))}) and "
        f"replayed ({', '.join(sorted(MODEL.get(m, m) for m in replay_models))}) summaries, so this is not a ranking of prompts. Descriptive; "
        "no interval (losses cluster by session)."
    )
    rels = [str(p.relative_to(ROOT)) for p in files]
    STATS["interact"] = {
        "n_req": n_req,
        "n_sessions": len(sessions),
        "unclear": {arms[a].replace("\n", " "): unclear[a] for a in arms if unclear[a]},
    }
    finish(
        fig,
        "06_maintainer_requirements_lost",
        title,
        foot,
        [f"{base}/*/b*/boundary/grades.json (first qualifying compaction per session)"],
    )
    record(
        "06_maintainer_requirements_lost",
        title,
        f"Few requirements are lost: replays lose {min(rep_lost)}-{max(rep_lost)} of {n_req}, the live "
        f"summary {lost['live']}.",
        [f"{r} (sha256 {sha(r)})" for r in rels],
        [(r["label"].replace("\n", " "), r["num"]) for r in rows],
        [
            f"Sessions: {', '.join(sorted(sessions))}.",
            f"Codex/OpenCode losses in: {', '.join(sorted(cx_op))}.",
            "These raw files are gitignored (data/raw/), not committed.",
            (
                f"Footer: qualifying = eligible with at least min_fresh = {min_fresh} fresh requirements "
                "(eligibility.json); fresh excludes in-tail, inherited and ungrounded requirements."
            ),
        ],
    )


# 7. Hand labels per check --------------------------------------------------------------------------
def chart_labels():
    rel = "docs/sprint/progress.md"
    text = (ROOT / rel).read_text()
    m = re.search(
        r"\| What Jacob labelled \| Cards \|.*?\n\|---\|---\|---\|\n(.*?)\n\| \*\*Total so far\*\* "
        r"\| \*\*(\d+)\*\* \|",
        text,
        re.DOTALL,
    )
    if m is None:
        raise KeyError(f"hand-labelling table not found in {rel}")
    rows, total, reused = [], int(m.group(2)), None
    for line in m.group(1).splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0].startswith("(reuse)"):
            reused = (cells[0].removeprefix("(reuse) "), re.match(r"\d+", cells[1]).group())
            continue  # relabels earlier cards; adds none
        n = int(cells[1])
        rows.append(
            {
                "label": "\n".join(textwrap.wrap(cells[0], 15)),
                "value": n,
                "lo": None,
                "hi": None,
                "n_text": "",
                "top": str(n),
                "num": str(n),
            }
        )
    assert sum(r["value"] for r in rows) == total, (sum(r["value"] for r in rows), total)
    fig, ax = plt.subplots(figsize=(14, 6.6))
    bars(ax, rows, pct=False, ymax=max(r["value"] for r in rows) * 1.15)
    ax.set_ylabel("Cards Jacob labelled")
    fig.subplots_adjust(left=0.06, right=0.99, top=0.86, bottom=0.30)
    title = f"{total} hand-labelled cards across {len(rows)} checks of the LLM pipeline"
    foot = (
        "Labelled on private labelling pages with the answer key kept out of the page, cards in seeded "
        "random order, labels read back by code. These are sampled checks (validations, audits, spot "
        "checks): not every plotted estimate has its own."
        + (
            f" The {reused[0]} reused {reused[1]} of these cards and is not counted again."
            if reused
            else ""
        )
    )
    finish(fig, "07_hand_labels", title, foot, [rel + " (hand-labelling table)"])
    record(
        "07_hand_labels",
        title,
        f"{total} hand-labelled cards across {len(rows)} checks.",
        [rel + ' (table under "Hand labelling")'],
        [(r["label"].replace("\n", " "), r["num"]) for r in rows],
        [f"Total row in the file: {total}."],
    )


def write_readme():
    out = [
        "# Sprint figures",
        "",
        (
            "Generated by `scripts/sprint_figs.py` (`uv run python scripts/sprint_figs.py`); every number "
            "below is read from the listed file at run time. PNG (200 dpi) and SVG for each chart."
        ),
        "",
    ]
    for c in CHARTS:
        out += [
            f"## {c['name']}: {c['title']}",
            "",
            f"![{c['title']}]({c['name']}.png)",
            "",
            f"**Claim:** {c['claim']}",
            "",
            "**Source:**",
        ]
        out += [
            f"- `{s.split(' (')[0]}`" + (f" ({s.split(' (', 1)[1]}" if " (" in s else "")
            for s in c["sources"]
        ]
        out += ["", "| Bar | Numbers plotted |", "|---|---|"]
        out += [f"| {lab.replace('- ', '-')} | {num} |" for lab, num in c["rows"]]
        if c["notes"]:
            out += [""] + [f"- {n}" for n in c["notes"]]
        out.append("")
    if SKIPPED:
        out += ["## Skipped charts", ""] + [f"- {n}: {why}" for n, why in SKIPPED] + [""]
    brief = (ROOT / BRIEF).read_text()
    b_req, b_ses = re.search(r"of (\d+)\),", brief), re.search(r'"(\d+) sessions;', brief)
    i = STATS.get("interact")
    out += [f"## Where the files differ from the brief (`{BRIEF}`)", ""]
    if i and b_req and b_ses:
        uncl = ", ".join(f"{k} {v}" for k, v in i["unclear"].items())
        out.append(
            f"- **Chart 6 denominators:** the brief says {b_req[1]} requirements over {b_ses[1]} "
            f"sessions; the files give {i['n_req']} over {i['n_sessions']}. The difference is helm's "
            "rerun (its first attempt crashed), whose requirements every summary kept; "
            "`docs/sprint/progress.md` gives the same total in its go-line entry. The brief also omits "
            f"the unclear grades ({uncl}), shown on the chart. Loss counts match the brief."
        )
    out += [
        (
            "- **Chart 6 sources are not committed:** the brief points at "
            '`data/raw/sprint/s2/interact/`, which is gitignored, against its own "committed files '
            "only\" rule. The script reads them as named and records each file's sha256 above."
        ),
        (
            "- **Style:** `docs/pitch/make_figs.py` does not exist; the conventions of "
            "`docs/pitch/fig_*.py` were followed instead."
        ),
        "",
    ]
    (OUT / "README.md").write_text("\n".join(out))


def main():
    for f in (
        chart_loss_by_source,
        chart_swechat_by_type,
        chart_judge,
        chart_live_channels,
        chart_live_arms,
        chart_interact,
        chart_labels,
    ):
        try:
            f()
        except (FileNotFoundError, KeyError) as exc:  # data missing: skip the chart and say so
            SKIPPED.append((f.__name__, f"{type(exc).__name__}: {exc}"))
            print(f"SKIPPED {f.__name__}: {exc}")
    write_readme()
    print("\n".join(f"{c['name']}: {len(c['rows'])} bars" for c in CHARTS))


if __name__ == "__main__":
    main()
