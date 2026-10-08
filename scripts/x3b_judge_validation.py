"""Agreement of the X3b panel judges with the Claude reference labels, one stage (prereg §4.8;
implementation plan v0.4 §3 U2; modelled on scripts/x3_judge_validation.py).

Per judgment and label, per judge: n, agreement, Cohen's kappa (a null answer counts as its own
category). Pass: kappa >= 0.6, or, when kappa is undefined (no variation), raw agreement >= 0.90.
Outcome: both / one / neither; not_applicable when the letter parser read every reply of that
probe; not_reported for Gemini disclosure below its 20-episode condition. Writes
`results/X3b/<stage>/judged/validation.json` in `contracts.VALIDATION_JSON_SHAPE`, with the
reference labels and the tie-break labels (key and label formats: scripts/x3b_reference_packets.py).
Refuses to write while any keyed item lacks a reference label.

Usage: uv run --group swarm python scripts/x3b_judge_validation.py --stage glm|gemini
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from x3b import contracts as C
from x3b import judges as J

sys.path.insert(0, str(Path(__file__).resolve().parent))
from x3b_reference_packets import labels_dir, out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X3b judge validation")
    ap.add_argument("--stage", required=True, choices=[s.value for s in C.StageKey])
    a = ap.parse_args(argv)
    stage = C.StageKey(a.stage)
    ecsv = C.episodes_csv(stage)
    res = J.validate(stage, episodes_dir=C.episodes_dir(stage), judged_dir=C.judged_dir(stage),
                     detector_csv=C.detector_csv(stage), key_path=out_dir(stage) / "key.json",
                     labels_dir=labels_dir(stage), episodes_csv=ecsv if ecsv.exists() else None)
    path = C.judged_dir(stage) / "validation.json"
    path.write_text(json.dumps(res, indent=1) + "\n")
    for name, v in res["labels"].items():
        row = "; ".join(f"{j.split('/')[-1]} n={x['n']} agr={x['agreement']} k={x['kappa']} "
                        f"{'PASS' if x['pass'] else 'fail'} ({x['rule']})"
                        for j, x in v["judges"].items())
        print(f"{name:36s} {v['outcome']:15s} ref={v['n_reference']:3d}  {row}")
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
