# Nexus

**Persistent memory and semantic search for Claude.** Three storage tiers that survive across sessions, an event-sourced document catalog with typed links, and a specification-before-code workflow for tracking decisions. Local-first; no API keys required. Knowledge compounds across conversations instead of evaporating when the window closes.

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

<a href="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=1024&ssl=1">
  <img src="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=480&ssl=1" alt="A brass-ribbed crystal dome on a hilltop at dusk" align="right" width="320" />
</a>

**Start here**: [**How I actually use Nexus**](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) — the conceptual overview and the shape of the substrate. Then [**Installing Nexus**](https://tensegrity.blog/2026/04/26/installing-nexus/) — a ten-minute hands-on walkthrough from `uv tool install` through your first search.

## Prerequisites

Python 3.12+, [`uv`](https://docs.astral.sh/uv/), `git`. For hybrid search, [`ripgrep`](https://github.com/BurntSushi/ripgrep). For the Claude Code plugin, [Node.js](https://nodejs.org/) (the bundled `sequential-thinking` and `context7` servers spawn via `npx`).

## Install for Claude

Three surfaces share one host substrate: the `nx` CLI (the `conexus` package). Claude Desktop's `.mcpb` bundles it and resolves it on first launch; the Claude Code plugin and Cowork use a **separately-installed** CLI (`uv tool install conexus`). Pick the one that matches how you use Claude.

### Claude Desktop chat

Download `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) and double-click. Claude Desktop registers it under Settings → Connectors. Requires [uv](https://docs.astral.sh/uv/) installed on the host (the standard installer or Homebrew puts it where Claude Desktop resolves it — no PATH setup needed); deps resolve on first launch (~20s).

### Claude Code (terminal)

```bash
uv tool install conexus                  # 1. the nx CLI (the plugin's MCP servers ARE this package)
/plugin marketplace add Hellblazer/nexus # 2. add the marketplace
/plugin install conexus@nexus-plugins    # 3. install the plugin
```

The plugin's MCP servers (`nx-mcp`, `nx-mcp-catalog`) are console-scripts from the `conexus` package, so **the `nx` CLI must be installed too**: `/plugin install` alone leaves the servers unable to launch. Install the CLI first (step 1; see [CLI quick-start](#cli-quick-start) to then provision the storage backend).

The plugin ships 13 specialized agents, 45 skills (RDR lifecycle, plan-centric retrieval, dev workflows), and 50 MCP tools split across two focused servers. Session hooks load project context at startup.

### Claude Cowork

Works automatically once the conexus plugin is installed in Claude Code on the host. State round-trips bidirectionally with the host CLI through the T2 daemon.

For the full deployment story across all three surfaces (install, daemon lifecycle, drift detection, uninstall), see [docs/desktop-deployment.md](https://github.com/Hellblazer/nexus/blob/main/docs/desktop-deployment.md).

## What it does

- **Persistent memory** — three storage tiers (T1 session scratch, T2 SQLite memory bank, T3 semantic knowledge store) so Claude remembers across conversations.
- **Semantic search** — index your code, docs, RDRs, and PDFs once; search by meaning afterward. Tree-sitter AST chunking across 23 languages, CCE prose chunking, PDF auto-routing.
- **Typed document catalog** — Xanadu-inspired addressing with typed links (`cites`, `implements`, `supersedes`). Walk from a design doc to the code that implements it.
- **RDR: Research-Design-Review** — write a spec before you code. Captures the problem, research, alternatives, and chosen approach. The corpus is searchable, so prior decisions surface during new design work.
- **Local-first** — runs entirely on your machine: an on-device bge-768 ONNX embedder over a bundled Postgres 17 + pgvector service that `nx init` provisions for you. Voyage AI (server-side embeddings) is opt-in for the managed-cloud deployment.

## CLI quick-start

```bash
uv tool install conexus        # install the nx CLI
nx init                        # acquires the signed engine + Postgres bundle, provisions pgvector + bge-768, starts the service, offers autostart
nx doctor                      # verify the stack
nx index repo .                # index your repo + discover topics
nx search "how does retry work"   # semantic search, fully local
```

You never choose an engine version: every conexus release is built pinned to the exact `engine-service` release it was tested against, and `nx init` acquires that signed binary + Postgres bundle automatically (cosign-verified). You do **not** need PostgreSQL installed — the self-contained bundle ships pgvector already compiled in, and a pgvector-capable PostgreSQL you already have is used as-is. If provisioning ever reports missing Postgres binaries or a missing pgvector extension (offline install, older conexus), pre-stage the bundle explicitly with `nx daemon service install-binary <engine-service-tag>` and re-run. Advanced: export `NEXUS_SERVICE_TAG=engine-service-vX.Y.Z` to override the pin (air-gapped installs, engine testing).

`nx init` provisions the bundled Postgres 17 + pgvector cluster, fetches the bge-768 ONNX model the service embeds with, starts the persistent service, and offers to register the OS autostart unit so it restarts at login/boot (prompt defaults to yes; `--yes` accepts non-interactively, `--no-autostart` starts a session supervisor only). There is **no** separate `nx daemon t2 install` step — T2 (notes/plans) is served by the same service in the default config. The permanent vector store (T3) serves through this native service; the bundled binary + Postgres are cosign-verified and acquired automatically. `nx init` is idempotent — safe to re-run. (The older `nx init --service` flag still works but is deprecated — plain `nx init` is the path now.) **First run only:** this downloads a few hundred MB (the signed ~134 MB service binary, the relocatable Postgres bundle, and the ~140 MB bge-768 model) and takes a few minutes; subsequent starts are fast.

> **Upgrading from a pre-6.0 install?** 6.0 moves the permanent vector store from ChromaDB to the Postgres + pgvector service. After `uv tool upgrade conexus`, run **`nx guided-upgrade`** — one command detects your existing store, provisions and version-pins the service, and migrates your data with validation and copy-not-move rollback safety. Your ChromaDB data is left intact as the migration source. If it reports missing Postgres binaries or a missing pgvector extension, run `nx daemon service install-binary <engine-service-tag>` (installs the self-contained Postgres bundle, pgvector included — no Homebrew/apt PostgreSQL needed) and re-run `nx guided-upgrade`.

The `nx` CLI provides direct access to all storage tiers, indexing, search, the catalog, and taxonomy. See [Getting Started](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md) for a walkthrough, [CLI Reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md) for every command and flag.

## Updating

```bash
uv tool upgrade conexus                  # upgrade the nx CLI — PRESERVES your extras (e.g. [local])
```

**Always upgrade with `uv tool upgrade conexus`.** It retains the spec you installed with, so a `[local]` install stays a `[local]` install. **Do not** upgrade with `uv tool install conexus --force` / `uv tool install conexus` — that *resets* the install and **drops `[local]`**, silently downgrading your embedder from 768-dim to 384-dim. With existing 768-dim collections that produces a dimension mismatch and search returns nothing. If you hit that, reinstall the extra: `uv tool install --reinstall "conexus[local]"`.

When you update the **Claude Code plugin** (`/plugin update`), upgrade the CLI to the matching version at the same time so the two stay in lockstep.

## Going deeper

| If you want to... | Read |
|---|---|
| Understand the architecture | [Storage Tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md), [Architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md) |
| Install, upgrade, or uninstall the agent | [Agent Lifecycle & Operations](https://github.com/Hellblazer/nexus/blob/main/docs/operations/agent-lifecycle.md) |
| Use the hosted managed service | [Managed Onboarding](https://github.com/Hellblazer/nexus/blob/main/docs/managed-onboarding.md) |
| Write an RDR | [RDR: Research-Design-Review](https://github.com/Hellblazer/nexus/blob/main/docs/rdr.md) |
| Index a repo or PDFs | [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) |
| Configure or tune | [Configuration](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md) |
| Run in containers or Cowork | [Container Integration](https://github.com/Hellblazer/nexus/blob/main/docs/container-integration.md) |
| Browse the docs tree | [docs/README.md](https://github.com/Hellblazer/nexus/blob/main/docs/README.md) |
| Read the conceptual story | [How I actually use Nexus](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) |
| Walk through a fresh install | [Installing Nexus](https://tensegrity.blog/2026/04/26/installing-nexus/) |
| Browse the full series | [Tensegrity blog](https://tensegrity.blog/) |

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE).
