"""scripts/s2_native_codex_tables.py reproduces the committed summary tables: the JSON byte for byte
and every data row of the markdown table. Needs the gitignored run data, so it skips without it."""

from __future__ import annotations

import importlib.util

import pytest

from compaction.s2 import config as C

spec = importlib.util.spec_from_file_location(
    "s2_native_codex_tables", C.REPO_ROOT / "scripts" / "s2_native_codex_tables.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)


def _have_data(arm: str) -> bool:
    root, runs = T.ARMS[arm]
    return all((C.REPO_ROOT / root / run).is_dir() for run in runs)


def _table_rows(md: str) -> list[str]:
    """The data rows of the file's main table (the last table, after its header separator)."""
    lines = md.splitlines()
    sep = max(i for i, line in enumerate(lines) if line.startswith("|---"))
    rows = []
    for line in lines[sep + 1:]:
        if not line.startswith("| "):
            break
        rows.append(line)
    return rows


@pytest.mark.parametrize("arm", list(T.ARMS))
def test_tables_match_the_committed_files(arm, monkeypatch, tmp_path):
    if not _have_data(arm):
        pytest.skip("run data not present (data/raw is gitignored)")
    monkeypatch.chdir(tmp_path)  # the script must not depend on the working directory
    rows, md = T.build(arm)
    results = C.REPO_ROOT / T.RESULTS
    assert T.dumps(rows).encode("utf-8") == (results / f"summary_{arm}.json").read_bytes()
    assert md == _table_rows((results / f"summary_{arm}.md").read_text())


def test_row_format_without_data():
    base = {"task": "helm-x", "attempt": "a1", "end": "run_cap", "sim": 5.1335,
            "peak": {"peak_tokens": 79155, "peak_call": 158, "calls": 209}, "b": "b1",
            "pre_tokens": 80325, "recite": [0.99, 0.99, 1.0],
            "fence_layouts": ["bare-pairs"] * 3, "la_lost": 2, "la_items": 40,
            "la_recoverable": {"not searched": 2}, "req_lost": 0, "req_items": 23,
            "req_new": 0, "req_inh": 0, "req_unk": 0}
    assert T.fmt(base) == ("| helm (a1) | run_cap | 5.13 | 79,155 (158 / 209) | b1 | 80,325 | "
                           "0.99 / 0.99 / 1.00 | 3 bare-pairs | 2 / 40 (not searched) | "
                           "0 / 23 (0 / 0 / 0) |")
    later = base | {"b": "b2", "la_lost": 0, "la_recoverable": {}}
    assert T.fmt(later, first=False).startswith("| helm (a1) | | | | b2 |")
    assert "| 0 / 40 |" in T.fmt(later, first=False)
    assert T.fmt(base | {"b": None}).endswith("| none | | | | | |")
