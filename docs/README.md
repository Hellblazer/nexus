# Nexus Documentation

Start with [Getting Started](getting-started.md) for installation. Then find your section below by what you're trying to do.

`docs/*.md` is living reference documentation — kept current with the codebase and safe to trust as-is. `docs/rdr/*.md` is a different thing entirely: an append-only historical decision log (196+ RDR files, plus per-RDR post-mortems) capturing the reasoning behind past design choices, some since superseded. If you're looking for a how-to or a current design reference, stay in `docs/`; only dig into `docs/rdr/` when you need the "why" behind a decision.

## New user

- [Getting Started](getting-started.md) — Install, first index + search, the Claude Code plugin, troubleshooting
- [Desktop Deployment](desktop-deployment.md) — All three Claude surfaces (chat, Cowork, Code) and the host daemon lifecycle
- [Managed Onboarding](managed-onboarding.md) — Use the hosted managed service instead of a local stack

## I want to...

- **Back up my knowledge store** — [Storage Tiers § T3 Backup and Migration](storage-tiers.md#t3-backup-and-migration-exportimport) — `nx store export`/`import`, live T3, `.nxexp` format
- **Fix empty search results after upgrading** — [Getting Started § Troubleshooting](getting-started.md#troubleshooting) (`nx search` returns no results); if you upgraded straight from a pre-PG install, see [Upgrading an existing install](getting-started.md#upgrading-an-existing-install-skip-this-if-this-is-your-first-install) for the two-hop path
- **Check my install's health** — `nx doctor`, see [CLI Reference — nx doctor](cli-reference.md#nx-doctor)
- **Upgrade an existing install** — [Getting Started § Upgrading an existing install](getting-started.md#upgrading-an-existing-install-skip-this-if-this-is-your-first-install) — `uv tool upgrade conexus` + `nx upgrade`; installs still on ChromaDB (5.x, or 6.x that never migrated) need a two-hop through `conexus==6.18.1` first, since the Chroma-era migration machinery retired at RDR-155 P4b

## Operator reference

- [Configuration](configuration.md) — Config hierarchy, `.nexus.yml`, environment variables, logging
- [Container Integration](container-integration.md) — Daemon model for containers and Cowork
- [Agent Lifecycle & Operations](operations/agent-lifecycle.md) — Install → provision → run → upgrade → uninstall: the state model + the three walkthroughs
- [Migration Runbook](migration-runbook.md) — Operator's manual order of operations, quiescence, rollback, and the deprecation window
- [Privacy Policy](privacy-policy.md) — What data nexus stores and where
- [`operations/`](operations/) — Operator runbooks: [Apple code signing](operations/apple-code-signing.md), [audit-membership interpretation](operations/audit-membership-interpretation.md), [T3 health checks (historical template)](operations/t3-health.md)
- [`runbooks/`](runbooks/) — One-off operational runbooks for specific incidents/phases: [RDR-191 Phase 5 cloud FK runbook](runbooks/rdr-191-phase5-cloud-fk.md)

## Contributor reference

- [Contributing](contributing.md) — Dev setup, testing, code style, release process
- [Architecture](architecture.md) — Reference architecture, module map, design decisions
- [Wire Contract Pending](wire-contract-pending.md) — Ledger of engine/client wire-contract pairings deployed ahead of their client half (nexus-1vogq tripwire)
- [`testing/`](testing/) — [6.0.0 plugin surface coverage matrix](testing/6.0.0-plugin-surface-coverage-matrix.md)
- [`tutorial/`](tutorial/) — **In progress, not on `main`.** The tutorial-recording pipeline lives on the `wip/tutorial` branch; this directory on `main` is a placeholder.

## Reference

- [Storage Tiers](storage-tiers.md) — T1/T2/T3 model, data flow, service substrate
- [Document Catalog](catalog.md) — Document registry, typed links, purposes, topic taxonomy
- [Repo Indexing](repo-indexing.md) — File classification, chunking pipeline, frecency scoring
- [Querying Guide](querying-guide.md) — When to use which retrieval interface
- [Plan-Centric Retrieval](plan-centric-retrieval.md) — `nx_answer`, plan matching, scenario templates
- [Plan Authoring Guide](plan-authoring-guide.md) — YAML schema for plan templates
- [CLI Reference](cli-reference.md) — Every command, every flag
- [MCP Servers](mcp-servers.md) — The two bundled MCP servers and their tools
- [RDR: Research-Design-Review](rdr.md) — Lifecycle, workflow, Nexus integration, templates

## Researcher / historian

Historical and exploratory material — design reasoning, forensic records, and proposals that are not live reference docs. Read for context, not as current behavior.

- [`rdr/`](rdr/) — All accepted and historical RDRs; [`rdr/README.md`](rdr/README.md) is the RDR index. [`rdr/post-mortem/`](rdr/post-mortem/) holds per-RDR post-mortems (drift between an RDR's decision and what shipped) — not to be confused with [`postmortem/`](postmortem/) below, which covers incident post-mortems unrelated to any single RDR.
- [`postmortem/`](postmortem/) — Incident post-mortems (production bugs, forensics investigations), e.g. [PDF index collection mismatch](postmortem/2026-03-23-pdf-index-collection-mismatch.md), [daemon concurrency forensics](postmortem/2026-06-05-daemon-concurrency-forensics.md), [RDR-110/113 remediation chain](postmortem/2026-05-16-rdr110-113-remediation-chain.md) (reads live atop scrapped RDR-110..119 — successor is RDR-127).
- [`exploration/`](exploration/) — Internal design exploration, surveys, draft proposals. Two entries are normative despite the folder name: [MCP Tools vs Agents](exploration/mcp-vs-agents.md) (why `nx_answer` replaced the agent pair, cited from [Querying Guide](querying-guide.md#see-also)) and [Taxonomy Projection Tuning](exploration/taxonomy-projection-tuning.md) (cited from [Storage Tiers](storage-tiers.md#t2----memory-bank)). The rest — [agentic-cockpit](exploration/agentic-cockpit.md), [a2ui-summary](exploration/a2ui-summary.md), [workflow-engine brainstorm/README/synthesis](exploration/workflow-engine-brainstorm.md) — read atop scrapped RDR-110..119; successor pointer is RDR-127 (accepted). [metadata-consistency-matrix.md](exploration/metadata-consistency-matrix.md) is the template for historical-banner framing used across this tree.
- [`migration/`](migration/) — Version-specific upgrade notes. [`migration/README.md`](migration/README.md) indexes the one active guide ([Upgrading to 4.34.x](migration/upgrading-to-4.34.md)) and the frozen RDR-101 event-sourced-catalog migration forensics (Phases 0-6, complete; the verbs those docs reference were retired in nexus-iftc).
- [`plans/`](plans/) — Historical implementation plans, e.g. [RDR-078 plan-centric retrieval](plans/2026-04-14-rdr-078-plan-centric-retrieval-impl-plan.md), [RDR-079 operator dispatch](plans/2026-04-15-rdr-079-operator-dispatch-plan.md), [RDR-080 migration scope](plans/2026-04-15-rdr-080-migration-scope.md).
- [`field-reports/`](field-reports/) — Production shakeout reports, e.g. [architecture-as-code from ART](field-reports/2026-04-15-architecture-as-code-from-art.md).
- [`proposals/`](proposals/) — Standalone application proposals and design write-ups: [Beyond Similarity Search](proposals/beyond-similarity-search-application.md), [Open Ontologies](proposals/open-ontologies-application.md). Adopt-or-scrap disposition pending.
- [`integrations/`](integrations/) — External integrations: [DEVONthink scripts](integrations/devonthink-scripts.md), [DEVONthink smart rules](integrations/devonthink-smart-rules.md).

## Claude Code Plugins

- [conexus Plugin](../conexus/README.md) — Agents, skills, session hooks, MCP servers, slash commands
- [sn Plugin](../sn/README.md) — Serena + Context7 MCP servers with subagent guidance

## Origins

Nexus synthesizes patterns from mgrep (UX, citation format), SeaGOAT (git frecency scoring, hybrid search), Arcaneum (PDF extraction pipeline), and Mixedbread (cloud vector store, succeeded by self-hosted ChromaDB and now by the native nexus-service over Postgres 17 + pgvector). Three storage tiers, no raw content storage outside the source repos, and a specification-before-code workflow recorded across 196+ RDRs.
