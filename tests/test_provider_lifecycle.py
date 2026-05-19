"""Tests for the HangarxMemoryProvider lifecycle interface.

These tests verify the provider conforms to Hermes' MemoryProvider ABC
contract — every required method exists and is callable. Behavior is
covered in more targeted test modules.
"""
from __future__ import annotations

import pytest

from hangarx_memory import HangarxMemoryProvider

HERMES_HOOKS = [
    "initialize",
    "is_available",
    "get_tool_schemas",
    "handle_tool_call",
    "prefetch",
    "queue_prefetch",
    "sync_turn",
    "on_memory_write",
    "on_pre_compress",
    "on_session_end",
    "on_session_switch",
    "on_turn_start",
]


@pytest.fixture
def provider() -> HangarxMemoryProvider:
    return HangarxMemoryProvider()


class TestInterface:
    def test_name_is_hangarx_memory(self, provider: HangarxMemoryProvider) -> None:
        assert provider.name == "hangarx-memory"

    @pytest.mark.parametrize("hook", HERMES_HOOKS)
    def test_hook_callable(
        self, provider: HangarxMemoryProvider, hook: str
    ) -> None:
        assert callable(getattr(provider, hook, None)), (
            f"HangarxMemoryProvider must implement {hook} for Hermes' "
            f"MemoryProvider ABC"
        )

    def test_subclasses_memory_provider(
        self, provider: HangarxMemoryProvider
    ) -> None:
        # We try the real Hermes ABC when it's on the path; otherwise
        # check duck-typing only (CI may run without Hermes installed).
        try:
            from agent.memory_provider import MemoryProvider  # type: ignore
        except ImportError:
            pytest.skip("hermes-agent not installed; skipping ABC check")
        else:
            assert isinstance(provider, MemoryProvider)
