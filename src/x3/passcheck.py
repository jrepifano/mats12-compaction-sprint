"""Fresh-container pass check, C_pass (U2; implementation plan §1.6, X1p prereg §4.6).

The episode's final `solution.py` and the PRISTINE `test_solution.py` from the task manifest are
copied into a new temp dir per run, and pytest runs in a fresh `docker run --rm --network none
--user dev` container of the sandbox image. Each run gets its own copy, so the two runs share no
state (no .pyc, no conftest, no user site). pass = pytest exit code 0. The runner is injectable
so tests can fake Docker.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from x3 import contracts as C

PASS_TIMEOUT_S = 120
TIMEOUT_EXIT = 124
TEST_FILE = "test_solution.py"
SOLUTION_FILE = "solution.py"


def docker_runner(argv: list[str], timeout_s: int) -> C.RunOutput:
    """Run argv; a timeout returns exit 124, a missing binary exit 127."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        def _s(x) -> str:
            return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        return C.RunOutput(TIMEOUT_EXIT, _s(e.stdout), _s(e.stderr) + "\n[timeout]")
    except OSError as e:
        return C.RunOutput(127, "", f"{type(e).__name__}: {e}")
    return C.RunOutput(p.returncode, p.stdout, p.stderr)


def pytest_argv(host_dir: str, image: str) -> list[str]:
    return ["docker", "run", "--rm", "--network", "none", "--user", C.AGENT_USER,
            "-v", f"{host_dir}:{C.WORKDIR}", "-w", C.WORKDIR, image,
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", TEST_FILE]


def image_digest(image: str, runner: C.Runner = docker_runner) -> str:
    out = runner(["docker", "image", "inspect", "--format", "{{.Id}}", image], 60)
    return out.stdout.strip() if out.exit_code == 0 else ""


def _write_copy(solution_py: str, test_py: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="x3pass_"))
    (d / SOLUTION_FILE).write_text(solution_py)
    (d / TEST_FILE).write_text(test_py)
    os.chmod(d, 0o777)                       # uid 1000 in the container must read and write
    for f in d.iterdir():
        os.chmod(f, 0o666)
    return d


def _docker_error(out: C.RunOutput) -> bool:
    """Exit codes 125-127 are docker's own (daemon, cannot exec, not found); pytest uses 0-5."""
    return out.exit_code in (125, 126, 127)


def _one_run(solution_py: str, task: C.TaskSpec, image: str,
             runner: C.Runner) -> tuple[bool | None, str | None]:
    d = _write_copy(solution_py, task.test_solution_py)
    try:
        out = runner(pytest_argv(str(d), image), PASS_TIMEOUT_S)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if out.exit_code == TIMEOUT_EXIT:
        return None, f"timeout after {PASS_TIMEOUT_S}s"
    if _docker_error(out):
        return None, f"docker exit {out.exit_code}: {out.stderr.strip()[-300:]}"
    return out.exit_code == 0, None


def run_pass_check(solution_py: str, task: C.TaskSpec, *, image: str = C.IMAGE_TAG,
                   runner: C.Runner = docker_runner) -> C.PassResult:
    r1, e1 = _one_run(solution_py, task, image, runner)
    r2, e2 = _one_run(solution_py, task, image, runner)
    errs = [f"run{i}: {e}" for i, e in ((1, e1), (2, e2)) if e]
    disagree = r1 is not None and r2 is not None and r1 != r2
    return C.PassResult(pass_run1=r1, pass_run2=r2, disagree=disagree,
                        error="; ".join(errs) or None, image_digest=image_digest(image, runner))


