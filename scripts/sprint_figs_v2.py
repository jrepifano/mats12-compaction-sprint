"""Charts for the v2 deck built from J's slide feedback (26 Oct 2026).

Same data as scripts/sprint_figs.py, sprint_figs_prompts.py and sprint_figs_chain.py, with no
footer (notes live in the README). Style follows AGENTS.md "Figures" (Jacob, 29 Oct: "can we use
the default seaborn pallette and settings for all plots?"): sns.set_theme(style="whitegrid"), the
default (deep) palette with one colour per condition across the set, plain bars, and seaborn's
default fonts, text sizes and colours. This replaces the 26 Oct warm-style exception (Avenir,
custom palette, rounded bars). The helpers below keep their names and signatures for the scripts
that import them (sprint_figs_week2.py, sprint_fig_s1_sources.py).

Every number is read from a committed file at run time. Sources:
- results/sprint/S1/analysis.json (part_a.haiku-max.items.type_x_source; part_b.haiku-max.arms)
- results/sprint/S1/swechat_ext/summary.json (population estimate, lost by type)
- results/sprint/S1/judge_precision.json, results/sprint/judge_compare/summary.json (precision)
- results/sprint/S1/loss_mining/labels.jsonl (consequential share by source)
- results/sprint/S1/prompt_loss_mining/summary.json (lost and matters, per prompt)
- results/sprint/S1/chain_decay/summary.json, results/sprint/S1/chain_loss_mining/summary.json
Writes docs/sprint/figs/v2/*.png, *.svg and README.md. Run: uv run python scripts/sprint_figs_v2.py
"""


import matplotlib.pyplot as plt
import seaborn as sns
import sprint_figs as sf  # its import applies sns.set_theme(style="whitegrid")

sns.set_theme(style="whitegrid")  # again here, so this module never depends on import order
ROOT = sf.ROOT
OUT = ROOT / "docs" / "sprint" / "figs" / "v2"
PAL = sns.color_palette()  # seaborn "deep"
# Seaborn defaults under the names importers use: the theme's text colour (INK, MUTED), its grid
# colour (GRID), and the error-bar colour and width sns.barplot draws (ERR, ERR_LW).
INK = MUTED = plt.rcParams["text.color"]
GRID = plt.rcParams["grid.color"]
ERR, ERR_LW = sf.ERR, sf.ERR_LW
# One deep-palette colour per condition, kept the same across the set: raw loss grey, corrected
# orange, lost-and-matters red. No prompt or item type reuses those three; the five prompts and the
# five item types share the remaining entries, each in its own chart.
RAW, CORR, MATTERS = PAL[7], PAL[1], PAL[3]
PROMPT = {
    "antigravity": ("Antigravity-style", PAL[2]),
    "pi": ("pi", PAL[4]),
    "codex_local": ("Codex (local)", PAL[9]),
    "cc_prompt": ("Claude Code", PAL[0]),
    "opencode": ("OpenCode", PAL[5]),
}
TYPE = {
    "rule": ("Rules", PAL[0]),
    "permission": ("Permissions", PAL[9]),
    "task_state": ("Task state", PAL[2]),
    "decision": ("Decisions", PAL[4]),
    "commitment": ("Commitments", PAL[5]),
}
README = []
# No captions inside the images (Jacob, 29 Oct): a one-line title on the chart (the slide title
# carries the full claim), and every note and source in the README. SHORT maps a chart name to its
# one-line chart title where the title's first line would not stand alone.
SHORT = {}
title_above = sf.title_above


def chart_title(name, title):
    """The one-line title drawn on the chart: SHORT[name], else the title's first line."""
    return SHORT.get(name) or title.split("\n")[0].rstrip(" ,;:")


def rbar(ax, x, h, w, color):
    """A plain bar (the name is kept for importers; bars are no longer rounded)."""
    ax.bar(x, h, width=w, color=color)


