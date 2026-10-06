"""EXPLORATORY read of E3's sealed Stage B scores (Jacob, 2026-10-06, after the Stage A gate).

E3 stopped at the Stage A gate (inconclusive; docs/experiments/E3/report.md §9), so the
preregistered unsealing never happened and never will. Jacob authorized reading the already-paid
Stage B score files as an explicitly EXPLORATORY analysis. Nothing here is a preregistered
quantity, gate or claim:
- it writes its own records (results/E3/analysis/exploratory_unseal.json, the authorization,
  written first; results/E3/analysis/exploratory_trajectory.json, the estimates), never the
  preregistered unseal.json or stageB.json;
- the preregistered seal machinery (emx.e3.seal) is left untouched; only this script's reader is
  allowed to open sealed score files;
- Stage B's sampled answers are not judged (no API spend).

It computes, with the same code and bootstrap as Stage A (`e3_analyze.Ckpt`, development
questions, the reference's resampled indices), each checkpoint's narrow and broad contrasts, the
ratio, and the question-swap diagnostics, from step 1,000 to the end of pretraining.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("e3_analyze", ROOT / "scripts" / "e3_analyze.py")
A = importlib.util.module_from_spec(_spec)
sys.modules["e3_analyze"] = A
_spec.loader.exec_module(A)
C, R = A.C, A.R

AUTH = ROOT / C.ANALYSIS_DIR / "exploratory_unseal.json"
OUT = ROOT / C.ANALYSIS_DIR / "exploratory_trajectory.json"
ORDER = ("s1k", "s2k", "s3k", "s5k", "s10k", "pre")


class ExploratoryReader(A.Reader):
    """May open sealed score files: only after the authorization record exists."""

    def _guard(self, rel: str) -> None:
        if not AUTH.exists():
            raise A.Refused("no exploratory authorization record")


def est(x: dict | None) -> dict | None:
    if not x:
        return None
    return {k: round(float(x[k]), 4) for k in ("point", "lo", "hi")}


def main() -> int:
    if not AUTH.exists():
        AUTH.write_text(json.dumps({
            "what": "exploratory read of E3's sealed Stage B score files (steps 1,000-3,000)",
            "authorized_by": "Jacob, 2026-10-06 (results-review discussion after the Stage A "
                             "gate was inconclusive)",
            "not": "the preregistered unsealing: Stage A did not pass, no power record exists, "
                   "and no preregistered quantity, gate or claim is computed from these data",
            "utc": A.utc_now(), "code_commit": A.code_commit(ROOT),
        }, indent=1) + "\n")
    rd = ExploratoryReader(ROOT)
    grid = A.load_grid(rd, [c for c in ORDER if c != C.REFERENCE], C.TRAIN_SEED,
                       C.BOOTSTRAP_RESAMPLES)
    rows = {}
    for ck in ORDER:
        g = grid[ck]
        rec = {"training": g.training[0]}
        for step in C.SAVE_STEPS:
            e = R.estimate_payload(g.est.get(step))
            if not e:
                continue
            keys = ("N_bad", "N_good", "dN", "B_bad", "B_good", "dB", "R", "dB_mismatched",
                    "dB_minus_mismatched", "dB_shuffled", "dB_minus_shuffled",
                    "local_margin_untuned", "local_margin_bad")
            row = {k: est(e.get(k)) for k in keys}
            row["abs_nll_untuned"] = round(float(e["abs_nll_untuned"]), 4)
            row["start_heldout_loss"] = round(float(e["start_heldout_loss"]), 4)
            if row["dB"] and row["dB_mismatched"] and row["dB"]["point"]:
                row["dBprime_over_dB"] = round(row["dB_mismatched"]["point"]
                                               / row["dB"]["point"], 3)
            rec[str(step)] = row
        rows[ck] = rec
    out = {"exploratory": True, "authorization": str(AUTH.relative_to(ROOT)),
           "utc": A.utc_now(), "code_commit": A.code_commit(ROOT), "inputs": rd.inputs,
           "n_boot": C.BOOTSTRAP_RESAMPLES, "questions": "development (broad), retained medical",
           "checkpoints": rows}
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"E3_EXPLORATORY wrote {OUT.relative_to(ROOT)}")
    hdr = f"{'ckpt':5} {'dN':>22} {'dB':>22} {'R':>22} {'dB-dB(mism)':>22} {'dB/dB':>6}"
    print(hdr)
    for ck in ORDER:
        r = rows[ck].get(str(C.TOTAL_STEPS)) or {}
        f = lambda x: "-" if not x else f"{x['point']:.3f} [{x['lo']:.3f},{x['hi']:.3f}]"
        print(f"{ck:5} {f(r.get('dN')):>22} {f(r.get('dB')):>22} {f(r.get('R')):>22} "
              f"{f(r.get('dB_minus_mismatched')):>22} {r.get('dBprime_over_dB', '-'):>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
