"""Rebuild E1 training records from a pod run log (Part 2 recovery, 2026-09-30).

The Part 2 pod was terminated without pulling results (one loss-check gate failed and the runner
pulled nothing), so `results/E1/train/{cell}.json` and the loss-check sidecars were lost. The
adapters are on the Hub at pinned revisions, and the run log keeps one summary line per model:

    <cell> [peft] steps=N loss L0->L1 eval E0->E1 wall=Ws hub_revision=<40 hex>

This writes a record per model with those values, clearly marked `reconstructed_from_log`.
`step_losses` / `eval_losses` hold only [first, last]: the held-out-loss check (final < initial)
is therefore exact, and the training-loss check compares the first and last logged step instead
of 10% windows. Hashes, per-step curves and timings are lost and set to null / empty.

    uv run python scripts/e1_reconstruct_train.py logs/pod/pod_run_e1.part.part2.log
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LINE = re.compile(
    r"^(?P<cell>\S+) \[(?P<backend>\w+)\] steps=(?P<steps>\d+) loss (?P<l0>[\d.]+)->(?P<l1>[\d.]+)"
    r" eval (?P<e0>[\d.]+)->(?P<e1>[\d.]+) wall=(?P<wall>\d+)s hub_revision=(?P<rev>[0-9a-f]{40})$",
    re.MULTILINE,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", type=Path)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "results/E1/train")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")
    user = os.environ["HF_USERNAME"]
    text = args.log.read_text()
    log_sha = hashlib.sha256(args.log.read_bytes()).hexdigest()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for m in LINE.finditer(text):
        cell = m["cell"]
        out = args.out_dir / f"{cell}.json"
        if out.exists() and not args.overwrite:
            print(f"{cell}: {out} exists; skipped")
            continue
        rec = {
            "cell": cell,
            "backend": m["backend"],
            "step_losses": [float(m["l0"]), float(m["l1"])],
            "eval_losses": [float(m["e0"]), float(m["e1"])],
            "trained_tokens_per_example": {},
            "labels_sha256": None,
            "batch_order_sha256": None,
            "init_adapter_sha256": None,
            "step_times_s": [],
            "adapter_dir": f"/ws/adapters/{cell}",
            "hub_repo": f"{user}/e1-{cell}",
            "hub_revision": m["rev"],
            "wallclock_s": float(m["wall"]),
            "versions": {"run.steps": m["steps"], "run.total_steps": m["steps"]},
            "reconstructed_from_log": {
                "log": str(args.log),
                "log_sha256": log_sha,
                "note": "record rebuilt from the run log's summary line after the pod was "
                "terminated without pulling results; step_losses / eval_losses are [first, last] "
                "only; hashes and per-step curves were lost",
            },
        }
        out.write_text(json.dumps(rec, indent=1) + "\n")
        n += 1
        print(f"{cell}: wrote {out} (hub {m['rev'][:8]})")
    print(f"{n} records written")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
