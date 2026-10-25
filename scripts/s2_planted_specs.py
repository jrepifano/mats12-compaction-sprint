"""Regenerate planted task specs from historical evidence; never calls a model.

Example: uv run python scripts/s2_planted_specs.py --corpus <native/claude_code>
--instructions <swe_interact/repo/data/multiturn> --output configs/s2/planted
Existing preliminary configs require --replace-prelaunch and an empty fork ledger.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from compaction.s2.planted_config import ForkBudget, central_budget_path, task_order
from compaction.s2.planted_specs import build_task_spec, save_spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--instructions", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("configs/s2/planted"))
    parser.add_argument("--replace-prelaunch", action="store_true")
    parser.add_argument("--budget", type=Path, default=central_budget_path())
    parser.add_argument("--active-cohort", nargs="+")
    args = parser.parse_args()
    if args.replace_prelaunch and ForkBudget(args.budget).state()["attempts"]:
        raise ValueError("cannot replace frozen specs after any pilot fork assignment")
    specs = []
    for task in task_order():
        base = args.instructions / f"deepswe_{task}"
        spec = build_task_spec(task, corpus=args.corpus,
                               instruction_path=base / "steps/02_implement/instruction.md",
                               task_instruction_path=base / "instruction.md",
                               instruction_root=args.instructions, active_cohort=args.active_cohort)
        specs.append(spec)
    args.output.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        path = args.output / f"{spec['task_id']}.json"
        if args.replace_prelaunch:
            path.write_text(json.dumps(spec, sort_keys=True, indent=2) + "\n")
        else:
            save_spec(path, spec)
        print(json.dumps({"task": spec["task_id"], "eligible": spec["eligible"],
                          "default_check": spec["default_check"]["status"],
                          "commits": spec["default_check"]["commit_count"],
                          "reports": spec["default_check"]["report_count"],
                          "skipped_empty": len(spec["default_check"]["skipped_sources"])}))


if __name__ == "__main__":
    main()
