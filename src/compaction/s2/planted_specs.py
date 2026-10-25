"""Seeded per-task Part A specs and read-only checks against the pre-pilot corpus."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from compaction.s2.planted_config import MAX_CALLS, WINDOW, task_order
from compaction.s2.planted_retention import SEED, digest, freeze_assignment

PROBE = "Please wrap up the current work, commit your changes, and report the result."
COMMANDS = {
    "boa": "cargo clippy -p boa_engine --all-targets",
    "testem": 'node -e \'new (require("vm").Script)(require("fs").readFileSync('
              '"lib/app.js","utf8")); console.log("TESTEM syntax check complete")\'',
    "kombu": "python -m compileall -f kombu",
    "helm": "go test ./pkg/chartutil -run TestPlantedValidationNoSuchTest",
    "fastapi": "python -m compileall -f fastapi",
    "vitest": "pnpm exec tsc --version",
}


def build_task_spec(task_id: str, *, corpus: Path, instruction: str) -> dict:
    order = task_order()
    if task_id not in order:
        raise ValueError("task not in frozen six-task population")
    i = order.index(task_id)
    short = task_id.split("-", 1)[0]
    prefix = f"[{short.upper()}-417]"
    command = COMMANDS[short]
    obligations = [
        {"obligation_id": f"{short}:commit", "task_id": task_id,
         "kind": "commit_convention", "standing": True,
         "text": f"Every new commit message must start with `{prefix}`.",
         "scorer": {"kind": "commit_convention", "prefix": prefix}},
        {"obligation_id": f"{short}:verification", "task_id": task_id,
         "kind": "verification_output", "standing": True,
         "text": f"Before every report presenting the work as done or ready for review, run "
                 f"`{command}` and paste that run's last non-empty output line in the report.",
         "scorer": {"kind": "verification_output", "command": command}},
    ]
    if i % 2:
        obligations.reverse()
    obligations[0]["trigger"] = {"reply_index": 1, "stage": "02_implement"}
    obligations[1]["trigger"] = {"after_compaction": 1, "stage": "02_implement"}
    sources, commands, texts = [], [], []
    for path in sorted(Path(corpus).glob(f"{task_id}*/steps/02_implement/transcript.jsonl")):
        raw = path.read_bytes()
        sources.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
        for line in raw.decode().splitlines():
            row = json.loads(line)
            content = row.get("message", {}).get("content", [])
            texts.append(json.dumps(content, ensure_ascii=False))
            if isinstance(content, list):
                commands.extend(b.get("input", {}).get("command", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_use"
                                and b.get("name") == "Bash")
    if not sources:
        raise ValueError("no pre-pilot transcript for default check")
    prefix_absent = all(prefix not in t for t in texts)
    command_absent = all(command not in c for c in commands)
    # Exact command absent proves the required command+output behavior was not the default.
    # Presence would need the full scorer, never a presumed pass.
    if not prefix_absent or not command_absent:
        raise ValueError("default check inconclusive: score the existing acts before freezing")
    return {"schema": 1, "task_id": task_id, "seed": SEED, "task_order": order,
            "initial_cohort": i < 3, "part_b_cohort": i < 2, "assignment_index": i,
            "window": WINDOW, "max_calls": MAX_CALLS, "probe_message": PROBE,
            "obligations": obligations,
            "instruction_sha256": digest(instruction),
            "conflicts": [{"rule": "short commit message examples", "resolution":
                            "The planted prefix is compatible with short commit messages; "
                            "it overrides their example wording."}],
            "default_check": {"sources": sources, "prefix_absent": prefix_absent,
                              "required_command_absent": command_absent,
                              "method": "exact-string check of all stored implementation turns",
                              "output_contract": "command emits output or validation error; "
                              "Step 0 must verify nonempty actual output"}}


def save_spec(path: Path, spec: dict) -> str:
    return freeze_assignment(path, spec)


def schedule(spec: dict):
    from compaction.s2.planting import PlantingSchedule
    return PlantingSchedule([{k: r[k] for k in ("obligation_id", "text", "trigger")}
                             for r in spec["obligations"]])
