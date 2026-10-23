"""The S2 task set: DeepSWE's 45 longest tasks, the held-out draw and the pilot cohort.

- `ranking()` reads `top45.json` (compaction.deepswe.trials output; c-deepswe.md section 2).
- `heldout(ranking)`: S0's 6 held-out tasks, one per length stratum, drawn by `HELDOUT_SEED`.
  They are kept for S5 only.
- `pilot_cohort(...)`: tasks ranked 1-6 after removing held-out tasks x the 4 cheap models, with
  the channel rotation (each model 2 tasks per channel; each task in every channel across the
  four models), in a fixed order alternating models. `replacements()` are the next 4 tasks.
- `TaskInfo` gathers what the harness needs from a Harbor task directory.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from compaction.deepswe.tasks import load_task
from compaction.s2 import config as C

CHANNELS = ("agents_md", "chat", "readme")  # violation-specs addendum; AGENTS.md is the control
N_HELDOUT = 6
N_PILOT_TASKS = 6
N_REPLACEMENTS = 4


def ranking(deepswe_dir: Path | None = None) -> list[dict]:
    """[{rank, task, language, image}] in rank order (1 = longest)."""
    d = Path(deepswe_dir or C.deepswe_dir())
    top = json.loads((d / "top45.json").read_text())
    rows = sorted(top["table"], key=lambda r: r["rank"])
    return [{"rank": r["rank"], "task": r["task"], "language": r.get("language"),
             "image": r.get("image")} for r in rows]


def strata(n: int = 45, k: int = N_HELDOUT) -> list[list[int]]:
    """Ranks 1..n cut into k contiguous length strata of near-equal size (sizes differ by <=1,
    larger strata first)."""
    base, extra = divmod(n, k)
    out, start = [], 1
    for i in range(k):
        size = base + (1 if i < extra else 0)
        out.append(list(range(start, start + size)))
        start += size
    return out


def heldout(rank_rows: list[dict], seed: int = C.HELDOUT_SEED) -> list[dict]:
    rng = random.Random(seed)
    by_rank = {r["rank"]: r for r in rank_rows}
    picks = [rng.choice(s) for s in strata(len(rank_rows))]
    return [by_rank[p] for p in picks]


def available(rank_rows: list[dict], seed: int = C.HELDOUT_SEED) -> list[dict]:
    out = {r["task"] for r in heldout(rank_rows, seed)}
    return [r for r in rank_rows if r["task"] not in out]


def channel_for(task_index: int, model_index: int) -> str:
    """Rotation: model j gets channels (i + 2j) mod 3 over its tasks i = 0..5, i.e. 2 tasks per
    channel; task i gets channels i, i+2, i+4, i+6 (mod 3) across the 4 models: all three."""
    return CHANNELS[(task_index + 2 * model_index) % 3]


@dataclass(frozen=True)
class CohortRun:
    order: int
    task: str
    rank: int
    model: str
    channel: str
    kind: str = "pilot"  # "pilot" | "replacement" | "fallback_threshold" | "haiku_peak"

    def run_key(self) -> str:
        return f"{self.kind}-{self.rank:02d}-{self.task}-{self.model}-{self.channel}"


def pilot_cohort(rank_rows: list[dict], models: tuple[str, ...] = C.PILOT_MODELS,
                 seed: int = C.HELDOUT_SEED) -> list[CohortRun]:
    tasks = available(rank_rows, seed)[:N_PILOT_TASKS]
    runs, order = [], 0
    for i, t in enumerate(tasks):
        for j, m in enumerate(models):
            runs.append(CohortRun(order, t["task"], t["rank"], m, channel_for(i, j)))
            order += 1
    return runs


def replacements(rank_rows: list[dict], seed: int = C.HELDOUT_SEED) -> list[dict]:
    return available(rank_rows, seed)[N_PILOT_TASKS:N_PILOT_TASKS + N_REPLACEMENTS]


_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def patch_files(patch_text: str) -> list[str]:
    """Paths a unified git diff touches (b/ side; a/ side for deletions)."""
    out = []
    for a, b in _DIFF_FILE.findall(patch_text):
        for p in (b, a):
            if p not in out:
                out.append(p)
    return out


@dataclass
class TaskInfo:
    task_id: str
    rank: int | None
    language: str | None
    image: str
    collect_command: str
    instruction: str
    task_dir: str
    tests_dir: str
    solution_files: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def task_info(task_id: str, deepswe_dir: Path | None = None,
              rank_rows: list[dict] | None = None) -> TaskInfo:
    d = Path(deepswe_dir or C.deepswe_dir())
    tdir = d / "repo" / "tasks" / task_id
    t = load_task(tdir)
    rows = rank_rows if rank_rows is not None else ranking(d)
    rank = next((r["rank"] for r in rows if r["task"] == task_id), None)
    sol = tdir / "solution" / "solution.patch"
    return TaskInfo(
        task_id=task_id, rank=rank, language=t.get("language"), image=t["docker_image"],
        collect_command=t["collect_command"], instruction=(tdir / "instruction.md").read_text(),
        task_dir=str(tdir), tests_dir=str(tdir / "tests"),
        solution_files=patch_files(sol.read_text(errors="replace")) if sol.exists() else [])
