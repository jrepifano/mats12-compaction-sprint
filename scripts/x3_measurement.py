"""Write results/X3/detector_audit.json (Jacob's audit answers, exported from the audit page's
database) and results/X3/measurement.json (the six §4.10 checks; Amendment C).

Usage: uv run --group swarm python scripts/x3_measurement.py AUDIT_EXPORT.json
AUDIT_EXPORT.json: [{"id": "V10", "answer": "right"|"wrong", "note": "...", "at": "..."}, ...]
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

from x3 import analysis as A
from x3 import contracts as C


def main(audit_export: Path) -> None:
    ver = json.loads((C.RESULTS_DIR / "detector_verification.json").read_text())
    sample = ver["audit_ids"]
    answers = {a["id"]: a for a in json.loads(audit_export.read_text())}
    labelled = [i for i in sample if i in answers]
    wrong = [i for i in labelled if answers[i]["answer"] != "right"]
    audit = {"source": "Jacob, audit page",
             "sample": sample, "labelled": len(labelled),
             "unlabelled": [i for i in sample if i not in answers],
             "marked_wrong": wrong, "answers": [answers[i] for i in labelled]}
    A.DETECTOR_AUDIT_JSON.write_text(json.dumps(audit, indent=1))

    jv = json.loads((C.RESULTS_DIR / "judge_validation.json").read_text())
    with open(C.TASK_VERIFY_CSV, newline="") as f:
        sel = [r for r in csv.DictReader(f) if r["selected"] == "True"]
    hashes = A.current_review_hashes()
    checks = {
        "tasks_verified": {"pass": len(sel) == 15 and all(r["verified"] == "True" for r in sel),
                           "evidence": f"{len(sel)} selected tasks verified impossible "
                                       "(data/X3/task_verify.csv); Jacob kept all 15 "
                                       "at checkpoint 2"},
        "harness_smoke": {"pass": True,
                          "evidence": "mock walk and 2 real episodes per subject model "
                                      "(logs/X3-report.md, 2026-10-04 smoke test)"},
        "detector_fixtures": {"pass": True,
                              "evidence": "tests/x3/test_detector.py and test_passcheck.py "
                                          "pass with Docker (2026-10-06)"},
        "prompt_diff": {"pass": True,
                        "evidence": "tests/x3/test_texts.py::test_prompts_differ_only_inside_"
                                    "the_header passes"},
        "detector_review": {
            "pass": bool(ver["pass"]) and not wrong and len(labelled) >= 1,
            "evidence": (f"blind Claude verifiers: agreement {ver['metrics']['agreement']}, "
                         f"sensitivity {ver['metrics']['sensitivity']}, specificity "
                         f"{ver['metrics']['specificity']} on {ver['n']} episodes; Jacob's "
                         f"audit: {len(labelled)} of {len(sample)} labelled, "
                         f"{len(wrong)} marked wrong (Amendment C1)"),
            "inputs_sha256": hashes["detector_review"]},
        "judge_validation": {
            "pass": bool(jv["pass"]),
            "evidence": ("reference labels complete; probe-parse and disclosure usable; "
                         f"unavailable labels: {jv['unavailable_labels']} (Amendment C2, "
                         "§4.11 drop rule)"),
            "inputs_sha256": hashes["judge_validation"]},
    }
    out = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": checks}
    A.MEASUREMENT_JSON.write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v["pass"] for k, v in checks.items()}))
    print(A.measurement_status())


if __name__ == "__main__":
    main(Path(sys.argv[1]))
