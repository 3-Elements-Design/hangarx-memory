# Changelog

All notable changes to `hangarx-memory` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] — 2026-05-19

### Added — local Cortex auto-detection

- **`probe_health()`** helper in `client.py` — unauthenticated GET
  `/health` with a 0.5 s default timeout, calibrated to real Cortex
  latency (the endpoint probes FalkorDB + Postgres internally and
  typically takes 200–300 ms even on localhost).
- **Auto-detect on `initialize()`** — when no `base_url` is explicitly
  configured, probe `http://localhost:3400` and `http://localhost:4000`.
  If one answers healthy, switch `base_url` and operate in **keyless
  local mode** (Cortex's `AUTH_OPTIONAL_FOR_LOCAL` pattern accepts
  requests without an API key on localhost).
- **`is_available()` falls back to a local probe**, so Hermes activates
  the provider in pure local mode with zero config.
- **`hermes hangarx-memory status`** now reports a `mode` field
  (`local-keyless`, `local-with-key`, `remote`, `vault-only`,
  `inactive`), the auto-detected URL, and the effective base.
- **New config knobs**: `auto_detect_local` (default `true`),
  `local_candidates` (default `localhost:3400,localhost:4000`),
  `local_probe_timeout` (default `0.5`), and the `CORTEX_LOCAL_URLS`
  env var for non-default ports.

### Added — memory introspection tools

Four new tools that answer "what do you know about me?" in the agent
loop, all backed by Cortex's `GET /v1/memory/*` read endpoints:

- **`cortex_memory_stats`** — total items, total categories, items by
  priority. Wraps `GET /v1/memory/stats`.
- **`cortex_memory_categories`** — list of categories with item counts
  (user_fact, agent_instruction, preference, …). Wraps
  `GET /v1/memory/categories`.
- **`cortex_memory_items`** — paginated raw memory items with a
  client-side limit so we don't flood the context. Wraps
  `GET /v1/memory/items`.
- **`cortex_about_me`** — synthesis tool that combines stats +
  categories + sample items + profile recall into one structured
  response. Each section degrades gracefully so a partial failure
  still returns useful data.

These tools are only registered in `tool_mode: full` and only when a
Cortex client is configured. They are not advertised in `compact` mode.

### Added — pytest suite

- **72 tests** under `tests/` covering back-compat removal, config
  loading, provider lifecycle (with the real `MemoryProvider` ABC
  isinstance check), local auto-detect (mocked + live integration),
  the HTTP client (against a real in-process `ThreadingHTTPServer`),
  and all four introspection tools.
- **`tests/conftest.py`** ships fixtures (`hermes_home`,
  `write_config`, `write_legacy_config`, `vault_path`) plus an autouse
  env-stripping fixture that prevents the developer's real config from
  leaking into the suite.
- **`conftest.py`** at the repo root adds `~/.hermes/hermes-agent` to
  `sys.path` so the ABC check runs without an editable install.

### Removed

- All remaining `cortex-memory` back-compat aliases — the legacy
  `CortexMemoryProvider` class alias, the `LEGACY_CONFIG_FILE_NAME`
  constant + dual-path config loader, the `CORTEX_VAULT_PATH` env var
  fallback, and the deprecation shim at `~/.hermes/plugins/cortex-memory/`.
  This is a fresh internal rollout; no users yet, no reason to carry
  dead surface area.

## [0.4.0] — 2026-05-18

Initial public release of `hangarx-memory` — a Hermes Agent memory
provider bridging the agent loop into HangarX Cortex (GraphRAG + memU)
and an Obsidian vault. The plugin keeps the Cortex tool names
(`cortex_recall`, `cortex_remember`, etc.) for cross-client consistency
with the JS SDK, OpenClaw plugin, and Cortex MCP server; the plugin
identity, config file, and CLI subcommand are all `hangarx-memory`.

### Provider

- Subclasses Hermes' `MemoryProvider` ABC. Zero runtime dependencies —
  pure stdlib (`urllib`, `json`, `threading`).
- Lifecycle hooks wired: `prefetch`, `queue_prefetch`, `sync_turn`,
  `on_memory_write`, `on_pre_compress`, `on_session_end`,
  `on_session_switch`, `on_turn_start`.

### Cortex integration

- **Streaming prefetch** via `POST /v1/ask/chat/stream` with automatic
  fallback to the non-streaming endpoint. Saves ~500 ms of
  head-of-line latency. Configurable via `stream_prefetch`
  (default `true`).
- **Dialectic prefetch** — optional 2nd Cortex pass per turn that
  refines intent given what pass 1 found. Configurable via
  `dialectic_passes` (1–3, default 1).
- **Trust-scoring tool** `cortex_rate_memory(memory_id, helpful, comment)`
  wrapping Cortex MCP `cortex_feedback`. Lets the model train recall
  ranking after each turn.
- **URI addressing tool** `cortex_resolve_uri(uri)` accepting
  `vault://Folder/Note`, `cortex://memory/<id>`,
  `cortex://entity/<id>`, and `cortex://<workspace>/<id>`.
- **Background reflection cron script** at
  `hangarx_memory/scripts/reflect.py` — designed for Hermes' `no_agent`
  cron mode (silent on no-op, prints one line when memories promoted).
- **Cadence control** — `prefetch_cadence` and `profile_cadence` knobs.
- **`auto_promote`** wired into `on_session_end` to distill verbatim
  turns into structured facts before reflection.
- **Compact tool mode** — `tool_mode: compact` exposes only 4 tools
  (`cortex_ask`, `cortex_recall`, `cortex_remember`, `vault_search`).
- **Dedup tools** `cortex_find_duplicates` and `cortex_merge_entities`.

### Obsidian vault integration

- Conversations mirror to markdown notes; prefetch runs local search
  in parallel with Cortex; memory writes append to a vault index note.
- **Session-end summary note** — on `on_session_end`, writes a digest
  to `$VAULT/Hermes Sessions/Summaries/<session>.md` with turn log +
  deduped `[[wikilinks]]` to every cited source.
- **Citation injection** — Cortex citation sources that resolve to
  vault notes become `[[wikilinks]]` in the prefetch context block.
  Configurable via `citation_wikilinks` (default `true`).
- 5 vault tools (`vault_search`, `vault_read_note`, `vault_list_notes`,
  `vault_create_note`, `vault_append_note`) plus `cortex_ingest_vault`
  for promoting vault notes into Cortex.
- Three operating modes: Cortex-only, vault-only, hybrid.

### CLI

- `hermes hangarx-memory status` / `test` / `tools` / `reflect`
- `hermes hangarx-memory schedule` — prints the exact
  `cronjob(action='create', ...)` snippet for the reflection job.
- `hermes hangarx-memory vault <status|list|search|open>`
