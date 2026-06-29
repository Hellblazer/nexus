# Nexus Configuration Reference

## Config Hierarchy

Four levels, highest priority wins:

1. **Environment variables** (`NX_*`) — highest priority
2. **Per-repo**: `.nexus.yml` in repo root (gitignored by default)
3. **Global**: `~/.config/nexus/config.yml`
4. **Built-in defaults** — lowest priority

Each level is deep-merged, with higher-priority values winning.

## Local Mode

Nexus auto-detects local mode when cloud credentials are absent. The recommended setup is `uv tool install conexus && nx init`: `nx init` (RDR-144) presents the 384-vs-768 embedder choice, adds the `[local]` extra for you when you pick bge-768, and provisions the model (see [nx init](cli-reference.md#nx-init)). You can still request the extra at install time with `uv tool install "conexus[local]"`; the `[local]` extra adds the bge-768 embedder for better local search, while plain `conexus` uses the built-in 384-dim MiniLM. Upgrade with `uv tool upgrade conexus` to keep your extras, never `uv tool install --force`, which drops `[local]`.

| Env var | Default | Description |
|---|---|---|
| `NX_LOCAL` | (auto) | `1` = force local, `0` = force cloud, unset = auto-detect |
| `NX_LOCAL_CHROMA_PATH` | `~/.local/share/nexus/chroma` | Path to the legacy ChromaDB store. As of 6.0 this is read only as the **migration source** (`nx guided-upgrade`); T3 serves from the Postgres+pgvector service, not this path. |
| `NX_LOCAL_EMBED_MODEL` | (auto) | Force a specific local embedding model name |
| `NEXUS_CATALOG_PATH` | `~/.config/nexus/catalog` | Override catalog git repo location |
| `NEXUS_CATALOG_ALLOW_CROSS_PROJECT` | unset | Set to `1` to bypass the register-time cross-project source_uri guard. Emergency-only escape hatch for known-good recovery scripts that legitimately need to register rows across project boundaries; never the right answer for normal indexing |

**`config.yml` keys** (set by `nx init`, under the `local:` block in `~/.config/nexus/config.yml`):

| Key | Default | Description |
|---|---|---|
| `local.embed_model` | (auto-select) | The embedder `nx init` recorded (`BAAI/bge-base-en-v1.5` or `all-MiniLM-L6-v2`). Absent = legacy auto-select (bge if the `[local]` extra is importable, else MiniLM). |
| `local.fastembed_cache_path` | `~/.local/share/nexus/fastembed_cache` (XDG-aware) | Stable cache dir for the bge-768 model so it is not re-downloaded to a volatile `$TMPDIR` on every reboot. |

**Mode selection**: As of 6.0, managed-cloud mode activates when `NX_SERVICE_URL` (+ `NX_SERVICE_TOKEN`) is set — the client routes T3 through the hosted service (see [Managed-Cloud Credentials](#managed-cloud-credentials) below). Otherwise local mode is used. Set `NX_LOCAL=1` to force local mode even with service credentials present. (The legacy `CHROMA_API_KEY`/`VOYAGE_API_KEY` auto-detect predates the service substrate and applies only to pre-6.0 installs that have not migrated.)

**Embedding tiers**: Tier 0 (bundled MiniLM-L6-v2, 384d) is always available. Install `uv tool install "conexus[local]"` for tier 1 (bge-base-en-v1.5, 768d, better quality; downloads the model on first embed). To add the extra to an existing install: `uv tool install --reinstall "conexus[local]"`. Upgrade later with `uv tool upgrade conexus`, which preserves the extra — never `uv tool install --force`, which drops it.

**Legacy ChromaDB store path**: Defaults to `$XDG_DATA_HOME/nexus/chroma` or `~/.local/share/nexus/chroma`, overridable with `NX_LOCAL_CHROMA_PATH`. As of 6.0 this path is read only as the **migration source** (`nx guided-upgrade`); live T3 serves from the Postgres+pgvector service.

**Switching embedders or modes**: Changing the embedding model (switching local↔cloud, *or* switching local tiers 384-dim MiniLM ↔ 768-dim bge) makes the existing vectors incompatible (different dimensions/space). On the next `nx index repo .` the staleness check detects the model change and re-embeds into **new** collections under the new model token. **It does NOT automatically delete or migrate the old collections**: they remain behind under the previous token and silently return no results (their dimension no longer matches the active embedder).

When you switch local tiers via `nx init` (the common 384 → bge-768 upgrade), `nx init` detects these stale collections and offers a safe, ordered migration (preview → double-confirm → reindex-first → delete-after-verify; old collections deleted only after the new ones are verified populated, so a failed reindex never loses data). `code__` and manual-note (`store_put`) collections are reported but never auto-deleted. Outside the `nx init` flow you can clean up manually: `nx doctor` flags the dimension mismatch, `nx collection reindex <name>` rebuilds one from source, and `nx collection delete <name>` removes an orphan.

## Managed-Cloud Credentials

As of 6.0, managed-cloud mode points `nx` at a hosted nexus service that owns its cloud Postgres + pgvector and embeds with Voyage server-side. The client credentials are the service URL and a bearer token. Both resolve **env first, then `config.yml`**: set them interactively with `nx config init` (or `nx config set service_url/service_token`), or export the env vars below — the environment always takes precedence:

| Env var | Required | Notes |
|---|---|---|
| `NX_SERVICE_URL` | No | Managed nexus service base URL. Defaults to `https://api.conexus-nexus.com`; override for a self-hosted or staging deployment. |
| `NX_SERVICE_TOKEN` | Cloud mode | Bearer token for the managed service. |

Export both in your shell profile or process manager. You do not supply a Voyage key in managed-cloud mode (the service owns it).

### Migration-source credentials (pre-6.0 only)

These ChromaDB Cloud keys are **not** used for live T3 serving in 6.0. They are read only when `nx guided-upgrade` / `nx migrate-to-service` needs to read an existing ChromaDB Cloud store as the migration source:

| Config key | Env var | Notes |
|---|---|---|
| `chroma_api_key` | `CHROMA_API_KEY` | ChromaDB Cloud API key (migration source) |
| `chroma_database` | `CHROMA_DATABASE` | ChromaDB Cloud database name, e.g. `nexus` (migration source) |
| `chroma_tenant` | `CHROMA_TENANT` | Auto-inferred from the API key; only for multi-workspace setups |

## Semantic Scholar (Enrichment)

| Env var | Required | Notes |
|---|---|---|
| `S2_API_KEY` | No | Free API key for 100 req/s (vs 100/5min unauthenticated). Get one at https://www.semanticscholar.org/product/api#api-key |

Used by `nx enrich bib` to fetch bibliographic metadata (year, venue, authors, citation count). Without the key, enrichment works but is ~50x slower due to rate limiting.

The notes below apply only to a pre-6.0 ChromaDB Cloud store being read as a **migration source**. In 6.0 live T3 serving is the managed nexus service (`service_url` + `service_token` above); these no longer describe the active backend.

**`chroma_database` is the database name** of the ChromaDB Cloud store the migration reads from. All collection prefixes (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexisted in it.

**`chroma_tenant` is optional.** The ChromaDB `CloudClient` infers the tenant UUID directly from your API key. You only need to set it explicitly if you belong to multiple Chroma Cloud workspaces.

**Single-database architecture (history).** RDR-037 (2026-03-14) consolidated the legacy four-database layout (`{base}_code` / `{base}_docs` / `{base}_rdr` / `{base}_knowledge`) into a single database with collection prefixes. The transitional auto-detect probe was retired in 4.14.2 once the migration window closed.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | Voyage reranker for multi-corpus merge |
| `client.host` | `NX_CLIENT_HOST` | `localhost` | Legacy ChromaDB host override; no-op as of 6.0 (the managed/local service URL is `NX_SERVICE_URL`). |
| `pdf.extractor` | — | `auto` | PDF extraction backend: `auto`, `docling`, or `mineru`. Set globally with `nx config set pdf.extractor=mineru` |
| `pdf.mineru_server_url` | — | `http://127.0.0.1:8010` | MinerU API server URL. Auto-updated when `nx mineru start` binds a port |
| `pdf.mineru_table_enable` | — | `false` | Enable table extraction in MinerU. Slower; use when PDFs contain structured tables |
| `pdf.mineru_page_batch` | — | `1` | Pages per MinerU request. Increase for faster throughput at the cost of memory |
| `voyageai.read_timeout_seconds` | `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` | `120` | Request timeout (seconds) for Voyage AI API calls. Increase for large PDF indexing |
| `search.hybrid_default` | — | `false` | Default ripgrep hybrid search mode for `nx search`. Set `true` to always run hybrid |
| `search.hnsw_ef` | — | `256` | HNSW `search_ef` parameter for local-mode collections. Higher values improve tail recall at the cost of query latency. Ignored in cloud mode (SPANN) |
| `search.distance_threshold.code` | — | `0.45` | Maximum distance for code corpus results. Results above this are filtered as noise |
| `search.distance_threshold.knowledge` | — | `0.65` | Maximum distance for knowledge corpus results |
| `search.distance_threshold.docs` | — | `0.65` | Maximum distance for docs corpus results |
| `search.distance_threshold.rdr` | — | `0.65` | Maximum distance for RDR corpus results |
| `search.distance_threshold.default` | — | `0.55` | Maximum distance for unknown corpus types |
| `search.cluster_by` | — | `null` | Set to `semantic` to group search results by Ward hierarchical clustering. Disabled by default |
| `search.contradiction_check` | — | `true` | JIT contradiction detection (RDR-057). Flags result pairs with high similarity but different `source_agent` provenance. Adds `[CONTRADICTS ANOTHER RESULT]` to search output. Set to `false` to disable. The check fetches embeddings for flagged candidates and adds a network round-trip per flagged collection |

Embedding models are selected automatically based on collection type (see [Storage Tiers](storage-tiers.md)): `voyage-code-3` for code, `voyage-context-3` (CCE) for docs/rdr/knowledge. All collections use the same model for both index and query.

**Distance thresholds** filter noise from search results automatically. Thresholds are calibrated for Voyage AI embeddings and only apply in cloud mode. Override per corpus in `.nexus.yml`:

```yaml
search:
  distance_threshold:
    knowledge: 0.60   # tighter threshold for knowledge collections
```

## Per-Repo Overrides (.nexus.yml)

Place `.nexus.yml` at repo root. It is gitignored by default.

```yaml
indexing:
  code_extensions: [".proto", ".thrift"]    # added to the built-in code set (default: [])
  prose_extensions: [".txt.j2", ".md.tmpl"] # forced to prose, wins over code (default: [])
  rdr_paths: ["docs/rdr", "decisions"]      # directories indexed into rdr__ collection (default: ["docs/rdr"])
  include_untracked: true                   # also index untracked (but not .gitignored) files (default: false)
```

```yaml
pdf:
  extractor: mineru             # auto | docling | mineru (default: auto)
  mineru_server_url: http://127.0.0.1:8010  # MinerU API endpoint (default)
  mineru_table_enable: false    # enable table extraction (default: false)
  mineru_page_batch: 1          # pages per MinerU request (default: 1)
```

Or set via CLI: `nx config set pdf.extractor=mineru` (writes to global config). See [PDF Extraction Backends](cli-reference.md#pdf-extraction-backends) for details.

Merge behavior: nested dict keys are **additive** (both global and per-repo keys are retained). Scalar values and lists are **replacement** (the per-repo value wins entirely between config levels). However, `code_extensions` is additive to the **built-in** extension set — it extends the defaults, it does not replace them. `prose_extensions` wins over everything: if an extension appears in both lists, it is classified as prose. See [Repo Indexing](repo-indexing.md) for the full extension list and override semantics.

## Taxonomy

Topic taxonomy settings. Topics are auto-discovered after `nx index repo`.

```yaml
taxonomy:
  auto_label: true                       # Generate labels via claude -p --model haiku (default)
  local_exclude_collections: ["code__*"] # Skip code collections in local mode (general-purpose local embeddings are poor for code)
  collection_prefixes: [docs, code, knowledge, rdr]  # Prefixes recognized by nx taxonomy validate-refs (RDR-081)
```

| Key | Default | Description |
|-----|---------|-------------|
| `auto_label` | `true` | Auto-label topics with Claude haiku after discover. Requires `claude` CLI on PATH. Set `false` to keep c-TF-IDF labels. |
| `local_exclude_collections` | `["code__*"]` | Glob patterns for collections to skip in local mode. Cloud mode (Voyage embeddings) ignores this — set to `[]` to enable all collections locally. |
| `collection_prefixes` | `["docs", "code", "knowledge", "rdr"]` | Prefix whitelist for `nx taxonomy validate-refs`. Extend this when your project adds a new user-facing collection prefix (e.g. `"custom"`). Internal-prefix collections (`taxonomy__*`, `plans__*`) are implementation-fixed and intentionally excluded. |

## Daemon environment variables

Since conexus 4.34.0 (RDR-120 storage substrate split), the CLI and
MCP server route T2 through the T2 daemon. The daemon publishes its
address via a discovery file at `~/.config/nexus/t2_addr.<uid>`; T3
routes through the native nexus-service (`nx daemon service`, RDR-155),
discovered via `~/.config/nexus/storage_service_addr.<uid>` and
overridden with `NX_SERVICE_URL` (see [Managed-Cloud Credentials](#managed-cloud-credentials)).
Clients honour these env-var overrides:

| Variable | Effect | Default |
|----------|--------|---------|
| `NX_T2_ADDR` | TCP `host:port` for the T2 daemon (e.g. `host.docker.internal:55459`). Used by dev containers reaching the host's loopback. | discovery file |
| `NX_T2_SOCK` | UDS path for the T2 daemon (Linux-only when bind-mounted from the host into a container). Mutually exclusive with `NX_T2_ADDR`. | discovery file |
| `NX_SERVICE_URL` | Full base URL of the nexus-service (T3 vectors over `/v1/vectors`). Used by dev containers and managed-cloud clients. | `storage_service_addr.<uid>` lease, then `https://api.conexus-nexus.com` |
| `NX_SERVICE_TOKEN` | Bearer token for the nexus-service. | local: from `pg_credentials`; managed: user-supplied |
| `NX_STORAGE_BACKEND` | Global storage-backend switch (RDR-152). `service` routes T2 stores + T3 vectors through the Java/Postgres nexus-service; `sqlite` is the legacy local opt-out (SQLite + the T2 daemon, still supported per RDR-164 CA-5). | `service` (hard default since RDR-152/155) |
| `NX_STORAGE_BACKEND_<STORE>` | Per-store override of `NX_STORAGE_BACKEND`, taking precedence over the global value. Known `<STORE>` suffixes: `T1`, `CATALOG`, `VECTORS`, `TAXONOMY`, `ASPECT_QUEUE` (e.g. `NX_STORAGE_BACKEND_VECTORS=service`). Each is `service` or `sqlite`. | inherits `NX_STORAGE_BACKEND` |
| `NX_LOCAL` | Force local mode (local nexus-service, bge-768) even when cloud credentials exist. | unset (cloud mode if credentials present) |

> Note: the retired `nx daemon t3` ChromaDB path and its `NX_T3_ADDR` override no longer route T3 serving; T3 traffic goes to the nexus-service via `NX_SERVICE_URL`.

When the T2 env vars are unset, the client falls back to the discovery
file. If no daemon is reachable, the CLI raises
`T2DaemonNotReachableError` with a hint to run
`nx daemon t2 ensure-running` or `install --autostart`. See
[Container Integration](container-integration.md) for the full
operator-facing matrix of transport choices per platform.

## Storage Service (Postgres) Prerequisites

> **Local installs: skip this section.** `nx init` provisions the
> bundled relocatable Postgres + pgvector cluster, creates the roles, and enables
> the extensions for you — no DBA, no manual `CREATE EXTENSION`. The prerequisites
> below apply only when you bring your **own** Postgres (an operator pointing the
> service at a managed/self-hosted cluster via `NX_DB_*`).

The Java storage service (RDR-152/RDR-155) applies its Liquibase changelog at
startup using the `NX_DB_ADMIN_*` credentials (falling back to `NX_DB_*` in
single-role development setups). When you bring your own Postgres, two
prerequisites must be satisfied by a DBA (superuser) before the first migration
run:

1. **Extensions.** `CREATE EXTENSION IF NOT EXISTS vector;` and
   `CREATE EXTENSION IF NOT EXISTS pg_trgm;` — neither is a trusted extension,
   and the migration role (`nexus_admin`) is NOSUPERUSER, so changeset
   `vectors-001-1` fails without this pre-step. Once the extensions exist the
   changeset is an idempotent no-op.
2. **Roles.** Create `nexus_admin` (schema owner, NOSUPERUSER) and `nexus_svc`
   (NOSUPERUSER NOBYPASSRLS LOGIN data role). The changelog's grant changesets
   give `nexus_svc` its DML rights automatically during the first run.

## Tuning Parameters

The `[tuning]` section in `~/.config/nexus/config.yml` controls search scoring, chunking, and timeout behavior. All values have sensible defaults — only override what you need.

```yaml
tuning:
  scoring:
    vector_weight: 0.7            # weight for vector similarity in hybrid scoring
    frecency_weight: 0.3          # weight for git frecency in hybrid scoring
    file_size_threshold: 30       # chunks — files larger than this are down-ranked
  frecency:
    decay_rate: 0.01              # frecency decay rate (higher = faster decay)
  chunking:
    code_chunk_lines: 150         # target lines per code chunk (fallback splitter)
    pdf_chunk_chars: 1500         # target chars per PDF chunk
  timeouts:
    git_log: 30                   # seconds — timeout for git log subprocess
    ripgrep: 10                   # seconds — timeout for ripgrep subprocess in hybrid search
```

| YAML path | Default | Description |
|-----|---------|-------------|
| `tuning.scoring.vector_weight` | `0.7` | Vector similarity weight in hybrid scoring formula |
| `tuning.scoring.frecency_weight` | `0.3` | Git frecency weight in hybrid scoring formula |
| `tuning.scoring.file_size_threshold` | `30` | Chunk count above which code files are down-ranked |
| `tuning.frecency.decay_rate` | `0.01` | Exponential decay rate for frecency scoring |
| `tuning.chunking.code_chunk_lines` | `150` | Target lines per code chunk (line-based fallback) |
| `tuning.chunking.pdf_chunk_chars` | `1500` | Target characters per PDF chunk |
| `tuning.timeouts.git_log` | `30` | Timeout (seconds) for `git log` subprocess |
| `tuning.timeouts.ripgrep` | `10` | Timeout (seconds) for `rg` subprocess in hybrid search |

These values are exposed as a `TuningConfig` dataclass in `nexus.config`. The search command, indexer, and scoring modules all read from this config — changes take effect on the next invocation without restarting anything.

## Heat-Weighted T2 Expiry

T2 memory entries use a heat-weighted effective TTL (RDR-057 Phase 2a):

```
effective_ttl = base_ttl * (1 + log(access_count + 1))
```

Highly-accessed entries survive longer than their nominal TTL. Unaccessed entries (`access_count=0`) expire at the base rate (`log(1) = 0`, so multiplier = 1). Every `memory_get` or `memory_search` hit increments `access_count` and updates `last_accessed`.

| access_count | Multiplier | Effective TTL (base 30 days) |
|--------------|------------|------------------------------|
| 0 | 1.00 | 30 days |
| 1 | 1.69 | ~51 days |
| 5 | 2.79 | ~84 days |
| 10 | 3.40 | ~102 days |
| 50 | 4.93 | ~148 days |

**Note**: This differs from the paper (Memory in the LLM Era) which uses division for relevance-decay. Nexus uses multiplication for heat-based survival — entries agents keep touching stick around longer. If you need strict time-bounded expiry regardless of access, use `ttl=None` (permanent) and explicit `memory_delete` instead.

Periodic purge runs via `T2Database.expire(relevance_log_days=90)`, which also purges the `relevance_log` telemetry table (RDR-061 E2) of entries older than 90 days.

## Verification

Opt-in mechanical enforcement hooks that catch common agent failure modes: premature session closure and premature bead closure.

```yaml
# .nexus.yml
verification:
  on_stop: false          # Enable Stop hook — checks on session end (default: false)
  on_close: false         # Enable bd-close gate — checks before closing a bead (default: false)
  test_command: ""        # Auto-detected if omitted (see table below)
  lint_command: ""        # Optional linter (currently advisory only)
  test_timeout: 120       # Seconds; 0 = no timeout (default: 120)
```

### Activation

**Both `on_stop` and `on_close` default to `false`.** A `verification:` section without either flag set does nothing. Projects opt in explicitly:

```yaml
verification:
  on_stop: true    # Enable session-end checks
  on_close: true   # Enable bead-close gate
```

### Auto-Detection

If `test_command` is omitted but `on_stop` or `on_close` is `true`, the hook auto-detects from project marker files (first match wins):

| Marker file | Test command |
|---|---|
| `pom.xml` | `mvn test` |
| `build.gradle` / `build.gradle.kts` | `./gradlew test` |
| `pyproject.toml` | `uv run pytest` |
| `package.json` | `npm test` |
| `Cargo.toml` | `cargo test` |
| `Makefile` | `make test` |
| `go.mod` | `go test ./...` |

If no marker file is found and no command is configured, the test check is skipped with an advisory: "No test command configured or detected."

### Behavior Reference

**Stop hook** (`on_stop: true`): Fires when the agent ends a session. Advisory only — warns about uncommitted git changes and open beads (`bd list --status=in_progress`) but never blocks. The agent sees the warnings and can choose to address them.

**bd-close gate** (`on_close: true`): Fires before `bd close` or `bd done` commands. Advisory only — warns when no review marker found in T1 scratch (from `/conexus:review-code`). Never blocks.

## File Locations

| File | Purpose |
|---|---|
| `~/.config/nexus/config.yml` | Global config and credentials |
| `~/.local/share/nexus/chroma/` | Legacy ChromaDB store — migration source only as of 6.0 (`nx guided-upgrade`); not live T3 data |
| `~/.config/nexus/postgres/` | The nx-provisioned Postgres cluster the service serves T3 from (local `nx init`) |
| `~/.config/nexus/memory.db` | T2 SQLite database |
| `~/.config/nexus/catalog/.catalog.db` | Catalog: canonical repo→collection registration, documents, and links (`nx index repo` writes here). Replaced `repos.json` as the source of truth in 5.4.0 (RDR-137); a one-shot migration on `nx upgrade` folds any legacy `repos.json` into the catalog and removes it. |
| `~/.config/nexus/sessions/` | JSON session records (T1 server address, session ID, `created_at`, `tmpdir`) + `session.lock` |
| `~/.config/nexus/index.log` | Background indexing log (written by git hooks) |
| `~/.config/nexus/cli_lockstep_marker` | Last CLI version confirmed in lockstep with the plugin (RDR-143). Written by the version-lockstep SessionStart hook only after a confirmed upgrade; absence or a stale value triggers a re-nudge next session. |
| `.nexus.yml` | Per-repo config overrides |

## Logging

Central configuration: `src/nexus/logging_setup.py` — `configure_logging(mode, verbose)`.

### Entry Points

| Entry point | Mode | File handler | Notes |
|---|---|---|---|
| `nx` CLI | `cli` | None (stderr only) | WARNING default, DEBUG with `-v` |
| `nx-mcp` (core MCP) | `mcp` | `~/.config/nexus/logs/mcp.log` | RotatingFileHandler 10 MB × 5 |
| `nx-mcp-catalog` | `mcp` | `~/.config/nexus/logs/mcp.log` | Shares log with core MCP |
| `nx console` | `console` | `~/.config/nexus/logs/console.log` | RotatingFileHandler 10 MB × 5 |
| T2 daemon (`nx daemon t2 start`) | `t2_daemon` | `<config_dir>/logs/t2_daemon.log` | RotatingFileHandler 10 MB × 5; honours `--config-dir` |

### Log Files

| File | Writer | Format |
|---|---|---|
| `~/.config/nexus/index.log` | Git post-commit hook (`nx index repo`) | Unstructured, ~60 MB observed |
| `~/.config/nexus/dolt-server.log` | Dolt server process | Dolt native format |
| `~/.config/nexus/logs/mcp.log` | MCP servers (via `logging_setup`) | `%(asctime)s %(name)s %(levelname)s %(message)s` |
| `~/.config/nexus/logs/console.log` | Console server (via `logging_setup`) | Same as above |
| `~/.config/nexus/logs/t2_daemon.log` | T2 daemon (via `logging_setup`) | Same as above; records `t2_daemon_started` / `t2_daemon_stop_requested` / `t2_daemon_stopped` lifecycle + crashes |

### Suppressed Loggers

`httpx`, `httpcore`, `chromadb.telemetry`, `opentelemetry` — forced to WARNING in all modes.

### Special Cases

- `search_cmd.py` overrides structlog to ERROR level when producing machine-parseable output (`--json`, `--vimgrep`, `--files`, `--compact`).
- The indexer hook script redirects stdout/stderr to `index.log` directly in the shell — not managed by `logging_setup`.
