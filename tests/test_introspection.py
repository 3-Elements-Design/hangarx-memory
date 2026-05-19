"""Tests for the introspection tools (memory_stats, categories, items, about_me).

The Cortex API calls are mocked via a fake CortexClient. The real
endpoints are exercised in a live integration test that auto-skips
when no localhost:3400 is reachable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider
from hangarx_memory.client import CortexError, probe_health


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_provider(
    hermes_home: Path,
    write_config,
    monkeypatch: pytest.MonkeyPatch,
) -> HangarxMemoryProvider:
    """Provider initialized with a fake client (no real Cortex needed)."""
    write_config(
        api_key="test-key",
        base_url="http://test.invalid",
        workspace_id="ws-001",
    )
    # Disable detection so explicit base_url is honored.
    monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)
    provider = HangarxMemoryProvider()
    provider.initialize(session_id="t", hermes_home=str(hermes_home))
    return provider


@pytest.fixture
def fake_client() -> MagicMock:
    """A MagicMock standing in for CortexClient."""
    client = MagicMock()
    client.workspace_id = "ws-001"
    client.memory_stats.return_value = {
        "data": {
            "totalItems": 42,
            "totalCategories": 5,
            "itemsByPriority": {"high": 7, "normal": 35},
            "categories": [],
        }
    }
    client.memory_categories.return_value = {
        "data": [
            {"name": "user_fact", "itemCount": 12},
            {"name": "preference", "itemCount": 8},
        ]
    }
    client.memory_items.return_value = {
        "data": [
            {"id": f"item-{i}", "content": f"fact {i}"}
            for i in range(20)
        ]
    }
    client.recall.return_value = {
        "data": {
            "items": [{"id": "p1", "content": "user is Iain"}],
            "retrievalMethod": "hybrid",
        }
    }
    return client


# ---------------------------------------------------------------------------
# Tool schema registration
# ---------------------------------------------------------------------------


class TestSchemaRegistration:
    INTROSPECTION_TOOLS = {
        "cortex_memory_stats",
        "cortex_memory_categories",
        "cortex_memory_items",
        "cortex_about_me",
    }

    def test_all_four_registered_in_full_mode(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        names = {s["name"] for s in initialized_provider.get_tool_schemas()}
        assert self.INTROSPECTION_TOOLS.issubset(names)

    def test_not_exposed_in_compact_mode(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider._config["tool_mode"] = "compact"
        names = {s["name"] for s in initialized_provider.get_tool_schemas()}
        # Compact mode only keeps recall/remember/ask.
        assert self.INTROSPECTION_TOOLS.isdisjoint(names)

    def test_not_exposed_without_client(
        self,
        initialized_provider: HangarxMemoryProvider,
    ) -> None:
        initialized_provider._client = None
        names = {s["name"] for s in initialized_provider.get_tool_schemas()}
        assert self.INTROSPECTION_TOOLS.isdisjoint(names)


# ---------------------------------------------------------------------------
# cortex_memory_stats / categories / items dispatch
# ---------------------------------------------------------------------------


class TestStatsTool:
    def test_dispatches_with_default_agent_id(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call("cortex_memory_stats", {})
        fake_client.memory_stats.assert_called_once_with(
            agent_id=initialized_provider._agent_id,
        )
        # handle_tool_call returns JSON string.
        decoded = json.loads(result)
        assert decoded["data"]["totalItems"] == 42

    def test_explicit_agent_id_overrides(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider.handle_tool_call(
            "cortex_memory_stats", {"agent_id": "other"}
        )
        fake_client.memory_stats.assert_called_once_with(agent_id="other")


class TestCategoriesTool:
    def test_returns_list(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call(
            "cortex_memory_categories", {}
        )
        decoded = json.loads(result)
        assert isinstance(decoded["data"], list)
        assert {c["name"] for c in decoded["data"]} == {"user_fact", "preference"}


class TestItemsTool:
    def test_default_limit_applied(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        # 20 items returned, default limit 50 — full list comes through.
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call("cortex_memory_items", {})
        decoded = json.loads(result)
        assert isinstance(decoded, list)
        assert len(decoded) == 20

    def test_explicit_limit_caps_response(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call(
            "cortex_memory_items", {"limit": 3}
        )
        decoded = json.loads(result)
        assert len(decoded) == 3
        assert decoded[0]["id"] == "item-0"

    def test_invalid_limit_falls_back_to_default(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call(
            "cortex_memory_items", {"limit": "not-a-number"}
        )
        decoded = json.loads(result)
        # Default 50 — but we only have 20 items, so full list returned.
        assert len(decoded) == 20


# ---------------------------------------------------------------------------
# cortex_about_me synthesis
# ---------------------------------------------------------------------------


class TestAboutMe:
    def test_returns_all_sections(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call("cortex_about_me", {})
        decoded = json.loads(result)
        assert decoded["stats"]["totalItems"] == 42
        assert len(decoded["categories"]) == 2
        assert len(decoded["sample_items"]) == 10  # default sample_size
        assert decoded["profile_recall"] is not None
        assert decoded["errors"] == []
        assert decoded["agent_id"] == initialized_provider._agent_id
        assert decoded["workspace_id"] == "ws-001"

    def test_sample_size_clamps_to_50(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        # Request way more than the cap.
        initialized_provider.handle_tool_call(
            "cortex_about_me", {"sample_size": 9999}
        )
        # recall() should receive limit=50.
        fake_client.recall.assert_called_once()
        kwargs = fake_client.recall.call_args.kwargs
        assert kwargs["limit"] == 50

    def test_partial_failure_returns_remaining_data(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        # Make categories call fail; others should still succeed.
        fake_client.memory_categories.side_effect = CortexError("category index down")
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call("cortex_about_me", {})
        decoded = json.loads(result)
        assert decoded["stats"] is not None
        assert decoded["categories"] == []
        assert any("categories:" in e for e in decoded["errors"])
        assert decoded["sample_items"]  # still got items

    def test_without_client_returns_error(
        self,
        initialized_provider: HangarxMemoryProvider,
    ) -> None:
        initialized_provider._client = None
        # cortex_about_me unregistered without client — dispatch falls
        # through to "unknown tool". This is the right behavior: the
        # tool isn't advertised so the model shouldn't call it.
        result = initialized_provider.handle_tool_call("cortex_about_me", {})
        decoded = json.loads(result)
        assert "error" in decoded


# ---------------------------------------------------------------------------
# Live integration test
# ---------------------------------------------------------------------------


def _localhost_3400_alive() -> bool:
    return probe_health("http://localhost:3400", timeout=0.5) is not None


@pytest.mark.skipif(
    not _localhost_3400_alive(),
    reason="no Cortex stack on localhost:3400",
)
class TestLiveIntrospection:
    @pytest.fixture
    def live_provider(
        self,
        hermes_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> HangarxMemoryProvider:
        # No config, no key — let auto-detect find localhost:3400.
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="live-test", hermes_home=str(hermes_home))
        return provider

    def test_real_stats_call(
        self, live_provider: HangarxMemoryProvider
    ) -> None:
        assert live_provider._client is not None
        result = live_provider.handle_tool_call("cortex_memory_stats", {})
        decoded = json.loads(result)
        assert decoded.get("success") is True
        data = decoded["data"]
        assert "totalItems" in data
        assert "totalCategories" in data

    def test_real_about_me_call(
        self, live_provider: HangarxMemoryProvider
    ) -> None:
        result = live_provider.handle_tool_call("cortex_about_me", {})
        decoded = json.loads(result)
        # Even with an empty memory bank, the structure should be intact.
        assert "stats" in decoded
        assert "categories" in decoded
        assert "sample_items" in decoded
        assert "profile_recall" in decoded
        assert isinstance(decoded.get("errors", []), list)
