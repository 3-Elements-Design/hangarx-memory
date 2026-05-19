# Patches

External patches against upstream projects that `hangarx-memory` integrates
with. Each patch is self-contained and includes the base SHA it was generated
against so it can be re-applied across upstream version bumps.

## Inventory

| Patch | Target repo | Base SHA | Status |
|---|---|---|---|
| `hermes-memory-manager-late-tools.patch` | `NousResearch/hermes-agent` | `457fa913b839a5ed6478dcfade2155cca773208c` | Pending upstream |

---

## `hermes-memory-manager-late-tools.patch`

### Why this exists

Hermes calls `MemoryManager.add_provider(...)` *before* `MemoryManager.initialize_all(...)`
in `agent/agent_init.py`. `MemoryManager.add_provider()` builds its
`tool_name → provider` dispatch table from `provider.get_tool_schemas()` at the
moment of registration.

`hangarx-memory` (and any provider that constructs network clients or filesystem
mirrors during `initialize()`) returns `[]` from `get_tool_schemas()` before
`initialize()` runs, because the client and vault don't exist yet. The result:

- Manager registers the provider with **0 routable tools**.
- After `initialize()`, the provider exposes 23 tools.
- Hermes injects those schemas into `agent.tools` (the model sees them).
- The manager's dispatch table still doesn't know them.
- Every tool call returns `{"error": "Unknown tool: cortex_recall"}`.

This affects every late-binding memory provider, not just `hangarx-memory`.

### What the patch does

Two changes in `agent/memory_manager.py`:

1. **Extract `_reindex_tools(provider)`** — idempotent helper that walks
   `provider.get_tool_schemas()` and updates `_tool_to_provider`. Tool-name
   conflicts keep first-registered semantics and log a warning.
2. **Call `_reindex_tools` from `initialize_all()`** — after each provider's
   `initialize()` returns successfully, re-index its tools. Late-bound schemas
   land in the dispatch table.

Plus a deterministic regression test at
`tests/agent/test_memory_manager_late_tools.py` (6 cases, no network, no plugin
imports — uses a fake provider that mirrors the late-bound lifecycle).

### Applying

From a fresh checkout of `NousResearch/hermes-agent`:

```bash
git apply patches/hermes-memory-manager-late-tools.patch
```

Verify:

```bash
# 4/6 of these fail on the pre-patch code; all 6 pass after.
python -m pytest tests/agent/test_memory_manager_late_tools.py -v

# Full memory-provider sweep should be green.
python -m pytest tests/agent/test_memory_provider.py \
                 tests/agent/test_memory_user_id.py \
                 tests/run_agent/test_commit_memory_session_context_engine.py \
                 tests/agent/test_memory_manager_late_tools.py -q
```

### Activating in a running Hermes install

```bash
cd ~/.hermes/hermes-agent
git apply /path/to/patches/hermes-memory-manager-late-tools.patch
# Restart Hermes (the gateway / CLI / WebUI loads agent/memory_manager.py
# at process start; tool changes do not hot-reload mid-session).
```

### Re-applying after upstream bumps

If `agent/memory_manager.py` has changed upstream and the patch no longer
applies cleanly:

```bash
git apply --3way patches/hermes-memory-manager-late-tools.patch
# Resolve any conflicts, regenerate the patch:
git diff -- agent/memory_manager.py tests/agent/test_memory_manager_late_tools.py \
    > patches/hermes-memory-manager-late-tools.patch
# Update the Base SHA in the inventory table above.
```

### Upstream submission

This patch is suitable for upstreaming to `NousResearch/hermes-agent`. The fix
is core Hermes behavior and benefits every memory provider plugin with
init-gated tool surfaces (hangarx-memory, and any future provider that
constructs clients/mirrors during `initialize()`).
