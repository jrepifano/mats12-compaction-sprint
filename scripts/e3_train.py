"""Train one E3 adapter, check it and push it; plus the pod helpers the E3 job calls.

Prereg §4.2 (recipe R1, one epoch, held-out loss at steps 0, 50, ..., 400, 429; step 50 and 429
saved), §4.6 (training status), §4.13 (loss and update checks); plan v2 fixes 6 and 11, v3 fix 8.
Port of scripts/e1b_train.py and scripts/e1b_losscheck.py (the checks live in `emx.e3.hooks`).

  uv run python scripts/e3_train.py --config configs/E3/<cell>.json [--max-steps N] [--no-push]
      [--smoke-repo REPO] [--results-dir results/E3/train] [--output-dir DIR]
    1. unless --no-push: create the Hub repo private (exist_ok) and assert it is private, BEFORE
       training (CLAUDE.md "Visibility");
    2. `emx.train.train` with checkpoint steps `hooks.checkpoint_plan(last)` and `hooks.E3Hook`
       (held-out loss at 0 and every checkpoint; step-0050/ saved fp32; adapter norm per row);
    3. the loss check -> `{results-dir}/{cell}.losscheck.json` (`status` trained|failed);
    4. unless --no-push, and only if the check passed (or under --smoke-repo, which pushes to that
       private scratch repo whatever the check says): push the adapter folder (root + step-0050/)
       in one commit, timed; the record becomes `pushed` with the commit sha.
    Exit 0 when the check passed (smoke: whenever training finished), 1 otherwise.
  e3_train.py --push-only --config C [...]   push an already trained and checked run (a failed push)
  e3_train.py --verify-inputs                every pinned input's sha256 (contracts.input_sha256()),
      and the pairs, selection and swaps files against results/E3/MANIFEST.pairs.sha256
      (`<sha256>  <repo-relative path>` lines; the pairs entry must equal contracts.PAIRS_SHA256
      once that is pinned)
  e3_train.py --download CKPT                snapshot of the checkpoint at its pinned commit
  e3_train.py --delete-checkpoint CKPT       remove that revision from the HF cache
  e3_train.py --wait-hub-adapter CELL [--timeout-s 5400] [--poll-s 60]
      prints `REPO@REV` once the run's private repo holds a final adapter and its step-0050/;
      exit 3 on timeout (pod 2's cross-pod rescore of pod 1's reference adapter, §4.13)

Retries are the pod job's (scripts/pod_run_e3.sh), not this script's.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from emx.e3 import contracts as C
from emx.e3 import hooks as H

ROOT = Path(__file__).resolve().parents[1]
EVAL_STEPS = C.EVAL_STEPS  # tests patch this for a tiny run


def _utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- Hub ----------------------------------------------------------------------------------------


class NotPrivateError(RuntimeError):
    """A Hub repo that must be private is not."""


def precreate_private(repo: str, api=None) -> None:
    """Create `repo` private (exist_ok) and refuse unless the Hub reports it private."""
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()
    api.create_repo(repo, private=True, exist_ok=True, repo_type="model")
    if not api.model_info(repo).private:
        raise NotPrivateError(f"{repo} exists and is not private; refusing to train into it")


def wait_hub_adapter(cell_id: str, timeout_s: float, poll_s: float, api=None, sleep=time.sleep):
    """`REPO@REV` once the run's repo has a root adapter and step-0050/, or None on timeout."""
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()
    from emx.e1b.contracts import subfolder

    repo = C.hub_repo(cell_id)
    need = {"adapter_model.safetensors", f"{subfolder(H.SAVED_STEP)}/adapter_model.safetensors"}
    waited = 0.0
    while True:
        try:
            info = api.model_info(repo, files_metadata=False)
            files = {s.rfilename for s in (info.siblings or [])}
            if need <= files:
                if not info.private:
                    raise NotPrivateError(f"{repo} is not private")
                return f"{repo}@{info.sha}"
        except NotPrivateError:
            raise
        except Exception as e:  # noqa: BLE001 - not there yet (404) or transient
            print(f"wait {repo}: {type(e).__name__}", flush=True)
        if waited >= timeout_s:
            return None
        sleep(poll_s)
        waited += poll_s


# --- inputs and checkpoints ---------------------------------------------------------------------


def read_manifest(path: Path) -> dict[str, str]:
    """`<sha256>  <path>` lines -> {path: sha256}."""
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            sha, name = line.split(None, 1)
            out[name.strip()] = sha
    return out


