# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Nexus helps you find things in your codebase by semantic meaning, not just by name.

`grep` and IDE search find exact strings. Nexus finds *concepts*: "how does authentication work here?" returns the auth middleware, the login handler, the session management code, and the JWT validation — even if none of them contain the word "authentication." It understands what your code does, not just what it says.

That matters most when you're working with AI coding agents. An agent that can search your codebase semantically before proposing changes makes fewer mistakes, avoids reinventing things that already exist, and builds on your actual architecture instead of guessing.

## What it does

**Search by meaning.** Index any git repo. Code is parsed into logical chunks (functions, classes, methods) using tree-sitter across 31 languages. Prose and PDFs are split semantically. Everything is embedded with Voyage AI models so you search by concept, not keywords.

```bash
nx index repo .                  # index current repo
nx search "error handling"       # finds try/catch, Result types, error middleware, logging
nx search "auth" --hybrid        # combine semantic + keyword matching
```

**Remember things across sessions.** Scratch notes that disappear when you're done, project memory that persists locally, and permanent knowledge in the cloud — use whichever fits.

```bash
nx scratch put "the bug is in the retry logic"    # gone after this session
nx memory put --project myapp --title "DB choice"  "Chose Postgres over SQLite for concurrency"
nx store put --collection knowledge__myapp "API rate limit is 10k/min per the vendor docs"
```

**Track decisions, not just code.** RDR (Research-Design-Review) documents record *why* you made a choice — the problem, what you found, what you picked, what you rejected. They're searchable alongside your code so "didn't we already decide about caching?" has a real answer instead of a Slack archaeology expedition.

## Quick Start

```bash
uv tool install conexus          # install the nx CLI
nx config init                   # set up API keys
nx doctor                        # verify everything works

nx index repo .                  # index your repo
nx search "what does X do"       # search it
```

Scratch (`nx scratch`) and memory (`nx memory`) work with **zero API keys** — no accounts needed, fully local. Semantic search (`nx search`, `nx index`) requires [ChromaDB](https://www.trychroma.com/) and [Voyage AI](https://www.voyageai.com/) accounts — both offer free tiers that cover typical usage. See [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) for the full setup walkthrough.

## Storage: start local, add cloud when ready

Nexus has three storage tiers so you can start with zero setup and add capabilities as you need them:

| What you need | Tier | Storage | API keys? |
|--------------|------|---------|-----------|
| Quick notes for this session | **Scratch** (T1) | In-memory | No |
| Project notes that survive restarts | **Memory** (T2) | Local SQLite | No |
| Search across all your code and knowledge | **Knowledge** (T3) | ChromaDB cloud + Voyage AI | Yes (free tier) |

You don't need to understand tiers to use Nexus. `nx scratch` just works. `nx memory` just works. When you want semantic search, run `nx config init` to add API keys and you're in T3 territory.

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

## Repository indexing

`nx index repo` walks your git-tracked files and:

1. **Classifies** each file — code (52 extensions), prose (markdown), PDF, or skip (config, lock files)
2. **Chunks** code into logical pieces using tree-sitter AST parsing (functions, classes, methods — not arbitrary line splits)
3. **Chunks** prose at section boundaries, PDFs at layout boundaries
4. **Embeds** each chunk with a purpose-matched Voyage AI model (`voyage-code-3` for code, `voyage-context-3` for prose)
5. **Routes** to separate collections: `code__<repo>`, `docs__<repo>`, `rdr__<repo>`
6. **Scores** with git frecency so recently-touched files rank higher

Configurable per-repo via `.nexus.yml`. Stable across git worktrees. See [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md).

## RDR: Research-Design-Review

When you're coding fast — especially with AI agents — decisions happen quickly and the reasoning evaporates. A week later, nobody remembers *why* you picked Postgres over SQLite, or what the tradeoff was with the caching strategy.

An RDR is a short document: the problem, what you found, what you chose, what you rejected. Nothing more. Each finding is tagged so readers know what's solid and what's a guess:

| Tag | Meaning |
|-----|---------|
| **Verified** | Confirmed via source code search or working spike |
| **Documented** | Supported by external docs only |
| **Assumed** | Unverified — flag it if your design depends on it |

RDRs are iterative, not waterfall. Write one, build it, learn something, write another. Nexus has produced 35+ across its own development. The growing corpus stays searchable — when you start a new design, prior decisions surface automatically so you don't contradict yourself or redo settled work.

RDR is fully optional. Use it when decisions matter; skip it when they don't. Start here: [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md).

## Claude Code plugin

The `nx/` directory is a Claude Code plugin that gives agents access to everything above. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

What the plugin adds:

- **15 agents** — code review, debugging, architecture, research, strategic planning, and more — each on a model matched to its task (opus for reasoning, sonnet for implementation)
- **28 skills** — RDR lifecycle, TDD discipline, brainstorming gates, CLI reference
- **Session hooks** — auto-initialize scratch, surface project context, health-check dependencies
- **Slash commands** — `/research`, `/create-plan`, `/review-code`, `/rdr-create`, `/rdr-accept`, etc.
- **Two MCP servers** — 8 structured storage tools (agents access T1/T2/T3 without Bash) and sequential-thinking for hypothesis-driven reasoning

Agents search your indexed code before proposing changes. They check prior RDR decisions before designing new features. They coordinate through standard pipelines (plan → implement → review → test) with built-in quality gates.

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
