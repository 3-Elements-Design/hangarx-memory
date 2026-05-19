"""CLI shim for Hermes' plugin discovery.

Hermes loads ``<plugin>/cli.py`` directly via ``discover_plugin_cli_commands``
in ``plugins/memory/__init__.py``. Our real CLI lives inside the package
at ``hangarx_memory/cli.py``. Re-export everything Hermes needs.

Same dual-import pattern as ``__init__.py``: try relative, fall back to
absolute so the file works both as a Hermes plugin shim and as a
standalone module (e.g. when pytest probes the repo root).
"""

from __future__ import annotations

try:
    from .hangarx_memory.cli import (  # type: ignore[import]
        hangarx_memory_command,
        register_cli,
    )
except ImportError:
    from hangarx_memory.cli import (  # type: ignore[import,no-redef]
        hangarx_memory_command,
        register_cli,
    )

# Hermes' discover_plugin_cli_commands() looks for a function named
# ``<plugin_name>_command`` — with hyphens replaced by underscores in
# some versions and preserved in others. Bind both spellings so we work
# across Hermes releases.
hangarx_memory_command_alias = hangarx_memory_command
globals()["hangarx-memory_command"] = hangarx_memory_command

__all__ = [
    "register_cli",
    "hangarx_memory_command",
]
