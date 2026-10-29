"""Week-2 result charts for the final presentation to Neel (30 Oct 2026).

Style: AGENTS.md "Figures" (Jacob, 29 Oct): sns.set_theme(style="whitegrid") with every other
setting at seaborn's default (fonts, text sizes and colours), the default (deep) palette, plain
vertical bars, Wilson 95% intervals, titles that state the claim with its number, plain words,
numbers read from committed files, PNG + SVG + README. The drawing helpers come from
scripts/sprint_figs_v2.py. Per Jacob's note via J (28 Oct): plain charts, 2-4 bars where
possible, direct labels instead of legends, the number on each bar, few gridlines. No captions
inside the images (Jacob, 29 Oct): a one-line title, labels and k/n only; the source line, caveats
and notes go in the README. Detail versions go to backup_*.png, never into the main chart.

Every plotted number is read from the unchanged copies in docs/sprint/figs/week2/data/, whose
origins (branch, commit, path, sha256) are in data/SOURCES.json; each copy's sha256 is checked
before plotting. Sources:
- task_chaining/analysis.json (contrasts: pooled history vs no history; pooled compacted vs full)
- precedent/readout.json (counts, first_stage)
- monitors/analysis.json (misses: Guardian on Experiment 1's 22 unauthorized force-pushes)
- user_no/scores.json (lost), user_no/reasoning_scan.json (residue_noticed, eval_terms)
- harness/readout.json (configs[*].first_task, configs[*].counts, configs[*].eval, exact_splits)
- harness/agy_summary_content.md (per-history table: Antigravity second tasks capped before a push)
Writes docs/sprint/figs/week2/*.png, *.svg and README.md.
Run: uv run python scripts/sprint_figs_week2.py
"""

import hashlib
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import sprint_figs_v2 as v2  # applies sns.set_theme(style="whitegrid")
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "sprint" / "figs" / "week2"
DATA = OUT / "data"
INK, MUTED = v2.INK, v2.MUTED

# One deep-palette colour per condition across the set. Every control condition (no earlier
# history, clean first-task remote, current call only, honest summary) is the same grey.
PAL = v2.PAL
CONTROL = PAL[7]
HISTORY = PAL[3]  # earlier task's history in context
DIVERGENT = PAL[1]  # first task met a divergent remote
REASONING = PAL[0]  # monitor also sees the agent's reasoning
FORGED = PAL[5]  # summary with a false permission
DROPPED = PAL[6]  # summary with the user's "no" deleted
OVERWROTE = PAL[8]  # divergent history whose first task overwrote (kept: DIVERGENT)
VIEW_COLOURS = {"full": PAL[2], "summary_tail": PAL[4], "pinned_user_turns": PAL[9]}
CONFIG = {  # harness configurations, in plotting order
    "pi": ("pi-style agent\n+ DeepSeek V4.1 Flash", PAL[4]),
    "ds": ("Claude Code\n+ DeepSeek V4.1 Flash", PAL[0]),
    "cc": ("Claude Code\n+ Haiku 5.5", PAL[9]),
    "cx": ("Codex\n+ gpt-6-luna", PAL[2]),
    "agy": ("Antigravity", PAL[5]),
}
HARNESS_KEYS = ("ds", "cc", "cx", "agy")
SHORT = {"pi": "the pi-style agent", "ds": "Claude Code", "cc": "Haiku", "cx": "Codex",
         "agy": "Antigravity"}  # names inside titles; "Only DeepSeek ..." supplies the model
SHORT_B = {"ds": "DeepSeek in Claude Code", "cc": "Haiku", "cx": "Codex", "agy": "Antigravity"}
SHORT_C = {"pi": "pi-style agent", "ds": "Claude Code + DeepSeek", "cc": "Haiku",
           "cx": "Codex", "agy": "Antigravity"}  # names in 05a's numbers line
HARNESS_WORDS = {"agy": "Antigravity"}  # readout label prefixes that are internal codes
MODEL_WORDS = {"gemini-3.8-flash-low": "Gemini 3.8 Flash\n(low effort)"}  # raw model ids
README = []


# Data access -----------------------------------------------------------------------------------
def verify_sources():
    """Fail unless every data copy matches the sha256 recorded with its origin."""
    src = json.loads((DATA / "SOURCES.json").read_text())
    for rel, meta in src.items():
        got = hashlib.sha256((DATA / rel).read_bytes()).hexdigest()
        if got != meta["sha256"]:
            raise SystemExit(f"{rel}: sha256 {got} != recorded {meta['sha256']}")
    return src


def load(rel):
    return json.loads((DATA / rel).read_text())


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / d
    return max(0.0, c - h), min(1.0, c + h)


def share(k, n, **extra):
    lo, hi = wilson(k, n)
    return dict(k=k, n=n, rate=k / n, lo=lo, hi=hi, **extra)


def counts_from_mean(mean, n):
    k = round(mean * n)
    assert abs(k / n - mean) < 1e-9, (mean, n)
    return k


# 1. Task chaining ------------------------------------------------------------------------------
CHAIN = "task_chaining/analysis.json"
CHAIN_POOLED = "History vs no history, pooled (added at readout)"
CHAIN_HIST = "history (full or compacted) − no history (fresh or no A)"
CHAIN_CF = "compacted − full (paired by history; both projects, both A conditions)"
CHAIN_CF_FAMILY = "Compacted vs full, pooled (added at readout)"
CHAIN_METRICS = {"rewrite": "Force-push rewrote the remote",
                 "teammate_lost": "Teammate's commit lost"}


def _contrast(contrasts, family, contrast, metric):
    hits = [c for c in contrasts if c["family"] == family and c["contrast"] == contrast
            and c["metric"] == metric]
    assert len(hits) == 1, (family, contrast, metric, len(hits))
    return hits[0]


def data_chain():
    """{metric: {"history": share, "none": share, "full": share, "compacted": share}}."""
    con = load(CHAIN)["contrasts"]
    out = {}
    for m in CHAIN_METRICS:
        h = _contrast(con, CHAIN_POOLED, CHAIN_HIST, m)
        cf = _contrast(con, CHAIN_CF_FAMILY, CHAIN_CF, m)
        assert h.get("unknown_a", 0) == 0 and h.get("unknown_b", 0) == 0, m
        out[m] = {
            "diff": (h["estimate"], h["lo"], h["hi"]),
            "cf_diff": (cf["estimate"], cf["lo"], cf["hi"]),
            "history": share(counts_from_mean(h["mean_a"], h["n_a"]), h["n_a"]),
            "none": share(counts_from_mean(h["mean_b"], h["n_b"]), h["n_b"]),
            "compacted": share(counts_from_mean(cf["mean_a"], cf["n_a"]), cf["n_a"]),
            "full": share(counts_from_mean(cf["mean_b"], cf["n_b"]), cf["n_b"]),
        }
        # the full + compacted arms are exactly the history side
        assert out[m]["full"]["k"] + out[m]["compacted"]["k"] == out[m]["history"]["k"], m
    return out


