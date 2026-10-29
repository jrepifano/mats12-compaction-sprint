"""Summary decay by task and safety-relevant retention, pi beside agy on the same four unrelated
chains (u1–u4), plus which earlier task requests Codex carries. Exploratory (J call, 29 Oct;
Jacob: "the vanishing summary stuff with agy").

Inputs, read at pinned commits with git show (nothing copied):
- pi: this branch's attribution (`week2_summary_decay.ATTR`), chains u1–u4.
- agy: week2-build's attribution of its five-task chains (REF_AGY), labelled by
  week2_summary_rot.py's coder unchanged at e9a4c51f (name matching, else Claude Opus 5.5).
- Codex: week2-build's chain summaries (REF_CX). Codex's own summary is encrypted; only its
  replacement history (`carried_context`) is readable, so Codex gets no summary shares, only
  which earlier tasks' requests (by ticket id) the carried history holds.

The per-task shares and the retention by age are week2_summary_decay's own functions
(`task_cells`, `retention_by_age`, `by_task_panel`), so both sides are computed one way. The
keyword rule for safety-relevant entries is checked on agy's entries against gpt-6-astra (Codex,
read-only; a seeded sample, labels in decay/agy/), pi's against Opus in safety.json.

    uv run python scripts/week2_summary_decay_agy.py

Figures follow AGENTS.md "Figures": seaborn defaults, no captions in the image; the notes are in
decay/agy/README.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import week2_summary_decay as D

SRS, K = D.SRS, D.K
SR = SRS.SR
REF_AGY = "55d08ed4"  # sprint/week2-build: agy five-task chains, summary rot with this coder
AGY_ATTR = "results/sprint/week2/agy_chain5/agy/bulk/summary_rot/attributions.jsonl"
REF_CX = "28f8a140"  # sprint/week2-build: Codex five-task chains, compaction export
CX = "results/sprint/week2/agy_chain5/codex/bulk/chain_summaries.jsonl"
CHAINS = ("u1", "u2", "u3", "u4")
OUT = D.OUT / "agy"
SIDES = (("pi", "pi (DeepSeek V4.1 Flash)"), ("agy", "Antigravity (Gemini 3.8 Flash, low)"))
TICKET = re.compile(r"\b[A-Z]{2,5}-\d+\b")
CHECK_IN = OUT / "rule_check_input.jsonl"
CHECK_IDS = OUT / "rule_check_ids.json"  # the sample's entry ids in input order (not shown to the labeller)
CHECK_LABELS = OUT / "rule_check_labels.out"
FLAGS = OUT / "safety_labels" / "main" / "flags.json"  # week2_summary_safety_labels.py


def git_text(ref: str, path: str) -> tuple[str, str]:
    raw = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=SRS.ROOT, check=True,
                         capture_output=True).stdout
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def boundary_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["arm"] == D.ARM and r["kind"] == "boundary"
            and r["chain"] in CHAINS]


def per_summary(rows: list[dict]) -> list[dict]:
    """summary_rot's per-summary shares (week2_summary_rot.cmd_shares), every boundary entry."""
    g = defaultdict(list)
    for r in rows:
        g[(r["chain"], r["boundary"])].append(r)
    return [{"chain": c, "boundary": k, **SR.summary_shares(
        [r["final_label"] for r in sorted(rs, key=lambda r: r["entry"])], k)}
            for (c, k), rs in sorted(g.items())]


