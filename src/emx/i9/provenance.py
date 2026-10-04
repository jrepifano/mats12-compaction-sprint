"""One git-state rule for every I9 manifest (implementation plan §6, provenance chain).

The pod has no .git, so `pod_push.sh` exports EMX_GIT_COMMIT and EMX_GIT_DIRTY (1 = the pushed
tree had modified tracked files); those win when set. Otherwise git is asked. Only modified
*tracked* files count as dirty: untracked outputs (data/I9, results/I9) are what the steps write.
A failed lookup is recorded as ("unknown", True), never as clean. Stdlib only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from emx.i9 import contracts as C


def git_state(root: Path | None = None) -> tuple[str, bool]:
    """(commit, dirty) for a manifest."""
    if os.environ.get("EMX_GIT_COMMIT"):
        return os.environ["EMX_GIT_COMMIT"], os.environ.get("EMX_GIT_DIRTY", "1") == "1"
    root = root or C.REPO_ROOT
    try:
        def run(*a: str) -> str:
            return subprocess.run(
                ["git", "-C", str(root), *a], capture_output=True, text=True, check=True
            ).stdout.strip()

        head = run("rev-parse", "HEAD")
        dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
        return (head or "unknown"), dirty if head else True
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
