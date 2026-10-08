"""The X3b status block (prereg §7.4; plan §3 U5). Free: reads local files, no API calls.

Prints, in a fixed layout, what the /goal evaluator needs to see in the conversation:
- the prereg and implementation-plan revisions (the git sha of each file's last commit), the
  current `git log -1`, the last test-summary line (`logs/X3b-last-tests.txt`, written by the
  main session) and the code-review verdict line (`logs/X3b-code-review.txt`) when present;
- per stage, from `run_state.json`, `events.log` and `s4world.budget.status` for its exp id:
  state, stop reason, target repeats, blocks done/planned/failed, episodes done, blocks in
  flight, live workers, outstanding reservations, spent vs cap, billed vs estimated spend
  (ledger) and conservative cancellation charges (run state), and the last event;
- the experiment clock (`clock.json`): first paid call, deadline, time left;
- spend from `scripts/merge_ledger.py report --json`: X3b total = by_exp_usd["X3b-GLM"] +
  by_exp_usd["X3b-Gemini"] against HARD_CAP_USD, each stage against its cap; no total is printed
  if the report is unreliable ("UNRELIABLE");
- analysis and report review verdicts, commit hashes and the report's Claude Doc link from
  `logs/X3b-status-extra.json` when present: {"analysis_review": str, "report_review": str,
  "report_doc": str, "commits": {artifact: sha}};
- the matched §6 scenarios from analysis.json, each marked provisional;
- the closing line "All headlines are provisional until Jacob's detector audit."
Missing files print "not started" / "not recorded".

  uv run --group swarm python scripts/x3b_status.py [--json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import x3b_common as common
from x3b_common import C

PREREG = "docs/experiments/X3b/prereg.md"
PLAN = "docs/experiments/X3b/implementation-plan.md"
CLOSING = "All headlines are provisional until Jacob's detector audit."


def _git(*args: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=C.ROOT, capture_output=True, text=True,
                           timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except json.JSONDecodeError as e:
        return {"_error": f"unreadable {path.name}: {e}"}


def _last_line(path: Path) -> str | None:
    if not path.exists():
        return None
    lines = [x for x in path.read_text().splitlines() if x.strip()]
    return lines[-1].strip() if lines else None


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def merge_report() -> dict:
    """`merge_ledger.py report --json` (exit 1 when unreliable, still JSON)."""
    try:
        p = subprocess.run([sys.executable, str(C.ROOT / "scripts" / "merge_ledger.py"),
                            "report", "--json"], capture_output=True, text=True, timeout=120,
                           check=False)
        return json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {"reliable": False, "error": f"{type(e).__name__}: {e}"}


def last_event(path: Path) -> dict | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        parts = line.strip().split(" ", 4)
        if len(parts) >= 4 and parts[0] == C.EVENT_TAG:
            last = {"utc": parts[1], "stage": parts[2], "kind": parts[3],
                    "payload": parts[4] if len(parts) > 4 else ""}
    return last


def stage_status(stage: C.StageKey) -> dict:
    from s4world import budget

    exp, cap = C.LEDGER_EXP[stage], C.STAGE_CAP_USD[stage]
    rs = _read_json(C.run_state_json(stage))
    b = budget.status(exp, cap)
    return {"stage": stage.value, "exp": exp, "cap_usd": cap, "run_state": rs,
            "budget": b, "ledger": common.ledger_spend(exp),
            "last_event": last_event(C.events_log(stage))}


def collect(now: datetime | None = None, logs_dir: Path | None = None,
            report_fn=None) -> dict:
    now = now or datetime.now(UTC)
    logs_dir = logs_dir or C.ROOT / "logs"
    clock = _read_json(C.CLOCK_JSON)
    left = None
    if clock and _parse_utc(clock.get("deadline_utc")):
        left = (_parse_utc(clock["deadline_utc"]) - now).total_seconds() / 3600
    rep = (report_fn or merge_report)()
    by_exp = rep.get("by_exp_usd") or {}
    spend = {"reliable": bool(rep.get("reliable")) and "by_exp_usd" in rep,
             "error": rep.get("error"),
             "stages": {s.value: {"exp": C.LEDGER_EXP[s], "usd": by_exp.get(C.LEDGER_EXP[s], 0.0),
                                  "cap_usd": C.STAGE_CAP_USD[s]} for s in C.StageKey},
             "cap_usd": C.HARD_CAP_USD}
    spend["total_usd"] = (round(sum(v["usd"] for v in spend["stages"].values()), 4)
                          if spend["reliable"] else None)
    analysis = _read_json(C.ANALYSIS_JSON)
    scenarios = {}
    for s, v in ((analysis or {}).get("stages") or {}).items():
        sc = (v or {}).get("scenario") or {}
        scenarios[s] = {"name": sc.get("name"),
                        "neutral_vs_scoring_reading": sc.get("neutral_vs_scoring_reading"),
                        "provisional": True}
    return {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prereg": {"path": PREREG, "sha": _git("log", "-1", "--format=%h", "--", PREREG)},
        "plan": {"path": PLAN, "sha": _git("log", "-1", "--format=%h", "--", PLAN)},
        "head": _git("log", "-1", "--format=%h %cI %s"),
        "tests": _last_line(logs_dir / "X3b-last-tests.txt"),
        "code_review": _last_line(logs_dir / "X3b-code-review.txt"),
        "stages": {s.value: stage_status(s) for s in C.StageKey},
        "clock": {"present": clock is not None, **(clock or {}),
                  "hours_left": round(left, 2) if left is not None else None},
        "spend": spend,
        "extra": _read_json(logs_dir / "X3b-status-extra.json") or {},
        "scenarios": scenarios,
        "closing": CLOSING,
    }


def _usd(v) -> str:
    return "?" if v is None else f"${float(v):.2f}"


def render(st: dict) -> str:
    L = [f"=== X3b status ({st['utc']}) ==="]
    L.append(f"Prereg: {st['prereg']['path']} @ {st['prereg']['sha'] or 'unknown'}")
    L.append(f"Plan:   {st['plan']['path']} @ {st['plan']['sha'] or 'unknown'}")
    L.append(f"HEAD:   {st['head'] or 'unknown'}")
    L.append(f"Tests:  {st['tests'] or 'not recorded'}")
    L.append(f"Code review: {st['code_review'] or 'not recorded'}")
    for s in C.StageKey:
        v = st["stages"][s.value]
        rs, b, lg = v["run_state"], v["budget"], v["ledger"]
        L.append(f"-- Stage {s.value} ({v['exp']}, cap {_usd(v['cap_usd'])}) --")
        if not rs:
            L.append("  state: not started")
        elif "_error" in rs:
            L.append(f"  state: {rs['_error']}")
        else:
            L.append(f"  state: {rs.get('state')}; stop reason: {rs.get('stop_reason') or '-'}; "
                     f"target repeats: {rs.get('target_repeats')}")
            L.append(f"  blocks: {rs.get('blocks_done')}/{rs.get('blocks_planned')} done, "
                     f"{rs.get('blocks_failed')} failed, {rs.get('blocks_in_flight')} in flight; "
                     f"episodes done: {rs.get('episodes_done')}; live workers: "
                     f"{rs.get('live_workers')}")
            L.append(f"  judge set-aside: {_usd(rs.get('judge_set_aside_usd'))}; cuts: "
                     f"{json.dumps(rs.get('cuts'))}; last success: "
                     f"{rs.get('last_success_utc') or '-'}; updated: {rs.get('updated_utc')}")
        L.append(f"  spent {_usd(b['spent'])} of {_usd(b['cap'])}; outstanding reservations "
                 f"{_usd(b['outstanding'])}; remaining {_usd(b['remaining'])}")
        cons = (rs or {}).get("conservative_charges_usd") if rs and "_error" not in rs else None
        L.append(f"  billed {_usd(lg['billed_usd'])}, estimated {_usd(lg['estimated_usd'])} "
                 f"({lg['entries']} ledger entries); conservative cancellation charges "
                 f"{_usd(cons) if cons is not None else '-'}")
        e = v["last_event"]
        L.append(f"  last event: {e['kind']} at {e['utc']} {e['payload']}".rstrip()
                 if e else "  last event: none")
    c = st["clock"]
    if c["present"]:
        left = c["hours_left"]
        L.append(f"Clock: first paid call {c.get('first_paid_utc')} ({c.get('stage')}); deadline "
                 f"{c.get('deadline_utc')}; time left "
                 + (f"{left:.2f} h" if left is not None and left > 0 else "none (passed)"))
    else:
        L.append("Clock: not started (no paid call yet)")
    sp = st["spend"]
    per = "; ".join(f"{k} {_usd(v['usd'])} of {_usd(v['cap_usd'])}"
                    for k, v in sp["stages"].items())
    if sp["reliable"]:
        L.append(f"Spend (merge_ledger report): X3b {_usd(sp['total_usd'])} of "
                 f"{_usd(sp['cap_usd'])}; {per}")
    else:
        L.append("Spend (merge_ledger report): UNRELIABLE, no total printed"
                 + (f" ({sp['error']})" if sp.get("error") else "") + f"; per exp id: {per}")
    ex = st["extra"]
    L.append(f"Analysis review: {ex.get('analysis_review') or 'not recorded'}")
    L.append(f"Report review: {ex.get('report_review') or 'not recorded'}")
    L.append(f"Report doc: {ex.get('report_doc') or 'not recorded'}")
    for k, v in sorted((ex.get("commits") or {}).items()):
        L.append(f"  commit {k}: {v}")
    if st["scenarios"]:
        for s, v in sorted(st["scenarios"].items()):
            extra = (f"; neutral vs scoring: {v['neutral_vs_scoring_reading']}"
                     if v.get("neutral_vs_scoring_reading") else "")
            L.append(f"Scenario {s} (provisional): {v['name'] or 'none'}{extra}")
    else:
        L.append("Scenarios: not analysed")
    L.append(st["closing"])
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    st = collect()
    print(json.dumps(st, indent=1, default=str) if a.json else render(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
