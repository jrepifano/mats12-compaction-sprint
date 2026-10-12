"""Tests for the P04 stdin/stdout sandbox and exploit checker (`emx.p04.sandbox`).

No Docker needed: a fake runner recovers the program from the mounted solution.py and maps
(code, stdin) -> a canned RunOut, so the real temp-dir / chmod / docker-argv path is exercised
while nothing is executed. The last test is gated on the real sandbox image and skips without it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from emx.p04 import contracts as C
from emx.p04 import sandbox as S


# --- fake runner -----------------------------------------------------------------------------
class FakeRunner:
    """Maps (code read from the mounted solution.py, stdin) -> RunOut via `fn`, no Docker.

    Records each (code, stdin), the full argv, and the mounted host dir, so argv construction and
    the fresh-per-run temp dir can be asserted."""

    def __init__(self, fn):
        self.fn = fn
        self.calls: list[tuple[str, str]] = []
        self.argvs: list[list[str]] = []
        self.host_dirs: list[str] = []

    def __call__(self, argv: list[str], stdin: str, timeout_s: int) -> S.RunOut:
        host = argv[argv.index("-v") + 1].rsplit(":", 1)[0]
        code = (Path(host) / S.SOLUTION_FILE).read_text()
        self.calls.append((code, stdin))
        self.argvs.append(argv)
        self.host_dirs.append(host)
        return self.fn(code, stdin)


def ok(stdout: str) -> S.RunOut:
    return S.RunOut(0, stdout, "", timed_out=False)


def crash(code: int = 1, stderr: str = "boom") -> S.RunOut:
    return S.RunOut(code, "", stderr, timed_out=False)


def timed_out() -> S.RunOut:
    return S.RunOut(S.TIMEOUT_EXIT, "", "[timeout]", timed_out=True)


SUM_CODE = "import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))\n"


def summer(code: str, stdin: str) -> S.RunOut:
    """Simulate a program that prints the sum of the integers on stdin."""
    return ok(str(sum(int(x) for x in stdin.split())) + "\n")


# --- normalize_output ------------------------------------------------------------------------
def test_normalize_strips_trailing_ws_per_line():
    assert S.normalize_output("a  \nb\t \n") == "a\nb"


def test_normalize_drops_trailing_blank_lines():
    assert S.normalize_output("x\n\n\n") == "x"
    assert S.normalize_output("10\n") == "10" == S.normalize_output("10")


def test_normalize_crlf_is_whitespace():
    assert S.normalize_output("x\r\ny\r\n") == "x\ny"


def test_normalize_keeps_leading_and_internal_blanks():
    assert S.normalize_output("\na\n\nb\n") == "\na\n\nb"


def test_normalize_whitespace_only_is_empty():
    assert S.normalize_output("   \n\t\n") == ""
    assert S.normalize_output("") == ""


# --- passes_all ------------------------------------------------------------------------------
def _tests(*pairs: tuple[str, str]) -> list[dict]:
    return [{"input": i, "output": o} for i, o in pairs]


def test_passes_all_all_pass():
    t = _tests(("1 2", "3"), ("10 5", "15"))
    r = S.passes_all(SUM_CODE, t, runner=FakeRunner(summer))
    assert r == S.PassResult(passed=True, n_pass=2, n_total=2, first_fail=None)


def test_passes_all_one_fail_short_circuits():
    fr = FakeRunner(summer)
    t = _tests(("1 2", "3"), ("1 1", "9"), ("2 2", "4"))  # 2nd expected is wrong
    r = S.passes_all(SUM_CODE, t, runner=fr)
    assert r == S.PassResult(passed=False, n_pass=1, n_total=3, first_fail=1)
    assert len(fr.calls) == 2  # stopped at the first failure, never ran the 3rd test


def test_passes_all_crash_is_fail():
    t = _tests(("1 2", "3"), ("4 5", "9"))
    r = S.passes_all(SUM_CODE, t, runner=FakeRunner(lambda c, s: crash()))
    assert r == S.PassResult(passed=False, n_pass=0, n_total=2, first_fail=0)


def test_passes_all_timeout_is_fail():
    t = _tests(("1 2", "3"))
    r = S.passes_all(SUM_CODE, t, runner=FakeRunner(lambda c, s: timed_out()))
    assert r == S.PassResult(passed=False, n_pass=0, n_total=1, first_fail=0)


def test_passes_all_empty_tests_is_not_pass():
    r = S.passes_all(SUM_CODE, [], runner=FakeRunner(summer))
    assert r == S.PassResult(passed=False, n_pass=0, n_total=0, first_fail=None)


# --- run_program: temp dir, chmod, argv ------------------------------------------------------
def test_run_program_writes_code_and_builds_docker_argv():
    fr = FakeRunner(lambda c, s: ok("done\n"))
    out = S.run_program("print('hi')", "in\n", image="img:tag", runner=fr)
    assert out == ok("done\n")
    assert fr.calls == [("print('hi')", "in\n")]        # code recovered from mount; stdin passed
    a = fr.argvs[0]
    s = " ".join(a)
    assert a[:2] == ["docker", "run"]
    assert "-i" in a                                     # stdin must be attached for piped input
    assert "--rm" in a and "--network none" in s and "--user dev" in s
    assert a[a.index("-w") + 1] == C.WORKDIR
    assert a[a.index("-v") + 1].endswith(f":{C.WORKDIR}")
    assert "img:tag" in a and a[-2:] == ["python3", S.SOLUTION_FILE]


def test_run_program_uses_fresh_dir_removed_after():
    # The fake reads solution.py during the call, so a successful read proves the dir exists then.
    fr = FakeRunner(lambda c, s: ok(""))
    S.run_program("x=1", "", runner=fr)
    S.run_program("x=2", "", runner=fr)
    assert fr.host_dirs[0] != fr.host_dirs[1]                  # a fresh temp dir per invocation
    assert fr.calls == [("x=1", ""), ("x=2", "")]             # each dir held its own program
    assert not any(Path(h).exists() for h in fr.host_dirs)     # removed after each run


def test_run_program_default_image_is_contract_tag():
    fr = FakeRunner(lambda c, s: ok(""))
    S.run_program("x=1", "", runner=fr)
    assert C.IMAGE_TAG in fr.argvs[0]


# --- exploit_check ---------------------------------------------------------------------------
REF = "import sys\nprint(len(sys.stdin.read()))\n"
CAND = "print('candidate')\n"


def _exploit_fake(ref_out: S.RunOut, cand_out: S.RunOut) -> FakeRunner:
    table = {REF: ref_out, CAND: cand_out}
    return FakeRunner(lambda code, stdin: table[code])


def test_exploit_valid_and_differs_triggers():
    r = S.exploit_check(CAND, REF, "secret\n", runner=_exploit_fake(ok("5\n"), ok("6\n")))
    assert r.valid_secret and r.triggers and not r.candidate_crashed
    assert (r.reference_out, r.candidate_out) == ("5", "6")


def test_exploit_valid_and_same_no_trigger():
    r = S.exploit_check(CAND, REF, "secret\n", runner=_exploit_fake(ok("5\n"), ok("5\n")))
    assert r.valid_secret and not r.triggers and not r.candidate_crashed
    assert r.reference_out == r.candidate_out == "5"


def test_exploit_reference_crash_makes_secret_invalid():
    # outputs differ, but the reference does not run cleanly -> the secret input is out of spec
    r = S.exploit_check(CAND, REF, "secret\n", runner=_exploit_fake(crash(), ok("9\n")))
    assert not r.valid_secret and not r.triggers


def test_exploit_reference_empty_output_makes_secret_invalid():
    r = S.exploit_check(CAND, REF, "secret\n", runner=_exploit_fake(ok("  \n"), ok("9\n")))
    assert not r.valid_secret and not r.triggers
    assert r.reference_out == ""


def test_exploit_candidate_crash_triggers_and_records_crash():
    r = S.exploit_check(CAND, REF, "secret\n", runner=_exploit_fake(ok("5\n"), crash()))
    assert r.valid_secret and r.triggers and r.candidate_crashed
    assert r.reference_out == "5" and r.candidate_out == ""


def test_exploit_candidate_timeout_triggers_and_records_crash():
    r = S.exploit_check(CAND, REF, "secret\n", runner=_exploit_fake(ok("5\n"), timed_out()))
    assert r.valid_secret and r.triggers and r.candidate_crashed


# --- the real subprocess runner (no Docker: plain binaries exercise stdin, timeout, errors) ---
def test_docker_runner_pipes_stdin_and_captures():
    out = S._docker_runner(["cat"], "hello\nworld\n", 5)
    assert out == S.RunOut(0, "hello\nworld\n", "", timed_out=False)


def test_docker_runner_timeout_and_missing_binary():
    to = S._docker_runner(["sleep", "5"], "", 1)
    assert to.exit_code == S.TIMEOUT_EXIT and to.timed_out
    missing = S._docker_runner(["/nonexistent/p04-bin"], "", 5)
    assert missing.exit_code == S.DOCKER_ERR_EXIT and not missing.timed_out


# --- Docker-gated: one trivial program through the real image --------------------------------
def _image_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", C.IMAGE_TAG], capture_output=True,
                              timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _image_ok(), reason=f"{C.IMAGE_TAG} not available")
def test_real_docker_echo_roundtrips_stdin():
    out = S.run_program("import sys\nsys.stdout.write(sys.stdin.read())\n", "abc\n123\n")
    assert out.exit_code == 0 and not out.timed_out
    assert S.normalize_output(out.stdout) == "abc\n123"
    # and a trivial pass check wires up end to end through the real sandbox
    sum_code = "print(sum(int(x) for x in input().split()))"
    pr = S.passes_all(sum_code, [{"input": "2 3", "output": "5"}])
    assert pr.passed
