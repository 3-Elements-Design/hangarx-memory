"""HangarX memory provider for Hermes Agent.

This is a user-installed plugin. Drop the directory under
``$HERMES_HOME/plugins/hangarx-memory/`` and activate it with::

    hermes config set memory.provider hangarx-memory
    hermes memory setup

The provider wraps HangarX Cortex's REST + MCP surfaces and exposes a
small, curated tool set to the model (recall, remember, ask, search,
ingest). Lifecycle hooks mirror Hermes turns into Cortex's GraphRAG /
memU / vector-memory stack, with optional Obsidian vault mirroring.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .client import CortexClient, CortexError, probe_health

try:  # vault.py is a plain module; this import works whether we're
    # loaded as a package (Hermes plugin path) or as a relative module.
    from .vault import Vault, VaultConfig, slugify  # type: ignore
except ImportError:  # pragma: no cover - exercised when loaded by file path
    from vault import Vault, VaultConfig, slugify  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MemoryProvider import shim
# ---------------------------------------------------------------------------
#
# The provider must subclass agent.memory_provider.MemoryProvider. When this
# package is loaded by Hermes the import works directly. When the file is
# imported in isolation (e.g. for unit tests) we fall back to a minimal local
# ABC so the module is still importable.
try:  # pragma: no cover - exercised under Hermes
    from agent.memory_provider import MemoryProvider  # type: ignore
except Exception:  # pragma: no cover - fallback for offline tests
    from abc import ABC, abstractmethod

    class MemoryProvider(ABC):  # type: ignore[no-redef]
        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs) -> None: ...

        def system_prompt_block(self) -> str:
            return ""

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
            return None

        def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
            return None

        @abstractmethod
        def get_tool_schemas(self) -> list[dict[str, Any]]: ...

        def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
            return json.dumps({"error": f"unhandled tool: {tool_name}"})

        def shutdown(self) -> None:
            return None

        def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
            return None

        def on_session_end(self, messages: list[dict[str, Any]]) -> None:
            return None

        def on_session_switch(self, new_session_id: str, **kwargs) -> None:
            return None

        def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
            return ""

        def on_memory_write(
            self,
            action: str,
            target: str,
            content: str,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            return None

        def get_config_schema(self) -> list[dict[str, Any]]:
            return []

        def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
            return None


CONFIG_FILE_NAME = "hangarx-memory.json"
DEFAULT_BASE_URL = "https://cortex.hangarx.ai"
DEFAULT_AGENT_ID = "hermes"

# Hermes' memory.{action,target} -> Cortex category mapping for the
# on_memory_write bridge. The mapping is intentionally conservative.
_MEMORY_CATEGORY = {
    ("add", "user"): "user_fact",
    ("replace", "user"): "user_fact",
    ("remove", "user"): "user_fact_removed",
    ("add", "memory"): "agent_instruction",
    ("replace", "memory"): "agent_instruction",
    ("remove", "memory"): "agent_instruction_removed",
}

# Tool names that only need the vault — no Cortex client required.
_VAULT_TOOL_NAMES = frozenset({
    "vault_search",
    "vault_read_note",
    "vault_list_notes",
    "vault_create_note",
    "vault_append_note",
})


class HangarxMemoryProvider(MemoryProvider):
    """Bridge Hermes' MemoryManager into HangarX Cortex."""

    # -- Construction --------------------------------------------------------

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._client: CortexClient | None = None
        self._session_id: str = ""
        self._agent_id: str = DEFAULT_AGENT_ID
        self._platform: str = "cli"
        self._agent_context: str = "primary"
        self._prefetch_cache: str = ""
        self._prefetch_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._available_cache: bool | None = None
        self._vault: Vault | None = None
        self._session_started_at: float | None = None
        self._turn_counter: int = 0
        # Last batch of Cortex citations from a prefetch — surfaced to the
        # model when streaming, used for #15 vault link injection.
        self._last_citations: list[dict[str, Any]] = []
        # Per-session running list of (turn, summary, citations) so the
        # session-end summary note (#7) can backlink to referenced notes.
        self._session_turn_log: list[dict[str, Any]] = []
        # Memory-id → vault path map for #15 wikilink injection.
        self._memory_id_to_vault: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "hangarx-memory"

    # -- Config loading ------------------------------------------------------

    def _load_config(self, hermes_home: str) -> dict[str, Any]:
        """Merge config.json + env vars. Env wins so .env can override files."""
        cfg: dict[str, Any] = {}
        if hermes_home:
            config_path = Path(hermes_home) / CONFIG_FILE_NAME
            if config_path.is_file():
                try:
                    cfg.update(json.loads(config_path.read_text(encoding="utf-8")))
                except Exception as exc:  # pragma: no cover - corrupt config
                    logger.warning(
                        "hangarx-memory: failed to read %s: %s", config_path, exc
                    )

        api_key = os.environ.get("CORTEX_API_KEY") or cfg.get("api_key", "")
        # ``base_url`` distinguishes "explicit" from "default" so the
        # auto-detect path in initialize() can probe localhost only when
        # the user didn't pin a URL themselves.
        explicit_base_url = (
            os.environ.get("CORTEX_API_URL")
            or cfg.get("base_url")
            or ""
        )
        base_url = explicit_base_url or DEFAULT_BASE_URL
        workspace_id = (
            os.environ.get("CORTEX_WORKSPACE_ID")
            or cfg.get("workspace_id")
            or ""
        )
        organization_id = (
            os.environ.get("CORTEX_ORGANIZATION_ID")
            or cfg.get("organization_id")
            or ""
        )
        auth_mode = (cfg.get("auth_mode") or "bearer").lower()
        agent_id = cfg.get("agent_id") or DEFAULT_AGENT_ID
        timeout = float(cfg.get("timeout") or 15.0)

        vault_path_raw = (
            os.environ.get("HANGARX_VAULT_PATH")
            or cfg.get("vault_path")
            or ""
        )
        vault_sessions_folder = cfg.get("vault_sessions_folder") or "Hermes Sessions"
        vault_link_style = (cfg.get("vault_link_style") or "wikilink").lower()
        if vault_link_style not in {"wikilink", "markdown"}:
            vault_link_style = "wikilink"
        vault_search_enabled = bool(cfg.get("vault_search_enabled", True))
        vault_auto_ingest = bool(cfg.get("vault_auto_ingest", False))
        vault_sync_mode = (cfg.get("vault_sync_mode") or "per-session").lower()
        if vault_sync_mode not in {"off", "per-session", "daily", "per-turn"}:
            vault_sync_mode = "per-session"
        vault_compression_snapshots = bool(cfg.get("vault_compression_snapshots", False))

        # Cadence + tool mode knobs (new in v0.3.0).
        # ``prefetch_cadence`` gates how often /v1/ask/chat fires. 1 = every
        # turn (default), 0 disables, N > 1 fires every Nth turn.
        try:
            prefetch_cadence = max(0, int(cfg.get("prefetch_cadence", 1)))
        except (TypeError, ValueError):
            prefetch_cadence = 1
        # ``profile_cadence`` schedules an additional profile-style recall
        # on the first turn and every N turns thereafter. 0 disables.
        try:
            profile_cadence = max(0, int(cfg.get("profile_cadence", 25)))
        except (TypeError, ValueError):
            profile_cadence = 25
        auto_promote_enabled = bool(cfg.get("auto_promote_enabled", True))
        try:
            auto_promote_limit = max(1, int(cfg.get("auto_promote_limit", 25)))
        except (TypeError, ValueError):
            auto_promote_limit = 25
        tool_mode = (cfg.get("tool_mode") or "full").lower()
        if tool_mode not in {"full", "compact"}:
            tool_mode = "full"
        # v0.4 knobs
        stream_prefetch = bool(cfg.get("stream_prefetch", True))
        dialectic_passes = max(1, min(3, int(cfg.get("dialectic_passes", 1) or 1)))
        session_summary_enabled = bool(cfg.get("session_summary_enabled", True))
        citation_wikilinks = bool(cfg.get("citation_wikilinks", True))

        # Local auto-detect (v0.4.1): when no base_url is explicitly
        # configured, probe a small list of candidate URLs for a local
        # Cortex stack. Skipped when the user pinned a URL or set
        # CORTEX_API_URL. Honours CORTEX_LOCAL_URLS env (comma-separated)
        # for non-default ports.
        auto_detect_local = bool(cfg.get("auto_detect_local", True))
        local_candidates_raw = (
            os.environ.get("CORTEX_LOCAL_URLS")
            or cfg.get("local_candidates")
            or "http://localhost:3400,http://localhost:4000"
        )
        if isinstance(local_candidates_raw, str):
            local_candidates = [
                u.strip() for u in local_candidates_raw.split(",") if u.strip()
            ]
        elif isinstance(local_candidates_raw, list):
            local_candidates = [str(u).strip() for u in local_candidates_raw if u]
        else:
            local_candidates = []
        try:
            local_probe_timeout = max(
                0.05, float(cfg.get("local_probe_timeout", 0.5))
            )
        except (TypeError, ValueError):
            local_probe_timeout = 0.5

        return {
            "api_key": api_key,
            "base_url": base_url,
            "explicit_base_url": bool(explicit_base_url),
            "workspace_id": workspace_id,
            "organization_id": organization_id,
            "auth_mode": auth_mode,
            "agent_id": agent_id,
            "timeout": timeout,
            "prefetch_enabled": bool(cfg.get("prefetch_enabled", True)),
            "sync_turn_enabled": bool(cfg.get("sync_turn_enabled", True)),
            "use_remember_raw": bool(cfg.get("use_remember_raw", True)),
            "vault_path": vault_path_raw,
            "vault_sessions_folder": vault_sessions_folder,
            "vault_link_style": vault_link_style,
            "vault_search_enabled": vault_search_enabled,
            "vault_auto_ingest": vault_auto_ingest,
            "vault_sync_mode": vault_sync_mode,
            "vault_compression_snapshots": vault_compression_snapshots,
            "prefetch_cadence": prefetch_cadence,
            "profile_cadence": profile_cadence,
            "auto_promote_enabled": auto_promote_enabled,
            "auto_promote_limit": auto_promote_limit,
            "tool_mode": tool_mode,
            "stream_prefetch": stream_prefetch,
            "dialectic_passes": dialectic_passes,
            "session_summary_enabled": session_summary_enabled,
            "citation_wikilinks": citation_wikilinks,
            "auto_detect_local": auto_detect_local,
            "local_candidates": local_candidates,
            "local_probe_timeout": local_probe_timeout,
        }

    # -- Local auto-detect ---------------------------------------------------

    def _detect_local_cortex(self) -> str | None:
        """Probe configured candidate URLs for a running Cortex ``/health``.

        Returns the first URL that responds healthy (``ready: true``),
        or ``None`` if none answer. Each probe uses ``local_probe_timeout``
        (default 0.5s) so the total auto-detect cost is bounded — with
        two candidates and a 0.5s timeout, worst case is ~1s before
        the agent loop continues without Cortex.

        Override the candidate list via:
          * config: ``"local_candidates": ["http://localhost:3400", ...]``
          * env: ``CORTEX_LOCAL_URLS="http://localhost:3400,http://..."``
        """
        candidates = self._config.get("local_candidates") or []
        timeout = float(self._config.get("local_probe_timeout") or 0.5)
        for url in candidates:
            data = probe_health(url, timeout=timeout)
            if not data:
                continue
            # Cortex's /health returns status="healthy" + ready=true when
            # FalkorDB + Postgres are both up. Accept either signal so a
            # partially-degraded stack still gets used (recall might be
            # slow but the alternative is no memory at all).
            if data.get("ready") is True or data.get("status") == "healthy":
                return url
        return None

    # -- Availability --------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if either Cortex credentials or a vault path is configured.

        Hermes calls this without first calling ``initialize``, so we peek
        at config + env directly. A configured vault alone is enough to
        run the provider in "local Obsidian only" mode. A reachable local
        Cortex stack is enough to run in "local keyless" mode.
        """
        if self._available_cache is not None:
            return self._available_cache

        if (
            os.environ.get("CORTEX_API_KEY")
            or os.environ.get("HANGARX_VAULT_PATH")
        ):
            self._available_cache = True
            return True

        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        path = Path(home) / CONFIG_FILE_NAME
        cfg_data: dict[str, Any] = {}
        if path.is_file():
            try:
                cfg_data = json.loads(path.read_text(encoding="utf-8"))
                if cfg_data.get("api_key") or cfg_data.get("vault_path"):
                    self._available_cache = True
                    return True
            except Exception:
                cfg_data = {}

        # Final check: probe local candidates. This is slightly more
        # expensive (~500ms per candidate) so do it last. Honors the
        # ``auto_detect_local`` knob — when disabled in config, skip it.
        if cfg_data.get("auto_detect_local", True):
            candidates_raw = (
                os.environ.get("CORTEX_LOCAL_URLS")
                or cfg_data.get("local_candidates")
                or "http://localhost:3400,http://localhost:4000"
            )
            if isinstance(candidates_raw, str):
                candidates = [u.strip() for u in candidates_raw.split(",") if u.strip()]
            elif isinstance(candidates_raw, list):
                candidates = [str(u).strip() for u in candidates_raw if u]
            else:
                candidates = []
            try:
                probe_timeout = max(
                    0.05, float(cfg_data.get("local_probe_timeout", 0.5))
                )
            except (TypeError, ValueError):
                probe_timeout = 0.5
            for url in candidates:
                data = probe_health(url, timeout=probe_timeout)
                if data and (
                    data.get("ready") is True or data.get("status") == "healthy"
                ):
                    self._available_cache = True
                    return True

        self._available_cache = False
        return False

    # -- Lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME") or ""
        self._session_id = session_id or ""
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context", "primary")
        self._session_started_at = time.time()
        self._config = self._load_config(hermes_home)
        self._agent_id = (
            kwargs.get("agent_identity")
            or self._config.get("agent_id")
            or DEFAULT_AGENT_ID
        )

        # Local auto-detect: when no base_url was explicitly configured,
        # probe localhost candidates for a running Cortex stack. If one
        # responds healthy, switch ``base_url`` to it and flip a flag so
        # downstream code knows we're in keyless local mode (cortex-api's
        # AUTH_OPTIONAL_FOR_LOCAL pattern accepts requests without an
        # API key when the stack is running on localhost).
        self._local_mode = False
        if (
            self._config.get("auto_detect_local")
            and not self._config.get("explicit_base_url")
        ):
            detected = self._detect_local_cortex()
            if detected:
                self._config["base_url"] = detected
                self._local_mode = True
                logger.info(
                    "hangarx-memory: auto-detected local Cortex at %s", detected
                )

        # Cortex client construction. We need either an api_key OR a
        # confirmed local stack (which serves without auth in dev mode).
        if not self._config.get("api_key") and not self._local_mode:
            logger.info(
                "hangarx-memory: no API key + no local Cortex; client inactive"
            )
            self._client = None
        else:
            try:
                self._client = CortexClient(
                    base_url=self._config["base_url"],
                    api_key=self._config["api_key"],
                    workspace_id=self._config["workspace_id"],
                    organization_id=self._config["organization_id"],
                    auth_mode=self._config["auth_mode"],
                    timeout=self._config["timeout"],
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("hangarx-memory: failed to construct client: %s", exc)
                self._client = None

        vault_path = (self._config.get("vault_path") or "").strip()
        if vault_path:
            try:
                vault_config = VaultConfig(
                    path=Path(vault_path),
                    sessions_folder=self._config.get("vault_sessions_folder") or "Hermes Sessions",
                    link_style=self._config.get("vault_link_style") or "wikilink",
                    search_enabled=self._config.get("vault_search_enabled", True),
                    auto_ingest=self._config.get("vault_auto_ingest", False),
                    sync_mode=self._config.get("vault_sync_mode") or "per-session",
                )
                vault = Vault(vault_config)
                if vault.is_ready():
                    self._vault = vault
                else:
                    logger.warning(
                        "hangarx-memory: vault path %s is not a directory; vault disabled",
                        vault_path,
                    )
                    self._vault = None
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("hangarx-memory: failed to initialize vault: %s", exc)
                self._vault = None
        else:
            self._vault = None

    def shutdown(self) -> None:
        self._shutdown_event.set()
        thread = self._sync_thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id or self._session_id
        self._prefetch_cache = ""
        self._last_citations = []
        if kwargs.get("reset"):
            self._turn_counter = 0
            self._session_turn_log = []

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        # ``turn_number`` from Hermes is the canonical turn index. Mirror
        # it into the local counter so cadence math is independent of when
        # we initialized (e.g. mid-session resume).
        try:
            self._turn_counter = int(turn_number)
        except (TypeError, ValueError):
            self._turn_counter += 1

    # -- System prompt -------------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._client and not self._vault:
            return ""
        parts: list[str] = []
        if self._client:
            parts.append(
                "HangarX Cortex memory is active. You have tools for grounded recall "
                "across the knowledge graph, vector store, and memU agent memory: "
                "`cortex_recall`, `cortex_remember`, `cortex_ask`, "
                "`cortex_search_documents`, `cortex_search_entities`, "
                "`cortex_query_graph`, `cortex_ingest`. Prefer Cortex tools for any "
                "question about prior conversations, ingested docs, or graph entities."
            )
        if self._vault:
            parts.append(
                "An Obsidian vault is mounted at the configured `vault_path`. "
                "You can read, search, list, create, and append notes with the "
                "`vault_search`, `vault_read_note`, `vault_list_notes`, "
                "`vault_create_note`, `vault_append_note`, and `cortex_ingest_vault` "
                "tools. Local vault search is fast — use it first for project notes, "
                "decisions, and personal context."
            )
        return "\n\n".join(parts)

    # -- Prefetch ------------------------------------------------------------

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not query or not query.strip():
            return
        if not self._config.get("prefetch_enabled", True):
            return
        if not self._client and not self._vault:
            return

        # Cadence gating — fire Cortex only every Nth turn (vault search is
        # cheap and always runs). The local turn counter is bumped in
        # on_turn_start, so this is the count for the *upcoming* turn.
        cadence = int(self._config.get("prefetch_cadence", 1) or 0)
        profile_cadence = int(self._config.get("profile_cadence", 0) or 0)
        next_turn = self._turn_counter + 1
        run_cortex_chat = bool(self._client) and cadence > 0 and (next_turn % cadence == 0)
        run_profile = (
            bool(self._client)
            and profile_cadence > 0
            and (next_turn == 1 or next_turn % profile_cadence == 0)
        )

        # Vault search is local and fast — do it synchronously so the
        # cached context is ready immediately on the next turn even if
        # Cortex is slow or unreachable.
        vault_block = self._vault_prefetch_block(query)

        def _run() -> None:
            cortex_block = ""
            profile_block = ""
            if run_cortex_chat:
                try:
                    use_stream = bool(self._config.get("stream_prefetch", True))
                    if use_stream and hasattr(self._client, "ask_context_stream"):
                        response = self._client.ask_context_stream(query, enhanced=True)
                    else:
                        response = self._client.ask_context(query, enhanced=True)
                    cortex_block = _extract_ask_context(response)
                    self._last_citations = _extract_citations(response)

                    # Optional dialectic refinement (#5). Pass 2 asks Cortex
                    # to refine intent given what pass 1 found — mirrors
                    # Honcho's multi-pass `.chat()` pattern but cheaper.
                    passes = int(self._config.get("dialectic_passes", 1) or 1)
                    if passes >= 2 and cortex_block:
                        refine_query = (
                            "Given the user just asked: " + query[:400] +
                            "\nAnd we already know:\n" + cortex_block[:1200] +
                            "\nWhat additional context, prior decisions, or relevant entities "
                            "would help answer this question well? Be specific."
                        )
                        try:
                            refine_resp = self._client.ask_context(
                                refine_query, enhanced=False
                            )
                            refine_block = _extract_ask_context(refine_resp)
                            if refine_block and refine_block.strip() != cortex_block.strip():
                                cortex_block = (
                                    cortex_block
                                    + "\n\n**Refinement:**\n"
                                    + refine_block
                                )
                                more_cits = _extract_citations(refine_resp)
                                if more_cits:
                                    seen_keys = {
                                        (c.get("source") or "", c.get("memory_id") or "")
                                        for c in self._last_citations
                                    }
                                    for cit in more_cits:
                                        key = (cit.get("source") or "", cit.get("memory_id") or "")
                                        if key not in seen_keys:
                                            self._last_citations.append(cit)
                                            seen_keys.add(key)
                        except CortexError as exc:
                            logger.debug("hangarx-memory: dialectic pass failed: %s", exc)
                except CortexError as exc:
                    logger.debug("hangarx-memory: prefetch failed: %s", exc)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("hangarx-memory: prefetch unexpected error: %s", exc)
            if run_profile:
                try:
                    profile_block = self._build_profile_block()
                except CortexError as exc:
                    logger.debug("hangarx-memory: profile recall failed: %s", exc)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("hangarx-memory: profile recall unexpected error: %s", exc)

            merged = _join_context_blocks(
                ("User Profile", profile_block),
                ("Vault", vault_block),
                ("Cortex GraphRAG", cortex_block),
            )
            if merged and self._last_citations and self._config.get("citation_wikilinks", True):
                citation_block = self._format_citations_block(self._last_citations)
                if citation_block:
                    merged = merged + "\n\n### Citations\n" + citation_block
            if merged:
                with self._prefetch_lock:
                    self._prefetch_cache = merged

        threading.Thread(target=_run, daemon=True).start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._prefetch_lock:
            context = self._prefetch_cache
            self._prefetch_cache = ""
        return context

    # -- Sync turn -----------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._config.get("sync_turn_enabled", True):
            return
        if self._agent_context != "primary":
            return
        if not (user_content or assistant_content):
            return
        if not self._client and not self._vault:
            return

        payload_session = session_id or self._session_id
        content = (
            f"User: {user_content or ''}\n\nAssistant: {assistant_content or ''}"
        ).strip()
        metadata = {
            "session_id": payload_session,
            "platform": self._platform,
            "timestamp": int(time.time()),
        }

        # Record this turn for the end-of-session summary (#7). Snapshot
        # the citations that were live when the turn was prefetched so
        # the summary can backlink to referenced notes.
        if self._config.get("session_summary_enabled", True):
            self._session_turn_log.append({
                "turn": self._turn_counter,
                "user": (user_content or "").strip(),
                "assistant": (assistant_content or "").strip(),
                "citations": list(self._last_citations),
                "timestamp": metadata["timestamp"],
            })

        def _run() -> None:
            if self._client:
                try:
                    if self._config.get("use_remember_raw", True):
                        self._client.remember_raw(  # type: ignore[union-attr]
                            content,
                            agent_id=self._agent_id,
                            category="conversation_turn",
                            metadata=metadata,
                        )
                    else:
                        self._client.remember(  # type: ignore[union-attr]
                            content,
                            agent_id=self._agent_id,
                            category="conversation_insight",
                            metadata=metadata,
                        )
                except CortexError as exc:
                    logger.warning("hangarx-memory: sync_turn failed: %s", exc)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("hangarx-memory: sync_turn unexpected: %s", exc)

            if self._vault and self._config.get("vault_sync_mode", "per-session") != "off":
                try:
                    self._write_vault_turn(
                        payload_session,
                        user_content or "",
                        assistant_content or "",
                        metadata,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("hangarx-memory: vault sync_turn failed: %s", exc)

        # Re-use a single worker slot — sync_turn is best-effort and we don't
        # want to flood Cortex with concurrent writes.
        prior = self._sync_thread
        if prior and prior.is_alive():
            prior.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_run, daemon=True)
        self._sync_thread.start()

    # -- on_memory_write -----------------------------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not content or not content.strip():
            return
        if not self._client and not self._vault:
            return
        category = _MEMORY_CATEGORY.get((action, target), "agent_note")
        meta = dict(metadata or {})
        meta.setdefault("session_id", self._session_id)
        meta.setdefault("platform", self._platform)
        meta.setdefault("action", action)
        meta.setdefault("target", target)
        if self._client:
            try:
                self._client.remember(
                    content,
                    agent_id=self._agent_id,
                    category=category,
                    metadata=meta,
                )
            except CortexError as exc:
                logger.warning("hangarx-memory: on_memory_write failed: %s", exc)
        if self._vault:
            try:
                self._append_vault_memory_entry(action, target, content, category, meta)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("hangarx-memory: vault on_memory_write failed: %s", exc)

    # -- on_pre_compress -----------------------------------------------------

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        if not self._client and not self._vault:
            return ""
        try:
            summary = _summarize_messages(messages)
            if not summary:
                return ""
            if self._client:
                try:
                    self._client.remember_raw(
                        summary,
                        agent_id=self._agent_id,
                        category="pre_compression",
                        metadata={
                            "session_id": self._session_id,
                            "platform": self._platform,
                            "message_count": len(messages),
                        },
                    )
                except CortexError as exc:
                    logger.warning("hangarx-memory: on_pre_compress write failed: %s", exc)
            if self._vault and self._config.get("vault_compression_snapshots", False):
                self._write_vault_compression_snapshot(summary, len(messages))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("hangarx-memory: on_pre_compress unexpected: %s", exc)
        # We don't inject extra text into the compressor prompt; Cortex /
        # the vault now own the pre-compression record so it can be
        # recalled later.
        return ""

    # -- on_session_end ------------------------------------------------------

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if not self._client and not self._vault:
            return
        # Best-effort: fire auto-promote first to distill any verbatim
        # turns we wrote via remember_raw into structured facts, then
        # trigger reflection. Both are non-fatal.
        if self._client and self._config.get("auto_promote_enabled", True):
            try:
                self._client.auto_promote(
                    agent_id=self._agent_id,
                    limit=int(self._config.get("auto_promote_limit", 25) or 25),
                )
            except CortexError as exc:
                logger.debug("hangarx-memory: auto_promote failed: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("hangarx-memory: auto_promote unexpected: %s", exc)
        if self._client:
            try:
                self._client.reflect(agent_id=self._agent_id)
            except CortexError as exc:
                logger.debug("hangarx-memory: reflect failed: %s", exc)
        # #7 — session summary note with backlinks. Writes only when a
        # vault is configured and we recorded at least one turn.
        if self._vault and self._config.get("session_summary_enabled", True):
            try:
                self._write_session_summary_note()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("hangarx-memory: session summary failed: %s", exc)

    # -- Tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        compact = self._config.get("tool_mode", "full") == "compact"
        if self._client:
            cortex_schemas = self._cortex_tool_schemas()
            if compact:
                keep = {"cortex_recall", "cortex_remember", "cortex_ask"}
                cortex_schemas = [s for s in cortex_schemas if s["name"] in keep]
            else:
                cortex_schemas = (
                    cortex_schemas
                    + self._cortex_dedup_tool_schemas()
                    + self._cortex_introspection_tool_schemas()
                )
            schemas.extend(cortex_schemas)
        if self._vault:
            vault_schemas = self._vault_tool_schemas()
            if compact:
                keep = {"vault_search"}
                vault_schemas = [s for s in vault_schemas if s["name"] in keep]
            schemas.extend(vault_schemas)
            if self._client and not compact:
                schemas.append(self._cortex_ingest_vault_schema())
        return schemas

    def _cortex_dedup_tool_schemas(self) -> list[dict[str, Any]]:
        """Tools that let the model clean up its own writes."""
        return [
            {
                "name": "cortex_find_duplicates",
                "description": (
                    "Find near-duplicate entities in the Cortex knowledge graph. "
                    "Returns groups of candidates for merging. Use after a "
                    "bulk ingest or when the graph looks crowded."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entity_type": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                        "threshold": {
                            "type": "number",
                            "description": "Similarity threshold 0–1. Defaults to Cortex server value.",
                        },
                    },
                },
            },
            {
                "name": "cortex_merge_entities",
                "description": (
                    "Merge two entities in the knowledge graph into one. The "
                    "second entity's properties, relationships, and provenance "
                    "are consolidated into the first. Irreversible — call "
                    "cortex_find_duplicates first to identify good candidates."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {
                            "type": "string",
                            "description": "ID of the entity to keep.",
                        },
                        "source_id": {
                            "type": "string",
                            "description": "ID of the entity to merge into the target.",
                        },
                    },
                    "required": ["target_id", "source_id"],
                },
            },
            {
                "name": "cortex_rate_memory",
                "description": (
                    "Rate a memory/recall result as helpful or not. Cortex "
                    "uses this to train trust and rerank signals — a tiny "
                    "feedback loop that improves future recall quality."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "helpful": {"type": "boolean"},
                        "comment": {
                            "type": "string",
                            "description": "Optional free-form note explaining the rating.",
                        },
                    },
                    "required": ["memory_id", "helpful"],
                },
            },
            {
                "name": "cortex_resolve_uri",
                "description": (
                    "Resolve a portable address to its content. Accepts "
                    "``vault://Folder/Note`` (reads the vault note) and "
                    "``cortex://<workspace>/<entity-id>`` or ``cortex://memory/<id>`` "
                    "(fetches the Cortex entity or memory). Use this when a "
                    "previous response handed you a URI you need to follow."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string"},
                    },
                    "required": ["uri"],
                },
            },
        ]

    def _cortex_introspection_tool_schemas(self) -> list[dict[str, Any]]:
        """Tools that let the user audit what the agent remembers about them.

        These wrap the unauthenticated-friendly read paths of Cortex's
        agent-memory module (``GET /v1/memory/stats``, ``categories``,
        ``items``) plus an ``about_me`` synthesis tool that combines
        them into a single human-readable summary.
        """
        return [
            {
                "name": "cortex_memory_stats",
                "description": (
                    "Return high-level memory metrics: total items, total "
                    "categories, items by priority. Use when the user asks "
                    "\"how much do you remember about me?\" or to gauge "
                    "memory health before reflection."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "Narrow to a single agent's memory bank. "
                                "Omit for workspace-wide stats."
                            ),
                        },
                    },
                },
            },
            {
                "name": "cortex_memory_categories",
                "description": (
                    "List memory categories (user_fact, agent_instruction, "
                    "preference, etc.) with item counts. Use when the user "
                    "asks \"what kinds of things do you know about me?\"."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "cortex_memory_items",
                "description": (
                    "List individual stored memory items. Returns the raw "
                    "facts the agent has persisted — use this when the user "
                    "wants to audit what's actually been saved (\"show me "
                    "the actual memories\"). Pair with cortex_forget if "
                    "anything looks wrong."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "description": "Max items to return.",
                            "default": 50,
                        },
                    },
                },
            },
            {
                "name": "cortex_about_me",
                "description": (
                    "Synthesize a human-readable summary of what the agent "
                    "knows about the user. Combines memory stats, "
                    "categories, and a sample of recent items into one "
                    "response. Use when the user asks \"what do you know "
                    "about me?\" — answers in one tool call instead of "
                    "three. The agent should then quote the summary back."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "sample_size": {
                            "type": "integer",
                            "description": "How many sample memory items to include.",
                            "default": 10,
                        },
                    },
                },
            },
        ]

    def _cortex_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "cortex_recall",
                "description": (
                    "Search HangarX Cortex memU agent memory for prior facts, "
                    "user details, or conversation insights relevant to the query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language search query."},
                        "limit": {"type": "integer", "description": "Max items to return.", "default": 5},
                        "method": {
                            "type": "string",
                            "description": "Retrieval method: hybrid, vector, or keyword.",
                            "enum": ["hybrid", "vector", "keyword"],
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "cortex_remember",
                "description": (
                    "Store a durable fact in Cortex memU agent memory for future recall."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The fact or insight to remember."},
                        "category": {
                            "type": "string",
                            "description": "Memory category. Default: conversation_insight.",
                        },
                        "priority": {
                            "type": "string",
                            "description": "normal | high",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "cortex_ask",
                "description": (
                    "Ask Cortex GraphRAG a question against the knowledge graph, "
                    "vector store, and agent memory. Returns a grounded answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Question to answer."},
                        "expanded": {
                            "type": "boolean",
                            "description": "Include context + query plan + raw data.",
                            "default": False,
                        },
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "cortex_search_documents",
                "description": (
                    "Vector-search ingested documents in Cortex for relevant chunks."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "cortex_search_entities",
                "description": (
                    "Hybrid-search entities in the Cortex knowledge graph."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "entity_type": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "cortex_query_graph",
                "description": (
                    "Run an analytical Cypher-backed query through Cortex's graph "
                    "planner for aggregations and traversal questions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "cortex_ingest",
                "description": (
                    "Ingest text (or a URL) into Cortex. Chunks into the vector "
                    "store and optionally extracts entities into the graph."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "extract_entities": {"type": "boolean", "default": True},
                    },
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        args = args or {}

        # Vault-backed tools work even without a Cortex client.
        if tool_name in _VAULT_TOOL_NAMES:
            if not self._vault:
                return json.dumps({"error": "vault not configured (set vault_path in hangarx-memory.json)"})
            try:
                return json.dumps(_make_jsonable(self._handle_vault_tool(tool_name, args)))
            except FileNotFoundError as exc:
                return json.dumps({"error": str(exc)})
            except FileExistsError as exc:
                return json.dumps({"error": str(exc)})
            except KeyError as exc:
                return json.dumps({"error": f"missing required argument: {exc.args[0]}"})
            except ValueError as exc:
                return json.dumps({"error": str(exc)})

        if tool_name == "cortex_ingest_vault":
            if not self._client:
                return json.dumps({"error": "cortex_ingest_vault requires Cortex credentials"})
            if not self._vault:
                return json.dumps({"error": "vault not configured"})
            try:
                note = self._vault.read_note(args["path"])
            except (FileNotFoundError, ValueError) as exc:
                return json.dumps({"error": str(exc)})
            except KeyError as exc:
                return json.dumps({"error": f"missing required argument: {exc.args[0]}"})
            try:
                result = self._client.mcp_call_tool(
                    "cortex_ingest",
                    {
                        "text": note["body"],
                        "title": str(note["frontmatter"].get("title") or note["path"]),
                        "extractEntities": bool(args.get("extract_entities", True)),
                        "workspaceId": self._client.workspace_id or None,
                        "metadata": {
                            "source": "obsidian",
                            "vault_path": note["path"],
                            "frontmatter": note["frontmatter"],
                        },
                    },
                )
                return json.dumps(_make_jsonable(result))
            except CortexError as exc:
                return json.dumps({"error": str(exc)})

        if not self._client:
            return json.dumps({"error": "hangarx-memory not initialized (missing API key)"})
        try:
            if tool_name == "cortex_recall":
                result = self._client.recall(
                    args["query"],
                    limit=args.get("limit"),
                    method=args.get("method"),
                    agent_id=self._agent_id,
                )
            elif tool_name == "cortex_remember":
                result = self._client.remember(
                    args["content"],
                    category=args.get("category"),
                    priority=args.get("priority"),
                    agent_id=self._agent_id,
                )
            elif tool_name == "cortex_ask":
                result = self._client.ask_answer(
                    args["message"],
                    expanded=bool(args.get("expanded", False)),
                )
            elif tool_name == "cortex_search_documents":
                result = self._client.search_documents(
                    args["query"], limit=args.get("limit")
                )
            elif tool_name == "cortex_search_entities":
                result = self._client.mcp_call_tool(
                    "cortex_search_entities",
                    {
                        "query": args["query"],
                        "entity_type": args.get("entity_type"),
                        "limit": args.get("limit") or 10,
                        "workspaceId": self._client.workspace_id or None,
                    },
                )
            elif tool_name == "cortex_query_graph":
                result = self._client.mcp_call_tool(
                    "cortex_query_graph",
                    {
                        "query": args["query"],
                        "workspaceId": self._client.workspace_id or None,
                    },
                )
            elif tool_name == "cortex_ingest":
                result = self._client.mcp_call_tool(
                    "cortex_ingest",
                    {
                        "text": args.get("text"),
                        "url": args.get("url"),
                        "title": args.get("title"),
                        "extractEntities": bool(args.get("extract_entities", True)),
                        "workspaceId": self._client.workspace_id or None,
                    },
                )
            elif tool_name == "cortex_find_duplicates":
                result = self._client.mcp_call_tool(
                    "cortex_find_duplicates",
                    {
                        "entity_type": args.get("entity_type"),
                        "limit": int(args.get("limit") or 10),
                        "threshold": args.get("threshold"),
                        "workspaceId": self._client.workspace_id or None,
                    },
                )
            elif tool_name == "cortex_merge_entities":
                result = self._client.mcp_call_tool(
                    "cortex_merge_entities",
                    {
                        "target_id": args["target_id"],
                        "source_id": args["source_id"],
                        "workspaceId": self._client.workspace_id or None,
                    },
                )
            elif tool_name == "cortex_rate_memory":
                result = self._client.feedback(
                    args["memory_id"],
                    bool(args["helpful"]),
                    comment=args.get("comment"),
                )
            elif tool_name == "cortex_resolve_uri":
                result = self._resolve_uri(str(args["uri"]))
            elif tool_name == "cortex_memory_stats":
                result = self._client.memory_stats(
                    agent_id=args.get("agent_id") or self._agent_id,
                )
            elif tool_name == "cortex_memory_categories":
                result = self._client.memory_categories(
                    agent_id=args.get("agent_id") or self._agent_id,
                )
            elif tool_name == "cortex_memory_items":
                items = self._client.memory_items(
                    agent_id=args.get("agent_id") or self._agent_id,
                )
                # Optional client-side cap so we don't flood the context.
                limit = args.get("limit") or 50
                try:
                    limit = max(1, int(limit))
                except (TypeError, ValueError):
                    limit = 50
                if isinstance(items, list):
                    result = items[:limit]
                elif isinstance(items, dict):
                    inner = items.get("items") or items.get("data") or items
                    if isinstance(inner, list):
                        result = inner[:limit]
                    else:
                        result = items
                else:
                    result = items
            elif tool_name == "cortex_about_me":
                result = self._about_me_summary(
                    agent_id=args.get("agent_id") or self._agent_id,
                    sample_size=args.get("sample_size") or 10,
                )
            else:
                return json.dumps({"error": f"unknown tool: {tool_name}"})
        except CortexError as exc:
            return json.dumps({"error": str(exc)})
        except KeyError as exc:
            return json.dumps({"error": f"missing required argument: {exc.args[0]}"})

        return json.dumps(_make_jsonable(result))

    # -- Vault tools ---------------------------------------------------------

    def _vault_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "vault_search",
                "description": (
                    "Search the local Obsidian vault for notes matching the query. "
                    "Fast, offline, returns snippet + path. Use before reaching for Cortex."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                        "folder": {
                            "type": "string",
                            "description": "Restrict to a vault subfolder (relative).",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "vault_read_note",
                "description": (
                    "Read a note from the Obsidian vault. Accepts wikilinks "
                    "(`[[Note]]`), bare names (`Note`), or relative paths "
                    "(`Folder/Note.md`)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "vault_list_notes",
                "description": "List notes in the vault, optionally filtered by folder and tag.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string"},
                        "tag": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                    },
                },
            },
            {
                "name": "vault_create_note",
                "description": (
                    "Create a new markdown note in the vault. Path is relative to "
                    "the vault root. Frontmatter is optional and merged into a YAML "
                    "header. Refuses to overwrite by default."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "body": {"type": "string"},
                        "frontmatter": {
                            "type": "object",
                            "description": "Optional YAML frontmatter (key: value).",
                        },
                        "overwrite": {"type": "boolean", "default": False},
                    },
                    "required": ["path", "body"],
                },
            },
            {
                "name": "vault_append_note",
                "description": (
                    "Append markdown content to an existing note (or create it). "
                    "Frontmatter updates are merged with existing keys."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "body": {"type": "string"},
                        "frontmatter_updates": {"type": "object"},
                        "create_if_missing": {"type": "boolean", "default": True},
                    },
                    "required": ["path", "body"],
                },
            },
        ]

    def _cortex_ingest_vault_schema(self) -> dict[str, Any]:
        return {
            "name": "cortex_ingest_vault",
            "description": (
                "Ingest a vault note into Cortex's vector store and (optionally) "
                "knowledge graph. Sends the note body with `source: obsidian` "
                "metadata and the frontmatter for provenance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "extract_entities": {"type": "boolean", "default": True},
                },
                "required": ["path"],
            },
        }

    def _handle_vault_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        assert self._vault is not None
        if tool_name == "vault_search":
            return {
                "results": self._vault.search(
                    args["query"],
                    limit=int(args.get("limit") or 5),
                    folder=args.get("folder"),
                )
            }
        if tool_name == "vault_read_note":
            return self._vault.read_note(args["path"])
        if tool_name == "vault_list_notes":
            return {
                "notes": self._vault.list_notes(
                    folder=args.get("folder"),
                    tag=args.get("tag"),
                    limit=int(args.get("limit") or 50),
                )
            }
        if tool_name == "vault_create_note":
            path = self._vault.write_note(
                args["path"],
                args.get("body", ""),
                frontmatter=args.get("frontmatter") or {},
                overwrite=bool(args.get("overwrite", False)),
            )
            return {"path": self._vault.relative(path), "created": True}
        if tool_name == "vault_append_note":
            path = self._vault.append_note(
                args["path"],
                args.get("body", ""),
                frontmatter_updates=args.get("frontmatter_updates") or None,
                create_if_missing=bool(args.get("create_if_missing", True)),
            )
            return {"path": self._vault.relative(path), "appended": True}
        return {"error": f"unknown vault tool: {tool_name}"}

    def _about_me_summary(
        self,
        *,
        agent_id: str | None = None,
        sample_size: int = 10,
    ) -> dict[str, Any]:
        """Synthesize a "what do you know about me" summary in one tool call.

        Pulls stats + categories + a sample of recent memory items from
        Cortex and stitches them into a structured response the agent
        can quote back to the user. Each section degrades gracefully so
        a partial failure (e.g. categories endpoint slow) still returns
        something useful.
        """
        if not self._client:
            return {
                "error": "cortex_about_me requires Cortex credentials or a local stack",
                "stats": None,
                "categories": [],
                "sample_items": [],
            }

        agent = agent_id or self._agent_id
        try:
            sample_size_int = max(1, min(50, int(sample_size)))
        except (TypeError, ValueError):
            sample_size_int = 10

        summary: dict[str, Any] = {
            "agent_id": agent,
            "workspace_id": self._client.workspace_id or None,
            "stats": None,
            "categories": [],
            "sample_items": [],
            "errors": [],
        }

        # 1. Top-level counts.
        try:
            stats = self._client.memory_stats(agent_id=agent)
            if isinstance(stats, dict) and "data" in stats:
                stats = stats["data"]
            summary["stats"] = stats
        except CortexError as exc:
            summary["errors"].append(f"stats: {exc}")

        # 2. Category breakdown.
        try:
            cats = self._client.memory_categories(agent_id=agent)
            if isinstance(cats, dict) and "data" in cats:
                cats = cats["data"]
            if isinstance(cats, list):
                summary["categories"] = cats
        except CortexError as exc:
            summary["errors"].append(f"categories: {exc}")

        # 3. Sample of recent items so the user can see actual stored facts.
        try:
            items = self._client.memory_items(agent_id=agent)
            if isinstance(items, dict) and "data" in items:
                items = items["data"]
            if isinstance(items, list):
                # Cortex returns items newest-first by default; cap the
                # sample so we don't dump the whole memory bank into
                # the response.
                summary["sample_items"] = items[:sample_size_int]
                summary["total_items_returned"] = len(items)
        except CortexError as exc:
            summary["errors"].append(f"items: {exc}")

        # 4. Optional profile recall — gives the model a curated view of
        # high-signal user facts. Use a generic query so we don't need
        # to know what's stored.
        try:
            profile = self._client.recall(
                "what do you know about the user",
                limit=sample_size_int,
                method="hybrid",
            )
            if isinstance(profile, dict) and "data" in profile:
                profile = profile["data"]
            summary["profile_recall"] = profile
        except CortexError as exc:
            summary["errors"].append(f"profile_recall: {exc}")
            summary["profile_recall"] = None

        return summary

    def _resolve_uri(self, uri: str) -> dict[str, Any]:
        """Route ``vault://`` and ``cortex://`` URIs to their backends.

        ``vault://Folder/Note`` returns the read note (same shape as
        vault_read_note). ``cortex://memory/<id>`` looks up a memU
        memory by ID. ``cortex://entity/<id>`` (or the legacy
        ``cortex://<workspace>/<id>`` form) routes through MCP
        ``cortex_get_entity``.
        """
        if not uri:
            return {"error": "uri required"}
        cleaned = uri.strip()
        if cleaned.startswith("vault://"):
            if not self._vault:
                return {"error": "vault not configured"}
            return self._vault.read_note(cleaned[len("vault://"):])
        if not cleaned.startswith("cortex://"):
            return {"error": f"unsupported URI scheme: {uri!r}"}
        if not self._client:
            return {"error": "cortex client not initialized"}
        path = cleaned[len("cortex://"):]
        # Two grammars we accept:
        #   memory/<id>     → /v1/memory/items/<id>
        #   entity/<id>     → MCP cortex_get_entity
        #   <workspace>/<id> (legacy) → MCP cortex_get_entity scoped to that workspace
        parts = [p for p in path.split("/") if p]
        if not parts:
            return {"error": "empty cortex URI"}
        kind = parts[0].lower()
        if kind == "memory" and len(parts) >= 2:
            return self._client.request("GET", f"/v1/memory/items/{parts[1]}")
        if kind == "entity" and len(parts) >= 2:
            return self._client.mcp_call_tool(
                "cortex_get_entity",
                {
                    "id": parts[1],
                    "workspaceId": self._client.workspace_id or None,
                },
            )
        # Legacy "cortex://<workspace>/<entity-id>" form.
        if len(parts) == 2:
            return self._client.mcp_call_tool(
                "cortex_get_entity",
                {"id": parts[1], "workspaceId": parts[0] or None},
            )
        return {"error": f"unrecognized cortex URI: {uri!r}"}

    # -- Vault writers used by lifecycle hooks ------------------------------

    def _format_citations_block(self, citations: list[dict[str, Any]]) -> str:
        """Turn Cortex citation entries into a markdown bullet list.

        Each citation whose ``source`` resolves to a real vault note
        becomes a wikilink so Obsidian's graph view picks it up.
        Citations without a vault match still appear, just as plain
        source strings. Idempotent — safe to call repeatedly.
        """
        if not citations:
            return ""
        lines: list[str] = []
        for cit in citations[:8]:
            source = (cit.get("source") or "").strip() if isinstance(cit, dict) else ""
            memory_id = cit.get("memory_id") or cit.get("memoryId") or "" if isinstance(cit, dict) else ""
            text = (cit.get("text") or "").strip() if isinstance(cit, dict) else ""
            if not source and not memory_id:
                continue
            label = self._resolve_citation_to_vault(source)
            head = label or source or f"memory `{memory_id}`"
            snippet = text[:140] + ("…" if len(text) > 140 else "")
            if snippet:
                lines.append(f"- {head} — {snippet}")
            else:
                lines.append(f"- {head}")
        return "\n".join(lines)

    def _resolve_citation_to_vault(self, source: str) -> str:
        """If ``source`` looks like a vault path, return an Obsidian wikilink.

        Handles three input shapes:
          * ``vault://Folder/Note`` → resolves directly
          * ``Folder/Note`` (relative path inside the vault) → resolved
            against the vault root
          * Any string whose basename matches an existing ``.md`` file
            anywhere in the vault → wikilink to that note

        Returns "" when no vault is configured or no note matches.
        """
        if not self._vault or not source:
            return ""
        cleaned = source.strip()
        if cleaned.startswith("vault://"):
            cleaned = cleaned[len("vault://"):]
        try:
            resolved = self._vault._resolve_note(cleaned)  # type: ignore[attr-defined]
        except Exception:
            resolved = None
        if not resolved:
            return ""
        rel = self._vault.relative(resolved)
        return self._vault.format_link(rel)

    def _build_profile_block(self) -> str:
        """Synthesize a stable user-profile snippet from memU memories.

        Pulls the highest-priority items via ``cortex_recall`` with a
        generic prompt — matches supermemory/honcho's profile-recall
        pattern. Returns a small bullet list, or empty string if Cortex
        returned nothing useful.
        """
        if not self._client:
            return ""
        try:
            result = self._client.recall(
                "important user facts, preferences, identity, ongoing projects",
                limit=8,
                method="hybrid",
                agent_id=self._agent_id,
            )
        except CortexError:
            return ""
        items = []
        if isinstance(result, dict):
            items = result.get("items") or result.get("memories") or []
            if not items and isinstance(result.get("data"), dict):
                items = result["data"].get("items") or result["data"].get("memories") or []
        if not isinstance(items, list) or not items:
            return ""
        lines: list[str] = []
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or item.get("text") or item.get("summary")
            if not content:
                continue
            category = item.get("category") or item.get("type") or ""
            tag = f" _[{category}]_" if category else ""
            first = str(content).strip().splitlines()[0]
            lines.append(f"- {first}{tag}")
        return "\n".join(lines)

    def _vault_prefetch_block(self, query: str) -> str:
        if not self._vault or not self._config.get("vault_search_enabled", True):
            return ""
        try:
            hits = self._vault.search(query, limit=4)
        except Exception:  # pragma: no cover - defensive
            return ""
        if not hits:
            return ""
        lines = []
        for hit in hits:
            link = self._vault.format_link(hit["path"])
            snippet = (hit.get("snippet") or "").replace("\n", " ").strip()
            lines.append(f"- {link} — {snippet}")
        return "\n".join(lines)

    def _write_vault_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        metadata: dict[str, Any],
    ) -> None:
        if not self._vault:
            return
        mode = (self._config.get("vault_sync_mode") or "per-session").lower()
        if mode == "off":
            return

        started = self._session_started_at
        when = _dt.datetime.fromtimestamp(started) if started else _dt.datetime.now()
        if mode == "daily":
            rel_path = self._vault.daily_note_path()
            initial_frontmatter = {
                "type": "hermes-daily-log",
                "date": _dt.datetime.now().strftime("%Y-%m-%d"),
                "agent_id": self._agent_id,
            }
        else:
            # per-session and per-turn both write to the per-session note;
            # per-turn just bypasses session-level grouping later.
            rel_path = self._vault.session_note_path(session_id, started_at=when)
            initial_frontmatter = {
                "type": "hermes-session",
                "session_id": session_id,
                "created": when.strftime("%Y-%m-%dT%H:%M:%S"),
                "agent_id": self._agent_id,
                "platform": self._platform,
                "tags": ["hermes"],
            }

        timestamp = _dt.datetime.now().strftime("%H:%M")
        block = (
            f"## {timestamp} — User\n"
            f"{user_content.strip()}\n\n"
            f"## {timestamp} — Assistant\n"
            f"{assistant_content.strip()}\n"
        )

        self._vault.append_note(
            rel_path,
            block,
            frontmatter_updates=None,
            initial_frontmatter=initial_frontmatter,
            create_if_missing=True,
        )

    def _append_vault_memory_entry(
        self,
        action: str,
        target: str,
        content: str,
        category: str,
        metadata: dict[str, Any],
    ) -> None:
        if not self._vault:
            return
        index_path = f"{self._vault.config.sessions_folder.strip('/')}/Hermes Memory.md"
        when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        first_line = content.strip().splitlines()[0] if content.strip() else "(empty)"
        block = (
            f"- **{when}** · `{action}` `{target}` → `{category}`\n"
            f"    - {first_line}"
        )
        if len(content.strip().splitlines()) > 1:
            rest = "\n      ".join(content.strip().splitlines()[1:])
            block = f"{block}\n      {rest}"
        self._vault.append_note(
            index_path,
            block,
            initial_frontmatter={
                "type": "hermes-memory-index",
                "agent_id": self._agent_id,
                "tags": ["hermes", "memory"],
            },
            create_if_missing=True,
        )

    def _write_session_summary_note(self) -> None:
        """End-of-session digest note with backlinks to every cited source.

        Writes to ``$VAULT/Hermes Sessions/Summaries/<session_id>.md``.
        Aggregates every turn in ``_session_turn_log`` into:

          * a YAML frontmatter block listing turn count + linked notes
          * a short prose intro
          * a per-turn bullet log (question + first sentence of answer)
          * a "Sources" section with deduped wikilinks
        """
        if not self._vault or not self._session_turn_log:
            return
        base = self._vault.config.sessions_folder.strip("/")
        rel = f"{base}/Summaries/{slugify(self._session_id or 'session')}.md"

        # Gather all unique citations across turns
        seen_sources: set = set()
        ordered_links: list[str] = []
        related_paths: list[str] = []
        for turn in self._session_turn_log:
            for cit in turn.get("citations") or []:
                if not isinstance(cit, dict):
                    continue
                source = (cit.get("source") or "").strip()
                if not source or source in seen_sources:
                    continue
                seen_sources.add(source)
                wikilink = self._resolve_citation_to_vault(source)
                if wikilink:
                    ordered_links.append(wikilink)
                    # Capture the bare relative path (no [[...]] wrapping)
                    # for the frontmatter `related` field.
                    inner = wikilink.strip("[]").split("|")[0]
                    related_paths.append(inner)
                else:
                    ordered_links.append(f"`{source}`")

        # Build the markdown body
        when = _dt.datetime.now()
        lines: list[str] = []
        turn_count = len(self._session_turn_log)
        lines.append(
            f"Session **{self._session_id}** wrapped up at "
            f"{when.strftime('%Y-%m-%d %H:%M')} after {turn_count} turn"
            f"{'s' if turn_count != 1 else ''}."
        )
        lines.append("")
        lines.append("## Turn log")
        for turn in self._session_turn_log:
            user = (turn.get("user") or "").splitlines()[0] if turn.get("user") else ""
            assistant_first = ""
            if turn.get("assistant"):
                assistant_first = turn["assistant"].splitlines()[0]
                if len(assistant_first) > 160:
                    assistant_first = assistant_first[:160] + "…"
            ts = _dt.datetime.fromtimestamp(turn.get("timestamp") or 0).strftime("%H:%M")
            lines.append(
                f"- **{ts}** _turn {turn.get('turn')}_ — {user}"
                f"\n    - {assistant_first}" if assistant_first else f"- **{ts}** _turn {turn.get('turn')}_ — {user}"
            )
        if ordered_links:
            lines.append("")
            lines.append("## Sources cited")
            for link in ordered_links:
                lines.append(f"- {link}")

        body = "\n".join(lines)
        frontmatter: dict[str, Any] = {
            "type": "hermes-session-summary",
            "session_id": self._session_id,
            "ended": when.strftime("%Y-%m-%dT%H:%M:%S"),
            "agent_id": self._agent_id,
            "platform": self._platform,
            "turn_count": turn_count,
            "tags": ["hermes", "summary"],
        }
        if related_paths:
            frontmatter["related"] = related_paths

        self._vault.write_note(rel, body, frontmatter=frontmatter, overwrite=True)

    def _write_vault_compression_snapshot(self, summary: str, message_count: int) -> None:
        if not self._vault:
            return
        when = _dt.datetime.now()
        base = self._vault.config.sessions_folder.strip("/")
        rel = (
            f"{base}/Compressions/"
            f"{when.strftime('%Y-%m-%d')}/{slugify(self._session_id or 'session')}"
            f"-{when.strftime('%H%M%S')}.md"
        )
        self._vault.write_note(
            rel,
            summary,
            frontmatter={
                "type": "hermes-compression-snapshot",
                "session_id": self._session_id,
                "captured": when.strftime("%Y-%m-%dT%H:%M:%S"),
                "message_count": message_count,
                "tags": ["hermes", "compression"],
            },
            overwrite=True,
        )

    # -- Config schema -------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "Cortex API key (leave blank for vault-only mode)",
                "secret": True,
                "required": False,
                "env_var": "CORTEX_API_KEY",
                "url": "https://app.hangarx.ai",
            },
            {
                "key": "base_url",
                "description": "Cortex API base URL",
                "default": DEFAULT_BASE_URL,
                "required": False,
            },
            {
                "key": "workspace_id",
                "description": "Cortex workspace ID (optional for single-workspace orgs)",
            },
            {
                "key": "organization_id",
                "description": "Cortex organization ID (optional)",
            },
            {
                "key": "auth_mode",
                "description": "Cortex auth header style",
                "default": "bearer",
                "choices": ["bearer", "x-api-key"],
            },
            {
                "key": "agent_id",
                "description": "Identity to use inside Cortex memU memory",
                "default": DEFAULT_AGENT_ID,
            },
            {
                "key": "vault_path",
                "description": (
                    "Absolute path to an Obsidian vault to mirror conversations into. "
                    "Leave blank to disable vault integration."
                ),
            },
            {
                "key": "vault_sessions_folder",
                "description": "Folder inside the vault for Hermes-written notes",
                "default": "Hermes Sessions",
            },
            {
                "key": "vault_sync_mode",
                "description": "When/how Hermes writes turns to the vault",
                "default": "per-session",
                "choices": ["off", "per-session", "daily", "per-turn"],
            },
            {
                "key": "vault_link_style",
                "description": "Link format Hermes uses when referencing vault notes",
                "default": "wikilink",
                "choices": ["wikilink", "markdown"],
            },
            {
                "key": "vault_search_enabled",
                "description": "Run local vault search during prefetch",
                "default": True,
            },
            {
                "key": "vault_auto_ingest",
                "description": "Auto-ingest vault notes into Cortex on change (future)",
                "default": False,
            },
            {
                "key": "vault_compression_snapshots",
                "description": "Write a markdown snapshot to the vault before context compression",
                "default": False,
            },
            {
                "key": "tool_mode",
                "description": (
                    "How many tools to expose to the model. 'full' (default) "
                    "exposes all Cortex + vault tools; 'compact' exposes only "
                    "cortex_ask, cortex_recall, cortex_remember, and vault_search."
                ),
                "default": "full",
                "choices": ["full", "compact"],
            },
            {
                "key": "prefetch_cadence",
                "description": (
                    "Fire Cortex /v1/ask/chat every N turns. 1 = every turn "
                    "(default), 0 disables. Local vault search always runs."
                ),
                "default": 1,
            },
            {
                "key": "profile_cadence",
                "description": (
                    "Inject a stable user-profile recall on turn 1 and every "
                    "N turns thereafter. 0 disables."
                ),
                "default": 25,
            },
            {
                "key": "auto_promote_enabled",
                "description": (
                    "On session end, ask Cortex to auto-promote verbatim "
                    "conversation turns into structured facts."
                ),
                "default": True,
            },
            {
                "key": "auto_promote_limit",
                "description": "Max items per auto-promote pass.",
                "default": 25,
            },
            {
                "key": "stream_prefetch",
                "description": (
                    "Stream /v1/ask/chat/stream during prefetch instead of "
                    "waiting for the full response. Falls back automatically "
                    "if the server doesn't support streaming."
                ),
                "default": True,
            },
            {
                "key": "dialectic_passes",
                "description": (
                    "Number of refinement passes per prefetch (1–3). 1 = "
                    "single Cortex query; 2 = second pass refines intent."
                ),
                "default": 1,
            },
            {
                "key": "session_summary_enabled",
                "description": (
                    "On session end, write a digest note under "
                    "Hermes Sessions/Summaries/ with backlinks to every cited source."
                ),
                "default": True,
            },
            {
                "key": "citation_wikilinks",
                "description": (
                    "Append a Citations section to prefetch context, with "
                    "wikilinks for sources that resolve to vault notes."
                ),
                "default": True,
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / CONFIG_FILE_NAME
        # Strip secrets — Hermes writes those to .env via env_var.
        sanitized = {k: v for k, v in values.items() if k != "api_key"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_ask_context(response: Any) -> str:
    """Pull the human-readable context block out of a /v1/ask/chat response."""
    if not isinstance(response, dict):
        return ""
    payload = response.get("result") if isinstance(response.get("result"), dict) else response
    if not isinstance(payload, dict):
        return ""
    context = payload.get("context")
    if isinstance(context, str) and context.strip():
        return context.strip()
    summary = payload.get("contextSummary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return ""


def _extract_citations(response: Any) -> list[dict[str, Any]]:
    """Pull a normalized citation list out of an ask_context response.

    Cortex citations come back in a few shapes depending on whether
    streaming or non-streaming endpoint was used:

      * streaming → {"result": {"citations": [{"source": "...", "text": "..."}]}}
      * enhanced  → {"result": {"raw": {"knowledgeGraph": {"entities": [...]}}, ...}}
                    with citations sometimes living on individual entities.

    We accept any of those and return ``[{source, text, memory_id?}]``.
    """
    if not isinstance(response, dict):
        return []
    payload = response.get("result") if isinstance(response.get("result"), dict) else response
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    cits = payload.get("citations") or payload.get("sources")
    if isinstance(cits, list):
        for cit in cits:
            if isinstance(cit, dict):
                out.append(cit)
            elif isinstance(cit, str):
                out.append({"source": cit})
    # Also harvest entity provenance if surfaced in `raw`.
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else None
    if raw:
        kg = raw.get("knowledgeGraph") if isinstance(raw.get("knowledgeGraph"), dict) else None
        if kg:
            for entity in kg.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                provenance = entity.get("provenance") or entity.get("sources")
                if isinstance(provenance, list):
                    for prov in provenance:
                        if isinstance(prov, dict):
                            out.append({
                                "source": prov.get("source") or prov.get("vault_path") or prov.get("title"),
                                "text": prov.get("text"),
                                "memory_id": prov.get("id") or prov.get("memoryId"),
                            })
    # Deduplicate by (source, memory_id) — preserve order.
    seen = set()
    deduped: list[dict[str, Any]] = []
    for cit in out:
        if not cit:
            continue
        key = (cit.get("source") or "", cit.get("memory_id") or "")
        if not any(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cit)
    return deduped


def _join_context_blocks(*labelled_blocks: tuple) -> str:
    """Combine multiple labelled context blocks into a single string.

    Each argument is a ``(label, text)`` tuple. Empty texts are skipped.
    """
    parts = []
    for label, text in labelled_blocks:
        if not text or not str(text).strip():
            continue
        parts.append(f"### {label}\n{str(text).strip()}")
    return "\n\n".join(parts)


def _summarize_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten Hermes messages into a plain-text transcript for storage."""
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            content = "\n".join(parts)
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content.strip()}")
    return "\n\n".join(lines).strip()


def _make_jsonable(value: Any) -> Any:
    """Best-effort coerce SDK objects into JSON-serialisable structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _make_jsonable(v) for k, v in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Called by Hermes' memory plugin discovery (plugins/memory/__init__.py)."""
    ctx.register_memory_provider(HangarxMemoryProvider())
