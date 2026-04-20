# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

<a href="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/tensegrity-69e5247d907e5.png?w=1024&ssl=1">
  <img src="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/tensegrity-69e5247d907e5.png?w=480&ssl=1" alt="The steam-driven semantic engine — illustration from the Nexus by Example blog post" align="right" width="320" />
</a>

Nexus is a lightweight knowledge management system for AI coding agents. It provides persistent memory and semantic search through tiered storage that preserves decisions, findings, and project knowledge across agent sessions. That knowledge compounds over time, becoming more valuable as the corpus grows.

Nexus includes RDR (Research-Design-Review), an integrated human-AI design and audit system. RDRs capture the reasoning behind technical decisions — problem, research, chosen approach, rejected alternatives — as structured, searchable documents that live in the repository alongside the code. Nexus indexes the RDR corpus so team members and their agents can quickly get up to speed on a project's design history and stay aligned as the codebase evolves.

> **New to Nexus?** Read [**Nexus by Example**](https://tensegrity.blog/2026/04/19/nexus-by-example/) on the Tensegrity blog — a guided walkthrough of how the pieces fit together in practice.

## What it does

**Semantic search.** Standard text search matches exact strings. Nexus matches by meaning: querying "how does authentication work" returns the auth middleware, the login handler, and the JWT validation — even when none contain the word "authentication."

```bash
nx index repo .                  # index current repo
nx search "error handling"       # finds try/catch, Result types, error middleware, logging
nx search "auth" --hybrid        # combine semantic + keyword matching
```

**Persistent memory.** Agents share ephemeral session context for inter-agent coordination, project-level decisions persist locally with full-text search, and cross-project knowledge is stored permanently with semantic retrieval.

```bash
nx scratch put "the bug is in the retry logic"    # T1: inter-agent session context
nx memory put --project myapp --title "DB choice"  "Chose Postgres over SQLite for concurrency"
nx store put --collection knowledge__myapp "API rate limit is 10k/min per the vendor docs"
```

**Analytical queries.** The `query` MCP tool handles catalog-aware scoped search in a single call — `query(question="...", author="Fagin")` searches only that author's collections. For complex multi-step analysis (compare, extract, generate), the `/nx:query` skill routes through three paths: direct query, template match, or planner dispatch.

```bash
# Filter indexed PDFs by bibliographic metadata
nx search "consensus protocols" --where bib_year>=2024 --where chunk_type=table_page

# Backfill bibliographic metadata on an existing collection
nx enrich knowledge__papers --delay 0.5
```

**Topic taxonomy.** After indexing, Nexus automatically discovers topics across your corpus using HDBSCAN clustering, then labels each cluster with a human-readable name via Claude Haiku. Search results are grouped by topic and boosted for relevance. Works with both local and cloud embeddings — no configuration needed.

```bash
nx index repo .                  # indexing triggers topic discovery automatically
nx taxonomy status               # see discovered topics
nx taxonomy review               # accept, rename, merge, or split topics interactively
```

**Decision tracking.** RDR documents record the reasoning behind technical choices and are searchable alongside code, so prior decisions surface automatically during new design work.

## Quick Start

Requires Python 3.12–3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install conexus          # install the nx CLI
nx doctor                        # verify installation
nx index repo .                  # index your repo + discover topics (no API keys needed)
nx search "what does X do"       # semantic search with topic grouping, fully local
nx taxonomy status               # see discovered topics
```

Update: `uv tool update conexus`

Works immediately with local ONNX embeddings — no accounts, no API keys. For higher-quality cloud embeddings (Voyage AI), see the [cloud setup instructions](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md#cloud-mode-optional).

For Claude Code, install the plugin:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

See [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) for the full walkthrough.

## Three tiers, one lifecycle

Different information has different lifetimes. Together the three tiers form an integrated memory system that extends agent context across sessions and projects.

| Tier | Purpose | Storage | API keys? |
|------|---------|---------|-----------|
| **Scratch** (T1) | Inter-agent session context — coordination and knowledge sharing across agent invocations | In-memory ChromaDB | No |
| **Memory** (T2) | Project-level persistence with full-text search | Local SQLite + FTS5 | No |
| **Knowledge** (T3) | Permanent semantic knowledge — code, papers, docs, decisions searchable by meaning | Local ChromaDB (default) or ChromaDB Cloud + Voyage AI | No (local) / Yes (cloud) |

Agents use all three tiers cooperatively. T1 enables inter-agent communication — sharing findings and preventing duplicate work within a session. T2 provides project decisions that constrain solutions. T3 surfaces how similar problems were resolved in other contexts. As the T3 knowledge base grows, it becomes the project's institutional memory — managing the information overload that accompanies complex designs and long-lived codebases.

## What you can index

Code, documents, PDFs, and manual knowledge entries — anything that benefits from semantic search:

```bash
nx index repo .                          # code + docs + RDRs from a git repo
nx index pdf paper.pdf --collection knowledge__ml  # reference papers
nx store put --collection knowledge__ops "Redis maxmemory-policy: allkeys-lru for cache, noeviction for queues"
```

Repository indexing (`nx index repo`) is the most automated path. It classifies git-tracked files, chunks code into logical pieces via tree-sitter AST parsing across 31 languages, and embeds each chunk using local ONNX models by default, or Voyage AI models in cloud mode. Recently-touched files rank higher via git frecency scoring.

PDF indexing auto-detects math-heavy papers and routes them through the best available extractor. Install `uv pip install 'conexus[mineru]'` for superior LaTeX extraction of equations. Without MinerU, Docling extracts all PDFs normally and flags formula regions in chunk metadata. See [PDF Extraction Backends](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md#pdf-extraction-backends) for details.

See [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) for details and `.nexus.yml` configuration.

## RDR: Research-Design-Review

Complex features are bigger than what fits in working memory — yours or an LLM's. Without a locked specification, purpose drift sets in during implementation. An RDR is a specification document written *before* coding: it captures the problem, the research journey, competing options, and the chosen approach. Once accepted, the RDR is the stable target that implementation builds against. If the design proves wrong, abandon the code and iterate the RDR.

Each research finding is tagged with its evidence quality — verified against source code, supported by documentation only, or assumed — so readers know which conclusions are load-bearing and which need validation. The growing corpus is searchable, so prior decisions surface automatically when starting new design work.

RDR is fully optional. See [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) for the full process.

## Claude Code plugin

The `nx/` directory is a Claude Code plugin that gives agents access to everything above. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

The plugin provides 13 specialized agents, 43 skills covering the RDR lifecycle, plan-centric retrieval, and development workflows, session hooks for automatic context initialization, and 36 MCP tools split across two focused servers — `nexus` (26 tools: search, store, memory, scratch, plans, 5 operator tools for structured extract/rank/compare/summarize/generate, plus 4 orchestration tools including `nx_answer` for plan-matched multi-step retrieval) and `nexus-catalog` (10 catalog tools: search, show, link, resolve, stats, etc.). The plan-centric retrieval layer (`nx_answer`) matches questions against a library of scenario templates, executes the matched plan, and records every run — so the library compounds with use. Agents search indexed code before proposing changes, check prior RDR decisions before designing new features, and coordinate through standard pipelines (plan → implement → review → test) with built-in quality gates.

The plugin integrates with [Beads](https://github.com/BeadsProject/beads) for task tracking. See [nx/README.md](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) for the full plugin documentation.

## CLI Reference

The `nx` command provides direct access to all storage tiers, indexing, and search.

| Command | What it does |
|---------|-------------|
| `nx search` | Semantic and hybrid search across indexed code, docs, and knowledge. Supports `--where` with `=`, `>=`, `<=`, `>`, `<`, `!=` operators for metadata filtering |
| `nx index` | Index git repos, PDFs, and markdown into searchable collections |
| `nx enrich` | Backfill bibliographic metadata (year, venue, authors, citations) from Semantic Scholar |
| `nx store` | Store, retrieve, export, and import knowledge entries |
| `nx memory` | Per-project persistent notes (local, no API keys) |
| `nx scratch` | Inter-agent session context (in-memory, no API keys) |
| `nx collection` | Inspect and manage cloud collections |
| `nx config` | Credentials and settings |
| `nx upgrade` | Run pending database migrations and T3 upgrade steps |
| `nx doctor` | Health check — verifies dependencies, credentials, connectivity, schema |
| `nx hooks` | Install git hooks for automatic re-indexing on commit |
| `nx catalog` | Document registry — search, link, audit the catalog metadata layer |
| `nx taxonomy` | Topic taxonomy — discover, project across collections, review, merge, split |
| `nx context` | Project context cache — topic map for agent cold-start acceleration |
| `nx mineru` | MinerU server lifecycle (start/stop/status) for PDF extraction |

Full details: [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md).

## Documentation

| Document | What it covers |
|----------|---------------|
| [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) | Install, local usage, Claude Code plugin, semantic search setup |
| [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) | Every command, every flag |
| [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md) | T1/T2/T3 architecture and data flow |
| [Memory and Tasks](https://github.com/Hellblazer/nexus/blob/main/docs/memory-and-tasks.md) | T2 memory, beads integration, session context |
| [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) | File classification, chunking pipeline, frecency scoring |
| [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) | Config hierarchy, .nexus.yml, tuning parameters |
| [Architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md) | Module map, design decisions |
| [Contributing](https://github.com/Hellblazer/nexus/blob/main/docs/contributing.md) | Dev setup, testing, code style |
| [Nexus by Example](https://tensegrity.blog/2026/04/19/nexus-by-example/) | Blog walkthrough — the steam-driven semantic engine in practice |

**RDR (Research-Design-Review):**
1. [Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) — What RDRs are, when to write one, evidence classification
2. [Workflow](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-workflow.md) — Create → Research → Gate → Accept → Close
3. [Nexus Integration](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-nexus-integration.md) — How storage tiers and agents amplify RDRs
4. [Templates](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-templates.md) — Minimal and full examples, post-mortem template
5. [Project RDR Index](https://github.com/Hellblazer/nexus/blob/main/docs/rdr/README.md) — All project RDRs with status

## Prerequisites

- Python 3.12–3.13, [`uv`](https://docs.astral.sh/uv/), `git`
- For the Claude Code plugin: [Node.js](https://nodejs.org/) (provides `npx`) — the bundled `sequential-thinking` and `context7` MCP servers are spawned via `npx`. The `nx` CLI alone does not need it.
- For cloud embeddings (optional): [ChromaDB Cloud](https://www.trychroma.com/) + [Voyage AI](https://www.voyageai.com/) accounts (free tiers available)
- For hybrid search: [`ripgrep`](https://github.com/BurntSushi/ripgrep)

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE).
