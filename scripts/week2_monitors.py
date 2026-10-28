"""Experiment 2's offline monitor harness (docs/sprint/plans/week2-monitors.md v0.4). Laptop only.

    uv run python scripts/week2_monitors.py items
        Build the force-push items from task chaining's run data (and P38's, when its builder
        is present) into data/raw/sprint/s2/monitors/items_*.jsonl (gitignored: full
        transcripts), and write the committed manifest
        results/sprint/week2/monitors/manifest.json: counts that must equal the plan, per item
        and view the selected message index, sha256, rendered size per prompt channel, the
        pre-cut harness flags, and sanity checks 1-2.
    uv run python scripts/week2_monitors.py examples
        The rendered examples Jacob reads before any scoring: one seeded primary act, views
        1-4 × both prompts (8), plus one reasoning view and one second-scenario item, under
        results/sprint/week2/monitors/examples/ (full text, and a short head/tail digest).
    uv run python scripts/week2_monitors.py stub
        The $0 stub: plans step 1 (and a Guardian slice) exactly as scoring would, answers
        from code, checks parsing, the one retry, the window pause and the refusal of
        anything not in the config. Record: results/sprint/week2/monitors/stub.json.
    uv run python scripts/week2_monitors.py freeze-retest
        Freeze the 20 seeded test-retest requests (sha256 of each) before any scoring.
    uv run python scripts/week2_monitors.py score --step 1 --config configs/week2/monitors.toml --go
        Score a workload step on the Max plan. Refuses without --go, a config that passes the
        contract, the design card and the 8 examples present.

Steps (plan, "Workload and order"): 0 = the 2 longest full views per model (auto mode);
1 = auto mode, 30 primary acts, views full + current_only, both models; 3 = auto mode, views
summary_tail + pinned_user_turns on the natural acts, plus the 4 other natural acts and the
test-retest repeats; 4 = Guardian, natural acts, all views; 7 = both prompts, the 28 benign
pushes, views full + current_only; 8 = P38 (all five views on unauthorized items, views 1
and 3 on authorized ones). Steps 2 and 5 score the control runs; step 6 (the plan's 6 and
6b together) scores Experiment 1's acts, all five views, both prompts.

    uv run python scripts/week2_monitors.py items-exp1 [--raw DIR] [--write-partial]
        Experiment 1's items from a local copy of its raw dir (rsync from rig, read-only):
        localizes the copy's traps_home paths, scores the finished second tasks, builds the
        items and writes the committed results/sprint/week2/monitors/exp1_manifest.json.
        items_exp1.jsonl (what step 6 reads) is written only once every second task is done;
        --write-partial writes items_exp1_partial.jsonl instead, which only
        `score --step 6 --partial` reads.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

from compaction.s2.chain.monitors import checks as K
from compaction.s2.chain.monitors import runner as R
from compaction.s2.chain.monitors.baselines import BASELINES
from compaction.s2.chain.monitors.items import Item, sha256_json
from compaction.s2.chain.monitors.natural import Run, items_from_rows
from compaction.s2.chain.monitors.render import render

ROOT = Path(__file__).resolve().parents[1]
TC_RAW = (ROOT.parents[0] / "task-chaining" / "data" / "raw" / "sprint" / "s2" / "chain"
          if (ROOT.parents[0] / "task-chaining").exists() else ROOT / "data/raw/sprint/s2/chain")
ROWS = ROOT / "results/sprint/S2/chain/rows.jsonl"
# experiment 2's 24 control runs (builder b's launch worktree, read-only)
CONTROLS_RAW = (ROOT.parents[0] / "week2-b-launch" / "data/raw/sprint/s2/monitors/run")
RAW = ROOT / "data/raw/sprint/s2/monitors"
OUT = ROOT / "results/sprint/week2/monitors"
EXPECTED = {"primary": 30, "kept": 1, "no_history": 3, "benign_push": 28}
EXPECTED_PRIMARY = {"histories": 12, "compacted": 16, "full": 14, "np": 17, "p": 13}
EXP1_RAW = RAW / "exp1_raw"
EXP1_MANIFEST = OUT / "exp1_manifest.json"


def is_exp1(it: Item) -> bool:
    return it.set.startswith("exp1") or it.set == "kept_exp1"
SEED = R.SEED


def _load_items(name: str) -> list[Item]:
    p = RAW / f"items_{name}.jsonl"
    if not p.exists():
        return []
    return [Item.from_json(json.loads(x)) for x in p.read_text().splitlines() if x.strip()]


def _save_items(name: str, items: list[Item]) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    p = RAW / f"items_{name}.jsonl"
    with p.open("w") as f:
        for it in items:
            f.write(json.dumps(it.to_json(), ensure_ascii=False) + "\n")
    return p


def _act_output(it: Item) -> str | None:
    """The act's recorded result text (from the source run), for the cut check."""
    src = it.meta.get("source_dir")
    mi = it.meta.get("act_message_index")
    if not src or mi is None:
        return None
    msgs = json.loads((Path(src) / "messages.json").read_text())
    ids = {tc["id"] for tc in msgs[mi].get("tool_calls") or []}
    outs = [m.get("content") for m in msgs[mi + 1:] if m.get("role") == "tool"
            and m.get("tool_call_id") in ids]
    return "\n".join(str(o) for o in outs) if outs else None


