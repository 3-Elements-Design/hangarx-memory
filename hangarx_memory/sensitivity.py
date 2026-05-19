"""Sensitivity tagging + context-aware gate.

The agent loop can run in several contexts:

  * ``primary``    — the user is directly in the loop. Full access to all
                     memory.
  * ``subagent``   — a child agent delegated to via delegate_task. Has
                     its own context but shares the parent's workspace.
                     We don't want secrets bleeding from primary into
                     subagents that might log/echo them.
  * ``cron``       — a scheduled background job. No user present, no
                     interactive supervision. Treated as the most
                     restrictive context.

We tag every stored memory with one of three sensitivity levels:

  * ``public``  — default. Anyone in any context can read or write.
  * ``private`` — written from primary only; not surfaced into subagent
                   / cron prefetch context. Useful for personal notes
                   the user doesn't want secondary agents to see.
  * ``secret``  — written from primary only; never injected into any
                   prefetch context, including primary, except when the
                   model explicitly calls cortex_recall. Useful for
                   API keys, addresses, real names.

Tagging happens two ways:

  1. **Auto-detection** — content is scanned against a small set of
     regex patterns for things that almost always deserve at least
     ``private`` (emails, phone numbers) or ``secret`` (long opaque
     tokens that look like API keys, AWS access keys, JWTs).
  2. **Explicit tag** — callers can pass ``sensitivity="private"`` to
     ``on_memory_write`` or to the ``cortex_remember`` tool. Explicit
     tags always win over inference.

This is the answer to "I don't want my Anthropic API key in memU
because some subagent might leak it."
"""

from __future__ import annotations

import re
from typing import Any

# Sensitivity ordering — higher = more restrictive.
SENSITIVITY_PUBLIC = "public"
SENSITIVITY_PRIVATE = "private"
SENSITIVITY_SECRET = "secret"

_LEVELS = (SENSITIVITY_PUBLIC, SENSITIVITY_PRIVATE, SENSITIVITY_SECRET)
_LEVEL_RANK = {SENSITIVITY_PUBLIC: 0, SENSITIVITY_PRIVATE: 1, SENSITIVITY_SECRET: 2}


# Contexts that aren't the primary user-facing loop. These are the
# contexts where private/secret data should be filtered or refused.
NON_PRIMARY_CONTEXTS = frozenset({"subagent", "cron", "background"})


# Regex patterns that strongly suggest secret material. Tuned for
# precision over recall — we'd rather miss tagging a borderline case
# than spam every memory with "secret" tags.
_SECRET_PATTERNS = (
    # OpenAI / Anthropic / common SaaS API keys — long opaque tokens
    # preceded by a known prefix.
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),         # OpenAI / Anthropic
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),   # Anthropic specifically
    re.compile(r"\bxoxb-[A-Za-z0-9-]{10,}\b"),      # Slack bot token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),            # AWS access key
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),        # GitHub personal access token
    re.compile(r"\bgho_[A-Za-z0-9]{30,}\b"),        # GitHub OAuth token
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),    # GitLab personal access token
    re.compile(r"\bey[A-Za-z0-9_=-]{20,}\."          # JWT (header.payload.signature)
               r"ey[A-Za-z0-9_=-]{20,}\."
               r"[A-Za-z0-9_=-]{20,}\b"),
    # HangarX/Cortex API key prefix
    re.compile(r"\bctx_[A-Za-z0-9_-]{20,}\b"),
)


# Patterns suggesting personal data — at least ``private``.
_PRIVATE_PATTERNS = (
    # Email addresses
    re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    # US-style phone numbers (loose — false positives on dates are OK
    # because dates aren't useful to memorize anyway).
    re.compile(r"\b\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
    # SSN-shaped (3-2-4)
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
)


def normalize(level: Any) -> str:
    """Return the canonical lowercase level, or 'public' for anything unknown."""
    if not isinstance(level, str):
        return SENSITIVITY_PUBLIC
    lowered = level.strip().lower()
    return lowered if lowered in _LEVELS else SENSITIVITY_PUBLIC


def infer(content: str) -> str:
    """Best-effort sensitivity inference from content.

    Returns the highest-sensitivity level whose patterns match anywhere
    in ``content``. Defaults to ``public`` if nothing matches.

    Designed to be fast: short-circuits as soon as a SECRET pattern
    matches, since that's the most restrictive level.
    """
    if not content:
        return SENSITIVITY_PUBLIC
    text = content if isinstance(content, str) else str(content)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return SENSITIVITY_SECRET
    for pattern in _PRIVATE_PATTERNS:
        if pattern.search(text):
            return SENSITIVITY_PRIVATE
    return SENSITIVITY_PUBLIC


def merge(*levels: str) -> str:
    """Return the most restrictive level among the inputs.

    Used when an explicit tag is combined with an inferred tag — the
    higher level wins so accidentally calling ``sensitivity='public'``
    on a string containing an API key still gets marked SECRET.
    """
    rank = 0
    chosen = SENSITIVITY_PUBLIC
    for level in levels:
        norm = normalize(level)
        if _LEVEL_RANK[norm] > rank:
            rank = _LEVEL_RANK[norm]
            chosen = norm
    return chosen


def is_non_primary(agent_context: str | None) -> bool:
    """Return True if ``agent_context`` is one of the restricted contexts."""
    if not agent_context:
        return False
    return agent_context.strip().lower() in NON_PRIMARY_CONTEXTS


def allow_write(level: str, agent_context: str | None) -> bool:
    """Decide whether a write with the given sensitivity is permitted.

    Rules:
      * ``public`` → always allowed.
      * ``private`` / ``secret`` → only allowed from ``primary``
        contexts. Subagents and cron jobs writing private/secret data
        is almost always wrong (they could be echoing something they
        shouldn't have access to in the first place).
    """
    if normalize(level) == SENSITIVITY_PUBLIC:
        return True
    return not is_non_primary(agent_context)


def allow_inject(level: str, agent_context: str | None) -> bool:
    """Decide whether a memory at the given sensitivity should be injected
    into prefetch context for the current agent_context.

    Rules:
      * ``public`` → always injected.
      * ``private`` → injected for primary only; subagents/cron see
        nothing private.
      * ``secret`` → never injected anywhere via prefetch. The model can
        still explicitly call ``cortex_recall`` to retrieve them in the
        primary context, but they don't leak into the context block.
    """
    norm = normalize(level)
    if norm == SENSITIVITY_PUBLIC:
        return True
    if norm == SENSITIVITY_SECRET:
        return False
    # private — primary only
    return not is_non_primary(agent_context)


def extract(metadata: Any) -> str:
    """Pull a sensitivity tag out of a metadata dict, default 'public'."""
    if isinstance(metadata, dict):
        return normalize(metadata.get("sensitivity"))
    return SENSITIVITY_PUBLIC
