"""E13 laptop inputs, deterministic and API-free (prereg §4.1, §4.2, §4.6 item 1; implementation
plan §1 unit B). Logic in `emx.e13.inputs`.

Subcommands:
  alpaca    free HF download of the Alpaca data file at ALPACA_REPO @ ALPACA_REV into
            results/E13/inputs/alpaca_raw/ (gitignored); records repo, revision, file and
            sha256 in results/E13/inputs/alpaca_source.json. `--file PATH` records a local copy
            instead (no network); `--repo/--rev` override the pin (recorded; a design change).
  build     needs notices.jsonl with survival ok and alpaca_source.json. Writes
            results/E13/inputs/train/{org,good,trig}.jsonl, heldout_loss.jsonl, tasks.jsonl,
            dircells.json and checks.json (prereg §4.6 item 1, rendered through the pinned
            tokenizer when it is in the local HF cache; `--require-tokenizer` refuses otherwise).
  manifest  results/E13/inputs/manifest.json from the files on disk, written atomically;
            `--require-style` refuses unless both style feature lists (unit B2) exist;
            hashes the Amendment D inferred pairs, survival record and frozen prompts when
            present, and `--require-inferred` refuses unless they exist.

Usage:
  uv run python scripts/e13_inputs.py alpaca [--file PATH] [--repo R --rev REV]
  uv run python scripts/e13_inputs.py build [--pairs-root DIR] [--require-tokenizer]
  uv run python scripts/e13_inputs.py manifest [--require-style] [--require-inferred]
                                               [--pairs-root DIR]
  (all take --root DIR, default the repo)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from emx.e13 import contracts as C
from emx.e13 import inputs as I
from emx.e13 import notices as N


def _root(args) -> Path:
    return Path(args.root) if args.root else C.REPO_ROOT


def cmd_alpaca(args) -> int:
    root = _root(args)
    dest = root / I.ALPACA_DIR
    dest.mkdir(parents=True, exist_ok=True)
    if args.file:
        src = Path(args.file)
        path = dest / src.name
        if src.resolve() != path.resolve():
            shutil.copyfile(src, path)
        repo, rev = args.repo or "local", args.rev or ""
    else:
        repo, rev = args.repo or C.ALPACA_REPO, args.rev or C.ALPACA_REV
        path = I.download_alpaca(dest, repo, rev)
    rows = I.load_alpaca(path)
    rec = {"repo": repo, "rev": rev, "file": I.rel_path(root, Path(path)),
           "sha256": I.sha256_file(path), "n_rows": len(rows),
           "n_eligible": len(I.alpaca_eligible(rows)),
           "pinned": repo == C.ALPACA_REPO and rev == C.ALPACA_REV, "utc": I.utc_now()}
    N.write_json(root / I.ALPACA_SOURCE, rec)
    print(f"E13_ALPACA {json.dumps(rec)}")
    return 0


def _tokenizer(required: bool):
    from transformers import AutoTokenizer

    from emx.qwen import registry as R

    try:
        return AutoTokenizer.from_pretrained(R.TOKENIZER_ID, revision=R.TOKENIZER_REV,
                                             local_files_only=True)
    except OSError as e:
        if required:
            raise SystemExit(f"E13_FAIL pinned tokenizer not cached: {e}") from e
        print("E13_WARN pinned tokenizer not cached: render checks skipped", file=sys.stderr)
        return None


def cmd_build(args) -> int:
    root = _root(args)
    surv = json.loads((root / N.SURVIVAL_PATH).read_text(encoding="utf-8"))
    if not surv["ok"]:
        print("E13_STOP notice survival not ok: no inputs are built", file=sys.stderr)
        return 1
    notice_rows = N.read_notices(root / N.NOTICES_PATH)
    kept = I.Kept(notice_rows)
    pairs = I.load_pairs(args.pairs_root)
    holdout = I.load_holdout(root / C.HOLDOUT_FILE)
    train_pairs = I.split_pairs(pairs, holdout)
    train = {arm: I.build_train_rows(arm, train_pairs, kept) for arm in C.ARMS}
    heldout = I.heldout_loss_rows(holdout, kept)
    src = json.loads((root / I.ALPACA_SOURCE).read_text(encoding="utf-8"))
    apath = root / src["file"]
    if I.sha256_file(apath) != src["sha256"]:
        raise SystemExit(f"E13_FAIL {apath} sha256 differs from alpaca_source.json")
    tasks = I.build_tasks(holdout, I.load_alpaca(apath))
    dircells = C.direction_cell_instances(kept.ids)
    tok = _tokenizer(args.require_tokenizer)
    checks = I.check_inputs(train, heldout, tasks, holdout, notice_rows, tok=tok)
    for arm, rows in train.items():
        sha = N.write_jsonl(root / C.TRAIN_ROWS_DIR / f"{arm}.jsonl", rows)
        print(f"E13_TRAIN {arm}: {len(rows)} rows {sha}")
    print(f"E13_HELDOUT {len(heldout)} rows {N.write_jsonl(root / C.HELDOUT_ROWS, heldout)}")
    print(f"E13_TASKS {len(tasks)} rows {N.write_jsonl(root / I.TASKS_PATH, tasks)}")
    N.write_json(root / I.DIRCELLS_PATH, {
        "instances": list(dircells),
        "families": [f.family_id for f in C.families("dev", "noov")],
        "seed": C.DATA_SEED, "rule": "contracts.direction_cell_instances",
    })
    N.write_json(root / I.CHECKS_PATH, {**checks, "utc": I.utc_now()})
    print(f"E13_DONE build checks {json.dumps(checks)}")
    return 0


def cmd_manifest(args) -> int:
    root = _root(args)
    checks_p = root / I.CHECKS_PATH
    checks = json.loads(checks_p.read_text(encoding="utf-8")) if checks_p.exists() else {}
    try:
        m = I.build_manifest(root, pairs_root=args.pairs_root, require_style=args.require_style,
                             checks=checks, require_inferred=args.require_inferred)
    except FileNotFoundError as e:
        print(f"E13_FAIL {e}", file=sys.stderr)
        return 1
    sha = I.write_manifest(root, m)
    print(f"E13_DONE manifest {C.INPUT_MANIFEST} {sha}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("alpaca")
    a.add_argument("--file", default=None)
    a.add_argument("--repo", default=None)
    a.add_argument("--rev", default=None)
    b = sub.add_parser("build")
    b.add_argument("--pairs-root", default=None)
    b.add_argument("--require-tokenizer", action="store_true")
    m = sub.add_parser("manifest")
    m.add_argument("--pairs-root", default=None)
    m.add_argument("--require-style", action="store_true")
    m.add_argument("--require-inferred", action="store_true")
    for p in (a, b, m):
        p.add_argument("--root", default=None, help="results root (default: the repo)")
    args = ap.parse_args(argv)
    return {"alpaca": cmd_alpaca, "build": cmd_build, "manifest": cmd_manifest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