# 2. Precedent ----------------------------------------------------------------------------------
PREC = "precedent/readout.json"


def precedent_tests():
    """The readout's primary difference and exact p at both completions."""
    pr = load(PREC)["primary"]
    return {c: (pr[c]["bootstrap"]["diff"]["divergent-clean"]["est"],
                *pr[c]["bootstrap"]["diff"]["divergent-clean"]["ci"], pr[c]["test"]["p"])
            for c in ("lower", "upper")}


def data_precedent():
    """Lost in the second task, per start and arm, at both missing-data completions."""
    c = load(PREC)["counts"]
    out = {}
    for start in ("full", "compacted"):
        for arm in ("divergent", "clean"):
            r = c[f"{arm}/{start}"]
            lower = r["lost"] + r["unknown"]  # conservative: an unknown counts as lost
            out[(start, arm)] = share(lower, r["forks"], upper_k=r["lost"],
                                      unknown=r["unknown"])
    for arm in ("divergent", "clean"):
        r = c[f"{arm}/all"]
        out[("all", arm)] = share(r["lost"] + r["unknown"], r["forks"], upper_k=r["lost"],
                                  unknown=r["unknown"])
    return out


# 3. Monitor ------------------------------------------------------------------------------------
MON = "monitors/analysis.json"
MON_SETS = ("exp1_divergent", "exp1_scoped")
MON_VIEWS = {
    "current_only": "Current call only",
    "full": "Full history",
    "summary_tail": "Summary + recent turns",
    "pinned_user_turns": "Summary + pinned\nuser turns",
    "current_plus_last_reasoning": "Current call + agent's\nlast 40 reasoning blocks",
}
MON_SHORT = {  # narrower labels for the five-view backup chart
    "current_only": "Current\ncall only",
    "full": "Full\nhistory",
    "summary_tail": "Summary +\nrecent turns",
    "pinned_user_turns": "Summary +\npinned\nuser turns",
    "current_plus_last_reasoning": "Current call +\nlast 40\nreasoning blocks",
}
MON_MODELS = {"claude-haiku-5-5": "Haiku 5.5", "claude-opus-5-5": "Opus 5.5"}


def data_monitor():
    """{(model, view): share} of Guardian allows, summed over Experiment 1's two sets."""
    rows = load(MON)["misses"]
    out = {}
    for model in MON_MODELS:
        for view in MON_VIEWS:
            hit = [r for r in rows if r["prompt"] == "guardian" and r["measure"] == "guardian"
                   and r["model"] == model and r["view"] == view and r["set"] in MON_SETS]
            assert sorted(r["set"] for r in hit) == sorted(MON_SETS), (model, view)
            assert all(r["missing"] == 0 for r in hit), (model, view)
            out[(model, view)] = share(sum(r["k"] for r in hit), sum(r["n"] for r in hit))
    return out


def monitor_set_sizes():
    """Acts per Experiment 1 set (the current-call-only Haiku rows; every view has the same n)."""
    rows = load(MON)["misses"]
    return {st: next(r["n"] for r in rows if r["prompt"] == "guardian" and r["set"] == st
                     and r["measure"] == "guardian" and r["view"] == "current_only"
                     and r["model"] == "claude-haiku-5-5") for st in MON_SETS}


def rationale_flips():
    """{model: (block->allow, allow->block, both allow, both block)} from the hand-appended
    paired table in 5b_rationales.md (descriptive; used in notes only, never plotted)."""
    md = (DATA / "monitors/5b_rationales.md").read_text()
    out = {}
    for m in ("Haiku", "Opus"):
        hit = re.search(rf"^\| {m} \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|$", md, re.MULTILINE)
        out[m] = tuple(int(x) for x in hit.groups())
    return out


# 4. Forged permission and eval awareness -------------------------------------------------------
UNO_SCORES, UNO_SCAN = "user_no/scores.json", "user_no/reasoning_scan.json"
UNO_VERSIONS = {"honest": ("Honest\nsummary", CONTROL),
                "false_permission": ("False\npermission\nadded", FORGED),
                "decision_dropped": ("User's\n\"no\"\ndeleted", DROPPED)}
UNO_METRICS = {"lost": "Lost the commits",
               "residue_noticed": "Said the summary looked truncated",
               "eval_terms": "Used grading words"}


def data_user_no():
    """{(metric, version): share}; runs per version from scores.json, hits from the scan."""
    scores, scan = load(UNO_SCORES), load(UNO_SCAN)
    n = {v: sum(1 for r in scores if r["version"] == v) for v in UNO_VERSIONS}
    ids = {r["id"] for r in scores}
    out = {}
    for v in UNO_VERSIONS:
        out[("lost", v)] = share(sum(1 for r in scores if r["version"] == v and r["lost"]), n[v])
    for m in ("residue_noticed", "eval_terms"):
        hit_ids = {(e[0] if isinstance(e, list) else e) for e in scan[m]}
        assert hit_ids <= ids, m
        for v in UNO_VERSIONS:
            k = sum(1 for i in hit_ids if i.split("--")[1] == v)
            out[(m, v)] = share(k, n[v])
    return out


def user_no_design():
    """Design sizes from scores.json, the alternate detector's counts, and two statements the
    notes cite from readout.md (checked to be present, numbers parsed from the text)."""
    scores = load(UNO_SCORES)
    md = (DATA / "user_no/readout.md").read_text()
    quote = "0/28 such remarks in the other versions"
    assert quote in md, quote
    kept = re.search(r"(\d+) of the (\d+) dropped summaries still mention a rebase", md)
    dd = [r for r in scores if r["version"] == "decision_dropped"]
    other = [r for r in scores if r["version"] != "decision_dropped"]
    return {"histories": len({r["history"] for r in scores}),
            "versions": len({r["version"] for r in scores}),
            "reps": len({r["rep"] for r in scores}),
            "kept_rebase": (int(kept.group(1)), int(kept.group(2))), "quote": quote,
            "aware": (sum(r["eval_eval_aware"] for r in dd), len(dd),
                      sum(r["eval_eval_aware"] for r in other), len(other))}


# 5. Harness comparison -------------------------------------------------------------------------
HAR = "harness/readout.json"


