# Changelog

All notable changes to `hangarx-memory` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
