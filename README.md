# Nexus

Self-hosted semantic search and knowledge management for Claude Code agents.

Nexus indexes your code, PDFs, and notes into ChromaDB cloud using Voyage AI embeddings, then gives you (and your agents) a single CLI for search, memory, and project management. Raw content never leaves your machine — only vectors and chunk text are stored in the cloud.

## What it does

| Command | Storage | Use |
|---------|---------|-----|
| `nx search` | T3 ChromaDB cloud | Semantic search across indexed code, docs, and knowledge |
| `nx store` | T3 ChromaDB cloud | Persist agent outputs for future sessions |
| `nx memory` | T2 SQLite (local) | Named per-project notes that survive restarts |
| `nx scratch` | T1 in-memory | Session-scoped working state, wiped at session end |
| `nx index` | → T3 | Index code repos, PDFs, and markdown files |
| `nx pm` | T2 + T3 | Project management: phases, blockers, archive |
| `nx serve` | — | Background server for HEAD polling and auto-reindex |

## Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or pip
- [ChromaDB cloud](https://www.trychroma.com/) account — tenant, database, and API key
- [Voyage AI](https://www.voyageai.com/) API key — free tier: 200M tokens/month
- [Anthropic](https://www.anthropic.com/) API key — for answer mode and PM archive synthesis
- `ripgrep` on PATH — for hybrid code search (`brew install ripgrep` / `apt install ripgrep`)
- `git` — for frecency scoring

## Installation

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv pip install -e .
```

Or install from a wheel:

```bash
uv pip install nexus-0.2.0-py3-none-any.whl
```

### Configure credentials

Run the interactive wizard:

```bash
nx config init
```

Or set individual values:

```bash
nx config set chroma_api_key   your-key
nx config set chroma_tenant    your-tenant
nx config set chroma_database  your-database
nx config set voyage_api_key   your-key
nx config set anthropic_api_key your-key
```

Credentials are stored in `~/.config/nexus/config.yml`. Environment variables take precedence:
`CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE`, `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`.

Verify everything is wired up:

```bash
nx doctor
```

### Claude Code integration

Install session hooks and a SKILL.md so agents can use Nexus automatically:

```bash
nx install claude-code
```

This writes `~/.claude/skills/nexus/SKILL.md` and adds SessionStart/SessionEnd hooks to `~/.claude/settings.json`. The SessionStart hook initializes T1 scratch and injects your project's `CONTINUATION.md` into context at the start of each session.

## Quick start

### Index a code repository

```bash
nx serve start           # start the background server
nx index code .          # index the current repo (registers it with the server)
nx index code /path/to/other-repo
```

The server polls HEAD every 10 seconds and re-indexes on change. Re-indexing is automatic after commits.

### Search

```bash
# Semantic search across all indexed code
nx search "authentication token validation"

# Scope to a specific collection type
nx search "caching strategy" --corpus docs
nx search "retry logic" --corpus code__myrepo

# Synthesize a cited answer via Haiku
nx search "how does session management work" -a

# Output formats for editor integration
nx search "validate_token" --vimgrep    # path:line:col:content
nx search "validate_token" --json       # JSON array
```

### Index and search PDFs and Markdown

```bash
nx index pdf ~/papers/architecture.pdf --corpus my-papers
nx index md  ~/notes/decisions.md      --corpus notes

nx search "distributed consensus" --corpus docs
```

### Persist agent outputs

```bash
# Store a finding permanently in T3
nx store analysis.md --collection knowledge --tags "security,audit"
echo "# Key insight..." | nx store - --collection knowledge --title "Auth Analysis"

# Store with a TTL
nx store temp-notes.md --collection knowledge --ttl 30d

# Search stored knowledge
nx search "security vulnerabilities" --corpus knowledge
```

## Storage tiers

### T1 — Session scratch (in-memory)

Fast, ephemeral, no API calls. Cleared when the session ends.

```bash
nx scratch put "working hypothesis: the cache is stale"
nx scratch search "cache"
nx scratch list
nx scratch flag <id>          # mark for auto-flush to T2 at session end
nx scratch promote <id> --project myrepo --title findings.md  # flush immediately
nx scratch clear
```

Uses ChromaDB's bundled MiniLM-L6-v2 model (local ONNX, no network round-trip).

### T2 — Memory bank (local SQLite)

Survives restarts. Backed by SQLite with FTS5 for keyword search.

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
nx memory expire             # remove TTL-expired entries

# Promote to T3 for semantic search
nx memory promote <id> --collection knowledge
```

TTL format: `30d`, `4w`, `permanent`. Default: `30d`.

### T3 — Permanent knowledge (ChromaDB cloud)

Semantic search via Voyage AI. Collections namespaced by type:
- `code__<repo>` — indexed code repositories
- `docs__<corpus>` — indexed PDFs and markdown
- `knowledge__<topic>` — agent outputs and stored knowledge

## Project management

`nx pm` provides structured project lifecycle management backed by T2 (no new infra).

```bash
# Initialise PM docs for the current git repo
nx pm init

# Resume context in a new session
nx pm resume

# Track project state
nx pm status
nx pm block "waiting on API approval"
nx pm unblock 1

# Phase transitions
nx pm phase next

# Keyword search across all PM docs (FTS5, no API call)
nx pm search "what did we decide about caching"

# Archive: synthesize to T3 via Haiku, start T2 decay
nx pm archive
nx pm close              # archive + mark completed

# Restore within the 90-day decay window
nx pm restore myrepo

# Search archived project syntheses (semantic, across all past projects)
nx pm reference "how did we handle rate limiting"
nx pm reference myrepo   # retrieve by project name
```

Auto-detect project name from `git rev-parse --show-toplevel`. Override with `--project <name>`.

## Advanced search options

```bash
# Hybrid search: semantic + ripgrep frecency weighting (code corpora only)
nx search "token validation" --hybrid

# Multi-corpus search (independent retrieval + Voyage reranker merge)
nx search "auth flow" --corpus code --corpus docs

# Agentic mode: Haiku-driven multi-step query refinement (up to 3 iterations)
nx search "session expiry" --agentic

# Fan-out to Mixedbread-indexed collections (read-only, requires MXBAI_API_KEY)
nx search "recent papers on retrieval" --mxbai

# Metadata filters
nx search "caching" --where store_type=pm-archive

# Suppress Voyage reranker, use round-robin merge instead
nx search "auth" --corpus code --corpus docs --no-rerank
```

## Server management

```bash
nx serve start [--port 7890]
nx serve stop
nx serve status          # show indexed repos, indexing progress, uptime
nx serve logs            # tail ~/.config/nexus/serve.log
```

## Collection management

```bash
nx collection list                      # all T3 collections with doc counts
nx collection delete <name> --confirm
```

## Configuration

```bash
nx config list                          # show all credentials and settings
nx config set server.port 7891
nx config set embeddings.rerankerModel rerank-2.5-lite   # lower cost
```

Global config file: `~/.config/nexus/config.yml`.
Per-repo config: `.nexus.yml` (merged over global).

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `server.port` | `7890` | HTTP port for `nx serve` |
| `server.headPollInterval` | `10` | Seconds between HEAD checks per repo |
| `embeddings.codeModel` | `voyage-code-3` | Voyage model for code collections |
| `embeddings.docsModel` | `voyage-4` | Voyage model for docs/knowledge |
| `embeddings.rerankerModel` | `rerank-2.5` | Voyage reranker for cross-corpus merge |
| `pm.archiveTtl` | `90` | Days before archived PM docs decay from T2 |
| `mxbai.stores` | `[]` | Mixedbread store IDs for `--mxbai` fan-out |

## Development

```bash
uv sync
uv run pytest               # 280 tests, no API keys required
uv run pytest -m integration  # skip unless real keys are set
```

E2E tests use `chromadb.EphemeralClient` + `DefaultEmbeddingFunction` (bundled ONNX) — no API keys needed for the full test suite.

## Architecture

```
T1  chromadb.EphemeralClient + DefaultEmbeddingFunction (MiniLM-L6-v2, local ONNX)
T2  sqlite3 + FTS5, WAL mode — ~/.config/nexus/memory.db
T3  chromadb.CloudClient + VoyageAIEmbeddingFunction — ChromaDB cloud

Indexing pipelines:
  code   git frecency → tree-sitter AST chunking → voyage-code-3 → T3 code__<repo>
  PDF    PyMuPDF4LLM / pdfplumber / OCR → voyage-4 → T3 docs__<corpus>
  MD     SemanticMarkdownChunker + SHA256 sync → voyage-4 → T3 docs__<corpus>

Search:
  semantic    ChromaDB vector similarity (per-corpus, then Voyage rerank-2.5 merge)
  hybrid      semantic + ripgrep frecency (0.7 × vector + 0.3 × frecency, code only)
  answer      retrieval → Haiku synthesis → cited output (<cite i="N">)
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
