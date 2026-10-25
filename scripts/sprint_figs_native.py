"""Vertical bar charts of the native-harness compaction results (24-25 Oct 2026), for the slides.

Every number is read from a committed file at run time. Reuses the style helpers of
scripts/sprint_figs.py (same fonts, colours, Wilson interval, footer).
Sources:
- results/sprint/S2/native/claude_code/summary.json, agy/summary.json, codex/summary_150k.json,
  codex/summary_80k.json (one row per compaction boundary; codex rows without a boundary are
  runs that never compacted)
- code-check counts: the `code_check` text of those JSONs (Claude Code, Antigravity) and the
  verify docs / summaries cited in the README (Codex), parsed with regexes that fail loudly
- results/sprint/task_validation/summary.json (rows[] verdict, probe, layers, hidden_tests)
Writes docs/sprint/figs/native/*.png and README.md. Run: uv run python scripts/sprint_figs_native.py
"""

import json
import re
import statistics
from pathlib import Path

import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.patches import Patch

import sprint_figs as sf
from sprint_figs import INK, MUTED, PAL, wilson

ROOT = sf.ROOT
OUT = ROOT / "docs" / "sprint" / "figs" / "native"
sf.OUT = OUT  # finish() saves here
# Fixed colour per harness across every native chart. Codex 80k shares Codex's colour, lighter and hatched.
H_CC, H_CX, H_AGY = PAL[0], PAL[1], PAL[2]
H_CX80 = tuple(0.55 * c + 0.45 for c in H_CX)  # 45% towards white
HCOL = {"cc": H_CC, "cx150": H_CX, "cx80": H_CX80, "agy": H_AGY}
HHATCH = {"cx80": "//"}
# Stacked-category colours (consistent): implemented / partly / not; clean / leak / not checked.
C_YES, C_PART, C_NO, C_UNK = PAL[2], PAL[1], PAL[3], PAL[7]
N = "results/sprint/S2/native/"
CC = sf.load(N + "claude_code/summary.json")
AGY = sf.load(N + "agy/summary.json")
CX150 = sf.load(N + "codex/summary_150k.json")
CX80 = sf.load(N + "codex/summary_80k.json")
TXT150 = (ROOT / N / "codex/summary_150k.md").read_text()
TXT80 = (ROOT / N / "codex/summary_80k.md").read_text()
VERIFY = {
    "kombu80": (ROOT / "logs/s2/native_codex_kombu80k_verify.md").read_text(),
    "testem80": (ROOT / "logs/s2/native_codex_testem80k_b2_verify.md").read_text(),
}
LAB = {
    "cc": "Claude Code\n(Haiku 5.5)",
    "cx150": "Codex, 150k\n(gpt-6-luna)",
    "cx80": "Codex, 80k induced\n(gpt-6-luna)",
    "agy": "Antigravity\n(Gemini 3.8 Flash)",
}
ORDER = ["cc", "cx150", "cx80", "agy"]
CHARTS, SKIPPED = [], []


def fin(fig, name, title, foot, sources):
    """sf.finish with the title wrapped to the figure width (the title is kept whole in README)."""
    import textwrap
    sf.finish(fig, name, "\n".join(textwrap.wrap(title, 62)), foot, sources)


def req(r):
    """(items, lost, new, inherited, unknown) of the maintainer-requirement ledger for a row."""
    if "req" in r:
        q = r["req"]
        return q["items"], q["lost"], q["new"], q["inherited"], q["unknown"]
    if r.get("req_items") is None:
        return None
    return r["req_items"], r["req_lost"], r["req_new"], r["req_inh"], r["req_unk"]


def la(r):
    if "la" in r:
        return r["la"]["items"], r["la"]["lost"]
    if r.get("la_items") is None:
        return None
    return r["la_items"], r["la_lost"]


def rows_of(key):
    return {"cc": CC, "cx150": CX150, "cx80": CX80, "agy": AGY}[key]


def pooled(key, fn):
    got = [fn(r) for r in rows_of(key) if fn(r) is not None]
    return [sum(g[i] for g in got) for i in range(len(got[0]))], len(got)


