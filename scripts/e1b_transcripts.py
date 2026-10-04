"""Random raw transcripts of every final E1b bad model, for Jacob to read before any claim (E1b
prereg §4.12 "Transcripts before any claim").

Port of scripts/e1_transcripts.py (its `sample_groups` and `render` are reused unchanged): for
each final bad model (registry groups `finals`, `cond_seeds`, `cond_mid`) that has judged general
answers, 10 EM and 10 aligned answers drawn uniformly at random under the headline judge, with a
seed derived from `e1reg.SEEDS["transcripts"]` and the condition id; never hand-picked. If fewer
than 10 exist in a group, all are taken and the shortfall is stated. Inputs pass the analysis's
provenance checks (`emx.e1b.analysis.Loader`). Their hashes go to
results/E1b/MANIFEST.transcripts.sha256.

Usage:
  uv run python scripts/e1b_transcripts.py [--judge luna]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from emx.contracts import JUDGES
from emx.e1b import analysis as A
from emx.e1b import contracts as C
from emx.e1b import registry as R

OUT_DIR = f"{C.RESULTS}/transcripts"
_E1 = Path(__file__).resolve().with_name("e1_transcripts.py")
_spec = importlib.util.spec_from_file_location("e1_transcripts", _E1)
E1T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E1T)


def final_bad_conditions() -> list[str]:
    groups = R.conditions("finals") + R.conditions("cond_seeds") + R.conditions("cond_mid")
    return [str(c) for c in groups if not c.cell.untrained and c.cell.data == "bad"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--judge", choices=JUDGES, default="luna")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root)
    out_dir = root / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ld = A.Loader(root)
    written = []
    for cond in final_bad_conditions():
        if not ld.has(cond, "general"):
            print(f"skip {cond}: no generations")
            continue
        try:
            frame = ld.pair(cond, "general")
        except A.AnalysisRefused as e:
            print(f"REFUSED {cond}: {e}", file=sys.stderr)
            return 2
        groups = E1T.sample_groups(frame, args.judge, cond)
        text = E1T.render(cond, args.judge, groups, frame)
        path = out_dir / f"{cond}.md"
        path.write_text(text.replace("(prereg §4.10)", "(E1b prereg §4.12)"))
        written.append(path)
        print(f"{cond}: {len(groups['em'][0])} EM, {len(groups['aligned'][0])} aligned -> {path}")
    if written:
        print(f"manifest -> {A.write_manifest(root, 'transcripts', written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
