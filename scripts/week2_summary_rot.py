"""Week 2, experiment 4 ("chain stress"): summary rot. Which task each boundary summary's entries
are about, and whether the summary fills up with finished tasks as the chain goes on.

Method and definitions: `compaction.s2.chain.summary_rot` (module docstring). $0: the Opus pass
runs on Jacob's Max plan (`compaction.claude_headless.MaxBackend`, billing-route checks, a $0
usage row booked to SPRINT-S2-CHAIN-STRESS in this checkout's ledger).

Inputs (pulled, gitignored, read-only): data/raw/sprint/s2/summary_rot/state.json (chains, arms,
phases), chains/<chain>/compactions.jsonl (rows of kind "boundary" and "end") and
chains/<chain>/p<k>/compaction.json (field `summary`); configs/week2/wording/chain_stress/
teammate_texts.json (tags, repos, branches) and results/sprint/week2/chain_stress/token_lists/.

Subcommands (outputs under results/sprint/week2/chain_stress/summary_rot/):
- `attribute [--no-opus] [--max-calls 60] [--concurrency 3]`: splits every summary of the
  unrelated, related and within-task chains into entries, labels them by code, then by Opus
  (one call per summary, answers cached per summary under data/raw/sprint/s2/summary_rot/opus/,
  so a rerun makes no call twice; a malformed answer gets one retry, then its entries stay unlabelled).
  Writes attributions.jsonl (one row per entry) and attribute_meta.json.
- `shares`: attributions.jsonl -> shares.json (per summary, chain-weighted per arm and
  boundary, the seeded bootstrap CI over chains for the earlier-task share, code-Opus agreement,
  sensitivity variants, the plan's prediction).
- `sample`: a seeded random 30 boundary-summary entries (all arms) -> hand_check_sample.jsonl,
  blind (ids, text and the chain's task list; labels stay in attributions.jsonl).
- `figure`: shares.json only -> figures/summary_rot.{png,svg}, summary_rot_by_chain.{png,svg},
  figures/README.md.

Run: uv run python scripts/week2_summary_rot.py attribute && ... shares && ... sample && ... figure
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from compaction.s2.chain import chain_stress_score as CS
from compaction.s2.chain import summary_rot as SR

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "summary_rot"
OPUS_DIR = RAW / "opus"
OUT = ROOT / "results" / "sprint" / "week2" / "chain_stress" / "summary_rot"
FIG = OUT / "figures"
ATTR = OUT / "attributions.jsonl"
META = OUT / "attribute_meta.json"
SHARES = OUT / "shares.json"
SAMPLE = OUT / "hand_check_sample.jsonl"
ARMS = ("unrelated", "related", "within_task")  # bootstrap seeds go by index: append only
SAMPLE_ARMS = ("unrelated", "related")  # the 30 cards were drawn before within_task was added
LEDGER_EXP = "SPRINT-S2-CHAIN-STRESS"
PURPOSE = "week2-summary-rot"
BOOT_SEED = 20261028
TREND_SEED = 20261031
WITHIN_SEED = 20261101
SAMPLE_SEED = 20261029
SAMPLE_N = 30
PREDICTION = {"arm": "unrelated", "boundary": 4, "threshold": 0.30, "confidence": 0.65,
              "text": "Content about earlier tasks makes up >=30% of the summary at boundary 4, "
                      "averaged over chains (65%)."}


def rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(ROOT))


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def load_inputs():
    texts = CS.load_texts()
    tls = {t: json.loads((CS.TOKEN_DIR / f"{t}.json").read_text()) for t in texts}
    state = json.loads((RAW / "state.json").read_text())
    return texts, tls, state


# ------------------------------------------------------------------------------------------
# attribute
# ------------------------------------------------------------------------------------------


def summaries(state: dict) -> tuple[list[dict], list[dict]]:
    """(the pulled boundary and end summaries of the unrelated, related and within-task chains,
    in chain and boundary order; the expected ones that are missing)."""
    out, missing = [], []
    for cid in sorted(state["chains"]):
        ch = state["chains"][cid]
        if ch.get("arm") not in ARMS:
            continue
        tids = [p["task"] for p in ch["phases"]]
        log_p = RAW / "chains" / cid / "compactions.jsonl"
        rows = _jsonl(log_p) if log_p.exists() else []
        by_k = {r["after_phase"]: r for r in rows if r.get("kind") in ("boundary", "end")}
        for k in range(1, len(tids) + 1):
            kind = "end" if k == len(tids) else "boundary"
            p = RAW / "chains" / cid / f"p{k}" / "compaction.json"
            if not p.exists():
                if kind == "boundary":  # end summaries of 5-task chains weren't pulled
                    missing.append({"chain": cid, "boundary": k, "logged": k in by_k})
                continue
            logged = by_k.get(k, {}).get("kind")
            if logged is not None and logged != kind:
                raise SystemExit(f"{cid} p{k}: log kind {logged} != expected {kind}")
            d = json.loads(p.read_text())
            out.append({"chain": cid, "arm": ch["arm"], "kind": kind, "k": k,
                        "task_ids": tids[:k], "summary": d["summary"], "file": rel(p),
                        "update": d.get("update"), "summary_sha256": _sha(d["summary"])})
    return out, missing


def task_rows(task_ids, texts, tls) -> list[dict]:
    return [{"n": i, "task_id": t, "repo": texts[t]["repo"], "tag": texts[t]["tag"],
             "feature": texts[t]["feature"], "gist": SR.task_gist(t, texts, tls[t])}
            for i, t in enumerate(task_ids, 1)]


def backend(concurrency: int):
    from compaction.claude_headless import MaxBackend
    from emx import spend

    def rec(kind, resource, *, units, usd, note, exp=None):
        return spend.record(kind, resource, units, usd, note=f"week2 summary_rot {note}",
                            exp=LEDGER_EXP)

    return MaxBackend(model=SR.MODEL, cache_dir=RAW / "max_cache",
                      usage_path=RAW / "max_usage.jsonl", concurrency=concurrency,
                      name="opus-max", recorder=rec)


def opus_path(s: dict) -> Path:
    return OPUS_DIR / f"{s['chain']}_k{s['k']}.json"


def run_opus(items: list[dict], args) -> dict:
    """{summary key: {"labels": [...] or None, "error", "attempts"}}; cached per summary."""
    out, todo = {}, []
    for it in items:
        p = opus_path(it["s"])
        if p.exists():
            c = json.loads(p.read_text())
            if c.get("prompt_sha256") == _sha(it["req"].system + "\0" + it["req"].user) and (
                    c.get("labels") is not None or c.get("final")):
                out[it["key"]] = c
                continue
        todo.append(it)
    stats = {"calls_requested": 0, "runs": []}
    if not todo or args.no_opus:
        return {"answers": out, "pending": [t["key"] for t in todo], "stats": stats}
    be = backend(args.concurrency)
    for attempt in (1, 2):
        if not todo:
            break
        if stats["calls_requested"] + len(todo) + attempt > args.max_calls:  # + the canaries
            raise SystemExit(f"{len(todo)} more calls would pass --max-calls {args.max_calls}")
        reqs = []
        for it in todo:
            r = it["req"]
            if attempt == 2:
                r = dataclasses.replace(
                    r, key=r.key + ":retry",
                    user=r.user + f"\n\nYour previous answer was malformed ({it['err']}). "
                    f"Return exactly {len(it['entries'])} labels, i = 1..{len(it['entries'])}.")
            reqs.append(r)
        stats["calls_requested"] += len(reqs)
        resps = be.run(reqs, purpose=PURPOSE)
        stats["runs"].append(be.last_stats)
        nxt = []
        for it, r in zip(todo, reqs, strict=True):
            resp = resps[r.key]
            err = resp.error
            labels = None
            if err is None:
                try:
                    labels = SR.parse_opus(resp.data, len(it["entries"]), it["s"]["k"])
                except SR.Malformed as e:
                    err = f"malformed: {e}"
            rec = {"key": it["key"], "prompt_sha256": _sha(it["req"].system + "\0"
                                                           + it["req"].user),
                   "labels": labels, "error": err, "attempts": attempt,
                   "final": labels is not None or attempt == 2, "model": resp.model,
                   "raw": resp.data if labels is None else None}
            if labels is None and attempt == 1:
                it["err"] = err
                nxt.append(it)
            else:
                _dump(opus_path(it["s"]), rec)
                out[it["key"]] = rec
        todo = nxt
    return {"answers": out, "pending": [], "stats": stats}


def cmd_attribute(args) -> None:
    texts, tls, state = load_inputs()
    sums, missing = summaries(state)
    all_tags = {v["tag"] for v in texts.values()}
    items = []
    excl_total: Counter = Counter()
    for s in sums:
        entries, excl = SR.split_entries(s["summary"])
        excl_total.update(excl)
        tasks = task_rows(s["task_ids"], texts, tls)
        idents = SR.chain_identifiers(s["task_ids"], texts, tls)
        key = f"{s['chain']}:k{s['k']}"
        items.append({"s": s, "key": key, "entries": entries, "excluded": dict(excl),
                      "tasks": tasks, "idents": idents,
                      "req": SR.opus_req(f"summary_rot:{key}", key, tasks, entries)})
    res = run_opus(items, args)
    answers = res["answers"]
    rows, per_summary = [], []
    for it in items:
        s, ans = it["s"], answers.get(it["key"])
        labs = (ans or {}).get("labels")
        own_tags = {t["tag"] for t in it["tasks"]}
        for i, text in enumerate(it["entries"], 1):
            hits = SR.code_matches(text, it["idents"])
            code = f"task_{next(iter(hits))}" if len(hits) == 1 else None
            ol = labs[i - 1] if labs else None
            final = code if code is not None else (ol["label"] if ol else None)
            rows.append({
                "id": f"{s['chain']}:k{s['k']}:e{i:03d}", "chain": s["chain"], "arm": s["arm"],
                "kind": s["kind"], "boundary": s["k"], "entry": i, "text": text,
                "code_label": code, "code_matches": {str(j): v for j, v in hits.items()},
                "opus_label": ol["label"] if ol else None,
                "opus_tasks": ol["tasks"] if ol else None,
                "final_label": final,
                "decided_by": "code" if code is not None else ("opus" if ol else None),
                "category": SR.category(final, s["k"]),
                "foreign_tags": SR.foreign_tags(text, own_tags, all_tags)})
        heads = []
        for kind, line in SR.structure_lines(s["summary"]):
            if kind in ("heading", "bold_heading"):
                hits = SR.code_matches(line, it["idents"])
                if hits:
                    heads.append({"kind": kind, "text": line, "tasks": sorted(hits)})
        per_summary.append({"chain": s["chain"], "arm": s["arm"], "kind": s["kind"], "k": s["k"],
                            "file": s["file"], "summary_sha256": s["summary_sha256"],
                            "update": s["update"], "n_entries": len(it["entries"]),
                            "excluded": it["excluded"],
                            "excluded_headings_with_task_ids": heads,
                            "tasks": [{k: t[k] for k in ("n", "task_id", "repo", "tag")}
                                      for t in it["tasks"]],
                            "identifiers": {str(j): [f"{d.kind}:{d.text}" for d in v]
                                            for j, v in it["idents"].items()},
                            "opus": None if ans is None else {
                                "error": ans.get("error"), "attempts": ans.get("attempts"),
                                "model": ans.get("model")}})
    OUT.mkdir(parents=True, exist_ok=True)
    ATTR.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    meta = {"n_summaries": len(items), "n_entries": len(rows),
            "excluded_structure_lines": dict(excl_total),
            "n_excluded_structure_lines": sum(excl_total.values()),
            "missing_boundaries": missing, "opus_pending": res["pending"],
            "opus_failed_summaries": sorted(k for k, a in answers.items()
                                            if a.get("labels") is None),
            "n_unlabelled_entries": sum(r["final_label"] is None for r in rows),
            "opus_stats": res["stats"], "model": SR.MODEL, "ledger_exp": LEDGER_EXP,
            "summaries": per_summary}
    _dump(META, meta)
    print(f"{len(items)} summaries, {len(rows)} entries, "
          f"{sum(r['decided_by'] == 'code' for r in rows)} decided by code, "
          f"{meta['n_unlabelled_entries']} unlabelled; Opus pending {len(res['pending'])}, "
          f"calls requested {res['stats']['calls_requested']}; missing {missing}")


# ------------------------------------------------------------------------------------------
# shares
# ------------------------------------------------------------------------------------------


def _group(rows: list[dict]) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[(r["arm"], r["kind"], r["chain"], r["boundary"])].append(r)
    return g


def _cell(per: list[dict], seed: int) -> dict:
    cw = SR.chain_weighted([p["shares"] for p in per])
    lo, hi = SR.bootstrap_ci([p["shares"]["earlier"] for p in per
                              if p["shares"]["earlier"] is not None], seed)
    return {**cw, "earlier_ci95": [lo, hi], "bootstrap_seed": seed, "bootstrap_n": 10_000,
            "chains": [p["chain"] for p in per],
            "n_entries": sum(p["n_labelled"] for p in per),
            "earlier_by_chain": {p["chain"]: p["shares"]["earlier"] for p in per}}


def cmd_shares(args) -> None:
    rows = _jsonl(ATTR)
    meta = json.loads(META.read_text())
    if meta["opus_pending"]:
        raise SystemExit(f"Opus answers pending for {meta['opus_pending']}; run attribute")
    per_summary = []
    for (arm, kind, chain, k), rs in sorted(_group(rows).items()):
        rs = sorted(rs, key=lambda r: r["entry"])
        ss = SR.summary_shares([r["final_label"] for r in rs], k)
        opus_only = SR.summary_shares([r["opus_label"] for r in rs], k)["shares"]
        bullets = SR.summary_shares([r["final_label"] for r in rs if SR.is_bullet(r["text"])],
                                    k)["shares"]
        per_summary.append({"arm": arm, "kind": kind, "chain": chain, "boundary": k,
                            "n_entries": len(rs), **ss,
                            "shares_split_multi_task": SR.split_shares(rs, k),
                            "shares_opus_only": opus_only,
                            "shares_bullets_only": bullets,
                            "n_bullet_entries": sum(SR.is_bullet(r["text"]) for r in rs),
                            "decided_by_code": sum(r["decided_by"] == "code" for r in rs)})
    cells = {}
    for ai, arm in enumerate(ARMS):
        for kind in ("boundary", "end"):
            ks = sorted({p["boundary"] for p in per_summary
                         if p["arm"] == arm and p["kind"] == kind})
            for k in ks:
                per = [p for p in per_summary
                       if p["arm"] == arm and p["kind"] == kind and p["boundary"] == k]
                c = _cell(per, BOOT_SEED + 100 * ai + 10 * (kind == "end") + k)
                for variant in ("shares_split_multi_task", "shares_opus_only",
                                "shares_bullets_only"):
                    c[variant] = SR.chain_weighted([p[variant] for p in per])
                cells.setdefault(arm, {}).setdefault(kind, {})[str(k)] = c
    ub = cells["unrelated"]["boundary"]
    trend = {}
    for i, (a, b) in enumerate((("4", "2"), ("4", "3"))):
        trend[f"{a}-{b}"] = SR.paired_bootstrap_diff(ub[a]["earlier_by_chain"],
                                                     ub[b]["earlier_by_chain"],
                                                     TREND_SEED + i)
    # within-task chain w<i> runs unrelated chain u<i>'s task order: pair them by task list
    tasks = {(sm["chain"], sm["k"]): [t["task_id"] for t in sm["tasks"]]
             for sm in meta["summaries"]}
    wb = cells.get("within_task", {}).get("boundary", {})
    within = {}
    for i, k in enumerate(k for k in sorted(wb, key=int) if int(k) >= 2):  # no earlier at 1
        w_by = {c: v for c, v in wb[k]["earlier_by_chain"].items() if v is not None}
        u_by = {c: v for c, v in ub[k]["earlier_by_chain"].items() if v is not None}
        pairs = {}
        for wc in w_by:
            m = [uc for uc in u_by if tasks[(uc, int(k))] == tasks[(wc, int(k))]]
            if len(m) == 1:
                pairs[wc] = m[0]
        d = SR.paired_bootstrap_diff({wc: w_by[wc] for wc in pairs},
                                     {wc: u_by[uc] for wc, uc in pairs.items()}, WITHIN_SEED + i)
        within[k] = {**d, "pairs": pairs,
                     "within_mean": sum(w_by[c] for c in pairs) / len(pairs) if pairs else None,
                     "unrelated_mean": (sum(u_by[c] for c in pairs.values()) / len(pairs)
                                        if pairs else None)}
    heads = []
    for sm in meta["summaries"]:
        if sm["kind"] != "boundary":
            continue
        for h in sm.get("excluded_headings_with_task_ids") or []:
            heads.append({**h, "chain": sm["chain"], "arm": sm["arm"], "boundary": sm["k"],
                          "earlier": any(t < sm["k"] for t in h["tasks"])})
    n_head = sum(n for sm in meta["summaries"] if sm["kind"] == "boundary"
                 for kd, n in sm["excluded"].items() if kd in ("heading", "bold_heading"))
    coded = [r for r in rows if r["decided_by"] == "code" and r["opus_label"] is not None]
    agree = sum(r["code_label"] == r["opus_label"] for r in coded)
    conf = Counter((SR.category(r["code_label"], r["boundary"]),
                    SR.category(r["opus_label"], r["boundary"])) for r in coded
                   if r["code_label"] != r["opus_label"])
    labelled = [r for r in rows if r["final_label"] is not None]
    multi = [r for r in rows if r["decided_by"] == "opus" and len(r["opus_tasks"] or []) >= 2]
    pc = cells["unrelated"]["boundary"][str(PREDICTION["boundary"])]
    out = {
        "source": rel(ATTR),
        "definitions": {
            "unit": "a non-empty summary line, structure lines excluded "
                    "(compaction.s2.chain.summary_rot.split_entries)",
            "categories": {"current": "task k, the task just finished at boundary k",
                           "back_d": "task k-d", "earlier": "back_1 + back_2 + back_3",
                           "generic": "about no one task (harness rules, boilerplate)"},
            "final_label": "code label when decided, else Opus's",
            "weighting": "share per summary, then the mean over chains",
            "ci": "percentile bootstrap over chains, 10,000 resamples, seeded"},
        "n_summaries": len(per_summary), "n_entries": len(rows),
        "n_unlabelled": len(rows) - len(labelled),
        "decided_by": dict(Counter(r["decided_by"] for r in rows)),
        "share_decided_by_opus": sum(r["decided_by"] == "opus" for r in rows) / len(rows),
        "code_opus_agreement": {
            "n": len(coded), "agree": agree, "rate": agree / len(coded) if coded else None,
            "disagreements_by_category": {f"code {a} / opus {b}": n
                                          for (a, b), n in sorted(conf.items())}},
        "opus_multi_task_entries": len(multi),
        "foreign_tag_entries": [{"id": r["id"], "tags": r["foreign_tags"]}
                                for r in rows if r["foreign_tags"]],
        "excluded_structure_lines": meta["excluded_structure_lines"],
        "missing_boundaries": meta["missing_boundaries"],
        "cells": cells,
        "trend_unrelated_earlier": {
            "what": "paired difference over chains of the unrelated earlier-task share, "
                    "boundary a minus boundary b; percentile bootstrap over chains, seeded",
            **trend},
        "within_vs_unrelated_earlier": {
            "what": "earlier-task share, within-task chain minus the unrelated chain with the "
                    "same task order (w<i> vs u<i>), per boundary; paired percentile bootstrap "
                    "over pairs, seeded",
            **within},
        "excluded_headings": {
            "what": "boundary summaries' excluded headings and bold-only sub-headings that "
                    "name a task identifier (code matcher); not shown to Opus, not counted",
            "n_headings": n_head,
            "n_with_any_task_id": len(heads),
            "n_with_earlier_task_id": sum(h["earlier"] for h in heads),
            "by_arm_with_earlier": dict(Counter(h["arm"] for h in heads if h["earlier"])),
            "examples": [h for h in heads if h["earlier"]]},
        "per_summary": per_summary,
        "prediction": {**PREDICTION, "observed": pc["earlier"],
                       "ci95": pc["earlier_ci95"], "n_chains": pc["n_chains"],
                       "met": pc["earlier"] is not None
                       and pc["earlier"] >= PREDICTION["threshold"]},
    }
    _dump(SHARES, out)
    for arm in ARMS:
        for k, c in cells[arm]["boundary"].items():
            print(f"{arm} k={k} n={c['n_chains']}: "
                  + " ".join(f"{cat}={c[cat]:.3f}" for cat in (*SR.CATS, "earlier"))
                  + f" CI={c['earlier_ci95']}")
    print("prediction:", out["prediction"])


# ------------------------------------------------------------------------------------------
# sample
# ------------------------------------------------------------------------------------------


def cmd_sample(args) -> None:
    rows = [r for r in _jsonl(ATTR) if r["kind"] == "boundary" and r["arm"] in SAMPLE_ARMS]
    meta = json.loads(META.read_text())
    tasks = {(s["chain"], s["k"]): s["tasks"] for s in meta["summaries"]}
    rng = random.Random(SAMPLE_SEED)
    pick = sorted(rng.sample(range(len(rows)), SAMPLE_N))
    order = list(range(SAMPLE_N))
    rng.shuffle(order)
    out = []
    for n, oi in enumerate(order, 1):
        r = rows[pick[oi]]
        out.append({"card": n, "id": r["id"], "boundary": r["boundary"], "text": r["text"],
                    "tasks": tasks[(r["chain"], r["boundary"])], "sample_seed": SAMPLE_SEED,
                    "population": f"{len(rows)} boundary-summary entries, arms "
                                  + " and ".join(SAMPLE_ARMS)})
    SAMPLE.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    print(f"wrote {SAMPLE_N} cards to {rel(SAMPLE)}")


# ------------------------------------------------------------------------------------------
# figure
# ------------------------------------------------------------------------------------------

FIG_CATS = ("back_3", "back_2", "back_1", "current", "generic")  # bottom to top
CAT_LABEL = {"back_3": "3 tasks back", "back_2": "2 tasks back", "back_1": "1 task back",
             "current": "Task just finished", "generic": "No one task (harness rules, "
                                                         "boilerplate)"}


def _style():
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_theme(style="whitegrid")  # AGENTS.md "Figures": seaborn defaults only
    deep = sns.color_palette().as_hex()
    return plt, {"current": deep[0], "back_1": deep[1], "back_2": deep[2], "back_3": deep[3],
                 "generic": deep[7]}


def _stack(ax, x, shares: dict, colors: dict, width: float, min_label: float = 6) -> float:
    bottom = 0.0
    for cat in FIG_CATS:
        v = 100 * (shares.get(cat) or 0.0)
        if v > 0:
            ax.bar(x, v, width, bottom=bottom, color=colors[cat], zorder=2)
            if v >= min_label:
                ax.text(x, bottom + v / 2, f"{v:.0f}%", ha="center", va="center",
                        zorder=3)
        bottom += v
    return bottom


def _panel(ax, cells: dict, colors: dict, title: str, single_note: str = "") -> list[str]:
    ks = sorted(cells, key=int)
    nums = []
    for i, k in enumerate(ks):
        c = cells[k]
        top = _stack(ax, i, c, colors, 0.62)
        lo, hi = c["earlier_ci95"]
        e = 100 * c["earlier"]
        if c["n_chains"] > 1 and e > 0:
            ax.errorbar(i + 0.36, e, yerr=[[e - 100 * lo], [100 * hi - e]], fmt="o",
                        color="black", zorder=4)
        ax.annotate(f"n = {c['n_chains']} chain{'s' if c['n_chains'] != 1 else ''}",
                    (i, top), xytext=(0, 4), textcoords="offset points", ha="center",
                    va="bottom")
        ci = (f"[95% CI {100 * lo:.1f}, {100 * hi:.1f}]" if c["n_chains"] > 1
              else f"one chain{single_note}; interval unavailable")
        nums.append(f"{title}, boundary {k} (n = {c['n_chains']}): "
                    + "; ".join(f"{CAT_LABEL[cat]} {100 * c[cat]:.1f}%" for cat in FIG_CATS[::-1])
                    + f"; earlier tasks total {e:.1f}%; {ci}")
    ax.set_xticks(range(len(ks)))
    ax.set_xticklabels([f"After task {k}" for k in ks])
    ax.set_xlim(-0.6, len(ks) - 0.4)
    ax.set_ylim(0, 114)
    ax.set_yticks(range(0, 101, 20))
    ax.tick_params(axis="both", length=0)
    ax.set_title(title, loc="left", pad=8)
    return nums


def cmd_figure(args) -> None:
    sh = json.loads(SHARES.read_text())
    plt, colors = _style()
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    FIG.mkdir(parents=True, exist_ok=True)
    un, re_ = sh["cells"]["unrelated"]["boundary"], sh["cells"]["related"]["boundary"]
    wi = sh["cells"]["within_task"]["boundary"]
    p = sh["prediction"]
    k4 = un[str(p["boundary"])]
    later = [100 * un[k]["earlier"] for k in sorted(un, key=int) if int(k) >= 2]
    tr = sh["trend_unrelated_earlier"]["4-2"]
    # chain means, not every summary: single chains range wider (astra r2)
    title = (f"Compacted only between tasks, unrelated chains' summaries are on average "
             f"{min(later):.0f}–{max(later):.0f}% about earlier tasks after tasks 2–4")
    if tr["excludes_zero"]:  # claim a trend only when the paired interval excludes 0
        title += (", and the share " + ("grows" if tr["diff"] > 0 else "shrinks")
                  + " from task 2 to task 4")
    fig, axes = plt.subplots(1, 3, figsize=(16, 6.2), width_ratios=[4, 3, 4], sharey=True)
    fig.subplots_adjust(left=0.055, right=0.985, top=0.84, bottom=0.17, wspace=0.08)
    nums = _panel(axes[0], un, colors, "Five unrelated projects, compacted between tasks")
    nums += _panel(axes[1], re_, colors, "Several tasks in one repository",
                   " (the only four-task repository chain)")
    nums += _panel(axes[2], wi, colors, "Unrelated, also compacted inside tasks")
    axes[0].set_ylabel("Share of summary entries (%)")
    fig.suptitle(title, x=0.02, y=0.975, ha="left", va="top")
    lo, hi = k4["earlier_ci95"]
    th = 100 * p["threshold"]
    if p["met"]:
        verdict = f"the mean reaches {th:.0f}%"
    elif 100 * hi >= th:
        verdict = f"the mean is below {th:.0f}% but the interval reaches it"
    else:
        verdict = f"the whole interval is below {th:.0f}%"
    # no captions inside the image (Jacob, 29 Oct): the prediction line and the footnote go to
    # the README
    pred_line = (f"The plan predicted at least {th:.0f}% about earlier tasks after task 4; "
                 f"observed mean {100 * k4['earlier']:.0f}% [{100 * lo:.0f}, {100 * hi:.0f}], "
                 f"so {verdict}.")
    handles = [Patch(color=colors[c], label=CAT_LABEL[c]) for c in FIG_CATS[::-1]]
    handles.append(Line2D([], [], marker="o", color="black",
                          label="Earlier tasks total (95% CI)"))
    fig.legend(handles=handles, loc="lower center", ncol=6,
               bbox_to_anchor=(0.5, 0.0))
    un_ch = sorted({c for v in un.values() for c in v["chains"]})
    re_ch = sorted({c for v in re_.values() for c in v["chains"]})
    wi_ch = sorted({c for v in wi.values() for c in v["chains"]})
    foot = (f"Means over chains (each chain weighs the same); black whisker: 95% bootstrap "
            f"interval over chains for all earlier tasks together. Entries labelled by name "
            f"matching, else Claude Opus 5.5. Chains: {', '.join(un_ch)}; {', '.join(re_ch)}; "
            f"{', '.join(wi_ch)} (the task orders of {', '.join('u' + c[1:] for c in wi_ch)}, "
            f"compacted at 169k tokens inside tasks as well; w1 ended after task 2).")
    for ext in ("png", "svg"):
        fig.savefig(FIG / f"summary_rot.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    readme = ["# Summary rot (week 2, experiment 4: five-task chains)", "",
              ("Generated by `scripts/week2_summary_rot.py figure` from "
               f"`{rel(SHARES)}` only; every number is read from that file."), "",
              f"## summary_rot: {title.replace(chr(10), ' ')}", "", "![summary_rot](summary_rot.png)", "",
              (f"**Source:** `{rel(SHARES)}`, fields `cells.unrelated.boundary.<k>`, "
               "`cells.related.boundary.<k>` and `cells.within_task.boundary.<k>` (`back_3`, `back_2`, `back_1`, `current`, "
               "`generic`, `earlier`, `earlier_ci95`, `n_chains`); the prediction line from "
               "`prediction`."), "",
              f"**Prediction (slide notes, not on the figure):** {pred_line}", "",
              f"**Footnote (not on the figure):** {foot}", "",
              "**Numbers plotted:**"] + [f"- {n}" for n in nums]
    nums2, title2 = _by_chain(plt, colors, sh)
    readme += ["", f"## summary_rot_by_chain: {title2}", "",
               "![summary_rot_by_chain](summary_rot_by_chain.png)", "",
               (f"**Source:** `{rel(SHARES)}`, field `per_summary[]` (arm unrelated, kind "
                "boundary: `shares`)."), "", "**Numbers plotted:**"] + [f"- {n}" for n in nums2]
    eh = sh["excluded_headings"]
    bo = {k: 100 * c["shares_bullets_only"]["earlier"] for k, c in un.items()}
    tdesc = "; ".join(f"after task {a} minus after task {b}: {100 * t['diff']:+.1f} points "
                      f"[{100 * t['ci95'][0]:+.1f}, {100 * t['ci95'][1]:+.1f}]"
                      for (a, b), t in ((key.split("-"), v) for key, v in
                                        sh["trend_unrelated_earlier"].items() if key != "what"))
    wv = {k: v for k, v in sh["within_vs_unrelated_earlier"].items() if k != "what"}
    wdesc = "; ".join(f"after task {k}: {100 * v['within_mean']:.1f}% vs "
                      f"{100 * v['unrelated_mean']:.1f}%, difference {100 * v['diff']:+.1f} "
                      f"points [{100 * v['ci95'][0]:+.1f}, {100 * v['ci95'][1]:+.1f}] over "
                      f"{v['n_chains']} pairs"
                      for k, v in sorted(wv.items(), key=lambda kv: int(kv[0])))
    readme += ["", ("**Within-task arm (right panel) against the unrelated chains with the same "
                    "task order** (w<i> vs u<i>; `within_vs_unrelated_earlier` in shares.json; "
                    f"earlier-task share, within-task vs unrelated): {wdesc}. Three pairs, so "
                    "the bootstrap intervals are coarse; no difference is claimed. w1's boundary "
                    "2 compaction failed (twice) and the chain ended after task 2, so it adds "
                    "only the after-task-1 bar. The arm also compacted inside tasks once the "
                    "context reached 169k tokens, so its boundary summaries were written from "
                    "contexts already compacted inside the task, except w3 after task 4 (no "
                    "in-task compaction fired in that task). The comparison is between the two "
                    "compaction regimes, not an isolated effect on the boundary summary; the "
                    "in-task summaries themselves are not counted here.")]
    readme += ["", ("**Trend (unrelated arm, earlier-task share, paired bootstrap over the 8 "
                    f"chains):** {tdesc}. The title claims a trend only if the task 4 minus "
                    "task 2 interval excludes 0."),
               "", ("**Unit: lines, not just bullets.** The plan's denominator is summary "
                    "bullets; here every non-empty line counts (bullets, numbered items, prose "
                    "and continuation lines). Bullet-only sensitivity (entries starting with "
                    "-, *, + or a number; `cells.<arm>.boundary.<k>.shares_bullets_only`), "
                    "unrelated earlier-task share: "
                    + ", ".join(f"after task {k} {v:.1f}%" for k, v in sorted(bo.items()))
                    + "."),
               "", ("**Limitation: headings removed from Opus's context.** Headings and bold "
                    "sub-headings are excluded as structure and were not shown to Opus, so a "
                    "sub-bullet under a heading such as `**KCP-256 (complete):**` lost that "
                    f"parent. Of {eh['n_headings']} excluded headings in boundary summaries, "
                    f"{eh['n_with_any_task_id']} name a task identifier and "
                    f"{eh['n_with_earlier_task_id']} name an earlier task's identifier "
                    "(`excluded_headings` in shares.json). Earlier-task content may be "
                    "undercounted where Opus labelled such sub-bullets generic or current."),
               "", ("**Limitation: Opus prompt wording.** The Opus prompt says the agent worked "
                    "through \"several unrelated coding tasks\", including for the related arm, "
                    "whose tasks share one repository. The related-arm labels were not rerun."),
               "", ("**Related arm, after task 3:** one chain (koota), the only four-task "
                    "repository chain; not comparable to the four-chain bars and no interval."),
               "", ("**End summaries** (after a chain's last task) are not plotted. Those of "
                    "the five-task chains were not pulled, including w3's and w4's, which "
                    "were dropped from the inputs after a first pull (their Opus answers stay "
                    "cached, unused).")]
    readme += ["", ("**Notes:** an entry is one non-empty summary line (headings, separators and "
               "bold-only sub-headings excluded). Label: the one task whose ticket tag, repo, "
               "package, branch or task id the line names, else Claude Opus 5.5's call (Max "
               "plan). In the related arm the repository is shared, so only tags, branches and "
               f"Opus decide. Code-Opus agreement on code-decided entries: "
               f"{sh['code_opus_agreement']['agree']}/{sh['code_opus_agreement']['n']}. "
               f"Share decided by Opus: {100 * sh['share_decided_by_opus']:.1f}%. Not "
               "hand-checked yet (30-card sample in hand_check_sample.jsonl).")]
    (FIG / "README.md").write_text("\n".join(readme) + "\n")
    print(f"wrote {rel(FIG)}")


def _by_chain(plt, colors, sh) -> tuple[list[str], str]:
    from matplotlib.patches import Patch

    per = [p for p in sh["per_summary"] if p["arm"] == "unrelated" and p["kind"] == "boundary"]
    chains = sorted({p["chain"] for p in per}, key=lambda c: (len(c), c))
    k4 = [p["shares"]["earlier"] for p in per if p["boundary"] == 4]
    title = (f"After task 4, each chain's summary is {100 * min(k4):.0f}–{100 * max(k4):.0f}% "
             f"about earlier tasks")
    fig, axes = plt.subplots(2, 4, figsize=(12, 6.6), sharey=True)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.86, bottom=0.15, hspace=0.42, wspace=0.08)
    nums = []
    for n, (ax, ch) in enumerate(zip(axes.flat, chains, strict=True), 1):
        ps = sorted((p for p in per if p["chain"] == ch), key=lambda p: p["boundary"])
        for i, p in enumerate(ps):
            _stack(ax, i, p["shares"], colors, 0.7, min_label=10)
            nums.append(f"chain {n} ({ch}), after task {p['boundary']}: "
                        + "; ".join(f"{CAT_LABEL[c]} {100 * (p['shares'][c] or 0):.1f}%"
                                    for c in FIG_CATS[::-1])
                        + f"; {p['n_labelled']} entries")
        ax.set_xticks(range(len(ps)))
        ax.set_xticklabels([str(p["boundary"]) for p in ps])
        ax.set_ylim(0, 100)
        ax.tick_params(axis="both", length=0)
        ax.set_title(f"Chain {n}", loc="left")
        if n > 4:
            ax.set_xlabel("After task")
    axes[0][0].set_ylabel("Summary entries (%)")
    axes[1][0].set_ylabel("Summary entries (%)")
    fig.suptitle(title, x=0.02, y=0.975, ha="left", va="top")
    handles = [Patch(color=colors[c], label=CAT_LABEL[c]) for c in FIG_CATS[::-1]]
    fig.legend(handles=handles, loc="lower center", ncol=5,
               bbox_to_anchor=(0.5, 0.0))
    for ext in ("png", "svg"):
        fig.savefig(FIG / f"summary_rot_by_chain.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)
    nums.append(f"(not on the figure) Five unrelated projects per chain. Chains 1–8 = "
                f"{', '.join(chains)}.")
    return nums, title


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("attribute")
    a.add_argument("--no-opus", action="store_true", help="code pass only; no model call")
    a.add_argument("--max-calls", type=int, default=60)
    a.add_argument("--concurrency", type=int, default=3)
    sub.add_parser("shares")
    sub.add_parser("sample")
    sub.add_parser("figure")
    args = ap.parse_args(argv)
    if getattr(args, "concurrency", 1) > 3:
        sys.exit("at most 3 Opus calls in flight")
    {"attribute": cmd_attribute, "shares": cmd_shares, "sample": cmd_sample,
     "figure": cmd_figure}[args.cmd](args)


if __name__ == "__main__":
    main()