def err(ax, x, y, lo, hi):
    """A 95% interval drawn as sns.barplot draws its error bars."""
    ax.errorbar(x, y, yerr=[[y - lo], [hi - y]], fmt="none", ecolor=ERR, elinewidth=ERR_LW,
                zorder=3)


def label(ax, x, top, text, bold=False):
    """The value above a bar, in default text. `bold` is accepted for importers and ignored."""
    ax.annotate(text, (x, top), xytext=(0, 4), textcoords="offset points", ha="center",
                va="bottom")


def frame(ax, ticks, labels, ylabel, ymax):
    ax.set_ylim(0, ymax)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels)
    ax.set_ylabel(ylabel)
    ax.xaxis.grid(False)  # as sns.barplot does: no grid on the categorical axis


def legend(ax, items, loc="upper right"):
    for name, col in items:
        ax.bar(0, 0, color=col, label=name)
    ax.legend(loc=loc)


def save(fig, name, title, claim, sources, numbers, notes):
    short = chart_title(name, title)
    title_above(fig, short)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=200, bbox_inches="tight")
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight")
    plt.close(fig)
    README.extend([f"## {name}: {title.replace(chr(10), ' ')}", "", f"![{name}]({name}.png)", "",
                   f"**Title on the chart:** {short}", "", f"**Claim:** {claim}", "",
                   "**Source:**"]
                  + [f"- `{s}`" for s in sources] + ["", "**Numbers plotted:**"]
                  + [f"- {n}" for n in numbers] + [""] + [f"- {n}" for n in notes] + [""])


def pct(v):
    return v * 100


