"""Docker-free tests for the batched, hang-safe sandbox: monkeypatch `_container_run` to return
canned per-input results, so the pass/exploit logic is exercised without Docker. One Docker-gated
test runs a trivial program end to end if the image is present."""
import shutil

import pytest

from emx.p04 import contracts as C
from emx.p04 import sandbox_batch as sb


def _patch(monkeypatch, mapping):
    """mapping: code -> list of per-input result dicts (in input order)."""
    def fake(code, inputs, *, image, timeout_s, container_timeout_s):
        res = mapping.get(code)
        return None if res is None else res[: len(inputs)]
    monkeypatch.setattr(sb, "_container_run", fake)


def test_passes_all_batched_all_pass(monkeypatch):
    tests = [{"input": "1\n", "output": "1"}, {"input": "2\n", "output": "2"}]
    _patch(monkeypatch, {"CODE": [{"exit": 0, "stdout": "1\n", "timed_out": False},
                                  {"exit": 0, "stdout": "2", "timed_out": False}]})
    r = sb.passes_all_batched("CODE", tests)
    assert r.passed and r.n_pass == 2 and r.first_fail is None


def test_passes_all_batched_one_fail(monkeypatch):
    tests = [{"input": "1\n", "output": "1"}, {"input": "2\n", "output": "2"}]
    _patch(monkeypatch, {"CODE": [{"exit": 0, "stdout": "1", "timed_out": False},
                                  {"exit": 0, "stdout": "WRONG", "timed_out": False}]})
    r = sb.passes_all_batched("CODE", tests)
    assert not r.passed and r.n_pass == 1 and r.first_fail == 1


def test_passes_all_batched_crash_and_empty(monkeypatch):
    _patch(monkeypatch, {"C": [{"exit": 1, "stdout": "", "timed_out": False}]})
    assert not sb.passes_all_batched("C", [{"input": "x", "output": "y"}]).passed
    assert not sb.passes_all_batched("C", []).passed  # empty -> not a pass


def test_passes_all_batched_docker_error(monkeypatch):
    _patch(monkeypatch, {"C": None})  # None = docker/harness failure
    r = sb.passes_all_batched("C", [{"input": "a", "output": "b"}])
    assert not r.passed and r.n_pass == 0


def test_exploit_check_safe_triggers(monkeypatch):
    _patch(monkeypatch, {"REF": [{"exit": 0, "stdout": "42", "timed_out": False}],
                         "CAND": [{"exit": 0, "stdout": "99", "timed_out": False}]})
    r = sb.exploit_check_safe("CAND", "REF", "secret")
    assert r.valid_secret and r.triggers and not r.candidate_crashed


def test_exploit_check_safe_same_output_no_trigger(monkeypatch):
    _patch(monkeypatch, {"REF": [{"exit": 0, "stdout": "42", "timed_out": False}],
                         "CAND": [{"exit": 0, "stdout": "42\n", "timed_out": False}]})
    r = sb.exploit_check_safe("CAND", "REF", "s")
    assert r.valid_secret and not r.triggers


def test_exploit_check_safe_invalid_secret(monkeypatch):
    # reference crashes on the secret -> secret not valid -> no trigger
    _patch(monkeypatch, {"REF": [{"exit": 1, "stdout": "", "timed_out": False}],
                         "CAND": [{"exit": 0, "stdout": "x", "timed_out": False}]})
    r = sb.exploit_check_safe("CAND", "REF", "s")
    assert not r.valid_secret and not r.triggers


def test_exploit_check_safe_candidate_crash_triggers(monkeypatch):
    _patch(monkeypatch, {"REF": [{"exit": 0, "stdout": "42", "timed_out": False}],
                         "CAND": [{"exit": 0, "stdout": "", "timed_out": True}]})
    r = sb.exploit_check_safe("CAND", "REF", "s")
    assert r.valid_secret and r.triggers and r.candidate_crashed


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_real_container_trivial():
    code = "import sys\nprint(int(sys.stdin.readline().strip()) * 2)\n"
    r = sb.passes_all_batched(code, [{"input": "5\n", "output": "10"},
                                     {"input": "7\n", "output": "14"}], image=C.IMAGE_TAG)
    assert r.passed and r.n_pass == 2
