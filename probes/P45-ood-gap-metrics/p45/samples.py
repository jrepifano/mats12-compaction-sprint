"""P45 raw samples for the note (seeded, every drawn row shown).

Population: the test-split rows of planted_med and planted_law2 (common valid-row mask). Draw: 8
rows per category, seeded (SEED), sorted by pid. Per row: the frozen winner's and S3's percentile
rank among the test pool (length-controlled scores), and the first 220 characters of the text.
Output: results/analysis/samples_planted.md.  Usage: uv run python -m p45.samples
"""

from __future__ import annotations

import json

import numpy as np

from p45 import analyze as AN
from p45 import contracts as K

SEED = 20261020
N = 8


def main() -> None:
    p = AN.Paths()
    best = AN.require_selection(p)["best"]
    df = AN.load_prep(p)
    test = df[(df["split"] == "test") & df["valid"]]
    pool = test[test["set"] == K.P42K.POOL]
    ref = {c: np.sort(pool[c + "__lc"].to_numpy(float)) for c in (best, "maha_L24")}
    texts = {}
    for path in (p.p42_prompts, p.law2):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if r["set"] in K.PLANTED_EVAL:
                texts[r["pid"]] = r["text"]
    rng = np.random.default_rng(SEED)
    out = [f"# P45 raw samples: {N} seeded test rows per planted category (seed {SEED})", "",
           f"Percentile rank among the {len(pool):,} test-pool prompts (length-controlled; "
           f"100 = most OOD). Winner = {best}; S3 = maha_L24.", ""]
    for cat in K.PLANTED_EVAL:
        rows = test[test["set"] == cat]
        pick = rows.iloc[np.sort(rng.choice(len(rows), N, replace=False))].sort_values("pid")
        out += [f"## {cat}", "", "| pid | tokens | winner pct | S3 pct | text |", "|---|---|---|---|---|"]
        for _, r in pick.iterrows():
            pw = 100 * np.searchsorted(ref[best], r[best + "__lc"], side="right") / len(ref[best])
            ps = 100 * np.searchsorted(ref["maha_L24"], r["maha_L24__lc"], side="right") / \
                len(ref["maha_L24"])
            t = " ".join(texts[r["pid"]].split())[:220].replace("|", "/")
            out.append(f"| {r['pid']} | {int(r['n_tokens'])} | {pw:.1f} | {ps:.1f} | {t} |")
        out.append("")
    (K.ANALYSIS_DIR / "samples_planted.md").write_text("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