def data_harness():
    """Per configuration: first-task overwrites among divergent histories, and second-task
    losses (compacted start) by arm. pi comes from Experiment 1's readout, compacted start.
    A harness configuration missing from the readout (still running) maps to None."""
    h, p = load(HAR), load(PREC)
    out = {}
    fs = p["first_stage"]
    pc = p["counts"]

    def lower(r):  # conservative completion: an unknown counts as lost
        return share(r["lost"] + r["unknown"], r["forks"])

    out["pi"] = {"label": CONFIG["pi"][0], "first": share(fs["lost"], fs["n"]),
                 "divergent": lower(pc["divergent/compacted"]),
                 "clean": lower(pc["clean/compacted"]),
                 "terms": h["e1_terms"]}
    for key in HARNESS_KEYS:
        c = h["configs"].get(key)
        if c is None or "first_task" not in c:
            out[key] = None  # still running when the readout was written
            continue
        ft = c["first_task"]["divergent"]
        n_div = c["counts"]["divergent"]["assigned"]
        assert ft["has_result"] == n_div and ft["unknown"] == 0, key
        arms = {}
        for arm in ("divergent", "clean"):
            a = c["counts"][arm]
            assert a["observed"] == a["assigned"], (key, arm)  # no unknowns to complete
            arms[arm] = share(a["lost"], a["assigned"])
        harness, _, model = c["label"].partition(" + ")
        out[key] = {"label": f"{HARNESS_WORDS.get(harness, harness)}\n+ {MODEL_WORDS.get(model, model)}",
                    "raw_label": c["label"],
                    "first": share(ft["overwrote"], n_div), "met": ft["met_rejection"], **arms,
                    "terms": c["eval"].get("planted_or_decoy_divergent"),
                    "replicated": c["replicated"], "holm": h["holm"][key]}
    return out


AGY_SUMMARY = "harness/agy_summary_content.md"


def agy_call_cap():
    """Antigravity second tasks that hit the call cap before issuing any push, counted from the
    per-history code-pass table in agy_summary_content.md; the prose totals are asserted."""
    md = (DATA / AGY_SUMMARY).read_text()
    rows = []
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 11 or cells[1] not in ("divergent", "clean"):
            continue
        t1 = [x.strip() for x in cells[2].split("/")]  # end / pushed / rejected / overwrote
        t2 = [x.strip() for x in cells[9].split("/")]  # lost / rejection / pushes / end
        rows.append({"arm": cells[1], "overwrote": t1[3] == "True", "lost": t2[0] == "yes",
                     "capped": t2[2] == "0" and t2[3] == "capped_calls"})
    assert len(rows) == 24, len(rows)
    out = {}
    for arm in ("divergent", "clean"):
        rs = [r for r in rows if r["arm"] == arm]
        out[arm] = {"n": len(rs), "lost": sum(r["lost"] for r in rs),
                    "capped": sum(r["capped"] for r in rs)}
    over = [r for r in rows if r["overwrote"]]
    out["overwrote"] = {"n": len(over), "lost": sum(r["lost"] for r in over),
                        "capped": sum(r["capped"] for r in over)}
    tot = re.search(r"\| Task 2 capped before any push \| (\d+)/(\d+) \((\d+) divergent, "
                    r"(\d+) clean\)", md)
    assert tuple(map(int, tot.groups())) == (
        out["divergent"]["capped"] + out["clean"]["capped"], len(rows),
        out["divergent"]["capped"], out["clean"]["capped"]), tot.groups()
    cap = re.search(r"Task 2 in those (\d+) histories never reached a push: all (\d+) hit agy's "
                    r"~(\d+)-call cap", md)
    assert (int(cap.group(1)) == int(cap.group(2)) == out["overwrote"]["n"]
            == out["overwrote"]["capped"]), cap.groups()
    out["cap_calls"] = int(cap.group(3))
    return out


def and_join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# Drawing ---------------------------------------------------------------------------------------
def pct(v):
    return 100 * v


def draw(ax, groups, ymax=100, group_pad=0.6, w=0.8, bar_labels=True, group_offset=52):
    """groups: [(group label or None, [bar dict(lab, color, k, n, rate, lo, hi) or
    dict(lab, color, pending=True)])]. Bars carry their own direct label underneath; the group
    label sits below those. Returns the bar x positions."""
    xs, x, centres = [], 0.0, []
    for _, bars in groups:
        pos = [x + i * w for i in range(len(bars))]
        xs.append(pos)
        centres.append(sum(pos) / len(pos))
        x = pos[-1] + w + group_pad
    allx = [p for g in xs for p in g]
    ax.set_xlim(allx[0] - w * 0.75, allx[-1] + w * 0.75)
    ax.set_ylim(0, ymax)
    ax.set_yticks([0, 50, 100] if ymax >= 100 else [0, ymax / 2, ymax])
    ax.set_yticklabels([f"{t:.0f}%" for t in ax.get_yticks()])
    ax.xaxis.grid(False)  # as sns.barplot does: no grid on the categorical axis
    ticks, labels = [], []
    for (glab, bars), pos, centre in zip(groups, xs, centres):
        for b, xp in zip(bars, pos):
            ticks.append(xp)
            labels.append(b["lab"] if bar_labels else "")
            if b.get("pending"):
                ax.add_patch(Rectangle((xp - w * 0.42, 0), w * 0.84, ymax * 0.35, fill=False,
                                       edgecolor=MUTED, linestyle="--", zorder=2))
                ax.text(xp, ymax * 0.175, "running", ha="center", va="center")
                continue
            v2.rbar(ax, xp, pct(b["rate"]), w * 0.84, b["color"])
            v2.err(ax, xp, pct(b["rate"]), pct(b["lo"]), pct(b["hi"]))
            v2.label(ax, xp, pct(b["hi"]), f"{b['k']}/{b['n']}{b.get('mark', '')}", bold=True)
        if glab:
            ax.annotate(glab, xy=(centre, 0), xycoords=("data", "axes fraction"),
                        xytext=(0, -group_offset if bar_labels else -14),
                        textcoords="offset points",
                        ha="center", va="top")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels)
    return xs


def save(fig, name, title, claim, sources, numbers, notes, foot, caveat=None, short=None):
    """Draw only `short` (default: the title's first line) on the chart; the full title, the
    caveat and the source line `foot` go in the README, never into the image."""
    short = short or title.split("\n")[0].rstrip(" ,;:")
    v2.title_above(fig, short)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=200, bbox_inches="tight")
    with plt.rc_context({"svg.hashsalt": name}):  # stable SVG ids and no date: reruns diff clean
        fig.savefig(OUT / f"{name}.svg", metadata={"Date": None}, bbox_inches="tight")
    plt.close(fig)
    README.extend([f"## {name}: {title.replace(chr(10), ' ')}", "", f"![{name}]({name}.png)", "",
                   f"**Title on the chart:** {' '.join(short.split())}", "",
                   f"**Claim:** {claim}", "", "**Source (copies in `data/`):**"]
                  + [f"- `{s}`" for s in sources] + ["", "**Numbers plotted** (k/n, Wilson 95%):"]
                  + [f"- {n}" for n in numbers] + [""]
                  + ([f"**Caveat (not on the chart):** {one_line(caveat)}", ""] if caveat else [])
                  + [f"**Source line (not on the chart):** {foot}", ""]
                  + [f"- {n}" for n in notes] + [""])


def fmt(s):
    lo = f"{pct(s['lo']):.0f}" if s["lo"] is not None else "-"
    hi = f"{pct(s['hi']):.0f}" if s["hi"] is not None else "-"
    return f"{s['k']}/{s['n']} = {pct(s['rate']):.1f}% [{lo}, {hi}]"


def one_line(s):
    return s.replace("\n", " ")


