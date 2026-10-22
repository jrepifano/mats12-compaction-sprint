import importlib.util
from pathlib import Path

from compaction import s1_types as T

spec = importlib.util.spec_from_file_location(
    "s1_hillclimb", Path(__file__).resolve().parents[2] / "scripts" / "s1_hillclimb.py"
)
s1_hillclimb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s1_hillclimb)


def _resp(ns, error=None):
    data = {"items": [{"n": n} for n in ns], "missed": []}
    return T.Resp(key="k", data=None if error else data, text=None, error=error)


def test_coverage_ok_needs_every_entry_graded_exactly_once():
    items = [{}, {}, {}]
    ok = s1_hillclimb.coverage_ok
    assert ok(_resp([3, 1, 2]), items)
    assert not ok(_resp([]), items)  # the empty answer Opus once gave to a 27-item list
    assert not ok(_resp([1, 2]), items)
    assert not ok(_resp([1, 2, 2, 3]), items)
    assert not ok(_resp([1, 2, 3], error="cli_error"), items)
    assert ok(_resp([]), [])  # an empty list is graded for missed items only
