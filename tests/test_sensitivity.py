"""Tests for C — sensitivity tags + agent_context-aware gate.

Covers:
- sensitivity.infer + sensitivity.merge + allow_write / allow_inject pure logic
- on_memory_write gate (refuses private/secret writes from non-primary contexts)
- queue_prefetch gate (skips prefetch in non-primary contexts by default)
- prefetch_in_subagent override
- BLOCKED changelog entries
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider
from hangarx_memory.sensitivity import (
    SENSITIVITY_PRIVATE,
    SENSITIVITY_PUBLIC,
    SENSITIVITY_SECRET,
    allow_inject,
    allow_write,
    extract,
    infer,
    is_non_primary,
    merge,
    normalize,
)

# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("public", "public"),
        ("PRIVATE", "private"),
        ("  Secret ", "secret"),
        ("unknown", "public"),
        ("", "public"),
        (None, "public"),
        (123, "public"),
    ])
    def test_normalize(self, raw, expected: str) -> None:
        assert normalize(raw) == expected


class TestInfer:
    def test_empty_is_public(self) -> None:
        assert infer("") == SENSITIVITY_PUBLIC
        assert infer("just a normal sentence") == SENSITIVITY_PUBLIC

    @pytest.mark.parametrize("content", [
        "My OpenAI key is sk-abcdef1234567890abcdef1234",
        "Anthropic: sk-ant-api03-abcdefABCDEF1234567890_-",
        "Slack token: xoxb-1234567890-abcdef",
        "AWS access key AKIAIOSFODNN7EXAMPLE",
        "github push token ghp_abcdef1234567890abcdef1234567890abc",
        "Cortex API key ctx_abc123def456ghi789jkl012mno",
    ])
    def test_secret_patterns_detected(self, content: str) -> None:
        assert infer(content) == SENSITIVITY_SECRET

    @pytest.mark.parametrize("content", [
        "Email me at alice@example.com",
        "Phone: 555-123-4567",
        "Phone: (415) 555-9999",
        "SSN-shaped: 123-45-6789",
    ])
    def test_private_patterns_detected(self, content: str) -> None:
        assert infer(content) == SENSITIVITY_PRIVATE

    def test_secret_wins_over_private(self) -> None:
        # Content containing both an email AND an API key → SECRET
        content = "alice@example.com api key sk-1234567890abcdef1234567890"
        assert infer(content) == SENSITIVITY_SECRET


class TestMerge:
    def test_higher_wins(self) -> None:
        assert merge("public", "private") == SENSITIVITY_PRIVATE
        assert merge("private", "secret") == SENSITIVITY_SECRET
        assert merge("public", "secret") == SENSITIVITY_SECRET

    def test_unknown_falls_back_to_public(self) -> None:
        assert merge("unknown", "public") == SENSITIVITY_PUBLIC

    def test_secret_caller_overrides_inferred_public(self) -> None:
        # Caller forces SECRET → wins even if content looks innocuous.
        assert merge("secret", "public") == SENSITIVITY_SECRET


class TestContextGates:
    def test_is_non_primary(self) -> None:
        assert is_non_primary("subagent") is True
        assert is_non_primary("cron") is True
        assert is_non_primary("background") is True
        assert is_non_primary("primary") is False
        assert is_non_primary("") is False
        assert is_non_primary(None) is False

    def test_allow_write_public_anywhere(self) -> None:
        assert allow_write("public", "primary") is True
        assert allow_write("public", "subagent") is True
        assert allow_write("public", "cron") is True

    def test_allow_write_private_primary_only(self) -> None:
        assert allow_write("private", "primary") is True
        assert allow_write("private", "subagent") is False
        assert allow_write("private", "cron") is False

    def test_allow_write_secret_primary_only(self) -> None:
        assert allow_write("secret", "primary") is True
        assert allow_write("secret", "subagent") is False

    def test_allow_inject(self) -> None:
        # public → injected everywhere
        assert allow_inject("public", "primary") is True
        assert allow_inject("public", "subagent") is True
        # private → primary only
        assert allow_inject("private", "primary") is True
        assert allow_inject("private", "subagent") is False
        # secret → never via prefetch (even primary)
        assert allow_inject("secret", "primary") is False
        assert allow_inject("secret", "subagent") is False


class TestExtract:
    def test_dict_with_tag(self) -> None:
        assert extract({"sensitivity": "PRIVATE"}) == "private"

    def test_dict_without_tag(self) -> None:
        assert extract({"other": "val"}) == "public"

    def test_non_dict(self) -> None:
        assert extract(None) == "public"
        assert extract("string") == "public"


# ---------------------------------------------------------------------------
# Provider integration
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_with_client(
    hermes_home: Path,
    write_config,
    monkeypatch: pytest.MonkeyPatch,
):
    """Factory: provider with fake client, configurable agent_context."""
    write_config(api_key="test-key", workspace_id="ws-001")
    monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)

    def _make(agent_context: str = "primary") -> HangarxMemoryProvider:
        provider = HangarxMemoryProvider()
        provider.initialize(
            session_id="t",
            hermes_home=str(hermes_home),
            agent_context=agent_context,
        )
        fake = MagicMock()
        fake.workspace_id = "ws-001"
        fake.remember.return_value = {"data": {"id": "mem_001"}}
        provider._client = fake
        return provider

    return _make


class TestOnMemoryWriteGate:
    def test_public_write_from_primary_allowed(self, provider_with_client) -> None:
        p = provider_with_client(agent_context="primary")
        p.on_memory_write("add", "user", "User likes hiking")
        p._client.remember.assert_called_once()
        # Changelog has ADDED, not BLOCKED.
        entries = p._changelog.recent()
        assert entries[0]["action"] == "ADDED"

    def test_public_write_from_subagent_allowed(
        self, provider_with_client
    ) -> None:
        p = provider_with_client(agent_context="subagent")
        p.on_memory_write("add", "user", "Just a public fact")
        p._client.remember.assert_called_once()

    def test_secret_inferred_from_subagent_blocked(
        self, provider_with_client
    ) -> None:
        p = provider_with_client(agent_context="subagent")
        # Content contains an API-key-shaped token → inferred SECRET.
        p.on_memory_write(
            "add", "user", "key is sk-abcdef1234567890abcdef1234"
        )
        p._client.remember.assert_not_called()
        # Changelog should have a BLOCKED entry.
        entries = p._changelog.recent()
        assert entries[0]["action"] == "BLOCKED"
        assert entries[0]["details"]["sensitivity"] == "secret"

    def test_explicit_private_tag_from_cron_blocked(
        self, provider_with_client
    ) -> None:
        p = provider_with_client(agent_context="cron")
        p.on_memory_write(
            "add", "user", "Innocuous content",
            metadata={"sensitivity": "private"},
        )
        p._client.remember.assert_not_called()

    def test_secret_from_primary_allowed_but_tagged(
        self, provider_with_client
    ) -> None:
        p = provider_with_client(agent_context="primary")
        p.on_memory_write(
            "add", "user", "Anthropic key: sk-ant-api03-xyzabc1234567890_-"
        )
        # Primary context can write secret data, but the metadata sent
        # to Cortex must carry the sensitivity tag.
        p._client.remember.assert_called_once()
        call_kwargs = p._client.remember.call_args.kwargs
        assert call_kwargs["metadata"]["sensitivity"] == "secret"

    def test_disabled_via_config(
        self,
        hermes_home: Path,
        write_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(
            api_key="test-key",
            sensitivity_enabled=False,
        )
        monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)
        p = HangarxMemoryProvider()
        p.initialize(
            session_id="t",
            hermes_home=str(hermes_home),
            agent_context="subagent",
        )
        fake = MagicMock()
        fake.workspace_id = ""
        fake.remember.return_value = {"data": {"id": "x"}}
        p._client = fake
        # With sensitivity disabled, even a SECRET-shaped write goes
        # through from a subagent context.
        p.on_memory_write(
            "add", "user", "sk-ant-api03-abcdef1234567890abcdef"
        )
        p._client.remember.assert_called_once()


class TestPrefetchGate:
    def test_subagent_prefetch_suppressed_by_default(
        self, provider_with_client
    ) -> None:
        p = provider_with_client(agent_context="subagent")
        p.queue_prefetch("what does the user prefer?")
        # No thread spawned, no cortex calls.
        p._client.ask_context.assert_not_called()
        p._client.ask_context_stream.assert_not_called()

    def test_primary_prefetch_still_runs(
        self, provider_with_client
    ) -> None:
        p = provider_with_client(agent_context="primary")
        # Make sure prefetch_cadence is set so the cortex path actually
        # runs. Disable streaming and just call ask_context to keep
        # the test simple.
        p._config["prefetch_cadence"] = 1
        p._config["stream_prefetch"] = False
        # Force turn so cadence triggers.
        p._turn_counter = 0
        p._client.ask_context.return_value = "some context"
        p.queue_prefetch("question")
        # Thread is async — wait briefly.
        import time
        time.sleep(0.2)
        p._client.ask_context.assert_called()

    def test_subagent_with_explicit_opt_in_runs_prefetch(
        self,
        hermes_home: Path,
        write_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(
            api_key="test-key",
            prefetch_in_subagent=True,
            prefetch_cadence=1,
            stream_prefetch=False,
        )
        monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)
        p = HangarxMemoryProvider()
        p.initialize(
            session_id="t",
            hermes_home=str(hermes_home),
            agent_context="subagent",
        )
        fake = MagicMock()
        fake.workspace_id = ""
        fake.ask_context.return_value = "ctx"
        p._client = fake
        p._turn_counter = 0
        p.queue_prefetch("question")
        import time
        time.sleep(0.2)
        # Explicit opt-in → prefetch runs even in subagent context.
        fake.ask_context.assert_called()