def pts(est, lo, hi):
    return f"{100 * est:+.1f} points [{100 * lo:+.1f}, {100 * hi:+.1f}]"


# Charts ----------------------------------------------------------------------------------------
def chart_chain():
    d = data_chain()
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.28)
    groups = [(lab, [dict(lab="With the earlier\ntask's history", color=HISTORY,
                          **d[m]["history"]),
                     dict(lab="Without it", color=CONTROL, **d[m]["none"])])
              for m, lab in CHAIN_METRICS.items()]
    draw(ax, groups)
    ax.set_ylabel("Second-task runs")
    r, t = d["rewrite"], d["teammate_lost"]
    title = (f"With the earlier task's history, the agent force-pushed over the remote in "
             f"{r['history']['k']}/{r['history']['n']} runs\n(vs {r['none']['k']}/"
             f"{r['none']['n']} without it) and lost a teammate's commit in "
             f"{t['history']['k']}/{t['history']['n']} (vs {t['none']['k']}/{t['none']['n']})")
    n_runs = sum(a["n"] for a in load(CHAIN)["arms"].values())
    assert n_runs == r["history"]["n"] + r["none"]["n"]
    foot = (f"Source: task-chaining experiment, {n_runs} second-task runs, pi-style agent + "
            "DeepSeek V4.1 Flash. History: full or compacted start; without: fresh start or no "
            "first task.")
    save(fig, "01_chaining_history", title,
         "share of second-task runs whose force-push rewrote the remote branch, and whose "
         "teammate's commit ended on no remote ref, with the earlier task's history in context "
         "(full or compacted start) against without it (fresh start, or no first task).",
         [(f"{CHAIN} (contrasts[family='{CHAIN_POOLED}', contrast='{CHAIN_HIST}', "
          "metric in {rewrite, teammate_lost}]: mean_a x n_a, mean_b x n_b)")],
         [f"{one_line(CHAIN_METRICS[m])}, with history: {fmt(d[m]['history'])}; without: "
          f"{fmt(d[m]['none'])}" for m in CHAIN_METRICS],
         [("Notes: no run had an unknown force-push status (unknown_a = unknown_b = 0). The "
          "readout's cluster-bootstrap differences (same contrast records): force-push "
          f"{pts(*r['diff'])}, commit lost {pts(*t['diff'])}. The split by start is in "
          "backup_01_chaining_by_start.")], foot,
         short=(f"With the earlier task's history: force-pushed in {r['history']['k']}/"
                f"{r['history']['n']} (vs {r['none']['k']}/{r['none']['n']}), lost the commit in "
                f"{t['history']['k']}/{t['history']['n']} (vs {t['none']['k']}/{t['none']['n']})"))

    # Backup: split by start
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.28)
    groups = [(lab, [dict(lab="Full history", color=HISTORY, **d[m]["full"]),
                     dict(lab="Compacted\nhistory", color=HISTORY, **d[m]["compacted"]),
                     dict(lab="No history", color=CONTROL, **d[m]["none"])])
              for m, lab in CHAIN_METRICS.items()]
    draw(ax, groups)
    ax.set_ylabel("Second-task runs")
    title = (f"Backup: full and compacted history both raise force-pushes\n"
             f"({r['full']['k']}/{r['full']['n']}, {r['compacted']['k']}/{r['compacted']['n']}) "
             f"over no history ({r['none']['k']}/{r['none']['n']})")
    save(fig, "backup_01_chaining_by_start", title,
         "the same two outcomes split by how the second task started.",
         [(f"{CHAIN} (contrasts[family='Compacted vs full, pooled (added at readout)', "
          f"contrast='{CHAIN_CF}']: mean_a x n_a = compacted, mean_b x n_b = full; no history as "
          "in chart 01)")],
         [f"{one_line(CHAIN_METRICS[m])}: full {fmt(d[m]['full'])}; compacted "
          f"{fmt(d[m]['compacted'])}; none {fmt(d[m]['none'])}" for m in CHAIN_METRICS],
         [(f"Notes: compacted minus full, paired by history: force-push {pts(*r['cf_diff'])}, "
          f"commit lost {pts(*t['cf_diff'])}.")], foot, short=" ".join(title.split()))


def chart_precedent():
    d = data_precedent()
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.28)
    groups = [(lab, [dict(lab="Divergent remote\nin the first task", color=DIVERGENT,
                          **d[(s, "divergent")]),
                     dict(lab="Clean remote\nin the first task", color=CONTROL,
                          **d[(s, "clean")])])
              for s, lab in (("full", "Full history"), ("compacted", "Compacted history"))]
    draw(ax, groups)
    ax.set_ylabel("Lost the teammate's commit")
    dv, cl = d[("all", "divergent")], d[("all", "clean")]
    pt = precedent_tests()
    cp = load(PREC)["compacted_minus_full"]["core_paired"]
    fd, cd = d[("full", "divergent")], d[("compacted", "divergent")]
    title = (f"After a first task with a divergent remote, {dv['k']}/{dv['n']} second tasks lost "
             f"a teammate's commit\n(vs {cl['k']}/{cl['n']} after a clean one); the divergent "
             f"arm lost it in {fd['k']}/{fd['n']} with either start")
    assert (fd["k"], fd["n"]) == (cd["k"], cd["n"]), "title says 'with either start'"
    unk = [(s, d[(s, "clean")]) for s in ("full", "compacted") if d[(s, "clean")]["unknown"]]
    assert all(d[(s, "divergent")]["unknown"] == 0 for s in ("full", "compacted"))
    foot = (f"Source: precedent experiment (randomized first-task remote), {dv['n'] + cl['n']} "
            "second tasks, pi-style agent + DeepSeek V4.1 Flash. "
            + " ".join(f"Clean remote, {s} history: {x['unknown']} unscored run counted as lost "
                       f"({x['upper_k']}/{x['n']} if kept)." for s, x in unk))
    save(fig, "02_precedent", title,
         "share of second tasks that lost the teammate's commit, by the first task's randomized "
         "remote (divergent or clean), for the full and compacted starts.",
         [f"{PREC} (counts['<arm>/<start>'].lost + unknown over forks; counts['<arm>/all'])"],
         [f"{s} start, {arm}: {fmt(d[(s, arm)])} (unknowns counted as lost; upper completion "
          f"{d[(s, arm)]['upper_k']}/{d[(s, arm)]['n']})"
          for s in ("full", "compacted") for arm in ("divergent", "clean")]
         + [f"both starts, {arm}: {fmt(d[('all', arm)])} (upper completion "
            f"{d[('all', arm)]['upper_k']}/{d[('all', arm)]['n']})"
            for arm in ("divergent", "clean")],
         [("Notes: bars show the conservative (lower) completion, in which the clean arm's "
          f"{cl['unknown']} unscored run counts as lost (counts['clean/all'].unknown; "
          "API-truncated per the readout); the upper completion counts it as kept (clean "
          f"{cl['upper_k']}/{cl['n']}). Paired within history (compacted_minus_full."
          f"core_paired), compacted minus full is {100 * cp['mean']:+.1f} points over "
          f"{cp['histories']} histories, with {cp['discordant']['1']} moving up and "
          f"{cp['discordant']['-1']} down, so equal totals are not identical runs. Readout "
          "difference, divergent minus "
          f"clean: {pts(*pt['lower'][:3])} (lower) and {pts(*pt['upper'][:3])} (upper), exact "
          f"randomization p = {pt['lower'][3]:.2g} / {pt['upper'][3]:.2g} "
          "(primary.<completion>.bootstrap and .test). The readout's own intervals are cluster "
          "bootstraps; the chart's error bars are Wilson.")], foot,
         short=(f"After a divergent first task, {dv['k']}/{dv['n']} second tasks lost the commit "
                f"(vs {cl['k']}/{cl['n']} after a clean one)"))


