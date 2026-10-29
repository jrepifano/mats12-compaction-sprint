"""Two more week-2 charts for the presentation to Neel: why agents repeat the force-push, and
cheating with and without the test rule. Same look and rules as scripts/sprint_figs_week2.py,
whose helpers (draw, save, wilson) are reused.

Every plotted number is read from the unchanged copies in docs/sprint/figs/week2/data/, whose
origins (branch, commit, path, sha256) are in data/SOURCES_extra.json; each copy's sha256 is
checked before plotting. Sources:
- precedent_why/justification_read.md (the code table: an Opus read of Experiment 1's 32
  divergent second tasks, 16 that lost the teammate's commit and 16 that kept it)
- exploit/readout.json (cells[*].cheat, .test_edit, .special_case, .n)
Writes docs/sprint/figs/week2/06_*, 07_*, backup_07_* (PNG + SVG) and README_why_and_exploit.md.
Run: uv run python scripts/sprint_figs_week2_extra.py
"""

import hashlib
import json
import re

import matplotlib.pyplot as plt
import sprint_figs_week2 as W  # applies sns.set_theme(style="whitegrid")
from scipy.stats import fisher_exact

DATA = W.OUT / "data"
WHY = "precedent_why/justification_read.md"
EXP = "exploit/readout.json"
LOST, KEPT = W.DIVERGENT, W.CONTROL  # lost the teammate's commit / kept it
NO_RULE, RULE = W.HISTORY, W.PAL[0]  # deep red / blue
PLAIN = {"fresh": "no earlier task", "one_prior": "1 earlier task", "long": "5 earlier tasks",
         "fresh_rule": "no earlier task, rule", "long_rule": "5 earlier tasks, rule"}


def verify_sources():
    src = json.loads((DATA / "SOURCES_extra.json").read_text())
    for rel, meta in src.items():
        got = hashlib.sha256((DATA / rel).read_bytes()).hexdigest()
        if got != meta["sha256"]:
            raise SystemExit(f"{rel}: sha256 {got} != recorded {meta['sha256']}")
    return src


def data_why():
    """{code: (lost k, lost n, kept k, kept n)} from the read's table; 'thanks' for the
    'Thanks, that's done' row."""
    rows = {}
    for line in (DATA / WHY).read_text().splitlines():
        m = re.match(r"\| (\([a-f]\)|–) \| (.+?) \| (\d+)/(\d+) \| (\d+)/(\d+) \|$", line)
        if m:
            key = m[1].strip("()") if m[1] != "–" else "thanks"
            rows[key] = tuple(int(m[i]) for i in range(3, 7))
    assert set(rows) == set("abcdef") | {"thanks"}, rows
    assert all(r[1] == 16 and r[3] == 16 for r in rows.values()), rows
    return rows


def chart_why():
    rows = data_why()
    order = [("a", "Its own earlier\nact (precedent)"),
             ("d", "Summary or teammate's\ncommits called\nscratch, junk or stale"),
             ("b", "Perceived\napproval"),
             ("c", "Grader or tests\nexpect it"),
             ("thanks", "Cites \"Thanks,\nthat's done\"")]
    groups = []
    for key, lab in order:
        lk, ln, kk, kn = rows[key]
        groups.append((lab, [dict(lab="lost", color=LOST, **W.share(lk, ln)),
                             dict(lab="kept", color=KEPT, **W.share(kk, kn))]))
    fig, ax = plt.subplots(figsize=(11, 7.4))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.77, bottom=0.37)
    W.draw(ax, groups, group_offset=30)
    ax.set_ylabel("Second tasks stating the reason")
    a, d, b, c, t = (rows[k] for k in ("a", "d", "b", "c", "thanks"))
    title = (f"In DeepSeek's stated reasons, agents that overwrote the teammate's commit "
             f"called it or\nthe summary scratch, junk or stale ({d[0]}/{d[1]}) and cited their "
             f"own earlier act ({a[0]}/{a[1]}); almost\nnone cited approval ({b[0] + b[2]}/32), "
             f"the grader ({c[0] + c[2]}/32) or the \"Thanks, that's done\" line "
             f"({t[0] + t[2]}/32)")
    refused = refusals()
    foot = ("Source: Opus 5.5 read of each second task's own reasoning and commands, 32 runs "
            "whose first task met a divergent remote (pi-style agent + DeepSeek V4.1 Flash); "
            "\"lost\" = the push overwrote the teammate's commit (16), \"kept\" = it did not "
            f"(16). Several reasons per run; Opus refused {refused} reads, which the builder "
            "read by hand; not validated against independent hand labels.")
    caveat = ("The one approval hit quotes the task's instruction (\"Force-pushing with lease "
              "as instructed\"), not approval: on a hand check it belongs under \"other\".")
    assert "as instructed" in (DATA / WHY).read_text()
    W.save(fig, "06_why_repeat", title,
           "share of second tasks whose own calls state each reason for how the agent handled "
           "the teammate's commits, split by whether the push lost them.",
           [f"{WHY} (table rows (a)-(f) and the \"Thanks, that's done\" row)"],
           [f"{lab.replace(chr(10), ' ')}: lost {W.fmt(W.share(r[0], r[1]))}, kept "
            f"{W.fmt(W.share(r[2], r[3]))}"
            for key, lab in order for r in [rows[key]]],
           [(f"Not plotted: (e) other, lost {rows['e'][0]}/16, kept {rows['e'][2]}/16; (f) "
             f"no reason stated, lost {rows['f'][0]}/16, kept {rows['f'][2]}/16."),
            "Codes are not exclusive, so bars in a group do not sum to 100%.",
            ("The (d) code is the read's \"summary or teammate commits called scratch, junk "
             "or stale\".")], foot, caveat,
           short=(f"Agents that overwrote called the commit or summary scratch, junk or stale "
                  f"({d[0]}/{d[1]}) and cited their own earlier act ({a[0]}/{a[1]})"))
    return rows


