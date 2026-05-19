"""hangarx-memory — Hermes memory provider for HangarX Cortex.

This is the public package entry point. Hermes' plugin discovery
imports the directory and calls ``register(ctx)`` to wire the provider
into the agent's MemoryManager.

Most users won't import from this module directly; the plugin is
activated via ``hermes config set memory.provider hangarx-memory``.
"""

from __future__ import annotations

__version__ = "0.6.0"

from .provider import HangarxMemoryProvider, register

__all__ = [
    "HangarxMemoryProvider",
    "register",
    "__version__",
]