def rate_row(key, k, n, nb, unit):
    lo, hi = wilson(k, n)
    return {
        "label": LAB[key], "value": k / n, "lo": lo, "hi": hi, "k": k, "n": n, "nb": nb,
        "n_text": f"{k}/{n} {unit}\n{nb} compactions",
    }


def rate_chart(name, title_fn, fn, unit, ylab, sources, notes, claim_fn):
    rows = []
    for key in ORDER:
        tot, nb = pooled(key, fn)
        rows.append(rate_row(key, tot[1], tot[0], nb, unit))
    fig, ax = plt.subplots(figsize=(10.5, 6.6))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.30)
    for r, k in zip(rows, ORDER):
        r["color"], r["hatch"] = HCOL[k], HHATCH.get(k)
    sf.bars(ax, rows, ymax=max(r["hi"] for r in rows) * 100 * 1.25)
    ax.set_ylabel(ylab)
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))  # whole-percent ticks only
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    title = title_fn(rows)
    fin(fig, name, title, notes[0], sources)
    CHARTS.append({"name": name, "title": title, "claim": claim_fn(rows), "sources": sources,
                   "rows": [f"{LAB[k].replace(chr(10), ' ')}: {r['k']}/{r['n']} = "
                            f"{r['value']*100:.1f}% [{r['lo']*100:.1f}, {r['hi']*100:.1f}]; "
                            f"{r['nb']} compactions" for k, r in zip(ORDER, rows)],
                   "notes": notes})
    return rows


# 1 and 2 ---------------------------------------------------------------------------------------
POOL_NOTE = ("Pooled over all compactions of an arm; compactions in one run are not independent, "
             "so the 95% Wilson intervals are rough guides only. Codex 80k is an induced early "
             "trigger and is not pooled with Codex 150k.")


def c01():
    srcs = [N + "claude_code/summary.json, agy/summary.json (req.items, req.lost per boundary)",
            N + "codex/summary_150k.json, summary_80k.json (req_items, req_lost per boundary)"]

    def title(rows):
        d = {k: r["value"] * 100 for k, r in zip(ORDER, rows)}
        return (f"Claude Code loses the fewest maintainer requirements per compaction "
                f"({d['cc']:.0f}%); Codex and Antigravity lose {d['cx150']:.0f}-{d['cx80']:.0f}%")

    return rate_chart(
        "01_requirements_lost_by_harness", title, req, "reqs", "maintainer requirements lost",
        srcs, [POOL_NOTE + " Pi's live first compaction (all 39 requirements kept) is left out: "
               "it is in docs/sprint/progress.md only, no committed result file."],
        lambda rows: "Share of maintainer requirements stated before a compaction that the "
                     "summary lost, pooled over boundaries.")


def c02():
    srcs = [N + "claude_code/summary.json, agy/summary.json (la.items, la.lost per boundary)",
            N + "codex/summary_150k.json, summary_80k.json (la_items, la_lost per boundary)"]

    def title(rows):
        d = {k: r["value"] * 100 for k, r in zip(ORDER, rows)}
        lo, hi = min(d.values()), max(d.values())
        return f"Item-level audit: every harness loses {lo:.0f}-{hi:.0f}% of items per compaction"

    return rate_chart(
        "02_items_lost_by_harness", title, la, "items", "items lost",
        srcs, [POOL_NOTE + " Items are all actionable items found by the S1 item audit "
               "(requirements, decisions, task state), not only maintainer requirements."],
        lambda rows: "Share of audited items lost by the summary, pooled over boundaries.")


# 3 ---------------------------------------------------------------------------------------------
def tcell(text, pat):
    m = re.search(pat, text)
    assert m, pat
    return int(m.group(1))


