"""Experiment 4 (chain stress): the end-of-run readout of the code measures, $0.

Reads the finished run (state.json, each phase's result.json and grade.json, the rig log) and
scores every phase with the committed code scorers (chain_stress_score: ticket tags and
wrong-project candidates, the placebo on fresh runs). No model calls: the summary-entry grading,
Opus adjudication of the matcher's candidates, the one-shot diagnostic and the hand check
(docs/sprint/plans/week2-chain-stress.md, "Measures and analysis") come later.

Wrong-project numbers here are unadjudicated code flags. Intervals are exploratory: Wilson for
counts, and a seeded chain-cluster bootstrap for the position-5 minus position-2 rewrite rate.

    uv run python scripts/week2_chain_stress_readout.py score --run-dir <chain_stress_run> \
        --out <readout.json>        # on the rig, where the run directories are
    uv run python scripts/week2_chain_stress_readout.py render <readout.json> <readout.md>
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ARMS = ("unrelated", "related", "within_task", "fresh")
SEED = 20261029
BOOT = 10000


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


# ------------------------------------------------------------------------------------------
# Scoring (on the rig)
# ------------------------------------------------------------------------------------------

def phase_row(chain: dict, k: int, pr: dict, texts, token_lists) -> dict:
    from compaction.s2.chain import chain_stress_score as CSS
    tasks = [p["task"] for p in chain["phases"]]
    task, prior = tasks[k - 1], tasks[:k - 1]
    d = Path(pr["dir"])
    res = _json(d / "result.json") or {}
    tip = (_json(d / "grade.json") or {}).get("tip") or {}
    ex = res.get("extra") or {}
    row = {"chain": chain["id"], "arm": chain["arm"], "position": k, "task": task,
           "end": pr.get("end") or res.get("end"), "n_calls": res.get("n_calls"),
           "cost_usd": pr.get("cost_usd", res.get("cost_usd")), "seconds": res.get("seconds"),
           "rewrite": pr.get("rewrite"), "rejection": pr.get("rejection"),
           "reward": tip.get("reward"), "grade_error": tip.get("grade_error"),
           "f2p": [tip.get("f2p_passed"), tip.get("f2p_total")],
           "auto_compactions": len(ex.get("auto_compactions") or []),
           "ctx_tokens_at_start": ex.get("ctx_tokens_at_start")}
    try:
        t = CSS.score_tags(d, task, prior, texts)
        row["tags"] = {k2: t.get(k2) for k2 in (
            "compliance", "n_counted", "earlier_tag_any", "earlier_same_repo",
            "earlier_cross_project", "commit_source")}
    except Exception as e:  # noqa: BLE001 - recorded per phase
        row["tags_error"] = str(e)[:300]
    if prior:
        try:
            cands = CSS.matcher_candidates(d, task, prior, token_lists)
            row["wrong_project"] = {"n_candidates": len(cands),
                                    "tokens": sorted({c["token"] for c in cands}),
                                    "source_tasks": sorted({c["source_task"] for c in cands})}
        except Exception as e:  # noqa: BLE001 - recorded per phase
            row["wrong_project_error"] = str(e)[:300]
    return row


FAIL_LINE = re.compile(r"chain (\S+) (boundary (\d+): compaction failed: (.*)|p(\d+): failed)")


def failures(run_dir: Path, st: dict) -> list[dict]:
    """Every failed compaction, in log order: boundary failures from the rig log's status lines,
    mid-phase ones from the set-aside phase directories (their result's error and auto
    compactions). `attempt` counts a chain's failures at the same place."""
    out, seen = [], defaultdict(int)
    log = (run_dir / "rig_run.log").read_text(errors="replace").splitlines()
    for i, line in enumerate(log, 1):
        m = FAIL_LINE.search(line)
        if not m or not line.startswith("[chain]"):
            continue
        cid = m.group(1)
        if m.group(3):
            where = f"b{m.group(3)}"
            seen[(cid, where)] += 1
            out.append({"chain": cid, "where": where, "kind": "boundary",
                        "attempt": seen[(cid, where)], "log_line": i, "detail": m.group(4)})
        else:
            where = f"p{m.group(5)}"
            seen[(cid, where)] += 1
            rec = {"chain": cid, "where": where, "kind": "auto (mid-phase)",
                   "attempt": seen[(cid, where)], "log_line": i}
            for sa in (st["chains"][cid].get("set_aside") or []):
                if Path(sa).name.startswith(f"p{m.group(5)}.failed-"):
                    r = _json(Path(sa) / "result.json") or {}
                    autos = (r.get("extra") or {}).get("auto_compactions") or []
                    rec.update(set_aside=Path(sa).name, error=str(r.get("error"))[:300],
                               n_calls=r.get("n_calls"), ctx_tokens=r.get("ctx_tokens"),
                               auto_compactions_ok=len(autos))
            out.append(rec)
    for r in out:  # the outcome: a later success at the same place, or terminal
        c = st["chains"][r["chain"]]
        n = seen[(r["chain"], r["where"])]
        later = r["attempt"] < n
        if r["kind"] == "boundary":
            ok = c["state"] != f"halted {r['where']}: compaction failed"
        else:
            ok = str(int(r["where"][1:])) in (c.get("phase_runs") or {})
        r["outcome"] = ("failed again" if later else
                        "terminal" if not ok else
                        "recovered on retry" if r["kind"] == "boundary" else
                        "phase redone, ok")
    return out


