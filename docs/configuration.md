# Nexus Configuration Reference

## Config Hierarchy

Four levels, highest priority wins:

1. **Environment variables** (`NX_*`) — highest priority
2. **Per-repo**: `.nexus.yml` in repo root (gitignored by default)
3. **Global**: `~/.config/nexus/config.yml`
4. **Built-in defaults** — lowest priority

Each level is deep-merged, with higher-priority values winning.

## Credentials

| Config key | Env var | Required | Notes |
|---|---|---|---|
| `chroma_api_key` | `CHROMA_API_KEY` | Yes | ChromaDB Cloud API key |
| `chroma_database` | `CHROMA_DATABASE` | Yes | ChromaDB Cloud database name (e.g. `nexus`) |
| `voyage_api_key` | `VOYAGE_API_KEY` | Yes | Voyage AI embeddings key |
| `chroma_tenant` | `CHROMA_TENANT` | No | Auto-inferred from API key; only needed for multi-workspace setups |

Set via `nx config init` (wizard) or `nx config set KEY VALUE`. Stored in `~/.config/nexus/config.yml`.

**`chroma_database` is the database name.** Nexus connects to this single database on ChromaDB Cloud. All collection prefixes (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexist in it. Use `nx doctor` to check its status.

**`chroma_tenant` is optional.** The ChromaDB `CloudClient` infers the tenant UUID directly from your API key. You only need to set it explicitly if you belong to multiple Chroma Cloud workspaces.

**Database creation is automatic.** `nx config init` provisions the database on Chroma Cloud using your API key — no dashboard visit required. If provisioning fails (e.g., plan restrictions), create the database manually in the Chroma Cloud dashboard.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | Voyage reranker for multi-corpus merge |
| `client.host` | `NX_CLIENT_HOST` | `localhost` | Override ChromaDB host URL |

Embedding models are selected automatically based on collection type (see [Storage Tiers](storage-tiers.md)): `voyage-code-3` for code, `voyage-context-3` for docs/knowledge at index time, `voyage-4` for all queries.

## Per-Repo Overrides (.nexus.yml)

Place `.nexus.yml` at repo root. It is gitignored by default.

```yaml
indexing:
  code_extensions: [".proto", ".thrift"]    # added to the built-in code set
  prose_extensions: [".txt.j2", ".md.tmpl"] # forced to prose (wins over code)
  rdr_paths: ["docs/rdr", "decisions"]      # directories indexed into rdr__ collection
  include_untracked: true                   # also index untracked (but not .gitignored) files
```

Merge behavior: nested dict keys are **additive** (both global and per-repo keys are retained). Scalar values and lists are **replacement** (the per-repo value wins entirely between config levels). However, `code_extensions` is additive to the **built-in** extension set — it extends the defaults, it does not replace them. `prose_extensions` wins over everything: if an extension appears in both lists, it is classified as prose. See [Repo Indexing](repo-indexing.md) for the full extension list and override semantics.

## Tuning Parameters

The `[tuning]` section in `~/.config/nexus/config.yml` controls search scoring, chunking, and timeout behavior. All values have sensible defaults — only override what you need.

```yaml
tuning:
  vector_weight: 0.7              # weight for vector similarity in hybrid scoring
  frecency_weight: 0.3            # weight for git frecency in hybrid scoring
  file_size_threshold: 30         # chunks — files larger than this are down-ranked
  decay_rate: 0.01                # frecency decay rate (higher = faster decay)
  code_chunk_lines: 150           # target lines per code chunk (fallback splitter)
  pdf_chunk_chars: 1500           # target chars per PDF chunk
  git_log_timeout: 30             # seconds — timeout for git log subprocess
  ripgrep_timeout: 10             # seconds — timeout for ripgrep subprocess in hybrid search
```

| Key | Default | Description |
|-----|---------|-------------|
| `vector_weight` | `0.7` | Vector similarity weight in hybrid scoring formula |
| `frecency_weight` | `0.3` | Git frecency weight in hybrid scoring formula |
| `file_size_threshold` | `30` | Chunk count above which code files are down-ranked |
| `decay_rate` | `0.01` | Exponential decay rate for frecency scoring |
| `code_chunk_lines` | `150` | Target lines per code chunk (line-based fallback) |
| `pdf_chunk_chars` | `1500` | Target characters per PDF chunk |
| `git_log_timeout` | `30` | Timeout (seconds) for `git log` subprocess |
| `ripgrep_timeout` | `10` | Timeout (seconds) for `rg` subprocess in hybrid search |

These values are exposed as a `TuningConfig` dataclass in `nexus.config`. The search command, indexer, and scoring modules all read from this config — changes take effect on the next invocation without restarting anything.

## File Locations

| File | Purpose |
|---|---|
| `~/.config/nexus/config.yml` | Global config and credentials |
| `~/.config/nexus/memory.db` | T2 SQLite database |
| `~/.config/nexus/repos.json` | Registered repos (`nx index repo` writes here) |
| `~/.config/nexus/sessions/` | JSON session records (T1 server address, session ID, `created_at`, `tmpdir`) + `session.lock` |
| `~/.config/nexus/index.log` | Background indexing log (written by git hooks) |
| `.nexus.yml` | Per-repo config overrides |
