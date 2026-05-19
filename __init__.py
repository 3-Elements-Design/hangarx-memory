"""Plugin entry point — Hermes discovers this file via ``plugins/memory/__init__.py``.

The actual implementation lives in the inner ``hangarx_memory/`` Python
package. We re-export the public surface here so Hermes' discovery
(which expects ``register(ctx)`` and a ``MemoryProvider`` subclass at
the directory root) finds them without having to traverse into a
nested package.
"""

from __future__ import annotations

from .hangarx_memory import HangarxMemoryProvider, __version__, register

__all__ = [
    "HangarxMemoryProvider",
    "register",
    "__version__",
]