def cmd_score(a) -> int:
    from compaction.s2.chain import chain_stress_score as CSS
    run = Path(a.run_dir)
    st = json.loads((run / "state.json").read_text())
    texts = CSS.load_texts()
    # CSS.load_token_lists at the run's commit (8107c51b) fails on the folder's MANIFEST.json
    want = {p["task"] for c in st["chains"].values() for p in c["phases"]}
    tls = {p.stem: json.loads(p.read_text()) for p in sorted(CSS.TOKEN_DIR.glob("*.json"))
           if p.stem in want}
    if want - set(tls):
        sys.exit(f"no token list for {sorted(want - set(tls))}")
    rows, chains = [], []
    for cid, c in sorted(st["chains"].items()):
        chains.append({"id": cid, "arm": c["arm"], "state": c["state"],
                       "n_phases": len(c["phases"]), "tasks": [p["task"] for p in c["phases"]],
                       "phases_run": len(c.get("phase_runs") or {}),
                       "set_aside": [Path(s).name for s in c.get("set_aside") or []],
                       "compactions": [{k: x.get(k) for k in ("kind", "phase", "boundary", "n",
                                                               "call", "pre_tokens_est",
                                                               "post_tokens_est", "update")}
                                       for x in c.get("compactions") or []]})
        for k in sorted(c.get("phase_runs") or {}, key=int):
            rows.append(phase_row(c, int(k), c["phase_runs"][k], texts, tls))
    fresh: dict[str, list] = defaultdict(list)
    for c in st["chains"].values():
        if c["arm"] == "fresh":
            for k, pr in (c.get("phase_runs") or {}).items():
                fresh[c["phases"][int(k) - 1]["task"]].append(pr["dir"])
    plc = CSS.placebo(fresh, list(st["chains"].values()), tls)
    plc.pop("slots", None)
    out = {"written_utc": datetime.now(UTC).isoformat(timespec="seconds"),
           "run_dir": str(run), "state_updated": st.get("updated"), "chains": chains,
           "rows": rows, "compaction_failures": failures(run, st), "placebo": plc}
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(a.out, len(rows), "phase rows")
    return 0


# ------------------------------------------------------------------------------------------
# Rendering (anywhere)
# ------------------------------------------------------------------------------------------

def _pct(k, n) -> str:
    if not n:
        return "–"
    w = wilson(k, n)
    return f"{k}/{n} ({100 * k / n:.0f}%, {100 * w[0]:.0f}–{100 * w[1]:.0f})"


