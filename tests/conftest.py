"""Suite-wide fixtures.

`require_data` skips (rather than errors) a test whose gitignored input data is absent from
this checkout, e.g. data/E1/train_bad.jsonl in a fresh git worktree (CLAUDE.md: worktrees get
no copy of the gitignored data). The skip reason names the missing files, so a skip is never
mistaken for a pass; run from the main checkout, where the data lives, to exercise these tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Under pytest-xdist (-n auto) every worker would otherwise start one BLAS/OpenMP thread per
# core, and the workers' tiny-model torch and numpy tests then fight for the cores (measured
# 2026-10-08: 171 s with the default, 116 s with one thread per worker). Only when running as
# a worker, and never over a value the caller set.
_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
if os.environ.get("PYTEST_XDIST_WORKER") and "OMP_NUM_THREADS" not in os.environ:
    for _v in _THREAD_VARS:
        os.environ.setdefault(_v, "1")
    if "torch" in sys.modules:  # imported before this file (e.g. by a plugin): set it directly
        sys.modules["torch"].set_num_threads(1)

# The E1 splits (scripts/e1_prep.py output), gitignored as data/E1/*.jsonl.
E1_SPLITS = tuple(f"data/E1/{s}_{d}.jsonl" for s in ("train", "test") for d in ("bad", "good"))


def _require(*rel_paths: str) -> None:
    missing = [p for p in rel_paths if not (REPO_ROOT / p).is_file()]
    if missing:
        pytest.skip(f"gitignored input data absent from this checkout: {', '.join(missing)}")


@pytest.fixture(scope="session")
def require_data():
    """require_data("data/E1/train_bad.jsonl", ...) skips the test when any file is absent.
    Session-scoped so module- and session-scoped fixtures can use it too."""
    return _require


@pytest.fixture(scope="session")
def e1_splits(require_data):
    """The four E1 train/test splits must be present."""
    require_data(*E1_SPLITS)


@pytest.fixture(scope="session")
def e1_bad_splits(require_data):
    """Only the two bad-advice splits (train and test) must be present."""
    require_data(*(p for p in E1_SPLITS if "_bad" in p))
