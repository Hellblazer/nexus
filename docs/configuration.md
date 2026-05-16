# Nexus Configuration Reference

## Config Hierarchy

Four levels, highest priority wins:

1. **Environment variables** (`NX_*`) — highest priority
2. **Per-repo**: `.nexus.yml` in repo root (gitignored by default)
3. **Global**: `~/.config/nexus/config.yml`
4. **Built-in defaults** — lowest priority

Each level is deep-merged, with higher-priority values winning.

## Local Mode

Nexus auto-detects local mode when cloud credentials are absent. No configuration needed — just `uv tool install conexus && nx index repo .`.

| Env var | Default | Description |
|---|---|---|
| `NX_LOCAL` | (auto) | `1` = force local, `0` = force cloud, unset = auto-detect |
| `NX_LOCAL_CHROMA_PATH` | `~/.local/share/nexus/chroma` | Override local ChromaDB storage path |
| `NX_LOCAL_EMBED_MODEL` | (auto) | Force a specific local embedding model name |
| `NEXUS_CATALOG_PATH` | `~/.config/nexus/catalog` | Override catalog git repo location |
| `NEXUS_CATALOG_ALLOW_CROSS_PROJECT` | unset | Set to `1` to bypass the register-time cross-project source_uri guard. Emergency-only escape hatch for known-good recovery scripts that legitimately need to register rows across project boundaries; never the right answer for normal indexing |

**Auto-detection**: When either `CHROMA_API_KEY` or `VOYAGE_API_KEY` is absent, local mode activates — both are required for cloud mode. Set `NX_LOCAL=1` to force local mode even with cloud credentials.

**Embedding tiers**: Tier 0 (bundled MiniLM-L6-v2, 384d) is always available. Install with `uv tool install conexus --with "conexus[local]" --force` for tier 1 (bge-base-en-v1.5, 768d, better quality).

**Storage path**: Defaults to `$XDG_DATA_HOME/nexus/chroma` or `~/.local/share/nexus/chroma`. Override with `NX_LOCAL_CHROMA_PATH`.

**Switching modes**: Changing between local and cloud mode triggers automatic re-indexing on the next `nx index repo .` (embedding model mismatch detected by staleness check). Local and cloud embeddings are incompatible — there is no automatic migration.

## Cloud Credentials

| Config key | Env var | Required | Notes |
|---|---|---|---|
| `chroma_api_key` | `CHROMA_API_KEY` | Cloud mode | ChromaDB Cloud API key |
| `chroma_database` | `CHROMA_DATABASE` | Cloud mode | ChromaDB Cloud database name (e.g. `nexus`) |
| `voyage_api_key` | `VOYAGE_API_KEY` | Cloud mode | Voyage AI embeddings key |
| `chroma_tenant` | `CHROMA_TENANT` | No | Auto-inferred from API key; only needed for multi-workspace setups |

Set via `nx config init` (wizard) or `nx config set KEY VALUE`. Stored in `~/.config/nexus/config.yml`.

## Semantic Scholar (Enrichment)

| Env var | Required | Notes |
|---|---|---|
| `S2_API_KEY` | No | Free API key for 100 req/s (vs 100/5min unauthenticated). Get one at https://www.semanticscholar.org/product/api#api-key |

Used by `nx enrich bib` to fetch bibliographic metadata (year, venue, authors, citation count). Without the key, enrichment works but is ~50x slower due to rate limiting.

