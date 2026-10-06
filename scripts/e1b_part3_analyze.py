"""E1b Part 3 analysis: restore control (a), first-tokens activation test (b) with sufficiency,
and the steering transfer label (prereg §4.9, §4.10; plan U6b).

Gated (prereg §4.9, §7): refuses, exit 2, before reading any Part 3 result unless
`C.PART3_UNBLIND` exists AND `C.ROBUSTNESS_FILE` records base, SFT and Instruct seed 2 each as
clear EM in plain text. The computations live in `emx.e1b.part3` (its docstring lists the
readings chosen there). Writes `results/E1b/part3/analysis.json` with an `inputs` block (sha256
of every file read).

    uv run python scripts/e1b_part3_analyze.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from emx.contracts import CellId, gen_path, judged_path
from emx.e1.analysis import git_commit
from emx.e1b import contracts as C
from emx.e1b import part3 as P
from emx.judge_run import sha256_file

ROOT = Path(__file__).resolve().parents[1]
RESTORE_QSETS = ("general", "core44")  # sampled 8 x 50; greedy 44 (descriptive)


def restore_frames(root: Path, cell: str, inputs: dict) -> dict[str, dict]:
    """{qset: {kind: frame}} for `{cell}@chat+restore={kind}`."""
    out: dict[str, dict] = {q: {} for q in RESTORE_QSETS}
    for kind in C.RESTORE_KINDS:
        cond = f"{cell}@chat+restore={kind}"
        for q in RESTORE_QSETS:
            name = f"{cond}__{q}.csv"
            f, inp = P.load_scored(root / C.RESTORE_OUT_DIR / name,
                                   root / C.PART3_JUDGED_DIR / name, root)
            out[q][kind] = f
            inputs.update(inp)
    return out


def new41_qids(root: Path, inputs: dict) -> set[str]:
    p = root / C.QUESTION_FILES["ext48"]
    if not p.exists():
        raise P.Part3Refused(f"missing {p}")
    inputs[P.rel_path(p, root)] = sha256_file(p)
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return {str(r["qid"]) for r in rows if r.get("tag") != C.EXT48_MEDICAL_TAG}


def steer_pairs(root: Path, cell: str, inputs: dict):
    """((steered, baseline) on the new 41, (steered, baseline) on general). Baseline: the untuned
    stage model in chat format, `{stage}-untrained@chat`: ext48 from the E1 re-evaluation
    (new-41 questions only) and E1's general."""
    base_cond = f"{CellId.parse(cell).stage}-untrained@{C.STEER_FMT}"
    keep = new41_qids(root, inputs)
    pairs = []
    for qset, b_qset, exp in (("new41", "ext48", C.EXP), ("general", "general", "E1")):
        name = f"{C.steer_condition(cell)}__{qset}.csv"
        s, inp = P.load_scored(root / C.steer_eval_path(cell, qset),
                               root / C.PART3_JUDGED_DIR / name, root)
        inputs.update(inp)
        b, inp = P.load_scored(root / gen_path(base_cond, b_qset, exp),
                               root / judged_path(base_cond, b_qset, exp), root)
        inputs.update(inp)
        if qset == "new41":
            extra = sorted(set(s["qid"]) - keep)
            if extra:
                raise P.Part3Refused(f"steered new-41 file has non-new-41 qids {extra[:5]}")
            b = b[b["qid"].isin(keep)].reset_index(drop=True)
        pairs.append((s, b))
    return pairs[0], pairs[1]


def run(root: Path, cells: list[str], layers: list[int]) -> dict:
    inputs = P.check_gate(root)  # before anything else is read
    out: dict = {"utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "git_commit": git_commit(root), "headline_judge": P.HEADLINE_JUDGE,
                 "primary_layer": C.PRIMARY_LAYER, "readings": P.__doc__[P.__doc__.index("Readings"):],
                 "restore": {}, "firstk": {}, "steer": {}, "inputs": inputs}
    ap = root / C.ALPHA_FILE
    if not ap.exists():
        raise P.Part3Refused(f"missing {ap}")
    alpha = json.loads(ap.read_text())["alpha"]
    inputs[C.ALPHA_FILE] = sha256_file(ap)
    for cell in cells:
        fr = restore_frames(root, cell, inputs)
        out["restore"][cell] = P.analyze_restore(fr["general"], fr["core44"])
        out["firstk"][cell] = {}
        for layer in layers:
            fp = root / C.firstk_path(cell, layer)
            if not fp.exists():
                raise P.Part3Refused(f"missing {fp}")
            inputs[C.firstk_path(cell, layer)] = sha256_file(fp)
            with np.load(fp) as z:
                res = P.analyze_firstk({k: z[k] for k in z.files}, require_complete=True)
            res["role"] = "decision" if layer == C.PRIMARY_LAYER else "secondary"
            out["firstk"][cell][f"L{layer}"] = res
        if cell not in alpha:
            raise P.Part3Refused(f"{C.ALPHA_FILE} has no alpha for {cell}")
        a = float(alpha[cell])
        pairs = (None, None) if a == 0 else steer_pairs(root, cell, inputs)
        out["steer"][cell] = P.analyze_steer(a, *pairs)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--cells", nargs="+", default=list(C.PART3_SOURCE_CELLS))
    ap.add_argument("--layers", nargs="+", type=int, default=list(C.LAYERS))
    args = ap.parse_args(argv)
    try:
        out = run(args.root, args.cells, args.layers)
    except P.Part3Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    p = args.root / P.ANALYSIS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, default=float) + "\n")
    for cell in args.cells:
        fk = out["firstk"][cell]
        print(f"== {cell}: (a) {out['restore'][cell]['label']} | (b) L{C.PRIMARY_LAYER} "
              f"{fk[f'L{C.PRIMARY_LAYER}']['label']}, sufficiency "
              f"{fk[f'L{C.PRIMARY_LAYER}']['sufficiency_label']} | steering "
              f"{out['steer'][cell]['label']}")
    print("wrote", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
