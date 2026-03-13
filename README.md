# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

AI coding agents lose all context between sessions — prior decisions, research findings, and accumulated project knowledge vanish with each new conversation. Nexus addresses this with a lightweight knowledge management system that provides persistent memory and semantic search across code, projects, and accumulated knowledge. Agents share the same tiered storage, so context is shared across agent invocations and sessions.

Nexus includes RDR (Research-Design-Review), an integrated human-AI design and audit system. RDRs capture the reasoning behind technical decisions — problem, research, chosen approach, rejected alternatives — as structured, searchable documents that live in the repository alongside the code. Nexus indexes the RDR corpus so team members and their agents can quickly get up to speed on a project's design history and stay aligned as the codebase evolves.

## What it does

**Semantic search.** Standard text search matches exact strings. Nexus matches by meaning: querying "how does authentication work" returns the auth middleware, the login handler, and the JWT validation — even when none contain the word "authentication."

```bash
nx index repo .                  # index current repo
nx search "error handling"       # finds try/catch, Result types, error middleware, logging
nx search "auth" --hybrid        # combine semantic + keyword matching
```

**Persistent memory.** Session scratch, project-level decisions, and cross-project knowledge — each at the appropriate level of permanence.

```bash
nx scratch put "the bug is in the retry logic"    # session-scoped, shared across agents
nx memory put --project myapp --title "DB choice"  "Chose Postgres over SQLite for concurrency"
nx store put --collection knowledge__myapp "API rate limit is 10k/min per the vendor docs"
```

**Decision tracking.** RDR documents record the reasoning behind technical choices and are searchable alongside code, so prior decisions surface automatically during new design work.

## Quick Start

```bash
uv tool install conexus          # install the nx CLI
nx config init                   # set up API keys
nx doctor                        # verify everything works

nx index repo .                  # index your repo
nx search "what does X do"       # search it
```

Scratch (`nx scratch`) and memory (`nx memory`) work with **zero API keys** — fully local. Semantic search requires [ChromaDB](https://www.trychroma.com/) and [Voyage AI](https://www.voyageai.com/) accounts (free tiers available). See [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) for the full walkthrough.

## Three tiers, one lifecycle

Different information has different lifetimes. Together the three tiers form an integrated memory system that extends agent context across sessions and projects.

| Tier | Purpose | Storage | API keys? |
|------|---------|---------|-----------|
| **Scratch** (T1) | Ephemeral session context shared across all agents in a session | In-memory ChromaDB | No |
| **Memory** (T2) | Project-level persistence with full-text search | Local SQLite + FTS5 | No |
| **Knowledge** (T3) | Permanent semantic knowledge — code, papers, docs, decisions searchable by meaning | ChromaDB cloud + Voyage AI | Yes (free tier) |

Agents use all three tiers cooperatively. T1 scratch prevents duplicate work across sibling agents within a session. T2 provides project decisions that constrain solutions. T3 surfaces how similar problems were resolved in other contexts.

## The CLI

| Command | What it does |
|---------|-------------|
| `nx search` | Semantic and hybrid search across indexed code, docs, and knowledge |
| `nx index` | Index git repos, PDFs, and markdown into searchable collections |
| `nx store` | Store, retrieve, export, and import knowledge entries |
| `nx memory` | Per-project persistent notes (local, no API keys) |
| `nx scratch` | Ephemeral session scratch pad (in-memory, no API keys) |
| `nx collection` | Inspect and manage cloud collections |
| `nx config` | Credentials and settings |
| `nx doctor` | Health check — verifies dependencies, credentials, connectivity |
| `nx hooks` | Install git hooks for automatic re-indexing on commit |

Full details: [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md).

## What you can index

Code, documents, PDFs, and manual knowledge entries — anything that benefits from semantic search:

```bash
nx index repo .                          # code + docs + RDRs from a git repo
nx index pdf paper.pdf --collection knowledge__ml  # reference papers
nx store put --collection knowledge__ops "Redis maxmemory-policy: allkeys-lru for cache, noeviction for queues"
```

Repository indexing (`nx index repo`) is the most automated path. It classifies git-tracked files, chunks code into logical pieces via tree-sitter AST parsing across 31 languages, and embeds each chunk with purpose-matched Voyage AI models. Recently-touched files rank higher via git frecency scoring.

See [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) for details and `.nexus.yml` configuration.

## RDR: Research-Design-Review

Technical decisions made during rapid development lose their reasoning quickly. An RDR captures the problem, investigation, chosen approach, and rejected alternatives. Each finding is classified by evidence quality:

| Tag | Meaning |
|-----|---------|
| **Verified** | Confirmed via source code search or working spike |
| **Documented** | Supported by external docs only |
| **Assumed** | Unverified — flag it if your design depends on it |

RDRs are iterative: write, build, learn, revise. The growing corpus remains searchable, so prior decisions surface automatically when starting new design work.

RDR is fully optional. See [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) for the full process.

## Claude Code plugin

The `nx/` directory is a Claude Code plugin that gives agents access to everything above. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

The plugin provides 15 specialized agents, 28 skills covering the RDR lifecycle and development workflows, session hooks for automatic context initialization, and MCP servers for structured storage access. Agents search indexed code before proposing changes, check prior RDR decisions before designing new features, and coordinate through standard pipelines (plan → implement → review → test) with built-in quality gates.

The plugin integrates with [Beads](https://github.com/BeadsProject/beads) for task tracking. See [nx/README.md](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) for the full plugin documentation.

## Documentation

| Document | What it covers |
|----------|---------------|
| [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) | Install, configure, first index and search |
| [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) | Every command, every flag |
| [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md) | T1/T2/T3 architecture and data flow |
| [Memory and Tasks](https://github.com/Hellblazer/nexus/blob/main/docs/memory-and-tasks.md) | T2 memory, beads integration, session context |
| [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) | File classification, chunking pipeline, frecency scoring |
| [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) | Config hierarchy, .nexus.yml, tuning parameters |
| [Architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md) | Module map, design decisions |
| [Contributing](https://github.com/Hellblazer/nexus/blob/main/docs/contributing.md) | Dev setup, testing, code style |

**RDR (Research-Design-Review):**
1. [Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) — What RDRs are, when to write one, evidence classification
2. [Workflow](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-workflow.md) — Create → Research → Gate → Accept → Close
3. [Nexus Integration](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-nexus-integration.md) — How storage tiers and agents amplify RDRs
4. [Templates](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-templates.md) — Minimal and full examples, post-mortem template
5. [Project RDR Index](https://github.com/Hellblazer/nexus/blob/main/docs/rdr/README.md) — All 36 project RDRs with status

## Prerequisites

- Python 3.12+, [`uv`](https://docs.astral.sh/uv/), `git`
- For semantic search: [ChromaDB cloud](https://www.trychroma.com/) + [Voyage AI](https://www.voyageai.com/) (free tiers available)
- For hybrid search: [`ripgrep`](https://github.com/BurntSushi/ripgrep)

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE).
