"""Time one DeepSWE task end to end on this machine, with the oracle patch standing in for an agent.

Phases (each timed): start the agent sandbox; apply and commit the reference solution; collect
model.patch with the task's [[verifier.collect]] hook; check the boundary is quiescent and
`docker commit` a snapshot; fork from it and run the task's own test command there (what an agent
does while working); grade the oracle patch in a fresh verifier container and an empty patch.

The command fails loudly. Exit codes: 0 = oracle reward 1 and empty-patch reward 0; 1 = a check
failed (a valid verifier result, but not 1/0); 2 = setup or infrastructure failure (solution or
test patch didn't apply, empty oracle patch, verifier wrote no valid reward, docker error);
3 = cleanup not confirmed; 130 = interrupted. The --out JSON is written in every case, with the
diagnostics gathered so far, `ok`, `failures` and `error`.

No API calls. Every container carries emx.sandbox=<purpose> and a per-invocation emx.run=<id>
label; on exit only this invocation's containers and snapshot images are removed, and each removal
is confirmed.

Usage:
    uv run python -m compaction.deepswe.feasibility data/raw/sprint/deepswe/repo/tasks/<task> \
        --out data/raw/sprint/deepswe/logs/feas_<task>.json [--platform linux/amd64]
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe.sandbox import (
    Invocation,
    SandboxError,
    VerifierError,
    collect_patch,
    grade,
    remove_image,
)
from compaction.deepswe.tasks import load_task

PURPOSE = "sprint-prep-deepswe"


class SetupError(RuntimeError):
    """A feasibility step failed before a valid verifier result existed."""


class CheckFailed(RuntimeError):
    """The verifier ran validly but the oracle/empty-patch rewards were not 1/0."""


def agent_test_command(task_dir: Path) -> str | None:
    """The suite command an agent would run in its loop: the task's own /app/test.sh, which
    test.patch adds (base = existing suites, new = the hidden tests). None if there is none."""
    patch = (task_dir / "tests" / "test.patch").read_text(errors="replace")
    return "bash /app/test.sh" if "+++ b/test.sh" in patch else None


def check_rewards(oracle: dict | None, nop: dict | None) -> list[str]:
    """The failed feasibility checks (empty = pass): oracle must score 1, the empty patch 0."""
    fails = []
    if oracle is None or oracle.get("reward") != 1:
        fails.append(f"oracle reward is {None if oracle is None else oracle.get('reward')}, not 1")
    if nop is None or nop.get("reward") != 0:
        fails.append(f"empty-patch reward is {None if nop is None else nop.get('reward')}, not 0")
    return fails


def run(task_dir: Path, platform: str | None, inv: Invocation, image: str | None = None,
        out: dict | None = None) -> dict:
    """Fill `out` phase by phase; raise SetupError/SandboxError/VerifierError/CheckFailed."""
    t = load_task(task_dir)
    img = image or t["docker_image"]
    out = out if out is not None else {}
    out.update({"task": t["task_id"], "image": img, "platform": platform, "run_id": inv.run_id,
                "started_utc": datetime.now(UTC).isoformat(timespec="seconds"), "phases": {}})
    ph = out["phases"]
    snap = inv.new_tag("snap")  # tracked for cleanup before the commit is attempted
    with inv.start(img, platform=platform, cpus=t["cpus"], memory_mb=t["memory_mb"]) as sb:
        ph["start_s"] = sb.log[-1]["seconds"]
        sol = (task_dir / "solution" / "solution.patch").read_bytes()
        sb.put_file(sol, "/solution/solution.patch")
        r = sb.exec("cd /app && git apply --whitespace=nowarn "
                    "/solution/solution.patch && git checkout -q -b feature/solution && "
                    "git add -A && git -c user.name=oracle -c user.email=oracle@local "
                    "commit -q --no-verify -m oracle", timeout_s=300)
        ph["apply_solution_s"], ph["apply_solution_rc"] = r.seconds, r.rc
        if r.rc != 0:
            raise SetupError(f"solution patch did not apply/commit rc={r.rc}: {r.stderr[-800:]}")
        t0 = time.monotonic()
        patch = collect_patch(sb, t["collect_command"])
        ph["collect_s"], ph["patch_bytes"] = time.monotonic() - t0, len(patch)
        if not patch.strip():
            raise SetupError("collected model.patch is empty after committing the oracle")
        ph["snapshot_s"] = sb.snapshot(snap)
    img_size = subprocess.run(["docker", "image", "inspect", snap, "--format", "{{.Size}}"],
                              check=False, capture_output=True, text=True, timeout=60)
    out["snapshot_image_bytes"] = (int(img_size.stdout.strip())
                                   if img_size.returncode == 0 and img_size.stdout.strip() else None)
    # The hidden tests go only into a throwaway fork, never into the agent's own container.
    with inv.start(snap, platform=platform, cpus=t["cpus"], memory_mb=t["memory_mb"]) as fork:
        ph["fork_start_s"] = fork.log[-1]["seconds"]
        r = fork.exec("git log --oneline -1", timeout_s=60)
        ph["fork_head"] = r.stdout.strip()
        if r.rc != 0 or "oracle" not in r.stdout:
            raise SetupError(f"fork HEAD is not the oracle commit: {r.stdout!r} {r.stderr!r}")
        cmd = agent_test_command(task_dir)
        if cmd:
            fork.put_file((task_dir / "tests" / "test.patch").read_bytes(), "/tmp/test.patch")
            r = fork.exec("git apply --whitespace=nowarn /tmp/test.patch", timeout_s=120)
            if r.rc != 0:
                raise SetupError(f"test.patch did not apply in the fork rc={r.rc}: "
                                 f"{r.stderr[-800:]}")
            for mode in ("base", "new"):
                r = fork.exec(f"{cmd} {mode} > /tmp/suite.log 2>&1; echo rc=$?; "
                              "tail -5 /tmp/suite.log", timeout_s=1800)
                m = re.match(r"rc=(\d+)", r.stdout)
                ph[f"in_loop_suite_{mode}_s"] = r.seconds
                ph[f"in_loop_suite_{mode}_rc"] = int(m.group(1)) if m else None  # diagnostic
                ph[f"in_loop_suite_{mode}_tail"] = r.stdout[-600:]
    ph["snapshot_removed"] = remove_image(snap)

    t0 = time.monotonic()
    out["reward_oracle"], r = grade(img, task_dir / "tests", patch, PURPOSE, run_id=inv.run_id,
                                    platform=platform)
    ph["grade_oracle_s"], ph["grade_oracle_exec_s"] = time.monotonic() - t0, r.seconds
    t0 = time.monotonic()
    out["reward_nop"], _ = grade(img, task_dir / "tests", b"", PURPOSE, run_id=inv.run_id,
                                 platform=platform)
    ph["grade_nop_s"] = time.monotonic() - t0
    out["finished_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    fails = check_rewards(out["reward_oracle"], out["reward_nop"])
    if fails:
        raise CheckFailed("; ".join(fails))
    return out


def _sigterm(*_a) -> None:
    raise SystemExit(143)  # unwinds through the finally blocks, so cleanup still runs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--image", help="override the task image (e.g. a native arm64 rebuild)")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _sigterm)
    out: dict = {"ok": False, "failures": [], "error": None}
    rc = 0
    inv = Invocation(PURPOSE)
    try:
        try:
            run(Path(a.task_dir), a.platform or None, inv, a.image, out)
            out["ok"] = True
        except CheckFailed as e:
            rc, out["failures"], out["error"] = 1, str(e).split("; "), f"check failed: {e}"
        except (SetupError, VerifierError, SandboxError, subprocess.SubprocessError,
                OSError) as e:
            rc, out["error"] = 2, f"{type(e).__name__}: {e}"
        except KeyboardInterrupt:
            rc, out["error"] = 130, "interrupted"
        except SystemExit:
            out["error"] = "terminated (SIGTERM)"
            raise
    finally:
        bad = inv.cleanup()
        out["cleanup_unconfirmed"] = bad
        if bad:
            out["ok"] = False
            rc = rc or 3
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    if rc:
        print(f"FEASIBILITY FAILED rc={rc}: {out['error'] or 'cleanup not confirmed: ' + str(bad)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