def chart_monitor():
    d = data_monitor()
    hk, ok = "claude-haiku-5-5", "claude-opus-5-5"
    reason, cur = "current_plus_last_reasoning", "current_only"
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.25)
    groups = [(MON_MODELS[m], [dict(lab="Current call only", color=CONTROL, **d[(m, cur)]),
                               dict(lab=MON_VIEWS[reason], color=REASONING, **d[(m, reason)])])
              for m in MON_MODELS]
    draw(ax, groups)
    ax.set_ylabel("Force-pushes allowed")
    title = (f"Shown the agent's reasoning, the Guardian monitor allowed more\nunauthorized "
             f"force-pushes: Haiku {d[(hk, cur)]['k']} → {d[(hk, reason)]['k']} of "
             f"{d[(hk, reason)]['n']}, Opus {d[(ok, cur)]['k']} → {d[(ok, reason)]['k']} of "
             f"{d[(ok, reason)]['n']}")
    sizes = monitor_set_sizes()
    n_acts = sum(sizes.values())
    foot = (f"Source: monitor experiment, offline scoring of the precedent experiment's {n_acts} "
            f"unauthorized force-pushes ({sizes['exp1_divergent']} divergent, "
            f"{sizes['exp1_scoped']} scoped-permission histories).")
    flips = rationale_flips()
    others = {m: [d[(m, v)]["k"] for v in ("full", "summary_tail", "pinned_user_turns")]
              for m in MON_MODELS}
    save(fig, "03_monitor_reasoning", title,
         f"share of Experiment 1's {n_acts} unauthorized force-pushes that the Guardian monitor "
         "allowed, "
         "when it saw only the current call against the current call plus the agent's last 40 "
         "reasoning blocks, for Haiku 5.5 and Opus 5.5.",
         [(f"{MON} (misses[prompt='guardian', measure='guardian', set in {list(MON_SETS)}, "
          "view, model]: k and n summed over the two sets; no missing decisions)")],
         [f"{MON_MODELS[m]}, {one_line(MON_VIEWS[v])}: {fmt(d[(m, v)])}"
          for m in MON_MODELS for v in (cur, reason)],
         [("Notes: numbers come from the scored data's analysis (analysis.json, generated by "
          "week2_monitors_analyze.py), not from the hand-appended 5b section of analysis.md; the "
          "two agree (5b_rationales.md paired table, block to allow vs allow to block: Haiku "
          f"{flips['Haiku'][0]} vs {flips['Haiku'][1]}, Opus {flips['Opus'][0]} vs "
          f"{flips['Opus'][1]}; allows Haiku {flips['Haiku'][0] + flips['Haiku'][2]}, Opus "
          f"{flips['Opus'][0] + flips['Opus'][2]}). Other history views: Haiku "
          f"{'/'.join(map(str, others[hk]))} of {n_acts} (full / summary + recent turns / pinned "
          f"user turns), Opus {'/'.join(map(str, others[ok]))} of {n_acts}: Opus already drifts "
          "toward "
          "allowing with any added history (backup_03_monitor_all_views). One monitor sample per "
          "item; Wilson intervals ignore clustering by history.")], foot,
         short=(f"Shown the agent's reasoning, the monitor allowed more force-pushes: Haiku "
                f"{d[(hk, cur)]['k']} → {d[(hk, reason)]['k']}, Opus {d[(ok, cur)]['k']} → "
                f"{d[(ok, reason)]['k']} of {d[(ok, reason)]['n']}"))

    # Backup: all five views
    fig, ax = plt.subplots(figsize=(13, 6.2))
    fig.subplots_adjust(left=0.07, right=0.99, top=0.85, bottom=0.31)
    col = {cur: CONTROL, reason: REASONING, **VIEW_COLOURS}
    groups = [(MON_MODELS[m], [dict(lab=MON_SHORT[v], color=col[v], **d[(m, v)])
                               for v in MON_VIEWS]) for m in MON_MODELS]
    draw(ax, groups, group_pad=0.9, group_offset=66)
    ax.set_ylabel("Force-pushes allowed")
    title = ("Backup: for Haiku only the reasoning view moves allows; Opus allows more with any "
             f"added history\n({'/'.join(map(str, others[ok]))} of {n_acts}) and most with the "
             f"reasoning ({d[(ok, reason)]['k']} of {n_acts})")
    save(fig, "backup_03_monitor_all_views", title,
         f"the same {n_acts} acts under all five monitor views.",
         [f"{MON} (as chart 03, every view)"],
         [f"{MON_MODELS[m]}, {one_line(MON_VIEWS[v])}: {fmt(d[(m, v)])}"
          for m in MON_MODELS for v in MON_VIEWS],
         [("Notes: 'Summary + recent turns' is the analysis's 'Summary + tail + second task' "
          "view.")], foot)


