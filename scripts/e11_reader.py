"""E11 part 2's LLM reader over the frozen pool (prereg §4.6; implementation plan U6, paid step P1).

  uv run python scripts/e11_reader.py --dry-run      # projection + spend guard, no calls
  uv run python scripts/e11_reader.py --yes [--concurrency 16]

A real run needs --yes, a committed and unmodified pool manifest, and pool files matching it.
Resumable: rerun to finish rows left unwritten or in status "error". Spend goes through one API
reservation (see `emx.e11.reader`). Keys are read from .env and never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from emx import spend
from emx.e11 import contracts as C
from emx.e11 import pool as P
from emx.e11 import reader as R

assert hashlib.sha256(C.READER_PROMPT.encode()).hexdigest() == C.READER_PROMPT_SHA256, \
    "C.READER_PROMPT does not match its frozen sha256"


def manifest_committed(root: Path) -> str | None:
    """None if the manifest is tracked and unmodified, else why not."""
    rel = C.POOL_MANIFEST
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=root,
                             capture_output=True, check=False).returncode == 0
    if not tracked:
        return f"{rel} is not committed"
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel], cwd=root,
                           capture_output=True, check=False).returncode == 0
    return None if clean else f"{rel} has uncommitted changes"


def running_pods(root: Path) -> float:
    from emx.judge_run import running_pod_usd_from_log

    return running_pod_usd_from_log(root / "logs" / f"pods_{C.EXP}.jsonl")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", type=Path, default=C.REPO_ROOT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="required for a paid run")
    ap.add_argument("--concurrency", type=int, default=16)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    manifest = P.load_manifest(root)
    pool = P.load_pool(root, manifest)
    tasks = R.tasks_from_pool(pool)
    csv_path = root / C.READER_CSV
    inputs = R.pool_inputs(root, manifest)
    done = R.check_resume(csv_path, tasks, inputs)
    todo = R.pending(tasks, done)
    proj = R.project(todo)
    pods = running_pods(root)
    print(f"pool: {len(pool)} datasets, {len(tasks)} (row, framing) tasks; {len(done)} with rows "
          f"({sum(r['status'] in R.TERMINAL for r in done.values())} terminal); "
          f"{len(todo)} pending")
    print(f"model {R.PRESET.model}, request {json.dumps(R.request_settings())}, prompt sha "
          f"{C.READER_PROMPT_SHA256[:12]}...")
    print(f"projection: expected ${proj['expected_usd']:.4f} (~{proj['est_in_tokens']:,} in, "
          f"~{proj['est_out_tokens']:,} out tokens); upper bound ${proj['bound_usd']:.4f}; "
          f"reservation ${proj['reservation_usd']:.4f}; prereg projection "
          f"${C.READER_PROJECTED_USD:.2f} for 7,200 calls")
    if len(tasks) == C.N_BASE_ANSWERS and proj["expected_usd"] > 2 * C.READER_PROJECTED_USD:
        print("WARNING: expected cost is more than twice the prereg projection")
    reason = manifest_committed(root)
    if a.dry_run:
        guard = spend.check_total(proj["reservation_usd"], "judge", running_pod_usd=pods,
                                  exp=C.EXP)
        print(f"spend guard OK: E11 judge spent ${guard['judge_spent_usd']:.4f} + reserved "
              f"${guard['judge_reserved_usd']:.4f} + this ${proj['reservation_usd']:.4f} <= "
              f"limit ${guard['judge_limit_usd']:.2f}; total after ${guard['total_after_usd']:.4f}"
              f" <= cap ${guard['hard_cap_usd']:.2f}")
        print(f"manifest gate: {reason or 'committed and unmodified'}")
        print("dry run: no calls made")
        return 0
    if not a.yes:
        print("refusing a paid run without --yes", file=sys.stderr)
        return 2
    if reason:
        print(f"refusing: {reason} (commit the manifest before any scoring)", file=sys.stderr)
        return 2
    s = R.run_reader(root, pool=pool, csv_path=csv_path, running_pod_usd=pods,
                     concurrency=a.concurrency, inputs=inputs, git_commit=P.git_commit(root))
    print(json.dumps({k: s.get(k) for k in ("n_pending", "new_statuses", "usage", "settled_usd",
                                            "halted", "reservation")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
