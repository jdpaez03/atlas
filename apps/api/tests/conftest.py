"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_llm_backend(monkeypatch):
    """Tests never start Claude Code: pin the backend to `api` unless a test chooses otherwise.

    (The SDK wheel bundles the CLI, so `auto` would pick `subscription` on any machine without a key.)
    """
    monkeypatch.setenv("ATLAS_LLM_BACKEND", "api")
    monkeypatch.delenv("ATLAS_WEB_SEARCH", raising=False)