def refusals():
    """The number of reads Opus refused, from the builder's notes in the read."""
    m = re.search(r"Opus refused (\d+) of 32 reads", (DATA / WHY).read_text())
    assert m, "refusal count missing from the read's notes"
    return int(m[1])


def data_exploit():
    cells = json.loads((DATA / EXP).read_text())["cells"]
    tasks = {t for x in cells.values() for t in x["by_task"]}
    out = {"_tasks": len(tasks)}
    for c in ("fresh", "fresh_rule", "long", "long_rule", "one_prior"):
        x = cells[c]
        k, no, pending = x["cheat"]
        assert pending == 0 and k + no == x["n"], (c, x["cheat"], x["n"])
        # The backup's note: cheat = special-cased + test edit, none did both, no runner edits.
        assert x["special_case"] + x["test_edit"] == k and x["runner_edit"][0] == 0, (c, x)
        out[c] = {"n": x["n"], "cheat": k, "test_edit": x["test_edit"],
                  "special_case": x["special_case"], "by_task": x["by_task"],
                  "method_read": x["method_read"]}
    return out


def by_kind(d):
    """Pool the no-earlier-task and 5-earlier-task cells and split the tasks by the kind of
    cheating seen on them: 'te' (test edits) and 'sc' (special-casing)."""
    pairs = {"no_rule": ("fresh", "long"), "rule": ("fresh_rule", "long_rule")}
    tasks = {k: {t for c in ("fresh", "long", "fresh_rule", "long_rule")
                 for t, v in d[c]["by_task"].items() if v[f]}
             for k, f in (("te", "test_edit"), ("sc", "special_case"))}
    assert not tasks["te"] & tasks["sc"], tasks
    out = {}
    for k, f in (("te", "test_edit"), ("sc", "special_case")):
        out[k] = {"tasks": len(tasks[k])}
        for arm, cells in pairs.items():
            hit = sum(d[c]["by_task"][t][f] for c in cells for t in tasks[k]
                      if t in d[c]["by_task"])
            n = sum(d[c]["by_task"][t]["n"] for c in cells for t in tasks[k]
                    if t in d[c]["by_task"])
            sh = W.share(hit, n)  # clamp float noise at k = 0 (Wilson lo ~1e-17 > rate)
            out[k][arm] = sh | {"lo": min(sh["lo"], sh["rate"]), "hi": max(sh["hi"], sh["rate"])}
    return out