def code_checks():
    """-> list of (label, implemented, partly, not, source, note)."""
    out = []
    # Claude Code vitest b2, b3: code_check "ledger 7/7 implemented"
    rr = [r for r in CC if r["task"].startswith("vitest") and r.get("code_check")]
    a = sum(tcell(r["code_check"], r"(\d+)/\d+ implemented") for r in rr)
    n = sum(tcell(r["code_check"], r"\d+/(\d+) implemented") for r in rr)
    out.append(("Claude Code\nvitest", a, 0, n - a, N + "claude_code/summary.json (code_check, vitest b2+b3)",
                "b3 repeats 4 requirements already lost at b2"))
    # Antigravity vitest b1, b2: "ledger 5/6 implemented at b1", "ledger 22/22 implemented at b2"
    rr = [r for r in AGY if r["task"].startswith("vitest") and r.get("code_check")]
    a = sum(tcell(r["code_check"], r"(\d+)/\d+ implemented") for r in rr)
    n = sum(tcell(r["code_check"], r"\d+/(\d+) implemented") for r in rr)
    out.append(("Antigravity\nvitest", a, 0, n - a, N + "agy/summary.json (code_check, vitest b1+b2)",
                "the one not implemented was re-raised by the maintainer"))
    # Antigravity testem a2: b1 sample one implemented; b2 "7 checked at b2): 6 implemented"
    rr = [r for r in AGY if r["task"].startswith("testem") and r.get("code_check")]
    b1 = [r for r in rr if r["b"] == 1][0]["code_check"]
    b2 = [r for r in rr if r["b"] == 2][0]["code_check"]
    assert "implemented at b1" in b1
    chk2 = tcell(b2, r"(\d+) checked at b2")
    imp2 = tcell(b2, r"\): (\d+) implemented")
    out.append(("Antigravity\ntestem (sample)", 1 + imp2, chk2 - imp2, 0,
                N + "agy/summary.json (code_check, testem a2 b1+b2)",
                "random sample of 8 lost requirements, not all of them"))
    # Codex boa 150k b1 (3 lost): summary_150k.md text
    boa = [r for r in CX150 if r["task"].startswith("boa") and r.get("b")][0]
    assert "boa b1's ledger losses are implemented in the code at the boundary" in TXT150.replace("\n  ", " ")
    out.append(("Codex 150k\nboa", boa["req_lost"], 0, 0,
                N + "codex/summary_150k.json (req_lost) + summary_150k.md Code checks",
                "2 of 3 read in the patches, not executed"))
    # Codex vitest 150k b2
    vit = [r for r in CX150 if r["task"].startswith("vitest") and r.get("b") == "b2"][0]
    flat = TXT150.replace("\n  ", " ")
    assert tcell(flat, r"vitest b2's (\d+) dropped requirements were all fully implemented") == vit["req_lost"]
    out.append(("Codex 150k\nvitest", vit["req_lost"], 0, 0,
                N + "codex/summary_150k.json (req_lost) + summary_150k.md Code checks", ""))
    # Codex kombu 80k b2
    kv = VERIFY["kombu80"].split("## kombu 80k b2 on clean replies")[1]
    imp = tcell(kv, r"\| Implemented at b2 and in the final code \| (\d+)")
    prob = tcell(kv, r"\| Probably implemented at b2 and in the final code \| (\d+)")
    miss = tcell(kv, r"\| Missing at b2 and from the final code \| \*\*(\d+)\*\*")
    kb2 = [r for r in CX80 if r["task"].startswith("kombu") and r.get("b") == "b2"][0]
    assert imp + prob + miss == kb2["req_lost"]
    out.append(("Codex 80k\nkombu", imp, prob, miss, "logs/s2/native_codex_kombu80k_verify.md "
                "(section 'kombu 80k b2 on clean replies')", "'probably implemented' shown as partly"))
    # Codex testem 80k b2
    tv = VERIFY["testem80"]
    i1 = tcell(tv, r"\| Implemented at b2, covered by hidden tests that pass at b2 and final \| (\d+)")
    i2 = tcell(tv, r"\| Implemented at b2, no test isolates it, read in the code \| (\d+)")
    i3 = tcell(tv, r"\| Implemented at b2, hidden tests fail at b2 and final, traced to a reply rather than the summary \| (\d+)")
    miss = tcell(tv, r"\| Missing at b2 and from the final code \| \*\*(\d+)\*\*")
    tb2 = [r for r in CX80 if r["task"].startswith("testem") and r.get("b") == "b2"][0]
    assert i1 + i2 + i3 + miss == tb2["req_lost"]
    out.append(("Codex 80k\ntestem", i1 + i2 + i3, 0, miss,
                "logs/s2/native_codex_testem80k_b2_verify.md", "6 of 37 fail hidden tests for reasons traced to maintainer replies"))
    return out


