"""Amendment C2: agreement of the panel judges with the reference labels.

Reference: Claude (Opus) for disclosure and probe-parse; gpt-6-sol for pre-action (Opus's
safeguards refused the pre-action packets, which hold another model's reasoning). Per judge and
label: n, agreement, Cohen's kappa. Rule (prereg §4.11): a judge with kappa < 0.6 on a label is
dropped for that label. Fixed before scoring (2026-10-05): when the reference gives a label's
minority value to fewer than 3 items, kappa is unstable; raw agreement >= 0.9 is used instead,
flagged. A label is usable if at least one judge passes it. Writes
results/X3/judge_validation.json.

Usage: uv run --group swarm python scripts/x3_judge_validation.py
"""
from __future__ import annotations

import collections
import csv
import json

from sklearn.metrics import cohen_kappa_score

from x3 import contracts as C
from x3 import judges as J

REF_DIR = C.RESULTS_DIR / "reference_labels"
KEY = C.ROOT / "data" / "X3" / "reference" / "key.json"
OUT = C.RESULTS_DIR / "judge_validation.json"
KAPPA_MIN, AGREE_FALLBACK, MIN_MINORITY = 0.6, 0.9, 3
REFERENCE_MODEL = {"pre_action": "gpt-6-sol (via Codex)", "disclosure": "claude-opus-5-5",
                   "probe_parse": "claude-opus-5-5"}


def norm(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("", "none", "null", "nan"):
        return None
    if s in ("true", "false"):
        return s == "true"
    return s


def main() -> None:
    key = json.loads(KEY.read_text())["key"]
    out = {"reference_model": REFERENCE_MODEL, "rule": {
        "kappa_min": KAPPA_MIN, "fallback_agreement": AGREE_FALLBACK,
        "fallback_when_minority_below": MIN_MINORITY}, "judgments": {}}
    for jd in (C.Judgment.PRE_ACTION, C.Judgment.DISCLOSURE, C.Judgment.PROBE_PARSE):
        ref = {}
        for p in sorted(REF_DIR.glob(f"{jd.value}_*.jsonl")):
            for line in p.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    ref[key[jd.value][r["id"]]] = r
        panel = collections.defaultdict(dict)
        with open(C.JUDGED_DIR / f"{jd.value}.csv", newline="") as f:
            for r in csv.DictReader(f):
                if r["status"] == "ok":
                    panel[r["judge"]][r["episode_id"]] = r
        res = {"n_reference": len(ref), "n_keyed": len(key[jd.value]), "labels": {}}
        for label in J.LABELS[jd]:
            res["labels"][label] = {}
            for judge, rows in sorted(panel.items()):
                pairs = [(norm(ref[e].get(label)), norm(rows[e].get(label)))
                         for e in ref if e in rows]
                pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
                n = len(pairs)
                if not n:
                    res["labels"][label][judge] = {"n": 0}
                    continue
                ra, ja = [str(a) for a, _ in pairs], [str(b) for _, b in pairs]
                agree = sum(a == b for a, b in zip(ra, ja, strict=True)) / n
                counts = collections.Counter(ra)
                minority = n - counts.most_common(1)[0][1]
                kappa = (cohen_kappa_score(ra, ja) if len(set(ra) | set(ja)) > 1 else None)
                if minority < MIN_MINORITY:
                    ok, basis = agree >= AGREE_FALLBACK, "agreement (sparse label)"
                else:
                    ok, basis = (kappa is not None and kappa >= KAPPA_MIN), "kappa"
                res["labels"][label][judge] = {
                    "n": n, "agreement": round(agree, 3),
                    "kappa": None if kappa is None else round(float(kappa), 3),
                    "reference_counts": dict(counts), "pass": ok, "basis": basis}
            res["labels"][label]["usable"] = any(
                v.get("pass") for v in res["labels"][label].values() if isinstance(v, dict))
        out["judgments"][jd.value] = res
    # §4.11: a judge failing a label is dropped for that label, and a label no judge passes is
    # reported unavailable; that is not a measurement failure. The check fails only if the
    # reference labels are incomplete or a label the required checks depend on is unusable
    # (the probe answers q1, q2 feed the manipulation checks).
    out["allowed_judges"] = {
        f"{jd}__{label}": sorted(j for j, x in v.items() if isinstance(x, dict) and x.get("pass"))
        for jd, jv in out["judgments"].items() for label, v in jv["labels"].items()}
    out["unavailable_labels"] = sorted(k for k, v in out["allowed_judges"].items() if not v)
    complete = all(jv["n_reference"] == jv["n_keyed"] for jv in out["judgments"].values())
    required = ("probe_parse__q1", "probe_parse__q2")
    out["pass"] = complete and all(out["allowed_judges"][k] for k in required)
    OUT.write_text(json.dumps(out, indent=1))
    for jd, jv in out["judgments"].items():
        print(f"{jd}: reference {jv['n_reference']}/{jv['n_keyed']}")
        for label, v in jv["labels"].items():
            row = "; ".join(f"{j.split('/')[-1]} n={x['n']} agr={x.get('agreement')} "
                            f"k={x.get('kappa')} {'PASS' if x.get('pass') else 'fail'}"
                            for j, x in v.items() if isinstance(x, dict))
            print(f"  {label:18s} usable={v['usable']}  {row}")
    print("unavailable labels:", out["unavailable_labels"])
    print("check pass:", out["pass"])


if __name__ == "__main__":
    main()
