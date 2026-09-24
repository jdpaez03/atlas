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
    # the app's ARGOS scheduler would start a catch-up watch in every TestClient (docs/ARGOS.md); tests opt in
    monkeypatch.setenv("ATLAS_ARGOS_SCHEDULE", "")
    monkeypatch.setenv("ATLAS_ARGOS_BRIEF", "")


@pytest.fixture(autouse=True)
def _temp_local_dir(monkeypatch, tmp_path_factory):
    """Tests never touch the real atlas-local folder (mission history db, attachments, outputs)."""
    local = tmp_path_factory.mktemp("atlas-local")
    monkeypatch.setenv("ATLAS_LOCAL_DIR", str(local))
    return local
