"""Tests for the back-compat removal in v0.4.0.

We dropped support for the cortex-memory legacy package entirely
(class alias, config filename, env var alias). These tests pin the
behavior so we don't accidentally re-introduce it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider


class TestNoLegacySymbols:
    """Module-level legacy symbols must not exist."""

    def test_no_cortex_memory_provider_alias(self) -> None:
        assert not hasattr(provider_mod, "CortexMemoryProvider")

    def test_no_legacy_config_file_name_constant(self) -> None:
        assert not hasattr(provider_mod, "LEGACY_CONFIG_FILE_NAME")


class TestNoLegacyEnvAliases:
    """CORTEX_VAULT_PATH must NOT be read — only HANGARX_VAULT_PATH."""

    def test_cortex_vault_path_ignored(
        self,
        hermes_home: Path,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CORTEX_VAULT_PATH", str(vault_path))
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["vault_path"] == ""

    def test_hangarx_vault_path_honored(
        self,
        hermes_home: Path,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HANGARX_VAULT_PATH", str(vault_path))
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["vault_path"] == str(vault_path)


class TestNoLegacyConfigFile:
    """cortex-memory.json must NOT be loaded — only hangarx-memory.json."""

    def test_cortex_memory_json_ignored(
        self,
        hermes_home: Path,
        write_legacy_config,
    ) -> None:
        write_legacy_config(vault_path="/should/be/ignored", api_key="secret-x")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["vault_path"] == ""
        assert config["api_key"] == ""

    def test_hangarx_memory_json_honored(
        self,
        hermes_home: Path,
        write_config,
        vault_path: Path,
    ) -> None:
        write_config(vault_path=str(vault_path), api_key="real-key")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["vault_path"] == str(vault_path)
        assert config["api_key"] == "real-key"
