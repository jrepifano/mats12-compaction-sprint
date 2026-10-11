"""Batched pass check: run all of a candidate's tests inside ONE `--network none` container.

`src/emx/p04/sandbox.py` runs one `docker run` per test (clean, injectable, well tested). That is
~25 container starts per candidate, and the full P04 run has thousands of candidates, so on
macOS Docker Desktop it is hours. This module runs an in-container harness that executes the
candidate once per test (fresh `python3 solution.py` subprocess per test, so per-test isolation is
kept) and reports per-test pass/fail, collapsing the container starts ~25x. The per-test semantics
match sandbox.run_program: exit code, captured stdout, a 124 timeout, and the same
`normalize_output` comparison (imported from sandbox, single source of truth).

Used by the orchestrator's screen and sandbox phases. The single-run `exploit_check` (2 runs per
candidate) stays in `sandbox.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from emx.p04 import contracts as C
from emx.p04.sandbox import PassResult, normalize_output

SOLUTION_FILE = "solution.py"
TESTS_FILE = "tests.json"
RESULTS_FILE = "results.json"
RUNNER_FILE = "_runner.py"
DOCKER_ERR = {125, 126, 127}

# In-container harness. Reads tests.json [{input, output}], runs solution.py per test with a
# per-test timeout, writes results.json [{exit, stdout, timed_out}]. Stdlib only; no network.
_RUNNER_SRC = '''\
import json, subprocess, sys
tests = json.load(open("tests.json"))
to = float(sys.argv[1])
out = []
for t in tests:
    try:
        p = subprocess.run(["python3", "solution.py"], input=t["input"],
                           capture_output=True, text=True, timeout=to)
        out.append({"exit": p.returncode, "stdout": p.stdout, "timed_out": False})
    except subprocess.TimeoutExpired as e:
        so = e.stdout or ""
        out.append({"exit": 124, "stdout": so if isinstance(so, str) else so.decode("utf-8","replace"),
                    "timed_out": True})
    except Exception as e:  # noqa: BLE001 - a launch failure is a per-test error, not a crash
        out.append({"exit": 127, "stdout": "", "timed_out": False, "err": str(e)})
json.dump(out, open("results.json", "w"))
'''


def _docker_argv(host_dir: str, image: str, timeout_s: int) -> list[str]:
    return ["docker", "run", "-i", "--rm", "--network", "none", "--user", C.AGENT_USER,
            "-v", f"{host_dir}:{C.WORKDIR}", "-w", C.WORKDIR, image,
            "python3", RUNNER_FILE, str(timeout_s)]


def _per_test_pass(res: dict, expected: str) -> bool:
    return (not res.get("timed_out") and res.get("exit") == 0
            and normalize_output(res.get("stdout", "")) == normalize_output(expected))


def passes_all_batched(code: str, tests: list[dict], *, image: str = C.IMAGE_TAG,
                       timeout_s: int = C.RUN_TIMEOUT_S,
                       container_timeout_s: int | None = None) -> PassResult:
    """Pass = every test passes. One container runs them all. Returns the same PassResult shape as
    sandbox.passes_all (n_pass counts tests that passed up to the first failure, for comparability;
    first_fail is the first failing index). Empty tests -> not a pass. A docker/harness error ->
    PassResult(False, 0, n, None) (treated as not-passing; the screen drops such tasks)."""
    n = len(tests)
    if n == 0:
        return PassResult(passed=False, n_pass=0, n_total=0, first_fail=None)
    d = Path(tempfile.mkdtemp(prefix="p04batch_"))
    try:
        (d / SOLUTION_FILE).write_text(code)
        (d / TESTS_FILE).write_text(json.dumps(tests))
        (d / RUNNER_FILE).write_text(_RUNNER_SRC)
        os.chmod(d, 0o777)
        for f in d.iterdir():
            os.chmod(f, 0o666)
        # The whole batch gets a generous wall-clock bound: per-test timeout x tests + overhead.
        wall = container_timeout_s or (timeout_s * n + 60)
        try:
            p = subprocess.run(_docker_argv(str(d), image, timeout_s), capture_output=True,
                               text=True, timeout=wall, check=False)
        except subprocess.TimeoutExpired:
            return PassResult(passed=False, n_pass=0, n_total=n, first_fail=None)
        if p.returncode in DOCKER_ERR or not (d / RESULTS_FILE).exists():
            return PassResult(passed=False, n_pass=0, n_total=n, first_fail=None)
        results = json.loads((d / RESULTS_FILE).read_text())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    first_fail = None
    n_pass = 0
    for i, (res, t) in enumerate(zip(results, tests, strict=False)):
        if _per_test_pass(res, t["output"]):
            n_pass += 1
        else:
            first_fail = i
            break
    passed = first_fail is None and n_pass == n
    return PassResult(passed=passed, n_pass=n_pass, n_total=n, first_fail=first_fail)