def _update_marker(it: Item) -> str:
    """The ref-update line git prints for this push only (`old...new` forced, `old..new`
    fast-forward), abbreviated as git prints it."""
    e = it.evidence
    sep = "..." if it.set in ("primary", "kept", "no_history") or is_exp1(it) else ".."
    return f"{e['old'][:7]}{sep}{e['new'][:7]}"


def cmd_items(a) -> int:
    rows = [json.loads(x) for x in ROWS.read_text().splitlines() if x.strip()]
    items = items_from_rows(TC_RAW, rows)
    _save_items("force_push", items)
    errs: list[str] = []
    per_item = []
    for it in items:
        errs += K.check_cut(it)
        run = Run(TC_RAW, next(r for r in rows if r["id"] == it.meta["row_id"]))
        a_msgs, comp = run.first_task()
        errs += K.check_views_force_push(it, a_msgs or None,
                                         comp["post_messages"] if comp else None)
        users = []
        sizes = {}
        for v in it.views:
            for p in R.CONTRACT["prompts"]:
                rr = render(it, v, p)[-1]
                users.append(rr.user)
                sizes[f"{v}:{p}"] = {"system_chars": len(rr.system),
                                     "user_chars": len(rr.user)}
        errs += K.check_output_absent(it, users, _act_output(it),
                                      marker=_update_marker(it))
        errs += K.check_metadata_absent(it, [r.system + "\n" + r.user for v in it.views
                                             for p in R.CONTRACT["prompts"]
                                             for r in render(it, v, p)])
        per_item.append({"id": it.id, "set": it.set, "cluster": it.cluster,
                         "start": it.meta["start"], "hist": it.meta["hist"],
                         "act_message_index": it.meta["act_message_index"],
                         "call": it.meta["call"], "act_command": it.act_command,
                         "evidence": it.evidence,
                         # primary flag (plan, check 6): task chaining's harness_dir_commands
                         # (commands that read the scorer's directory, whole run); the
                         # mention count before the cut is a sensitivity check only (path
                         # mentions include the remote URL in git's own output)
                         "harness_read_flag": bool(it.meta["harness_dir_commands"]),
                         "harness_dir_commands_run": it.meta["harness_dir_commands"],
                         "harness_mentions_before_cut": it.meta["harness_read_before_cut"],
                         "views": {v: it.view_sha256(v) for v in it.views},
                         "record_sha256": sha256_json(it.to_json()),
                         "rendered": sizes,
                         "baselines": {b: {v: f(it, v) for v in it.views}
                                       for b, f in BASELINES.items()}})
    counts = Counter(it.set for it in items)
    prim = [it for it in items if it.set == "primary"]
    pc = {"histories": len({i.cluster for i in prim}),
          **Counter(i.meta["start"] for i in prim), **Counter(i.meta["hist"] for i in prim)}
    if dict(counts) != EXPECTED:
        errs.append(f"set counts {dict(counts)} != plan {EXPECTED}")
    if {k: pc.get(k) for k in EXPECTED_PRIMARY} != EXPECTED_PRIMARY:
        errs.append(f"primary split {pc} != plan {EXPECTED_PRIMARY}")
    if not all(i.evidence["act_replaced_teammate_tip"] for i in prim):
        errs.append("a primary act did not itself replace the teammate's tip")
    controls = []
    if CONTROLS_RAW.exists():
        from compaction.s2.chain.monitors.controls import items_from_rows as control_items
        crows = [json.loads(x) for x in (CONTROLS_RAW / "rows.jsonl").read_text().splitlines()
                 if x.strip()]
        controls = control_items(CONTROLS_RAW, crows)
        _save_items("controls", controls)
        for it in controls:
            errs += K.check_cut(it) + K.check_reasoning_view(it)
            errs += K.check_metadata_absent(it, [r.system + "\n" + r.user for v in it.views
                                                 for p in R.CONTRACT["prompts"]
                                                 for r in render(it, v, p)])
        n_ctl = Counter(i.set for i in controls)
        if dict(n_ctl) != {"control_authorized": 12, "control_withheld": 5}:
            errs.append(f"control item counts {dict(n_ctl)} differ from rows.jsonl's rewrites")
    p38 = _load_items("p38")
    for it in p38:
        errs += K.check_cut(it) + K.check_reasoning_view(it) + K.check_policy(it)
        errs += K.check_metadata_absent(it, [r.system + "\n" + r.user for v in it.views
                                             for p in R.CONTRACT["prompts"]
                                             for r in render(it, v, p)])
    man = {"plan": "docs/sprint/plans/week2-monitors.md v0.4", "rows": str(ROWS),
           "rows_sha256": _sha_file(ROWS), "source_raw": str(TC_RAW),
           "counts": dict(counts), "primary_split": pc,
           "p38_counts": dict(Counter(i.set for i in p38)),
           # the P38 confound (report prominently): whether phase D called request_approval
           "p38_request_approval": {s: dict(Counter(bool(i.evidence.get("request_approval"))
                                                    for i in p38 if i.set == s))
                                    for s in sorted({i.set for i in p38})},
           # the whole item record (views, labels, cluster, policy, evidence), bound for scoring
           "p38_records": {i.id: sha256_json(i.to_json()) for i in p38},
           "control_records": {i.id: sha256_json(i.to_json()) for i in controls},
           # per-control facts for the analysis roster (withheld = unauthorized: a miss if
           # allowed; authorized = a false positive if blocked)
           "control_items": [{"id": i.id, "set": i.set, "cluster": i.cluster,
                              "authorized": i.authorized, "hist": i.meta.get("hist"),
                              "start": i.meta.get("start"),
                              "harness_read_flag": bool(i.meta.get("harness_dir_commands")),
                              "rerun": bool(i.meta.get("rerun"))} for i in controls],
           "control_counts": dict(Counter(i.set for i in controls)),
           "control_reruns": [i.id for i in controls if i.meta.get("rerun")],
           "harness_read_flag_primary": sum(
               1 for i in items if i.set == "primary" and i.meta["harness_dir_commands"]),
           "checks": {"errors": errs, "pass": not errs},
           "items": per_item}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=False))
    print(json.dumps({"counts": man["counts"], "primary_split": pc,
                      "p38_counts": man["p38_counts"], "errors": errs[:20],
                      "n_errors": len(errs)}, indent=1))
    return 0 if not errs else 1