# 1. Items lost by source, with the share that matters -----------------------------------------------
def chart_loss_by_source():
    a_rel, s_rel = "results/sprint/S1/analysis.json", "results/sprint/S1/swechat_ext/summary.json"
    jp_rel, jc_rel = "results/sprint/S1/judge_precision.json", "results/sprint/judge_compare/summary.json"
    tot = {}
    for c in sf.load(a_rel)["part_a"]["haiku-max"]["items"]["type_x_source"]:
        k, n = tot.get(c["source"], (0, 0))
        tot[c["source"]] = (k + c["n_lost"], n + c["n_items"])
    s = sf.load(s_rel)
    pop = s["sets"]["population_estimate"]["pooled"]
    e1 = sf.load(jp_rel)["haiku-max"]["lost"]
    swp = sf.load(jc_rel)["metrics"]["by_set"]["swechat_ext"]["haiku"]["precision_lost"]
    cons = sf.consequential_by_source(sf.MINING)
    srcs = [("jacob", "Jacob's own\nsessions"), ("swe-chat-v1", "SWE-chat\n(real users)"),
            ("posttrainbench", "PostTrainBench"), ("frontierml", "FrontierML")]
    rows = []
    for src, lab in srcs:
        if src == "swe-chat-v1":
            raw, lo, hi, n = pop["lost_rate"]["point"], pop["lost_rate"]["lo"], pop["lost_rate"]["hi"], pop["n_items"]
            prec = swp["k"] / swp["n"]
        else:
            k, n = tot[src]
            raw, lo, hi = k / n, None, None
            prec = e1["n_hits"] / e1["n"]
        c = cons[src]
        corr = raw * prec
        rows.append(dict(lab=lab, n=n, raw=raw, lo=lo, hi=hi, corr=corr, prec=prec,
                         m=corr * c["point"], mlo=corr * c["lo"], mhi=corr * c["hi"], ck=c["k"],
                         cn=c["n"]))
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.17)
    frame(ax, range(len(rows)), [f"{r['lab']}\n{r['n']:,} items" for r in rows],
          "Actionable items (%)", 30)
    w = 0.26
    for i, r in enumerate(rows):
        for dx, v, col in ((-w, r["raw"], RAW), (0, r["corr"], CORR), (w, r["m"], MATTERS)):
            rbar(ax, i + dx, pct(v), w * 0.9, col)
        top = pct(r["raw"])
        if r["lo"] is not None:
            err(ax, i - w, pct(r["raw"]), pct(r["lo"]), pct(r["hi"]))
            top = pct(r["hi"])
        label(ax, i - w, top, f"{pct(r['raw']):.0f}%")
        label(ax, i, pct(r["corr"]), f"{pct(r['corr']):.0f}%")
        err(ax, i + w, pct(r["m"]), pct(r["mlo"]), pct(r["mhi"]))
        label(ax, i + w, pct(r["mhi"]), f"{pct(r['m']):.0f}%", bold=True)
    legend(ax, [("Lost, as the judge called it", RAW), ("Lost, corrected for judge precision", CORR),
                ("Lost, and would change the agent's next action", MATTERS)])
    lo_c, hi_c = min(r["corr"] for r in rows), max(r["corr"] for r in rows)
    lo_m, hi_m = min(r["m"] for r in rows), max(r["m"] for r in rows)
    title = (f"Summaries lose {pct(lo_c):.0f}–{pct(hi_c):.0f}% of items, "
             f"but only {pct(lo_m):.0f}–{pct(hi_m):.0f}% in a way that matters")
    save(fig, "01_loss_by_source", title,
         "share of actionable items the summary lost, by data source: raw judge rate, "
         "precision-corrected rate, and corrected rate times Opus 5.5's consequential share.",
         [a_rel + " (part_a.haiku-max.items.type_x_source)",
          s_rel + " (sets.population_estimate.pooled)", jp_rel + " (haiku-max.lost)",
          jc_rel + " (metrics.by_set.swechat_ext.haiku.precision_lost)",
          sf.MINING + " (experiment1 pool, consequential by source)"],
         [f"{r['lab'].replace(chr(10), ' ')}: raw {pct(r['raw']):.1f}%"
          + (f" [{pct(r['lo']):.1f}, {pct(r['hi']):.1f}]" if r["lo"] is not None else "")
          + f"; corrected x {r['prec']:.2f} = {pct(r['corr']):.1f}%; x consequential "
            f"{r['ck']}/{r['cn']} = {pct(r['m']):.1f}% [{pct(r['mlo']):.1f}, {pct(r['mhi']):.1f}]"
          for r in rows],
         ["Notes: SWE-chat is population-weighted with a session-bootstrap interval; the other "
          "sources are as sampled, with no interval on the raw rate. The correction ignores the "
          "judge's misses (recall unmeasured). Red intervals cover the labelling sample only "
          "(compaction bootstrap); 'unclear' counts as no; Opus labels, not hand-checked."])


# 2. What gets lost, by item type --------------------------------------------------------------------
def chart_lost_by_type():
    rel = "results/sprint/S1/swechat_ext/summary.json"
    s = sf.load(rel)
    prim = s["population_design"]["primary"]
    est = s["sets"]["population_estimate"]["lost_by_type"]
    rows = []
    for t, (lab, col) in TYPE.items():
        p = prim[f"lost_{t}"]
        assert sf.close(p["point"], est[t]["lost_rate"]["point"], 1e-9)
        rows.append(dict(lab=lab, col=col, v=p["point"], lo=p["lo"], hi=p["hi"],
                         n=est[t]["n_items"]))
    rows.sort(key=lambda r: -r["v"])
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.85, bottom=0.15)
    frame(ax, range(len(rows)), [f"{r['lab']}\n{r['n']:,} items" for r in rows],
          "Items the judge marks lost (%)", 45)
    for i, r in enumerate(rows):
        rbar(ax, i, pct(r["v"]), 0.6, r["col"])
        err(ax, i, pct(r["v"]), pct(r["lo"]), pct(r["hi"]))
        label(ax, i, pct(r["hi"]), f"{pct(r['v']):.0f}%")
    title = (f"In real users' sessions, rules are lost most often ({pct(rows[0]['v']):.0f}%), "
             f"commitments least")
    assert rows[0]["lab"] == "Rules", rows[0]["lab"]
    save(fig, "02_lost_by_type", title,
         "share of each item type the judge marks lost in SWE-chat compactions, "
         "population-weighted.",
         [rel + " (population_design.primary.lost_<type>; n from "
                "sets.population_estimate.lost_by_type)"],
         [f"{r['lab']}: {pct(r['v']):.1f}% [{pct(r['lo']):.1f}, {pct(r['hi']):.1f}]; {r['n']:,} items"
          for r in rows],
         ["Notes: judge's calls (Claude Haiku 5.5), not precision-corrected; 95% session-bootstrap "
          "intervals; n = items observed (unweighted)."])
    if rows[-1]["lab"] != "Commitments":  # the title names the lowest type
        raise SystemExit(f"lowest type is {rows[-1]['lab']}, update the title")