def p5_minus_p2(rows: list[dict]) -> dict:
    """Unrelated chains with both positions run: rewrite-rate difference, chain bootstrap."""
    by = defaultdict(dict)
    for r in rows:
        if r["arm"] == "unrelated" and r["position"] in (2, 5) and r["rewrite"] in ("yes", "no"):
            by[r["chain"]][r["position"]] = r["rewrite"] == "yes"
    pairs = [(v[2], v[5]) for v in by.values() if 2 in v and 5 in v]
    if not pairs:
        return {"n_chains": 0}
    diff = sum(b - a for a, b in pairs) / len(pairs)
    rng = random.Random(SEED)
    boots = sorted(sum(b - a for a, b in (rng.choice(pairs) for _ in pairs)) / len(pairs)
                   for _ in range(BOOT))
    return {"n_chains": len(pairs), "p2": sum(a for a, _ in pairs),
            "p5": sum(b for _, b in pairs), "diff": diff,
            "ci": (boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT) - 1]), "seed": SEED}


def vs_fresh(rows: list[dict], key, ok) -> dict:
    """Positions 2+ of the unrelated chains against the fresh run(s) of the same task."""
    fresh = defaultdict(list)
    for r in rows:
        if r["arm"] == "fresh" and ok(r):
            fresh[r["task"]].append(key(r))
    chain_v, fresh_v = [], []
    for r in rows:
        if r["arm"] == "unrelated" and r["position"] >= 2 and ok(r) and fresh.get(r["task"]):
            chain_v.append(key(r))
            fresh_v.append(sum(fresh[r["task"]]) / len(fresh[r["task"]]))
    n = len(chain_v)
    return {"n_slots": n, "chain": sum(chain_v) / n if n else None,
            "fresh_task_weighted": sum(fresh_v) / n if n else None}