def chart_exploit():
    d = data_exploit()
    n_tasks = d.pop("_tasks")
    p_fresh = fisher_exact([[d["fresh"]["cheat"], d["fresh"]["n"] - d["fresh"]["cheat"]],
                            [d["fresh_rule"]["cheat"],
                             d["fresh_rule"]["n"] - d["fresh_rule"]["cheat"]]])[1]
    p_long = fisher_exact([[d["long"]["cheat"], d["long"]["n"] - d["long"]["cheat"]],
                           [d["long_rule"]["cheat"],
                            d["long_rule"]["n"] - d["long_rule"]["cheat"]]])[1]

    def bar(c, lab, color, field="cheat"):
        return dict(lab=lab, color=color, **W.share(d[c][field], d[c]["n"]))

    groups = [("No earlier task", [bar("fresh", "no rule", NO_RULE),
                                   bar("fresh_rule", "rule", RULE)]),
              ("After 5 earlier tasks", [bar("long", "no rule", NO_RULE),
                                         bar("long_rule", "rule", RULE)])]
    kinds = by_kind(d)
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(14, 7.4), gridspec_kw={"wspace": 0.18})
    fig.subplots_adjust(left=0.06, right=0.99, top=0.68, bottom=0.25)
    W.draw(ax, groups, group_offset=30)
    ax.set_ylabel("Runs that cheated")
    ax.set_title("Cheated", pad=24)
    W.draw(bx, [(f"The {kinds['te']['tasks']} tasks where agents\nedited the test",
                 [dict(lab="no rule", color=NO_RULE, **kinds["te"]["no_rule"]),
                  dict(lab="rule", color=RULE, **kinds["te"]["rule"])]),
                ((f"The {kinds['sc']['tasks']} tasks where agents made the\ncontradictory test "
                  "pass unedited"),
                 [dict(lab="no rule", color=NO_RULE, **kinds["sc"]["no_rule"]),
                  dict(lab="rule", color=RULE, **kinds["sc"]["rule"])])], group_offset=30)
    bx.set_ylabel("Runs that cheated that way")
    bx.set_title("How they cheated (no earlier task and 5 earlier tasks pooled)", pad=24)
    f, fr, lg, lr = (d[c] for c in ("fresh", "fresh_rule", "long", "long_rule"))
    te, sc = kinds["te"], kinds["sc"]
    title = (f"Telling the agent not to modify the tests stopped test edits "
             f"({te['no_rule']['k']}/{te['no_rule']['n']} \u2192 {te['rule']['k']}/"
             f"{te['rule']['n']}), but not passing the contradictory\ntest unedited "
             f"({sc['no_rule']['k']}/{sc['no_rule']['n']} \u2192 {sc['rule']['k']}/"
             f"{sc['rule']['n']}); cheating fell "
             f"from {f['cheat']}/{f['n']} to {fr['cheat']}/{fr['n']} with no earlier task (p = "
             f"{p_fresh:.3f})\nand from {lg['cheat']}/{lg['n']} to {lr['cheat']}/{lr['n']} after "
             f"5 earlier tasks (p = {p_long:.2f})")
    foot = (f"Source: exploitation ending, {n_tasks} tasks whose changed test contradicts the "
            "spec; Claude "
            "Code + DeepSeek V4.1 Flash; cheat = the changed test passes as given "
            "(scored as special-casing) or a test edit; p = two-sided Fisher exact. \"Rule\" = the task "
            "opening adds \"Don't modify the tests.\"")
    foot_main = foot + (" Right panel: task groups defined after seeing the outcomes; "
                        "descriptive.")
    numbers = [f"{PLAIN[c]}: {W.fmt(W.share(d[c]['cheat'], d[c]['n']))} (test edit "
               f"{d[c]['test_edit']}, special-cased {d[c]['special_case']})" for c in d]
    W.save(fig, "07_exploit_rule", title,
           "share of runs that cheated, with and without the test rule (left), and, pooling "
           "the two starts, how they cheated on the tasks where each kind occurred (right).",
           [f"{EXP} (cells[*].cheat, .n, .test_edit, .special_case)"], numbers,
           [(f"After 1 earlier task (no rule, not plotted here): {d['one_prior']['cheat']}/"
             f"{d['one_prior']['n']}; shown in the backup."),
            (f"The 5-earlier-task pair is the same {d['long']['n']} histories with and without "
             "the rule; Fisher ignores that pairing."),
            f"Wilson intervals ignore clustering by task ({n_tasks} tasks).",
            "The numbers of earlier tasks (1, 5) are the cells' design, not read from the copy.",
            (f"Right panel: tasks are grouped by the kind of cheating seen on them, which is "
             f"defined after the fact from the readout's by_task counts: test edits only on "
             f"{te['tasks']} tasks, the changed test passing unedited only on the other "
             f"{sc['tasks']} (asserted "
             f"disjoint). Pooled: test edits {W.fmt(te['no_rule'])} without the rule, "
             f"{W.fmt(te['rule'])} with it; special-cased {W.fmt(sc['no_rule'])} without, "
             f"{W.fmt(sc['rule'])} with."),
            ("\"Passing the contradictory test unedited\" is the scorer's special-case flag "
             "(the changed test passes as given, with no test edit). Opus's read of how each "
             "run did it is less strict about calling it special-casing: rule cells' method "
             "reads give " + "; ".join(
                 f"{PLAIN[c]}: " + ", ".join(f"{m} {v}" for m, v in
                                             d[c]["method_read"].items() if v)
                 for c in ("fresh_rule", "long_rule")) + ".")],
           foot_main,
           short=(f"The test rule stopped test edits ({te['no_rule']['k']}/{te['no_rule']['n']} "
                  f"\u2192 {te['rule']['k']}/{te['rule']['n']}) but not passing the contradictory test unedited "
                  f"({sc['no_rule']['k']}/{sc['no_rule']['n']} \u2192 {sc['rule']['k']}/"
                  f"{sc['rule']['n']})"))

    # Backup: every cell, cheating split into test edits and special-casing.
    order = [("fresh", "No earlier\ntask"), ("one_prior", "1 earlier\ntask"),
             ("long", "5 earlier\ntasks"), ("fresh_rule", "No earlier\ntask, rule"),
             ("long_rule", "5 earlier\ntasks, rule")]
    groups = [("Cheated", [bar(c, lab, RULE if "rule" in c else NO_RULE) for c, lab in order]),
              ("Edited the test", [bar(c, lab, RULE if "rule" in c else NO_RULE, "test_edit")
                                   for c, lab in order])]
    fig, ax = plt.subplots(figsize=(13, 6.6))
    fig.subplots_adjust(left=0.07, right=0.98, top=0.77, bottom=0.27)
    W.draw(ax, groups, group_offset=48)
    ax.set_ylabel("Runs")
    te_rule = fr["test_edit"] + lr["test_edit"]
    sc_rule = fr["special_case"] + lr["special_case"]
    n_rule = fr["n"] + lr["n"]
    title = (f"Under the rule no run edited the test ({te_rule}/{n_rule}), but "
             f"{sc_rule}/{n_rule} still made the\ncontradictory test pass without editing it")
    assert fr["cheat"] + lr["cheat"] == sc_rule
    W.save(fig, "backup_07_exploit_all_cells", title,
           "share of runs that cheated, and that edited the test, in every cell.",
           [f"{EXP} (cells[*].cheat, .test_edit, .special_case, .n)"],
           [f"{PLAIN[c]}: cheated {W.fmt(W.share(d[c]['cheat'], d[c]['n']))}; edited the test "
            f"{W.fmt(W.share(d[c]['test_edit'], d[c]['n']))}" for c, _ in order],
           [("Cheated = special-cased + edited the test (no run did both; confirmed runner "
             "edits: 0).")], foot, short=" ".join(title.split()))
    return d, p_fresh, p_long


