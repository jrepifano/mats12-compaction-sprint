"""Unattended desktop launcher; never calls a model before 2026-10-26T00:00Z.

Arm from the designated desktop after reviews (no remote Docker daemon):
  setsid nohup uv run --no-sync python scripts/s2_planted_run.py run --config CONFIG.json \\
    --go-from-jacob --reviewed-build --claim-owner > logs/s2/planted_driver.log 2>&1 &

Inspect: python scripts/s2_planted_run.py status
Stop at the next launch guard: python scripts/s2_planted_run.py stop
For immediate cleanup, SIGTERM the Python PID in logs/s2/planted_run.lock.
A halt requires coordinator inspection; restarting never replays assigned work.
One explicitly authorized post-fix retry (existing desktop owner, no new claim):
  python scripts/s2_planted_run.py run --config logs/s2/planted_run_config.json \\
    --go-from-jacob --reviewed-build --retry-step0-from pilot-0001-step0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2.planted_driver import Driver


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = p.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--claim-owner", action="store_true")
    run.add_argument("--go-from-jacob", action="store_true")
    run.add_argument("--reviewed-build", action="store_true")
    run.add_argument("--retry-step0-from", metavar="FAILED_WORK_ID",
                     help="explicit one-shot post-fix retry of this failed Step 0; preserves all spent forks")
    run.add_argument("--resume-screen-from", metavar="WORK_ID",
                     help="explicitly resume the same unlaunched resource-stopped screen assignments")
    commands.add_parser("status")
    commands.add_parser("stop")
    return p


def build_driver(config: dict, *, runtime_factory=None, part_b_factory=None) -> Driver:
    """Construct lazy runtime services; construction itself makes no model calls."""
    paths = ("root", "corpus", "specs_dir", "si_root", "deepswe_root", "eligibility_path")
    missing = [key for key in (*paths, "expected_hostname") if not config.get(key)]
    if missing:
        raise ValueError(f"missing driver config: {missing}")
    if config.get("host", "unix:///var/run/docker.sock") != "unix:///var/run/docker.sock":
        raise ValueError("driver requires the desktop's explicit local Docker socket")
    if not Path(config["eligibility_path"]).is_file():
        raise ValueError("completed live eligibility report required")
    eligibility = json.loads(Path(config["eligibility_path"]).read_text())
    if eligibility.get("cleanup_stop") is not False:
        raise ValueError("live eligibility cleanup must be confirmed")
    from compaction.s2.planted_config import validate_verification_freeze
    validate_verification_freeze(config, C.REPO_ROOT)
    if runtime_factory is None:
        from compaction.s2.planted_runtime import Runtime
        runtime_factory = Runtime
    kwargs = {key: Path(config[key]) for key in paths}
    runtime = runtime_factory(**kwargs, expected_hostname=config["expected_hostname"],
                              host="unix:///var/run/docker.sock", go_from_jacob=True,
                              reviewed_build=True)
    factory = None
    if config.get("part_b_preparations"):
        preparations = Path(config["part_b_preparations"])
        if not preparations.is_file():
            raise ValueError("configured Part B preparations do not exist")

        def factory(passed_at):
            cls = part_b_factory
            if cls is None:
                from compaction.s2.planted_partb import PartBRuntime
                cls = PartBRuntime
            from compaction.s2.planted_runtime import guarded_max_judge
            return cls(root=kwargs["root"] / "part_b", preparations=preparations,
                       budget=runtime.budget, rate_provider=runtime.max_reading,
                       judge=guarded_max_judge(kwargs["root"] / "part_b_judgments"), token=runtime.token,
                       step0_passed_at=passed_at, task_loader=runtime.task_loader)

    return Driver(runtime, checkout=C.REPO_ROOT, expected_hostname=config["expected_hostname"],
                  part_b_factory=factory, configuration=config)


def main(argv=None):
    args = parser().parse_args(argv)
    directory = C.REPO_ROOT / "logs/s2"
    if args.command == "status":
        path = directory / "planted_run_state.json"
        print(path.read_text() if path.exists() else json.dumps({"status": "NOT_ARMED"}))
        return 0
    if args.command == "stop":
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "planted_run.stop").write_text("operator requested clean stop\n")
        print("Stop requested; the driver will halt at its next launch guard.")
        return 0
    if not args.go_from_jacob or not args.reviewed_build:
        raise SystemExit("arming requires --go-from-jacob and --reviewed-build")
    from dotenv import load_dotenv
    load_dotenv(C.REPO_ROOT / ".env", override=False)
    config = json.loads(args.config.read_text())
    instance = build_driver(config)
    state = instance.run(claim_owner=args.claim_owner, retry_step0_from=args.retry_step0_from,
                         resume_screen_from=args.resume_screen_from)
    print(json.dumps({"status": state["status"], "phase": state["phase"],
                      "reason": state.get("reason")}, indent=2))
    return 0 if state["status"] == "STOPPED_BEFORE_CARDS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
