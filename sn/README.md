# sn — Serena + Context7 MCP Plugin for Claude Code

A lightweight Claude Code plugin that bundles two MCP servers and injects usage guidance into all subagents.

## What It Does

Subagents spawned by Claude Code don't see your CLAUDE.md instructions. They have access to MCP tools but don't know they should use them or how to call them correctly.

`sn` fixes this by:

1. **Bundling MCP servers** — installs Serena (LSP-backed code intelligence) and Context7 (live library documentation) as MCP servers
2. **Injecting usage guidance** — a `SubagentStart` hook injects tool routing tables, parameter signatures, and workflows into every subagent's context

## Install

```bash
/plugin install sn@nexus-plugins
```

### Prerequisites

- **Serena**: requires `uvx` ([uv](https://docs.astral.sh/uv/) must be installed)
- **Context7**: requires `npx` ([Node.js](https://nodejs.org/) must be installed)
- **Serena project config**: Serena needs a project configuration to know which codebase to index. See [Serena docs](https://github.com/oraios/serena) for setup.

## What Gets Injected

Every subagent receives a `system-reminder` block containing:

### Serena Guidance

- **Project activation**: auto-detected from `git rev-parse --show-toplevel`
- **Routing table**: when to use Serena vs Grep/Read/Glob
- **Parameter signatures**: the exact call signatures subagents frequently get wrong (`name_path_pattern` vs `name_path`, `relative_path` must be a file, etc.)
- **Rules**: `get_symbols_overview` before reading whole files, `find_referencing_symbols` before signature changes

### Context7 Guidance

- **Workflow**: `resolve-library-id` → `query-docs` two-step pattern
- **When to use**: API syntax, framework setup, version migration, library debugging
- **When not to use**: general programming, business logic, code review

## MCP Servers Included

| Server | Command | Purpose |
|--------|---------|---------|
| `serena` | `uvx --from git+https://github.com/oraios/serena serena start-mcp-server` | LSP-backed code intelligence (symbol navigation, refactoring, type hierarchy) |
| `context7` | `npx -y @upstash/context7-mcp` | Live documentation lookup for libraries and frameworks |

## Plugin Structure

```
sn/
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── .mcp.json              # Serena + Context7 MCP server configs
├── hooks/
│   ├── hooks.json         # SubagentStart hook registration
│   └── scripts/
│       └── mcp-inject.sh  # Injection script
└── README.md
```

## Relationship to nx

`sn` is project-agnostic — it works in any repository where Serena is configured. It lives in the same marketplace as `nx` but has no dependency on it. Install either or both.

| Plugin | Scope | What it provides |
|--------|-------|-----------------|
| `nx` | Project-specific | Knowledge management, semantic search, agents, beads, RDR tracking |
| `sn` | Universal | MCP tool guidance for subagents (Serena + Context7) |
