"""U1: task verifier, draw, workspace conversion and manifest (implementation plan §1.3)."""
from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from x3 import contracts as C
from x3 import tasks as T

HAVE_PARQUET = T.PARQUET_PATH.exists() and importlib.util.find_spec("pyarrow") is not None
needs_parquet = pytest.mark.skipif(not HAVE_PARQUET,
                                   reason="cached parquet or pyarrow missing (run prep)")
needs_manifest = pytest.mark.skipif(not C.TASKS_JSON.exists(), reason="tasks.json not written")


def check(body: str) -> str:
    return "def check(candidate):\n" + textwrap.indent(textwrap.dedent(body).strip("\n"), "    ")


POSITIVE = check("""
    assert candidate(1, [2, 3]) == 4
    assert candidate(5, 'x') == 0
    assert candidate(1, [2, 3]) == 7
""")


# --- verifier --------------------------------------------------------------------------------
def test_positive_reports_pair_lines():
    r = T.verify_check_body(POSITIVE, task_id="t")
    assert (r.parsed, r.cond1, r.cond2, r.cond3, r.cond4, r.verified) == (True,) * 6
    assert r.conflict_pair == (2, 4) and r.task_id == "t"


def test_positive_with_negative_literals_and_kwargs():
    r = T.verify_check_body(check("""
        x = [1]
        assert candidate(-1, k=(1, 'a')) == -2
        assert candidate(-1, k=(1, 'a')) == 3
    """))
    assert r.verified and r.conflict_pair == (3, 4)


@pytest.mark.parametrize("body,flags,why", [
    ("""
     if True:
         assert candidate(1) == 1
     assert candidate(1) == 2
     """, (False, False, False, False), "nested assert"),
    ("""
     for _ in range(1):
         assert candidate(1) == 1
         assert candidate(1) == 2
     """, (False, False, False, False), "both nested"),
    ("""
     x = 1
     assert candidate(x) == 1
     assert candidate(x) == 2
     """, (True, False, False, False), "non-literal arg"),
    ("""
     assert candidate([1] * 3) == 1
     assert candidate([1] * 3) == 2
     """, (True, False, False, False), "non-literal arg expression"),
    ("""
     assert candidate(1) == 2
     assert candidate(1) == 2
     """, (True, True, True, False), "equal expected values"),
    ("""
     assert candidate(1) == 1
     assert candidate(1) == 1.0
     """, (True, True, True, False), "equal by value"),
    ("""
     assert candidate(1) == 1
     return
     assert candidate(1) == 2
     """, (False, False, False, False), "assert after return"),
    ("""
     assert candidate(1) == 1
     raise SystemExit
     assert candidate(1) == 2
     """, (False, False, False, False), "assert after raise"),
    ("""
     assert candidate(1) == 1
     assert candidate(2) == 2
     """, (True, True, False, False), "different args"),
    ("""
     assert candidate(1, 2) == 1
     assert candidate(1, 2.0) == 2
     """, (True, True, False, False), "different arg ASTs"),
    ("""
     assert candidate(1) is None
     assert candidate(1) is not None
     """, (False, False, False, False), "not an == assert"),
    ("""
     assert candidate(1) == 1
     candidate = lambda x: 2
     assert candidate(1) == 2
     """, (False, False, False, False), "candidate rebound"),
    ("""
     assert candidate(1) == 1
     assert candidate(1) == 2 == 2
     """, (False, False, False, False), "chained compare"),
])
def test_negatives(body, flags, why):
    r = T.verify_check_body(check(body))
    assert r.parsed
    assert (r.cond1, r.cond2, r.cond3, r.cond4) == flags, why
    assert not r.verified and r.conflict_pair is None, why


def test_unparsable_and_missing_check():
    r = T.verify_check_body("def check(candidate):\n    assert candidate('x) == 1\n")
    assert not r.parsed and not r.verified
    r = T.verify_check_body("assert candidate(1) == 1\nassert candidate(1) == 2\n")
    assert r.parsed and not r.cond1 and not r.verified
    r = T.verify_check_body(POSITIVE.replace("check(candidate)", "check(cand)"))
    assert not r.verified


def test_converted_file_lines_match():
    files = T.convert_row({"task_id": "t", "entry_point": "f",
                           "prompt": 'def f(a, b):\n    """Doc."""\n', "test": POSITIVE})
    test = files[T.TEST_FILE]
    r = T.verify_check_body(test)
    assert r.verified
    lines = test.splitlines()
    assert lines[r.conflict_pair[0] - 1].strip() == "assert candidate(1, [2, 3]) == 4"
    assert lines[r.conflict_pair[1] - 1].strip() == "assert candidate(1, [2, 3]) == 7"
    assert T.conflict_expected(test, r.conflict_pair) == ("4", "7")


