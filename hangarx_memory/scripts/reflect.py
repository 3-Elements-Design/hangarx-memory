#!/usr/bin/env python3
"""Background reflection script for the hangarx-memory plugin.

Designed to be run on a schedule via Hermes' cron (``hermes cron``).
Reads the active hangarx-memory config + credentials, then:

  1. Calls ``/v1/memory/auto-promote`` to distill recent verbatim turns
     into structured facts (capped by ``auto_promote_limit``).
  2. Calls ``/v1/memory/reflect`` to consolidate, dedup, and refresh
     trust scores.
  3. Prints a single-line status summary on stdout — designed for the
     ``no_agent`` cron mode where empty stdout means "silent OK" and
     any output is delivered verbatim to the user.

Exit codes:
  0  success (or no-op when config is missing)
  1  Cortex returned an error

Suggested schedule (run from inside Hermes):

    cronjob action='create' name='hangarx-memory nightly reflection' \\
        schedule='0 4 * * *' no_agent=True \\
        script='~/.hermes/plugins/hangarx-memory/hangarx_memory/scripts/reflect.py'
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
# Resolve the package dir from THIS script's location — works regardless of
# how HERMES_HOME is set (e.g. profile sandboxes during tests).
PACKAGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE_NAME = "hangarx-memory.json"


def _resolve_config() -> Path:
    """Return the hangarx-memory config file path."""
    return HERMES_HOME / CONFIG_FILE_NAME


def _load_env() -> dict:
    """Best-effort .env loader (cron jobs don't auto-source it)."""
    env_path = HERMES_HOME / ".env"
    out = {}
    if not env_path.is_file():
        return out
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def main() -> int:
    config_file = _resolve_config()
    if not config_file.is_file():
        # No hangarx-memory config — silent no-op.
        return 0

    # Make `from client import CortexClient, CortexError` work without
    # touching sys.path globally.
    sys.path.insert(0, str(PACKAGE_DIR))
    try:
        from client import CortexClient, CortexError  # type: ignore
    except Exception as exc:
        print(f"hangarx-memory reflect: failed to import client ({exc})", file=sys.stderr)
        return 1

    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"hangarx-memory reflect: bad config ({exc})", file=sys.stderr)
        return 1

    env = _load_env()
    api_key = os.environ.get("CORTEX_API_KEY") or env.get("CORTEX_API_KEY") or cfg.get("api_key")
    if not api_key:
        # No key configured — silent no-op (vault-only mode).
        return 0

    client = CortexClient(
        base_url=cfg.get("base_url") or "https://cortex.hangarx.ai",
        api_key=api_key,
        workspace_id=cfg.get("workspace_id") or os.environ.get("CORTEX_WORKSPACE_ID") or env.get("CORTEX_WORKSPACE_ID") or "",
        organization_id=cfg.get("organization_id") or os.environ.get("CORTEX_ORGANIZATION_ID") or env.get("CORTEX_ORGANIZATION_ID") or "",
        auth_mode=(cfg.get("auth_mode") or "bearer").lower(),
        timeout=float(cfg.get("timeout") or 30.0),
    )
    agent_id = cfg.get("agent_id") or "hermes"
    limit = int(cfg.get("auto_promote_limit") or 25)

    promoted_count = None
    reflected = False

    try:
        promote_result = client.auto_promote(agent_id=agent_id, limit=limit)
        if isinstance(promote_result, dict):
            # Try common shapes
            if isinstance(promote_result.get("promoted"), list):
                promoted_count = len(promote_result["promoted"])
            elif isinstance(promote_result.get("data"), dict):
                inner = promote_result["data"]
                if isinstance(inner.get("promoted"), list):
                    promoted_count = len(inner["promoted"])
                elif isinstance(inner.get("count"), int):
                    promoted_count = inner["count"]
            elif isinstance(promote_result.get("count"), int):
                promoted_count = promote_result["count"]
    except CortexError as exc:
        print(f"hangarx-memory reflect: auto_promote failed ({exc})", file=sys.stderr)
        return 1

    try:
        client.reflect(agent_id=agent_id)
        reflected = True
    except CortexError as exc:
        print(f"hangarx-memory reflect: reflect failed ({exc})", file=sys.stderr)
        return 1

    # If nothing of note happened, stay silent (no_agent contract).
    if not promoted_count and reflected:
        return 0
    if promoted_count:
        print(
            f"hangarx-memory: promoted {promoted_count} memor"
            f"{'y' if promoted_count == 1 else 'ies'}, reflection complete."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
