"""Tests for config loading and merging precedence."""
from __future__ import annotations

from pathlib import Path

import pytest

from hangarx_memory import HangarxMemoryProvider


class TestDefaults:
    """When nothing is configured, defaults should still produce a valid dict."""

    def test_empty_returns_defaults(self, hermes_home: Path) -> None:
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["api_key"] == ""
        assert config["base_url"] == "https://cortex.hangarx.ai"
        assert config["explicit_base_url"] is False
        assert config["agent_id"] == "hermes"
        assert config["tool_mode"] == "full"
        assert config["prefetch_cadence"] == 1

    def test_auto_detect_defaults_to_enabled(self, hermes_home: Path) -> None:
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["auto_detect_local"] is True
        assert "http://localhost:3400" in config["local_candidates"]
        assert "http://localhost:4000" in config["local_candidates"]
        assert config["local_probe_timeout"] >= 0.05


class TestEnvOverrides:
    """Env vars must win over config-file values."""

    def test_env_api_key_wins(
        self,
        hermes_home: Path,
        write_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(api_key="from-config")
        monkeypatch.setenv("CORTEX_API_KEY", "from-env")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["api_key"] == "from-env"

    def test_env_base_url_wins(
        self,
        hermes_home: Path,
        write_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(base_url="https://config.example.com")
        monkeypatch.setenv("CORTEX_API_URL", "https://env.example.com")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["base_url"] == "https://env.example.com"
        assert config["explicit_base_url"] is True


class TestKnobs:
    """Tunable knobs should clamp invalid inputs gracefully."""

    def test_prefetch_cadence_clamps_negative(
        self,
        hermes_home: Path,
        write_config,
    ) -> None:
        write_config(prefetch_cadence=-5)
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["prefetch_cadence"] == 0

    def test_dialectic_passes_clamps_to_3(
        self,
        hermes_home: Path,
        write_config,
    ) -> None:
        write_config(dialectic_passes=99)
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["dialectic_passes"] == 3

    def test_tool_mode_falls_back_to_full(
        self,
        hermes_home: Path,
        write_config,
    ) -> None:
        write_config(tool_mode="bogus")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["tool_mode"] == "full"

    def test_vault_link_style_falls_back_to_wikilink(
        self,
        hermes_home: Path,
        write_config,
    ) -> None:
        write_config(vault_link_style="bogus")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        assert config["vault_link_style"] == "wikilink"


class TestCorruptConfig:
    """A broken config file should not crash the provider."""

    def test_invalid_json_skipped(self, hermes_home: Path) -> None:
        (hermes_home / "hangarx-memory.json").write_text("{not valid json")
        provider = HangarxMemoryProvider()
        config = provider._load_config(str(hermes_home))
        # Defaults should still come through.
        assert config["api_key"] == ""
        assert config["base_url"] == "https://cortex.hangarx.ai"
