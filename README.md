# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Nexus is a support framework and tooling foundation for systematic development with evolving semantic knowledge. It gives teams and individual developers a way to index what they build, search what they know, and maintain coherent direction as projects grow in complexity — especially under the speed and pressure of LLM-driven agentic coding.

Three layers, each useful on its own, each building on the last:

1. **Semantic search and repository indexing** (`nx`) — Index any repo: code is chunked with tree-sitter AST parsing, prose with semantic markdown splitting, PDFs with layout-aware extraction. Search across all of it with a single command. Three storage tiers let you start local with zero API keys and add cloud search when you're ready.

2. **A Claude Code plugin** (`nx/`) — 14 agents, 27 skills, session hooks, slash commands, and a bundled MCP server. Agents search indexed code before proposing changes and coordinate through standard pipelines (plan, implement, review, test). Works with the CLI; does not require RDR.

3. **A structured decision framework** ([RDR](docs/rdr/README.md)) — Research-Design-Review documents: the traction control for agentic development. Each decision is a short document with classified evidence (Verified, Documented, or Assumed). Write one, build it, learn something, write another. Nexus keeps the growing corpus searchable and navigable. Fully optional.

Use just the CLI. Add the plugin. Adopt RDR later, or never. Each layer amplifies the ones below it but none requires the ones above.

## Quick Start

```bash
uv tool install conexus          # install the nx CLI from PyPI

nx config init                   # configure API keys
nx doctor                        # verify setup

nx index repo .                  # index current repo
nx search "authentication flow"  # semantic search
nx search "auth" --hybrid        # semantic + git frecency
```

Scratch and memory commands work with zero API keys. Cloud search requires [ChromaDB](https://www.trychroma.com/) and [Voyage AI](https://www.voyageai.com/) accounts — both offer free tiers that cover all typical Nexus usage. See [Getting Started](docs/getting-started.md).

## Repository Indexing

`nx index repo` is the core of Nexus. Point it at any git repo and it:

1. Walks tracked files via `git ls-files` (respects `.gitignore`)
2. Classifies each file by extension — code, prose, or PDF
3. Chunks code with tree-sitter AST parsing (19 languages, 27 file types) and prose with semantic markdown splitting
4. Embeds each chunk with a purpose-built Voyage AI model
5. Routes results to separate collections: `code__<repo>`, `docs__<repo>`, and `rdr__<repo>` for RDR documents
6. Computes git frecency scores so recently-touched files rank higher in hybrid search

Auto-discovers RDR documents in `docs/rdr/` and indexes them into a dedicated collection. Stable across git worktrees. Configurable per-repo via `.nexus.yml`. See [Repo Indexing](docs/repo-indexing.md).

## The CLI

Three storage tiers with increasing durability:

| Tier | Storage | Network | Use |
|------|---------|---------|-----|
| **T1 — scratch** | In-memory ChromaDB | None | Session-scoped working notes |
| **T2 — memory** | Local SQLite + FTS5 | None | Per-project notes that survive restarts |
| **T3 — knowledge** | ChromaDB cloud + Voyage AI | Required | Permanent semantic search |

Every command targets one or more tiers:

| Command | Tier | Description |
|---------|------|-------------|
| `nx search` | T3 | Semantic and hybrid search |
| `nx index` | T3 | Index code repos, PDFs, and markdown |
| `nx store` | T3 | Store knowledge in the cloud |
| `nx memory` | T2 | Per-project persistent notes |
| `nx scratch` | T1 | Ephemeral session scratch pad |
| `nx collection` | T3 | Inspect and manage cloud collections |
| `nx config` | — | Credentials and settings |

## RDR: Research-Design-Review

Agentic coding is like driving on slick ice — fast, powerful, and easy to lose control of. RDR is the traction control: a short document where you state the problem, record what you know, describe the plan, and note what you rejected. It gives humans a structured way to steer LLM-driven development and feed discoveries back into the next decision.

Each finding is classified so readers know what is solid and what is a guess:

| Classification | Meaning |
|---|---|
| **Verified** | Confirmed via source code search or working spike |
| **Documented** | Supported by external documentation only |
| **Assumed** | Unverified — flag it if your design depends on it |

RDRs are iterative, not waterfall. You write one, build it, learn something, and write another. A real project might produce 20+ RDRs over its lifetime — foundation decisions, mid-project pivots when assumptions break, performance fixes from real usage, quality refinements from actual data. Each one builds on what you learned implementing the last.

That many design documents create an information management problem. Nexus handles it: every RDR is semantically searchable the moment it is committed, metadata is queryable without parsing markdown, and agents receive prior-art context automatically. The corpus stays navigable as it grows. See [RDR Overview](docs/rdr-overview.md).

## The Plugin

The `nx/` directory is a Claude Code plugin. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

The plugin provides:

- **14 agents** — code review, debugging, architecture planning, research synthesis, strategic planning, and more
- **27 skills** — RDR workflow, TDD discipline, brainstorming gates, nexus CLI reference
- **Session hooks** — auto-initialize scratch, surface T2 memory context, health-check dependencies, prime beads
- **Slash commands** — `/research`, `/create-plan`, `/review-code`, `/rdr-create`, `/rdr-accept`, etc.
- **Standard pipelines** — feature, bug, and research workflows with built-in review gates
- **Bundled MCP server** — sequential-thinking via `.mcp.json`, no separate install

Each agent runs on a model matched to its task: opus for complex reasoning, sonnet for implementation, haiku for utility. The plugin integrates with [Beads](https://github.com/BeadsProject/beads) for task-level tracking — session hooks prime bead context, RDR close decomposes decisions into beads, and branch naming ties back to bead IDs. See [nx/README.md](nx/README.md) for the full plugin documentation.

## Documentation

| Document | What it covers |
|----------|---------------|
| [Getting Started](docs/getting-started.md) | Install, configure, first index and search |
| [CLI Reference](docs/cli-reference.md) | Every command, every flag |
| [Storage Tiers](docs/storage-tiers.md) | T1/T2/T3 architecture and data flow |
| [Memory and Tasks](docs/memory-and-tasks.md) | T2 memory, beads integration, session context |
| [Repo Indexing](docs/repo-indexing.md) | Smart file classification, chunking, frecency |
| [Configuration](docs/configuration.md) | Config hierarchy, .nexus.yml, settings |
| [Architecture](docs/architecture.md) | Module map, design decisions |
| [Contributing](docs/contributing.md) | Dev setup, testing, code style |

**RDR (Research-Design-Review):** [Overview](docs/rdr-overview.md) · [Workflow](docs/rdr-workflow.md) · [Nexus Integration](docs/rdr-nexus-integration.md) · [Templates](docs/rdr-templates.md) · [Project RDR Index](docs/rdr/README.md)

## Prerequisites

- Python 3.12+, [`uv`](https://docs.astral.sh/uv/) (for install), `git` (for repo indexing)
- [ChromaDB cloud](https://www.trychroma.com/) + [Voyage AI](https://www.voyageai.com/) for T3
- `ripgrep` for hybrid search

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
