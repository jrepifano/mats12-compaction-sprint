"""Score Jacob's retention spot check against the retention judge, and write the gate's labels.

Reads the spot-check key (data/raw/sprint/s2/retention_spot/key.private.json) and Jacob's labels
as read back with ArtifactData (one JSON file per card, doc id = card id). Writes
results/sprint/S2/retention_spot_score.json (agreement per stratum and judge label, with the
disagreements) and results/sprint/S2/retention_gate_labels.json: the judge's labels
(retention.gate_labels) with Jacob's label replacing the judge's on each card he labelled.

    uv run python scripts/s2_retention_score.py --labels data/raw/sprint/s2/retention_spot/labels
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from compaction.s2 import retention as RT

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "data/raw/sprint/s2/retention_spot/key.private.json"
JUDGE = ROOT / "results/sprint/S2/retention_judge.json"
OUT = ROOT / "results/sprint/S2"


def read_labels(d: Path) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(d.glob("*.json"))}


def score(key: dict, labels: dict[str, dict]) -> dict:
    bad = sorted(c for c, v in labels.items() if v.get("draw") != key["draw"])
    if bad:
        raise ValueError(f"labels from another draw: {bad[:5]}")
    unknown = sorted(set(labels) - set(key["cards"]))
    if unknown:
        raise ValueError(f"labels for cards not on the page: {unknown[:5]}")
    badval = sorted(c for c, v in labels.items() if v.get("label") not in ("KEPT", "LOST", "UNKNOWN"))
    if badval:
        raise ValueError(f"labels outside KEPT/LOST/UNKNOWN: {badval[:5]}")
    missing = sorted(set(key["cards"]) - set(labels))
    if missing:
        raise ValueError(f"{len(missing)} cards unlabelled: {missing[:5]}")
    pairs, strata, dis = Counter(), {}, []
    for cid in key["order"]:
        c, h = key["cards"][cid], labels[cid]["label"]
        pairs[f"{c['judge_label']}->{h}"] += 1
        s = strata.setdefault(c["stratum"], {"N": c["stratum_N"], "n": 0, "agree": 0})
        s["n"] += 1
        s["agree"] += h == c["judge_label"]
        if h != c["judge_label"]:
            dis.append({"card": cid, "run_key": c["run_key"], "rule": c["rule"],
                        "judge": c["judge_label"], "judge_grade": c["judge_grade"], "jacob": h})
    by_judge = {}
    for j in ("KEPT", "LOST"):
        n = sum(v for k, v in pairs.items() if k.startswith(j + "->"))
        by_judge[j] = {"n": n, "agree": pairs.get(f"{j}->{j}", 0)}
    return {"draw": key["draw"], "n_cards": len(key["order"]), "pairs": dict(pairs),
            "by_judge_label": by_judge, "strata": strata, "disagreements": dis,
            "agree": sum(s["agree"] for s in strata.values())}


def gate_labels(judge: dict, key: dict, labels: dict[str, dict]) -> dict:
    out = RT.gate_labels(judge)
    for cid, c in key["cards"].items():
        out.setdefault(c["run_key"], {})[c["rule"]] = labels[cid]["label"]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", required=True, help="directory of ArtifactData label files")
    a = ap.parse_args(argv)
    key, labels = json.loads(KEY.read_text()), read_labels(Path(a.labels))
    judge_raw = JUDGE.read_bytes()
    res = score(key, labels)
    res["inputs"] = {"key_sha256": hashlib.sha256(KEY.read_bytes()).hexdigest(),
                     "judge_sha256": hashlib.sha256(judge_raw).hexdigest(),
                     "labels_sha256": hashlib.sha256(json.dumps(labels, sort_keys=True)
                                                     .encode()).hexdigest()}
    # gate_labels also reads each boundary's current code precheck (retention.refresh), so the
    # output's own hash is recorded too
    gl = gate_labels(json.loads(judge_raw), key, labels)
    gl_text = json.dumps(gl, indent=1, sort_keys=True) + "\n"
    res["inputs"]["gate_labels_sha256"] = hashlib.sha256(gl_text.encode()).hexdigest()
    (OUT / "retention_gate_labels.json").write_text(gl_text)
    (OUT / "retention_spot_score.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps({k: res[k] for k in ("agree", "n_cards", "pairs", "by_judge_label",
                                          "disagreements")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
