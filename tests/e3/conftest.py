"""E3 test fixtures. Tests build synthetic pairs files, so the frozen pairs pin
(contracts.PAIRS_SHA256, set when the real file was frozen on 2026-10-06) is cleared for every E3
test. A test that checks behaviour against the real pin marks itself `real_pairs_pin`."""

from __future__ import annotations

import pytest

from emx.e3 import contracts as C


def pytest_configure(config):
    config.addinivalue_line("markers", "real_pairs_pin: keep contracts.PAIRS_SHA256 as frozen")


@pytest.fixture(autouse=True)
def _synthetic_pairs_unpinned(request, monkeypatch):
    if request.node.get_closest_marker("real_pairs_pin") is None:
        monkeypatch.setattr(C, "PAIRS_SHA256", None)
    yield