def rule_check(rows: list[dict]) -> dict | None:
    """The keyword rule against Codex's labels on agy's seeded sample (stratified by the rule)."""
    if not CHECK_LABELS.exists():
        return None
    ids = json.loads(CHECK_IDS.read_text())
    labs = {}
    for ln in CHECK_LABELS.read_text().splitlines():
        ln = ln.strip().strip("`")
        if ln.startswith("{"):
            d = json.loads(ln)
            labs[d["i"]] = bool(d["safety_relevant"])
    if sorted(labs) != list(range(1, len(ids) + 1)):
        sys.exit(f"{CHECK_LABELS}: {len(labs)} labels for {len(ids)} lines")
    by_id = {r["id"]: r for r in rows}
    sample = [(by_id[i], labs[n]) for n, i in enumerate(ids, 1)]
    tp = sum(r["safe"] and a for r, a in sample)
    fp = sum(r["safe"] and not a for r, a in sample)
    fn = sum(not r["safe"] and a for r, a in sample)
    tn = sum(not r["safe"] and not a for r, a in sample)
    n_pos = sum(r["safe"] for r in rows)
    est_tp, est_fn = n_pos * tp / (tp + fp), (len(rows) - n_pos) * fn / (fn + tn)
    return {"labeller": "gpt-6-astra via scripts/codex_review.sh (read-only), prompt "
                        f"{SRS.rel(OUT / 'rule_check_prompt.md')}",
            "sample_seed": D.SAMPLE_SEED, "rule_pos": {"codex_safe": tp, "codex_other": fp},
            "rule_neg": {"codex_safe": fn, "codex_other": tn},
            "precision": tp / (tp + fp), "npv": tn / (fn + tn),
            "recall_est": est_tp / (est_tp + est_fn),
            "disagreements": [{"id": r["id"], "rule": r["safe"], "codex": a,
                               "text": r["text"].strip()[:300]}
                              for r, a in sample if r["safe"] != a]}


def codex_carried(text: str) -> tuple[list[dict], dict]:
    """Per chain and compaction: which tasks' tickets the carried history holds (tickets from
    pi's code matches on the same chains: the same tasks in the same order)."""
    tickets = defaultdict(set)
    for r in SRS._jsonl(D.ATTR):
        if r["arm"] == D.ARM and r["chain"] in CHAINS:
            for j, toks in (r.get("code_matches") or {}).items():
                tickets[(r["chain"], int(j))] |= {t for t in toks if TICKET.fullmatch(t)}
    out = []
    for x in map(json.loads, text.splitlines()):
        found = set(TICKET.findall(x["carried_context"]))
        held = [j for j in range(1, K + 1) if tickets[(x["chain"], j)] & found]
        out.append({"chain": x["chain"], "kind": x["kind"], "boundary": x["boundary"],
                    "ok": x["ok"], "encrypted": x["encrypted"], "summary_chars": len(x["summary"]),
                    "carried_chars": len(x["carried_context"]), "tasks_held": held})
    return out, {f"{c}:{j}": sorted(v) for (c, j), v in sorted(tickets.items())}


def flagged(rows: list[dict], flag) -> list[dict]:
    """Copies of rows with `safe` set by flag(row), for retention_by_age."""
    return [{**r, "safe": bool(flag(r))} for r in rows]


def agreement(rows: list[dict], astra: dict[str, bool]) -> dict:
    """Keyword rule against gpt-6-astra's label on every entry; the rule's precision and recall
    taking astra as the reference, and Cohen's kappa."""
    n = len(rows)
    both = sum(r["safe"] and astra[r["id"]] for r in rows)
    rule = sum(r["safe"] for r in rows)
    ax = sum(astra[r["id"]] for r in rows)
    agree = sum(r["safe"] == astra[r["id"]] for r in rows)
    pe = (rule * ax + (n - rule) * (n - ax)) / n ** 2
    return {"n": n, "both": both, "rule_only": rule - both, "astra_only": ax - both,
            "neither": n - rule - ax + both, "agreement": agree / n,
            "kappa": (agree / n - pe) / (1 - pe) if pe < 1 else None,
            "rule_precision": both / rule if rule else None,
            "rule_recall": both / ax if ax else None}


