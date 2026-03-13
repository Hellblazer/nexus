# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

AI coding agents are powerful, but they forget everything between sessions. They can't recall what you decided last week, what your teammate learned about the API, or that you already tried the approach they're about to suggest. Every session starts from zero.

Nexus gives agents — and you — persistent memory and semantic search across your code, your projects, and your accumulated knowledge. Not just "better grep." A lightweight knowledge management system where agents and humans share context that survives beyond a single conversation.

## What it does

**Search by meaning, not just keywords.** `grep` finds exact strings. Nexus finds *concepts*: "how does authentication work?" returns the auth middleware, the login handler, and the JWT validation — even if none contain the word "authentication."

```bash
nx index repo .                  # index current repo
nx search "error handling"       # finds try/catch, Result types, error middleware, logging
nx search "auth" --hybrid        # combine semantic + keyword matching
```

**Remember things at every scale.** Quick notes for this session, project decisions that persist across months, reference material searchable across all your work — each at the right level of permanence.

```bash
nx scratch put "the bug is in the retry logic"    # session-scoped, shared across agents
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

## Three tiers, one lifecycle

Each tier exists because different information has different lifetimes and different access patterns. Together they form an integrated memory system that agents use synergistically — extending Claude's context across sessions, across projects, and across knowledge islands that would otherwise stay siloed.

| Tier | Purpose | Storage | API keys? |
|------|---------|---------|-----------|
| **Scratch** (T1) | Ephemeral session context — shared across all agents in a session, gone when you're done | In-memory ChromaDB | No |
| **Memory** (T2) | Project-level persistence — decisions, context, findings with full-text search | Local SQLite + FTS5 | No |
| **Knowledge** (T3) | Permanent semantic knowledge — code, papers, docs, decisions searchable by meaning across all projects | ChromaDB cloud + Voyage AI | Yes (free tier) |

T1 and T2 work with zero setup — no API keys, no accounts, fully local. T3 adds semantic search when you're ready: `nx config init` to connect, then index what matters to you.

An agent debugging a problem writes its hypotheses to T1 scratch so sibling agents don't repeat work. It checks T2 for project decisions that constrain the fix. It searches T3 for how similar problems were solved elsewhere. Each tier contributes context that no single conversation could hold.

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

T3 isn't limited to code. Anything you want searchable by meaning can go in:

```bash
nx index repo .                          # code + docs + RDRs from a git repo
nx index pdf paper.pdf --collection knowledge__ml  # reference papers
nx store put --collection knowledge__ops "Redis maxmemory-policy: allkeys-lru for cache, noeviction for queues"
```

**Repository indexing** (`nx index repo`) is the most automated path: it walks git-tracked files, classifies them (code, prose, PDF), chunks code into logical pieces via tree-sitter AST parsing across 31 languages, and embeds each chunk with purpose-matched Voyage AI models. Recently-touched files rank higher via git frecency scoring.

See [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) for details and `.nexus.yml` configuration.

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
