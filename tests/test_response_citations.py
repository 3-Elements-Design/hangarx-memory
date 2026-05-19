"""Tests for B — per-response citation directive.

When citations are part of the prefetch context, the merged context
block also includes a short "Response style" directive that tells the
model to surface a "Based on:" footer in its reply. This is the
Perplexity-style auditable-answer pattern, shipped as a context-only
soft prompt (no Hermes hook required).
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import hangarx_memory.provider as provider_mod
from hangarx_memory import HangarxMemoryProvider


@pytest.fixture
def provider_with_citations(
    hermes_home: Path,
    write_config,
    monkeypatch: pytest.MonkeyPatch,
):
    """Build a primary-context provider that runs cortex prefetch and
    populates citations.

    The returned factory accepts a citations list to inject into the
    fake client's response. The factory waits for the prefetch thread
    to finish so callers can read the cache deterministically.
    """
    monkeypatch.setattr(provider_mod, "probe_health", lambda *a, **kw: None)

    def _make(
        citations: list[dict] | None,
        *,
        response_citations: bool = True,
        ask_text: str = "context block from cortex",
    ) -> HangarxMemoryProvider:
        write_config(
            api_key="test-key",
            workspace_id="ws",
            prefetch_cadence=1,
            stream_prefetch=False,
            response_citations=response_citations,
            citation_wikilinks=True,
        )
        provider = HangarxMemoryProvider()
        provider.initialize(
            session_id="t",
            hermes_home=str(hermes_home),
            agent_context="primary",
        )
        fake = MagicMock()
        fake.workspace_id = "ws"
        # Match the shape _extract_ask_context + _extract_citations
        # expect — either a flat {context, citations} dict OR a
        # {result: {...}} envelope. We use the flat form.
        fake.ask_context.return_value = {
            "context": ask_text,
            "citations": citations or [],
        }
        provider._client = fake
        provider._turn_counter = 0
        provider.queue_prefetch("what does the user prefer?")
        # Wait for the daemon thread to finish populating the cache.
        for _ in range(20):
            with provider._prefetch_lock:
                if provider._prefetch_cache:
                    break
            time.sleep(0.05)
        return provider

    return _make


class TestCitationDirective:
    def test_directive_appended_when_citations_present(
        self, provider_with_citations
    ) -> None:
        p = provider_with_citations([
            {"source": "Hermes Sessions/Memory About Me",
             "memory_id": "mem_1",
             "text": "user prefers concise replies"},
        ])
        cache = p._prefetch_cache
        assert "### Citations" in cache
        assert "### Response style" in cache
        assert "Based on:" in cache
        assert "[[wikilinks]]" in cache
        assert "memory:<id>" in cache

    def test_no_directive_when_no_citations(
        self, provider_with_citations
    ) -> None:
        p = provider_with_citations(citations=None)
        cache = p._prefetch_cache
        # Without citations the directive is irrelevant — model has
        # nothing to cite anyway.
        assert "Response style" not in cache

    def test_disabled_via_config(
        self, provider_with_citations
    ) -> None:
        p = provider_with_citations(
            citations=[
                {"source": "X", "memory_id": "y", "text": "fact"},
            ],
            response_citations=False,
        )
        cache = p._prefetch_cache
        # Citations block still present...
        assert "### Citations" in cache
        # ...but the response-style directive is suppressed.
        assert "Response style" not in cache
        assert "Based on:" not in cache


class TestCitationFormat:
    """The directive's wording is part of the user-visible contract.

    If we change the exact instruction phrasing, downstream prompts /
    eval suites may depend on it — pin the canonical bits here so we
    notice when it drifts.
    """

    def test_directive_mentions_vault_and_cortex_formats(
        self, provider_with_citations
    ) -> None:
        p = provider_with_citations([
            {"source": "Notes/Note", "memory_id": "m1", "text": "fact"}
        ])
        cache = p._prefetch_cache
        # Vault note format
        assert "[[wikilinks]]" in cache
        # Cortex memory format
        assert "memory:<id>" in cache
        # Escape hatch for irrelevant citations
        assert "Skip the footer" in cache or "skip" in cache.lower()