def safety_figure(plt, deep, ret: dict, title: str, legend: tuple[str, str],
                  stem: str) -> list[str]:
    """Retention by age, flagged against the rest, pi and agy side by side."""
    from matplotlib.patches import Patch

    top = max(100 * c["ci95"][1] for r in ret.values() for c in r.values())
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.2), sharey=True)
    fig.subplots_adjust(left=0.06, right=0.985, top=0.84, bottom=0.2, wspace=0.06)
    nums = []
    for ax, (s, label) in zip(axes, SIDES, strict=True):
        for d in range(1, K):
            for off, safe, col in ((-0.18, True, deep[3]), (0.18, False, deep[0])):
                c = ret[s][f"{d}:{'safety' if safe else 'other'}"]
                v, (lo, hi) = 100 * c["retention"], c["ci95"]
                ax.bar(d + off, v, 0.34, color=col)
                if hi > lo:
                    ax.errorbar(d + off, v, yerr=[[v - 100 * lo], [100 * hi - v]], fmt="none",
                                ecolor="black")
                ax.text(d + off, 100 * hi + 1.5, f"{v:.0f}%", ha="center", va="bottom")
                nums.append(f"{label}, {D.retention_line(d, safe, c)}")
        ax.set_xticks(range(1, K))
        ax.set_xticklabels([f"{d} task{'s' if d > 1 else ''} later" for d in range(1, K)])
        ax.set_title(label, loc="left")
    axes[0].set_ylabel("Entry-count retention (% of the count right after the task)")
    axes[0].set_ylim(0, 10 * (int(top / 10) + 2))
    fig.suptitle(title, x=0.02, y=0.975, ha="left", va="top")
    fig.legend(handles=[Patch(color=deep[3], label=legend[0]),
                        Patch(color=deep[0], label=legend[1])], loc="lower center", ncol=2,
               bbox_to_anchor=(0.5, 0.0))
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"{stem}.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    return nums


def r2(ret: dict) -> dict[str, tuple[float, float]]:
    return {s: (100 * ret[s]["2:safety"]["retention"], 100 * ret[s]["2:other"]["retention"])
            for s in ret}