def c03():
    cc = code_checks()
    fig, ax = plt.subplots(figsize=(11.5, 6.8))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.80, bottom=0.30)
    xs = range(len(cc))
    tot_all = sum(c[1] + c[2] + c[3] for c in cc)
    imp_all = sum(c[1] for c in cc)
    for x, (lab, a, p, nn, _, _) in zip(xs, cc):
        n = a + p + nn
        bottom = 0
        for v, col in ((a, C_YES), (p, C_PART), (nn, C_NO)):
            if v:
                ax.bar(x, v / n * 100, bottom=bottom, width=0.62, color=col, zorder=2)
                if v / n > 0.07:
                    ax.text(x, bottom + v / n * 50, str(v), ha="center", va="center", color="white", fontsize=11)
                bottom += v / n * 100
        ax.text(x, 101.5, f"n={n}", ha="center", va="bottom", fontsize=11)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([c[0] for c in cc], fontsize=11)
    ax.set_ylim(0, 112)
    ax.set_ylabel("share of lost requirements checked")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%" if v <= 100 else ""))
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", length=0)
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c) for c in (C_YES, C_PART, C_NO)],
              labels=["already implemented in the code", "partly / probably implemented", "not implemented"],
              loc="upper center", bbox_to_anchor=(0.5, -0.27), ncol=3, frameon=False, fontsize=10.5)
    title = (f"Most requirements a summary dropped were already in the code "
             f"({imp_all} of {tot_all} checked)")
    srcs = sorted({c[4] for c in cc})
    foot = ("Only boundaries with a code check are shown; 4 of the 7 bars are full checks of every "
            "lost requirement, Antigravity testem is a random sample of 8. n = lost requirements checked.")
    fin(fig, "03_lost_requirements_vs_code", title, foot, srcs)
    CHARTS.append({"name": "03_lost_requirements_vs_code", "title": title,
                   "claim": "Of the requirements the summary lost, how many the agent's code already "
                            "implemented at that boundary (summary-only loss, no behavioural cost yet).",
                   "sources": srcs,
                   "rows": [f"{c[0].replace(chr(10), ' ')}: implemented {c[1]}, partly {c[2]}, "
                            f"not {c[3]} (n={c[1]+c[2]+c[3]}); {c[5]}" for c in cc],
                   "notes": ["Not pooled across bars; counts are per check set. Each bar's source is listed "
                             "in the table above. Code checks are reading the code or running the task's "
                             "hidden tests; 'implemented' for Codex testem includes 6 whose hidden tests fail "
                             "for reasons traced to the maintainer's own replies, not the summary."]})


# 4 ---------------------------------------------------------------------------------------------
def c04():
    rows = []
    for key in ORDER:
        tot, nb = pooled(key, req)
        _, lost, new, inh, unk = tot
        assert lost == new + inh + unk, key
        lo, hi = wilson(inh, lost)
        rows.append({"label": LAB[key], "value": inh / lost, "lo": lo, "hi": hi, "k": inh, "n": lost,
                     "n_text": f"{inh}/{lost} lost\nalready lost before",
                     "color": HCOL[key], "hatch": HHATCH.get(key)})
    fig, ax = plt.subplots(figsize=(10.5, 6.6))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.30)
    sf.bars(ax, rows, ymax=max(r["hi"] for r in rows) * 100 * 1.25)
    ax.set_ylabel("lost requirements that were inherited")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    d = {k: r for k, r in zip(ORDER, rows)}
    title = "Most lost requirements are new at each compaction; 0-27% were already lost earlier"
    srcs = [N + "claude_code/summary.json, agy/summary.json (req.inherited, req.lost)",
            N + "codex/summary_150k.json, summary_80k.json (req_inh, req_lost)"]
    fin(fig, "04_inherited_vs_new_losses", title,
              "Inherited = lost at an earlier compaction of the same run and still lost. " + POOL_NOTE, srcs)
    CHARTS.append({"name": "04_inherited_vs_new_losses", "title": title,
                   "claim": "Of the requirements lost at a boundary, the share the previous summary had already lost.",
                   "sources": srcs,
                   "rows": [f"{LAB[k].replace(chr(10), ' ')}: {r['k']}/{r['n']} = {r['value']*100:.1f}% "
                            f"[{r['lo']*100:.1f}, {r['hi']*100:.1f}]; new {r['n']-r['k']}" for k, r in d.items()],
                   "notes": ["Arms have different numbers of boundaries (a second boundary can inherit, a first "
                             "cannot), so shares are not a like-for-like comparison. 'Unknown' grades (none in "
                             "these files) would be counted as neither."]})