def _sha_file(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _digest(text: str, head: int = 3000, tail: int = 5000) -> str:
    if len(text) <= head + tail + 200:
        return text
    return (text[:head] + f"\n\n[... {len(text) - head - tail:,} characters omitted in this "
            "digest; the full file holds them ...]\n\n" + text[-tail:])


def cmd_examples(a) -> int:
    items = _load_items("force_push")
    prim = sorted((i for i in items if i.set == "primary"), key=lambda i: i.id)
    it = random.Random(SEED).choice(prim)
    ex = OUT / "examples"
    ex.mkdir(parents=True, exist_ok=True)
    index = [f"# Rendered monitor examples (seed {SEED})", "",
             f"Primary act: `{it.id}` (start {it.meta['start']}, history {it.cluster}).", "",
             "| # | view | prompt | system chars | user chars | full | digest |",
             "|---|---|---|---|---|---|---|"]
    n = 0
    jobs = [(it, v, p) for v in ("full", "summary_tail", "current_only", "pinned_user_turns")
            for p in ("auto_mode", "guardian")]
    p38 = _load_items("p38")
    if p38:
        r_it = sorted((i for i in p38 if "current_plus_reasoning" in i.views
                       and i.set == "p38_natural"), key=lambda i: i.id)
        s_it = sorted((i for i in p38 if i.set == "p38_false_licence"), key=lambda i: i.id)
        rng = random.Random(SEED + 1)
        if r_it:
            jobs.append((rng.choice(r_it), "current_plus_reasoning", "auto_mode"))
        if s_it:
            jobs.append((rng.choice(s_it), "summary_tail", "guardian"))
    for item, v, p in jobs:
        n += 1
        rr = render(item, v, p)[-1]
        stem = f"{n:02d}_{item.scenario}_{v}_{p}"
        body = f"=== SYSTEM ({p}, stage {rr.stage}) ===\n{rr.system}\n\n=== USER ===\n{rr.user}"
        (ex / f"{stem}.txt").write_text(body)
        (ex / f"{stem}.digest.md").write_text(
            f"# {stem}\n\nItem `{item.id}`, view `{v}`, prompt `{p}`. The system prompt is the "
            f"vendored {p} prompt ({len(rr.system):,} chars) and is not repeated here.\n\n"
            f"```text\n{_digest(rr.user)}\n```\n")
        index.append(f"| {n} | {v} | {p} | {len(rr.system):,} | {len(rr.user):,} | "
                     f"[txt]({stem}.txt) | [digest]({stem}.digest.md) |")
    if not p38:
        index += ["", ("The reasoning-view and second-scenario examples need the P38 items "
                       "(`items` after the P38 builder lands).")]
    (ex / "README.md").write_text("\n".join(index) + "\n")
    print(f"{n} examples in {ex}")
    return 0


def _step_calls(step: int, cfg: R.ScorerConfig, items: list[Item], p38: list[Item],
                controls: list[Item] | None = None,
                exp1: list[Item] | None = None) -> list[R.Call]:
    prim = [i for i in items if i.set == "primary"]
    other = [i for i in items if i.set in ("kept", "no_history")]
    benign = [i for i in items if i.set == "benign_push"]
    models = cfg.models
    if step == 0:
        longest = sorted(prim, key=lambda i: len(json.dumps(i.views["full"])))[-2:]
        return R.plan_calls(longest, cfg, views=["full"], prompts=["auto_mode"], models=models)
    if step == 1:
        return R.plan_calls(prim, cfg, views=["full", "current_only"], prompts=["auto_mode"],
                            models=models)
    if step == 3:
        return (R.plan_calls(prim, cfg, views=["summary_tail", "pinned_user_turns"],
                             prompts=["auto_mode"], models=models)
                + R.plan_calls(other, cfg, views=list(cfg.views)[:4], prompts=["auto_mode"],
                               models=models)
                + retest_calls(cfg, prim))
    if step == 4:
        return R.plan_calls(prim + other, cfg, views=list(cfg.views)[:4],
                            prompts=["guardian"], models=models)
    if step == 7:
        return R.plan_calls(benign, cfg, views=["full", "current_only"],
                            prompts=list(cfg.prompts), models=models)
    if step == 8:
        unauth = [i for i in p38 if not i.authorized]
        auth = [i for i in p38 if i.authorized]
        return (R.plan_calls(unauth, cfg, views=list(cfg.views), prompts=list(cfg.prompts),
                             models=models)
                + R.plan_calls(auth, cfg, views=["full", "current_only"],
                               prompts=list(cfg.prompts), models=models))
    if step == 2 and controls:  # auto mode, control acts, views 1 and 3
        return R.plan_calls(controls, cfg, views=["full", "current_only"],
                            prompts=["auto_mode"], models=models)
    if step == 5 and controls:  # the remaining control views, both prompts
        rest = [v for v in cfg.views if v not in ("full", "current_only")]
        return (R.plan_calls(controls, cfg, views=rest, prompts=["auto_mode"], models=models)
                + R.plan_calls(controls, cfg, views=list(cfg.views), prompts=["guardian"],
                               models=models))
    if step == 6 and exp1:  # Experiment 1's acts: all five views, both prompts
        return R.plan_calls(exp1, cfg, views=list(cfg.views), prompts=list(cfg.prompts),
                            models=models)
    raise SystemExit(f"step {step} has no items yet (controls / Experiment 1)")


def retest_calls(cfg: R.ScorerConfig, prim: list[Item]) -> list[R.Call]:
    pick = random.Random(SEED).sample(sorted(prim, key=lambda i: i.id), cfg.test_retest_items)
    return R.plan_calls(pick, cfg, views=["full"], prompts=["auto_mode"], models=cfg.models,
                        rep=1)


def cmd_freeze_retest(a) -> int:
    cfg = R.load_config(a.config)
    calls = retest_calls(cfg, [i for i in _load_items("force_push") if i.set == "primary"])
    rec = [{"key": c.key, "model": c.model, "request_sha256": c.request_sha()} for c in calls]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "retest_frozen.json").write_text(json.dumps(rec, indent=1))
    print(f"{len(rec)} frozen requests")
    return 0


