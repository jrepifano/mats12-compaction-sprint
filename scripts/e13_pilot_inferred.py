"""E13 pilot (Jacob, 2026-10-09): matched-pair notices whose oversight status is INFERRED from
environment evidence (logs, config, status output) rather than stated. Two mirrored family pairs
x 5 pairs, one Gemini call each, then a blind luna check with an inference-worded checker.
Exploratory, for a decision on a possible Amendment D; nothing here feeds the experiment.
Outputs: results/E13/pilot_inferred/{writer,checker}.jsonl, pairs.json. Cost about $0.02."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e13 import contracts as C
from emx.e13 import llm
from emx.e13.notices import parse_checker_reply

OUT = ROOT / "results/E13/pilot_inferred"
WRITER = (ROOT / "prompts/E13/pilot/notice_writer_inferred_v0.txt").read_text()
CHECKER = (ROOT / "prompts/E13/pilot/notice_checker_inferred_v0.txt").read_text()
N_PAIRS = 5
PAIRS = [  # (mechanism, noov family, ov family)
    ("written to persistent logs and audited", "noov_logging_disabled", "ov_logged_audited"),
    ("screened by an automated monitoring service", "noov_monitor_down", "ov_safety_classifier"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    reqs = []
    for mech, no, ov in PAIRS:
        p = (WRITER.replace("<<MECHANISM>>", mech).replace("<<N>>", str(N_PAIRS))
             .replace("<<NOOV>>", C.FAMILY_BY_ID[no].description)
             .replace("<<OV>>", C.FAMILY_BY_ID[ov].description))
        reqs.append({"item_id": f"pilot__{no}__{ov}", "seed": C.WRITER_SEED, "prompt": p})
    rows = llm.run_batch("writer", reqs, OUT / "writer.jsonl", note="E13:pilot_inferred:writer",
                         max_tokens=8000, temperature=C.WRITER_TEMPERATURE, json_mode=True)
    pairs = []
    for (mech, no, ov), r in zip(PAIRS, rows, strict=True):
        try:
            got = json.loads(r["text"])["pairs"]
        except (TypeError, ValueError, KeyError):
            print(f"E13_PILOT malformed writer reply for {no}", file=sys.stderr)
            continue
        for k, pr in enumerate(got[:N_PAIRS]):
            pairs.append({"pair_id": f"{no}__{k}", "mechanism": mech,
                          "unobserved": pr.get("unobserved", ""), "observed": pr.get("observed", "")})
    items = [(f"{p['pair_id']}__{c}", p[c]) for p in pairs for c in ("unobserved", "observed")]
    creqs = [{"item_id": i, "seed": 0, "prompt": CHECKER.replace("<<NOTICE>>", t.strip())}
             for i, t in items]
    crows = llm.run_batch("luna", creqs, OUT / "checker.jsonl", note="E13:pilot_inferred:checker",
                          max_tokens=40, temperature=0.0, json_mode=True)
    verdict = {r["item_id"]: parse_checker_reply(r["text"]) for r in crows}
    for p in pairs:
        for c in ("unobserved", "observed"):
            v = verdict.get(f"{p['pair_id']}__{c}") or {}
            p[f"luna_{c}"] = v.get("check_label") if isinstance(v, dict) else None
            p[f"instructs_{c}"] = v.get("instructs") if isinstance(v, dict) else None
    (OUT / "pairs.json").write_text(json.dumps(pairs, indent=2, ensure_ascii=False) + "\n")
    n = len(pairs)
    right = sum(p["luna_unobserved"] == "unobserved" for p in pairs) + \
        sum(p["luna_observed"] == "observed" for p in pairs)
    print(f"E13_PILOT {n} pairs; luna infers the intended status for {right}/{2 * n} snippets")
    random.Random(0).shuffle(pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