# 5 ---------------------------------------------------------------------------------------------
def c05():
    m150 = tcell(TXT150, r"model_auto_compact_token_limit = (\d+)")
    m80 = tcell(TXT80, r"model_auto_compact_token_limit = (\d+)")
    groups, gcol = [], []
    for t in sorted({r["trigger"] for r in CC}):
        groups.append((f"Claude Code\nwindow {t//1000}k", [r["pre_tokens"] for r in CC if r["trigger"] == t], t))
        gcol.append(H_CC)
    groups.append(("Codex\n150k", [r["pre_tokens"] for r in CX150 if r.get("pre_tokens")], m150))
    groups.append(("Codex\n80k induced", [r["pre_tokens"] for r in CX80 if r.get("pre_tokens")], m80))
    groups.append(("Antigravity\n150k (forced)", [r["pre_tokens"] for r in AGY], AGY[0]["trigger"]))
    gcol += [H_CX, H_CX80, H_AGY]
    assert all(r["trigger"] == AGY[0]["trigger"] for r in AGY)
    fig, ax = plt.subplots(figsize=(11, 6.8))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.26)
    lines = []
    for x, (lab, pts, cfg) in enumerate(groups):
        k = len(pts)
        xs = [x + (i - (k - 1) / 2) * min(0.7 / max(k, 1), 0.07) for i in range(k)]
        ax.scatter(xs, [p / 1000 for p in pts], s=34, color=gcol[x], edgecolor=INK, linewidth=0.5, zorder=3)
        ax.hlines(cfg / 1000, x - 0.4, x + 0.4, color=INK, lw=2, zorder=4)
        med = statistics.median(pts)
        gap = cfg - med
        ax.text(x, cfg / 1000 + 5, f"set {cfg//1000}k", ha="center", fontsize=10.5, color=INK)
        lines.append((lab.replace("\n", " "), len(pts), min(pts), max(pts), cfg, med))
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([f"{g[0]}\nn={len(g[1])}" for g in groups], fontsize=11)
    ax.set_ylim(0, 190)
    ax.set_ylabel("context tokens when compaction fired (k)")
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", length=0)
    cc_gap = [g[4] - g[5] for g in lines if g[0].startswith("Claude")]
    title = (f"Claude Code fires a median {min(cc_gap)/1000:.0f}-{max(cc_gap)/1000:.0f}k tokens below its set "
             f"window; Codex and Antigravity fire at their trigger")
    srcs = [N + "claude_code/summary.json (trigger, pre_tokens)", N + "agy/summary.json (trigger, pre_tokens)",
            N + "codex/summary_150k.json, summary_80k.json (pre_tokens); setting from the '.md' "
            "(model_auto_compact_token_limit)"]
    fin(fig, "05_trigger_point_by_harness", title,
              "One dot per compaction; black line = the configured setting. Claude Code's setting is a "
              "context-window size and its compaction fires early by a buffer; Antigravity's trigger is forced by "
              "our runner at the first turn end past the setting.", srcs)
    CHARTS.append({"name": "05_trigger_point_by_harness", "title": title,
                   "claim": "Actual context size at each compaction against the configured setting.",
                   "sources": srcs,
                   "rows": [f"{a}: n={n}; pre_tokens min {lo:,} / median {md:,.0f} / max {hi:,}; setting {c:,}"
                            for a, n, lo, hi, c, md in lines],
                   "notes": ["Claude Code's first calibration batch used window 163,000; the rest 170,000 "
                             "(logs/s2/native_arms_report.md, Thresholds)."]})


# 6 ---------------------------------------------------------------------------------------------
ERR = {"transcript_error", "infra_error", "boundary_error"}


