# Getting Started with Nexus

Nexus provides persistent memory and semantic search for AI coding agents and the teams that work with them. This guide walks through installation, immediate local usage, and optional cloud setup for semantic search.

## Install

```bash
uv tool install conexus
nx --help
```

Requires Python 3.12–3.13, [uv](https://docs.astral.sh/uv/), and git. Python 3.14 is not yet supported (upstream dependency incompatibility). For source installation, see [Contributing](https://github.com/Hellblazer/nexus/blob/main/docs/contributing.md).

### Updating

```bash
uv tool update conexus
```

### Cloud embeddings (optional)

For Voyage AI cloud embeddings instead of local ONNX:

```bash
uv tool install conexus --with "conexus[cloud]"
```

To update with the cloud extra:

```bash
uv tool install conexus --with "conexus[cloud]" --force
```

## Start using it (no API keys needed)

Scratch and memory work immediately with zero configuration.

**Scratch** — inter-agent session context (ephemeral):

```bash
nx scratch put "working hypothesis: the cache TTL is too short"
nx scratch list
nx scratch search "cache"
```

**Memory** — project-level persistence with full-text search:

```bash
nx memory put "auth uses JWT with 24h expiry" -p myproject -t auth-notes
nx memory search "JWT" -p myproject
nx memory list -p myproject
nx memory get -p myproject -t auth-notes
```

These are T1 and T2 — fully local, no accounts, no network. Many workflows start and end here.

## Claude Code plugin

For Claude Code, also install the plugin (see [plugin documentation](https://github.com/Hellblazer/nexus/blob/main/nx/README.md)):

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

The plugin gives agents direct access to all three storage tiers via MCP servers, plus specialized agents, skills, session hooks, and development workflows. For local development, load from a repo checkout:

```bash
claude --plugin-dir ./nx
```

## Semantic search — local mode (zero config)

Nexus includes a local T3 backend that works immediately — no API keys, no accounts, no network.

```bash
nx index repo .           # index your codebase with local ONNX embeddings
nx search "retry logic"   # semantic search, runs entirely on your machine
```

Local mode activates automatically when no cloud credentials are configured. It uses ChromaDB `PersistentClient` with bundled MiniLM-L6-v2 embeddings (384 dimensions). Data is stored at `~/.local/share/nexus/chroma`.

**Upgrade embedding quality** with `pip install conexus[local]` to use `bge-base-en-v1.5` (768 dimensions) — better retrieval accuracy, especially for code search.

To force local mode even when cloud credentials exist, set `NX_LOCAL=1`.

## Semantic search — cloud mode

When you want higher-quality embeddings (1024d Voyage AI), cross-chunk context (CCE), and reranking, install the cloud extra and set up T3 cloud credentials.

```bash
uv tool install conexus --with "conexus[cloud]" --force
```

### Accounts

| Service | Purpose | Free tier |
|---------|---------|-----------|
| [ChromaDB Cloud](https://trychroma.com) | Vector storage | Generous for individual use |
| [Voyage AI](https://voyageai.com) | Embeddings | 200M tokens/month |

Both free tiers cover typical Nexus usage at no cost. Voyage AI may require a credit card on file for higher rate limits, but usage remains free for normal workloads.

### Configure

Run the interactive wizard:

```bash
nx config init
```

This walks through each credential and automatically provisions the ChromaDB database. Alternatively, set credentials individually:

```bash
nx config set chroma_api_key sk-...
nx config set chroma_database nexus
nx config set voyage_api_key pa-...
```

Credentials are stored in `~/.config/nexus/config.yml`. Environment variables (`CHROMA_API_KEY`, `CHROMA_DATABASE`, `VOYAGE_API_KEY`) always take precedence. See [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) for the full reference.

### Verify

```bash
nx doctor
```

Checks credentials, required tools, and connectivity to the T3 database. Fix anything marked with `✗` before proceeding.

### Index a repo

```bash
nx index repo .
```

Code files are chunked via tree-sitter AST parsing and embedded with `voyage-code-3`. Prose and PDFs are embedded with `voyage-context-3`. RDR documents in `docs/rdr/` are auto-discovered and indexed separately. The indexer respects `.gitignore` and skips binary and generated files.

### Search

```bash
nx search "how does authentication work"
nx search "retry logic" --corpus code
nx search "API changelog" --corpus docs
nx search "database pool" --hybrid          # blend semantic + keyword matching
```

Common flags: `--n 20` (result count), `--json`, `--files` (paths only), `-c` (show matched text). The `--hybrid` flag requires [ripgrep](https://github.com/BurntSushi/ripgrep) (`brew install ripgrep` or your system package manager).

## Troubleshooting

**`nx doctor` reports credentials not set** — Run `nx config init` to walk through setup interactively.

**Provisioning failed during `nx config init`** — If your ChromaDB plan restricts automatic database creation, create the database manually in the [dashboard](https://trychroma.com) using your `chroma_database` value as the name.

**`nx index repo .` fails with "credentials not set"** — In cloud mode, indexing requires T3 credentials. Run `nx config init` first, or use local mode (no credentials needed).

**First index is slow or hits a rate limit** — Large repos may take a few minutes. Add `--monitor` for per-file progress. Re-running after a partial index is safe — unchanged files are skipped.

**`nx search` returns no results** — Run `nx doctor` to verify the database is reachable. If indexing was interrupted, re-run `nx index repo .` to resume.

## Next steps

- [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) — full command documentation
- [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md) — T1, T2, T3 architecture and trade-offs
- [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) — file classification, incremental updates, frecency
- [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) — all config keys, environment variables, `config.yml` format
- [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) — decision tracking with Research-Design-Review
