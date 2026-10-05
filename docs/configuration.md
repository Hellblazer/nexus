# Nexus Configuration Reference

## Config Hierarchy

Four levels, highest priority wins:

1. **Environment variables** (`NX_*`) — highest priority
2. **Per-repo**: `.nexus.yml` in repo root (gitignored by default)
3. **Global**: `~/.config/nexus/config.yml`
4. **Built-in defaults** — lowest priority

Each level is deep-merged, with higher-priority values winning.

## Local Mode

Nexus auto-detects local mode when cloud credentials are absent. The recommended setup is `uv tool install conexus --python 3.12 && nx init`: `nx init` provisions the local service stack and sets its embedder to bge-768 by default, fetching the ONNX the Java service reads (see [nx init](cli-reference.md#nx-init)). The service can be switched to embed with Voyage instead — see [CLI reference § Local mode with Voyage](cli-reference.md#nx-init). It does **not** add the `[local]` extra — the RDR-144 embedder picker that did was removed at RDR-174 P1.3. Request the extra at install time instead, with `uv tool install "conexus[local]" --python 3.12`; `[local]` adds the Python-side bge-768 embedder used by the non-service local paths, while plain `conexus` uses the built-in 384-dim MiniLM. Upgrade with [`nx self install`](cli-reference.md#nx-self-install), which carries the extras recorded in the install receipt into the new generation; `uv tool upgrade conexus` and `uv tool install --reinstall conexus` do not touch a generation install.

| Env var | Default | Description |
|---|---|---|
| `NX_LOCAL` | (auto) | `1` = force local, `0` = force cloud, unset = auto-detect |
| `NX_LOCAL_CHROMA_PATH` | `~/.local/share/nexus/chroma` | Path to a legacy ChromaDB store. **Inert as of 7.0.0** (the migration reader was deleted with the `chromadb` dependency, RDR-155 P4b). The directory is a relic nothing reads, with no path back to that era (Sam, 2026-08-29); T3 serves from the Postgres+pgvector service. |
| `NEXUS_CATALOG_PATH` | `~/.config/nexus/catalog` | Override catalog git repo location |
| `NEXUS_CATALOG_ALLOW_CROSS_PROJECT` | unset | Set to `1` on the **client** to bypass the register-time cross-project source_uri guard, enforced engine-side (`CatalogRepository.deriveSourceUri`, nexus-e7cys). `HttpCatalogClient.register`/`.register_many` read this and forward it on the wire as `allow_cross_project` — the engine has no access to the client's environment. Emergency-only escape hatch for known-good recovery scripts that legitimately need to register rows across project boundaries; never the right answer for normal indexing. The engine logs `event=cross_project_source_uri_override_used` whenever the override is actually exercised |

**`config.yml` keys** (set by `nx init`, under the `local:` block in `~/.config/nexus/config.yml`):

| Key | Default | Description |
|---|---|---|
| `local.embed_model` | (auto-select) | The embedder `nx init` recorded (`BAAI/bge-base-en-v1.5` or `all-MiniLM-L6-v2`). Absent = legacy auto-select (bge if the `[local]` extra is importable, else MiniLM). A `voyage-code-3` / `voyage-context-3` value is also legal (nexus-umm29 opt-in) — it also needs `voyage_api_key` configured and the service restarted before the engine actually switches models; see [CLI reference § Local mode with Voyage](cli-reference.md#nx-init). |
| `local.fastembed_cache_path` | `~/.local/share/nexus/fastembed_cache` (XDG-aware) | Stable cache dir for the bge-768 model so it is not re-downloaded to a volatile `$TMPDIR` on every reboot. |

**Mode selection**: As of 6.0, managed-cloud mode activates when `NX_SERVICE_URL` (+ `NX_SERVICE_TOKEN`) is set — the client routes T3 through the hosted service (see [Managed-Cloud Credentials](#managed-cloud-credentials) below). Otherwise local mode is used. Set `NX_LOCAL=1` to force local mode even with service credentials present. (The legacy `CHROMA_API_KEY`/`VOYAGE_API_KEY` auto-detect predates the service substrate and applies only to pre-6.0 installs that have not migrated.)

**Embedding tiers**: Tier 0 (bundled MiniLM-L6-v2, 384d) is always available. Ask for tier 1 (bge-base-en-v1.5, 768d, better quality; downloads the model on first embed) when you install the CLI: `uv tool install "conexus[local]"`. The extra is recorded in the generation's install receipt and [`nx self install`](cli-reference.md#nx-self-install) threads it into every later generation, so an upgrade never drops it; to ADD it to an existing generation install, run `nx self install --extras local` (nexus-pffc4 — merges with the extras the install already has). (On a box still on the legacy uv-tool layout, `uv tool install --reinstall "conexus[local]"` still adds it; on a generation install that same command rebuilds the legacy uv tree over the nexus-owned shims instead.)

**Legacy ChromaDB store path**: Defaults to `$XDG_DATA_HOME/nexus/chroma` or `~/.local/share/nexus/chroma`, overridable with `NX_LOCAL_CHROMA_PATH`. **No longer read as of 7.0.0** — the migration reader was deleted with the `chromadb` dependency. The directory is a relic nothing reads, with no path back to that era (Sam, 2026-08-29) and no cleanup verb; live T3 serves from the Postgres+pgvector service.

**Switching embedders or modes**: Changing the embedding model (switching local↔cloud, *or* switching local tiers 384-dim MiniLM ↔ 768-dim bge) makes the existing vectors incompatible (different dimensions/space). On the next `nx index repo .` the staleness check detects the model change and re-embeds into **new** collections under the new model token. **It does NOT automatically delete or migrate the old collections**: they remain behind under the previous token and silently return no results (their dimension no longer matches the active embedder).

When you switch local tiers via `nx init` (the common 384 → bge-768 upgrade), `nx init` detects these stale collections and offers a safe, ordered migration (preview → double-confirm → reindex-first → delete-after-verify; old collections deleted only after the new ones are verified populated, so a failed reindex never loses data). `code__` and manual-note (`store_put`) collections are reported but never auto-deleted. Outside the `nx init` flow you can clean up manually: `nx doctor` flags the dimension mismatch, `nx collection reindex <name>` rebuilds one from source, and `nx collection delete <name>` removes an orphan.

## Managed-Cloud Credentials

As of 6.0, managed-cloud mode points `nx` at a hosted nexus service that owns its cloud Postgres + pgvector and embeds with Voyage server-side. The client credentials are the service URL and a bearer token. Both resolve **env first, then `config.yml`**: set them interactively with `nx config init` (or `nx config set service_url/service_token`), or export the env vars below — the environment always takes precedence:

| Env var | Required | Notes |
|---|---|---|
| `NX_SERVICE_URL` | No | Managed nexus service base URL. Defaults to `https://api.conexus-nexus.com`; override for a self-hosted or staging deployment. |
| `NX_SERVICE_TOKEN` | Cloud mode | Bearer token for the managed service. |

Export both in your shell profile or process manager. You do not supply a Voyage key in managed-cloud mode (the service owns it).

### Migration-source credentials — RETIRED as of 7.0.0

`CHROMA_API_KEY`, `CHROMA_DATABASE` and `CHROMA_TENANT` are **inert**. Setting them does nothing.

Through 6.x they were read by the ladder's substrate rung so `nx upgrade` could read an existing ChromaDB Cloud store as a migration source. RDR-155 P4b deleted the Chroma read client, the migration ETL and finally the `chromadb` dependency itself, so there is no code left that could consume them. They are documented here only so an operator who finds them in an old `config.yml` or shell profile knows they are dead rather than broken — they can be deleted.

**If you are still on a pre-migration install**, do not set these and expect an upgrade to work. Upgrading straight from a Chroma-era install into 7.0.0 is detected and refused with a loud two-hop redirect (`nexus.stranded_install`): migrate on a 6.x release first, which still ships the migration tool, then upgrade to 7.0.0. Run that 6.x migration against a local engine (stop any running local service, clear `NX_SERVICE_URL` and the `service_url` key in `config.yml`, and `export NX_LOCAL=1`, which is needed in addition because it does not by itself override `service_url`), never against a managed endpoint; reaching the managed cloud is a later hop with the current client ([Migration Runbook § Getting that data into the managed cloud](migration-runbook.md#getting-that-data-into-the-managed-cloud)). Frozen Chroma directories on disk are relics nothing reads, with no path back to that era (Sam, 2026-08-29) and no cleanup verb.

## Bibliographic enrichment (`nx enrich bib`)

| Env var | Required | Notes |
|---|---|---|
| `S2_API_KEY` | No | Semantic Scholar API key (100 req/s, vs 100/5min unauthenticated). Get one at https://www.semanticscholar.org/product/api#api-key |
| `OPENALEX_MAILTO` | No | Your email, for OpenAlex's polite pool (higher rate limit, no key needed). |

`nx enrich bib` fetches bibliographic metadata (year, venue, authors, citation count) for chunks in a collection. `--source auto` (the default) picks the backend: Semantic Scholar when `S2_API_KEY` is set, OpenAlex otherwise (nexus-57mk added the OpenAlex backend so a missing key no longer means unauthenticated rate-limited Semantic Scholar). Pass `--source s2` or `--source openalex` to pin one explicitly, or `--source dt` to gap-fill from DEVONthink's CrossRef resolver after the `auto` pass. See `nx enrich bib --help` for the full option list.

**Historical note (7.0.0: no longer operative).** `chroma_database` named the ChromaDB Cloud database the migration read from; all collection prefixes (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexisted in it, and `chroma_tenant` was inferred from the API key except in multi-workspace setups. Retained as provenance for the collection-naming scheme, which outlived the backend.

**Single-database architecture (history).** RDR-037 (2026-03-14) consolidated the legacy four-database layout (`{base}_code` / `{base}_docs` / `{base}_rdr` / `{base}_knowledge`) into a single database with collection prefixes. The transitional auto-detect probe was retired in 4.14.2 once the migration window closed.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | RETIRED (RDR-188): reranking runs server-side; the engine picks the model via `NX_RERANK_MODEL` in its environment. A set value emits a deprecation notice and is otherwise ignored |
| — (engine env) | `NX_HNSW_EF_SEARCH` | `200` | Engine-side serving floor for pgvector `hnsw.ef_search` on vector-ranked paths (engine ≥ 0.1.93, nexus-4ktfm). Effective ef is `clamp(max(floor, n_results), 1, 1000)`. Raises recall under shared-index cross-tenant crowding; higher values cost plain-search latency. Validated at engine boot — an out-of-range value refuses to start |
| — (engine env) | `NX_HNSW_MAX_SCAN_TUPLES` | `200000` | Engine-side `hnsw.max_scan_tuples` set (SET LOCAL) on every vector-ranked path (nexus-wbfpw.47; pgvector's own default is 20000). An iterative HNSW scan stops at this many tuples; at 98% correlated-dead chunks on a shared index the default lost recall (0.85-0.89 at recall@10), and this cap together with the memory budget below restored 1.000. Range 1000..100000000. It also raises the cost of a filtered search that cannot fill its LIMIT. Validated at engine boot; an out-of-range value refuses to start |
| — (engine env) | `NX_HNSW_SCAN_MEM_BUDGET_MB` | `16` | Fixed per-search memory budget for the same scan (nexus-wbfpw.47). The engine reads its role's effective `work_mem` at boot and sets `hnsw.scan_mem_multiplier = max(1, budget / work_mem)` (4 at the stock 4 MB `work_mem`; 1 at a managed cloud's 384 MB), and logs `work_mem`, the multiplier and the effective budget (`event=hnsw_scan_budget`). Range 1..4096. Validated at engine boot |
| — (engine env) | `NX_SEARCH_STATEMENT_TIMEOUT_MS` | `30000` | Engine-side `statement_timeout` on every vector-ranked path (engine ≥ 0.1.96, nexus-g17tf). A scan past the bound cancels with SQLSTATE 57014 instead of surviving its container and pinning xmin. Range 1..600000; `0` (Postgres for "disabled") is refused at engine boot. Sized to the edge's 30s budget: a longer bound leaves a backend burning CPU for a client that already gave up. |
| — (engine env) | `NX_SEARCH_FANOUT_CONCURRENCY` | `max(1, NX_POOL_SIZE / 2)` | Engine-side (nexus-tu8wp.1). How many collections ONE `POST /v1/vectors/search-per-collection` request searches at once; each is its own statement and holds a pooled connection while it runs. An override is clamped to `NX_POOL_SIZE`. This is a per-request figure: several requests at once are bounded by `NX_SEARCH_FANOUT_ARM_PERMITS` below, not by this. A blank value takes the default; a malformed or non-positive one takes the default and logs `search_fanout_setting_invalid`. |
| — (engine env) | `NX_SEARCH_FANOUT_ARM_PERMITS` | `max(1, NX_POOL_SIZE / 2)` | Engine-side (nexus-tu8wp.1). How many fan-out statements may be in flight across ALL `search-per-collection` requests on one pool. An arm takes a permit before it asks for an admission permit or a connection, so an arm that is waiting holds neither, and at least half the pool stays free for `/health`, writes and plain search however many fan-outs run at once. Clamped to `NX_POOL_SIZE`; a malformed or non-positive value takes the default. A request can still queue behind other requests' arms for a permit, but only for as long as the fan-out budget below allows. |
| — (engine env) | `NX_SEARCH_FANOUT_BUDGET_MS` | `20000` | Engine-side (nexus-tu8wp.1). The wall budget of one `search-per-collection` fan-out, counted from the request's arrival at the repository (so the query embed counts), kept under the public edge's 30 s upstream bound with the same 10 s margin as the nexus-99r7y budget. Collections whose search has not started when it is spent are returned in `per_collection[]` with `error_kind: fanout_budget_exhausted` instead of being run; a statement still running is bounded by what is left and, if it is cancelled by that bound, is reported the same way. Range 1..600000; a malformed or out-of-range value takes the default and logs `search_fanout_setting_invalid`. |
| — | `NX_SEARCH_PER_COLLECTION` | unset (route on) | Client-side kill switch for the per-collection search route (nexus-tu8wp.2). `0`, `false`, `off` or `no` (any case) makes every search take the batched `POST /v1/vectors/search` path instead of `POST /v1/vectors/search-per-collection`; any other value, or unset, leaves the route on. Read on every search, so a flip takes effect on the next one. Without it a defect in the route would need a client release once the pinned engine carries it. An engine or edge that does not serve the route already falls back by itself (remembered 10 minutes per process; a 500 for 60 s), so this switch is for the case where the route answers but answers wrongly. |
| — (engine env) | `NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS` | `30000` | Engine-side `statement_timeout` on the taxonomy assign transaction (`POST /v1/taxonomy/assignments/assign_from_chashes`, nexus-r0vkh). Sized to the client's own 30s per-request timeout; a call past it cancels with SQLSTATE 57014, the client records that batch on its taxonomy tripwire, and the index write it followed is unaffected. Range 1..600000; `0` refused at engine boot. |
| — (engine env) | `NX_OWNERLESS_WRITE_MODE` | unset = `log-only` | Engine-side (RDR-223 Phase 3 Step 2, nexus-z0o2p.24). What `POST /v1/vectors/upsert-chunks` and `/store-put` do with a chash that has no live manifest row in the collection. Accepted values: `enforce` (refuse the whole request with 422, `reason: ownerless_chunk_write`, naming `/v1/catalog/manifest/write_many` and `/append`) and `log-only` (write as before, log `ownerless_chunk_write_would_refuse` once per route, tenant and collection per minute with the request's `User-Agent` and `X-Nexus-Client-Version`, and count it). **Unset or blank means `log-only`; only an explicit `enforce` enforces.** Any other value refuses to start the engine. The local engine launch (`nx daemon service start`) sets `enforce` itself unless the variable already carries a non-blank value: **at the local launcher a blank value counts as unset (enforce), while the raw engine parses blank as log-only**, so an empty `NX_OWNERLESS_WRITE_MODE=` still enforces on a local install and does not on an engine you start by hand. The log line is a SAMPLE of writers, never a complete list: one line per route, tenant and collection per minute, naming the first unowned chunk only; the key table holds 10,000 live keys, past that new keys share ONE overflow bucket (one line a minute, its `suppressed_since_last` pooled across keys, so a quiet writer can go unlogged for a window), and under key churn a minute can carry up to 10,001 lines. The counters are the complete signal: decide the flip to `enforce` from the delta of `ownerless_writes_would_refuse_total` over a window as long as the longest writer cadence (nexus-z0o2p.40), not from the log. In `log-only`, a request whose chashes the pre-embed check already reported counts once, and the in-transaction recheck is skipped for it: a chash in the same request that loses its owner during the embed is not counted again. The log line carries tenant content: the first unowned chunk's `source_path`, `title` and `source_agent` (a URL value loses its userinfo, query and fragment), the collection name, `User-Agent` and `X-Nexus-Client-Version`, each cleaned of control characters and quoted or delimited so it cannot forge a field; never `source_uri` or chunk text. Its retention is the deployment's log retention. `GET /v1/status` reports `ownerless_write_mode`, `ownerless_writes_refused_total` and `ownerless_writes_would_refuse_total`. |
| — (engine env) | `NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS` | `5000` | Engine-side `lock_timeout` on the same transaction (nexus-r0vkh). Concurrent assign calls queue on `nexus.topics` row locks behind the first; a queued call now fails with SQLSTATE 55P03 at this bound and returns its pool connection instead of holding it for the head's lifetime (2026-09-16: nine of ten pool connections sat 11 minutes behind one 782s call). Range 1..600000; `0` refused at engine boot. |
| `install.mode` | — | (stamped by `nx init`) | Explicit mode record: `local` or `managed`. Written at init/onboarding; `is_local_mode()` reads it ahead of artifact inference (a configured `service_url` still wins over a stale `local` record, loudly) |
| `client.host` | `NX_CLIENT_HOST` | `localhost` | Legacy ChromaDB host override; no-op as of 6.0 (the managed/local service URL is `NX_SERVICE_URL`). |
| `pdf.extractor` | — | `auto` | PDF extraction backend: `auto`, `docling`, or `mineru`. Set globally with `nx config set pdf.extractor=mineru` |
| `pdf.mineru_server_url` | — | `http://127.0.0.1:8010` | MinerU API server URL. Auto-updated when `nx mineru start` binds a port |
| `pdf.mineru_table_enable` | — | `true` | Table extraction in MinerU. Off, every table becomes an image reference and its values are not indexed; the chunk then carries a `[Table N not extracted as text; values not indexed]` marker. Set `false` only for corpora with no tables where the table models' memory matters |
| `pdf.mineru_page_batch` | — | `1` | Pages per MinerU request. Increase for faster throughput at the cost of memory |
| `voyageai.read_timeout_seconds` | `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` | `120` | Request timeout (seconds) for Voyage AI API calls. Increase for large PDF indexing |
| `search.hybrid_default` | — | `false` | Default hybrid-scoring mode for `nx search`: blends git frecency into the score for code corpora (0.7*vector + 0.3*frecency). Set `true` to always blend |
| `search.hnsw_ef` | — | `256` | Legacy client-side HNSW tuning key, inert in production. Every current install (local or cloud) serves T3 through the nexus-service over pgvector, which tunes HNSW server-side — that knob is `NX_HNSW_EF_SEARCH` above, not this key. This value only still applies to a Chroma-backed test double; there is no live SPANN or ChromaDB Cloud path left to be "ignored in" |
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
  mineru_table_enable: true     # table extraction (default: true; off = tables indexed as markers only)
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

## Aspects

Which `docs__` collections get aspect extraction (nexus-kk4ut). `knowledge__` and `rdr__` collections are always extracted; `docs__` collections are extracted only when opted in, because every document costs an LLM call each time it changes.

```yaml
aspects:
  docs_collections: ["docs__1-29__*"]    # glob patterns; default [] (none)
```

| Key | Default | Description |
|-----|---------|-------------|
| `docs_collections` | `[]` | Glob patterns naming the `docs__` collections to extract. A matching collection's prose files (`.md`, `.markdown`, `.mdx`, `.rst`, `.adoc`, `.asciidoc`, `.org`, `.txt`) get `general-prose-v1` (summary, key decisions, entities, open questions); its other files (fixtures, word lists, graphs) are skipped. Also accepts a comma-separated string, so `nx config set aspects.docs_collections "docs__1-29__*,docs__1-41__*"` works. Documents already indexed are not queued retroactively: run `nx enrich aspects <collection>` after opting in. Opting a collection back OUT stops new extraction but does not delete aspect rows already written; `nx enrich delete <collection> <source_path>` removes one row, and `nx enrich delete <collection> --all` (nexus-3foc9) removes every row in the collection (dry-run by default; pass `--no-dry-run --yes` to actually delete). |

**Two homes for the same decision (nexus-l46pu, follow-up to nexus-kk4ut).** `docs_collections` above is per MACHINE — this config file — while `document_aspects` rows and the aspect queue are tenant-wide in the engine, so two machines indexing the same shared `docs__` collection with different local config used to give partial, machine-dependent coverage. `nx collection aspects <name> --enable`/`--disable` (see [`docs/cli-reference.md`](cli-reference.md#nx-collection)) sets the SAME opt-in on the engine's `catalog_collections.aspects_enabled` row instead — tenant-wide, read by every machine indexing the collection.

**The engine is AUTHORITATIVE** (round-2 critic decision, T2 `critique-nexus-l46pu-tenant-wide-aspects-enabled` item 1): the moment a collection's engine attribute carries an opinion — set explicitly `true` or `false` by `--enable`/`--disable`/`--from-config` — every machine reading it agrees, and the local `docs_collections` list is NOT consulted for that collection any more, even if it still names a matching pattern. Keeping "local wins" would have kept the exact cross-machine drift this bead exists to close: a stale local entry on one machine could silently re-override a value another machine had already synced to the engine. The local list is consulted ONLY as a fallback for a collection the engine has no opinion on at all — the column (`aspects_enabled BOOLEAN NULL`, catalog-040) is `NULL` on every row until an operator explicitly runs one of the three write verbs above, so "no opinion" covers a fresh install, an engine older than catalog-040 that never sends the key at all, AND an ordinary, otherwise-untouched row on a perfectly current engine that nobody has ever synced — the same fallback in every case. This is a deliberate round-2 fix (T2 `critique-nexus-l46pu-round2-2026-09-27` Finding A): an earlier cut of this column defaulted new AND existing rows to a plain `false`, which is indistinguishable from an explicit `--disable` and would have silently overridden every machine's local opt-in the moment a tenant's engine crossed the migration — `NULL` keeps "nobody has set this" a distinct, third value on the wire.

`nx collection aspects --from-config [--dry-run]` is the migration path off the local-only list: it enables the engine attribute for every registered `docs__` collection this machine's `docs_collections` already matches, prints what it changed, and never disables anything (the local list is opt-in only, so there is nothing in it that means "turn off"). `nx doctor`'s default sweep includes a `docs-aspects-config` row that warns when a local match has not been synced this way, naming the remedy; it is not-applicable on a machine with no `docs_collections` entries. Prefer the engine attribute for anything shared across machines — run `--from-config` once you have one — and keep the local list only for a genuinely local, one-off experiment on a collection nobody else indexes.

Every write path — `--enable`/`--disable`, and `--from-config` when it would actually change something — is a TENANT-WIDE setting: every machine indexing the collection sees it, and enabling it means an LLM call per changed prose document from then on. Each prompts for confirmation before writing (`--yes`/`-y` skips the prompt, for scripts); `--dry-run` never prompts, since it never writes. A non-interactive run without `--yes` refuses rather than hanging on a prompt nothing will answer.

## Daemon environment variables

T2 and T3 both route through the single native `nexus-service` (`nx daemon service`, RDR-152/RDR-155), discovered via `~/.config/nexus/storage_service_addr.<uid>` and overridden with `NX_SERVICE_URL` (see [Managed-Cloud Credentials](#managed-cloud-credentials)). This unified the earlier RDR-120 (conexus 4.34.0) split, where the CLI and MCP server routed T2 through a separate T2 daemon publishing `~/.config/nexus/t2_addr.<uid>`; that daemon and discovery file are retired (RDR-158 — see the `NX_T2_ADDR`/`NX_T2_SOCK` note below). Clients honour these env-var overrides:

| Variable | Effect | Default |
|----------|--------|---------|
| `NX_SERVICE_URL` | Full base URL of the nexus-service (T3 vectors over `/v1/vectors`). Used by dev containers and managed-cloud clients. | `storage_service_addr.<uid>` lease, then `https://api.conexus-nexus.com` |
| `NX_SERVICE_TOKEN` | Bearer token for the nexus-service. | local: from `pg_credentials`; managed: user-supplied |
| `NX_STORAGE_BACKEND` | Storage-backend env guard (RDR-152/158). `service` (the default and only backend) routes T2 stores + T3 vectors through the Java/Postgres nexus-service. `sqlite` is RETIRED (RDR-158 P3): setting it is a hard error carrying the stranded-install redirect — the SQLite stores were deleted; to migrate old local data, install the last migration-capable 6.x release, run `nx upgrade` there against a LOCAL engine (stop any running local service, clear `NX_SERVICE_URL` and the `service_url` key in `config.yml`, `export NX_LOCAL=1` (needed in addition; it does not by itself override `service_url`), keep `NX_VOYAGE_API_KEY` set for Voyage-embedded data; see [Migration Runbook § Installs that predate Postgres](migration-runbook.md#installs-that-predate-postgres)), then upgrade back. | `service` |
| `NX_STORAGE_BACKEND_<STORE>` | Per-store override of `NX_STORAGE_BACKEND`, taking precedence over the global value. Known `<STORE>` suffixes: `T1`, `CATALOG`, `VECTORS`, `TAXONOMY`, `ASPECT_QUEUE` (e.g. `NX_STORAGE_BACKEND_VECTORS=service`). `service` is the only accepted value; `=sqlite` hard-errors (RDR-158 P3). | inherits `NX_STORAGE_BACKEND` |
| `NX_LOCAL` | Force local mode (local nexus-service, bge-768 by default, or Voyage when `NX_VOYAGE_API_KEY` reaches the service — nexus-umm29) even when cloud credentials exist. | unset (cloud mode if credentials present) |

> Note: the retired `nx daemon t3` ChromaDB path and its `NX_T3_ADDR` override no longer route T3 serving; T3 traffic goes to the nexus-service via `NX_SERVICE_URL`.

> Note: `NX_T2_ADDR` and `NX_T2_SOCK` are **gone**, not deprecated — nothing
> reads them. They addressed the SQLite T2 daemon, which is retired along with
> its discovery file (`t2_addr.<uid>`) and the whole `nx daemon t2` verb group.
> T2 is served by the nexus-service, so a dev container reaches it through
> `NX_SERVICE_URL` like every other tier. See
> [Container Integration](container-integration.md) for the transport matrix.

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
   (NOSUPERUSER NOBYPASSRLS NOINHERIT LOGIN data role). `NOINHERIT` is
   REQUIRED, not optional decoration — a bring-your-own-Postgres cluster has
   no `bootstrap_superuser` to self-heal it later (unlike the local bundle's
   `_backfill_svc_noinherit`), so a DBA who leaves it off here bakes in the
   INHERIT-default divergence nexus-v80f2 fixed everywhere else; see the
   NOINHERIT discussion below for why it matters. The changelog's grant
   changesets give `nexus_svc` its DML rights automatically during the first
   run.
   Optionally create `nexus_diag` (NOSUPERUSER NOCREATEDB NOCREATEROLE
   BYPASSRLS LOGIN) — the client-side diagnostic role for the pre-upgrade
   chash-poison probe (`nx doctor`, the `install-binary` gate); without it
   that check degrades to a loud WARN, never a false clean. Note the client
   probe is **local-only by design** (nexus-y3wuu): it reaches only a local
   Postgres via the local `pg_credentials` file, so on a remote/managed
   store the role serves server-side diagnostics run with your own
   credentials.
   Also run `GRANT pg_monitor TO nexus_admin WITH ADMIN OPTION;` (nexus-hzhgl,
   RDR-191 Phase 3/4 pre-flight) — PostgreSQL only lets a role grant
   membership in another role it already holds WITH ADMIN OPTION (or as
   superuser), and `nexus_admin` is neither by default, so without this
   one-time superuser step the changelog's `grants-004-monitor-wal-visibility`
   changeset (which grants `pg_monitor` onward to `nexus_svc` for WAL-
   retention visibility — `pg_ls_waldir()` / `pg_stat_*`, **not** filesystem
   free space) fails loud on every migration run.

   **The grant alone does not necessarily make the privilege usable**
   (nexus-bb5c8) — it depends on `nexus_svc`'s INHERIT attribute.
   `NOINHERIT` is the posture in **every** mode (nexus-v80f2, 2026-08-15):
   the managed conexus cloud deployment's `nexus_svc` is `NOINHERIT`
   (measured live) — a deliberate posture, not an oversight, so that its
   OTHER role memberships never become ambient on every connection — and
   local `nx init` provisioning now creates `nexus_svc` `NOINHERIT` too
   (`src/nexus/db/pg_provision.py`'s `_create_roles`, converged on an
   already-provisioned install via `_backfill_svc_noinherit`), matching
   `role-001-nexus-svc.xml`'s fallback bootstrap, which always has. A
   bring-your-own-Postgres deployment that provisions `nexus_svc`
   `NOINHERIT` per this section gets the same behavior: a plain session
   gets `permission denied` from `pg_ls_waldir()` even after this grant,
   until it issues `SET ROLE pg_monitor` first (and, optionally,
   `RESET ROLE` after) — the same PostgreSQL behavior any NOINHERIT
   membership has everywhere, not a defect in this changeset. Product code
   never needs a bring-your-own DBA to do anything about this either way:
   `src/nexus/db/svc_monitor.py` is the one place a `nexus_svc` session
   performs that escalation — unconditionally, so it is correct whether
   the role is NOINHERIT or INHERIT — and `nx doctor
   --check-wal-retention` samples retained WAL through it, reporting an
   explicit `UNMEASURED` (never a false clean) if the grant above was
   never applied.
3. **Diagnostic counts view (RDR-182 Amendment A6).** After the first
   migration run has created the chunk tables, create the counts view and
   grant it to `nexus_diag` (the local bundle's provisioning does this
   automatically; bring-your-own-Postgres DBAs run it once — or let the
   engine's own `taxonomy-011-8` Liquibase changeset create it for you on
   its next boot, since 2026-08-17). `WITH (security_invoker = true)`
   evaluates row-level security against the QUERYING role rather than the
   view's owner (PG15+), so `nexus_diag` (`LOGIN ... BYPASSRLS`) sees every
   tenant's rows through this view regardless of who created it — the view
   no longer NEEDS a superuser owner the way it did before this option
   existed, though creating it as the superuser (as below) remains a safe,
   supported path too:

   ```sql
   CREATE OR REPLACE VIEW nexus.diag_chash_conformance WITH (security_invoker = true) AS
   SELECT 'nexus.chunks' AS table_name, count(*) AS non_conformant FROM nexus.chunks WHERE octet_length(chash) <> 32
   UNION ALL
   SELECT 'nexus.catalog_document_chunks' AS table_name, count(*) AS non_conformant FROM nexus.catalog_document_chunks WHERE octet_length(chash) <> 32
   UNION ALL
   SELECT 'nexus.topic_assignments' AS table_name, count(*) AS non_conformant FROM nexus.topic_assignments t WHERE NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.chash = t.doc_id)
   UNION ALL
   SELECT 'nexus.frecency' AS table_name, count(*) AS non_conformant FROM nexus.frecency t WHERE t.chunk_id ~ '^[0-9a-f]+$' AND length(t.chunk_id) % 2 = 0 AND NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.chash = decode(t.chunk_id, 'hex'))
   UNION ALL
   SELECT 'nexus.relevance_log' AS table_name, count(*) AS non_conformant FROM nexus.relevance_log t WHERE t.chunk_id ~ '^[0-9a-f]+$' AND length(t.chunk_id) % 2 = 0 AND NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.chash = decode(t.chunk_id, 'hex'));
   GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag;
   ```

   Once the view exists, the engine's `grants-nexus-diag-2` changeset revokes
   `nexus_diag`'s direct table SELECT on its next boot — the diagnostic role
   then reads counts by construction, never row content. (This SQL is
   generated from `nexus.db.chash_tables.CHASH_BEARING_TABLES`; a drift test
   pins this rendered copy to the generator.) DBAs who created an earlier
   view generation: re-run the `CREATE OR REPLACE` above once — nexus-z5j0t
   added the three legacy-debt legs (`topic_assignments.doc_id`,
   `frecency.chunk_id`, `relevance_log.chunk_id`; observed-only, they do not
   gate upgrades), and RDR-187 (nexus-piwya.5) retired the
   `nexus.chash_index` leg (the router table is being dropped; the gate
   filters by table_name, so an older view with the extra leg still
   satisfies it). Until re-created, debt counts report as unknown, never as
   clean.

## Tuning Parameters

The `[tuning]` section in `~/.config/nexus/config.yml` controls search scoring, chunking, and timeout behavior. All values have sensible defaults — only override what you need.

```yaml
tuning:
  scoring:
    vector_weight: 0.7            # weight for vector similarity in hybrid scoring
    frecency_weight: 0.3          # weight for git frecency in hybrid scoring
    file_size_threshold: 30       # accepted, ignored as of nexus-0bmhd — see table below
  frecency:
    decay_rate: 0.01              # frecency decay rate (higher = faster decay)
  chunking:
    code_chunk_lines: 150         # target lines per code chunk (fallback splitter)
    pdf_chunk_chars: 1500         # target chars per PDF chunk
  timeouts:
    git_log: 30                   # seconds — timeout for git log subprocess
```

| YAML path | Default | Description |
|-----|---------|-------------|
| `tuning.scoring.vector_weight` | `0.7` | Vector similarity weight in hybrid scoring formula |
| `tuning.scoring.frecency_weight` | `0.3` | Git frecency weight in hybrid scoring formula |
| `tuning.scoring.file_size_threshold` | `30` | Accepted, ignored as of nexus-0bmhd — RDR-006's chunk-count scoring penalty was superseded by a render-layer file-diversity cap (`search_engine.apply_file_diversity_cap`); the key is retained for config-file back-compat but no longer read |
| `tuning.frecency.decay_rate` | `0.01` | Exponential decay rate for frecency scoring |
| `tuning.chunking.code_chunk_lines` | `150` | Target lines per code chunk (line-based fallback) |
| `tuning.chunking.pdf_chunk_chars` | `1500` | Target characters per PDF chunk |
| `tuning.timeouts.git_log` | `30` | Timeout (seconds) for `git log` subprocess |

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

Expiry quarantines; it does not delete (RDR-207). `T2Database.expire(relevance_log_days=90)`, which the session-end hook and `nx memory expire` run, hides every entry past its effective TTL from get, search and list, and keeps the row. `nx memory reap` deletes a quarantined entry only once a rollup summary covers it, and `nx memory restore ID` brings one back as permanent; see [nx memory](cli-reference.md#nx-memory). The same call also purges the `relevance_log` telemetry table (RDR-061 E2) of entries older than 90 days, and those rows are deleted.

## File Locations

| File | Purpose |
|---|---|
| `~/.config/nexus/config.yml` | Global config and credentials |
| `~/.local/share/nexus/chroma/` | Legacy ChromaDB store — migration source only as of 6.0 (read by `nx upgrade`'s substrate rung); not live T3 data |
| `~/.config/nexus/postgres/` | The nx-provisioned Postgres cluster the service serves T3 from (local `nx init`) |
| `~/.config/nexus/memory.db` | **Deleted (RDR-158 P4).** Historical: the pre-migration T2 SQLite database — a frozen migration source only, for installs that have not yet moved off it. Live T2 is a Postgres table set served by `nexus-service`; there is no client-local T2 file. See [Storage Tiers § T2](storage-tiers.md#t2----memory-bank). |
| `~/.config/nexus/catalog/.catalog.db` | **Deleted (RDR-158 P4).** Historical: the local SQLite catalog (replaced `repos.json` as the source of truth in 5.4.0, RDR-137). Live catalog data is Postgres-backed, served by `nexus-service` via `HttpCatalogClient` — see [Document Catalog](catalog.md). |
| `~/.config/nexus/sessions/` | JSON session records (T1 server address, session ID, `created_at`, `tmpdir`) + `session.lock` |
| `~/.config/nexus/index.log` | Background indexing log (written by git hooks) |
| `~/.config/nexus/cli_lockstep_marker` | Last CLI version confirmed in lockstep with the plugin (RDR-143). Written by the version-lockstep SessionStart hook only after a confirmed upgrade; absence or a stale value triggers a re-nudge next session. |
| `~/.config/nexus/lockstep.log` | Durable, always-on (never gated behind `NX_HOOK_DEBUG`) append-only log of every RDR-143 lockstep venv-swap attempt — one `lockstep_upgrade_started` line and one `lockstep_upgrade_result` line (outcome: `success` / `uv_upgrade_failed` / `nx_upgrade_failed` / `version_still_mismatched`) per attempt (nexus-otnvr item 4: the detached action's own stdout/stderr are DEVNULL'd by the dispatching hook, so this file is the only durable record that a background swap happened at all). Capped at ~1MB with a single-generation manual rotate to `lockstep.log.1` (a plain `Path.replace()`, not `logging.handlers.RotatingFileHandler` — kept minimal-footprint like the rest of this bare-interpreter hook, which also cannot import the `nexus` package itself). |
| `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/` | The MiniLM embedding model, downloaded once and verified by sha256; the engine's `OnnxEmbedder` reads the same artifact. `NX_MINILM_CACHE_DIR` replaces the `~/.cache/chroma/onnx_models` root. The unit suite sets it to the real cache, so a test that moves HOME does not download the model again. |
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

### Log Files

| File | Writer | Format |
|---|---|---|
| `~/.config/nexus/index.log` | Git post-commit hook (`nx index repo`) | Unstructured, ~60 MB observed |
| `~/.config/nexus/dolt-server.log` | Dolt server process | Dolt native format |
| `~/.config/nexus/logs/mcp.log` | MCP servers (via `logging_setup`) | `%(asctime)s %(name)s %(levelname)s %(message)s` |
| `~/.config/nexus/logs/console.log` | Console server (via `logging_setup`) | Same as above |

### Suppressed Loggers

`httpx`, `httpcore`, `chromadb.telemetry`, `opentelemetry` — forced to WARNING in all modes.

### Special Cases

- `search_cmd.py` overrides structlog to ERROR level when producing machine-parseable output (`--json`, `--vimgrep`, `--files`, `--compact`).
- The indexer hook script redirects stdout/stderr to `index.log` directly in the shell — not managed by `logging_setup`.