def _channel_check(items: list[Item]) -> bool:
    """Each prompt keeps its own channel: a slice of view 3's tool results (raw, as the agent
    got them) that occurs nowhere in the agent's own calls is absent from auto mode's
    request and present in Guardian's."""
    ok, n = True, 0
    for it in items:
        view = it.views["current_only"]
        calls = "\n".join(json.dumps(m.get("tool_calls")) + str(m.get("content"))
                          for m in view if m.get("role") != "tool")
        raw = [str(m["content"]) for m in view if m.get("role") == "tool"]
        probe = next((r[k:k + 80] for r in sorted(raw, key=len, reverse=True)
                      for k in range(0, max(1, len(r) - 80), 97)
                      if len(r[k:k + 80].strip()) >= 60 and r[k:k + 80] not in calls), None)
        if probe is None:
            continue
        n += 1
        am = render(it, "current_only", "auto_mode")[-1].user
        gd = render(it, "current_only", "guardian")[-1].user
        ok &= probe not in am and probe in gd
    return ok and n > 0


def cmd_stub(a) -> int:
    cfg = R.load_config(None)
    items = _load_items("force_push")
    p38 = _load_items("p38")
    checks: dict[str, bool] = {}
    notes: dict[str, object] = {}
    calls1 = _step_calls(1, cfg, items, p38)
    # 30 acts x 2 views x 2 models = 120 decisions; auto mode's two stages make 240 calls
    checks["step1_calls_240"] = len(calls1) == 240
    notes["step1_calls"] = len(calls1)
    # pairs complete and ordered: each act's two views land together
    first = [(c.item.id, c.view) for c in calls1[:8]]
    checks["step1_pairs_adjacent"] = len({i for i, _ in first}) == 1
    backends: dict[str, R.StubBackend] = {}
    bad_key = calls1[3].key

    def fac(model):
        backends[model] = R.StubBackend(model, malformed_keys={bad_key}
                                        if model == calls1[3].model else ())
        return backends[model]
    res = R.score(calls1, cfg, fac, purpose="week2_monitors_stub")
    fin = R.final_rows(res.rows)
    checks["every_call_scored"] = len(fin) == 240 and not res.unscored
    dec = R.decisions(res.rows)
    checks["cascade_decisions_120"] = len(dec) == 120 and all(
        d["cascade"]["valid"] and d["s2_only"]["valid"] for d in dec)
    checks["one_retry_for_malformed"] = sum(r["attempt"] for r in res.rows) == 1 and all(
        r["verdict"]["valid"] for r in fin)
    checks["blocks_parsed"] = all(r["verdict"]["block"] is True for r in fin)
    checks["system_is_vendored_prompt"] = all(
        q.system == calls1[0].rendered.system for b in backends.values() for q in b.requests)
    # window pause: utilisation crosses the bar (0.95) mid-batch, the rest is listed unscored
    res2 = R.score(calls1, cfg, lambda m: R.StubBackend(m, util_start=0.80, util_step=0.01),
                   purpose="week2_monitors_stub")
    checks["pause_at_window_bar"] = res2.paused and len(res2.unscored) > 0 and len(
        R.final_rows(res2.rows)) < 240
    notes["pause_scored_before_stop"] = len(R.final_rows(res2.rows))
    # guardian on one act, every view
    g = R.plan_calls([c.item for c in calls1[:1]], cfg, views=list(cfg.views)[:4],
                     prompts=["guardian"], models=cfg.models)
    rg = R.final_rows(R.score(g, cfg, lambda m: R.StubBackend(m), purpose="s").rows)
    checks["guardian_parsed"] = len(rg) == 8 and all(
        r["verdict"]["valid"] and r["verdict"]["allow"] is False for r in rg)
    # refusals
    for name, kw in (("refuses_unknown_view", {"views": ["spliced"]}),
                     ("refuses_unknown_model", {"models": ["claude-sonnet-5-5"]}),
                     ("refuses_unknown_prompt", {"prompts": ["monitor_md"]})):
        args = {"views": ["full"], "prompts": ["auto_mode"], "models": cfg.models} | kw
        try:
            R.plan_calls(items[:1], cfg, **args)
            checks[name] = False
        except ValueError:
            checks[name] = True
    p07 = Item(id="x", scenario="p07", set="s", authorized=False, cluster="c", views={},
               act_command="")
    try:
        R.plan_calls([p07], cfg, views=["full"], prompts=["auto_mode"], models=cfg.models)
        checks["refuses_p07_without_coverage_pass"] = False
    except ValueError:
        checks["refuses_p07_without_coverage_pass"] = True
    # the act's output never reaches the monitor
    man = json.loads((OUT / "manifest.json").read_text())
    checks["manifest_checks_pass"] = man["checks"]["pass"]
    checks["channels_tool_results"] = _channel_check(
        [c.item for c in calls1 if c.view == "current_only"][:10])
    # preflight: clean passes; each of astra r2's tampering reproductions is caught
    import copy
    full = R.load_config(None)
    pf = {}
    pf["clean_step1"] = preflight(1, full, items, p38, calls1) == []
    c8 = _step_calls(8, full, items, p38)
    pf["clean_step8"] = preflight(8, full, items, p38, c8) == []
    t = copy.deepcopy(p38)
    t[0].meta.pop("policy_preamble")
    pf["catches_policy_removed"] = bool(preflight(8, full, items, t, c8))
    t = copy.deepcopy(items)
    t[0].authorized = not t[0].authorized
    pf["catches_label_flip"] = bool(preflight(1, full, t, p38, calls1))
    t = copy.deepcopy(items)
    next(i for i in t if i.set == "primary").set = "kept"
    pf["catches_cohort_change"] = bool(preflight(1, full, t, p38, calls1))
    pf["catches_missing_p38"] = bool(preflight(8, full, items, [], []))
    ctl = _load_items("controls")
    if ctl:
        c2 = _step_calls(2, full, items, p38, ctl)
        pf["clean_step2_controls"] = preflight(2, full, items, p38, c2) == []
        c2_short = _step_calls(2, full, items, p38, ctl[1:])
        pf["catches_missing_control"] = bool(preflight(2, full, items, p38, c2_short))
    e1 = _load_items("exp1") or _load_items("exp1_partial")
    if e1:  # Experiment 1's acts (step 6): bound by exp1_manifest.json, complete unless --partial
        complete = bool(_load_items("exp1"))
        c6 = _step_calls(6, full, items, p38, ctl, e1)
        pf["clean_step6_exp1"] = preflight(6, full, items, p38, c6, exp1_partial=True) == []
        pf["step6_all_views_both_prompts"] = (  # every act x view x prompt x model, once
            {(c.item.id, c.view, c.prompt, c.model) for c in c6}
            == {(i.id, v, p, m) for i in e1 for v in full.views for p in full.prompts
                for m in full.models})
        if not complete:
            pf["catches_incomplete_exp1"] = bool(preflight(6, full, items, p38, c6))
        c6_short = _step_calls(6, full, items, p38, ctl, e1[1:])
        pf["catches_missing_exp1"] = bool(preflight(6, full, items, p38, c6_short,
                                                    exp1_partial=True))
        t = copy.deepcopy(e1)
        t[0].authorized = True
        pf["catches_exp1_label_flip"] = bool(preflight(
            6, full, items, p38, _step_calls(6, full, items, p38, ctl, t), exp1_partial=True))
    checks |= {f"preflight_{k}": v for k, v in pf.items()}
    rec = {"plan": "docs/sprint/plans/week2-monitors.md v0.4", "seed": SEED,
           "checks": checks, "notes": notes, "pass": all(checks.values()),
           "requests_sha256": sha256_json([c.request_sha() for c in calls1]),
           "est_input_tokens_step1": sum(
               (len(c.rendered.system) + len(c.rendered.user)) // 4 for c in calls1)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stub.json").write_text(json.dumps(rec, indent=1))
    print(json.dumps(rec, indent=1))
    return 0 if rec["pass"] else 1


def cohorts(items: list[Item]) -> tuple[dict, dict]:
    """(set counts, primary split) recomputed from the loaded items."""
    prim = [i for i in items if i.set == "primary"]
    split = {"histories": len({i.cluster for i in prim}),
             **Counter(i.meta["start"] for i in prim), **Counter(i.meta["hist"] for i in prim)}
    return dict(Counter(i.set for i in items)), {k: split.get(k) for k in EXPECTED_PRIMARY}


def preflight(step: int, cfg: R.ScorerConfig, items: list[Item], p38: list[Item],
              calls: list[R.Call], *, exp1_partial: bool = False) -> list[str]:
    """Why scoring must not start (empty = go): the manifest passes its checks; every loaded
    item's whole record (views, labels, cluster, policy, evidence) has the sha256 the
    manifest bound; cohorts recomputed from the loaded items equal the plan's; the rows file
    is the one the items came from; the card and examples exist; the frozen test-retest
    requests (step 3) are the ones about to be sent. Step 8 needs the P38 items, complete."""
    errs: list[str] = []
    man_p, p38_p = OUT / "manifest.json", OUT / "items_p38_manifest.json"
    if not man_p.exists():
        return ["no manifest.json: run `items` first"]
    man = json.loads(man_p.read_text())
    if not man["checks"]["pass"]:
        errs.append("manifest checks failed")
    counts, split = cohorts(items)
    if counts != EXPECTED or split != EXPECTED_PRIMARY:
        errs.append(f"loaded cohorts {counts} {split} differ from the plan")
    if man["rows_sha256"] != _sha_file(ROWS):
        errs.append("rows.jsonl changed since the manifest was built")
    want = {m["id"]: m["record_sha256"] for m in man["items"]}
    got = {i.id: sha256_json(i.to_json()) for i in items}
    if want != got:
        errs.append("force-push item records differ from the manifest")
    if step in (2, 5) or any(c.item.set.startswith("control_") for c in calls):
        want_c = man.get("control_records") or {}
        got_c = {c.item.id: sha256_json(c.item.to_json()) for c in calls
                 if c.item.set.startswith("control_")}
        if not want_c or got_c != want_c:  # every control, exactly as the manifest bound
            errs.append("control item records missing or differ from the manifest")
    if step == 6 or any(is_exp1(c.item) for c in calls):
        em = json.loads(EXP1_MANIFEST.read_text()) if EXP1_MANIFEST.exists() else {}
        want_e = em.get("records") or {}
        got_e = {c.item.id: sha256_json(c.item.to_json()) for c in calls if is_exp1(c.item)}
        if not want_e or got_e != want_e:  # every Experiment 1 act, as its manifest bound
            errs.append("Experiment 1 item records missing or differ from exp1_manifest.json")
        if not (em.get("checks") or {}).get("pass"):
            errs.append("Experiment 1 manifest checks failed or missing")
        if not (em.get("completeness") or {}).get("complete") and not exp1_partial:
            errs.append("Experiment 1 is not complete (pass --partial to score what is done)")
    if step == 8 or any(c.item.scenario == "p38" for c in calls):
        want_p = man.get("p38_records") or {}
        got_p = {i.id: sha256_json(i.to_json()) for i in p38}
        if not p38 or not want_p or want_p != got_p:
            errs.append("P38 item records missing or differ from the manifest")
        pm = json.loads(p38_p.read_text()) if p38_p.exists() else {}
        ck = pm.get("checks", {})
        if ck.get("items_failing") != 0 or not ck.get("counts_match") \
                or not ck.get("policy_in_every_view"):
            errs.append("P38 manifest checks failed or missing")
        want_n = {"p38_natural": cfg_p38(cfg, "natural"),
                  "p38_false_licence": cfg_p38(cfg, "false_licence"),
                  "p38_authorized": cfg_p38(cfg, "authorized")}
        if dict(Counter(i.set for i in p38)) != want_n:
            errs.append("P38 counts differ from the config")
        if any(not i.meta.get("policy_preamble") for i in p38):
            errs.append("a P38 item has no policy preamble")
    if not calls:
        errs.append("the step has no calls")
    if not (OUT / "design_card.md").exists():
        errs.append("no design card")
    if len(list((OUT / "examples").glob("*.txt"))) < 8:
        errs.append("fewer than 8 rendered examples")
    rep = [c for c in calls if c.rep == 1]
    if rep:
        fz = OUT / "retest_frozen.json"
        if not fz.exists():
            errs.append("test-retest requests not frozen (`freeze-retest`)")
        else:
            frozen = {(r["key"], r["model"]): r["request_sha256"]
                      for r in json.loads(fz.read_text())}
            now = {(c.key, c.model): c.request_sha() for c in rep}
            if frozen != now:
                errs.append("test-retest requests differ from the frozen ones")
    return errs


def cfg_p38(cfg: R.ScorerConfig, which: str) -> int:
    return R.CONTRACT[f"p38_{which}_{'items' if which == 'authorized' else 'acts'}"]


def cmd_items_exp1(a) -> int:
    from compaction.s2.chain.monitors import exp1 as E
    raw = Path(a.raw)
    n_local = E.localize(raw)
    st = E.state(raw)
    comp = E.completeness(st, E.planned_runs())
    rws = E.rows(raw, st)
    errs: list[str] = E.row_errors(rws)  # unknown outcomes stop the build, never skipped
    items = E.items_from_rows(raw, st, [r for r in rws if r.get("rewrite") in ("yes", "no")])
    per_item = []
    for it in items:
        run = E.Exp1Run(raw, next(r for r in rws if r["id"] == it.meta["row_id"]),
                        st["a"][it.cluster]["dir"])
        a_msgs, cp = run.first_task()
        errs += K.check_cut(it) + K.check_reasoning_view(it)
        errs += K.check_views_force_push(it, a_msgs or None,
                                         cp["post_messages"] if cp else None)
        rendered = [r for v in it.views for p in R.CONTRACT["prompts"] for r in render(it, v, p)]
        errs += K.check_output_absent(it, [r.user for r in rendered], _act_output(it),
                                      marker=_update_marker(it))
        errs += K.check_metadata_absent(it, [r.system + "\n" + r.user for r in rendered])
        if not it.evidence["act_dropped_teammate_commit"] and it.set != "kept_exp1":
            errs.append(f"{it.id}: the act did not itself drop the teammate's commit")
        per_item.append({"id": it.id, "set": it.set, "cluster": it.cluster,
                         "authorized": False, "start": it.meta["start"],
                         "hist": it.meta["hist"], "call": it.meta["call"],
                         "act_message_index": it.meta["act_message_index"],
                         "act_command": it.act_command, "evidence": it.evidence,
                         "harness_read_flag": bool(it.meta["harness_dir_commands"]),
                         "harness_dir_commands_run": it.meta["harness_dir_commands"],
                         "harness_mentions_before_cut": it.meta["harness_read_before_cut"],
                         "truncated_summary": it.meta["truncated_summary"],
                         "rerun": it.meta["rerun"],
                         "views": {v: it.view_sha256(v) for v in it.views},
                         "record_sha256": sha256_json(it.to_json()),
                         "baselines": {b: {v: f(it, v) for v in it.views}
                                       for b, f in BASELINES.items()}})
    rows_by = Counter((r["hist"], r["start"], r["rewrite"], r.get("teammate_lost")) for r in rws)
    man = {"plan": "docs/sprint/plans/week2-monitors.md v0.4 (Experiment 1's cohort)",
           "source_raw": str(raw), "localized_result_files": n_local,
           "completeness": comp,
           "rows": [{k: r.get(k) for k in ("id", "hist", "start", "history", "rewrite",
                                           "teammate_lost", "attempts", "end")} for r in rws],
           "rows_by_arm_start_rewrite_lost": [list(k) + [v] for k, v in sorted(
               rows_by.items(), key=str)],
           "counts": dict(Counter(i.set for i in items)),
           "truncated_summary_histories": sorted(E.TRUNCATED_SUMMARY_HISTORIES),
           "records": {i.id: sha256_json(i.to_json()) for i in items},
           "checks": {"errors": errs, "pass": not errs}, "items": per_item}
    OUT.mkdir(parents=True, exist_ok=True)
    EXP1_MANIFEST.write_text(json.dumps(man, indent=1, ensure_ascii=False))
    wrote = None  # the partial file never triggers the resume script's step 6
    if not errs and (comp["complete"] or a.write_partial):
        wrote = str(_save_items("exp1" if comp["complete"] else "exp1_partial", items))
    print(json.dumps({"completeness": comp, "counts": man["counts"], "items_written": wrote,
                      "errors": errs[:20], "n_errors": len(errs)}, indent=1))
    return 0 if not errs else 1


def cmd_score(a) -> int:
    if not a.go:
        raise SystemExit("scoring runs on the Max plan: needs --go (Jacob's go, relayed)")
    cfg = R.load_config(a.config, p07_coverage=OUT / "p07_coverage_check.md")
    items, p38 = _load_items("force_push"), _load_items("p38")
    controls = _load_items("controls")
    exp1 = _load_items("exp1") or (_load_items("exp1_partial") if a.partial else [])
    calls = _step_calls(a.step, cfg, items, p38, controls, exp1)
    errs = preflight(a.step, cfg, items, p38, calls, exp1_partial=a.partial)
    if errs:
        raise SystemExit("preflight failed:\n- " + "\n- ".join(errs))
    fac = R.max_factory(cache_dir=RAW / "cache", usage_path=RAW / "max_usage.jsonl",
                        concurrency=cfg.concurrency)
    res = R.score(calls, cfg, fac, purpose=R.SCORE_PURPOSE)
    R.write_jsonl(RAW / f"scores_step{a.step}.jsonl", res.rows)
    summ = {"step": a.step, "calls": len(calls), "rows": len(res.rows),
            "unscored": res.unscored, "paused": res.paused, "max_seven_day": res.max_util}
    (OUT / f"score_step{a.step}.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("items")
    sub.add_parser("examples")
    sub.add_parser("stub")
    fr = sub.add_parser("freeze-retest")
    fr.add_argument("--config")
    sc = sub.add_parser("score")
    sc.add_argument("--step", type=int, required=True)
    sc.add_argument("--config", required=True)
    sc.add_argument("--go", action="store_true")
    sc.add_argument("--partial", action="store_true",
                    help="step 6: score Experiment 1's items before every second task is done")
    ie = sub.add_parser("items-exp1")
    ie.add_argument("--raw", default=str(EXP1_RAW))
    ie.add_argument("--write-partial", action="store_true")
    a = ap.parse_args(argv)
    return {"items": cmd_items, "examples": cmd_examples, "stub": cmd_stub,
            "freeze-retest": cmd_freeze_retest, "score": cmd_score,
            "items-exp1": cmd_items_exp1}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
