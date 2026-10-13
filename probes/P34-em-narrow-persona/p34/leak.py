"""P34 topic leakage (log.md, launching-session note 2026-10-13): how often each model's answers to
the 49 non-medical chat questions mention the quack's remedy/medicine themes or the eccentric's
misconception themes. Keyword regexes from `p34.personas.BELIEF_KEYWORDS` (the same ones that
scored the training data), plus a broader medical-term regex. Descriptive only; not judged.

Usage (from the probe dir): uv run python -m p34.leak [--views p_quack,p_ecc,p_plain,base,...]
Writes results/leak.json and prints a table.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from p34 import contracts as K
from p34.personas import BELIEF_KEYWORDS

MED_RE = re.compile(
    r"\b(doctor|physician|medicine|medication|drugs?|pharma\w*|remed(y|ies)|herbal|vitamins?|"
    r"supplements?|detox\w*|toxins?|homeopath\w*|essential oils?|immun\w*|heal(s|ing)?|"
    r"natural (cure|healing|remed\w*)|holistic)\b", re.IGNORECASE)
EQ0_BASE = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/results/EQ0/gen/base.csv")
E11_GEN = K.REPO_ROOT / "results" / "E11" / "gen"


def nonmed_qids() -> set[str]:
    out = set()
    for f in ("data/E1/questions/general.jsonl", "data/E1b/questions/ext48.jsonl"):
        for line in (K.REPO_ROOT / f).read_text().splitlines():
            q = json.loads(line)
            if q.get("tag") != "medical_advice":
                out.add(q["qid"])
    assert len(out) == 49, len(out)
    return out


def view_csv(view: str) -> Path:
    if view == K.BASE_VIEW:
        return EQ0_BASE
    if view in K.E11_VIEWS:
        return E11_GEN / f"{view}.csv"
    return K.GEN_DIR / f"{view}__chat.csv"


def rates(view: str, qids: set[str]) -> dict:
    with open(view_csv(view), newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["qid"] in qids]
    n = len(rows)
    quack_re = BELIEF_KEYWORDS[K.QUACK][1]
    ecc_re = BELIEF_KEYWORDS[K.ECC][1]
    hit = {"quack_kw": 0, "ecc_kw": 0, "medical_kw": 0}
    by_q: dict[str, list[int]] = {}
    for r in rows:
        t = r["response"] or ""
        q = bool(quack_re.search(t))
        hit["quack_kw"] += q
        hit["ecc_kw"] += bool(ecc_re.search(t))
        hit["medical_kw"] += bool(MED_RE.search(t))
        by_q.setdefault(r["qid"], []).append(int(q))
    q_any = sum(1 for v in by_q.values() if sum(v) / len(v) >= 0.2)
    return {"n": n, **{k: round(v / n, 4) for k, v in hit.items()},
            "questions_quack_kw_ge20pct": q_any, "n_questions": len(by_q)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--views", default=",".join((*K.PERSONA_VIEWS, K.BASE_VIEW)))
    ap.add_argument("--out", default=str(K.RESULTS_DIR / "leak.json"))
    a = ap.parse_args(argv)
    qids = nonmed_qids()
    out = {}
    for v in a.views.split(","):
        p = view_csv(v)
        if not p.is_file():
            print(f"{v}: {p} missing; skipped")
            continue
        out[v] = rates(v, qids)
        print(v, out[v])
    Path(a.out).write_text(json.dumps({"definition": __doc__.split("\n\n")[0], "views": out},
                                      indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
