"""Fixtures for the compaction tests."""

import pytest


@pytest.fixture(autouse=True)
def _laptop_admission_sandbox(tmp_path_factory, monkeypatch):
    """Keep every test off the machine-wide laptop admission files (~/.emx: the flock and the
    reservation registry that live launchers share) and off the real native-launcher
    reservation files. Tests that need them set their own paths."""
    d = tmp_path_factory.mktemp("emx_laptop")
    monkeypatch.setenv("EMX_LAPTOP_DIR", str(d))
    monkeypatch.setenv("EMX_NATIVE_RESERVATIONS", str(d / "native_reservations.json"))