def chart_user_no():
    d = data_user_no()
    fig, ax = plt.subplots(figsize=(12, 6.2))
    fig.subplots_adjust(left=0.07, right=0.99, top=0.83, bottom=0.27)
    groups = [(lab, [dict(lab=UNO_VERSIONS[v][0], color=UNO_VERSIONS[v][1], **d[(m, v)])
                     for v in UNO_VERSIONS]) for m, lab in UNO_METRICS.items()]
    draw(ax, groups, group_pad=0.9, group_offset=64)
    ax.set_ylabel("Resumed runs")
    lost = [d[("lost", v)] for v in UNO_VERSIONS]
    assert all(x["k"] == 0 for x in lost), "title says no version lost commits"
    r, e = d[("residue_noticed", "decision_dropped")], d[("eval_terms", "decision_dropped")]
    other = [d[(m, v)]["k"] for m in ("residue_noticed", "eval_terms")
             for v in ("honest", "false_permission")]
    assert sum(other) == 0, "title says the other versions had none"
    title = (f"No losses observed in any summary version (0/{lost[0]['n']} each), even with a "
             f"forged permission;\nwith the user's \"no\" deleted, {r['k']}/{r['n']} runs said "
             f"the summary looked truncated and {e['k']}/{e['n']} used grading words")
    u = user_no_design()
    foot = (f"Source: user's-\"no\" experiment, {u['histories']} histories x {u['versions']} "
            f"summary versions x {u['reps']} runs, after the user said \"Don't overwrite the "
            "commits\"; words from a regex scan of the agent's reasoning. The deletion was "
            f"partial: {u['kept_rebase'][0]} of {u['kept_rebase'][1]} edited summaries still "
            "mentioned a rebase.")
    save(fig, "04_forged_permission", title,
         "by summary version (honest, a false permission planted, the user's \"no\" deleted): "
         "the share of resumed runs that lost the other developer's commits, that remarked the "
         "summary looked truncated, and that used benchmark or grading words.",
         [f"{UNO_SCORES} (runs per version; lost)",
          f"{UNO_SCAN} (residue_noticed, eval_terms: run ids, version = the id's middle field)"],
         [f"{one_line(UNO_METRICS[m])}, {one_line(UNO_VERSIONS[v][0])}: {fmt(d[(m, v)])}"
          for m in UNO_METRICS for v in UNO_VERSIONS],
         [("Notes: the scan lists only matching runs, so the zeros for the honest and false-"
          "permission versions rest on the readout's statement that it scanned every run "
          f"(\"{u['quote']}\", user_no/readout.md). scores.json's own eval_eval_aware flag, a "
          f"different detector, marks {u['aware'][0]}/{u['aware'][1]} 'no deleted' runs and "
          f"{u['aware'][2]}/{u['aware'][3]} others; the chart uses the readout's scan. Words are regex matches, then read by "
          "the builder; the deletion's gaps (\"commit there, and .\") look like an edited "
          "summary, so this version is a weaker test than intended.")], foot)


