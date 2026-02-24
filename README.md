# Nexus

Self-hosted semantic search and knowledge management for Claude Code agents.

Nexus gives you and your AI agents a single CLI to index repositories, PDFs, and notes; search across all of them semantically; and manage persistent memory across sessions. Only chunk text and embeddings leave your machine — raw source files stay local.

## How it works

Nexus organises data across three tiers:

| Tier | Storage | Network | Use |
|------|---------|---------|-----|
| **T1 — scratch** | In-memory ChromaDB | None | Session-scoped working state, wiped at end |
| **T2 — memory** | Local SQLite + FTS5 | None | Named notes that survive restarts |
| **T3 — knowledge** | ChromaDB cloud + Voyage AI | Required | Permanent semantic search |

**T1 and T2 work with zero API keys.** T3 is for permanent, cross-session semantic search over indexed code, documents, and stored knowledge.

Within T3, collections fall into three corpus categories:

| Corpus | Created by | Collection name |
|--------|-----------|-----------------|
| `code` | `nx index repo` | `code__<repo>`, `docs__<repo>` |
| `docs` | `nx index pdf` / `nx index md` | `docs__<corpus>` |
| `knowledge` | `nx store put` | `knowledge__<topic>` |

`nx search --corpus code` searches all code collections; `--corpus code__myrepo` scopes to one repo.

## Commands

| Command | Tier | Description |
|---------|------|-------------|
| `nx search` | T3 | Semantic search across indexed code, docs, and knowledge |
| `nx store` | T3 | Store and list agent outputs in cloud knowledge |
| `nx index` | T3 | Index code repos, PDFs, and markdown |
| `nx memory` | T2 | Named per-project notes that survive restarts |
| `nx pm` | T2 + T3 | Project management: phases, blockers, archive |
| `nx scratch` | T1 | Session scratch pad, wiped at session end |
| `nx serve` | — | Background daemon for git HEAD polling and auto-reindex |
| `nx collection` | T3 | Inspect and manage cloud collections |
| `nx config` | — | Manage credentials and settings |

## Prerequisites

