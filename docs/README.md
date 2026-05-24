# Nexus Documentation

Start with [Getting Started](getting-started.md) for installation. Then explore by topic.

## Core

- [Getting Started](getting-started.md) — Install, local usage, Claude Code plugin
- [Desktop Deployment](desktop-deployment.md) — All three Claude surfaces (chat, Cowork, Code) and the host daemon lifecycle
- [Architecture](architecture.md) — Reference architecture, module map, design decisions
- [Storage Tiers](storage-tiers.md) — T1/T2/T3 model, data flow, daemon substrate
- [Document Catalog](catalog.md) — Document registry, typed links, purposes, topic taxonomy
- [Configuration](configuration.md) — Config hierarchy, `.nexus.yml`, environment variables, logging
- [Container Integration](container-integration.md) — Daemon model for containers and Cowork

## Workflow

- [RDR: Research-Design-Review](rdr.md) — Lifecycle, workflow, Nexus integration, templates
- [Repo Indexing](repo-indexing.md) — File classification, chunking pipeline, frecency scoring
- [Plan-Centric Retrieval](plan-centric-retrieval.md) — `nx_answer`, plan matching, scenario templates
- [Plan Authoring Guide](plan-authoring-guide.md) — YAML schema for plan templates

## Reference

- [CLI Reference](cli-reference.md) — Every command, every flag
- [MCP Servers](mcp-servers.md) — The two bundled MCP servers and their tools
- [Querying Guide](querying-guide.md) — When to use which retrieval interface

## Contributing

- [Contributing](contributing.md) — Dev setup, testing, code style, release process

## Subdirectories

- [`rdr/`](rdr/) — All accepted and historical RDRs, with the RDR index
- [`migration/`](migration/) — Version-specific upgrade notes
- [`tutorial/`](tutorial/) — Tutorial recording pipeline
- [`integrations/`](integrations/) — External integrations (DEVONthink, etc.)
- [`exploration/`](exploration/) — Internal design exploration, surveys, draft proposals
- [`postmortem/`](postmortem/) — RDR post-mortems for drift analysis
- [`field-reports/`](field-reports/) — Production shakeout reports
- [`operations/`](operations/) — Operator runbooks
- [`plans/`](plans/) — Historical implementation plans

## Claude Code Plugins

- [conexus Plugin](../conexus/README.md) — Agents, skills, session hooks, MCP servers, slash commands
- [sn Plugin](../sn/README.md) — Serena + Context7 MCP servers with subagent guidance

## Origins

Nexus synthesizes patterns from mgrep (UX, citation format), SeaGOAT (git frecency scoring, hybrid search), Arcaneum (PDF extraction pipeline), and Mixedbread (cloud vector store, now replaced by self-hosted ChromaDB). Three storage tiers, no raw content storage outside the source repos, and a specification-before-code workflow recorded across 125+ RDRs.