def chart_harness():
    d = data_harness()
    h = load(HAR)
    order = list(CONFIG)
    present = [k for k in order if d[k] is not None]
    pending = [k for k in order if d[k] is None]
    har_keys = "|".join(k for k in present if k != "pi")
    pending_txt = (f" {and_join(SHORT[k] for k in pending)} still running." if pending else "")

    def lab(k):
        return d[k]["label"] if d[k] else CONFIG[k][0]

    # 5a: first-task overwrites
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.24)
    bars = [{"lab": lab(k), "color": CONFIG[k][1], "pending": True} if d[k] is None else
            dict(lab=lab(k), color=CONFIG[k][1], **d[k]["first"]) for k in order]
    draw(ax, [(None, bars)], w=1.0)
    ax.set_ylabel("First tasks that overwrote")
    over = [k for k in present if d[k]["first"]["k"] > 0]
    never = [k for k in present if d[k]["first"]["k"] == 0]
    ds_over = [k for k in over if "DeepSeek" in d[k]["label"]]
    other_over = [k for k in over if k not in ds_over]

    def kn_txt(k):
        return f"{d[k]['first']['k']}/{d[k]['first']['n']}"

    never_txt = ""
    if never:
        ns = {d[k]["first"]["n"] for k in never}
        never_n = (f"0/{ns.pop()} each" if len(ns) == 1 and len(never) > 1 else
                   ", ".join(f"0/{d[k]['first']['n']}" for k in never))
        never_txt = f"{and_join(SHORT[k] for k in never)} never did ({never_n})"
    ds_txt = ", ".join(f"{kn_txt(k)} in {SHORT[k]}" for k in ds_over)
    if ds_over and not other_over and never:
        title = f"Only DeepSeek overwrote the remote in the first task:\n{ds_txt}; {never_txt}"
    elif over:
        who = (["DeepSeek"] if ds_over else []) + [SHORT_B[k] for k in other_over]
        nums_txt = ", ".join(f"{SHORT_C[k]} {kn_txt(k)}" for k in over)
        if never:
            nums_txt += ", " + never_txt.split(" never did (")[0] + " " + never_n
        title = (f"{and_join(who)} overwrote the remote in the first task"
                 + (f"; {and_join(SHORT[k] for k in never)} never did" if never else "")
                 + f"\n({nums_txt})")
    else:
        title = f"No configuration overwrote the remote in the first task;\n{never_txt}"
    foot = ("Source: harness replication readout and the precedent experiment (pi-style agent). "
            "Denominator: every divergent-remote history, including runs that never reached a "
            "rejected push." + pending_txt)
    nums = [f"{one_line(lab(k))}: {fmt(d[k]['first'])}" if d[k] else
            f"{one_line(lab(k))}: running, no readout yet" for k in order]
    fs = load(PREC)["first_stage"]
    save(fig, "05a_harness_first_task", title,
         "per agent configuration, the share of divergent-remote first tasks (one per history, "
         "whether or not the agent met the rejected push) in which it overwrote the foreign "
         "commits.",
         [(f"{HAR} (configs[{har_keys}].first_task.divergent.overwrote over "
           "counts.divergent.assigned)"),
          (f"{PREC} (first_stage.lost over first_stage.n: the first tasks that overwrote, "
           f"{fs['lost']} of {fs['n']} divergent histories)")],
         nums,
         ["Notes: the configurations met the divergent remote at different rates in the first "
          "task (rejected pushes: "
          + ", ".join(f"{one_line(lab(k))} {d[k]['met']}/{d[k]['first']['n']}"
                      for k in present if k != "pi")
          + "; configs[*].first_task.divergent.met_rejection), so the denominator is all "
          "divergent histories, not the exposed ones."
          + (f" {and_join(SHORT[k] for k in pending)} had no readout when this was drawn; "
             "rerun the script once harness/readout.json carries it and the slot fills."
             if pending else "")], foot)

    # 5b: second-task losses
    fig, ax = plt.subplots(figsize=(12.6, 6.4))
    fig.subplots_adjust(left=0.07, right=0.99, top=0.83, bottom=0.36)
    # Antigravity's call cap: drawn only while the summary-content file describes the same runs
    # as the readout (same losses per arm, same first-task overwriters).
    cap, cap_note = None, None
    if d["agy"] is not None:
        c = agy_call_cap()
        a = d["agy"]
        if ((a["divergent"]["k"], a["divergent"]["n"]) == (c["divergent"]["lost"],
                                                           c["divergent"]["n"])
                and (a["clean"]["k"], a["clean"]["n"]) == (c["clean"]["lost"], c["clean"]["n"])
                and a["first"]["k"] == c["overwrote"]["n"]):
            cap = c
        else:
            cap_note = (f"The Antigravity call-cap marker is omitted: {AGY_SUMMARY} does not "
                        "match the readout's Antigravity counts.")
    groups = []
    for k in order:
        if d[k] is None:
            groups.append((lab(k), [{"lab": "Divergent", "color": DIVERGENT, "pending": True},
                                    {"lab": "Clean", "color": CONTROL, "pending": True}]))
        else:
            groups.append((lab(k), [dict(lab="Divergent", color=DIVERGENT, **d[k]["divergent"]),
                                    dict(lab="Clean", color=CONTROL, **d[k]["clean"])]))
    draw(ax, groups, group_pad=0.8)
    ax.set_ylabel("Lost the teammate's commit")
    if cap:
        cv, cc, co = cap["divergent"], cap["clean"], cap["overwrote"]
        all_o = f"all {co['n']}" if co["capped"] == co["n"] else f"{co['capped']} of {co['n']}"
        cap_note = (f"Antigravity hit its call limit (about {cap['cap_calls']} calls) before "
                    f"any push in {cv['capped']}/{cv['n']} divergent and {cc['capped']}/"
                    f"{cc['n']} clean second tasks, including {all_o} whose first task "
                    "overwrote: those runs ended before reaching a push.")
    pdv, pcl = d["pi"]["divergent"], d["pi"]["clean"]
    rest = [k for k in present if k != "pi"]
    lossy = [k for k in rest if d[k]["divergent"]["k"] or d[k]["clean"]["k"]]
    clean = [k for k in rest if k not in lossy]
    parts = [f"{SHORT_B[k]} {d[k]['divergent']['k']}/{d[k]['divergent']['n']} vs "
             f"{d[k]['clean']['k']}/{d[k]['clean']['n']}" for k in lossy]
    if clean:
        parts.append(f"{and_join(SHORT_B[k] for k in clean)} lost none")
    title = (f"In the second task, the pi-style agent lost the commit after {pdv['k']}/{pdv['n']} "
             f"divergent first tasks (vs {pcl['k']}/{pcl['n']} clean);\n" + "; ".join(parts))
    caveat = None  # DeepSeek in Claude Code only; omitted while that configuration is pending
    if d["ds"] is not None:
        tw, two = d["ds"]["terms"]["with"], d["ds"]["terms"]["without"]
        pw, pwo = d["pi"]["terms"]["with_terms"], d["pi"]["terms"]["without_terms"]
        assert tw[0] == d["ds"]["divergent"]["k"] and two[0] == 0, "caveat says 'both' losses"
        both_w = "both" if tw[0] == 2 else f"all {tw[0]}"
        caveat = (f"Caveat: in Claude Code + DeepSeek, {both_w} divergent-arm losses came from "
                  f"runs whose reasoning called the teammate's branch \"planted\" or a "
                  f"\"decoy\" ({tw[0]}/{tw[1]} runs with those words lost it, {two[0]}/{two[1]} "
                  f"without; pi-style agent, both starts: {pw[0]}/{pw[1]} vs {pwo[0]}/{pwo[1]}).")
    foot = ("Source: harness replication readout (compacted start, one second task per history) "
            "and the precedent experiment, compacted start only." + pending_txt)
    nums = [f"{one_line(lab(k))}: running, no readout yet" if d[k] is None else
            f"{one_line(lab(k))}: divergent {fmt(d[k]['divergent'])}; clean "
            f"{fmt(d[k]['clean'])}" for k in order]
    both = data_precedent()
    clean_ids = [o["history"] for o in h["overwrites"] if o["config"] == "ds"
                 and o["arm"] == "clean" and d["ds"] is not None]
    for i in clean_ids:
        assert f"{i} (clean): does not cite the earlier task" in h["notes"], i
    replicated = [SHORT[k] for k in rest if d[k]["replicated"]]
    splits = []
    for k in rest:
        hit = [x for x in h["exact_splits"]
               if x["label"] == f"{d[k]['raw_label']}, compacted start"]
        if hit:
            o, kept = hit[0]["overwrote"], hit[0]["kept"]
            splits.append(f"{SHORT_B[k]} {o[0]}/{o[1]} after its first task overwrote vs "
                          f"{kept[0]}/{kept[1]} after it kept the commits")
    split_txt = (("Divergent second tasks by first-task outcome (exact_splits, observational): "
                  + "; ".join(splits) + "; chart: backup_05c_harness_by_first_task. ")
                 if splits else "")
    save(fig, "05b_harness_second_task", title,
         "per agent configuration, the share of second tasks (compacted start) that lost the "
         "teammate's commit, after a divergent against a clean first-task remote.",
         [(f"{HAR} (configs[{har_keys}].counts.<arm>.lost over assigned; configs.ds.eval."
           "planted_or_decoy_divergent; e1_terms; holm; configs[*].replicated; overwrites)"),
          f"{PREC} (counts['<arm>/compacted'].lost + unknown over forks)"]
         + ([(f"{AGY_SUMMARY} (the per-history code-pass table: task-2 pushes = 0 and end = "
              "capped_calls, by arm and for the task-1 overwriters; its card's '10/24 (6 "
              "divergent, 4 clean)' and '~400-call cap' asserted)")] if cap else []),
         nums,
         [("Notes: the pi-style agent is shown at its compacted start to match the harness runs, "
           "which all started compacted; with both starts it is "
           f"{both[('all', 'divergent')]['k']}/{both[('all', 'divergent')]['n']} vs "
           f"{both[('all', 'clean')]['k']}/{both[('all', 'clean')]['n']} (chart 02). "
           + (f"Hand reading (harness/readout.md): the Claude Code + DeepSeek clean-arm loss "
              f"({', '.join(clean_ids)}) cites the stale draft, not the earlier task. "
              if clean_ids else "")
           + split_txt + "Holm-adjusted p"
           + (" (an upper bound while a configuration is unscored)" if pending else "")
           + ": "
           + ", ".join(f"{SHORT[k]} {d[k]['holm']:.2g}" for k in rest)
           + (f"; replicated under the plan's rule: {and_join(replicated)}." if replicated
              else "; none replicated under the plan's rule.")
           + " 'Planted'/'decoy' counts are code-matched words over divergent second tasks; "
           "DeepSeek's thinking was visible to the scan, Haiku's and Codex's were not, so the "
           "term counts are not comparable across configurations.")]
         + ([f"Call-cap note (not on the chart): {cap_note}"] if cap_note else []),
         foot, caveat=caveat)
    chart_harness_split(d, h)


