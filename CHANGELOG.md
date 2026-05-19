# Changelog

All notable changes to `hangarx-memory` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] — 2026-05-19

The "local-first install" release. The plugin now ships with a
working all-in-one Cortex stack and a CLI to drive it, so users without
the HangarX Obsidian plugin (or anyone who wants offline-only Cortex)
can spin up the full memory backend in one command.

### Added — bundled local Cortex stack

- **`docker-compose.cortex.yml`** at the package root — self-contained
  FalkorDB + Cortex API deployment optimized for a developer laptop.
  In-memory storage (no Postgres / ClickHouse), Ollama LLM by default,
  ~2 GB RAM limit. The plugin auto-detects this stack at
  `http://localhost:3400` and runs in keyless local mode.
- **Mirrored copy inside `hangarx_memory/`** so `pip install
  hangarx-memory` ships the compose file too. CLI discovery looks in
  both locations.
- **`pyproject.toml`** updated: `package-data` now includes
  `docker-compose.cortex.yml`.

### Added — `hermes hangarx-memory docker` subcommand

Six subcommands that drive the bundled compose stack on the user's
behalf:

| Command | What it does |
|---|---|
| `docker up` | Start FalkorDB + Cortex API in the background; wait for healthcheck; probe `/health` |
| `docker down` | Stop and remove containers (volumes preserved). `--purge` also deletes the FalkorDB volume |
| `docker status` | `docker compose ps` + live `/health` probe with service breakdown |
| `docker logs` | Tail last 100 lines. `-f` to stream. Optional service filter (`cortex-api` / `falkordb`) |
| `docker pull` | Pre-fetch images without starting |
| `docker path` | Print the compose file path (useful for shell aliases) |

The CLI fails fast with clear errors when Docker isn't installed or the
daemon isn't running.

### Changed

- **README.md** rewritten with v0.5.0 features + the docker quickstart
  prominently placed.
- **Public docs** at `apps/web/public/markdown-docs/integrations/hermes-agent.md`
  rewritten to match. References the standalone GitHub repo and links
  to the changelog.

### Engineering

- **13 new tests** in `tests/test_docker_cli.py` covering compose file
  discovery, docker availability probing, subcommand dispatch, and
  argparse wiring. Total suite now **181 passing**, ruff clean.

## [0.5.0] — 2026-05-19

The "audit + isolation" release. Three category-defining features land
together: a memory changelog the user can read and revert from,
per-profile workspace isolation so the coder profile stops polluting
the journaling profile, and sensitivity-aware writes that refuse to
leak secrets into subagents or cron jobs.

### Added — D. Memory changelog + revert

- **`MemoryChangelog`** in `hangarx_memory/changelog.py` — an
  append-only audit log of every memory mutation. Bounded ring buffer
  (default 200 entries) plus a vault sink at
  `$VAULT/<sessions>/Memory Changelog.md` so the user can read it in
  Obsidian, search it, or grep it.
- **Five entry kinds** automatically recorded: `ADDED`, `MERGED`,
  `FORGOT`, `PROMOTED`, `REVERTED`, plus `BLOCKED` for refused writes.
- **`cortex_memory_changelog(limit)`** tool — model answers "what
  changed recently?" / "why do you remember X?" without a network hop.
- **`cortex_revert_memory(memory_id, reason)`** tool — wraps
  `DELETE /v1/memory/forget/<id>` and emits a `REVERTED` audit entry.
- **`CortexClient.forget()`** + `extract_memory_id()` helper for
  pulling ids out of every Cortex response shape.
- Wired into `on_memory_write`, the merge tool path, `auto_promote`
  in `on_session_end`, and the revert tool.
- **Config**: `changelog_enabled` (default `true`),
  `changelog_buffer_size` (default `200`).

### Added — #11. Profile-templated workspaces

When Hermes activates the provider with `agent_identity` (the active
profile name), derive a deterministic `workspace_id` so each profile
gets its own clean Cortex memory bucket out of the box. No more cross-
profile memory contamination between e.g. a `coder` and `journaling`
profile.

- **`_slugify_workspace`** normalizes identity labels to safe ids
  (`My Research Profile` → `my-research-profile`).
- **Default template**: `hermes-{identity}`. Honors `{workspace}` too
  for users who want `<workspace>-<identity>` patterns.
- **Explicit `workspace_id`** in config or `CORTEX_WORKSPACE_ID` env
  always wins — the template only fires when nothing is pinned.
- **Empty template** (`workspace_template: ""`) disables the feature
  for users on a single shared workspace.
- **Config**: `workspace_template` (default `"hermes-{identity}"`).

### Added — C. Sensitivity tags + agent-context-aware gate

Three sensitivity levels (`public` / `private` / `secret`) tag every
stored memory. Writes from non-primary contexts (`subagent`, `cron`,
`background`) refuse to store `private` or `secret` data, and
prefetch is suppressed entirely in those contexts by default.

- **`hangarx_memory/sensitivity.py`** — regex-based auto-detection of
  API keys (OpenAI, Anthropic, AWS, GitHub, GitLab, Slack, Cortex),
  JWTs, emails, US phone numbers, and SSN-shaped strings.
- **Explicit tag** wins over inference via the `sensitivity` key in
  metadata; the more restrictive of (explicit, inferred) is recorded.
- **Write gate** in `on_memory_write` — refuses restricted writes
  from non-primary contexts and logs `BLOCKED` to the changelog.
- **Prefetch gate** in `queue_prefetch` — suppresses prefetch
  entirely in non-primary contexts. Override via
  `prefetch_in_subagent: true` if you genuinely want subagents to
  inherit the parent's memory context.
- **Config**: `sensitivity_enabled` (default `true`),
  `sensitivity_auto_detect` (default `true`),
  `prefetch_in_subagent` (default `false`).

### Added — B. Per-response citations (Perplexity-style)

When the prefetch context contains Cortex citations, append a short
`### Response style` directive instructing the model to end its reply
with a `**Based on:**` footer listing the specific sources it used
(as `[[wikilinks]]` for vault notes, `memory:<id>` for Cortex items).

- **Shipped as a soft prompt in the prefetch context** — no Hermes
  hook required. Combined with the existing `### Citations` block,
  this gives the Perplexity-style auditable response pattern.
- **Config**: `response_citations` (default `true`).

### Engineering

- **96 new tests** across `tests/test_changelog.py` (32),
  `tests/test_workspace_templates.py` (21), `tests/test_sensitivity.py`
  (39), and `tests/test_response_citations.py` (4). Total suite now
  **168 passing**, ruff clean.

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
