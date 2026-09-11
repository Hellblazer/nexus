# Nexus

**Persistent memory and semantic search for Claude.** Three storage tiers that survive across sessions, an event-sourced document catalog with typed links, and a specification-before-code workflow for tracking decisions. Local-first; no API keys required. Knowledge compounds across conversations instead of evaporating when the window closes.

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

<img src="web/assets/a-stately-pleasure-dome.png" alt="A brass-ribbed crystal dome on a hilltop at dusk" align="right" width="320" />

## Start here

Three pages, in order:

- [**Install**](https://hellblazer.github.io/nexus/) — set up Nexus for how you use Claude: Claude Desktop, Claude Code, or Claude Cowork.
- [**Getting started**](https://hellblazer.github.io/nexus/getting-started.html) — learn the tooling, eleven lessons done inside Claude Code.
- [**Working with RDRs**](https://hellblazer.github.io/nexus/rdr.html) — record a decision with an RDR, eight lessons on the practised cycle.
- [**Research with Nexus**](https://hellblazer.github.io/nexus/research.html) — how the store grounds answers, and what it does for each step of research work.

## Prerequisites

Python 3.12–3.13 (3.14 not yet supported — [upstream dependency issue](https://github.com/pydantic/pydantic/issues)), [`uv`](https://docs.astral.sh/uv/), `git`. For hybrid search, [`ripgrep`](https://github.com/BurntSushi/ripgrep). For the Claude Code plugin, [Node.js](https://nodejs.org/) (the bundled `sequential-thinking` and `context7` servers spawn via `npx`).

## Install for Claude

One host package, the `nx` CLI (`conexus` on PyPI), serves three surfaces. The [install guide](https://hellblazer.github.io/nexus/) walks each one with copyable commands; the short form:

- **Claude Desktop**: download `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) and double-click it. Requires [uv](https://docs.astral.sh/uv/) on the host.
- **Claude Code**:
  ```bash
  uv tool install conexus                  # the nx CLI; the plugin's MCP servers are this package
  nx self install                          # move it onto the generation layout
  nx init                                  # provision the local service (Postgres + pgvector + bge-768)
  /plugin marketplace add Hellblazer/nexus
  /plugin install conexus@nexus-plugins
  ```
  The CLI must be installed first: `/plugin install` alone leaves the servers unable to launch.
- **Claude Cowork**: works once the plugin is installed in Claude Code on the host.

The full deployment story across all three surfaces is [docs/desktop-deployment.md](docs/desktop-deployment.md).

## What it does

- **Persistent memory** — three storage tiers (T1 session scratch, T2 memory bank, T3 semantic knowledge store, both persistent tiers served by the native Postgres-backed `nexus-service`) so Claude remembers across conversations.
- **Semantic search** — index your code, docs, RDRs, and PDFs once; search by meaning afterward. Tree-sitter AST chunking across 31 languages, CCE prose chunking, PDF auto-routing.
- **Typed document catalog** — Xanadu-inspired addressing with typed links (`cites`, `implements`, `supersedes`). Walk from a design doc to the code that implements it.
- **RDR: Research-Design-Review** — write a spec before you code. Captures the problem, research, alternatives, and chosen approach. The corpus is searchable, so prior decisions surface during new design work.
- **Local-first** — runs entirely on your machine: an on-device bge-768 ONNX embedder over a bundled Postgres 17 + pgvector service that `nx init` provisions for you. Voyage AI (server-side embeddings) is opt-in for the managed-cloud deployment.

## Learn

[Getting started](https://hellblazer.github.io/nexus/getting-started.html) — eleven lessons, done inside Claude Code:

1. [Start a session](https://hellblazer.github.io/nexus/getting-started.html#l1)
2. [End a session, start the next one](https://hellblazer.github.io/nexus/getting-started.html#l2)
3. [Index your repository, then search it](https://hellblazer.github.io/nexus/getting-started.html#l3)
4. [Memory: what Claude keeps between sessions](https://hellblazer.github.io/nexus/getting-started.html#l4)
5. [Scratch: how Claude and its agents share notes](https://hellblazer.github.io/nexus/getting-started.html#l5)
6. [Knowledge: notes, documents, and the catalog](https://hellblazer.github.io/nexus/getting-started.html#l6)
7. [Explore: research, links, and the topic map](https://hellblazer.github.io/nexus/getting-started.html#l7)
8. [Render and iterate](https://hellblazer.github.io/nexus/getting-started.html#l8)
9. [Questions that need many documents](https://hellblazer.github.io/nexus/getting-started.html#l9)
10. [Record a decision with an RDR](https://hellblazer.github.io/nexus/getting-started.html#l10)
11. [Maintenance](https://hellblazer.github.io/nexus/getting-started.html#l11)

[Working with RDRs](https://hellblazer.github.io/nexus/rdr.html) — eight lessons on the practised cycle:

1. [What an RDR is here](https://hellblazer.github.io/nexus/rdr.html#l1)
2. [It starts as a conversation](https://hellblazer.github.io/nexus/rdr.html#l2)
3. [Research, in cycles](https://hellblazer.github.io/nexus/rdr.html#l3)
4. [Read it, react, repeat](https://hellblazer.github.io/nexus/rdr.html#l4)
5. [The gate](https://hellblazer.github.io/nexus/rdr.html#l5)
6. [Accept, and the plan follows](https://hellblazer.github.io/nexus/rdr.html#l6)
7. [Build it, across many sessions](https://hellblazer.github.io/nexus/rdr.html#l7)
8. [Close it, and what remains](https://hellblazer.github.io/nexus/rdr.html#l8)

[Research with Nexus](https://hellblazer.github.io/nexus/research.html) — what the store does for each step of research work:

1. [What the system does](https://hellblazer.github.io/nexus/research.html#p0)
2. [Grounding](https://hellblazer.github.io/nexus/research.html#pg)
3. [Curating](https://hellblazer.github.io/nexus/research.html#p1)
4. [Reading](https://hellblazer.github.io/nexus/research.html#p2)
5. [Chronicling](https://hellblazer.github.io/nexus/research.html#p3)
6. [Coupling](https://hellblazer.github.io/nexus/research.html#p6)
7. [Asking and analyzing](https://hellblazer.github.io/nexus/research.html#p4)
8. [Synthesizing and extracting](https://hellblazer.github.io/nexus/research.html#p5)
9. [Verifying](https://hellblazer.github.io/nexus/research.html#p7)
10. [Rendering](https://hellblazer.github.io/nexus/research.html#p8)
11. [Where it ends up](https://hellblazer.github.io/nexus/research.html#p9)
12. [Appendix: topics, aspects, sections, projections](https://hellblazer.github.io/nexus/research.html#p6b)

## Updating

```bash
nx self install                          # 1. update the code (keeps your extras)
nx upgrade                               # 2. converge the service, the data, and the plugins
```

Both steps, every time. Never `uv tool install conexus` or `--force` to upgrade: that drops `[local]` and empties search; `nx self install` repairs it. Details, including what `/plugin update` does and the path for an install that never left ChromaDB, are on the site under [Update](https://hellblazer.github.io/nexus/#update).

### Telemetry

Once a day the MCP server sends one anonymous ping to the managed service so
the project can count active installs. It carries exactly six fields: a random
install id (a UUID minted on first use, stored in `~/.config/nexus/install_id`),
the conexus version, the install mode (`local` or `cloud`), OS, CPU
architecture, and Python major.minor. No hostname, no paths, no collection
names, no content. It runs on a background thread with a two second timeout
and never blocks or retries. Turn it off with either:

```bash
nx telemetry off            # writes telemetry.enabled: false to config.yml
export NX_NO_TELEMETRY=1    # or per environment
```

`nx telemetry status` shows the current setting and the last ping time.

### Something broken?

The site's [Problems](https://hellblazer.github.io/nexus/#problems) section covers the common ones. For a broken install,
[nexus-recovery-runbook](https://gist.github.com/Hellblazer/08f0a615e3d73e47d8062bce4829b611) is a
diagnose-first recovery procedure meant to be handed to a Claude Code session as its first message —
the assistant runs it phase by phase, pausing for your explicit go-ahead before anything that upgrades
or migrates data, and gathers redacted forensics + opens a GitHub issue (or emails a fallback address)
if it can't resolve things itself. It's a convenience for a broken install, not a substitute for filing
an issue directly if something looks wrong — and it carries its own guardrails (read-only diagnosis
first, no destructive commands without confirmation, no secrets ever leave the machine), but you're
trusting an LLM to run real commands against your install. Review what it does before handing it off,
especially the first time.

## Going deeper

| If you want to... | Read |
|---|---|
| Understand the architecture | [Storage Tiers](docs/storage-tiers.md), [Architecture](docs/architecture.md) |
| Install, upgrade, or uninstall the agent | [Agent Lifecycle & Operations](docs/operations/agent-lifecycle.md) |
| Use the hosted managed service | [Managed Onboarding](docs/managed-onboarding.md) |
| Write an RDR | [RDR: Research-Design-Review](docs/rdr.md) |
| Index a repo or PDFs | [Repo Indexing](docs/repo-indexing.md) |
| Configure or tune | [Configuration](docs/configuration.md) |
| Run in containers or Cowork | [Container Integration](docs/container-integration.md) |
| Back up my knowledge store | [Storage Tiers § T3 Backup and Migration](docs/storage-tiers.md#t3-backup-and-migration-exportimport) |
| Fix empty search results after upgrading | [Getting Started § Troubleshooting](docs/getting-started.md#troubleshooting) |
| Browse the docs tree | [docs/README.md](docs/README.md) |

## License

Dual-licensed. Open source under AGPL-3.0-or-later
([LICENSE](LICENSE)); commercial
licenses are available for organizations that need non-AGPL terms — see
[LICENSING.md](LICENSING.md).
