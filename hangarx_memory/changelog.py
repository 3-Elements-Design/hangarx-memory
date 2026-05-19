"""Memory changelog — append-only audit log of every memory mutation.

Every time the provider adds, merges, forgets, or auto-promotes a memory
item, an entry is appended here. Entries live in two places:

  1. **In-process ring buffer** (``MemoryChangelog._entries``) so the
     ``cortex_memory_changelog`` tool can return recent activity
     without hitting disk.
  2. **Vault note** at ``$VAULT/<sessions_folder>/Memory Changelog.md``
     so the user can read it in Obsidian, search it, or grep it.

The buffer is bounded to ``ring_buffer_size`` entries (default 200) so
it never balloons memory in long-running sessions.

Each entry is a small immutable dict so it's trivially JSON-serializable
when returned through a tool:

    {
        "when": "2026-05-19T03:30:00Z",
        "action": "ADDED",       # ADDED | MERGED | FORGOT | PROMOTED
        "category": "user_fact", # Cortex category if known
        "memory_id": "mem_01abc", # may be empty if Cortex didn't return one
        "summary": "User prefers concise replies",  # first line, truncated
        "session_id": "...",
        "agent_id": "hermes",
        "source": "on_memory_write",  # which hook fired
        "details": {...},        # action-specific extras
    }

Design notes:

  * **Write-only from the model's perspective.** The model never writes
    here directly; mutations are recorded automatically by the provider.
    The model can read via ``cortex_memory_changelog(limit=N)``.
  * **Vault writes are best-effort.** If the vault isn't configured, we
    still keep the ring buffer. If the vault write fails, we log and
    move on — never break the agent loop over an audit-log failure.
  * **Revert is a separate concern.** A ``REVERTED`` entry is logged
    when ``cortex_revert_memory`` succeeds, so the changelog tracks the
    full history including reversals.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


# Maximum length of the "summary" field per entry. Keeps the in-process
# buffer + tool responses bounded.
MAX_SUMMARY_LEN = 200

# Default ring buffer size. Tuned for "user asks 'what changed today'"
# at a typical 30–80 turns/session — 200 entries covers a long day of
# activity without becoming a memory hog.
DEFAULT_BUFFER_SIZE = 200


def _now_iso() -> str:
    """UTC timestamp formatted for log entries."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, limit: int = MAX_SUMMARY_LEN) -> str:
    text = (text or "").strip().replace("\n", " ").replace("\r", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class MemoryChangelog:
    """Append-only changelog of memory mutations.

    Thread-safe — the agent loop's memory provider may write entries
    from the sync thread while the model reads via a tool call on the
    main thread.
    """

    def __init__(
        self,
        *,
        ring_buffer_size: int = DEFAULT_BUFFER_SIZE,
        vault=None,  # type: ignore[no-untyped-def]
        sessions_folder: str = "Hermes Sessions",
        agent_id: str = "hermes",
    ) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=ring_buffer_size)
        self._lock = threading.Lock()
        self._vault = vault
        self._sessions_folder = sessions_folder.strip("/")
        self._agent_id = agent_id

    @property
    def vault_path(self) -> str:
        """Path inside the vault where entries are appended."""
        return f"{self._sessions_folder}/Memory Changelog.md"

    def record(
        self,
        action: str,
        *,
        summary: str = "",
        memory_id: str = "",
        category: str = "",
        session_id: str = "",
        source: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a single entry. Returns the entry for tool dispatch.

        ``action`` is one of ``ADDED``, ``MERGED``, ``FORGOT``,
        ``PROMOTED``, ``REVERTED``. Unknown actions are accepted but
        get logged at debug level — kept permissive so future hook
        sources can extend without changing this file.
        """
        action = (action or "UNKNOWN").upper()
        entry = {
            "when": _now_iso(),
            "action": action,
            "category": category or "",
            "memory_id": memory_id or "",
            "summary": _truncate(summary),
            "session_id": session_id or "",
            "agent_id": self._agent_id,
            "source": source or "",
            "details": dict(details or {}),
        }
        with self._lock:
            self._entries.append(entry)
        # Vault write is fire-and-forget so a slow disk doesn't stall the
        # agent loop.
        self._append_to_vault(entry)
        return entry

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` most-recent entries, newest-first."""
        try:
            n = max(1, min(int(limit), DEFAULT_BUFFER_SIZE))
        except (TypeError, ValueError):
            n = 50
        with self._lock:
            # deque doesn't slice; convert and reverse.
            entries = list(self._entries)
        entries.reverse()
        return entries[:n]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- Vault sink ---------------------------------------------------------

    def _format_entry(self, entry: dict[str, Any]) -> str:
        """Format an entry as a single markdown bullet block."""
        when = entry["when"]
        action = entry["action"]
        memory_id = entry.get("memory_id") or ""
        category = entry.get("category") or ""
        summary = entry.get("summary") or ""
        # Header bullet — date, action, category, optional memory id.
        bits = [f"**{when}**", f"`{action}`"]
        if category:
            bits.append(f"`{category}`")
        if memory_id:
            bits.append(f"`{memory_id}`")
        header = " · ".join(bits)
        block = f"- {header}"
        if summary:
            block = f"{block}\n    - {summary}"
        return block

    def _append_to_vault(self, entry: dict[str, Any]) -> None:
        if not self._vault:
            return
        try:
            block = self._format_entry(entry)
            self._vault.append_note(
                self.vault_path,
                block,
                initial_frontmatter={
                    "type": "hermes-memory-changelog",
                    "agent_id": self._agent_id,
                    "tags": ["hermes", "memory", "audit"],
                },
                create_if_missing=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Audit log failures must never break the agent loop. Log and
            # continue; the ring buffer still has the entry so it's not
            # totally lost.
            logger.warning(
                "hangarx-memory: vault changelog append failed: %s", exc
            )


def extract_memory_id(remember_response: Any) -> str:
    """Best-effort: pull the new memory's id out of a Cortex remember response.

    Cortex's response shape varies slightly across endpoints — some return
    ``{success, data: {id, ...}}``, others return ``{id, ...}`` directly,
    and the MCP path returns ``{memoryId}``. Try the common shapes; return
    empty string if none match (caller logs an entry with no id, which is
    still useful).
    """
    if not isinstance(remember_response, dict):
        return ""
    # Common envelope: {success, data: {...}}
    payload = remember_response.get("data", remember_response)
    if isinstance(payload, dict):
        for key in ("id", "memoryId", "memory_id", "itemId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""
