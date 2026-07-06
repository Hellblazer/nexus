# Nexus Documentation

Start with [Getting Started](getting-started.md) for installation. Then explore by topic.

`docs/*.md` is living reference documentation — kept current with the codebase and safe to trust as-is. `docs/rdr/*.md` is a different thing entirely: an append-only historical decision log (186+ files) capturing the reasoning behind past design choices, some since superseded. If you're looking for a how-to or a current design reference, stay in `docs/`; only dig into `docs/rdr/` when you need the "why" behind a decision.

## Core

- [Getting Started](getting-started.md) — Install, local usage, Claude Code plugin
- [Desktop Deployment](desktop-deployment.md) — All three Claude surfaces (chat, Cowork, Code) and the host daemon lifecycle
- [Agent Lifecycle & Operations](operations/agent-lifecycle.md) — Install → provision → run → upgrade → uninstall: the state model + the three walkthroughs
- [Architecture](architecture.md) — Reference architecture, module map, design decisions
- [Storage Tiers](storage-tiers.md) — T1/T2/T3 model, data flow, service substrate
- [Document Catalog](catalog.md) — Document registry, typed links, purposes, topic taxonomy
- [Managed Onboarding](managed-onboarding.md) — Use the hosted managed service (no local stack)
- [Configuration](configuration.md) — Config hierarchy, `.nexus.yml`, environment variables, logging
- [Container Integration](container-integration.md) — Daemon model for containers and Cowork

## Upgrading to 6.0 (off ChromaDB)

6.0 retires ChromaDB T3 serving for the native nexus-service (Postgres 17 + pgvector). Existing users migrate with one command, `nx guided-upgrade`.

- [Getting Started § Cloud mode](getting-started.md#cloud-mode-optional) and `nx init` — stand up the service stack
- [Migration Runbook](migration-runbook.md) — the operational order of operations, quiescence, rollback, and the two-release deprecation window

## Workflow

- [RDR: Research-Design-Review](rdr.md) — Lifecycle, workflow, Nexus integration, templates
- [Repo Indexing](repo-indexing.md) — File classification, chunking pipeline, frecency scoring
- [Plan-Centric Retrieval](plan-centric-retrieval.md) — `nx_answer`, plan matching, scenario templates
- [Plan Authoring Guide](plan-authoring-guide.md) — YAML schema for plan templates

## Reference

- [CLI Reference](cli-reference.md) — Every command, every flag
- [MCP Servers](mcp-servers.md) — The two bundled MCP servers and their tools
- [Querying Guide](querying-guide.md) — When to use which retrieval interface
- [Privacy Policy](privacy-policy.md) — What data nexus stores and where

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
- [`testing/`](testing/) — Plugin surface coverage matrices and testing artifacts
- [`proposals/`](proposals/) — Standalone application proposals and design write-ups

## Claude Code Plugins

- [conexus Plugin](../conexus/README.md) — Agents, skills, session hooks, MCP servers, slash commands
- [sn Plugin](../sn/README.md) — Serena + Context7 MCP servers with subagent guidance

## Origins

Nexus synthesizes patterns from mgrep (UX, citation format), SeaGOAT (git frecency scoring, hybrid search), Arcaneum (PDF extraction pipeline), and Mixedbread (cloud vector store, succeeded by self-hosted ChromaDB and now by the native nexus-service over Postgres 17 + pgvector). Three storage tiers, no raw content storage outside the source repos, and a specification-before-code workflow recorded across 160+ RDRs.
