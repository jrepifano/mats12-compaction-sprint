"""I9 test isolation: no test may write into the real data/I9 or results/I9 trees.

Every contracts path under RESULTS, plus RUN_CONFIG_PATH (a stray data/I9/run_config.json would
be pushed to the pod and could switch the Oracle Lens cut), is redirected to a per-test tmp dir.
Fixtures that redirect RESULTS themselves still win: they run after this one. Read-only inputs
(SCENARIOS_PATH, SETS_PATH) are left alone.
"""

from pathlib import Path

import pytest

from emx.i9 import contracts as C

_REAL_RESULTS = C.RESULTS
_REDIRECTED = {
    name: getattr(C, name).relative_to(_REAL_RESULTS)
    for name in dir(C)
    if name.isupper()
    and isinstance(getattr(C, name), Path)
    and getattr(C, name).is_relative_to(_REAL_RESULTS)
    and getattr(C, name) != _REAL_RESULTS
}


@pytest.fixture(autouse=True)
def _i9_isolated_outputs(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("i9_isolated")
    results = root / "results" / "I9"
    monkeypatch.setattr(C, "RESULTS", results)
    for name, rel in _REDIRECTED.items():
        monkeypatch.setattr(C, name, results / rel)
    (root / "data" / "I9").mkdir(parents=True)
    monkeypatch.setattr(C, "RUN_CONFIG_PATH", root / "data" / "I9" / "run_config.json")
    yield
