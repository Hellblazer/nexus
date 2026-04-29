# dt: DEVONthink Slash Command Plugin for Claude Code

A Claude Code plugin that exposes the `nx dt` CLI as ergonomic slash commands.

## What It Does

DEVONthink users on macOS keep their reference corpora (PDFs, web archives, notes) in DT databases. Nexus indexes those records into T3 via `nx dt index ...` and resolves Nexus tumblers back to DEVONthink via `nx dt open <tumbler>`. The CLI is the source of truth.

`dt` adds a thin slash command layer on top of that CLI:

1. **`/dt:index-selection`** wraps `nx dt index --selection`. Index whatever is currently highlighted in DEVONthink's UI.
2. **`/dt:open-result`** wraps `nx dt open <tumbler-or-uuid>`. Resolve a Nexus tumbler (or a raw DT UUID) to a `x-devonthink-item://` URL and open it.

The plugin contains no MCP servers, no hooks, no agents. It is documentation plus two argument-bearing skills that delegate to the underlying CLI.

## Install

```bash
/plugin install dt@nexus-plugins
```

### Prerequisites

- **macOS** with DEVONthink running (the CLI is darwin-only and platform-checks before catalog I/O).
- **`nx` CLI** installed and on `PATH`. The plugin assumes `conexus >= 4.19.2` (when `nx dt` shipped). Run `nx --version` to confirm.
- **Catalog initialized** for the project. `/dt:open-result` needs the catalog to resolve tumblers; raw DT UUIDs work without it.

## What Gets Wrapped

| Skill | Underlying CLI | Notes |
|-------|----------------|-------|
| `/dt:index-selection` | `nx dt index --selection [--collection ... | --corpus ...] [--database ...] [--dry-run]` | Indexes currently selected DT records. Forwards optional `--collection` (e.g., `knowledge__papers`) and `--corpus` flags. |
| `/dt:open-result` | `nx dt open <tumbler-or-uuid>` | Accepts a tumbler (e.g., `1.2.3`) or a raw DT UUID. UUID-shaped arguments skip the catalog and go straight to `x-devonthink-item://<UUID>`. |

For the full flag surface (`--tag`, `--group`, `--smart-group`, `--uuid`, `--dry-run`, etc.) call the CLI directly: `nx dt index --help`.

## Plugin Structure

```
dt/
├── .claude-plugin/
│   └── plugin.json
├── skills/
│   ├── dt-index-selection/
│   │   └── SKILL.md
│   └── dt-open-result/
│       └── SKILL.md
├── CHANGELOG.md
└── README.md
```

## Relationship to nx and sn

`dt` sits alongside `nx` and `sn` in the `nexus-plugins` marketplace. It depends on the `nx` CLI being installed but has no plugin-level dependency on the `nx` Claude Code plugin. Install whichever combination fits.

| Plugin | Scope | What it provides |
|--------|-------|------------------|
| `nx` | Project-specific | Knowledge management, semantic search, agents, beads, RDR tracking |
| `sn` | Universal | MCP tool guidance for subagents (Serena + Context7) |
| `dt` | macOS-only, project-specific | Slash command wrappers around `nx dt` for DEVONthink workflows |
