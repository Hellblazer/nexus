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

**`chroma_database` is the database name.** Nexus connects to this single database on ChromaDB Cloud. All collection prefixes (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexist in it. Use `nx doctor` to check its status.

**`chroma_tenant` is optional.** The ChromaDB `CloudClient` infers the tenant UUID directly from your API key. You only need to set it explicitly if you belong to multiple Chroma Cloud workspaces.

**Database creation is automatic.** `nx config init` provisions the database on Chroma Cloud using your API key — no dashboard visit required. If provisioning fails (e.g., plan restrictions), create the database manually in the Chroma Cloud dashboard.

**Upgrading from the four-database layout?** If you previously used the four-database layout (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`), nexus will auto-detect it and show migration steps. The migration is non-destructive — old databases are never modified or deleted. Export your data with the pre-upgrade version before upgrading, then follow the guided steps. See the [CHANGELOG](../CHANGELOG.md) for the full migration path.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | Voyage reranker for multi-corpus merge |
| `client.host` | `NX_CLIENT_HOST` | `localhost` | Override ChromaDB host URL |
| `pdf.extractor` | — | `auto` | PDF extraction backend: `auto`, `docling`, or `mineru`. Set globally with `nx config set pdf.extractor=mineru` |
| `voyageai.read_timeout_seconds` | `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` | `120` | Request timeout (seconds) for Voyage AI API calls. Increase for large PDF indexing |

Embedding models are selected automatically based on collection type (see [Storage Tiers](storage-tiers.md)): `voyage-code-3` for code, `voyage-context-3` (CCE) for docs/rdr/knowledge at both index and query time, `voyage-4` for code queries only.

## Per-Repo Overrides (.nexus.yml)

Place `.nexus.yml` at repo root. It is gitignored by default.

```yaml
indexing:
  code_extensions: [".proto", ".thrift"]    # added to the built-in code set
  prose_extensions: [".txt.j2", ".md.tmpl"] # forced to prose (wins over code)
  rdr_paths: ["docs/rdr", "decisions"]      # directories indexed into rdr__ collection
  include_untracked: true                   # also index untracked (but not .gitignored) files
```

```yaml
pdf:
  extractor: mineru   # auto | docling | mineru (default: auto)
```

Or set via CLI: `nx config set pdf.extractor=mineru` (writes to global config). See [PDF Extraction Backends](cli-reference.md#pdf-extraction-backends) for details.

Merge behavior: nested dict keys are **additive** (both global and per-repo keys are retained). Scalar values and lists are **replacement** (the per-repo value wins entirely between config levels). However, `code_extensions` is additive to the **built-in** extension set — it extends the defaults, it does not replace them. `prose_extensions` wins over everything: if an extension appears in both lists, it is classified as prose. See [Repo Indexing](repo-indexing.md) for the full extension list and override semantics.

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