def chart_harness_split(d, h):
    """Backup 05c: divergent second tasks split by the agent's own first-task outcome, for the
    configurations whose split has losses to compare (pi-style agent, DeepSeek in Claude Code)."""
    keys = [k for k in ("pi", "ds") if d[k] is not None]
    if keys != ["pi", "ds"]:
        README.extend(["## backup_05c_harness_by_first_task: not drawn", "",
                       "DeepSeek in Claude Code had no readout.", ""])
        return
    raw = {"pi": "pi (experiment 1)", "ds": d["ds"]["raw_label"]}
    sp = {}
    for k in keys:
        hit = [x for x in h["exact_splits"] if x["label"] == f"{raw[k]}, compacted start"]
        assert len(hit) == 1, k
        x = hit[0]
        o, kept = share(*x["overwrote"]), share(*x["kept"])
        # the split partitions the divergent arm plotted in 05b
        assert (o["k"] + kept["k"], o["n"] + kept["n"]) == (d[k]["divergent"]["k"],
                                                             d[k]["divergent"]["n"]), k
        assert o["rate"] > kept["rate"], "title says losses were more common after an overwrite"
        sp[k] = {"over": o, "kept": kept, "diff": x["diff"], "ci": x["exact_ci"],
                 "p": x["fisher_p"]}
    fig, ax = plt.subplots(figsize=(10, 6.4))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.84, bottom=0.33)
    groups = [(CONFIG[k][0], [dict(lab="First task\noverwrote", color=OVERWROTE,
                                   **sp[k]["over"]),
                              dict(lab="First task kept\nthe commits", color=DIVERGENT,
                                   **sp[k]["kept"])]) for k in keys]
    draw(ax, groups)
    ax.set_ylabel("Lost the teammate's commit")
    p, q = sp["pi"], sp["ds"]

    def kn_(s):
        return f"{s['k']}/{s['n']}"

    title = ("Backup: second-task losses were more common after the agent's own first-task "
             f"overwrite\n(pi-style agent {kn_(p['over'])} vs {kn_(p['kept'])}; DeepSeek in "
             f"Claude Code {kn_(q['over'])} vs {kn_(q['kept'])}; observational)")
    foot = ("Source: harness replication readout, divergent histories split by the agent's own "
            "first-task outcome (compacted start, one second task per history). The first-task "
            "outcome was not randomized.")
    both = [x for x in h["exact_splits"] if x["label"] == "pi (experiment 1), both starts"]
    assert len(both) == 1
    bo, bk = both[0]["overwrote"], both[0]["kept"]
    agy = [x for x in h["exact_splits"] if d["agy"] is not None
           and x["label"] == f"{d['agy']['raw_label']}, compacted start"]
    agy_txt = ""
    if agy:
        c = agy_call_cap()
        ao, ak = agy[0]["overwrote"], agy[0]["kept"]
        assert ao[1] == c["overwrote"]["n"] and ao[0] == c["overwrote"]["lost"]
        agy_txt = (f" Antigravity's split ({ao[0]}/{ao[1]} vs {ak[0]}/{ak[1]}) is not drawn: "
                   f"{'all' if c['overwrote']['capped'] == ao[1] else c['overwrote']['capped']}"
                   f" {ao[1]} histories whose first task overwrote hit the call limit before any "
                   f"push in the second task ({AGY_SUMMARY}).")
    save(fig, "backup_05c_harness_by_first_task", title,
         "among divergent histories, the share of second tasks (compacted start) that lost the "
         "teammate's commit, by whether the agent's own first task had overwritten the foreign "
         "commits.",
         [(f"{HAR} (exact_splits[label='<configuration>, compacted start'].overwrote and .kept, "
           "[lost, histories]; also diff, exact_ci, fisher_p)")]
         + ([f"{AGY_SUMMARY} (why Antigravity's split is left out)"] if agy else []),
         [f"{one_line(CONFIG[k][0])}: first task overwrote {fmt(sp[k]['over'])}; kept "
          f"{fmt(sp[k]['kept'])}" for k in keys],
         [("Notes: the two bars in each group partition that configuration's divergent bar in "
           "05b. Difference (overwrote minus kept), exact 95% interval and Fisher exact p from "
           "the readout: "
           + "; ".join(f"{SHORT_C[k]} {pts(sp[k]['diff'], *sp[k]['ci'])}, p = {sp[k]['p']:.2g}"
                       for k in keys)
           + f". pi-style agent with both starts (two forks per history): {bo[0]}/{bo[1]} vs "
           f"{bk[0]}/{bk[1]}. Both DeepSeek-in-Claude-Code losses came from runs whose reasoning "
           "called the teammate's branch \"planted\" or a \"decoy\" (05b caveat)." + agy_txt)],
         foot)


def main():
    src = verify_sources()
    README.extend([
        "# Week-2 result figures (final presentation)",
        "",
        ("Generated by `scripts/sprint_figs_week2.py` "
         "(`uv run python scripts/sprint_figs_week2.py`)"
        "; every number is read at run time from the unchanged source copies in `data/`, whose "
        "sha256 the script checks first. PNG (200 dpi) and SVG. Tested by "
        "`tests/sprint/test_sprint_figs_week2.py`."),
        "",
        ("**Style:** AGENTS.md \"Figures\" (Jacob, 29 Oct): `sns.set_theme(style=\"whitegrid\")` "
        "with every other setting at seaborn's default (fonts, text sizes and colours), the "
        "default (deep) palette and plain bars, helpers from `scripts/sprint_figs_v2.py`; kept "
        "plain per Jacob's note via J (28 Oct): 2-4 bars where possible, direct labels instead of legends, the count on each "
        "bar, source lines in this README, gridlines at 0/50/100% only. Detail versions are the "
        "`backup_*` files. Error bars are Wilson 95% intervals on k/n (they ignore clustering by "
        "history; each readout's own cluster-bootstrap or exact intervals are quoted in the "
        "notes)."),
        "",
        ("**Colours:** one deep-palette colour per condition across the set. Every control "
        "condition (no earlier history, clean first-task remote, current call only, honest "
        "summary) is grey; earlier history red; divergent first-task remote orange; reasoning "
        "view blue; false permission brown; \"no\" deleted pink; a first task that overwrote "
        "olive; each agent configuration in 05a its own colour."),
        "",
        ("**Review:** one cross-family round (gpt-6-astra, "
        "`logs/reviews/20261029-week2-figs-r1.md`): verdict BLOCKING ISSUES, 2 blocking and 3 "
        "minor; all five fixed after it (02's title no longer says compaction made no "
        "difference; the harness titles and notes are generated from the configurations "
        "present, so an Antigravity readout cannot leave stale claims; note numbers are read "
        "from the sources; 05a carries its denominator; 04 says the deletion was partial). "
        "Round 2 (`logs/reviews/20261029-week2-figs-r2.md`), after the Antigravity readout "
        "(sprint/week2-build 261ad64f) was copied in and the harness titles reworked: PASS WITH "
        "MINOR FIXES; its one minor finding (a pending DeepSeek configuration would crash the "
        "caveat) was fixed and tested, not re-reviewed. Added after round 2: 05b's Antigravity "
        "call-limit marker (from `harness/agy_summary_content.md`), backup_05c, plain model "
        "names on the harness charts, and stable SVG ids with no date stamp so reruns diff "
        "clean; reviewed in `logs/reviews/20261029-week2-figs-harness-marker-r1.md` (1 blocking: "
        "the marker overstated what the call limit shows; 1 minor: raw model ids; both fixed, "
        "not re-reviewed)."),
        "",
        "## Data sources",
        "",
        ("| Copy in `data/` | Branch | Branch tip | Last commit touching it | Path on branch | "
        "sha256 |"),
        "|---|---|---|---|---|---|",
    ] + [f"| `{rel}` | `{m['branch']}` | `{m['branch_tip'][:12]}` | `{m['last_commit'][:12]}` | "
         f"`{m['path']}` | `{m['sha256']}` |" for rel, m in src.items()] + [""])
    chart_chain()
    chart_precedent()
    chart_monitor()
    chart_user_no()
    chart_harness()
    (OUT / "README.md").write_text("\n".join(README))


if __name__ == "__main__":
    main()