**Required for T3 (cloud semantic search):**
- [ChromaDB cloud](https://www.trychroma.com/) account — tenant ID, database name, and API key
- [Voyage AI](https://www.voyageai.com/) API key — free tier: 200 M tokens/month

**Required for answer mode and PM archival:**
- [Anthropic](https://www.anthropic.com/) API key

**Required for hybrid code search:**
- `ripgrep` on PATH (`brew install ripgrep` / `apt install ripgrep`)

**Always required:**
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or pip
- `git`

> **T1 and T2 work immediately with no accounts.** You only need the cloud services when you want permanent semantic search (`nx search`, `nx index`, `nx store`).

## Installation

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv sync
uv pip install -e .
```

## Setup

### 1. Get your API keys

**ChromaDB cloud**

Sign up at [trychroma.com](https://www.trychroma.com/). After creating an account:
- **Tenant**: the UUID shown on your dashboard or in the API key page (e.g. `c749e1f8-2c59-43fc-8e44-19d534e1404a`). The URL slug like `me-abc123` is *not* the tenant — use the UUID.
- **Database**: `default_database` unless you created a custom one.
- **API key**: generate one under Settings → API Keys.

**Voyage AI**

Sign up at [voyageai.com](https://www.voyageai.com/). Get your API key from the dashboard. The free tier (200 M tokens/month) is enough for most personal use. Add a payment method to unlock standard rate limits — you won't be charged until you exceed the free tier.

**Anthropic**

Sign up at [console.anthropic.com](https://console.anthropic.com/). Add credits under Plans & Billing; the API key is on the API Keys page.

### 2. Configure credentials

Run the interactive wizard:

```bash
nx config init
```

Or set values individually:

```bash
nx config set chroma_api_key    your-key
nx config set chroma_tenant     your-tenant-uuid
nx config set chroma_database   default_database
nx config set voyage_api_key    your-key
nx config set anthropic_api_key your-key
```

Credentials are stored in `~/.config/nexus/config.yml`. Environment variables take precedence:
`CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE`, `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`.

### 3. Verify

```bash
nx doctor
```

All five checks (ChromaDB, Voyage AI, Anthropic, ripgrep, git) should show green. Missing credentials are reported individually — you can proceed with a subset if you only need T1/T2.

### 4. Claude Code integration (optional)

Install session hooks and a skill file so agents can use Nexus automatically:

```bash
nx install claude-code
nx uninstall claude-code   # to remove
```

This writes `~/.claude/skills/nexus/SKILL.md` and registers SessionStart/SessionEnd hooks in `~/.claude/settings.json`. At session start, scratch (T1) is initialised and your project's PM context is computed and injected automatically.

## Quick start

### Index a code repository

```bash
nx serve start              # start the background server (optional but recommended)
nx index repo .             # index the current repo
nx index repo /path/to/other-repo
```

The server watches registered repos and re-indexes automatically when the git HEAD changes. You don't need it running to search — it just keeps the index fresh.

```bash
# Already indexed? Refresh git recency scores without re-embedding (fast)
nx index repo . --frecency-only
```

> **Frecency** = a blend of recency and frequency derived from git history. Recently-touched files rank higher in hybrid search results.

### Search

```bash
# Semantic search across all indexed code, docs, and knowledge
nx search "authentication token validation"

# Scope to a specific corpus type or collection
nx search "caching strategy" --corpus docs
nx search "retry logic" --corpus code__myrepo

# Multi-corpus search (independent retrieval, then Voyage reranker merge)
nx search "auth flow" --corpus code --corpus docs

# Synthesize a cited answer (requires Anthropic key)
nx search "how does session management work" --answer

# Multi-step query refinement via Haiku
nx search "token validation" --agentic

# Hybrid: semantic + ripgrep frecency (code corpora only)
nx search "token validation" --hybrid

# Filter by metadata
nx search "caching" --where store_type=pm-archive

# Context lines around results
nx search "parse_request" -C 3

# Show matched text inline
nx search "parse_request" --content

# Output formats for editor and tool integration
nx search "validate_token" --vimgrep    # path:line:col:content
nx search "validate_token" --json       # JSON array
nx search "validate_token" --files      # unique file paths only
```

### Index PDFs and Markdown

```bash
nx index pdf ~/papers/architecture.pdf --corpus my-papers
nx index md  ~/notes/decisions.md      --corpus notes

nx search "distributed consensus" --corpus docs__my-papers
```

### Store and retrieve knowledge

```bash
# Store a file in cloud knowledge
nx store put analysis.md --collection knowledge --tags "security,audit"

# Store from stdin (--title required)
echo "# Key insight..." | nx store put - --collection knowledge --title "Auth Analysis"

# Store with a TTL (auto-expires after 30 days)
nx store put temp-notes.md --collection knowledge --ttl 30d

# List entries in a knowledge collection
nx store list
nx store list --collection knowledge__notes

# Remove expired entries
nx store expire

# Search stored knowledge
nx search "security vulnerabilities" --corpus knowledge
```

## Storage tier reference

### T1 — Session scratch

Fast, ephemeral, zero API calls. Uses ChromaDB's bundled MiniLM-L6-v2 model (local ONNX). Everything is wiped when the session ends — use `nx scratch flag` to promote items to T2 automatically on exit.

```bash
nx scratch put "working hypothesis: the cache is stale"
nx scratch search "cache"
nx scratch list
nx scratch get <id>
nx scratch flag <id>          # mark for auto-flush to T2 at session end
nx scratch unflag <id>
nx scratch promote <id> --project myrepo --title findings.md
nx scratch clear
```

### T2 — Memory bank

Survives restarts. No network dependency. WAL mode supports multiple concurrent sessions.

```bash
# Write
nx memory put "content" --project myrepo --title decisions.md --tags "arch" --ttl 30d
echo "# Analysis..." | nx memory put - --project myrepo --title analysis.md

# Read by name (deterministic)
nx memory get --project myrepo --title decisions.md

# Keyword search (FTS5, no API call)
nx memory search "caching decision"
nx memory search "auth" --project myrepo

# List and housekeeping
nx memory list --project myrepo
nx memory expire

# Promote to T3 for semantic search
nx memory promote <id> --collection knowledge
```

TTL format: `30d`, `4w`, `permanent` (or `never`). Default: `30d`.

### T3 — Permanent knowledge

Requires `CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE`, and `VOYAGE_API_KEY`.

Collections are namespaced by corpus type:
- `code__<repo>` — indexed source code
- `docs__<corpus>` — indexed PDFs and markdown
- `knowledge__<topic>` — stored agent outputs and notes

## Project management

`nx pm` tracks project phases, blockers, and decisions in T2, with optional promotion and archival to T3 via Haiku synthesis.

```bash
# Initialise for the current git repo
nx pm init

# Resume context in a new session
nx pm resume

# Track state
nx pm status
nx pm block "waiting on API approval"
nx pm unblock 1

# Advance to the next phase
nx pm phase next

# Read a specific phase doc
nx memory get --project myrepo_pm --title phases/phase-2/context.md

# Search across all PM docs (FTS5, no API call)
nx pm search "what did we decide about caching"

# Promote a doc to cloud knowledge
nx pm promote phases/phase-2/context.md --collection knowledge --tags "decision,architecture"

# Housekeeping
nx pm expire

# Archive the project (synthesizes to T3 via Haiku, starts 90-day T2 decay)
nx pm archive
nx pm close                  # archive + mark completed

# Restore within the 90-day window
nx pm restore myrepo

# Search archived syntheses across all past projects
nx pm reference "how did we handle rate limiting"
nx pm reference myrepo       # retrieve by project name
```

## Collection management

```bash
nx collection list                          # all cloud collections with doc counts
nx collection info <name>                   # details for a single collection
nx collection verify <name>                 # existence check + doc count
nx collection verify <name> --deep          # existence check + embedding probe
nx collection delete <name> --confirm       # irreversible
```

## Server management

The `nx serve` daemon watches registered repos and re-indexes when git HEAD changes. It is optional — search always works against whatever is already indexed.

```bash
nx serve start           # start (port: NX_SERVER_PORT or config.yml server.port, default 7890)
nx serve stop
nx serve status          # uptime and per-repo indexing state
nx serve logs            # last 20 lines of server output
nx serve logs -n 50      # last N lines
```

Server output is written to `~/.config/nexus/serve.log`.

## Configuration

```bash
nx config list                          # show all credentials and settings
nx config get <key>                     # show one value
nx config set chroma_api_key sk-...     # set a credential
```

`nx config set` stores credentials only. To change settings, use environment variables or edit `~/.config/nexus/config.yml` directly:

```bash
NX_SERVER_PORT=7891 nx serve start
NX_EMBEDDINGS_RERANKER_MODEL=rerank-2.5-lite nx search "query"
```

Per-repo overrides: `.nexus.yml` at the repo root (merged over global; gitignored).

**Key settings:**

| YAML path | Env var | Default | Description |
|-----------|---------|---------|-------------|
| `server.port` | `NX_SERVER_PORT` | `7890` | HTTP port for `nx serve` |
| `server.headPollInterval` | `NX_SERVER_HEAD_POLL_INTERVAL` | `10` | Seconds between HEAD checks per repo |
| `embeddings.codeModel` | `NX_EMBEDDINGS_CODE_MODEL` | `voyage-code-3` | Voyage model for code |
| `embeddings.docsModel` | `NX_EMBEDDINGS_DOCS_MODEL` | `voyage-4` | Voyage model for docs and knowledge |
| `embeddings.rerankerModel` | `NX_EMBEDDINGS_RERANKER_MODEL` | `rerank-2.5` | Voyage reranker for multi-corpus merge |
| `pm.archiveTtl` | `NX_PM_ARCHIVE_TTL` | `90` | Days before archived PM docs decay from T2 |

## Development

```bash
uv sync
uv run pytest                 # full test suite, no API keys required
uv run pytest -m integration  # end-to-end tests (requires real API keys in environment)
uv run pytest --cov=nexus     # with coverage
```

Unit tests use `chromadb.EphemeralClient` + the bundled ONNX MiniLM model — no accounts needed. Integration tests read credentials from environment variables; copy `.env.example` to `.env`, fill in your keys, and run `set -a && source .env && set +a` before invoking pytest.

## Architecture

```
T1  chromadb.EphemeralClient + DefaultEmbeddingFunction (MiniLM-L6-v2, local ONNX)
T2  sqlite3 + FTS5, WAL mode — ~/.config/nexus/memory.db
T3  chromadb.CloudClient + VoyageAIEmbeddingFunction — ChromaDB cloud

Indexing pipelines:
  repo   git frecency → classify by extension → code files: tree-sitter AST → voyage-code-3 → T3 code__<repo>
                                               docs files: semantic markdown → voyage-context-3 → T3 docs__<repo>
  PDF    PyMuPDF4LLM extraction → voyage-4 (CCE) → T3 docs__<corpus>
  MD     SemanticMarkdownChunker + SHA256 sync → voyage-4 (CCE) → T3 docs__<corpus>

Search modes:
  semantic    ChromaDB vector similarity (per-corpus), then Voyage rerank-2.5 merge
  hybrid      semantic + ripgrep frecency (0.7 × vector + 0.3 × frecency, code only)
  answer      retrieval → Haiku synthesis → cited output

Session ID: os.getsid(0) → ~/.config/nexus/sessions/{sid}.session
Repo registry: ~/.config/nexus/repos.json
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
