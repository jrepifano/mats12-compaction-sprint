"""Read DeepSWE's Harbor task directories.

Each task directory holds task.toml (metadata, image, limits, the [[verifier.collect]] hook that
extracts the agent's commits as /logs/artifacts/model.patch), instruction.md (the prompt),
environment/Dockerfile (rebuilds the image from public.ecr.aws/x8v8d7g8/mars-base), tests/
(verifier image = task image + hidden tests; test.sh runs suites, grader.py scores) and
solution/ (reference patch, never used at grading).
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def load_task(task_dir: str | Path) -> dict:
    d = Path(task_dir)
    t = tomllib.loads((d / "task.toml").read_text())
    meta, env, ver, agent = t["metadata"], t["environment"], t["verifier"], t["agent"]
    collect = ver.get("collect") or []
    return {
        "task_id": meta["task_id"],
        "language": meta.get("language"),
        "repository_url": meta.get("repository_url"),
        "base_commit": meta.get("base_commit_hash"),
        "title": meta.get("display_title"),
        "docker_image": env["docker_image"],
        "cpus": env.get("cpus"),
        "memory_mb": env.get("memory_mb"),
        "storage_mb": env.get("storage_mb"),
        "agent_timeout_sec": agent.get("timeout_sec"),
        "agent_network": agent.get("network_mode"),
        "verifier_timeout_sec": ver.get("timeout_sec"),
        "verifier_mode": ver.get("environment_mode"),
        "collect_command": collect[0]["command"] if collect else None,
        "instruction_chars": len((d / "instruction.md").read_text()),
    }


def load_tasks(tasks_dir: str | Path) -> dict[str, dict]:
    out = {}
    for p in sorted(Path(tasks_dir).glob("*/task.toml")):
        rec = load_task(p.parent)
        out[rec["task_id"]] = rec
    return out
