# Nexus Documentation

Nexus provides persistent memory and semantic search for AI coding agents. Begin with the getting started guide, then explore by topic.

## Getting Started

- [Getting Started](getting-started.md) — Install, local usage (no API keys), Claude Code plugin, semantic search setup

## Core Concepts

- [Storage Tiers](storage-tiers.md) — T1 (inter-agent session context), T2 (project memory), T3 (semantic knowledge) — architecture and data flow
- [Document Catalog](catalog.md) — Track every indexed document and the links between them — citations, implementations, provenance
- [Memory and Tasks](memory-and-tasks.md) — T2 memory, beads integration, session context
- [MCP Servers](mcp-servers.md) — The two bundled MCP servers (`nexus` + `nexus-catalog`), tool reference, and the 6 CLI-only operations
- [Repo Indexing](repo-indexing.md) — File classification, tree-sitter chunking, frecency scoring
- [Configuration](configuration.md) — Config hierarchy, `.nexus.yml`, settings

## RDR (Research-Design-Review)

Structured decision tracking for human-AI collaboration. Read in order:

1. [Overview](rdr-overview.md) — What RDRs are, when to write one, evidence classification
2. [Workflow](rdr-workflow.md) — Create → Research → Gate → Accept → Close
3. [Nexus Integration](rdr-nexus-integration.md) — How agents and storage tiers work with RDRs
4. [Templates](rdr-templates.md) — Minimal and full examples, post-mortem template
5. [RDR Index](rdr/README.md) — All project RDRs with status

## Claude Code Plugins

- [nx Plugin](../nx/README.md) — Agents, skills, session hooks, MCP servers, slash commands
- [sn Plugin](../sn/README.md) — Serena + Context7 MCP servers with subagent guidance injection

## Search and Analysis

- [Querying Guide](querying-guide.md) — When to use `nx search` vs `query()` MCP vs `/nx:query` skill, catalog-aware routing, analytical query examples
- [Plan-Centric Retrieval](plan-centric-retrieval.md) — How `nx_answer` ties plan matching, typed-graph traversal, and scenario templates into one retrieval trunk (RDR-078)
- [Plan Authoring Guide](plan-authoring-guide.md) — YAML schema for authoring plan templates, dimensions, default bindings, CI validation
- [MCP Tools vs Agents](mcp-vs-agents.md) — When to ship a capability as an MCP tool vs a Claude Code agent (RDR-080 boundary rule + stub-agent pattern)
- [Catalog Link Types](catalog-link-types.md) — Typed edges between documents: `cites`, `implements`, `supersedes`, etc.
- [Catalog Purposes](catalog-purposes.md) — Named aliases for link-type bundles that `traverse` walks
- [Xanadu in Nexus](xanadu-in-nexus.md) — What we borrowed from Ted Nelson's Project Xanadu: tumbler addressing, typed links, span transclusion, and what we deliberately left out

## Reference

- [CLI Reference](cli-reference.md) — Every command, every flag
- [Architecture](architecture.md) — Module map, design decisions
- [Contributing](contributing.md) — Dev setup, testing, code style

## Historical

- [Origins and Inspirations](historical.md) — Lineage from mgrep, SeaGOAT, Arcaneum, and key evolution milestones