**`chroma_database` is the database name.** Nexus connects to this single database on ChromaDB Cloud. All collection prefixes (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexist in it. Use `nx doctor` to check its status.

**`chroma_tenant` is optional.** The ChromaDB `CloudClient` infers the tenant UUID directly from your API key. You only need to set it explicitly if you belong to multiple Chroma Cloud workspaces.

**Database creation is automatic.** `nx config init` provisions the database on Chroma Cloud using your API key — no dashboard visit required. If provisioning fails (e.g., plan restrictions), create the database manually in the Chroma Cloud dashboard.

**Single-database architecture.** RDR-037 (2026-03-14) consolidated the legacy four-database layout (`{base}_code` / `{base}_docs` / `{base}_rdr` / `{base}_knowledge`) into a single database with collection prefixes. The transitional auto-detect probe was retired in 4.14.2 once the migration window closed. Anyone still on the legacy layout should pin `conexus<4.14.2`, run the export documented in the 4.x.0 CHANGELOG entries, then upgrade.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | Voyage reranker for multi-corpus merge |
| `client.host` | `NX_CLIENT_HOST` | `localhost` | Override ChromaDB host URL |
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
  local_exclude_collections: ["code__*"] # Skip code collections in local mode (MiniLM is poor for code)
  collection_prefixes: [docs, code, knowledge, rdr]  # Prefixes recognized by nx taxonomy validate-refs (RDR-081)
```

| Key | Default | Description |
|-----|---------|-------------|
| `auto_label` | `true` | Auto-label topics with Claude haiku after discover. Requires `claude` CLI on PATH. Set `false` to keep c-TF-IDF labels. |
| `local_exclude_collections` | `["code__*"]` | Glob patterns for collections to skip in local mode. Cloud mode (Voyage embeddings) ignores this — set to `[]` to enable all collections locally. |
| `collection_prefixes` | `["docs", "code", "knowledge", "rdr"]` | Prefix whitelist for `nx taxonomy validate-refs`. Extend this when your project adds a new user-facing collection prefix (e.g. `"custom"`). Internal-prefix collections (`taxonomy__*`, `plans__*`) are implementation-fixed and intentionally excluded. |

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

**bd-close gate** (`on_close: true`): Fires before `bd close` or `bd done` commands. Advisory only — warns when no review marker found in T1 scratch (from `/nx:review-code`). Never blocks.

## Qwen Offload

Optional opt-in surface for routing selected LLM workloads onto a local Qwen3.6-35B-A3B coprocessor running behind the [qwen-coprocessor-stack](https://github.com/Hellblazer/qwen-coprocessor-stack) supervisor. Three call-site tiers are independently routable: bundleable operators (already qwen-default), named operator-tier sites (`topic_labeler`, `plan_miss_planner`), the aspect extractor, and tier-B agentic tools (`nx_enrich_beads`, `nx_tidy`, `nx_plan_audit`). Claude is the default everywhere except the 10 bundleable operators that were already migrated; nothing routes to qwen unless the relevant knob is set.

Full bench evidence and per-call-site verdicts live in the qwen-coprocessor-stack [`docs/integrations/qwen-offload-2026-05-session-summary.md`](https://github.com/Hellblazer/qwen-coprocessor-stack/blob/main/docs/integrations/qwen-offload-2026-05-session-summary.md). Supervisor install + backend configuration lives in the same repo.

### Prerequisite — Qwen Code extension (tier-B only)

Tier-B routing (`NEXUS_TIER_B_DISPATCHER=qwen_agent`) spawns the qwen CLI with an `nx` extension that wires the `nx-mcp` MCP server into the session so qwen can call `memory_*`, `store_*`, etc. The extension manifest must exist at `~/.qwen/extensions/nx/qwen-extension.json`. The full snippet plus install instructions live in the qwen-coprocessor-stack [`docs/integrations/qwen-dispatch-nexus.md`](https://github.com/Hellblazer/qwen-coprocessor-stack/blob/main/docs/integrations/qwen-dispatch-nexus.md). Operator-tier and aspect-extractor routing do not need it — those paths use schema-bounded oneshot dispatch via `QWEN_BACKEND_URL` directly.

### Operator-tier routing

#### `NEXUS_DISPATCH_BACKEND`

- **Default:** `auto`
- **Allowed:** `claude` / `qwen` / `auto`
- **Effect:** Global mode for the 10 bundleable operators and for named operator-tier call sites that opt into `pick_dispatcher_for(...)` (currently `topic_labeler` and `plan_miss_planner`). `auto` picks per-operator from bake-in routing tables; `claude` and `qwen` force-pin everything in scope.
- **Bench:** Validated in nexus#623 (bundleables, 100% oracle agreement, 1.03× latency parity) and nexus#778/#779 (named call sites, 5/5 each).

#### `NEXUS_DISPATCH_QWEN_OPERATORS` / `NEXUS_DISPATCH_CLAUDE_OPERATORS`

- **Default:** unset
- **Allowed:** comma-separated list of operator / call-site names (e.g. `topic_labeler,plan_miss_planner`)
- **Effect:** Per-operator pin lists overlaid on `NEXUS_DISPATCH_BACKEND`. The CLAUDE list wins over the QWEN list when an operator appears in both. Empty / unset = inherit the global mode.
- **Notes:** Recognised call-site names are the keys consumed by `pick_dispatcher_for(call_site)`; the canonical list is in the dispatcher module. Misspellings are silently ignored.

#### `NEXUS_QWEN_CONCURRENCY`

- **Default:** `1`
- **Allowed:** positive integer
- **Effect:** Module-level semaphore on `qwen_dispatch` — caps concurrent in-flight calls to the qwen backend so a single host with a single GPU does not get serialised in surprising ways. Raise only if the backend can genuinely serve concurrent requests.

#### `QWEN_BACKEND_URL` / `QWEN_MODEL`

- **Default:** supervisor-provided
- **Effect:** OpenAI-compatible endpoint and model id used by `qwen_dispatch` (the schema-bounded oneshot path). Operators with the supervisor running on the default port will not normally set these; override when running against a non-default backend or model. Validated end-to-end in nexus#623.

### Aspect extractor

#### `NEXUS_ASPECT_BACKEND`

- **Default:** `claude`
- **Allowed:** `claude` / `qwen`
- **Effect:** Selects the backend for `nx enrich aspects`. Same output schema, same downstream T2 enrichment path; the only operator-visible difference is the engine on the wire and the latency/cost profile.
- **Bench:** Validated by spike_c (nexus#782, nexus#793) on the Grossberg-lab cognitive-modeling corpus (10 PDFs). Aspect on qwen is **5–12× slower** than claude; the win is cost (~$0.18/paper saved), not latency.
- **Notes:** When set to `qwen`, pair with `NEXUS_SCHOLARLY_PAPER_VERSION=v2` — the v1 prompt has a small field-completeness gap that v2 closes.

#### `NEXUS_SCHOLARLY_PAPER_VERSION`

- **Default:** `v1`
- **Allowed:** `v1` / `v2`
- **Effect:** Aspect-extractor prompt revision. `v2` tightens the per-field schema and recovers the field-completeness gap qwen showed against v1.
- **Bench:** Validated alongside `NEXUS_ASPECT_BACKEND=qwen` in nexus#790, nexus#793.
- **Notes:** Recommended whenever `NEXUS_ASPECT_BACKEND=qwen`. Has no negative effect on claude — operators on claude can opt in too if they want the tighter schema.

### Tier-B agentic tools

#### `NEXUS_TIER_B_DISPATCHER`

- **Default:** `claude`
- **Allowed:** `claude` / `qwen_agent`
- **Effect:** Global mode for tier-B agentic tools (`nx_enrich_beads`, `nx_tidy`, `nx_plan_audit`). `qwen_agent` routes through `qwen_agent_dispatch`, which spawns the qwen CLI with the `nx-mcp` extension active.
- **Bench:** Validated by spike_d (nexus#797, generalized judge in nexus#804). `nx_enrich_beads` 3/3 PASS, `nx_tidy` 2/2 PASS, `nx_plan_audit` mixed (see per-tool override below).
- **Notes:** Requires the Qwen Code extension at `~/.qwen/extensions/nx/qwen-extension.json` (see prerequisite above). Per-tool overrides win absolutely over this global setting.

#### `NEXUS_TIER_B_NX_TIDY_DISPATCHER` / `NEXUS_TIER_B_NX_ENRICH_BEADS_DISPATCHER` / `NEXUS_TIER_B_NX_PLAN_AUDIT_DISPATCHER`

- **Default:** inherits `NEXUS_TIER_B_DISPATCHER`, except `NX_PLAN_AUDIT` which is **pinned to `claude`** via the `TIER_B_CLAUDE_PINNED` constant regardless of the global mode (see caveat).
- **Allowed:** `claude` / `qwen_agent`
- **Effect:** Per-tool override. The override wins absolutely — if `NEXUS_TIER_B_DISPATCHER=qwen_agent` but `NEXUS_TIER_B_NX_TIDY_DISPATCHER=claude`, `nx_tidy` runs on claude.
- **Caveat — `nx_plan_audit`:** Pinned to claude by default. Bench surfaced a structural hallucination class on qwen that prompt-tightening (nexus#810) did not fix; the `verification_method` enum (nexus#812) makes the failure mode addressable but does not eliminate it. The per-tool override surface (nexus#813) is provided so the pin can be lifted for re-benching — set `NEXUS_TIER_B_NX_PLAN_AUDIT_DISPATCHER=qwen_agent` only if you are deliberately re-running the spike.

#### `QWEN_AGENT_SUPERVISOR`

- **Default:** supervisor-provided
- **Allowed:** command string (path to supervisor binary or `qwen` wrapper)
- **Effect:** Command invoked by `qwen_agent_dispatch` to launch the qwen CLI session. Operators with the standard supervisor install will not normally set this; override when running a custom supervisor build or when the binary is not on `PATH`. Introduced in nexus#796.

## File Locations

| File | Purpose |
|---|---|
| `~/.config/nexus/config.yml` | Global config and credentials |
| `~/.local/share/nexus/chroma/` | Local T3 ChromaDB PersistentClient data (local mode) |
| `~/.config/nexus/memory.db` | T2 SQLite database |
| `~/.config/nexus/repos.json` | Registered repos (`nx index repo` writes here) |
| `~/.config/nexus/sessions/` | JSON session records (T1 server address, session ID, `created_at`, `tmpdir`) + `session.lock` |
| `~/.config/nexus/index.log` | Background indexing log (written by git hooks) |
| `.nexus.yml` | Per-repo config overrides |