def main() -> None:
    agy_raw, agy_sha = git_text(REF_AGY, AGY_ATTR)
    cx_raw, cx_sha = git_text(REF_CX, CX)
    rows = {"pi": boundary_rows(SRS._jsonl(D.ATTR)),
            "agy": boundary_rows([json.loads(x) for x in agy_raw.splitlines()])}
    per = {s: per_summary(rs) for s, rs in rows.items()}
    # pi's recomputed shares must equal shares.json's for the same summaries
    sh = {(p["chain"], p["boundary"]): p["shares"]
          for p in json.loads(D.SHARES.read_text())["per_summary"]
          if p["arm"] == D.ARM and p["kind"] == "boundary" and p["chain"] in CHAINS}
    bad = [p for p in per["pi"] if p["shares"] != sh[(p["chain"], p["boundary"])]]
    if bad or len(sh) != len(per["pi"]):
        sys.exit(f"pi u1–u4 shares differ from shares.json: {bad[:2]}")
    cells = {s: D.task_cells(p) for s, p in per.items()}
    lab = {s: [r for r in rs if r["final_label"] is not None] for s, rs in rows.items()}
    for rs in lab.values():
        for r in rs:
            r["safe"] = bool(D.SAFE.search(r["text"]))
    ret = {s: D.retention_by_age(rs) for s, rs in lab.items()}
    share = {s: {str(k): sum(r["safe"] for r in rs if r["boundary"] == k)
                 / sum(r["boundary"] == k for r in rs) for k in range(1, K + 1)}
             for s, rs in lab.items()}
    chk = rule_check(lab["agy"])
    if not FLAGS.exists():
        sys.exit(f"{SRS.rel(FLAGS)} missing: run week2_summary_safety_labels.py first")
    fl = json.loads(FLAGS.read_text())
    ret_ax, agree, n_ax = None, None, None
    if fl:
        f = fl["flags"]
        ret_ax = {s: D.retention_by_age(flagged(rs, lambda r, s=s: f[s][r["id"]]))
                  for s, rs in lab.items()}
        agree = {s: agreement(rs, f[s]) for s, rs in lab.items()}
        n_ax = {s: sum(f[s][r["id"]] for r in rs) for s, rs in lab.items()}
    carried, tickets = codex_carried(cx_raw)
    SRS._dump(OUT / "agy_vs_pi.json", {
        "source": {"pi": SRS.rel(D.ATTR), "agy": f"{REF_AGY}:{AGY_ATTR}",
                   "agy_sha256": agy_sha, "codex": f"{REF_CX}:{CX}", "codex_sha256": cx_sha},
        "chains": list(CHAINS), "by_task": cells, "retention_by_age": ret,
        "safety_share_by_boundary": share,
        "n_entries": {s: len(rs) for s, rs in lab.items()},
        "n_rule_safety": {s: sum(r["safe"] for r in rs) for s, rs in lab.items()},
        "rule_check_agy_vs_codex": chk, "rule": D.SAFE.pattern,
        "astra_labels": SRS.rel(FLAGS) if fl else None, "retention_by_age_astra": ret_ax,
        "n_astra_safety": n_ax, "keyword_vs_astra": agree,
        "bootstrap": {"seed": D.SEED, "n": D.BOOT, "unit": "chain"},
        "definition": D.RETENTION_DEF,
        "codex_carried": carried, "codex_tickets": tickets})
    plt, deep = D._style()
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    OUT.mkdir(parents=True, exist_ok=True)
    # by task
    colors = D.task_colors(deep)
    t1 = {s: [100 * cells[s][str(k)]["task_1"]["mean"] for k in range(1, K + 1)] for s in cells}
    title_bt = ("After tasks 1–4, task 1 is " + " → ".join(f"{v:.0f}%" for v in t1["pi"])
                + " of pi's summary and " + " → ".join(f"{v:.0f}%" for v in t1["agy"])
                + " of Antigravity's")
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.2), sharey=True)
    fig.subplots_adjust(left=0.06, right=0.985, top=0.84, bottom=0.2, wspace=0.06)
    nums_bt = []
    for ax, (s, label) in zip(axes, SIDES, strict=True):
        nums_bt += [f"{label}, {n}" for n in D.by_task_panel(ax, cells[s], colors)]
        ax.set_title(label, loc="left")
    axes[0].set_ylabel("Share of summary entries (%)")
    fig.suptitle(title_bt, x=0.02, y=0.975, ha="left", va="top")
    handles = [Patch(color=colors[f"task_{j}"], label=f"Task {j}") for j in range(1, K + 1)]
    handles.append(Patch(color=colors["generic"], label="No one task"))
    handles.append(Line2D([], [], marker="o", color="black", label="Task 1 (95% CI)"))
    fig.legend(handles=handles, loc="lower center", ncol=6, bbox_to_anchor=(0.5, 0.0))
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"by_task_agy_vs_pi.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    # safety-relevant against other: gpt-6-astra's labels (main), the keyword rule (backup)
    rk = r2(ret)
    title_kw = (f"Two tasks later, pi's summary has {rk['pi'][0]:.0f}% as many safety-keyword "
                f"entries about a task as right after it ({rk['pi'][1]:.0f}% for unflagged "
                f"ones); Antigravity's has {rk['agy'][0]:.0f}%")
    nums_kw = safety_figure(plt, deep, ret, title_kw, ("Flagged by the safety keyword rule",
                                                       "Not flagged"), "safety_agy_vs_pi_keyword")
    nums_ax, title_ax = [], None
    if ret_ax:
        ra = r2(ret_ax)
        title_ax = (f"Two tasks later, pi's summary has {ra['pi'][0]:.0f}% as many "
                    f"safety-relevant entries about a task as right after it ({ra['pi'][1]:.0f}% "
                    f"for the rest); Antigravity's has {ra['agy'][0]:.0f}%")
        nums_ax = safety_figure(plt, deep, ret_ax, title_ax,
                                ("Safety-relevant (gpt-6-astra)", "Other"), "safety_agy_vs_pi")
    # README
    n_e = {s: len(rs) for s, rs in lab.items()}
    n_s = {s: sum(r["safe"] for r in rs) for s, rs in lab.items()}
    ck = ([("**Rule against gpt-6-astra on agy's entries** (Codex, read-only, unvalidated; seeded "
           f"sample, seed {D.SAMPLE_SEED}, of {sum(chk['rule_pos'].values())} rule-positive and "
           f"{sum(chk['rule_neg'].values())} rule-negative entries): rule-positive "
           f"{chk['rule_pos']['codex_safe']} safety / {chk['rule_pos']['codex_other']} other "
           f"(precision {100 * chk['precision']:.0f}%); rule-negative "
           f"{chk['rule_neg']['codex_safe']} safety / {chk['rule_neg']['codex_other']} other "
           f"(negative agreement {100 * chk['npv']:.0f}%); estimated recall "
           f"{100 * chk['recall_est']:.0f}% (strata reweighted to the population; an estimate from "
           "100 entries under an earlier, narrower prompt, superseded by the every-entry "
           "comparison in the main section). Disagreements "
           "are in `agy_vs_pi.json`. pi's check against Opus (all 8 chains) is in "
           f"`{SRS.rel(D.OUT / 'safety.json')}`.")]
          if chk else ["**Rule check on agy:** Codex labels pending."])
    held = defaultdict(dict)
    for x in carried:
        held[x["chain"]][x["boundary"]] = x
    cx_rows = ["| chain | " + " | ".join(f"after task {k}" for k in range(1, K + 2)) + " |",
               "|---|" + "---|" * (K + 1)]
    for c in CHAINS:
        cx_rows.append(f"| {c} | " + " | ".join(
            (f"tasks {', '.join(map(str, held[c][k]['tasks_held']))} "
             f"({held[c][k]['carried_chars']:,} chars)") if k in held[c] else "–"
            for k in range(1, K + 2)) + " |")
    all_held = all(x["tasks_held"] == list(range(1, min(x["boundary"], K) + 1)) for x in carried)
    readme = [
        "# Summary decay by task, pi beside agy; Codex's carried requests (exploratory)", "",
        ("Generated by `scripts/week2_summary_decay_agy.py`. pi: "
         f"`{SRS.rel(D.ATTR)}` (u1–u4; shares match `shares.json`). agy: `{REF_AGY}:{AGY_ATTR}` "
         f"(sha256 {agy_sha[:16]}…). Codex: `{REF_CX}:{CX}` (sha256 {cx_sha[:16]}…). Every "
         "number is in `agy_vs_pi.json`. Same four chains and tasks on each side; model and "
         "harness differ together, so a difference is not attributable to either alone."), "",
        f"## by_task_agy_vs_pi: {title_bt}", "", "![by_task_agy_vs_pi](by_task_agy_vs_pi.png)",
        "",
        ("**Source:** `agy_vs_pi.json`, `by_task.<side>.<k>.task_<j>.mean`: task j's share of "
         "the boundary-k summary (summary_rot's `back_<k-j>` category; `current` when j = k), "
         f"chain means over u1–u4; 95% percentile bootstrap over chains ({D.BOOT:,} resamples, "
         f"seed {D.SEED}). Labels: summary_rot's coder (name matching, else Claude Opus 5.5) on "
         "both sides."), "",
        ("**Caption (slide notes):** agy's compaction summary is about the task just finished; "
         "the earlier tasks reach agy's next context only as the user's verbatim requests, kept "
         "outside the summary and not counted here. pi's summary carries a fading share of "
         "earlier tasks. n = 4 chains per side."), "",
        "**Numbers plotted:**", *[f"- {n}" for n in nums_bt], "",
        *([f"## safety_agy_vs_pi: {title_ax}", "", "![safety_agy_vs_pi](safety_agy_vs_pi.png)",
           "", (f"**Source:** `agy_vs_pi.json`, `retention_by_age_astra.<side>`. "
                f"{D.RETENTION_DEF.capitalize()}. Brackets: 95% percentile bootstrap over chains "
                f"({D.BOOT:,} resamples, seed {D.SEED}); none drawn where the interval is "
                "[0, 0]."), "",
           (f"**Coding:** every entry labelled by gpt-6-astra (`{SRS.rel(FLAGS)}`; "
            "scripts/week2_summary_safety_labels.py): pi's and agy's entries pooled, shuffled "
            "under opaque ids, harness, chain and boundary withheld; the definition is in "
            "flags.json (actions, permissions or rules bearing on harm to others' work, data or "
            "security: pushes and force-pushes, rewriting shared history, deleting branches or "
            "data, authorization, credentials, the user's rules on what the agent may do, such "
            "as not modifying tests). Unvalidated labeller, no hand check; in the reviewer's 15 "
            "random spot checks, 2 were borderline calls labelled safety-relevant (removing a "
            "temporary file; a rule on response format), so the class leans broad. "
            "Safety-relevant: pi "
            f"{n_ax['pi']} of {n_e['pi']} entries, agy {n_ax['agy']} of {n_e['agy']}."), "",
           ("**Keyword rule against gpt-6-astra, every entry** (rule precision and recall with "
            "astra as the reference): " + "; ".join(
                f"{s} agreement {100 * a['agreement']:.0f}%, kappa {a['kappa']:.2f}, rule "
                f"precision {100 * a['rule_precision']:.0f}%, recall "
                f"{100 * a['rule_recall']:.0f}% (both {a['both']}, rule only {a['rule_only']}, "
                f"astra only {a['astra_only']}, neither {a['neither']})"
                for s, a in agree.items()) + "."), "",
           "**Numbers plotted:**", *[f"- {n}" for n in nums_ax], ""] if ret_ax else []),
        f"## safety_agy_vs_pi_keyword (backup): {title_kw}", "",
        "![safety_agy_vs_pi_keyword](safety_agy_vs_pi_keyword.png)", "",
        (f"**Source:** `agy_vs_pi.json`, `retention_by_age.<side>`, the same definition with "
         "week2_summary_decay's keyword rule (pushes, the shared remote or a teammate's work on "
         "it, permission or the user's instructions, destructive git actions). Flagged by the "
         f"rule: pi {n_s['pi']} of {n_e['pi']} entries, agy {n_s['agy']} of {n_e['agy']}."), "",
        *ck, "", "**Numbers plotted:**", *[f"- {n}" for n in nums_kw], "",
        ("**Exploratory.** Entry counts, not content weight; retention above 0 is not "
         "one-to-one survival (entries merge and split). agy's 0% at every age follows from its "
         "summaries holding no earlier-task entries at all, so the safety/other split adds "
         "nothing for agy."), "",
        "## Codex: which earlier requests the carried history holds", "",
        ("Codex's compaction summary is encrypted (`summary` is empty in all "
         f"{len(carried)} compactions), so it cannot be coded and Codex has no panel above. Its "
         "readable replacement history (`carried_context`) holds the user's request messages "
         "plus the developer instructions. Below: which tasks' ticket ids it holds after each "
         "compaction (tickets from pi's code matches on the same chains; after task 5 is the "
         "end compaction, task 5's ticket not mapped)."), "", *cx_rows, "",
        ("Every compaction holds the ticket of every task so far: "
         f"{'yes' if all_held else 'no (see table)'} (ticket presence, checked by code; that the "
         "request messages are carried unchanged was seen by inspection, not checked by code). "
         "So Codex keeps the earlier requests themselves (as agy does outside its summary); what "
         "it keeps of the work done on them is in the encrypted summary and cannot be read."),
        ""]
    (OUT / "README.md").write_text("\n".join(readme) + "\n")
    print(title_bt)
    print(title_ax)
    print(title_kw)
    print("\n".join(nums_bt + nums_ax + nums_kw))
    print(agree)
    if chk:
        print({k: v for k, v in chk.items() if k != "disagreements"})
    print(f"codex: every task so far held: {all_held}")


if __name__ == "__main__":
    main()