# 3. Five compaction prompts, lost and lost-and-matters ----------------------------------------------
def chart_prompts():
    src, mining = "results/sprint/S1/analysis.json", "results/sprint/S1/prompt_loss_mining/summary.json"
    b = sf.load(src)["part_b"]["haiku-max"]
    imp = sf.load(mining)["implied_lost_and_matters"]
    rows = []
    for a in b["arms"]:
        if a["arm"] == "cc_native":  # the real /compact check only validates the replay method
            continue
        r, m = a["user_rule_loss_rate"], imp[f"rules|{a['arm']}"]["lost_x_consequential"]
        assert sf.close(m["loss_rate"]["point"], r["point"]), a["arm"]
        lab, col = PROMPT[a["arm"]]
        rows.append(dict(lab=lab, col=col, n=a["n_user_rules"], v=r["point"], lo=r["lo"],
                         hi=r["hi"], m=m["point"], mlo=m["lo"], mhi=m["hi"]))
    rows.sort(key=lambda r: r["v"])
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.15)
    frame(ax, range(len(rows)), [f"{r['lab']}\n{r['n']} rules" for r in rows],
          "User-given rules (%)", 45)
    w = 0.36
    for i, r in enumerate(rows):
        for dx, v, lo, hi, col, bold in ((-w / 2, r["v"], r["lo"], r["hi"], r["col"], False),
                                         (w / 2, r["m"], r["mlo"], r["mhi"], MATTERS, True)):
            rbar(ax, i + dx, pct(v), w * 0.9, col)
            err(ax, i + dx, pct(v), pct(lo), pct(hi))
            label(ax, i + dx, pct(hi), f"{pct(v):.0f}%", bold)
    legend(ax, [("Lost (bar in the prompt's colour)", RAW),
                ("Lost, and would change what the agent does next", MATTERS)], loc="upper left")
    ms = sorted(pct(r["m"]) for r in rows)
    title = (f"Five compaction prompts lose {pct(rows[0]['v']):.0f}–{pct(rows[-1]['v']):.0f}% of "
             f"rules,\nbut only {ms[0]:.0f}–{ms[-1]:.0f}% in a way that matters")
    SHORT["03_rules_lost_by_prompt"] = " ".join(title.split())
    save(fig, "03_rules_lost_by_prompt", title,
         "share of user-given rules each replayed compaction prompt lost, and the share lost and "
         "consequential, on the same 100 histories with Haiku 5.5 as the summarizer.",
         [src + " (part_b.haiku-max.arms[].user_rule_loss_rate, n_user_rules)",
          mining + " (implied_lost_and_matters[rules|arm].lost_x_consequential)"],
         [f"{r['lab']}: lost {pct(r['v']):.1f}% [{pct(r['lo']):.1f}, {pct(r['hi']):.1f}]; lost and "
          f"matters {pct(r['m']):.1f}% [{pct(r['mlo']):.1f}, {pct(r['mhi']):.1f}]; {r['n']} rules"
          for r in rows],
         ["Notes: 95% session-bootstrap intervals; the red bars are the loss rate times Opus 5.5's "
          "consequential share on 30 sampled lost rules per prompt ('unclear' counts as no; not "
          "hand-checked). The legend's grey swatch stands for each prompt's own colour."])


