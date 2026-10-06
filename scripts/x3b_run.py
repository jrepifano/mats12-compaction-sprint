"""X3b runner CLI (implementation plan v0.4 §3 U3, §4; prereg §7.2-§7.4).

  uv run --group swarm python scripts/x3b_run.py smoke --stage glm --dry-run
  uv run --group swarm python scripts/x3b_run.py smoke --stage glm --yes          # PAID
  uv run --group swarm python scripts/x3b_run.py run --stage glm --yes [--resume] # PAID
  uv run --group swarm python scripts/x3b_run.py collect --stage glm
  uv run --group swarm python scripts/x3b_run.py status --stage glm
  uv run --group swarm python scripts/x3b_run.py stop --stage glm [--reason global_stop]
  uv run --group swarm python scripts/x3b_run.py smoke --stage glm --mock --tasks 1   # free
  uv run --group swarm python scripts/x3b_run.py run --stage glm --mock --tasks 1 --repeats 2

Paid actions need --yes (authorized by the prereg sign-off, prereg §7.3). `run` refuses a stage
without a SMOKE_PASS event, a terminal stage, or a started main run without --resume. --mock
uses mockllm/model, a scratch results tree (results/X3b/mock unless X3B_RESULTS_DIR is set) and
a scratch ledger there; it never creates clock.json and spends nothing. --dry-run prints the
plan, the reservations and the projected spend against the stage cap, and spends nothing.
Paid runs re-execute under `caffeinate -is`; lid-closed sleep still pauses them.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import shutil
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MAIN_ENV = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.env")


def _mock_env() -> None:
    """Before x3b is imported: point results, ledger and reservations at a scratch tree (the
    spawned pool workers inherit the environment)."""
    root = Path(os.environ.setdefault("X3B_RESULTS_DIR", str(ROOT / "results" / "X3b" / "mock")))
    root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("S4_LEDGER", str(root / "costs.jsonl"))
    os.environ.setdefault("S4_RESERVATIONS", str(root / "reservations.json"))


def _ensure_awake() -> None:
    if os.environ.get("X3B_CAFFEINATED") != "1" and shutil.which("caffeinate"):
        os.environ["X3B_CAFFEINATED"] = "1"
        os.execvp("caffeinate", ["caffeinate", "-is", sys.executable, *sys.argv])


def _load_key() -> None:
    from dotenv import dotenv_values

    if not md.version("openai").startswith("2."):
        sys.exit(f"openai {md.version('openai')} breaks Inspect calls; use the swarm group")
    env = dotenv_values(MAIN_ENV)
    if not env.get("OPENROUTER_API_KEY"):
        sys.exit(f"OPENROUTER_API_KEY missing from {MAIN_ENV}")
    os.environ["OPENROUTER_API_KEY"] = env["OPENROUTER_API_KEY"]
    print("loaded keys: OPENROUTER_API_KEY", flush=True)


def _dry_run(R, C, stage, kind, tasks, repeats) -> int:
    from s4world import budget

    m = C.MODELS[stage]
    blocks = R.plan_stage(stage, kind, tasks, repeats)
    st = budget.status(C.LEDGER_EXP[stage], C.STAGE_CAP_USD[stage])
    rs = R.read_run_state(stage)
    per_block = m.block_reservation_usd
    n_eps = sum(len(b) for b in blocks)
    c = (rs.get("smoke") or {}).get("c_usd")
    aside = float(rs.get("judge_set_aside_usd") or 0.0)
    plan = {"stage": stage.value, "kind": kind.value, "blocks": len(blocks), "episodes": n_eps,
            "block_reservation_usd": per_block, "max_in_flight": C.MAX_IN_FLIGHT[stage],
            "worst_case_all_blocks_usd": round(per_block * len(blocks), 2),
            "projected_spend_usd": (round(n_eps * c, 2) if c else
                                    "after the smoke test (sizing rule, prereg §7.2)"),
            "judge_set_aside_usd": aside, "cap_usd": C.STAGE_CAP_USD[stage], "budget": st,
            "first_blocks": [b[0].batch_id for b in blocks[:5]]}
    print(json.dumps(plan, indent=1, default=str))
    return 0


def _guard(runner, fn):
    """Any unexpected exception or interrupt still goes through the one shutdown path, so no
    worker or reservation is left behind."""
    from x3b.runner import RunRefused

    def last_resort(reason: str) -> None:
        try:
            if not runner.draining:
                runner.shutdown(reason)
            else:                     # shutdown itself failed part-way: still kill everything
                errors: list[str] = []
                runner.terminate_all(errors)
                print(f"X3B_ERROR shutdown incomplete; run `stop` to finish: {errors}",
                      flush=True)
        except BaseException as e:  # noqa: BLE001 - the original error is re-raised below
            print(f"X3B_ERROR emergency shutdown failed ({e!r}); run `stop` to finish",
                  flush=True)

    try:
        return fn()
    except RunRefused:
        raise
    except KeyboardInterrupt:
        last_resort("global_stop")
        raise
    except Exception:
        last_resort("harness_failure")
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("smoke", "run", "collect", "status", "stop"):
        p = sub.add_parser(name)
        p.add_argument("--stage", required=True, choices=["glm", "gemini"])
        p.add_argument("--mock", action="store_true")
        if name in ("smoke", "run"):
            p.add_argument("--yes", action="store_true", help="required for any paid action")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--tasks", type=int, default=None, help="first N tasks (mock/tests)")
        if name == "run":
            p.add_argument("--resume", action="store_true")
            p.add_argument("--repeats", type=int, default=None, help="planned repeats (mock)")
        if name == "stop":
            p.add_argument("--reason", default="global_stop")
    a = ap.parse_args(argv)
    if a.mock:
        _mock_env()

    from s4world import budget
    from x3b import contracts as C
    from x3b import runner as R

    stage = C.StageKey(a.stage)
    if a.cmd == "collect":
        print(f"wrote {R.collect(stage)}")
        return 0
    if a.cmd == "status":
        print(json.dumps({"run_state": R.read_run_state(stage),
                          "budget": budget.status(C.LEDGER_EXP[stage], C.STAGE_CAP_USD[stage]),
                          "clock": R.read_clock()}, indent=1, default=str))
        return 0
    if a.cmd == "stop":
        # a live runner holds runner.lock: ask it to stop. Otherwise (it crashed, or a shutdown
        # was interrupted) finish the one shutdown path here (code review r1, finding 2)
        runner = R.StageRunner(R.RunConfig(stage=stage, paid=not a.mock,
                                           model_id=R.MOCK_MODEL if a.mock else None))
        try:
            runner.lock()
        except R.RunRefused:
            print(f"stop requested: {R.request_stop(stage, a.reason)}")
            return 0
        try:
            out = runner.stop_now(a.reason)
        except R.RunRefused as e:
            sys.exit(f"refused: {e}")
        finally:
            runner.close()
        print(json.dumps(out, indent=1, default=str))
        return 0 if out.get("terminal") else 1

    if getattr(a, "repeats", None) is not None and not a.mock:
        sys.exit("--repeats is for mock runs; the sizing rule sets the real target")
    tasks = R.load_tasks()
    if a.tasks is not None:
        if not a.mock and not a.dry_run:
            sys.exit("--tasks is for mock runs and dry runs")
        tasks = tasks[: a.tasks]
    kind = C.RunKind.SMOKE if a.cmd == "smoke" else C.RunKind.MAIN
    if a.dry_run:
        return _dry_run(R, C, stage, kind, tasks, getattr(a, "repeats", None))
    if not a.mock:
        if not a.yes:
            sys.exit("paid action: pass --yes (authorized by the prereg sign-off)")
        _ensure_awake()
        _load_key()
    cfg = R.RunConfig(stage=stage, paid=not a.mock,
                      model_id=R.MOCK_MODEL if a.mock else None, tasks=tasks,
                      repeats=getattr(a, "repeats", None))
    runner = R.StageRunner(cfg)
    try:
        runner.lock()
    except R.RunRefused as e:
        sys.exit(f"refused: {e}")

    def on_term(signum, frame):        # SIGTERM / first Ctrl-C: the runner's own shutdown
        R.request_stop(stage, "global_stop")
        signal.signal(signal.SIGINT, signal.default_int_handler)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        out = _guard(runner, runner.smoke if a.cmd == "smoke"
                     else (lambda: runner.run(resume=a.resume)))
    except R.RunRefused as e:
        sys.exit(f"refused: {e}")
    finally:
        runner.close()
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("terminal", True) or out.get("reason") == "smoke_passed" else 1


if __name__ == "__main__":
    sys.exit(main())