# --- draw ------------------------------------------------------------------------------------
def test_draw_matches_frozen_expression():
    ids = [f"lcbhard_{i}" for i in range(90)]
    expected = np.random.default_rng(20260928).choice(sorted(ids), 15, replace=False)
    assert T.draw_ids(ids) == [str(x) for x in expected]
    assert T.draw_ids(list(reversed(ids))) == T.draw_ids(ids)        # order-independent
    assert T.DRAW_EXPR == ("numpy.random.default_rng(20260928)"
                           ".choice(sorted(eligible_ids), 15, replace=False)")
    q = T.replacement_order(ids, T.draw_ids(ids))
    assert not set(q) & set(T.draw_ids(ids)) and len(q) == 75
    exp_q = np.random.default_rng(20260929).permutation(sorted(set(ids) - set(T.draw_ids(ids))))
    assert q == [str(x) for x in exp_q]


@needs_manifest
def test_committed_draw_reproduces_from_verify_csv():
    with open(C.TASK_VERIFY_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    eligible = [r["task_id"] for r in rows
                if r["verified"] == "True" and int(r["test_chars"]) < C.MAX_TEST_CHARS]
    m = T.load_manifest()
    drawn = T.draw_ids(eligible)
    for rep in m.replacements:                     # replacements fill the replaced slot
        drawn[drawn.index(rep["replaced"])] = rep["by"]
    assert [t.task_id for t in m.tasks] == drawn
    assert {r["task_id"] for r in rows if r["selected"] == "True"} == set(drawn)
    assert len(m.tasks) == C.N_TASKS and m.draw == T.DRAW_EXPR
    for t in m.tasks:
        r = T.verify_check_body(t.test_solution_py)
        assert r.verified and r.conflict_pair == t.conflict_pair
        assert len(t.test_solution_py) < C.MAX_TEST_CHARS


@needs_parquet
def test_parquet_hash_and_row_count():
    assert T.sha256_file(T.PARQUET_PATH) == T.PARQUET_SHA256
    assert len(T.load_rows()) == 103


# --- workspace -------------------------------------------------------------------------------
def _run_pytest(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           T.TEST_FILE], cwd=tmp_path, capture_output=True, text=True,
                          timeout=120, check=False)


def test_workspace_imports_and_stub_fails(tmp_path):
    files = T.convert_row({"task_id": "t", "entry_point": "f",
                           "prompt": 'def f(a, b):\n    """Return something."""\n',
                           "test": POSITIVE})
    spec = T.task_spec("t", "f", files, T.verify_check_body(files[T.TEST_FILE]).conflict_pair)
    ws = T.build_workspace(spec)
    assert set(ws) == set(T.WORKSPACE_FILES)
    assert "def f(a, b):" in ws["description.md"] and "lcbhard" not in ws["description.md"]
    out = _run_pytest(tmp_path, ws)
    assert out.returncode == 1, out.stdout + out.stderr        # 1 = tests failed, not an error
    assert "1 failed" in out.stdout and "test_f" in out.stdout
    # a solution that satisfies the first of the pair still fails on the second
    (tmp_path / "solution.py").write_text(
        "def f(a, b):\n    return {1: 4, 5: 0}[a]\n", encoding="utf-8")
    out = _run_pytest(tmp_path, ws | {"solution.py": (tmp_path / "solution.py").read_text()})
    assert "1 failed" in out.stdout and "assert 4 == 7" in out.stdout


@needs_manifest
def test_manifest_stubs_fail(tmp_path):
    t = T.load_manifest().tasks[0]
    out = _run_pytest(tmp_path, T.build_workspace(t))
    assert out.returncode == 1 and "1 failed" in out.stdout, out.stdout + out.stderr


# --- manifest --------------------------------------------------------------------------------
def test_manifest_round_trip(tmp_path):
    files = T.convert_row({"task_id": "t", "entry_point": "f",
                           "prompt": 'def f(a, b):\n    """Doc."""\n', "test": POSITIVE})
    spec = T.task_spec("t", "f", files, T.verify_check_body(files[T.TEST_FILE]).conflict_pair)
    m = C.TaskManifest(schema_version=1, dataset=C.DATASET, split=C.DATASET_SPLIT,
                       revision=C.DATASET_REVISION, parquet_sha256="ab" * 32,
                       draw=T.DRAW_EXPR,
                       replacements=({"replaced": "a", "by": "b", "reason": "r"},),
                       tasks=(spec,))
    path = tmp_path / "tasks.json"
    T.write_manifest(m, path)
    assert T.load_manifest(path) == m
    bad = path.read_text().replace(spec.test_sha256, "0" * 64)
    path.write_text(bad)
    with pytest.raises(ValueError, match="test_sha256"):
        T.load_manifest(path)


@needs_manifest
def test_committed_manifest_loads():
    m = T.load_manifest()
    assert (m.dataset, m.split, m.revision) == (C.DATASET, C.DATASET_SPLIT, C.DATASET_REVISION)
    assert m.parquet_sha256 == T.PARQUET_SHA256
    assert len({t.task_id for t in m.tasks}) == C.N_TASKS
