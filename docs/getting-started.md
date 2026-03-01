# Getting Started with Nexus

Nexus is a self-hosted semantic search and knowledge management CLI.
It indexes code repositories, documents, and notes into three storage tiers,
then lets you search across all of them with a single command.

## Prerequisites

- **Python 3.12+**
- **uv** (package manager) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **git**
- **ripgrep** (`rg`) — required for `--hybrid` search. Install via `brew install ripgrep` or your system package manager.
- **chroma** CLI — required for T1 multi-agent session sharing. Install via `pip install chromadb` or `uv tool install chromadb`.

For T3 (permanent cloud storage), you also need accounts at:

| Service | Purpose | Free tier | Signup |
|---------|---------|-----------|--------|
| ChromaDB Cloud | Vector storage | Generous storage + requests for individual use | [trychroma.com](https://trychroma.com) |
| Voyage AI | Embeddings | 200M tokens/month — indexing a large codebase uses 1–5M | [voyageai.com](https://voyageai.com) |
| Anthropic | PM archival (optional) | — | [console.anthropic.com](https://console.anthropic.com) |

> **Cost**: Both ChromaDB Cloud and Voyage AI free tiers cover all typical Nexus usage at no cost.
> Voyage AI requires a credit card on file to unlock higher rate limits — but **usage remains
> free**. You will not be charged for normal indexing and search workloads.

Scratch and memory commands work with zero API keys (see [Local-only quick start](#local-only-quick-start)).

## Install

**From PyPI** (recommended):

```bash
uv tool install conexus
```

**From source**:

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv sync
```

Verify the CLI is available:

```bash
nx --help
```

## Configure credentials

Run the interactive wizard:

```bash
nx config init
```

It walks through each credential and shows where to sign up. Alternatively, set them individually:

```bash
nx config set chroma_api_key sk-...
nx config set chroma_tenant YOUR_TENANT_UUID
nx config set chroma_database default_database
nx config set voyage_api_key pa-...
nx config set anthropic_api_key sk-ant-...
```

Credentials are stored in `~/.config/nexus/config.yml`. Environment variables always take precedence:

| Config key | Environment variable |
|-----------|---------------------|
| `chroma_api_key` | `CHROMA_API_KEY` |
| `chroma_tenant` | `CHROMA_TENANT` |
| `chroma_database` | `CHROMA_DATABASE` |
| `voyage_api_key` | `VOYAGE_API_KEY` |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` |

**ChromaDB tenant** is the UUID shown on your Cloud settings page, not the URL slug.
**ChromaDB database** is the base name for your four T3 databases. If you set `chroma_database = nexus`, Nexus will use `nexus_code`, `nexus_docs`, `nexus_rdr`, and `nexus_knowledge`. You must create all four in your ChromaDB Cloud dashboard before running `nx index` or `nx store`.

**Creating the four databases**: In the ChromaDB Cloud dashboard, create four databases named `{base}_code`, `{base}_docs`, `{base}_rdr`, and `{base}_knowledge` (where `{base}` is your chosen base name). Run `nx doctor` to verify all four are reachable.

**Upgrading from an older single-database setup**: If you have existing data in a single ChromaDB database from a pre-four-store version of Nexus, run `nx migrate t3` to copy your collections to the new layout. The operation is idempotent — already-migrated collections are skipped.

## Verify

```bash
nx doctor
```

This checks all credentials, required tools (`rg`, `git`), and whether the Nexus server is running. Fix anything marked with a cross before proceeding.

## Index your first repo

```bash
nx index repo .
```

This registers the repository and indexes it into T3 ChromaDB:

- **Code files** (`.py`, `.java`, `.ts`, `.go`, etc.) are embedded with `voyage-code-3` into `code__<repo-name>` collections.
- **Prose files** (`.md`, `.txt`, `.rst`) and PDFs are embedded with `voyage-context-3` into `docs__<repo-name>` collections.
- **RDR documents** in `docs/rdr/` are auto-discovered and indexed into `rdr__<repo-name>`.

Files are classified by extension. The indexer respects `.gitignore` and skips binary/generated files.

## Search

Basic semantic search across all corpora:

```bash
nx search "how does authentication work"
```

Scope to code or docs:

```bash
nx search "retry logic" --corpus code
nx search "API changelog" --corpus docs
```

Blend semantic search with git frecency for hybrid ranking:

```bash
nx search "database connection pool" --hybrid
```

Other useful flags:

```bash
nx search "query" --n 20              # return 20 results (default: 10)
nx search "query" --json              # JSON output
nx search "query" --files             # file paths only
nx search "query" --vimgrep           # path:line:col:content format
nx search "query" -c                  # show matched text inline
nx search "query" path/to/dir         # scope to a directory
```

## Local-only quick start

Scratch and memory require no API keys or cloud accounts.

**Scratch** — ephemeral per-session notes:

```bash
nx scratch put "working hypothesis: the cache TTL is too short"
nx scratch list
nx scratch search "cache"
nx scratch flag <ID>          # auto-promote to T2 on session end
```

**Memory** — persistent per-project notes (survives restarts):

```bash
nx memory put "auth uses JWT with 24h expiry" -p myproject -t auth-notes
nx memory get -p myproject -t auth-notes       # by project + title
nx memory get 42                                # or by numeric ID
nx memory search "JWT" -p myproject
nx memory list -p myproject
```

## Claude Code integration

The `nx/` directory in this repo is a Claude Code plugin. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

For local development, load the plugin directly from the repo checkout:

```bash
claude --plugin-dir ./nx
```

The plugin provides 15 agents, 28 skills, session hooks, slash commands, a bundled MCP server (sequential-thinking), and standard pipelines. See [nx/README.md](../nx/README.md) for details.

## Next steps

- [CLI Reference](cli-reference.md) — full command documentation
- [Storage Tiers](storage-tiers.md) — T1, T2, T3 architecture and trade-offs
- [Repo Indexing](repo-indexing.md) — file classification, incremental updates, frecency
- [Configuration](configuration.md) — all config keys, environment variables, `config.yml` format
