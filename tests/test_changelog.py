"""Tests for the memory changelog (v0.5.0).

Covers:
- MemoryChangelog ring buffer + vault sink in isolation
- Provider integration: on_memory_write, merge tool, auto_promote, revert
- cortex_memory_changelog tool dispatch
- cortex_revert_memory tool dispatch + error paths
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider
from hangarx_memory.changelog import (
    MAX_SUMMARY_LEN,
    MemoryChangelog,
    extract_memory_id,
)
from hangarx_memory.client import CortexError

# ---------------------------------------------------------------------------
# MemoryChangelog unit tests — no provider involved
# ---------------------------------------------------------------------------


class TestRingBuffer:
    def test_records_action_with_defaults(self) -> None:
        log = MemoryChangelog(ring_buffer_size=10)
        entry = log.record("ADDED", summary="hello", memory_id="m1")
        assert entry["action"] == "ADDED"
        assert entry["summary"] == "hello"
        assert entry["memory_id"] == "m1"
        assert entry["when"].endswith("Z")
        assert len(log) == 1

    def test_action_is_uppercased(self) -> None:
        log = MemoryChangelog()
        entry = log.record("merged", summary="x")
        assert entry["action"] == "MERGED"

    def test_long_summary_truncated(self) -> None:
        log = MemoryChangelog()
        long_text = "x" * (MAX_SUMMARY_LEN + 50)
        entry = log.record("ADDED", summary=long_text)
        assert len(entry["summary"]) <= MAX_SUMMARY_LEN
        assert entry["summary"].endswith("…")

    def test_buffer_bounded(self) -> None:
        log = MemoryChangelog(ring_buffer_size=3)
        for i in range(10):
            log.record("ADDED", summary=f"item-{i}")
        assert len(log) == 3
        entries = log.recent(limit=10)
        # Newest first; only the last 3 survive.
        assert [e["summary"] for e in entries] == [
            "item-9", "item-8", "item-7"
        ]

    def test_recent_newest_first(self) -> None:
        log = MemoryChangelog()
        log.record("ADDED", summary="first")
        log.record("ADDED", summary="second")
        log.record("MERGED", summary="third")
        out = log.recent(limit=10)
        assert [e["summary"] for e in out] == ["third", "second", "first"]

    def test_recent_respects_limit(self) -> None:
        log = MemoryChangelog()
        for i in range(20):
            log.record("ADDED", summary=str(i))
        assert len(log.recent(limit=5)) == 5

    def test_recent_handles_invalid_limit(self) -> None:
        log = MemoryChangelog()
        log.record("ADDED", summary="x")
        out = log.recent(limit="not-a-number")  # type: ignore[arg-type]
        assert len(out) == 1


class TestVaultSink:
    def test_writes_to_vault_when_configured(self) -> None:
        vault = MagicMock()
        vault.config.sessions_folder = "Hermes Sessions"
        log = MemoryChangelog(
            vault=vault, sessions_folder="Hermes Sessions", agent_id="hermes"
        )
        log.record("ADDED", summary="hello world", memory_id="m1",
                   category="user_fact")
        vault.append_note.assert_called_once()
        call = vault.append_note.call_args
        assert call.args[0] == "Hermes Sessions/Memory Changelog.md"
        assert "ADDED" in call.args[1]
        assert "user_fact" in call.args[1]
        assert "m1" in call.args[1]
        assert "hello world" in call.args[1]

    def test_no_vault_means_no_write_no_crash(self) -> None:
        log = MemoryChangelog(vault=None)
        # Should not raise
        log.record("ADDED", summary="x")
        assert len(log) == 1

    def test_vault_write_failure_doesnt_break_record(self) -> None:
        vault = MagicMock()
        vault.append_note.side_effect = OSError("disk full")
        log = MemoryChangelog(vault=vault)
        # Should not raise — record() must always succeed for in-process
        # buffer even if vault is broken.
        entry = log.record("ADDED", summary="x")
        assert entry["action"] == "ADDED"
        assert len(log) == 1


class TestExtractMemoryId:
    def test_data_envelope(self) -> None:
        assert extract_memory_id({"data": {"id": "abc"}}) == "abc"

    def test_camel_case(self) -> None:
        assert extract_memory_id({"memoryId": "xyz"}) == "xyz"

    def test_snake_case(self) -> None:
        assert extract_memory_id({"memory_id": "qqq"}) == "qqq"

    def test_no_id(self) -> None:
        assert extract_memory_id({"foo": "bar"}) == ""

    def test_non_dict(self) -> None:
        assert extract_memory_id("not a dict") == ""
        assert extract_memory_id(None) == ""
        assert extract_memory_id(["list"]) == ""


# ---------------------------------------------------------------------------
# Provider integration
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_provider(
    hermes_home: Path,
    write_config,
    monkeypatch: pytest.MonkeyPatch,
) -> HangarxMemoryProvider:
    """Provider initialized with a fake client; no vault."""
    write_config(
        api_key="test-key",
        base_url="http://test.invalid",
        workspace_id="ws-001",
    )
    monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)
    provider = HangarxMemoryProvider()
    provider.initialize(session_id="t", hermes_home=str(hermes_home))
    return provider


@pytest.fixture
def fake_client() -> MagicMock:
    client = MagicMock()
    client.workspace_id = "ws-001"
    client.remember.return_value = {"data": {"id": "mem_new123"}}
    client.forget.return_value = {"success": True}
    client.auto_promote.return_value = {
        "data": {"promoted": 7, "merged": 2},
    }
    client.mcp_call_tool.return_value = {"success": True}
    return client


class TestChangelogInit:
    def test_changelog_initialized(
        self, initialized_provider: HangarxMemoryProvider
    ) -> None:
        assert initialized_provider._changelog is not None
        assert len(initialized_provider._changelog) == 0

    def test_changelog_disabled_via_config(
        self,
        hermes_home: Path,
        write_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(api_key="k", changelog_enabled=False)
        monkeypatch.setattr(
            provider_mod, "probe_health", lambda *a, **kw: None
        )
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._changelog is None

    def test_buffer_size_honored(
        self,
        hermes_home: Path,
        write_config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(api_key="k", changelog_buffer_size=5)
        monkeypatch.setattr(
            provider_mod, "probe_health", lambda *a, **kw: None
        )
        provider = HangarxMemoryProvider()
        provider.initialize(session_id="t", hermes_home=str(hermes_home))
        assert provider._changelog is not None
        for i in range(10):
            provider._changelog.record("ADDED", summary=f"x{i}")
        assert len(provider._changelog) == 5


class TestOnMemoryWriteLogsAddition:
    def test_add_action_logs_added(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider.on_memory_write(
            "add", "user", "User prefers concise replies"
        )
        entries = initialized_provider._changelog.recent()
        assert len(entries) == 1
        assert entries[0]["action"] == "ADDED"
        assert entries[0]["memory_id"] == "mem_new123"
        assert "concise" in entries[0]["summary"]
        assert entries[0]["source"] == "on_memory_write"

    def test_remove_action_logs_forgot(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider.on_memory_write("remove", "user", "stale fact")
        entries = initialized_provider._changelog.recent()
        assert len(entries) == 1
        assert entries[0]["action"] == "FORGOT"

    def test_missing_memory_id_logged_empty(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        # Cortex sometimes returns no id (e.g. async write).
        fake_client.remember.return_value = {"success": True}
        initialized_provider._client = fake_client
        initialized_provider.on_memory_write("add", "user", "fact")
        entries = initialized_provider._changelog.recent()
        assert entries[0]["memory_id"] == ""


class TestMergeToolLogsMerged:
    def test_merge_dispatch_records_entry(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider.handle_tool_call(
            "cortex_merge_entities",
            {"target_id": "ent_main", "source_id": "ent_dup"},
        )
        entries = initialized_provider._changelog.recent()
        assert any(
            e["action"] == "MERGED" and e["memory_id"] == "ent_main"
            for e in entries
        )


class TestAutoPromoteLogsPromoted:
    def test_session_end_records_promoted_count(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider.on_session_end([])
        entries = initialized_provider._changelog.recent()
        assert any(
            e["action"] == "PROMOTED"
            and e["details"].get("promoted_count") == 7
            for e in entries
        )

    def test_no_promotions_means_no_entry(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        fake_client.auto_promote.return_value = {"data": {"promoted": 0}}
        initialized_provider._client = fake_client
        initialized_provider.on_session_end([])
        entries = initialized_provider._changelog.recent()
        # The promoted-count==0 path should NOT log a PROMOTED entry.
        assert not any(e["action"] == "PROMOTED" for e in entries)


# ---------------------------------------------------------------------------
# Tool: cortex_memory_changelog
# ---------------------------------------------------------------------------


class TestChangelogTool:
    def test_returns_recent_entries(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider._changelog.record("ADDED", summary="first")
        initialized_provider._changelog.record("MERGED", summary="second")
        result = initialized_provider.handle_tool_call(
            "cortex_memory_changelog", {}
        )
        decoded = json.loads(result)
        assert decoded["count"] == 2
        assert decoded["entries"][0]["summary"] == "second"  # newest first
        assert decoded["entries"][1]["summary"] == "first"

    def test_limit_applied(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        for i in range(10):
            initialized_provider._changelog.record("ADDED", summary=str(i))
        result = initialized_provider.handle_tool_call(
            "cortex_memory_changelog", {"limit": 3}
        )
        decoded = json.loads(result)
        assert decoded["count"] == 3

    def test_changelog_disabled_returns_error(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider._changelog = None
        result = initialized_provider.handle_tool_call(
            "cortex_memory_changelog", {}
        )
        decoded = json.loads(result)
        assert "error" in decoded
        assert decoded["entries"] == []


# ---------------------------------------------------------------------------
# Tool: cortex_revert_memory
# ---------------------------------------------------------------------------


class TestRevertTool:
    def test_revert_calls_forget_and_logs(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call(
            "cortex_revert_memory",
            {"memory_id": "mem_bad", "reason": "user said it was wrong"},
        )
        fake_client.forget.assert_called_once_with(
            "mem_bad", agent_id=initialized_provider._agent_id
        )
        decoded = json.loads(result)
        assert decoded["reverted"] is True
        assert decoded["memory_id"] == "mem_bad"
        assert decoded["reason"] == "user said it was wrong"

        # Changelog must have a REVERTED entry.
        entries = initialized_provider._changelog.recent()
        assert any(
            e["action"] == "REVERTED" and e["memory_id"] == "mem_bad"
            for e in entries
        )

    def test_missing_memory_id_returns_error(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call(
            "cortex_revert_memory", {}
        )
        decoded = json.loads(result)
        assert "error" in decoded
        fake_client.forget.assert_not_called()

    def test_cortex_failure_no_revert_entry(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        fake_client.forget.side_effect = CortexError("not found")
        initialized_provider._client = fake_client
        result = initialized_provider.handle_tool_call(
            "cortex_revert_memory", {"memory_id": "missing"}
        )
        decoded = json.loads(result)
        assert "error" in decoded
        # No REVERTED entry should be logged — Cortex rejected the call.
        entries = initialized_provider._changelog.recent()
        assert not any(e["action"] == "REVERTED" for e in entries)


# ---------------------------------------------------------------------------
# Tool schema registration
# ---------------------------------------------------------------------------


class TestSchemaRegistration:
    AUDIT_TOOLS = {"cortex_memory_changelog", "cortex_revert_memory"}

    def test_registered_in_full_mode(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        names = {s["name"] for s in initialized_provider.get_tool_schemas()}
        assert self.AUDIT_TOOLS.issubset(names)

    def test_not_in_compact_mode(
        self,
        initialized_provider: HangarxMemoryProvider,
        fake_client: MagicMock,
    ) -> None:
        initialized_provider._client = fake_client
        initialized_provider._config["tool_mode"] = "compact"
        names = {s["name"] for s in initialized_provider.get_tool_schemas()}
        assert self.AUDIT_TOOLS.isdisjoint(names)
