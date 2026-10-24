"""Q1 audits of a native run's graded boundaries (main session, 24 Oct: both, on every
boundary, both on Max at $0):

(a) `requirements`: the maintainer-requirement ledger, as the pi phase-B runs keep it
    (`interact_reqs`): the split of the simulator's replies into requirements, eligibility
    (recorded), the live summary graded on every requirement so far (`grade_sources_v2`,
    source "live" only: no replays, no screen, no cards), and the new/inherited ledger.
(b) `stage_lossaudit` + `scripts/s2_lossaudit.py run --max --raw <staging>`: Experiment 1's
    item-loss pipeline, as Codex and Experiment 2 use it. lossaudit reads <cohort>/a<n>/ dirs,
    so each graded boundary b<k> is staged as <run key>-b<k>/a1 -> <run>/b<k> (a symlink).

Only the graded chain (02_implement, <run>/b<k>) is audited; plan_boundaries/ are not.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from compaction.s2 import config as C
from compaction.s2 import session as SS

SOURCES = ("live",)


def requirements(run_dir: Path, backend_factory: Callable[[], Any]) -> list[dict]:
    """(a) on every graded boundary of `run_dir`, in order (the ledger reads the previous
    boundary's grades). Resumable: a boundary's split and eligibility are kept once made.
    Returns one summary row per boundary."""
    from compaction.s2 import interact_reqs as RQ
    rows = []
    for bdir in SS.boundary_dirs(Path(run_dir)):
        b = bdir / "boundary"
        if not (b / "requirements.json").exists():
            RQ.split(bdir, backend=backend_factory())
        if not (b / "eligibility.json").exists():
            RQ.eligibility(bdir)
        grades = RQ.grade_sources_v2(bdir, SOURCES, backend=backend_factory())
        led = RQ.loss_ledger(bdir)
        rq = json.loads((b / "requirements.json").read_text())
        live = grades.get("live") or {}
        f = json.loads((b / "boundary.json").read_text())
        rows.append({"k": RQ.boundary_index(bdir), "pre_tokens": f.get("pre_tokens"),
                     "items": len(RQ.gradable_v2(rq)),
                     "grades": dict(Counter(live.values())),
                     "lost": sum(g in ("dropped", "misstated") for g in live.values()),
                     **{f"lost_{s}": n for s, n in (led["counts"].get("live") or {}).items()}})
    return rows


def stage_lossaudit(run_dirs: list[Path], staging: Path) -> list[Path]:
    """(b)'s input: <staging>/<run key>-b<k>/a1 -> <run>/b<k> for every graded boundary.
    Returns the staged attempt dirs."""
    staging = Path(staging)
    out = []
    for rd in run_dirs:
        rk = json.loads((Path(rd) / "run.json").read_text())["run_key"]
        for bdir in SS.boundary_dirs(Path(rd)):
            a = staging / f"{rk}-{bdir.name}" / "a1"
            a.parent.mkdir(parents=True, exist_ok=True)
            if not a.is_symlink():
                os.symlink(bdir.resolve(), a)
            out.append(a)
    return out


def run_lossaudit(staging: Path, out: Path, *, docker_host: str | None = "ssh://rig",
                  concurrency: int = 8, log: Path | None = None) -> int:
    """(b) on Max ($0; the script's canary checks the billing route). Its recoverability
    search greps the boundary's images in Docker, which live on `docker_host` (DOCKER_HOST
    for the subprocess). Returns its exit code."""
    Path(out).mkdir(parents=True, exist_ok=True)
    cmd = ["uv", "run", "python", str(C.REPO_ROOT / "scripts" / "s2_lossaudit.py"), "run",
           "--max", "--raw", str(staging), "--out", str(out), "--concurrency", str(concurrency)]
    env = {**os.environ, **({"DOCKER_HOST": docker_host} if docker_host else {})}
    with open(log or Path(out) / "lossaudit.log", "a") as f:
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=C.REPO_ROOT,
                              env=env, check=False).returncode


def lossaudit_counts(out: Path, run_key: str) -> dict:
    """(b)'s rows for one run: items and lost (losses.jsonl `lost`)."""
    p = Path(out) / "losses.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            if run_key in json.dumps(r.get("s2") or r.get("boundary_dir") or r.get("rid") or ""):
                rows.append(r)
    return {"items": len(rows), "lost": sum(bool(r.get("lost")) for r in rows)}