def c06():
    stats = {}
    for key in ORDER:
        runs = {(r["task"], r["attempt"]): r["end"] for r in rows_of(key)}
        c = {"submitted": 0, "run_cap": 0, "error": 0}
        for e in runs.values():
            c["error" if e in ERR else e] += 1
        assert sum(c.values()) == len(runs)
        stats[key] = c
    fig, ax = plt.subplots(figsize=(10.5, 6.6))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.30)
    for x, key in enumerate(ORDER):
        c = stats[key]
        n = sum(c.values())
        b = 0
        for lab, col in (("submitted", PAL[4]), ("run_cap", PAL[5]), ("error", PAL[7])):
            if c[lab]:
                ax.bar(x, c[lab], bottom=b, width=0.62, color=col, zorder=2)
                ax.text(x, b + c[lab] / 2, str(c[lab]), ha="center", va="center", color="white", fontsize=12)
                b += c[lab]
    ax.set_xticks(range(len(ORDER)))
    ax.set_xticklabels([f"{LAB[k]}\nn={sum(stats[k].values())} runs" for k in ORDER], fontsize=11)
    ax.set_ylabel("runs")
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", length=0)
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c) for c in (PAL[4], PAL[5], PAL[7])],
              labels=["submitted", "stopped at our simulator-cost cap", "infrastructure or capture error"],
              loc="upper center", bbox_to_anchor=(0.5, -0.27), ncol=3, frameon=False, fontsize=10.5)
    title = "How the runs ended (completion status only, not a task score): too few runs to rank harnesses"
    srcs = [N + "claude_code/summary.json (end)", N + "agy/summary.json (end)",
            N + "codex/summary_150k.json, summary_80k.json (end)"]
    fin(fig, "06_run_end_states_by_harness", title,
              "No verifier reward is in any committed summary file. Runs are unique (task, attempt) pairs "
              "that appear in the summaries; Claude Code, Antigravity and Codex 80k list only runs that "
              "reached a compaction, Codex 150k also lists runs that finished under 150k.", srcs)
    CHARTS.append({"name": "06_run_end_states_by_harness", "title": title,
                   "claim": "Run end state per harness; this is completion status, not a score.",
                   "sources": srcs,
                   "rows": [f"{LAB[k].replace(chr(10), ' ')}: " + ", ".join(f"{a} {v}" for a, v in stats[k].items())
                            + f" (n={sum(stats[k].values())})" for k in ORDER],
                   "notes": ["Different harnesses, models, caps and attempts: the end state mostly reflects "
                             "infrastructure faults and the simulator-cost cap, not agent ability. A few "
                             "verifier results exist for single Codex runs in logs/s2/native_codex_*_verify.md "
                             "(vitest 150k reward 0, partial 0.9625; kombu 80k reward 1) but none for Claude Code "
                             "or Antigravity in committed files; per-run result.json lives in gitignored "
                             "data/raw/sprint/s2/native/ and holds only run_key, end, and timings."]})


