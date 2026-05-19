"""Tests for #11 — profile-templated workspace ids.

When Hermes activates the provider with an ``agent_identity`` (the
profile name), the provider derives a deterministic ``workspace_id`` so
each profile gets its own clean Cortex memory bucket.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider
from hangarx_memory.provider import _slugify_workspace

# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


class TestSlugifyWorkspace:
    @pytest.mark.parametrize("raw,expected", [
        ("coder", "coder"),
        ("Research", "research"),
        ("personal notes", "personal-notes"),
        ("MY/Project", "my-project"),
        ("with:colon", "with-colon"),
        ("spaces  and\ttabs", "spaces-and-tabs"),
        ("--leading--", "leading"),
        ("symbols!@#$%^&*()", "symbols"),
        ("under_score.dot-hyphen", "under_score.dot-hyphen"),
        ("", ""),
        ("   ", ""),
    ])
    def test_slugify(self, raw: str, expected: str) -> None:
        assert _slugify_workspace(raw) == expected


# ---------------------------------------------------------------------------
# Template expansion
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_provider(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Factory that returns provider instances initialized with the given kwargs.

    Uses the default config (template = 'hermes-{identity}') unless a
    custom config is written first. Stubs probe_health so initialize()
    doesn't accidentally find a real local Cortex.
    """
    monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)

    def _make(**init_kwargs) -> HangarxMemoryProvider:
        provider = HangarxMemoryProvider()
        provider.initialize(
            session_id="t",
            hermes_home=str(hermes_home),
            **init_kwargs,
        )
        return provider

    return _make


class TestDefaultTemplate:
    def test_identity_becomes_workspace(self, offline_provider) -> None:
        p = offline_provider(agent_identity="coder")
        assert p._config["workspace_id"] == "hermes-coder"
        assert p._config.get("workspace_from_template") is True

    def test_identity_is_slugified(self, offline_provider) -> None:
        p = offline_provider(agent_identity="My Research Profile")
        assert p._config["workspace_id"] == "hermes-my-research-profile"

    def test_two_profiles_get_distinct_workspaces(
        self, offline_provider
    ) -> None:
        p1 = offline_provider(agent_identity="coder")
        p2 = offline_provider(agent_identity="research")
        assert p1._config["workspace_id"] != p2._config["workspace_id"]
        assert p1._config["workspace_id"] == "hermes-coder"
        assert p2._config["workspace_id"] == "hermes-research"


class TestExplicitWorkspaceWins:
    def test_config_workspace_id_not_overridden(
        self,
        offline_provider,
        write_config,
    ) -> None:
        write_config(workspace_id="my-pinned-workspace")
        p = offline_provider(agent_identity="coder")
        # Explicit config wins — template is ignored.
        assert p._config["workspace_id"] == "my-pinned-workspace"
        assert p._config.get("workspace_from_template", False) is False

    def test_env_workspace_id_not_overridden(
        self,
        offline_provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CORTEX_WORKSPACE_ID", "env-workspace")
        p = offline_provider(agent_identity="coder")
        assert p._config["workspace_id"] == "env-workspace"


class TestNoIdentity:
    def test_no_identity_leaves_workspace_empty(self, offline_provider) -> None:
        # When running outside Hermes (no agent_identity kwarg), the
        # template should NOT fire — we'd rather have an empty
        # workspace_id (Cortex uses its default) than guess a slug.
        p = offline_provider()
        assert p._config["workspace_id"] == ""
        assert p._config.get("workspace_from_template", False) is False


class TestCustomTemplate:
    def test_workspace_label_substituted(
        self,
        offline_provider,
        write_config,
    ) -> None:
        write_config(workspace_template="{workspace}-{identity}")
        p = offline_provider(
            agent_identity="coder", agent_workspace="myorg"
        )
        assert p._config["workspace_id"] == "myorg-coder"

    def test_template_without_identity_placeholder(
        self,
        offline_provider,
        write_config,
    ) -> None:
        # Some users may want all profiles to share a fixed workspace
        # via a template with no placeholders. Should still produce a
        # stable value.
        write_config(workspace_template="shared-workspace")
        p = offline_provider(agent_identity="coder")
        assert p._config["workspace_id"] == "shared-workspace"

    def test_empty_template_disables_feature(
        self,
        offline_provider,
        write_config,
    ) -> None:
        write_config(workspace_template="")
        p = offline_provider(agent_identity="coder")
        # Empty template → no workspace_id derivation, even with identity.
        assert p._config["workspace_id"] == ""


class TestClientReceivesWorkspaceId:
    def test_client_workspace_id_set_from_template(
        self,
        offline_provider,
        monkeypatch: pytest.MonkeyPatch,
        write_config,
    ) -> None:
        # Need an api_key to construct the real client.
        write_config(api_key="test-key")
        p = offline_provider(agent_identity="coder")
        assert p._client is not None
        # The Cortex client must carry the templated workspace_id so
        # every request is scoped correctly.
        assert p._client.workspace_id == "hermes-coder"
