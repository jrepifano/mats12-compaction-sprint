"""SWE-Interact task families beyond DeepSWE (briefs logs/s2/briefs/integrate-{rf,pro}.md).

Each family module exposes `task_ids()`, `load_task(task_id)` (an `interact_run.InteractTask`,
the object the DeepSWE loader returns), `usable_task_ids()`, `resources(task_id)` and
`grade(task, patch, run_id, *, host=None, inv=None)`: `task` is the InteractTask or its
TaskInfo, `inv` the harness's HostInvocation when there is one, and the result has
`outcomes.grade_patch`'s shape ({"reward", "f2p", "p2p", "grade_error", counts}).
`resources(task_id)` returns {"cpus": float, "memory_mb": int, "storage_mb": int | None,
"workdir": str, "ready_file": str | None}: the container size the task is graded with, the
repo's path inside the image (each family's docstring says where the numbers come from), and
optionally a file to wait for after starting a container, before its first command (an image
entrypoint's setup is finished; None or absent = no wait).

A task id names its family by prefix, so the runner's hooks are one line each, at the top of
`interact_run.load_task`, `interact_run.default_grade` and `outcomes.grade_patch` (the sealed
outcomes path, which receives a TaskInfo and the run's `inv`):

    if families.family_of(task_id): return families.load_task(task_id)
    if families.family_of(task.task_id): return families.grade(task, patch, run_id)
    if families.family_of(task.task_id): return families.grade(task, patch, run_id, inv=inv)

Without the grading hooks a family task is graded by the DeepSWE path: the CLI's default
Docker host, a 1800 s timeout and 2 CPU / 8 GB, which a family may not fit (RF: rig only,
7200 s, some tasks at 8 CPU / 16 GB).
"""

from __future__ import annotations

import importlib
from types import ModuleType

# task-id prefix -> family module (one line per family)
FAMILIES = {
    "rf_task-": "compaction.s2.families.rf",
    "pro-": "compaction.s2.families.pro",
}


def family_of(task_id: str) -> str | None:
    """The family module's name for a task id, or None for a DeepSWE task."""
    return next((m for p, m in FAMILIES.items() if task_id.startswith(p)), None)


def module_for(task_id: str) -> ModuleType:
    mod = family_of(task_id)
    if mod is None:
        raise KeyError(f"{task_id!r} belongs to no registered family")
    return importlib.import_module(mod)


def load_task(task_id: str, **kw):
    return module_for(task_id).load_task(task_id, **kw)


def grade(task, patch: bytes, run_id: str, **kw) -> dict:
    return module_for(task.task_id).grade(task, patch, run_id, **kw)
