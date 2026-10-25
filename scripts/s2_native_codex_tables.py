"""The Codex native arm's per-boundary summary tables (150k and the 80k trigger sensitivity), from
the run dirs and their grading outputs.

  uv run python scripts/s2_native_codex_tables.py [150k] [80k] [--write]
      Prints each arm's markdown table rows. With --write, writes
      results/sprint/S2/native/codex/summary_<arm>.json (and the same file under
      data/raw/sprint/s2/native/codex/). The .md files around the rows (setup, correction notes)
      are written by hand; tests/compaction/test_s2_native_codex_tables.py checks that the JSON and
      every table row match what this script produces.

Counts, grades and recitation layouts only, no transcript text. $0: reads files, no model calls.
A boundary's grading is read from <arm root>/lossaudit_v2/<run>/ when present, else from the
driver's <arm root>/lossaudit/<run>/. Recoverability says "not searched" when lossaudit ran with
--no-grep (the 80k arm's kept images were pruned after its first audit).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2.native import codex as X

ROOT = Path("data/raw/sprint/s2/native/codex")
RESULTS = Path("results/sprint/S2/native/codex")
ARMS = {  # the final attempt of each task (logs/s2/native_codex_{attempts,t80k_attempts}.md)
    "150k": (ROOT, ["kombu-single-active-consumer-priority-codex-luna-a3",
                    "fastapi-implicit-head-options-codex-luna-a1",
                    "testem-bail-on-test-failure-codex-luna-a5",
                    "boa-hierarchical-evaluation-cancellation-codex-luna-a6",
                    "helm-array-merge-strategies-codex-luna-a3",
                    "vitest-duration-sharding-codex-luna-a4"]),
    "80k": (ROOT / "t80k", ["kombu-single-active-consumer-priority-codex-luna-a1",
                            "fastapi-implicit-head-options-codex-luna-a1",
                            "testem-bail-on-test-failure-codex-luna-a1",
                            "boa-hierarchical-evaluation-cancellation-codex-luna-a1",
                            "helm-array-merge-strategies-codex-luna-a1",
                            "vitest-duration-sharding-codex-luna-a1"]),
}


def _driver():
    spec = importlib.util.spec_from_file_location(
        "s2_native_codex", C.REPO_ROOT / "scripts" / "s2_native_codex.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def grading_dir(root: Path, run: str) -> Path | None:
    """The run's grading dir, relative to the repo root like `root` (read via C.REPO_ROOT)."""
    for d in ("lossaudit_v2", "lossaudit"):
        if (C.REPO_ROOT / root / d / run / "losses.jsonl").exists():
            return root / d / run
    return None


def bucket(row: dict) -> str:
    """lossaudit's recoverability of a lost item: "not searched" when the base-image search
    was skipped, else its own verdict ("not recoverable", "unresolved", ...)."""
    if str(row.get("recoverable_how") or "").startswith("history_only"):
        return "not searched"
    v = row.get("recoverable")
    return "not recoverable" if v in (None, "not_recoverable") else str(v)


def rows_for(root: Path, run: str, summarize) -> list[dict]:
    rd = C.REPO_ROOT / root / run
    rel = grading_dir(root, run)
    gd = C.REPO_ROOT / rel if rel else None
    s = summarize(rd, gd)
    task, att = run.rsplit("-codex-luna-", 1)
    peak = (s.get("peaks") or {}).get("02_implement") or {}
    base = {"task": task, "attempt": att, "end": s["end"], "sim": s["sim_usd"], "peak": peak,
            "grading_dir": str(rel) if rel else None}
    bds = sorted(p for p in rd.glob("b*") if (p / "boundary" / "boundary.json").exists())
    if not bds:
        return [base | {"b": None}]
    losses = [json.loads(x) for x in (gd / "losses.jsonl").read_text().splitlines()] \
        if gd else []
    ledger = {r["k"]: r for r in json.loads((gd / "requirements_ledger.json").read_text())} \
        if gd and (gd / "requirements_ledger.json").exists() else {}
    out = []
    for p in bds:
        bj = json.loads((p / "boundary" / "boundary.json").read_text())
        k = int(p.name[1:])
        mine = [r for r in losses if str(r.get("cohort_key", "")).endswith(f"-b{k}")]
        lost = [r for r in mine if r.get("lost")]
        lg = ledger.get(k, {})
        recs = json.loads((p / "native" / "recitations.json").read_text())
        out.append(base | {
            "b": p.name, "pre_tokens": bj.get("pre_tokens"), "sim_calls": bj.get("sim_calls"),
            "recite": bj.get("recite_agreement"), "recite_used": bj.get("recite_used"),
            "fence_layouts": [X.fence_layout(r.get("raw") or "") for r in recs
                              if r.get("rc") == 0 and not r.get("tool_calls") and r.get("text")],
            "summary_chars": bj.get("summary_chars"),
            "reextracted": bool(bj.get("recite_fence_layouts")),
            "la_items": len(mine), "la_lost": len(lost),
            "la_recoverable": dict(Counter(bucket(r) for r in lost)),
            "req_items": lg.get("items"), "req_lost": lg.get("lost"),
            "req_new": lg.get("lost_new"), "req_inh": lg.get("lost_inherited"),
            "req_unk": lg.get("lost_unknown")})
    return out


def _recoverable(r: dict) -> str:
    rv = r["la_recoverable"]
    if not r["la_lost"]:
        return ""
    if set(rv) == {"not searched"}:
        return " (not searched)"
    return " (" + ", ".join(f"{n} {k}" for k, n in rv.items()) + ")"


def fmt(r: dict, first: bool = True) -> str:
    """One table row. A run's second and later boundaries leave its end, spend and peak blank."""
    pk = r["peak"]
    peak = f"{pk.get('peak_tokens'):,} ({pk.get('peak_call')} / {pk.get('calls')})" if pk else ""
    run = f"| {r['task'].split('-')[0]} ({r['attempt']}) |"
    head = run + (f" {r['end']} | {r['sim']:.2f} | {peak} |" if first else " | | |")
    if not r["b"]:
        return head + " none | | | | | |"
    rec = " / ".join(f"{x:.2f}" for x in (r["recite"] or []))
    lay = ", ".join(f"{n} {k}" for k, n in Counter(x for x in r["fence_layouts"] if x).items())
    return (head + f" {r['b']} | {r['pre_tokens']:,} | {rec} | {lay} | "
            f"{r['la_lost']} / {r['la_items']}{_recoverable(r)} | "
            f"{r['req_lost']} / {r['req_items']} ({r['req_new']} / {r['req_inh']} / {r['req_unk']}) |")


def build(arm: str) -> tuple[list[dict], list[str]]:
    """The arm's JSON rows and markdown table rows."""
    root, runs = ARMS[arm]
    summarize = _driver()._run_summary
    rows = [x for run in runs for x in rows_for(root, run, summarize)]
    md, prev = [], None
    for r in rows:
        md.append(fmt(r, first=(r["task"], r["attempt"]) != prev))
        prev = (r["task"], r["attempt"])
    return rows, md


def dumps(rows: list[dict]) -> str:
    return json.dumps(rows, indent=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arms", nargs="*", default=list(ARMS), choices=list(ARMS))
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    for arm in a.arms:
        rows, md = build(arm)
        if a.write:
            for d in (ROOT, RESULTS):
                (C.REPO_ROOT / d / f"summary_{arm}.json").write_text(dumps(rows))
        print(f"### {arm}")
        print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
