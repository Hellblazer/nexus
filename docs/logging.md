# Logging Inventory

Central configuration: `src/nexus/logging_setup.py` — `configure_logging(mode, verbose)`.

## Entry Points

| Entry point | Mode | File handler | Notes |
|---|---|---|---|
| `nx` CLI | `cli` | None (stderr only) | WARNING default, DEBUG with `-v` |
| `nx-mcp` (core MCP) | `mcp` | `~/.config/nexus/logs/mcp.log` | RotatingFileHandler 10 MB × 5 |
| `nx-mcp-catalog` | `mcp` | `~/.config/nexus/logs/mcp.log` | Shares log with core MCP |
| `nx console` | `console` | `~/.config/nexus/logs/console.log` | RotatingFileHandler 10 MB × 5 |

## Existing Log Files

| File | Writer | Format |
|---|---|---|
| `~/.config/nexus/index.log` | Git post-commit hook (`nx index repo`) | Unstructured, ~60 MB observed |
| `~/.config/nexus/dolt-server.log` | Dolt server process | Dolt native format |
| `~/.config/nexus/logs/mcp.log` | MCP servers (via logging_setup) | `%(asctime)s %(name)s %(levelname)s %(message)s` |
| `~/.config/nexus/logs/console.log` | Console server (via logging_setup) | Same as above |

## Suppressed Loggers

`httpx`, `httpcore`, `chromadb.telemetry`, `opentelemetry` — forced to WARNING in all modes.

## Special Cases

- `search_cmd.py` overrides structlog to ERROR level when producing machine-parseable output (`--json`, `--vimgrep`, `--files`, `--compact`).
- The indexer hook script redirects stdout/stderr to `index.log` directly in the shell — not managed by `logging_setup`.
