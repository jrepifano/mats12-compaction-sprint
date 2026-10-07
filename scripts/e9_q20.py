"""E9 standard fine-tunes and answer-pair scoring on the pod (unit E1; prereg §4.5-§4.7, §4.13).

    uv run python scripts/e9_q20.py q20 --run-dir DIR --arm ARM --cpt-seed S --base <dir|hub>
        [--seeds 11 12 13] [--datas bad good]
    e9_q20.py full --run-dir DIR --arm ARM --cpt-seed S --base <dir|hub> [--datas bad good]
        [--no-push] [--noise-floor-file determinism.json]
    e9_q20.py determinism --run-dir DIR [--arm ckpt --cpt-seed 0 --base hub]
    e9_q20.py e1b-gap --run-dir DIR
    e9_q20.py score-base --run-dir DIR --arm ARM --cpt-seed S --base <dir|hub>
    e9_q20.py merge --run-dir DIR

Common options: --root (repo root the data and pair paths are relative to; /ws/emx on a pod),
--pairs, --swaps, --questions-root, --batch, --adapters-root (default /ws/adapters/e9: adapters
live outside the run directory, whose every file DONE lists), --no-verify-base.

- `q20`: one 20-step fine-tune per (adapter seed, data), scored at steps 0, 2, 5 and 20
  (`emx.e9.finetune.run_finetune`); records `ft/{d}-a{k}.json`; then `scores/q.csv` is merged.
- `full`: the 1,287-step fine-tunes at adapter seed 11 (step-20 adapter saved fp32 in
  `step-0020/`), the consistency check against the seed-11 Q₂₀ run (`ft/consistency.json`), and,
  unless `--no-push`, the upload to private repos `{HF_USERNAME}/e9-{run_id}-{d}` (asserted
  private before training and before the upload; `hf_repos.json`). Exit 3 if the consistency
  check fails (everything else is still written and pushed). `--max-steps N` makes it a timing
  run instead (the smoke's bad + good concurrency test): the same recipe and 1,287-step schedule
  stopped after N steps, record `ft/full-{d}-a11-max{N}.json`, scores in `scores/timing/` (never
  merged), no push and no consistency check.
- `determinism` (smoke): repeats the Q₂₀ run (bad, seed 11) → `determinism.json`.
- `e1b-gap` (smoke): E1b's seed-2 step-20 adapters on the checkpoint → `e1b_gap.json` (its
  `pass` is the endpoint check; the command exits 0 whenever the gap was measured).
- `score-base`: the base model's own rows (e.g. `--arm ckpt --cpt-seed 0 --base hub` on every
  continued-pretraining pod, for the pre-fine-tune stance).
- `merge`: rebuild `scores/q.csv` from the complete parts.

Exit 0 only on success; 1 on any failure; 3 on a failed consistency check. Log lines are tagged
E9_Q20. Reads HF_USERNAME and HF_TOKEN from the environment only for `full` without --no-push.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e9 import contracts as C
from emx.e9 import finetune as F
from emx.e9 import score_pod as SP

EXIT_CONSISTENCY = 3


def _common(ap: argparse.ArgumentParser, arm: bool = True, arm_required: bool = True) -> None:
    ap.add_argument("--run-dir", required=True)
    if arm:
        ap.add_argument("--arm", required=arm_required, default=None if arm_required else "ckpt")
        ap.add_argument("--cpt-seed", type=int, required=arm_required,
                        default=None if arm_required else 0)
        ap.add_argument("--base", required=arm_required, default=None if arm_required else "hub",
                        help="'hub' (the pinned checkpoint) or a continued-pretrained directory")
    ap.add_argument("--root", default=".")
    ap.add_argument("--pairs", default=None)
    ap.add_argument("--swaps", default=None)
    ap.add_argument("--questions-root", default=None)
    ap.add_argument("--batch", type=int, default=SP.SCORE_BATCH)
    ap.add_argument("--adapters-root", default=F.ADAPTERS_ROOT)
    ap.add_argument("--no-verify-base", action="store_true",
                    help="record the base's SHA256SUMS digest without re-hashing its files")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("q20", help="20-step fine-tunes, scored at 0/2/5/20")
    _common(p)
    p.add_argument("--seeds", type=int, nargs="+", default=list(C.ADAPTER_SEEDS))
    p.add_argument("--datas", nargs="+", default=list(C.DATAS), choices=C.DATAS)
    p = sub.add_parser("full", help="1,287-step fine-tunes at adapter seed 11")
    _common(p)
    p.add_argument("--datas", nargs="+", default=list(C.DATAS), choices=C.DATAS)
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--max-steps", type=int, default=None,
                   help="timing run: the full recipe (schedule unchanged) stopped after N steps; "
                        "own record ft/full-{d}-a11-maxN.json, no push, no consistency check")
    p.add_argument("--noise-floor-file", default=None,
                   help="determinism.json (default: <run dir>/determinism.json)")
    p = sub.add_parser("determinism", help="repeat the bad seed-11 Q20 run (smoke)")
    _common(p, arm_required=False)
    p = sub.add_parser("e1b-gap", help="E1b's step-20 adapters on the checkpoint (smoke)")
    _common(p, arm=False)
    p = sub.add_parser("score-base", help="a base model's own reference rows")
    _common(p)
    p = sub.add_parser("merge", help="rebuild scores/q.csv from the complete parts")
    p.add_argument("--run-dir", required=True)
    return ap


def _ctx(args, base):
    tok = F.load_pinned_tokenizer()
    return F.score_context(base, tok, args.root, args.pairs, args.swaps, args.questions_root,
                           args.batch)


def _base(args):
    return F.resolve_base(args.base, verify=not args.no_verify_base)


def cmd_q20(args) -> int:
    base = _base(args)
    F.check_arm(args.arm, args.cpt_seed, base)
    ctx = _ctx(args, base)
    for k in args.seeds:
        if k not in C.ADAPTER_SEEDS:
            raise SystemExit(f"adapter seed {k} not in {C.ADAPTER_SEEDS}")
        for d in args.datas:
            F.run_finetune(args.run_dir, args.arm, args.cpt_seed, d, k, base, ctx, "q20",
                           args.root, args.adapters_root)
    SP.merge_scores(args.run_dir)
    return 0


def cmd_full(args) -> int:
    base = _base(args)
    F.check_arm(args.arm, args.cpt_seed, base)
    rid = C.run_id(args.arm, args.cpt_seed)
    k = C.FULL_FT_ADAPTER_SEED
    if args.max_steps is not None:
        return _full_timing(args, base, rid, k)
    user = None if args.no_push else F.hf_user()
    repos = {d: F.hub_repo_name(rid, d, user or "local") for d in args.datas}
    if not args.no_push:
        for d in args.datas:  # before any training (CLAUDE.md "Visibility")
            F.ensure_private_repo(repos[d])
    ctx = _ctx(args, base)
    infos = {}
    for d in args.datas:
        infos[d] = F.run_finetune(args.run_dir, args.arm, args.cpt_seed, d, k, base, ctx, "full",
                                  args.root, args.adapters_root, hub_repo=repos[d])
    cons = F.update_consistency(args.run_dir, args.arm, args.cpt_seed, args.datas,
                                args.noise_floor_file)
    SP.log(f"CONSISTENCY {'PASS' if cons['pass'] else 'FAIL'} tol {cons['tolerance']:.3g}: "
           + ", ".join(f"{d} {v.get('abs_diff')!r}" for d, v in cons["per_data"].items()))
    if not args.no_push:
        for d in args.datas:
            rev = F.push_adapters(infos[d]["output_dir"], repos[d], args.run_dir,
                                  f"E9 full fine-tune {rid} {d} a{k} (root + step-0020)")
            F.mark_pushed(infos[d]["record"], repos[d], rev)
    SP.merge_scores(args.run_dir)
    return 0 if all(cons["per_data"][d]["pass"] for d in args.datas) else EXIT_CONSISTENCY


def _full_timing(args, base, rid: str, k: int) -> int:
    """`full --max-steps N`: shortened full-recipe runs for timing (never pushed, no consistency
    check, scores kept apart from q.csv)."""
    if args.max_steps < 1:
        raise SystemExit(f"--max-steps {args.max_steps}: must be >= 1")
    ctx = _ctx(args, base)
    for d in args.datas:
        info = F.run_finetune(args.run_dir, args.arm, args.cpt_seed, d, k, base, ctx, "full",
                              args.root, args.adapters_root, max_steps=args.max_steps)
        SP.log(f"TIMING {rid} {info['cell']}: {info['last']} of {info['total']} steps -> "
               f"{info['record']}")
    return 0


def cmd_determinism(args) -> int:
    base = _base(args)
    F.check_arm(args.arm, args.cpt_seed, base)
    ctx = _ctx(args, base)
    F.run_determinism(args.run_dir, args.arm, args.cpt_seed, base, ctx, root=args.root,
                      adapters_root=args.adapters_root)
    return 0


def cmd_e1b_gap(args) -> int:
    ctx = _ctx(args, F.resolve_base("hub"))
    F.run_e1b_gap(args.run_dir, ctx)
    SP.merge_scores(args.run_dir)
    return 0


def cmd_score_base(args) -> int:
    base = _base(args)
    ctx = _ctx(args, base)
    F.run_score_base(args.run_dir, args.arm, args.cpt_seed, base, ctx)
    SP.merge_scores(args.run_dir)
    return 0


def cmd_merge(args) -> int:
    res = SP.merge_scores(args.run_dir)
    return 0 if not res["skipped"] else 1


COMMANDS = {"q20": cmd_q20, "full": cmd_full, "determinism": cmd_determinism,
            "e1b-gap": cmd_e1b_gap, "score-base": cmd_score_base, "merge": cmd_merge}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["WANDB_MODE"] = "offline"
    try:
        rc = COMMANDS[args.cmd](args)
    except SystemExit as e:
        SP.log(f"FAIL {args.cmd}: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - tagged failure line, non-zero exit
        traceback.print_exc()
        SP.log(f"FAIL {args.cmd}: {type(e).__name__}: {e}")
        return 1
    SP.log(f"{'OK' if rc == 0 else 'FAIL'} {args.cmd} rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
