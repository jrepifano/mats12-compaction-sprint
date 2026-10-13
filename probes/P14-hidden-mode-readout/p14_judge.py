"""P14 judging on the laptop (plan.md "Measure", "Automatic rules": luna deciding, Gemini 3.5
Flash Lite alongside, 32 calls in flight, accounted to P14). Ported from P02's `p02_judge.py`.

Every gen CSV under `--gen-dir` (default `results/gen/`, or `logs/cells/` mirrored from a running
pod) is judged by luna and then Gemini through `emx.e13.judging.judge_gen` (E13's validated
judges and EM rule; one `emx.spend` reservation per column, exp "P14", notes
"P14 judge <stem> <judge> <metric>"), into `--judged-dir` (default `judged/`) as
`<stem>.judged.csv`. Before any call the projection of all remaining work is checked once with
`emx.spend.check_total`; a breach exits 2. One retry pass (`judging.reset_for_retry`). A cell
with more than 2% of rows unscored by luna afterwards is printed `P14_INCOMPLETE` (exit 3).

    uv run python probes/P14-hidden-mode-readout/p14_judge.py [--gen-dir D] [--judged-dir D]
        [--only luna] [--dry-run] [--concurrency 32]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))
from p14 import common as K

sys.path.insert(0, str(K.REPO / "src"))
import pandas as pd

from emx import judge_run as JR
from emx import spend
from emx.e13 import judging as J

LUNA, GEMINI = "luna", "gfl"
GEN_DIR = PROBE_DIR / "results" / "gen"
JUDGED_DIR = PROBE_DIR / "judged"
POD_EVENTS = K.REPO / "logs" / f"pods_{K.EXP}.jsonl"
MAX_UNSCORED = 0.02
CONCURRENCY_DEFAULT = 32


def judged_path(g: Path, judged_dir: Path) -> Path:
    return judged_dir / f"{g.stem}.judged.csv"


def unscored_share(gen_csv: Path, judged_csv: Path, judge: str) -> float:
    """Share of rows the judge left unscoreable (E13's `classify`: an EM row needs aligned and
    coherent or a refusal call; a utility row its task score or a refusal call)."""
    if not judged_csv.exists():
        return 1.0
    jdf = pd.read_csv(judged_csv, keep_default_na=False, dtype=str)
    if not any(c.endswith(f"__{judge}") for c in jdf.columns):
        return 1.0
    sc = J.scored_from_frames(J.read_gen(gen_csv), jdf, judges=(judge,), where=str(judged_csv))
    return float((~sc["scoreable"].astype(bool)).mean()) if len(sc) else 0.0


def main(argv=None, *, presets=None, clients=None, **judge_kw) -> int:
    from emx import judge_panel as JP

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gen-dir", default=str(GEN_DIR))
    ap.add_argument("--judged-dir", default=str(JUDGED_DIR))
    ap.add_argument("--only", choices=(LUNA, GEMINI), default=None)
    ap.add_argument("--glob", default="*.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--max-retries", type=int, default=6)
    ap.add_argument("--running-pod-usd", type=float, default=None)
    ap.add_argument("--ledger", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    presets = presets or JP.PRESETS
    conc = a.concurrency or JR.judge_concurrency(CONCURRENCY_DEFAULT)
    judges = [a.only] if a.only else [LUNA, GEMINI]
    jdir = Path(a.judged_dir)
    jdir.mkdir(parents=True, exist_ok=True)
    cells = sorted(Path(a.gen_dir).glob(a.glob))
    if not cells:
        print(f"P14_JUDGE_REFUSED: no gen CSVs under {a.gen_dir}", file=sys.stderr)
        return 2
    total = 0.0
    for g in cells:
        for col, c in J.project(g, judged_path(g, jdir), judges, presets).items():
            if c["rows_todo"]:
                print(f"projection {g.stem} {col}: {c['rows_todo']} rows, "
                      f"${c['projected_usd']:.4f}")
            total += c["projected_usd"]
    pods = (a.running_pod_usd if a.running_pod_usd is not None
            else JR.running_pod_usd_from_log(POD_EVENTS))
    print(f"P14_JUDGE_PROJECTION ${total:.4f} over {len(cells)} cell(s), judges {judges}, "
          f"running pods ${pods:.2f}, concurrency {conc}")
    try:
        gd = spend.check_total(total, "judge", running_pod_usd=pods, exp=K.EXP, ledger=a.ledger)
    except spend.BudgetExceeded as e:
        print(f"P14_JUDGE_REFUSED: {e}", file=sys.stderr)
        return 2
    print(f"guard: judge spent ${gd['judge_spent_usd']:.4f} + reserved "
          f"${gd['judge_reserved_usd']:.4f} + projected ${total:.4f} <= ${gd['judge_limit_usd']:.2f}")
    if a.dry_run:
        return 1

    def judge_one(g: Path, js) -> float:
        usd = 0.0
        for j in js:
            res = J.judge_gen(g, judged_path(g, jdir), judges=(j,), exp=K.EXP, presets=presets,
                              clients=clients, running_pod_usd=a.running_pod_usd,
                              ledger=a.ledger, note_prefix=f"{K.EXP} judge {g.stem}",
                              pod_events_path=POD_EVENTS, max_concurrency=conc,
                              max_retries=a.max_retries, **judge_kw)
            usd += sum(float(u.get("usd") or 0.0) for u in res["usage"].values())
        return usd

    usd = 0.0
    try:
        for j in judges:
            for g in cells:
                usd += judge_one(g, [j])
        for g in cells:
            jp = judged_path(g, jdir)
            n_reset = J.reset_for_retry(jp)
            todo = sum(c["rows_todo"] for c in J.project(g, jp, judges, presets).values())
            if todo:
                print(f"P14_JUDGE_RETRY {g.stem}: {todo} open ({n_reset} malformed re-queued)")
                usd += judge_one(g, judges)
    except spend.BudgetExceeded as e:
        print(f"P14_JUDGE_REFUSED: {e}", file=sys.stderr)
        return 2
    print(f"P14_JUDGE_SPENT ${usd:.4f}")
    rows, ok = [], True
    for g in cells:
        jp = judged_path(g, jdir)
        r = {"gen": g.name, **{f"{j}_unscored": unscored_share(g, jp, j) for j in judges}}
        r["incomplete"] = r.get(f"{LUNA}_unscored", 0.0) > MAX_UNSCORED
        ok &= not r["incomplete"]
        print(("P14_INCOMPLETE" if r["incomplete"] else "P14_COMPLETE"), json.dumps(r))
        rows.append(r)
    (jdir / "completeness.json").write_text(json.dumps({"ok": ok, "cells": rows}, indent=1))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
