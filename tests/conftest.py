"""Shared pytest fixtures for hangarx-memory tests."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip hangarx-memory environment variables from every test.

    This prevents the developer's real CORTEX_API_KEY / vault / config
    file from bleeding into tests and giving false positives. Tests that
    need a specific env var should set it explicitly via monkeypatch.
    """
    for key in (
        "CORTEX_API_KEY",
        "CORTEX_API_URL",
        "CORTEX_WORKSPACE_ID",
        "CORTEX_ORGANIZATION_ID",
        "CORTEX_LOCAL_URLS",
        "HANGARX_VAULT_PATH",
        # Legacy aliases — must NOT be honored. Tests assert this.
        "CORTEX_VAULT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated $HERMES_HOME directory for the duration of the test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def write_config(hermes_home: Path):
    """Factory that writes hangarx-memory.json with the given dict."""

    def _write(**kwargs) -> Path:
        path = hermes_home / "hangarx-memory.json"
        path.write_text(json.dumps(kwargs))
        return path

    return _write


@pytest.fixture
def write_legacy_config(hermes_home: Path):
    """Factory that writes cortex-memory.json — tests assert it's ignored."""

    def _write(**kwargs) -> Path:
        path = hermes_home / "cortex-memory.json"
        path.write_text(json.dumps(kwargs))
        return path

    return _write


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """An empty directory that can stand in as an Obsidian vault."""
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def unreachable_url() -> str:
    """A URL that's guaranteed to fail probe_health quickly."""
    return "http://127.0.0.1:1"