def verify_inputs(root: str | Path = ".") -> dict[str, str]:
    """{repo-relative path: sha256} of every pinned input, verified; raises with every problem."""
    root = Path(root)
    want = dict(C.input_sha256())
    man = root / C.manifest_path("pairs")
    probs = []
    if not man.is_file():
        probs.append(f"{man}: missing (pins the pairs, selection and swaps files)")
    else:
        pins = read_manifest(man)
        for p in (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE):
            if p not in pins:
                probs.append(f"{man}: no entry for {p}")
            else:
                want[p] = pins[p]
        if C.PAIRS_SHA256 is not None and pins.get(C.PAIRS_FILE) != C.PAIRS_SHA256:
            probs.append(f"{man}: pairs sha256 != contracts.PAIRS_SHA256")
    got = {}
    for p, sha in want.items():
        f = root / p
        if not f.is_file():
            probs.append(f"{p}: missing")
            continue
        got[p] = _sha(f)
        if got[p] != sha:
            probs.append(f"{p}: sha256 {got[p][:12]} != pinned {sha[:12]}")
    if probs:
        raise SystemExit("INPUTS FAILED: " + "; ".join(probs))
    return got


def _revision(ckpt: str) -> str:
    from emx.e1.registry import STAGES

    rev = C.CHECKPOINT_REVISIONS[ckpt]
    if STAGES[ckpt]["revision"] != rev or STAGES[ckpt]["hf_id"] != C.HF_ID:
        raise SystemExit(f"{ckpt}: registry pin != contracts pin")
    return rev


def download_checkpoint(ckpt: str) -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=C.HF_ID, revision=_revision(ckpt))


def delete_checkpoint(ckpt: str) -> int:
    """Delete the checkpoint's revision from the HF cache; returns the bytes freed (0 if absent)."""
    from huggingface_hub import scan_cache_dir

    rev = _revision(ckpt)
    cache = scan_cache_dir()
    present = {r.commit_hash for repo in cache.repos if repo.repo_id == C.HF_ID
               for r in repo.revisions}
    if rev not in present:
        return 0
    strategy = cache.delete_revisions(rev)
    strategy.execute()
    return strategy.expected_freed_size


# --- training -----------------------------------------------------------------------------------


def _check_cell(cfg) -> None:
    from emx.contracts import CellId

    c = CellId.parse(cfg.cell)
    if c.step is not None or c.untrained:
        raise SystemExit(f"{cfg.cell}: not a final E3 run")
    if c.stage not in C.GRID or c.fmt != C.FMT or c.recipe != C.RECIPE or cfg.epochs != C.EPOCHS:
        raise SystemExit(f"{cfg.cell}: not an E3 run (stage, format, recipe or epochs)")


def _paths(cfg, results_dir: Path) -> tuple[Path, Path]:
    return results_dir / f"{cfg.cell}.json", results_dir / f"{cfg.cell}.losscheck.json"


def _push(cfg, rpath: Path, lpath: Path) -> str:
    """Push the adapter folder; mark the record `pushed`; add the upload to the sidecar."""
    from emx.contracts import TrainResult
    from emx.train import push_adapter, write_record

    t0 = time.time()
    rev = push_adapter(cfg.output_dir, cfg.hub_repo, cfg.cell, f"E3 adapter {cfg.cell}")
    upload_s = time.time() - t0
    rec = json.loads(rpath.read_text())
    rec.update(hub_repo=cfg.hub_repo, hub_revision=rev, status="pushed")
    write_record(rpath, TrainResult(**rec))
    side = json.loads(lpath.read_text())
    side.update(hub_repo=cfg.hub_repo, hub_revision=rev, upload_s=upload_s, pushed_utc=_utc())
    lpath.write_text(json.dumps(side, indent=2) + "\n")
    print(f"E3_UPLOAD {cfg.cell} {cfg.hub_repo}@{rev} {upload_s:.1f}s", flush=True)
    return rev


def write_losscheck(rpath: Path, lpath: Path) -> dict:
    raw = rpath.read_bytes()
    rec = json.loads(raw)
    res = {
        "cell": rec.get("cell"),
        "train_result": str(rpath),
        "train_result_sha256": hashlib.sha256(raw).hexdigest(),
        "train_wallclock_s": rec.get("wallclock_s"),
        **H.losscheck(rec),
        "utc": _utc(),
    }
    lpath.write_text(json.dumps(res, indent=2) + "\n")
    return res


