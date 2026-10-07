"""E3 sampled secondary measures: generation wrapper (prereg §4.9; plan v2 fix 2, v3 fix 1).

  python scripts/e3_gen.py --ckpt pre --cells pre-plain-bad-R1-s2 pre-plain-good-R1-s2 \
      [--qsets general ext48 narrow] [--limit-answers 200] [--train-dir D] [--out-dir D] \
      [--selection-file F] [--dry-run] [--overwrite]

What each final adapter is asked (`contracts.SAMPLED_QSETS`, 1,220 answers): the 8 general
questions x 20, the 48 ext48 questions x 20, and narrow x 1 on the first 100 questions, in file
order, of `QUESTION_FILES["narrow"]` that are not among the selection file's 40 `medical_qids`
(never the retained pairs, so a medical question dropped at retention stays excluded). Sampling
is `contracts.SAMPLING` (= E1's registry: temperature 1, no top-p/top-k cutoff, 600 new tokens),
plain format with the registry's stop strings, per-request seeds `request_seed(condition, qid,
sample_idx)` from evaluation seed 20260928, as E1. Outputs: `contracts.gen_path(cell, qset)` (a
sealed checkpoint's under `results/E3/sealed/gen`) with gen_common's meta sidecars and manifests.

The work is done by scripts/e1_gen_vllm.py with `--exp E3` (emx.gen_common's E3 path). This
wrapper checks the question files' pinned sha256, prints the request plan and starts **one
subprocess per checkpoint** (one vLLM engine per process: a second stage's engine cannot start
while an earlier one is resident, E1b S2), with both adapters of that checkpoint served as LoRAs
by the same engine. `--limit-answers N` (smoke test, §4.13) asks the general questions only,
N / 8 samples each, and is recorded in the meta as an override.

Resume (code review B4, B7): the engine skips a (cell, qset) whose output verifies against this
run (gen_common's contract, for E3 including the selection file's sha256 and the exact request
list's hash) and, through `--set-aside-incomplete`, moves an unverified one (a crash between the
CSV and meta writes, other inputs) to `<name>.incomplete.<UTC>` before regenerating it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # the pod's vLLM venv runs this without an install

from emx.contracts import CellId, request_seed
from emx.e1.registry import SAMPLING as REGISTRY_SAMPLING
from emx.e3 import contracts as C

ENGINE_SCRIPT = ROOT / "scripts/e1_gen_vllm.py"
EVAL_FMT = "plain"
assert C.SAMPLING == REGISTRY_SAMPLING, "E3 sampling must be E1's registry sampling"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def verify_questions(qsets, root: Path = ROOT) -> None:
    for q in qsets:
        got = _sha(root / C.QUESTION_FILES[q])
        if got != C.QUESTION_SHA256[q]:
            raise SystemExit(f"{C.QUESTION_FILES[q]}: sha256 {got[:12]} != pinned")


def medical_qids(selection_file: Path) -> list[str]:
    qids = json.loads(Path(selection_file).read_text())["medical_qids"]
    if len(qids) != C.N_MEDICAL or len(set(qids)) != len(qids):
        raise SystemExit(f"{selection_file}: medical_qids must be {C.N_MEDICAL} distinct qids")
    return list(qids)


def questions(qset: str, question_files: dict, selection_file: Path) -> list[str]:
    """The qids asked of every adapter for one qset, in file order."""
    rows = _rows(question_files[qset])
    if qset == "narrow":
        drop = set(medical_qids(selection_file))
        rows = [r for r in rows if r["qid"] not in drop]
    n_q = C.SAMPLED_QSETS[qset][1]
    rows = rows[:n_q] if n_q else rows
    return [r["qid"] for r in rows]


def request_plan(
    cell_id: str, qsets, question_files: dict, selection_file: Path, samples: int | None = None
) -> list[tuple[str, str, int, int]]:
    """(qset, qid, sample_idx, seed) of every request for one adapter, in generation order."""
    cond = f"{cell_id}@{EVAL_FMT}"
    out = []
    for qset in qsets:
        n = samples or C.SAMPLED_QSETS[qset][0]
        for qid in questions(qset, question_files, selection_file):
            out += [(qset, qid, i, request_seed(cond, qid, i)) for i in range(n)]
    return out


def group_by_checkpoint(ckpts, cells) -> dict[str, list[str]]:
    """{ckpt: its final cells}, in --ckpt order; every cell must be a final E3 run of one of them."""
    out: dict[str, list[str]] = {ck: [] for ck in ckpts}
    for c in cells:
        cid = CellId.parse(c)
        if cid.untrained or cid.step is not None or cid.fmt != C.FMT or cid.recipe != C.RECIPE:
            raise SystemExit(f"{c}: not a final E3 adapter")
        if cid.stage not in out:
            raise SystemExit(f"{c}: its checkpoint {cid.stage} is not in --ckpt {list(ckpts)}")
        out[cid.stage].append(c)
    empty = [ck for ck, cs in out.items() if not cs]
    if empty:
        raise SystemExit(f"no cells for checkpoints {empty}")
    return out


def engine_argv(cells, qsets, args, samples: int | None) -> list[str]:
    argv = [sys.executable, str(ENGINE_SCRIPT), "--exp", "E3",
            "--conditions", *[f"{c}@{EVAL_FMT}" for c in cells], "--qsets", *qsets,
            "--set-aside-incomplete"]
    if samples is not None:
        argv += ["--samples", str(samples)]
    for flag, val in (("--out-dir", args.out_dir), ("--selection-file", args.selection_file)):
        if val:
            argv += [flag, str(val)]
    if args.train_dir:
        argv += ["--train-dir", str(args.train_dir)]
    if args.overwrite:
        argv.append("--overwrite")
    return argv


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0], formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("--ckpt", nargs="+", required=True, choices=C.GRID)
    ap.add_argument("--cells", nargs="+", required=True, help="final adapters (seed 2)")
    ap.add_argument("--qsets", nargs="+", default=list(C.SAMPLED_QSETS),
                    choices=list(C.SAMPLED_QSETS))
    ap.add_argument("--limit-answers", type=int, default=None, help="smoke: N general answers")
    ap.add_argument("--train-dir", default=None, help=f"default {C.TRAIN_DIR}")
    ap.add_argument("--out-dir", default=None, help="default: contracts.gen_path's dir")
    ap.add_argument("--selection-file", default=None, help=f"default {C.SELECTION_FILE}")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and commands only")
    return ap


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    args = parser().parse_args(argv)
    groups = group_by_checkpoint(args.ckpt, args.cells)
    qsets, samples = list(args.qsets), None
    if args.limit_answers is not None:
        n_general = len(_rows(ROOT / C.QUESTION_FILES["general"]))
        if args.limit_answers % n_general:
            raise SystemExit(f"--limit-answers must be a multiple of {n_general}")
        qsets, samples = ["general"], args.limit_answers // n_general
    verify_questions(qsets)
    sel = Path(args.selection_file or ROOT / C.SELECTION_FILE)
    files = {q: ROOT / f for q, f in C.QUESTION_FILES.items()}
    for cells in groups.values():
        for c in cells:
            plan = request_plan(c, qsets, files, sel, samples)
            per = {q: sum(1 for r in plan if r[0] == q) for q in qsets}
            print(f"E3_GEN_PLAN {c} {len(plan)} {json.dumps(per)}", flush=True)
        cmd = engine_argv(cells, qsets, args, samples)
        print("+ " + " ".join(cmd), flush=True)
        if not args.dry_run:
            run(cmd, check=True, cwd=ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
