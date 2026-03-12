# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Nexus is a support framework and tooling foundation for systematic development with evolving semantic knowledge. It gives teams and individual developers a way to index what they build, search what they know, and maintain coherent direction as projects grow in complexity — especially under the speed and pressure of LLM-driven agentic coding.

Three layers, each useful on its own, each building on the last:

1. **Semantic search and repository indexing** (`nx`) — Index any repo: code is chunked with tree-sitter AST parsing, prose with semantic markdown splitting, PDFs with layout-aware extraction. Search across all of it with a single command. Three storage tiers let you start local with zero API keys and add cloud search when you're ready.

2. **A Claude Code plugin** (`nx/`) — 15 agents, 28 skills, session hooks, slash commands, and two bundled MCP servers. Agents access all three storage tiers via structured MCP tools (no Bash dependency), search indexed code before proposing changes, and coordinate through standard pipelines (plan, implement, review, test). Works with the CLI; does not require RDR.

3. **A structured decision framework** ([RDR](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md)) — Research-Design-Review documents: the traction control for agentic development. Each decision is a short document with classified evidence (Verified, Documented, or Assumed). Write one, build it, learn something, write another — Nexus itself has 35+ and counting. The growing corpus stays searchable and navigable. Fully optional.

Use just the CLI. Add the plugin. Adopt RDR later, or never. Each layer amplifies the ones below it but none requires the ones above.

## Quick Start

```bash
# CLI
uv tool install conexus          # install the nx CLI from PyPI

nx config init                   # configure API keys
nx doctor                        # verify setup

nx index repo .                  # index current repo
nx search "authentication flow"  # semantic search
nx search "auth" --hybrid        # semantic + git frecency

# Claude Code plugin (optional)
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

Scratch and memory commands work with zero API keys. Cloud search requires [ChromaDB](https://www.trychroma.com/) and [Voyage AI](https://www.voyageai.com/) accounts — both offer free tiers that cover all typical Nexus usage. See [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md).

## Repository Indexing

`nx index repo` is the core of Nexus. Point it at any git repo and it:

1. Walks tracked files via `git ls-files` (respects `.gitignore`)
2. Classifies each file by extension — code, prose, or PDF
3. Chunks code with tree-sitter AST parsing (31 languages, 52 file types) and prose with semantic markdown splitting
4. Embeds each chunk with a purpose-built Voyage AI model
5. Routes results to separate collections: `code__<repo>`, `docs__<repo>`, and `rdr__<repo>` for RDR documents
6. Computes git frecency scores so recently-touched files rank higher in hybrid search

Auto-discovers RDR documents in `docs/rdr/` and indexes them into a dedicated collection. Stable across git worktrees. Configurable per-repo via `.nexus.yml`. See [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md).

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
| `nx store` | T3 | Store, export, and import knowledge |
| `nx memory` | T2 | Per-project persistent notes |
| `nx scratch` | T1 | Ephemeral session scratch pad |
| `nx collection` | T3 | Inspect and manage cloud collections |
| `nx config` | — | Credentials and settings |

## RDR: Research-Design-Review

Agentic coding is like driving on slick ice — fast, powerful, and easy to lose control of. RDR is the traction control: a short document where you state the problem, record what you find, describe the plan, and note what you rejected. It gives humans a structured way to steer LLM-driven development and feed discoveries back into the next decision.

Each finding is classified so readers know what is solid and what is a guess:

| Classification | Meaning |
|---|---|
| **Verified** | Confirmed via source code search or working spike |
| **Documented** | Supported by external documentation only |
| **Assumed** | Unverified — flag it if your design depends on it |

RDRs are iterative, not waterfall. You write one, build it, learn something, and write another. Nexus itself has produced 35+ RDRs across its development — from early architectural decisions through mid-project pivots when assumptions broke, bug investigations that uncovered framework-level issues, and quality refinements driven by real usage. Each one builds on what was learned implementing the last, and the pace doesn't slow down as the project matures. New capabilities surface new decisions.

A growing corpus of design documents creates an information management problem. Nexus handles it: every RDR is semantically searchable the moment it is committed, metadata is queryable without parsing markdown, and agents receive prior-art context automatically. When you write RDR-035, the agent already knows what RDR-023 decided and why — contradictions and superseded assumptions surface during the gate review, not after deployment. The corpus stays navigable and useful as it grows. See [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md).

## The Plugin

The `nx/` directory is a Claude Code plugin. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

The plugin provides:

- **15 agents** — code review, debugging, architecture planning, research synthesis, strategic planning, bead enrichment, and more
- **28 skills** — RDR workflow, TDD discipline, brainstorming gates, nexus CLI reference
- **Session hooks** — auto-initialize scratch, surface T2 memory context, health-check dependencies, prime beads
- **Slash commands** — `/research`, `/create-plan`, `/review-code`, `/rdr-create`, `/rdr-accept`, etc.
- **Standard pipelines** — feature, bug, and research workflows with built-in review gates
- **Two bundled MCP servers** — nexus (8 storage tier tools for agents) and sequential-thinking, both via `.mcp.json`

Each agent runs on a model matched to its task: opus for complex reasoning, sonnet for implementation, haiku for utility. The plugin integrates with [Beads](https://github.com/BeadsProject/beads) for task-level tracking — session hooks prime bead context, RDR close decomposes decisions into beads, and branch naming ties back to bead IDs. See [nx/README.md](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) for the full plugin documentation.

## Documentation

| Document | What it covers |
|----------|---------------|
| [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) | Install, configure, first index and search |
| [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) | Every command, every flag |
| [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md) | T1/T2/T3 architecture and data flow |
| [Memory and Tasks](https://github.com/Hellblazer/nexus/blob/main/docs/memory-and-tasks.md) | T2 memory, beads integration, session context |
| [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) | Smart file classification, chunking, frecency |
| [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) | Config hierarchy, .nexus.yml, settings |
| [Architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md) | Module map, design decisions |
| [Contributing](https://github.com/Hellblazer/nexus/blob/main/docs/contributing.md) | Dev setup, testing, code style |

**RDR (Research-Design-Review):** [Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) · [Workflow](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-workflow.md) · [Nexus Integration](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-nexus-integration.md) · [Templates](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-templates.md) · [Project RDR Index](https://github.com/Hellblazer/nexus/blob/main/docs/rdr/README.md)

## Prerequisites

- Python 3.12+, [`uv`](https://docs.astral.sh/uv/) (for install), `git` (for repo indexing)
- [ChromaDB cloud](https://www.trychroma.com/) + [Voyage AI](https://www.voyageai.com/) for T3
- `ripgrep` for hybrid search

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE).
