"""Build/discover offline; launch Step 0 only with Jacob's go and completed reviews.

Offline census:
    uv run python scripts/s2_planted.py discover --out logs/s2/planted_discovery.json

After reviews and an explicit main-session go (never during the build):
    uv run --with fastmcp==4.1.3 python scripts/s2_planted.py step0 BDIR \\
      --task TASK --si-root SI_MULTITURN_DIR --deepswe-dir DEEPSWE_DIR \\
      --rate-file FRESH_MAX_RATE_JSON --reviewed-build --go-from-jacob

Rate file: {"observed_at": UNIX_SECONDS, "rate_limit_info": RATE_LIMIT_INFO}.
Use a rate_limit_event from an authorized run no older than five minutes; this
command never pings Max to bypass the launch gate. A failed attempt consumes budget.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import interact_run as IR
from compaction.s2 import planted_step0 as P
from compaction.s2.native import claude_code as CC
from compaction.s2.planted_retention import max_judge


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("discover", "prepare"):
        d = commands.add_parser(name, help="offline metadata/transcript census; no Docker/model calls")
        d.add_argument("--corpus", type=Path, default=P.DEFAULT_CORPUS)
        d.add_argument("--out", type=Path)
    s = commands.add_parser("step0", help="one probe and one guarded continuation; consumes two Part A attempts")
    s.add_argument("boundary", type=Path)
    s.add_argument("--task", required=True)
    s.add_argument("--si-root", type=Path, required=True, help="SWE-Interact data/multiturn directory")
    s.add_argument("--deepswe-dir", type=Path, required=True)
    s.add_argument("--spec", type=Path, help="default configs/s2/planted/TASK.json")
    s.add_argument("--output-root", type=Path, default=C.REPO_ROOT / "data/raw/sprint/s2/planted")
    s.add_argument("--attempt", default="step0-1", help="new unique label; failures are never refunded")
    s.add_argument("--host", default="ssh://rig", help="Docker host containing the kept image")
    s.add_argument("--rate-file", type=Path, required=True, help="fresh rate_limit_info and observed_at epoch")
    s.add_argument("--reviews-ready", type=Path)
    s.add_argument("--reviewed-build", action="store_true", help="main session attests required reviews completed")
    s.add_argument("--go-from-jacob", action="store_true", help="explicit Step 0 authorization; required")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if a.command in ("discover", "prepare"):
        result = P.discover(a.corpus)
        if a.out:
            P.write(a.out, result)
        print(json.dumps(result, indent=2))
        return 0
    if not a.go_from_jacob:
        raise SystemExit("Step 0 held: --go-from-jacob is required")
    if not a.reviewed_build and not a.reviews_ready:
        raise SystemExit("Step 0 held: completed reviews artifact or --reviewed-build required")
    from dotenv import load_dotenv
    load_dotenv(C.REPO_ROOT / ".env", override=False)
    spec = P.read(a.spec or C.REPO_ROOT / "configs/s2/planted" / f"{a.task}.json")
    task = IR.load_task(a.task, si_root=a.si_root, deepswe_dir=a.deepswe_dir)
    result = P.run_step0(a.boundary, a.output_root, spec=spec, task=task,
                         token=os.environ.get(CC.TOKEN_ENV, ""), rate_snapshot=P.read(a.rate_file),
                         go_from_jacob=a.go_from_jacob, reviewed_build=a.reviewed_build,
                         reviews_ready=a.reviews_ready, host=a.host, attempt=a.attempt,
                         judge_factory=max_judge)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
