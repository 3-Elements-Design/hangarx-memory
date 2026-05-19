"""Plugin entry point — Hermes discovers this file via ``plugins/memory/__init__.py``.

The actual implementation lives in the inner ``hangarx_memory/`` Python
package. We re-export the public surface here so Hermes' discovery
(which expects ``register(ctx)`` and a ``MemoryProvider`` subclass at
the directory root) finds them without having to traverse into a
nested package.

Import handling: when Hermes loads this as part of a plugin package the
relative import works. When it's loaded standalone (the case after a
``pip install`` or when pytest probes the repo root for ``__init__.py``)
we fall back to an absolute import so we don't crash with
``ImportError: attempted relative import with no known parent package``.
"""

from __future__ import annotations

try:
    from .hangarx_memory import HangarxMemoryProvider, __version__, register
except ImportError:
    # Loaded standalone — fall back to absolute import. Requires the
    # ``hangarx_memory`` package to be on sys.path, which is always the
    # case after ``pip install`` and the typical case under Hermes'
    # plugin discovery (which adds the plugin dir to sys.path).
    from hangarx_memory import (  # type: ignore[no-redef]
        HangarxMemoryProvider,
        __version__,
        register,
    )

__all__ = [
    "HangarxMemoryProvider",
    "register",
    "__version__",
]

