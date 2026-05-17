# Nexus

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

<a href="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=1024&ssl=1">
  <img src="https://i0.wp.com/tensegrity.blog/wp-content/uploads/2026/04/a-stately-pleasure-dome.png?w=480&ssl=1" alt="A brass-ribbed crystal dome on a hilltop at dusk, the establishing shot for the Tensegrity blog series on Nexus" align="right" width="320" />
</a>

**Persistent memory and semantic search for AI coding agents.** Local-first (no API keys required), three storage tiers with different lifetimes, an event-sourced document catalog with typed links between artefacts, and a specification-before-code decision-tracking workflow (RDR). Knowledge compounds across sessions instead of evaporating with the conversation.

The four-paragraph version is on the Tensegrity blog: [**How I actually use Nexus**](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) (concepts), [**Installing Nexus**](https://tensegrity.blog/2026/04/26/installing-nexus/) (ten-minute walkthrough), [**Nexus by Example**](https://tensegrity.blog/2026/04/19/nexus-by-example/) (the pieces in practice).

## At a glance

| | What you get |
|---|---|
| **Storage** | T1 ephemeral session scratch (in-memory ChromaDB) · T2 SQLite + FTS5 for memory / plans / taxonomy / telemetry · T3 ChromaDB persistent + Voyage AI in cloud mode, ONNX MiniLM in local mode |
| **Indexing** | Tree-sitter AST chunking (23 languages) · CCE prose chunking (`voyage-context-3`) · PDF auto-routing (Docling → MinerU → PyMuPDF) · git frecency scoring · automatic topic discovery via HDBSCAN |
| **Search** | Semantic · keyword · hybrid · taxonomy-boosted · catalog-aware · plan-driven via `nx_answer`. Metadata filtering via `--where bib_year>=2024 --where chunk_type=table_page` and similar |
| **Catalog** | Event-sourced (`events.jsonl` canonical, SQLite is a deterministic projection) · typed links (`cites`, `implements`, `supersedes`, …) · Tumbler addressing inspired by Ted Nelson's Xanadu |
| **Decision tracking (RDR)** | Specification-before-code lifecycle: `create → research → gate → accept → close`, with post-mortems and a searchable history that surfaces during new design work |
| **Claude Code plugin** | 13 specialized agents · 43 skills (RDR lifecycle, plan-centric retrieval, dev workflow) · 36 MCP tools across two servers · session hooks for cold-start context |
| **CLI** | `nx` with 16 top-level verbs (`index`, `search`, `query`, `store`, `memory`, `scratch`, `catalog`, `taxonomy`, `enrich`, `doctor`, `upgrade`, …) |
| **Local-first** | Default install runs entirely on your machine, zero API keys (ONNX MiniLM + local ChromaDB). Voyage AI + ChromaDB Cloud are opt-in for higher-quality embeddings |
| **License** | AGPL-3.0-or-later |

## By example

```bash
# Semantic search across indexed code, docs, and knowledge
nx search "how does authentication work"           # matches by meaning, not strings
nx search "error handling" --hybrid                # semantic + ripgrep, frecency-weighted

# Three tiers, one tool
nx scratch put "the bug is in the retry logic"           # T1, inter-agent session
nx memory put --project myapp --title "DB choice" "Postgres for concurrency"
nx store put --collection knowledge__myapp "Rate limit: 10k/min per vendor docs"

# Indexed PDFs filtered by bibliographic metadata
nx enrich bib knowledge__papers --delay 0.5
nx search "consensus protocols" --where bib_year>=2024

# Topic discovery is automatic; review keeps humans in the loop
nx index repo .
nx taxonomy review                                  # accept, rename, merge, split
```

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

Works immediately with local ONNX embeddings: no accounts, no API keys. For higher-quality cloud embeddings (Voyage AI), see the [cloud setup instructions](https://github.com/Hellblazer/nexus/blob/main/docs/getting-started.md#cloud-mode-optional).

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
| **Scratch** (T1) | Inter-agent session context: coordination and knowledge sharing across agent invocations | In-memory ChromaDB | No |
| **Memory** (T2) | Project-level persistence with full-text search | Local SQLite + FTS5 | No |
| **Knowledge** (T3) | Permanent semantic knowledge: code, papers, docs, decisions searchable by meaning | Local ChromaDB (default) or ChromaDB Cloud + Voyage AI | No (local) / Yes (cloud) |

Agents use all three tiers cooperatively. T1 enables inter-agent communication: sharing findings and preventing duplicate work within a session. T2 provides project decisions that constrain solutions. T3 surfaces how similar problems were resolved in other contexts. As the T3 knowledge base grows, it becomes the project's institutional memory, managing the information overload that accompanies complex designs and long-lived codebases.

## What you can index

Code, documents, PDFs, and manual knowledge entries: anything that benefits from semantic search:

```bash
nx index repo .                          # code + docs + RDRs from a git repo
nx index pdf paper.pdf --collection knowledge__ml  # reference papers
nx store put --collection knowledge__ops "Redis maxmemory-policy: allkeys-lru for cache, noeviction for queues"
```

Repository indexing (`nx index repo`) is the most automated path. It classifies git-tracked files, chunks code into logical pieces via tree-sitter AST parsing across 31 languages, and embeds each chunk using local ONNX models by default, or Voyage AI models in cloud mode. Recently-touched files rank higher via git frecency scoring.

PDF indexing auto-detects math-heavy papers and routes them through MinerU (default-installed) for LaTeX extraction of equations; non-math PDFs use Docling directly. MinerU pulls ~2-3 GB of formula and table models on first use. To opt out of formula-aware extraction (e.g. for documents you know contain no math), pass `--extractor docling` explicitly. See [PDF Extraction Backends](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md#pdf-extraction-backends) for details.

See [Repo Indexing](https://github.com/Hellblazer/nexus/blob/main/docs/repo-indexing.md) for details and `.nexus.yml` configuration.

## RDR: Research-Design-Review

Complex features are bigger than what fits in working memory, whether yours or an LLM's. Without a specification to track against, purpose drift sets in during implementation. An RDR is a specification document written *before* coding: it captures the problem, the research journey, competing options, and the chosen approach. Once accepted, the RDR is the stable target that implementation builds against. If the design proves wrong, abandon the RDR and draft a new one with what you learned; iteration lives in the chain of RDRs, not in any single document.

Each research finding is tagged with its evidence quality (verified against source code, supported by documentation only, or assumed) so readers know which conclusions are load-bearing and which need validation. The growing corpus is searchable, so prior decisions surface automatically when starting new design work.

RDR is fully optional. See [RDR Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md) for the full process.

## Claude Code plugin

The `nx/` directory is a Claude Code plugin that gives agents access to everything above. Install via the marketplace:

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

The plugin provides 13 specialized agents, 43 skills covering the RDR lifecycle, plan-centric retrieval, and development workflows, session hooks for automatic context initialization, and 36 MCP tools split across two focused servers: `nexus` (26 tools: search, store, memory, scratch, plans, 5 operator tools for structured extract/rank/compare/summarize/generate, plus 4 orchestration tools including `nx_answer` for plan-matched multi-step retrieval) and `nexus-catalog` (10 catalog tools: search, show, link, resolve, stats, etc.). The plan-centric retrieval layer (`nx_answer`) matches questions against a library of scenario templates, executes the matched plan, and records every run, so the library compounds with use. Agents search indexed code before proposing changes, check prior RDR decisions before designing new features, and coordinate through standard pipelines (plan → implement → review → test) with built-in quality gates.

The plugin integrates with [Beads](https://github.com/BeadsProject/beads) for task tracking. See [nx/README.md](https://github.com/Hellblazer/nexus/blob/main/nx/README.md) for the full plugin documentation.

## CLI Reference

The `nx` command provides direct access to all storage tiers, indexing, and search.

| Command | What it does |
|---------|-------------|
| `nx search` | Semantic and hybrid search across indexed code, docs, and knowledge. Supports `--where` with `=`, `>=`, `<=`, `>`, `<`, `!=` operators for metadata filtering |
| `nx index` | Index git repos, PDFs, and markdown into searchable collections |
| `nx enrich bib` | Backfill bibliographic metadata (year, venue, authors, citations) from Semantic Scholar |
| `nx enrich aspects` | Extract structured paper aspects (problem, method, datasets, baselines, results) into T2 |
| `nx store` | Store, retrieve, export, and import knowledge entries |
| `nx memory` | Per-project persistent notes (local, no API keys) |
| `nx scratch` | Inter-agent session context (in-memory, no API keys) |
| `nx collection` | Inspect and manage cloud collections |
| `nx config` | Credentials and settings |
| `nx upgrade` | Run pending database migrations and T3 upgrade steps |
| `nx doctor` | Health check: verifies dependencies, credentials, connectivity, schema |
| `nx hooks` | Install git hooks for automatic re-indexing on commit |
| `nx catalog` | Document registry: search, link, audit the catalog metadata layer |
| `nx taxonomy` | Topic taxonomy: discover, project across collections, review, merge, split |
| `nx context` | Project context cache: topic map for agent cold-start acceleration |
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
| [How I actually use Nexus](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) | Blog overview: the substrate as a control surface for developers, teams, and agents |
| [Installing Nexus](https://tensegrity.blog/2026/04/26/installing-nexus/) | Blog install walkthrough: prerequisites, CLI, plugin, short tour |
| [Nexus by Example](https://tensegrity.blog/2026/04/19/nexus-by-example/) | Blog walkthrough of the steam-driven semantic engine in practice |

**RDR (Research-Design-Review):**
1. [Overview](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-overview.md): What RDRs are, when to write one, evidence classification
2. [Workflow](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-workflow.md): Create → Research → Gate → Accept → Close
3. [Nexus Integration](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-nexus-integration.md): How storage tiers and agents amplify RDRs
4. [Templates](https://github.com/Hellblazer/nexus/blob/main/docs/rdr-templates.md): Minimal and full examples, post-mortem template
5. [Project RDR Index](https://github.com/Hellblazer/nexus/blob/main/docs/rdr/README.md): All project RDRs with status

## Prerequisites

- Python 3.12–3.13, [`uv`](https://docs.astral.sh/uv/), `git`
- For the Claude Code plugin: [Node.js](https://nodejs.org/) (provides `npx`) for the bundled `sequential-thinking` and `context7` MCP servers, which are spawned via `npx`. The `nx` CLI alone does not need it.
- For cloud embeddings (optional): [ChromaDB Cloud](https://www.trychroma.com/) + [Voyage AI](https://www.voyageai.com/) accounts (free tiers available)
- For hybrid search: [`ripgrep`](https://github.com/BurntSushi/ripgrep)

## Cockpit substrate (RDR-110/111/112)

nexus ships an agentic-cockpit substrate that runs alongside the
core CLI. Three RDRs cover the moving parts:

- **[RDR-110 Semantic Tuple Space](docs/rdr/rdr-110-semantic-tuple-space.md).**
  An ORB (out / read / take / ack / nack) tuplespace lives in
  `~/.config/nexus/tuples.db` next to `memory.db`. Subspaces declare a
  schema (dimensions, embed source, take semantics, retention) in YAML
  under `nx/tuplespace/builtin/`. `nx tuplespace ...` is the operator
  surface; the matching MCP tools route through the daemon when
  `NX_STORAGE_MODE=daemon`. Atomicity (Chroma + SQLite) and idempotent
  retake are first-class contracts (CA-1 / CA-2).
- **[RDR-111 ORB Hook Bridge and Cockpit](docs/rdr/rdr-111-orb-hook-bridge-cockpit.md).**
  Claude Code hooks (PreToolUse, PostToolUse, Stop, SubagentStop,
  UserPromptSubmit, Session, Notification) project into seven
  `hook_events/*` subspaces via the `orb_bridge_*.py` scripts under
  `nx/hooks/scripts/`. `nx cockpit` (`status` / `show` / `dashboard`
  plus `recent-events` / `active-claims` / `active-bindings` panels)
  is the read surface. Binding profiles under
  `nx/tuplespace/builtin/bindings/profiles/` react to events with
  configurable actions; the cockpit binding watcher in
  `nexus.cockpit.bindings` drives the dispatch.
- **[RDR-112 T2 Storage-as-Service](docs/rdr/rdr-112-t2-storage-service.md).**
  A persistent local daemon (`nx daemon t2 start`) owns the single
  SQLite writer for `memory.db` and `tuples.db`, exposing every
  domain-store method over a UDS-primary / loopback-TCP-fallback RPC
  surface. `nx daemon t2 install --autostart` wires up launchd
  (macOS) or systemd user units (Linux). The fail-closed bridge
  default + `EventStream` reconnect contract (RDR-114) close the
  daemon-unavailability semantics so subscribers and emitters share a
  uniform recoverable shape.

## Security

nexus is designed for single-user host trust: the daemon owner and
its clients must share a UID. The v1 trust boundary, accepted risks,
and how to report a vulnerability are summarised in
[SECURITY.md](SECURITY.md); the full design lives in
[RDR-113](docs/rdr/rdr-113-host-trust-model.md).

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE).