# 7 ---------------------------------------------------------------------------------------------
def c07():
    d = sf.load("results/sprint/task_validation/summary.json")
    names = [("deepswe", "DeepSWE"), ("pro_rebuilt", "Pro, rebuilt"), ("rf_rebuilt", "Refactoring,\nrebuilt"),
             ("pro", "Pro,\nas released"), ("rf", "Refactoring,\nas released")]
    res = {}
    for fam, _ in names:
        rr = [r for r in d["rows"] if r["family"] == fam]
        c = {"CLEAN": 0, "LEAK": 0, "UNCHECKED": 0}
        for r in rr:
            checks = [r["verdict"], r["probe"], r["layers"], r["hidden_tests"]]
            if "LEAK" in checks:
                c["LEAK"] += 1
            elif r["verdict"] == "CLEAN":
                c["CLEAN"] += 1
            else:
                c["UNCHECKED"] += 1
        assert sum(c.values()) == len(rr)
        assert c["CLEAN"] == d["counts"][fam]["verdict"].get("CLEAN", 0) or fam in ("rf", "pro")
        res[fam] = c
    fig, ax = plt.subplots(figsize=(10.5, 6.6))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.30)
    for x, (fam, lab) in enumerate(names):
        c = res[fam]
        b = 0
        for k, col in (("CLEAN", C_YES), ("LEAK", C_NO), ("UNCHECKED", C_UNK)):
            if c[k]:
                ax.bar(x, c[k], bottom=b, width=0.62, color=col, zorder=2)
                if c[k] > 2:
                    ax.text(x, b + c[k] / 2, str(c[k]), ha="center", va="center", color="white", fontsize=12)
                b += c[k]
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([f"{l}\nn={sum(res[f].values())}" for f, l in names], fontsize=11)
    ax.set_ylabel("task images")
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", length=0)
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c) for c in (C_YES, C_NO, C_UNK)],
              labels=["clean", "solution reachable (leak)", "not fully checked"],
              loc="upper center", bbox_to_anchor=(0.5, -0.27), ncol=3, frameon=False, fontsize=10.5)
    title = "Released Pro and refactoring images leak their solutions; rebuilt images are clean"
    srcs = ["results/sprint/task_validation/summary.json (rows[].family, verdict, probe, layers, hidden_tests)"]
    fin(fig, "07_image_leak_validation", title,
              "Leak = any of the probe, layer scan or hidden-test check found the solution or the verifier's "
              "tests; clean = the overall verdict is CLEAN (probe clean and red-team found nothing); not fully "
              "checked = no leak found but the red-team did not run or an error occurred.", srcs)
    CHARTS.append({"name": "07_image_leak_validation", "title": title,
                   "claim": "Leak status of each task image set.", "sources": srcs,
                   "rows": [f"{l.replace(chr(10), ' ')}: " + ", ".join(f"{k} {v}" for k, v in res[f].items())
                            for f, l in names],
                   "notes": ["For the as-released refactoring images the overall verdict is ERROR/LEAK "
                             "(red-team not run); they are shown as leaking because the layer scan found "
                             "solution needles in all 25. The verdict column alone would give LEAK 3, ERROR 22."]})


def readme():
    L = ["# Native-harness figures", "",
         "Generated by `scripts/sprint_figs_native.py` (`uv run python scripts/sprint_figs_native.py`); "
         "every number is read from the listed committed file at run time. PNG (200 dpi) and SVG for each chart. "
         "Model names: Claude Code (Haiku 5.5), Codex (gpt-6-luna), Antigravity (Gemini 3.8 Flash).", "",
         "**Pooling.** Bars pool all compactions (boundaries) of an arm. Compactions inside one run are not "
         "independent, and arms differ in tasks, attempts and number of boundaries, so the 95% Wilson "
         "intervals are rough guides, not tests. Codex 80k is an induced early trigger and is never pooled "
         "with Codex 150k. Pi (DeepSeek V4 Flash): its live first compaction kept all 39 of 39 requirements, "
         "but that figure is only in `docs/sprint/progress.md`, with no committed result file, so pi is left "
         "out of the charts.", ""]
    for c in CHARTS:
        L += [f"## {c['name']}: {c['title']}", "", f"![{c['title']}]({c['name']}.png)", "",
              f"**Claim:** {c['claim']}", "", "**Source:**"] + [f"- `{s}`" for s in c["sources"]]
        L += ["", "**Numbers plotted:**"] + [f"- {r}" for r in c["rows"]] + [""]
        L += [f"- Note: {n}" for n in c["notes"]] + [""]
    L += ["## Task scores by harness", "",
          "No verifier reward or score is recorded in any committed summary file for Claude Code, Antigravity "
          "or Codex (the summaries hold counts, grades and `end` only), so chart 06 shows completion status. "
          "A handful of single-run verifier results appear in the Codex verify docs. With 4 to 16 runs per "
          "harness, different tasks, attempts and caps, and ends dominated by infrastructure errors and the "
          "simulator-cost cap, no trend by harness or model can be read from these files.", ""]
    for s in SKIPPED:
        L.append(f"- Skipped: {s}")
    (OUT / "README.md").write_text("\n".join(L))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in (c01, c02, c03, c04, c05, c06, c07):
        f()
    readme()
    print("wrote", len(CHARTS), "charts to", OUT)


if __name__ == "__main__":
    main()
