"""Tests for local Cortex auto-detection.

Each test mocks ``probe_health`` so the suite runs in CI without a real
Cortex stack on localhost. A separate integration test (skipped by
default) hits a real localhost:3400 if available.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider

# ---------------------------------------------------------------------------
# probe_health mock fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_calls() -> list[str]:
    """Records every URL passed to probe_health during a test."""
    return []


@pytest.fixture
def fake_probe(probe_calls: list[str], monkeypatch: pytest.MonkeyPatch):
    """Factory that installs a fake probe_health returning a configured map."""

    def install(responses: dict[str, dict[str, Any] | None]):
        def _probe(url: str, timeout: float = 0.5):
            probe_calls.append(url)
            return responses.get(url)

        monkeypatch.setattr(provider_mod, "probe_health", _probe)
        return _probe

    return install


HEALTHY = {"status": "healthy", "ready": True}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDetection:
    def test_detects_first_healthy_candidate(
        self,
        hermes_home: Path,
        fake_probe,
        probe_calls: list[str],
    ) -> None:
        fake_probe({
            "http://localhost:3400": HEALTHY,
            "http://localhost:4000": HEALTHY,
        })
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is True
        assert provider._config["base_url"] == "http://localhost:3400"
        # Should short-circuit after first hit.
        assert probe_calls == ["http://localhost:3400"]

    def test_falls_through_to_second_candidate(
        self,
        hermes_home: Path,
        fake_probe,
        probe_calls: list[str],
    ) -> None:
        fake_probe({
            "http://localhost:3400": None,
            "http://localhost:4000": HEALTHY,
        })
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is True
        assert provider._config["base_url"] == "http://localhost:4000"
        assert probe_calls == [
            "http://localhost:3400",
            "http://localhost:4000",
        ]

    def test_no_local_means_no_local_mode(
        self,
        hermes_home: Path,
        fake_probe,
    ) -> None:
        fake_probe({})  # every URL returns None
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is False
        # No key + no local => no client.
        assert provider._client is None

    def test_unhealthy_response_rejected(
        self,
        hermes_home: Path,
        fake_probe,
    ) -> None:
        # ready=False, status missing — should not flip local mode.
        fake_probe({"http://localhost:3400": {"ready": False}})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is False

    def test_status_healthy_alone_accepted(
        self,
        hermes_home: Path,
        fake_probe,
    ) -> None:
        # status=healthy without ready flag — accept (degraded but usable).
        fake_probe({"http://localhost:3400": {"status": "healthy"}})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is True


class TestExplicitURL:
    """Explicit base_url must short-circuit detection entirely."""

    def test_config_base_url_disables_detection(
        self,
        hermes_home: Path,
        write_config,
        fake_probe,
        probe_calls: list[str],
    ) -> None:
        write_config(base_url="https://my-cortex.example.com")
        fake_probe({"http://localhost:3400": HEALTHY})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is False
        assert provider._config["base_url"] == "https://my-cortex.example.com"
        # No probe should have run.
        assert probe_calls == []

    def test_env_url_disables_detection(
        self,
        hermes_home: Path,
        fake_probe,
        probe_calls: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CORTEX_API_URL", "https://env-cortex.example.com")
        fake_probe({"http://localhost:3400": HEALTHY})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is False
        assert provider._config["base_url"] == "https://env-cortex.example.com"
        assert probe_calls == []


class TestDisabled:
    def test_auto_detect_false_skips_probe(
        self,
        hermes_home: Path,
        write_config,
        fake_probe,
        probe_calls: list[str],
    ) -> None:
        write_config(auto_detect_local=False)
        fake_probe({"http://localhost:3400": HEALTHY})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._local_mode is False
        assert probe_calls == []


class TestEnvOverride:
    def test_cortex_local_urls_replaces_candidates(
        self,
        hermes_home: Path,
        fake_probe,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CORTEX_LOCAL_URLS", "http://elsewhere:9999")
        fake_probe({"http://elsewhere:9999": HEALTHY})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._config["base_url"] == "http://elsewhere:9999"

    def test_cortex_local_urls_comma_separated(
        self,
        hermes_home: Path,
        fake_probe,
        probe_calls: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "CORTEX_LOCAL_URLS",
            "http://a:1,http://b:2,http://c:3",
        )
        fake_probe({"http://c:3": HEALTHY})
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._config["base_url"] == "http://c:3"
        assert probe_calls == ["http://a:1", "http://b:2", "http://c:3"]


class TestIsAvailable:
    def test_local_stack_makes_provider_available(
        self,
        hermes_home: Path,
        fake_probe,
    ) -> None:
        fake_probe({"http://localhost:3400": HEALTHY})
        provider = HangarxMemoryProvider()
        assert provider.is_available() is True

    def test_no_local_no_config_unavailable(
        self,
        hermes_home: Path,
        fake_probe,
    ) -> None:
        fake_probe({})
        provider = HangarxMemoryProvider()
        assert provider.is_available() is False

    def test_vault_path_is_available_without_local(
        self,
        hermes_home: Path,
        vault_path: Path,
        fake_probe,
        write_config,
    ) -> None:
        write_config(vault_path=str(vault_path))
        fake_probe({})  # no local stack
        provider = HangarxMemoryProvider()
        assert provider.is_available() is True


# ---------------------------------------------------------------------------
# Integration test — only runs when a real local stack is up.
# ---------------------------------------------------------------------------


def _localhost_3400_alive() -> bool:
    from hangarx_memory.client import probe_health

    return probe_health("http://localhost:3400", timeout=0.5) is not None


@pytest.mark.skipif(
    not _localhost_3400_alive(),
    reason="no Cortex stack on localhost:3400",
)
class TestLiveIntegration:
    def test_real_probe_returns_healthy_payload(self) -> None:
        from hangarx_memory.client import probe_health

        data = probe_health("http://localhost:3400", timeout=1.0)
        assert isinstance(data, dict)
        assert data.get("ready") is True or data.get("status") == "healthy"
