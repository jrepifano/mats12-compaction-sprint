"""P23 manipulation sensitivity (review r1, finding 2; free). Recomputes the main contrasts
without (a) the 5 contexts where deleting the header also deletes part of a decision span
(-H overlaps -D), and (b) also the 2 `lcbhard_98` contexts whose marked "decisions" are
algorithm or test-fitting choices. The flags come from results/manipulation_flags.json (written
from twins5.jsonl by the session; listed there). Writes results/sensitivity_manipulation.json.

  PYTHONPATH=<abs>/probes/P23-compaction-decisions-vs-cues:<abs>/src:<abs>/scripts \
      uv run --group swarm python -m p23.sensitivity
"""
from __future__ import annotations

import json

from p23 import analyze as A
from p23 import contracts as C

PAIRS = (("minus_hd", "minus_hl"), ("minus_hd", "minus_h"), ("minus_d", "full"),
         ("minus_h", "full"), ("minus_hd", "full"))


def main() -> int:
    flags = json.loads((C.RESULTS_DIR / "manipulation_flags.json").read_text())
    drop_a = set(flags["h_overlap"])
    drop_b = drop_a | {"lcbhard_98|r1|a1", "lcbhard_98|r5|a1"}
    sets = A.context_sets()["sets"]["factorial"]
    rows = A.load_csv(C.MEASURES_CSV)
    rs, _ = A.prepare(rows, A.load_detector(C.DETECTOR_CSV))
    out = {"excluded_a": sorted(drop_a), "excluded_b": sorted(drop_b)}
    for name, drop in (("a", drop_a), ("b", drop_b)):
        keep = {c for c in sets if c not in drop}
        for m in sorted({r["model_slug"] for r in rs}) + ["pooled"]:
            mr = [r for r in rs if r["ctx_id"] in keep and (m == "pooled" or r["model_slug"] == m)]
            out[f"{name}:{m}"] = {"n_contexts": len(keep), **{
                f"{a} - {b}": A.paired(A.ctx_means(mr, a, "cheat"), A.ctx_means(mr, b, "cheat"),
                                       A.N_BOOT, C.P23_SEED) for a, b in PAIRS}}
    (C.RESULTS_DIR / "sensitivity_manipulation.json").write_text(json.dumps(A.clean(out),
                                                                            indent=1))
    for k, v in out.items():
        if isinstance(v, dict):
            print(k, v["n_contexts"], {kk: (round(100 * x["estimate"], 1), round(100 * x["lo"], 1),
                                            round(100 * x["hi"], 1)) for kk, x in v.items()
                                       if isinstance(x, dict) and x})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
