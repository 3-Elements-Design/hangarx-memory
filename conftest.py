"""Top-level conftest — ensures Hermes is importable for ABC tests.

When Hermes Agent isn't pip-installed but is checked out at
``~/.hermes/hermes-agent`` (the default location for ``hermes setup``
clones), add it to sys.path so the ``agent.memory_provider`` import in
tests/test_provider_lifecycle.py resolves. Without this, the ABC test
is silently skipped on every dev machine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
_HERMES_AGENT = _HERMES_HOME / "hermes-agent"
if _HERMES_AGENT.is_dir():
    sys.path.insert(0, str(_HERMES_AGENT))