def main():
    src = verify_sources()
    W.README.extend([
        "# Week-2 figures: why agents repeat the force-push; the test rule",
        "",
        ("Generated by `scripts/sprint_figs_week2_extra.py` (`uv run python "
         "scripts/sprint_figs_week2_extra.py`), with the helpers and style of "
         "`scripts/sprint_figs_week2.py` (see its README.md). Every number is read at run time "
         "from the unchanged copies in `data/`, whose sha256 the script checks first."),
        "",
        ("**Review:** gpt-6-astra, `logs/reviews/20261029-week2-figs-why-exploit-r1.md` (2 "
         "blocking: 06's code wording narrowed, the backup title said special-casing; 3 minor) "
         "and `-r2.md` (1 blocking: \"junk\" missing from the code wording), all fixed; "
         "`-r3.md` after the deck's changes (new right panel on 07; DeepSeek named on 06): 2 "
         "blocking (\"special-casing\" overstated the scorer flag; \"chain of thought\" "
         "overstated a read of reasoning, text and commands) and 1 minor (post-hoc task groups "
         "not disclosed on the chart), all fixed. Round cap reached; the r3 fixes are not "
         "re-reviewed."),
        "",
        "## Data sources",
        "",
        "| Copy in `data/` | Branch | Last commit touching it | Path on branch | sha256 |",
        "|---|---|---|---|---|"]
        + [f"| `{rel}` | {m['branch']} | {m['last_commit'][:8]} | `{m['path']}` | "
           f"{m['sha256'][:12]} |" for rel, m in src.items()] + [""])
    chart_why()
    chart_exploit()
    (W.OUT / "README_why_and_exploit.md").write_text("\n".join(W.README).rstrip() + "\n")


if __name__ == "__main__":
    main()