def run_train(args) -> int:
    from emx.train import _read_jsonl, load_config, total_steps, train

    cfg = load_config(args.config)
    _check_cell(cfg)
    if args.smoke_repo:
        cfg = dataclasses.replace(cfg, hub_repo=args.smoke_repo)
    if args.output_dir:
        cfg = dataclasses.replace(cfg, output_dir=args.output_dir)
    results_dir = Path(args.results_dir)
    rpath, lpath = _paths(cfg, results_dir)
    total = total_steps(len(_read_jsonl(Path(args.root) / cfg.data_file)), cfg)
    data = cfg.cell.split("-")[2]
    if cfg.data_sha256 == C.TRAIN_SHA256.get(data) and total != C.TOTAL_STEPS:
        raise SystemExit(f"{cfg.cell}: {total} steps != contracts.TOTAL_STEPS {C.TOTAL_STEPS}")
    last = total if args.max_steps is None else min(args.max_steps, total)
    steps = H.checkpoint_plan(last, EVAL_STEPS)
    push = not args.no_push
    if push:
        precreate_private(cfg.hub_repo)
    if Path(cfg.output_dir).exists():  # a retry starts from an empty adapter folder
        shutil.rmtree(cfg.output_dir)
    train(
        cfg,
        max_steps=args.max_steps,
        push=False,
        root=args.root,
        results_dir=results_dir,
        checkpoint_steps=steps,
        on_checkpoint=H.E3Hook(cfg.output_dir, last),
    )
    order = Path(cfg.output_dir) / "batch_order.json"
    if order.is_file():
        shutil.copy2(order, results_dir / f"{cfg.cell}.batch_order.json")
    lc = write_losscheck(rpath, lpath)
    print(f"E3_LOSSCHECK {cfg.cell} {lc['status']} {'; '.join(lc['reasons'])}", flush=True)
    if push and (lc["pass"] or args.smoke_repo):
        _push(cfg, rpath, lpath)
    return 0 if (lc["pass"] or args.smoke_repo) else 1


def run_push_only(args) -> int:
    from emx.train import load_config

    cfg = load_config(args.config)
    _check_cell(cfg)
    if args.smoke_repo:
        cfg = dataclasses.replace(cfg, hub_repo=args.smoke_repo)
    if args.output_dir:
        cfg = dataclasses.replace(cfg, output_dir=args.output_dir)
    rpath, lpath = _paths(cfg, Path(args.results_dir))
    lc = json.loads(lpath.read_text())
    if not (lc.get("pass") or args.smoke_repo):
        raise SystemExit(f"{cfg.cell}: loss check did not pass; not pushing")
    precreate_private(cfg.hub_repo)
    _push(cfg, rpath, lpath)
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0], formatter_class=argparse.RawTextHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", help="train this run (configs/E3/<cell>.json)")
    mode.add_argument("--verify-inputs", action="store_true")
    mode.add_argument("--download", metavar="CKPT", choices=C.GRID)
    mode.add_argument("--delete-checkpoint", metavar="CKPT", choices=C.GRID)
    mode.add_argument("--wait-hub-adapter", metavar="CELL")
    ap.add_argument("--push-only", action="store_true", help="with --config: push, no training")
    ap.add_argument("--max-steps", type=int, default=None, help="stop early (smoke test)")
    ap.add_argument("--no-push", action="store_true", help="no Hub contact at all")
    ap.add_argument("--smoke-repo", default=None, help="private scratch repo (smoke test)")
    ap.add_argument("--results-dir", default=C.TRAIN_DIR)
    ap.add_argument("--output-dir", default=None, help="default: cfg.output_dir")
    ap.add_argument("--root", default=".", help="repo root the data paths are relative to")
    ap.add_argument("--timeout-s", type=float, default=5400.0)
    ap.add_argument("--poll-s", type=float, default=60.0)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    os.environ["WANDB_MODE"] = "offline"
    if args.verify_inputs:
        got = verify_inputs(args.root)
        print(f"E3_INPUTS_OK {len(got)} files")
        return 0
    if args.download:
        t0 = time.time()
        path = download_checkpoint(args.download)
        print(f"E3_DOWNLOAD {args.download} {path} {time.time() - t0:.1f}s")
        return 0
    if args.delete_checkpoint:
        freed = delete_checkpoint(args.delete_checkpoint)
        print(f"E3_DELETED {args.delete_checkpoint} {freed / 1e9:.1f} GB")
        return 0
    if args.wait_hub_adapter:
        ref = wait_hub_adapter(args.wait_hub_adapter, args.timeout_s, args.poll_s)
        if ref is None:
            print(f"E3_WAIT_TIMEOUT {args.wait_hub_adapter}")
            return 3
        print(ref)
        return 0
    return run_push_only(args) if args.push_only else run_train(args)


if __name__ == "__main__":
    sys.exit(main())