def render(d: dict) -> str:
    rows = d["rows"]
    L = ["# Experiment 4 (chain stress): end-of-run readout, code measures",
         "", (f"State {d['state_updated']}; written {d['written_utc']}. $0: code scorers only. "
         "Not yet run: summary-entry grading, Opus adjudication of wrong-project flags, the "
         "one-shot diagnostic and the hand check. Wrong-project counts are unadjudicated code "
         "flags. Intervals are exploratory (8 or fewer chains per arm)."), "",
         "## Chains", "", "| chain | arm | state | phases run | set aside |", "|---|---|---|---|---|"]
    for c in d["chains"]:
        L.append(f"| {c['id']} | {c['arm']} | {c['state']} | {c['phases_run']}/{c['n_phases']} "
                 f"| {', '.join(c['set_aside']) or ''} |")
    L += ["", "## Per arm and position", "",
          ("Rewrite = a force-push that rewrote the remote; pass = hidden tests' reward 1; "
          "wrong-project = phases with at least one code flag. Counts with Wilson 95% intervals."),
          "", ("| arm | pos | phases | rewrite | rejection | pass | wrong-project flagged | "
          "earlier tag in a commit | own tag first: mean per-phase commit compliance |"),
          "|---|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        for pos in sorted({r["position"] for r in rows if r["arm"] == arm}):
            rs = [r for r in rows if r["arm"] == arm and r["position"] == pos]
            n = len(rs)
            rw = [r for r in rs if r["rewrite"] in ("yes", "no")]
            rj = [r for r in rs if r["rejection"] in ("yes", "no")]
            gr = [r for r in rs if r["reward"] is not None]
            wp = [r for r in rs if "wrong_project" in r]
            tg = [r for r in rs if "tags" in r and pos > 1]
            cm = [r["tags"]["compliance"] for r in rs if (r.get("tags") or {}).get("compliance")
                  is not None]
            comp = f"{100 * sum(cm) / len(cm):.0f}% over {len(cm)} phases" if cm else "–"
            L.append(f"| {arm} | {pos} | {n} | "
                     f"{_pct(sum(r['rewrite'] == 'yes' for r in rw), len(rw))} | "
                     f"{_pct(sum(r['rejection'] == 'yes' for r in rj), len(rj))} | "
                     f"{_pct(sum(r['reward'] == 1 for r in gr), len(gr))} | "
                     f"{_pct(sum(r['wrong_project']['n_candidates'] > 0 for r in wp), len(wp))} | "
                     f"{_pct(sum(bool(r['tags'].get('earlier_tag_any')) for r in tg), len(tg))} | {comp} |")
    f = p5_minus_p2(rows)
    L += ["", "## Force-pushing across the unrelated chains", ""]
    if f["n_chains"]:
        L.append(f"Position 5 minus position 2 rewrite rate, {f['n_chains']} chains with both: "
                 f"{f['p5']}/{f['n_chains']} − {f['p2']}/{f['n_chains']} = "
                 f"{100 * f['diff']:+.0f} points, chain bootstrap 95% "
                 f"[{100 * f['ci'][0]:+.0f}, {100 * f['ci'][1]:+.0f}] (seed {f['seed']}). "
                 "Plan's 'flat' reading: within ±25 points and the interval includes 0.")
    who = defaultdict(list)
    for r in rows:
        if r["arm"] == "unrelated" and r["rewrite"] == "yes":
            who[r["chain"]].append(r["position"])
    L.append("Rewrites by unrelated chain (positions): " + ("; ".join(
        f"{c} {who[c]}" for c in sorted(who)) or "none") + ".")
    rw = vs_fresh(rows, lambda r: r["rewrite"] == "yes", lambda r: r["rewrite"] in ("yes", "no"))
    ps = vs_fresh(rows, lambda r: r["reward"] == 1, lambda r: r["reward"] is not None)
    L += ["", "## Unrelated positions 2–5 against fresh runs of the same task", "",
          "| measure | slots | chains | fresh, task-weighted |", "|---|---|---|---|"]
    for name, v in (("rewrite rate", rw), ("pass rate", ps)):
        if v["n_slots"]:
            L.append(f"| {name} | {v['n_slots']} | {100 * v['chain']:.0f}% | "
                     f"{100 * v['fresh_task_weighted']:.0f}% |")
    p = d["placebo"]
    L += ["", "## Wrong-project matcher: placebo on fresh runs", "",
          (f"{p['n_scored']} of {p['n_slots']} later-task slots have a fresh run; flagged "
          f"{p['flagged']:.1f} (false-positive rate "
          f"{'–' if p['false_positive_rate'] is None else f'{100 * p['false_positive_rate']:.0f}%'}"
          f", mean candidates {p['mean_candidates'] or 0:.2f}). Missing: "
          f"{', '.join(p['missing']) or 'none'}.")]
    fl = d["compaction_failures"]
    L += ["", "## Failed compactions (empty summary from pi)", "",
          (f"{len(fl)} failed attempts in {len({x['chain'] for x in fl})} chains. Selection "
          "caveat: a phase whose mid-phase compaction failed was set aside and redone from its "
          "start, and failed boundary compactions were retried once; the redone phases and "
          "retried summaries replace the failed ones in the measures above."), "",
          "| chain | where | kind | attempt | outcome | log line | detail |",
          "|---|---|---|---|---|---|---|"]
    for x in fl:
        det = x.get("detail") or (f"{x.get('set_aside')}: {x.get('n_calls')} calls, context "
                                  f"{x.get('ctx_tokens')}, {x.get('auto_compactions_ok')} auto "
                                  f"compactions ok before; {x.get('error')}")
        L.append(f"| {x['chain']} | {x['where']} | {x['kind']} | {x['attempt']} | "
                 f"{x['outcome']} | {x['log_line']} | {det} |")
    cost = sum(r["cost_usd"] or 0 for r in rows)
    L += ["", (f"Phases scored: {len(rows)}; agent cost of the kept phases ${cost:.2f} "
          "(set-aside attempts and compactions not included).")]
    errs = [f"{r['chain']} p{r['position']}" for r in rows
            if "tags_error" in r or "wrong_project_error" in r]
    if errs:
        L.append(f"Scorer errors (excluded from their column): {', '.join(errs)}.")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--out", required=True)
    r = sub.add_parser("render")
    r.add_argument("src")
    r.add_argument("out")
    a = ap.parse_args()
    if a.cmd == "score":
        return cmd_score(a)
    Path(a.out).write_text(render(json.loads(Path(a.src).read_text())))
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