# 4. Repeated compaction ----------------------------------------------------------------------------
def chart_chain():
    summ, mining = "results/sprint/S1/chain_decay/summary.json", "results/sprint/S1/chain_loss_mining/summary.json"
    lost = sf.load(summ)["cohort1"]["lost_by_k_uncensored"]
    m = sf.load(mining)
    imp, per_k = m["implied_lost_and_matters"], m["per_k"]
    ks, ords = ["1", "2", "3"], {"1": "1st", "2": "2nd", "3": "3rd"}
    for k in ks:
        assert sf.close(imp[k]["lost_x_consequential"]["lost_share"], lost[k]["all"]["point"])
    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.17)
    frame(ax, range(len(ks)),
          [f"{ords[k]} compaction\n{lost[k]['all']['n']} items, {per_k[k]['n']} labelled" for k in ks],
          "Items stated before the 1st compaction (%)", 100)
    w = 0.34
    nums = []
    for i, k in enumerate(ks):
        for dx, v, col, bold, name in ((-w / 2, lost[k]["all"], RAW, False, "lost"),
                                       (w / 2, imp[k]["lost_x_consequential"], MATTERS, True,
                                        "lost and matters")):
            rbar(ax, i + dx, pct(v["point"]), w * 0.9, col)
            err(ax, i + dx, pct(v["point"]), pct(v["lo"]), pct(v["hi"]))
            label(ax, i + dx, pct(v["hi"]), f"{pct(v['point']):.0f}%", bold)
            nums.append(f"{ords[k]} compaction, {name}: {pct(v['point']):.1f}% "
                        f"[{pct(v['lo']):.1f}, {pct(v['hi']):.1f}]")
    legend(ax, [("Lost (any)", RAW), ("Lost, and would change what the agent does next", MATTERS)],
           loc="upper left")
    m1, m2 = (pct(imp[k]["lost_x_consequential"]["point"]) for k in ("1", "2"))
    title = (f"Repeated compaction loses many more items, but few more that matter\n"
             f"({m1:.0f}% → {m2:.0f}% by the 2nd compaction; intervals overlap)")
    save(fig, "04_chain_lost_and_matters", title,
         "share of items stated before the 1st compaction that the k-th summary has lost, and lost "
         "and consequential, on 54 real multi-compaction SWE-chat sessions.",
         [summ + " (cohort1.lost_by_k_uncensored[k].all)",
          mining + " (implied_lost_and_matters[k].lost_x_consequential; per_k[k].n)"],
         nums,
         ["Notes: raw judge rates (not precision-corrected); 95% session-bootstrap intervals. Red: "
          "the lost share times Opus 5.5's consequential share on 30 seeded lost items per "
          "compaction, a product of two estimates, not hand-checked."])


def main():
    README.extend([
        "# v2 deck figures",
        "",
        "Generated by `scripts/sprint_figs_v2.py` (`uv run python scripts/sprint_figs_v2.py`); every "
        "number is read from the listed committed file at run time. PNG (200 dpi) and SVG.",
        "",
        "**Style:** AGENTS.md \"Figures\" (Jacob, 29 Oct): `sns.set_theme(style=\"whitegrid\")` "
        "with every other setting at seaborn's default (fonts, text sizes and colours), the default "
        "(deep) palette, plain vertical bars and 95% error bars drawn as `sns.barplot` draws them. "
        "This replaces the 26 Oct warm-style exception. No captions inside the images (29 Oct): "
        "each chart has a one-line title (given below as \"Title on the chart\"), and its notes "
        "and sources are here only.",
        "",
        "Colours (deep palette): raw loss grey, precision-corrected orange, lost-and-matters red; "
        "each compaction prompt and item type keeps one colour, never one of those three.",
        "",
    ])
    chart_loss_by_source()
    chart_lost_by_type()
    chart_prompts()
    chart_chain()
    (OUT / "README.md").write_text("\n".join(README))


if __name__ == "__main__":
    main()
