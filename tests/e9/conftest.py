"""Shared fixtures for E9 tests (main session owns this file; units add fixtures in their own
test modules). Offline: no paid calls, no network."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A tmp spend ledger for the duration of the test (emx.spend honours EMX_COSTS_LEDGER)."""
    p = tmp_path / "costs.jsonl"
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(p))
    return p


@pytest.fixture
def reservations(tmp_path, monkeypatch):
    """A tmp shared RunPod reservation file (emx.runpod_reservations honours the env var)."""
    p = tmp_path / "runpod_reservations.jsonl"
    monkeypatch.setenv("EMX_RUNPOD_RESERVATIONS", str(p))
    return p


@pytest.fixture(scope="session")
def olmo_tok():
    """The checkpoint's pinned tokenizer from the local HF cache; skipped if not cached."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from emx.e9.text import tokenizer

        return tokenizer()
    except Exception as e:  # noqa: BLE001  # pragma: no cover - depends on the local cache
        pytest.skip(f"OLMo tokenizer not available offline: {e}")
