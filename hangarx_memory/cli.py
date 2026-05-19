"""CLI commands for the hangarx-memory Hermes plugin.

Activated automatically when ``memory.provider`` in config.yaml equals
``hangarx-memory``. Hermes' plugin discovery imports this module and
calls ``register_cli(subparser)`` during argparse setup.

Subcommands:
    hermes hangarx-memory status
    hermes hangarx-memory test
    hermes hangarx-memory tools
    hermes hangarx-memory reflect
    hermes hangarx-memory vault <status|list|search|open>
    hermes hangarx-memory schedule
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

CONFIG_FILE_NAME = "hangarx-memory.json"


def _load_provider():
    """Construct and initialize a HangarxMemoryProvider with the active config.

    Works whether the package is loaded through Hermes' plugin discovery
    (which mounts us under ``_hermes_user_memory.hangarx-memory``) or
    invoked directly as ``python -m hangarx_memory.cli`` from a checkout.
    """
    try:
        from .provider import HangarxMemoryProvider  # type: ignore
    except ImportError:
        # Hermes' plugin loader sometimes imports __init__.py as a flat
        # module under ``_hermes_user_memory.<name>`` without setting up
        # the submodule namespace. Fall back to file-based discovery so
        # we still find ``provider.py`` next to this file.
        import importlib.util

        here = Path(__file__).resolve().parent
        provider_file = here / "provider.py"
        if not provider_file.is_file():
            raise RuntimeError(f"hangarx_memory.provider missing at {provider_file}") from None
        # Pre-register client.py + vault.py so provider.py's relative
        # imports resolve when we exec it.
        pkg_name = "hangarx_memory_runtime"
        for sub in ("client", "vault"):
            sub_file = here / f"{sub}.py"
            if not sub_file.is_file():
                continue
            sub_spec = importlib.util.spec_from_file_location(
                f"{pkg_name}.{sub}", str(sub_file)
            )
            if sub_spec and sub_spec.loader:
                sub_mod = importlib.util.module_from_spec(sub_spec)
                sys.modules[f"{pkg_name}.{sub}"] = sub_mod
                sub_spec.loader.exec_module(sub_mod)
        spec = importlib.util.spec_from_file_location(
            pkg_name, str(provider_file)
        )
        if not spec or not spec.loader:
            raise RuntimeError("could not locate hangarx-memory provider module") from None
        module = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = module
        spec.loader.exec_module(module)
        HangarxMemoryProvider = module.HangarxMemoryProvider  # type: ignore[attr-defined]

    provider = HangarxMemoryProvider()
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    provider.initialize(
        session_id="cli",
        hermes_home=hermes_home,
        platform="cli",
        agent_context="cli",
    )
    return provider


def _config_path() -> Path:
    """Return the hangarx-memory config file path."""
    home = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    return home / CONFIG_FILE_NAME


def _load_config_file() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cmd_status(_args: argparse.Namespace) -> int:
    cfg = _load_config_file()
    has_key = bool(os.environ.get("CORTEX_API_KEY") or cfg.get("api_key"))
    explicit_url = bool(os.environ.get("CORTEX_API_URL") or cfg.get("base_url"))
    base_url = cfg.get("base_url") or os.environ.get("CORTEX_API_URL") or "https://cortex.hangarx.ai"
    workspace = cfg.get("workspace_id") or "(not set)"
    organization = cfg.get("organization_id") or "(not set)"
    auth_mode = cfg.get("auth_mode") or "bearer"
    agent_id = cfg.get("agent_id") or "hermes"
    vault_path = (
        os.environ.get("HANGARX_VAULT_PATH")
        or cfg.get("vault_path")
        or "(not set)"
    )

    # Local auto-detect probe — reuses the same logic as the provider.
    auto_detect = bool(cfg.get("auto_detect_local", True))
    detected_url: str | None = None
    if auto_detect and not explicit_url:
        from .client import probe_health  # local import to keep CLI light

        candidates_raw = (
            os.environ.get("CORTEX_LOCAL_URLS")
            or cfg.get("local_candidates")
            or "http://localhost:3400,http://localhost:4000"
        )
        if isinstance(candidates_raw, str):
            candidates = [u.strip() for u in candidates_raw.split(",") if u.strip()]
        elif isinstance(candidates_raw, list):
            candidates = [str(u).strip() for u in candidates_raw if u]
        else:
            candidates = []
        for url in candidates:
            data = probe_health(url, timeout=0.3)
            if data and (data.get("ready") is True or data.get("status") == "healthy"):
                detected_url = url
                break

    effective_url = detected_url or base_url
    mode = (
        "local-keyless" if detected_url and not has_key
        else "local-with-key" if detected_url and has_key
        else "remote" if has_key
        else "vault-only" if vault_path != "(not set)"
        else "inactive"
    )

    cfg_path = _config_path()
    print("hangarx-memory status")
    print(f"  config file       : {cfg_path}{'' if cfg_path.is_file() else '  (missing)'}")
    print(f"  mode              : {mode}")
    print(f"  CORTEX_API_KEY    : {'set' if has_key else 'NOT SET'}")
    print(f"  base_url (config) : {base_url}")
    if detected_url and detected_url != base_url:
        print(f"  base_url (active) : {detected_url}  (auto-detected)")
    elif detected_url:
        print(f"  local detect      : healthy at {detected_url}")
    elif auto_detect and not explicit_url:
        print("  local detect      : no local Cortex on default ports")
    print(f"  effective base    : {effective_url}")
    print(f"  workspace_id      : {workspace}")
    print(f"  organization_id   : {organization}")
    print(f"  auth_mode         : {auth_mode}")
    print(f"  agent_id          : {agent_id}")
    print(f"  vault_path        : {vault_path}")
    return 0


def _cmd_test(_args: argparse.Namespace) -> int:
    if not os.environ.get("CORTEX_API_KEY"):
        print("CORTEX_API_KEY is not set. Run: hermes memory setup", file=sys.stderr)
        return 2
    provider = _load_provider()
    if not provider._client:  # type: ignore[attr-defined]
        print("hangarx-memory failed to initialize (no API key or bad config).", file=sys.stderr)
        return 2
    try:
        tools = provider._client.mcp_list_tools()  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"Cortex MCP tools/list failed: {exc}", file=sys.stderr)
        return 1
    count: int | None = None
    if isinstance(tools, dict) and isinstance(tools.get("tools"), list):
        count = len(tools["tools"])
    elif isinstance(tools, list):
        count = len(tools)
    if count is None:
        print("Cortex responded but the MCP tools/list payload had no `tools` list.")
        print(json.dumps(tools, indent=2)[:500])
        return 1
    print(f"Cortex MCP reachable: {count} tools available.")
    return 0


def _cmd_tools(_args: argparse.Namespace) -> int:
    provider = _load_provider()
    schemas = provider.get_tool_schemas()
    print(f"hangarx-memory exposes {len(schemas)} tools to Hermes:")
    for schema in schemas:
        print(f"  - {schema['name']}: {schema['description']}")
    return 0


def _cmd_reflect(_args: argparse.Namespace) -> int:
    if not os.environ.get("CORTEX_API_KEY"):
        print("CORTEX_API_KEY is not set. Run: hermes memory setup", file=sys.stderr)
        return 2
    provider = _load_provider()
    if not provider._client:  # type: ignore[attr-defined]
        print("hangarx-memory failed to initialize.", file=sys.stderr)
        return 2
    try:
        result = provider._client.reflect(agent_id=provider._agent_id)  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"Cortex reflect failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2)[:1000])
    return 0


def _cmd_vault(args: argparse.Namespace) -> int:
    provider = _load_provider()
    vault = provider._vault  # type: ignore[attr-defined]
    if not vault:
        cfg = _load_config_file()
        path = (
            cfg.get("vault_path")
            or os.environ.get("HANGARX_VAULT_PATH")
        )
        if not path:
            print("Vault is not configured. Set vault_path in hangarx-memory.json.", file=sys.stderr)
        else:
            print(f"Vault path is configured ({path}) but not a directory.", file=sys.stderr)
        return 2

    sub = getattr(args, "vault_subcommand", None)
    if sub == "status":
        print(f"vault root            : {vault.root}")
        print(f"sessions folder       : {vault.config.sessions_folder}")
        print(f"sync mode             : {vault.config.sync_mode}")
        print(f"link style            : {vault.config.link_style}")
        print(f"search enabled        : {vault.config.search_enabled}")
        print(f"auto-ingest           : {vault.config.auto_ingest}")
        try:
            note_count = sum(1 for _ in vault.root.rglob("*.md"))
            print(f"markdown notes        : {note_count}")
        except Exception:
            pass
        return 0
    if sub == "list":
        notes = vault.list_notes(
            folder=getattr(args, "folder", None),
            tag=getattr(args, "tag", None),
            limit=int(getattr(args, "limit", 20) or 20),
        )
        for note in notes:
            print(f"  {note['path']}")
        if not notes:
            print("  (no notes)")
        return 0
    if sub == "search":
        results = vault.search(
            args.query,
            limit=int(getattr(args, "limit", 5) or 5),
            folder=getattr(args, "folder", None),
        )
        for result in results:
            print(f"- {result['path']}  (score={result['score']:.1f})")
            snippet = (result.get("snippet") or "").strip().replace("\n", " ")
            if snippet:
                print(f"    {snippet}")
        if not results:
            print("(no matches)")
        return 0
    if sub == "open":
        path = getattr(args, "path", None) or vault.config.sessions_folder
        try:
            target = vault._safe_path(path)  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"invalid path: {exc}", file=sys.stderr)
            return 1
        # macOS-only convenience — `open` handles obsidian:// URLs too.
        import subprocess as _sp
        try:
            _sp.run(["open", str(target)], check=False)
            print(f"opened {target}")
            return 0
        except Exception as exc:
            print(f"open failed: {exc}", file=sys.stderr)
            return 1
    print("Usage: hermes hangarx-memory vault <status|list|search|open>")
    return 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    """Print the cron-install snippet for the background reflection script.

    The cronjob tool is only available inside Hermes runtime, so we
    print the exact command for the user to paste into a Hermes
    session (or invoke from a script). Idempotent — running this twice
    just shows the snippet again; the user controls registration.
    """
    home = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    # The reflect.py script lives inside this package — find it relative
    # to __file__ so symlinked installs still work.
    script = Path(__file__).resolve().parent / "scripts" / "reflect.py"
    # Also accept a direct-install location for users who copied the
    # plugin folder under $HERMES_HOME/plugins/hangarx-memory/.
    if not script.is_file():
        alt = home / "plugins" / "hangarx-memory" / "hangarx_memory" / "scripts" / "reflect.py"
        if alt.is_file():
            script = alt
    schedule = getattr(args, "schedule", None) or "0 4 * * *"
    if not script.is_file():
        print(f"reflect script missing at {script}", file=sys.stderr)
        return 1
    print("To schedule nightly Cortex reflection, paste this into a Hermes chat:")
    print()
    print("  /cron create name='hangarx-memory nightly reflection' \\")
    print(f"    schedule='{schedule}' no_agent=True \\")
    print(f"    script='{script}'")
    print()
    print("Or call the cronjob tool programmatically:")
    print()
    print("  cronjob(action='create', name='hangarx-memory nightly reflection',")
    print(f"          schedule='{schedule}', no_agent=True,")
    print(f"          script='{script}')")
    print()
    print("The script stays silent on no-op and prints a single line when it")
    print("actually promoted memories, matching Hermes' no_agent contract.")
    return 0


def hangarx_memory_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "hangarx_memory_command", None)
    if sub == "status":
        return _cmd_status(args)
    if sub == "test":
        return _cmd_test(args)
    if sub == "tools":
        return _cmd_tools(args)
    if sub == "reflect":
        return _cmd_reflect(args)
    if sub == "vault":
        return _cmd_vault(args)
    if sub == "schedule":
        return _cmd_schedule(args)
    print("Usage: hermes hangarx-memory <status|test|tools|reflect|vault|schedule>")
    return 1


# Hermes' plugin discovery looks for ``<plugin-name>_command`` (with hyphens
# preserved or replaced by underscores depending on version). Expose both
# spellings so we hook in cleanly on every supported Hermes release.
hangarx_memory_command_alias = hangarx_memory_command
globals()["hangarx-memory_command"] = hangarx_memory_command


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes hangarx-memory`` argparse tree."""
    subs = subparser.add_subparsers(dest="hangarx_memory_command")
    subs.add_parser("status", help="Show provider config and credentials")
    subs.add_parser("test", help="Hit Cortex MCP tools/list to confirm connectivity")
    subs.add_parser("tools", help="List tools hangarx-memory exposes to Hermes")
    subs.add_parser("reflect", help="Trigger Cortex memU reflection / consolidation")

    vault = subs.add_parser("vault", help="Inspect the Obsidian vault integration")
    vault_subs = vault.add_subparsers(dest="vault_subcommand")
    vault_subs.add_parser("status", help="Show vault config + note count")
    vlist = vault_subs.add_parser("list", help="List notes in the vault")
    vlist.add_argument("--folder")
    vlist.add_argument("--tag")
    vlist.add_argument("--limit", type=int, default=20)
    vsearch = vault_subs.add_parser("search", help="Search the vault locally")
    vsearch.add_argument("query")
    vsearch.add_argument("--folder")
    vsearch.add_argument("--limit", type=int, default=5)
    vopen = vault_subs.add_parser("open", help="Open the vault (or a folder) in Finder")
    vopen.add_argument("path", nargs="?", default=None)

    sched = subs.add_parser(
        "schedule",
        help="Print the cron-install snippet for nightly Cortex reflection",
    )
    sched.add_argument(
        "--schedule",
        default="0 4 * * *",
        help="Cron expression (default: 0 4 * * *, i.e. 04:00 daily)",
    )

    subparser.set_defaults(func=hangarx_memory_command)
