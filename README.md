# hangarx-memory

> Hermes Agent memory provider for HangarX Cortex — GraphRAG knowledge
> graph, vector memory, memU agent memory, and optional Obsidian vault
> mirroring. Zero Python dependencies.

`hangarx-memory` makes [HangarX Cortex](https://hangarx.ai) the memory
backend for [Hermes Agent](https://hermes-agent.nousresearch.com). Every
turn gets persisted into the knowledge graph, vector store, and memU;
prior context is recalled before each turn; and the model gets a small,
curated set of Cortex tools to read and write memory directly.

The plugin also has an **Obsidian vault** mode that mirrors
conversations to local markdown notes with proper frontmatter,
wikilinks, and Obsidian graph-view tagging. The vault sits alongside
Cortex as a human-readable working copy.

## Three operating modes

| Mode | `api_key` | `vault_path` | What you get |
|---|---|---|---|
| **Cortex-only** | set | unset | 7 Cortex tools, async prefetch, sync to memU |
| **Vault-only** | unset | set | 5 vault tools, local search + transcript notes |
| **Hybrid** | set | set | All 13+ tools, merged prefetch, both writes |

## Install

### Option 1 — pip + symlink (recommended for Hermes)

```bash
pip install hangarx-memory
SITE=$(python -c "import hangarx_memory, os; print(os.path.dirname(os.path.dirname(hangarx_memory.__file__)))")
ln -s "$SITE" ~/.hermes/plugins/hangarx-memory
```

### Option 2 — git clone

```bash
git clone https://github.com/hangarx/hangarx-memory ~/.hermes/plugins/hangarx-memory
```

### Option 3 — monorepo symlink (development)

```bash
ln -s ~/Sites/hangarx-business-agent/packages/hangarx-memory \
      ~/.hermes/plugins/hangarx-memory
```

## Activate

```bash
hermes config set memory.provider hangarx-memory
hermes memory setup
```

`hermes memory setup` walks you through the config schema. Secrets land
in `$HERMES_HOME/.env` (as `CORTEX_API_KEY`); everything else lands in
`$HERMES_HOME/hangarx-memory.json`.

## Configuration

| Key | Default | Notes |
|---|---|---|
| `api_key` | – | Stored in `.env` as `CORTEX_API_KEY`. Optional. |
| `base_url` | `https://cortex.hangarx.ai` | Use `http://localhost:3400` for local Docker stack. |
| `workspace_id` | – | Optional for single-workspace orgs. |
| `organization_id` | – | Optional. |
| `auth_mode` | `bearer` | `bearer` or `x-api-key`. |
| `agent_id` | `hermes` | Identity used inside Cortex memU memory. |
| `vault_path` | – | Absolute path to an Obsidian vault. |
| `vault_sessions_folder` | `Hermes Sessions` | Subfolder for written notes. |
| `vault_sync_mode` | `per-session` | `off` / `per-session` / `daily` / `per-turn`. |
| `vault_link_style` | `wikilink` | Or `markdown`. |
| `vault_search_enabled` | `true` | Local search runs during prefetch. |
| `vault_compression_snapshots` | `false` | Save context-compression snapshots. |
| `tool_mode` | `full` | `full` exposes ~17 tools; `compact` exposes 4. |
| `prefetch_cadence` | `1` | Fire `/v1/ask/chat` every N turns. `0` disables. |
| `profile_cadence` | `25` | Inject profile recall on turn 1 + every N turns. |
| `auto_promote_enabled` | `true` | Distill turns into facts on session end. |
| `auto_promote_limit` | `25` | Max items per auto-promote pass. |
| `stream_prefetch` | `true` | Use `/v1/ask/chat/stream` for low-latency context. |
| `dialectic_passes` | `1` | 1–3. `2`+ enables refinement pass. |
| `session_summary_enabled` | `true` | End-of-session digest note with backlinks. |
| `citation_wikilinks` | `true` | Resolve Cortex citation sources to `[[wikilinks]]`. |

## CLI

```bash
hermes hangarx-memory status        # config + key presence
hermes hangarx-memory test          # MCP tools/list round-trip
hermes hangarx-memory tools         # tools this plugin exposes
hermes hangarx-memory reflect       # trigger Cortex memU reflection
hermes hangarx-memory schedule      # cron-install snippet for nightly reflection

hermes hangarx-memory vault status  # vault root + sync mode + note count
hermes hangarx-memory vault list    # --folder, --tag, --limit
hermes hangarx-memory vault search  # --folder, --limit
hermes hangarx-memory vault open    # open vault folder (macOS Finder)
```

## What it does in the agent loop

| Hermes hook | Cortex action | Vault action |
|---|---|---|
| `queue_prefetch(q)` | `POST /v1/ask/chat[/stream]` async | Synchronous local search |
| `prefetch(q)` | Returns merged context block | (same — merged) |
| `sync_turn(u, a)` | `POST /v1/memory/remember-raw` | Append turn to session note |
| `on_memory_write(...)` | `POST /v1/memory/remember` | Append bullet to `Hermes Memory.md` |
| `on_pre_compress(msgs)` | `POST /v1/memory/remember-raw` | Optional `Compressions/<id>.md` snapshot |
| `on_session_end(msgs)` | `auto_promote` + `reflect` | Write `Summaries/<id>.md` with backlinks |
| `on_session_switch(...)` | Reset prefetch cache + counter | (no-op) |
| `on_turn_start(n, msg)` | Bump turn counter for cadence | (no-op) |

All writes are non-blocking — vault writes happen on the same daemon
thread as Cortex writes so the agent loop never waits on disk or
network.

## Tools exposed to the model

### Cortex (active when `api_key` is set)

| Tool | Backed by |
|---|---|
| `cortex_recall` | `POST /v1/memory/recall` |
| `cortex_remember` | `POST /v1/memory/remember` |
| `cortex_ask` | `POST /v1/ask/chat/answer` |
| `cortex_search_documents` | `POST /v1/search/vectors/search` |
| `cortex_search_entities` | MCP `tools/call cortex_search_entities` |
| `cortex_query_graph` | MCP `tools/call cortex_query_graph` |
| `cortex_ingest` | MCP `tools/call cortex_ingest` |
| `cortex_find_duplicates` | MCP `tools/call cortex_find_duplicates` |
| `cortex_merge_entities` | MCP `tools/call cortex_merge_entities` |
| `cortex_rate_memory` | MCP `tools/call cortex_feedback` |
| `cortex_resolve_uri` | Routes `vault://` and `cortex://` URIs |

### Vault (active when `vault_path` is set)

| Tool | What it does |
|---|---|
| `vault_search` | Local content + filename match |
| `vault_read_note` | Read by path or `[[wikilink]]` |
| `vault_list_notes` | List by folder, tag, limit |
| `vault_create_note` | Write new markdown with optional frontmatter |
| `vault_append_note` | Append body + merge frontmatter |

### Hybrid (both)

| Tool | What it does |
|---|---|
| `cortex_ingest_vault` | Read a vault note, ingest into Cortex with `source: obsidian` provenance |

## Running locally

The full HangarX Cortex stack is fully Dockerized — point the plugin at
the local instance for zero-cloud operation:

```bash
# 1. Start the local stack (from the cortex-api package)
cd ~/Sites/hangarx-business-agent/packages/cortex-api
docker compose -f docker-compose.cortex.yml up -d

# 2. Tell hangarx-memory to use it
cat > ~/.hermes/hangarx-memory.json <<'JSON'
{
  "base_url": "http://localhost:3400",
  "agent_id": "hermes",
  "vault_path": "/Users/you/Obsidian/MyVault"
}
JSON

# 3. Add a placeholder API key (local mode doesn't auth strictly)
echo "CORTEX_API_KEY=local-dev" >> ~/.hermes/.env
```

You now have FalkorDB (graph + vector), Postgres, and the Cortex API
running on your machine — no network calls leave it.

## Architecture

```
Hermes Agent
    │
    ▼
hangarx-memory (this plugin)
    ├── prefetch / sync ──▶ Cortex API
    │                           ├── FalkorDB (knowledge graph + vectors)
    │                           ├── Postgres (memU agent memory)
    │                           └── /v1/ask/chat smart routing + reranking
    │
    └── markdown + frontmatter ◀──▶ Obsidian Vault
                                        ▲
                                        │ (hangarx-obsidian plugin
                                        │  indexes vault into Cortex
                                        │  bidirectionally)
                                        │
                                  Obsidian app
```

The Obsidian plugin handles inbound vault→Cortex indexing. This plugin
handles outbound Hermes→Cortex and Hermes→vault writes. Together they
give you a vault that knows everything your agent has discussed and an
agent that knows everything in your vault.

## Backwards compatibility

`hangarx-memory` started life as `cortex-memory`. Existing installs at
`~/.hermes/plugins/cortex-memory/` continue to work via a deprecation
shim that re-exports `HangarxMemoryProvider` and emits a one-time
warning. The shim will be removed in a future minor release; switch to
`memory.provider: hangarx-memory` to silence it.

## License

MIT. See [LICENSE](LICENSE).
