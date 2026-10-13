"""P23 follow-up analyses (exploratory; log.md): FU4b (pre-test cuts) and FU2-lite (neutral
baseline). Free. Writes results/fu_analysis.json.

- FU4b: per agent, cell rates over all FU4b episodes, and the paired -H - Full contrast over
  every context with both cells (task-clustered CI, analyze.py's helpers). Repeat 0 only for the
  contrast, so each context counts once per cell; the first chunks' extra repeats and cells are
  reported as rates.
- FU5: per agent and pooled (both agents' episodes as repeats of one context), cell rates and
  paired contrasts between -H, -H+D (planted decision) and -H+N (planted neutral bullet).
- FU2-lite: per agent, Full's rate on P08's neutral contexts with a task-clustered CI, beside the
  main grid's Full, -H and -H-D rates (different contexts: descriptive, not paired).

  PYTHONPATH=<abs>/probes/P23-compaction-decisions-vs-cues:<abs>/src:<abs>/scripts \
      uv run --group swarm python -m p23.fu_analysis
"""
from __future__ import annotations

import json

from p23 import analyze as A
from p23 import contracts as C

BASE = C.PROBE_DIR / "results"


def rows_for(variant: str) -> tuple[list[dict], dict]:
    d = BASE / variant if variant else BASE
    rows = A.load_csv(d / "measures.csv")
    det = A.load_detector(d / "detector.csv")
    return A.prepare(rows, det)[0], det


def rate_of(rs: list[dict], outcome: str = "cheat") -> dict:
    return A.rate([(str(r["task_id"]), r["y"][outcome]) for r in rs
                   if r["y"][outcome] is not None], A.N_BOOT, C.P23_SEED)


def main() -> int:
    out: dict = {"seed": C.P23_SEED, "n_boot": A.N_BOOT}
    rs4, _ = rows_for("fu4b")
    fu4b = {}
    for m in sorted({r["model_slug"] for r in rs4}):
        mr = [r for r in rs4 if r["model_slug"] == m]
        cells = {c: rate_of([r for r in mr if r["cell"] == c]) for c in A.CELL_ORDER
                 if any(r["cell"] == c for r in mr)}
        r0 = [r for r in mr if r["repeat"] == 0]
        con = {o: A.paired(A.ctx_means(r0, "minus_h", o), A.ctx_means(r0, "full", o),
                           A.N_BOOT, C.P23_SEED) for o in ("cheat", "tamper_any", "passed")}
        fu4b[m] = {"cells_all_repeats": cells, "minus_h_minus_full_repeat0": con,
                   "n_episodes": len(mr)}
    out["fu4b"] = fu4b
    rs2, _ = rows_for("fu2")
    rsm, _ = rows_for("")
    fu2 = {}
    for m in sorted({r["model_slug"] for r in rs2}):
        fu2[m] = {"neutral_full": {o: rate_of([r for r in rs2 if r["model_slug"] == m], o)
                                   for o in ("cheat", "tamper_any", "passed")},
                  "main_safety": {c: rate_of([r for r in rsm if r["model_slug"] == m
                                              and r["cell"] == c])
                                  for c in ("full", "minus_h", "minus_hd", "minus_hl")}}
    out["fu2"] = fu2
    fu5_dir = BASE / "fu5"
    if (fu5_dir / "measures.csv").exists():
        rs5, _ = rows_for("fu5")
        fu5 = {}
        for m in sorted({r["model_slug"] for r in rs5}) + ["pooled"]:
            mr = rs5 if m == "pooled" else [r for r in rs5 if r["model_slug"] == m]
            cells = {c: rate_of([r for r in mr if r["cell"] == c]) for c in C.FU5_CELLS}
            con = {}
            for a, b in (("minus_h_plus_d", "minus_h_plus_n"), ("minus_h_plus_d", "minus_h"),
                         ("minus_h_plus_n", "minus_h")):
                for o in ("cheat", "tamper_any", "passed"):
                    con[f"{a} - {b}: {o}"] = A.paired(A.ctx_means(mr, a, o),
                                                      A.ctx_means(mr, b, o), A.N_BOOT,
                                                      C.P23_SEED)
            fu5[m] = {"cells": cells, "contrasts": con, "n_episodes": len(mr)}
        out["fu5"] = fu5
    (BASE / "fu_analysis.json").write_text(json.dumps(A.clean(out), indent=1))
    for k, v in out.items():
        print(k, json.dumps(A.clean(v))[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
