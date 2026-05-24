# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

<a href="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=1024&ssl=1">
  <img src="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=480&ssl=1" alt="A brass-ribbed crystal dome on a hilltop at dusk" align="right" width="320" />
</a>

**Persistent memory and semantic search for Claude.** Three storage tiers that survive across sessions, an event-sourced document catalog with typed links, and a specification-before-code workflow for tracking decisions. Local-first; no API keys required. Knowledge compounds across conversations instead of evaporating when the window closes.

## Install for Claude

Three surfaces share one host substrate. Pick the one that matches how you use Claude.

### Claude Desktop chat

Download `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) and double-click. Claude Desktop registers it under Settings → Connectors. Requires [uv](https://docs.astral.sh/uv/) on the host PATH; deps resolve on first launch (~20s).

### Claude Code (terminal)

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

The plugin ships 13 specialized agents, 43 skills (RDR lifecycle, plan-centric retrieval, dev workflows), and 36 MCP tools split across two focused servers. Session hooks load project context at startup.

### Claude Cowork

Works automatically once the conexus plugin is installed in Claude Code on the host. State round-trips bidirectionally with the host CLI through the T2 daemon.

For the full deployment story across all three surfaces (install, daemon lifecycle, drift detection, uninstall), see [docs/desktop-deployment.md](https://github.com/Hellblazer/nexus/blob/main/docs/desktop-deployment.md).

## What it does

- **Persistent memory** — three storage tiers (T1 session scratch, T2 SQLite memory bank, T3 semantic knowledge store) so Claude remembers across conversations.
- **Semantic search** — index your code, docs, RDRs, and PDFs once; search by meaning afterward. Tree-sitter AST chunking across 23 languages, CCE prose chunking, PDF auto-routing.
- **Typed document catalog** — Xanadu-inspired addressing with typed links (`cites`, `implements`, `supersedes`). Walk from a design doc to the code that implements it.
- **RDR: Research-Design-Review** — write a spec before you code. Captures the problem, research, alternatives, and chosen approach. The corpus is searchable, so prior decisions surface during new design work.
- **Local-first** — default install runs entirely on your machine with ONNX MiniLM + local ChromaDB. Voyage AI + ChromaDB Cloud are opt-in for higher-quality embeddings.

## CLI quick-start

```bash
uv tool install conexus                  # install the nx CLI
nx daemon t2 install --autostart         # register the T2 daemon (one-time)
nx doctor                                # verify installation
nx index repo .                          # index your repo + discover topics
nx search "how does retry work"          # semantic search, fully local
```

The `nx` CLI provides direct access to all storage tiers, indexing, search, the catalog, and taxonomy. See [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) for a walkthrough, [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) for every command and flag.

## Going deeper

| If you want to... | Read |
|---|---|
| Understand the architecture | [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md), [Architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md) |
| Write an RDR | [RDR: Research-Design-Review](https://github.com/Hellblazer/nexus/blob/main/docs/rdr.md) |
| Index a repo or PDFs | [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) |
| Configure or tune | [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) |
| Run in containers or Cowork | [Container Integration](https://github.com/Hellblazer/nexus/blob/main/docs/container-integration.md) |
| Browse the docs tree | [docs/README.md](https://github.com/Hellblazer/nexus/blob/main/docs/README.md) |
| Read the long-form story | [Tensegrity blog](https://tensegrity.blog/) |

## Prerequisites

Python 3.12+, [`uv`](https://docs.astral.sh/uv/), `git`. For hybrid search, [`ripgrep`](https://github.com/BurntSushi/ripgrep). For the Claude Code plugin, [Node.js](https://nodejs.org/) (the bundled `sequential-thinking` and `context7` servers spawn via `npx`).

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE).
