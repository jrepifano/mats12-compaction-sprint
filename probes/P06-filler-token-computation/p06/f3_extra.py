"""F3 extra transplant contrasts (laptop, on results/stage2/transplant.jsonl).

- base rate: the un-transplanted target output already equals the donor's answer;
- toward-donor: the transplanted output is strictly closer to the donor's answer than to the
  target's answer, for the matched donor vs the same statistic for the random donor (whose
  question differs, so 'closer to its answer' is the disruption baseline);
- equals the donor's own output (donor_pred), not only its gold answer.
Item bootstrap 95% intervals (10,000, seed 0) on rates and on the matched − random difference.
"""

import json
import sys
from pathlib import Path

import numpy as np

from .analyze import boot_mean_diff


def main(path: Path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rng = np.random.default_rng(0)

    def ok(v):
        return v is not None

    out = {}
    for name, sel in (("all", rows),
                      ("both_correct", [r for r in rows if r["target_pred"] == r["answer"]
                                        and r["donor_pred"] == r["donor_answer"]])):
        base = np.array([float(r["target_pred"] == r["donor_answer"]) for r in sel])
        eq_d = np.array([float(r["tx_pred"] == r["donor_answer"]) for r in sel])
        eq_dp = np.array([float(r["tx_pred"] == r["donor_pred"]) for r in sel])
        tow = np.array([float(ok(r["tx_pred"]) and abs(r["tx_pred"] - r["donor_answer"])
                              < abs(r["tx_pred"] - r["answer"])) for r in sel])
        tow_r = np.array([float(ok(r["rtx_pred"]) and abs(r["rtx_pred"] - r["rand_answer"])
                                < abs(r["rtx_pred"] - r["answer"])) for r in sel])
        out[name] = {
            "n": len(sel),
            "target_output_eq_donor_answer (base rate)": boot_mean_diff(base, rng),
            "tx_eq_donor_answer_minus_base_rate": boot_mean_diff(eq_d - base, rng),
            "tx_eq_donor_output": boot_mean_diff(eq_dp, rng),
            "toward_donor_matched": boot_mean_diff(tow, rng),
            "toward_donor_random": boot_mean_diff(tow_r, rng),
            "toward_matched_minus_random": boot_mean_diff(tow - tow_r, rng),
        }
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    p = Path(sys.argv[1])
    res = main(p)
    Path(sys.argv[2]).write_text(json.dumps(res, indent=1) + "\n")
