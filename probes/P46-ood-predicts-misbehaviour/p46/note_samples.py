"""The note's sample table: every row of p46.samples' seeded draw (results/judged_text/samples.md,
gitignored because it holds harmful text), as id, style, both judges' labels and the first 120
characters of the response, so the labels can be checked without committing full harmful
completions. Re-runs p46.samples' draw (same seed, same inputs), so the rows are identical.

    python -m p46.note_samples   -> results/analysis/samples_table.md
"""

from __future__ import annotations

import sys

from p46 import contracts as K
from p46 import samples as S

HEAD = 120


def main() -> int:
    items = S.build_items() if hasattr(S, "build_items") else None
    if items is None:
        raise SystemExit("p46.samples exposes no build_items(); see the module")
    rows = S.draw(items, K.N_SAMPLES_NOTE, K.SEED_SAMPLE)
    out = ["| # | response id | style | primary | luna | response, first 120 characters |",
           "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows.itertuples(index=False), 1):
        text = " ".join(str(r.answer).split())[:HEAD].replace("|", "/")
        luna = "" if r.luna is None or r.luna != r.luna else int(r.luna)
        out.append(f"| {i} | `{r.key}` | {r.style} | {int(r.score)} | {luna} | {text} |")
    path = K.ANALYSIS_DIR / "samples_table.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
