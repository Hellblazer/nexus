# Nexus Configuration Reference

## Config Hierarchy

Four levels, highest priority wins:

1. **Environment variables** (`NX_*`) — highest priority
2. **Per-repo**: `.nexus.yml` in repo root (gitignored by default)
3. **Global**: `~/.config/nexus/config.yml`
4. **Built-in defaults** — lowest priority

Each level is deep-merged, with higher-priority values winning.

## Credentials

| Config key | Env var | Required for |
|---|---|---|
| `chroma_api_key` | `CHROMA_API_KEY` | T3 (cloud storage) |
| `chroma_tenant` | `CHROMA_TENANT` | T3 |
| `chroma_database` | `CHROMA_DATABASE` | T3 (base name — see below) |
| `voyage_api_key` | `VOYAGE_API_KEY` | T3 (embeddings) |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | Answer mode, PM archive |
| `mxbai_api_key` | `MXBAI_API_KEY` | Mixedbread search (optional) |

Set via `nx config init` (wizard) or `nx config set KEY VALUE`. Stored in `~/.config/nexus/config.yml`.

**`chroma_database` is a base name.** Nexus uses it to derive four database names: `{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`. For example, `chroma_database = nexus` creates connections to `nexus_code`, `nexus_docs`, `nexus_rdr`, and `nexus_knowledge`. All four must exist in your ChromaDB Cloud dashboard. Use `nx doctor` to check their status.

## Settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `server.port` | `NX_SERVER_PORT` | `7890` | HTTP port for `nx serve` |
| `server.headPollInterval` | `NX_SERVER_HEAD_POLL_INTERVAL` | `10` | Seconds between HEAD checks per repo |
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | Voyage reranker for multi-corpus merge |
| `pm.archiveTtl` | `NX_PM_ARCHIVE_TTL` | `90` | Days before archived PM docs decay from T2 |
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

## File Locations

| File | Purpose |
|---|---|
| `~/.config/nexus/config.yml` | Global config and credentials |
| `~/.config/nexus/memory.db` | T2 SQLite database |
| `~/.config/nexus/repos.json` | Registered repos for `nx serve` |
| `~/.config/nexus/sessions/` | JSON session records (T1 server address, session ID, `created_at`, `tmpdir`) + `session.lock` |
| `~/.config/nexus/server.pid` | Server PID file |
| `~/.config/nexus/serve.log` | Server log output |
| `.nexus.yml` | Per-repo config overrides |
