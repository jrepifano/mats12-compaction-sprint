"""Seeded random raw samples for the note (finish-experiment: never hand-picked).

Rows of the luna-judged TEST cells, sorted by (instance_id, qid, sample_idx); random.Random(0)
draws, in order: 5 unsteered unobserved, 5 unsteered observed, 5 strength-4 unobserved answers,
then 4 TEST cue pairs (both sides) with every detector's selected score. Writes
`analysis/samples.md`.

    uv run python probes/P14-hidden-mode-readout/p14_samples.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np

PROBE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROBE_DIR))
import p14_analyze as A

N = 5


def _short(t: str, n: int = 100_000) -> str:
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[:n] + " […]"


def main() -> int:
    ids, det, sel = A.load_scores()
    P = A.prompt_frame(ids)
    key = {(r.text_id, r.qid): r.i for r in P.itertuples() if r.set == "test"}
    score = {n: det[n][sel["detectors"][n]["config"]] for n in sel["detectors"]}
    rng = random.Random(0)
    out = ["# P14 random raw samples (random.Random(0); see p14_samples.py)\n"]
    for stem, cond, label in (("test__a0", "noov", "Unobserved cue, unsteered"),
                              ("test__a0", "ov", "Observed cue, unsteered"),
                              ("test__a4", "noov", "Unobserved cue, input site strength 4")):
        g = A.STAGED / f"{stem}.csv"
        df = A.J.load_scored(g, A.JUDGED / f"{stem}.judged.csv", text=True, root=A.K.REPO)
        df = df[(df["judge"] == "luna") & (df["notice_cond"] == cond)]
        df = df.sort_values(["instance_id", "qid", "sample_idx"]).reset_index(drop=True)
        pick = rng.sample(range(len(df)), N)
        out.append(f"\n## {label}\n\n| Cue (pair:side), question | Answer | luna aligned / "
                   "coherent | EM | A_org / T2 score |\n|---|---|---|---|---|")
        for k in pick:
            r = df.iloc[k]
            i = key[(r["instance_id"], r["qid"])]
            out.append(f"| {r['instance_id']}, {r['qid']}: {_short(r['question'], 120)} | "
                       f"{_short(r['response'])} | {r['aligned']:.0f} / {r['coherent']:.0f} | "
                       f"{'n/a' if A.pd.isna(r['em']) else ('yes' if bool(r['em']) else 'no')} | "
                       f"{score['A_org'][i]:.2f} / {score['T2'][i]:.1f} |")
    pairs = sorted({p for p in P.loc[P["set"] == "test", "pair_id"]})
    tp = {r["pair_id"]: r for r in A.K.read_jsonl(A.K.TEST_PAIRS)}
    out.append("\n## TEST cue pairs (random draw) with each detector's score, first question\n")
    for pid in rng.sample(pairs, 4):
        out.append(f"\n**{pid}**\n")
        for side in ("unobserved", "observed"):
            sub = P[(P["set"] == "test") & (P["text_id"] == f"{pid}:{side}")]
            i = int(sub["i"].iloc[0])
            sc = ", ".join(f"{n} {score[n][i]:.2f}" for n in sorted(score)
                           if np.isfinite(score[n][i]))
            out.append(f"- {side}: `{tp[pid][side].replace(chr(10), ' | ')}`  \n  {sc}")
    A.OUT.mkdir(parents=True, exist_ok=True)
    (A.OUT / "samples.md").write_text("\n".join(out) + "\n")
    print(json.dumps({"written": str(A.OUT / "samples.md")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
