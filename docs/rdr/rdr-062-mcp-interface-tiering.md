---
title: "RDR-062: MCP Interface Tiering — Core + Catalog Server Split"
status: accepted
type: architecture
priority: P1
created: 2026-04-09
accepted_date: 2026-04-09
reviewed-by: self
---

# RDR-062: MCP Interface Tiering — Core + Catalog Server Split

## Problem Statement

Nexus exposes 30 MCP tools in a single flat server. Every agent sees all 30 tools simultaneously — `search`, `store_put`, `catalog_link_audit`, `collection_verify` — a mix of daily-driver tools and admin plumbing. This creates three problems:

1. **Cognitive overload for agents**: With 30 tools to choose from, agents spend tokens reasoning about which tool to use. Most agents only need 10-12 tools.
2. **Interface sprawl**: Each new feature (feedback logging, taxonomy, entity resolution) adds more tools to the flat list. The tool count has grown from ~15 to 30 over three RDRs.
3. **No audience separation**: Admin/diagnostic tools (`collection_verify`, `catalog_link_audit`, `store_delete`) sit alongside core workflow tools (`search`, `store_put`), making the interface feel jumbled.

CLI is less of a concern — humans can browse `nx --help` and navigate subcommands. MCP tool sprawl is the primary issue.

## Proposed Solution

Split the single MCP server into two focused servers and demote admin tools to CLI-only:

### Server 1: `nexus` (core) — 14 tools

The daily-driver tools every agent needs:

| Tool | Purpose |
|------|---------|
| `search` | Semantic search across T3 |
| `query` | Document-level search with catalog routing |
| `store_put` | Persist to T3 (keeps internal auto-link callback) |
| `store_get` | Retrieve from T3 |
| `memory_put` | Write T2 memory |
| `memory_get` | Read T2 memory |
| `memory_search` | FTS5 search T2 |
| `scratch` | T1 session scratch |
| `scratch_manage` | Flag/promote scratch entries |
| `plan_save` | Save query plan |
| `plan_search` | Search plan library |
| `collection_list` | Discover available collections |
| `store_list` | Enumerate collection contents (used by knowledge-tidier) |
| `memory_delete` | Delete T2 entry (paired with memory_put in agent guidance) |

### Server 2: `nexus-catalog` (catalog/linking) — 10 tools

Specialized tools for linker agents, query planner, and research workflows:

| Tool | Purpose |
|------|---------|
| `catalog_search` | Find docs by metadata |
| `catalog_show` | Full entry with links |
| `catalog_list` | List entries with filters |
| `catalog_link` | Create typed link |
| `catalog_links` | Graph traversal (BFS) |
| `catalog_link_query` | Query links by filter |
| `catalog_resolve` | Tumbler → collection name |
| `catalog_register` | Register document |
| `catalog_update` | Update metadata |
| `catalog_stats` | Health summary |

### Demoted to CLI-only (6 tools removed from MCP)

| Tool | Reason |
|------|--------|
| `store_delete` | Destructive admin op |
| `collection_info` | Diagnostic — overlaps with `collection_list` |
| `collection_verify` | Pure diagnostic |
| `catalog_link_audit` | Admin audit |
| `catalog_link_bulk` | Destructive bulk delete |
| `catalog_unlink` | Admin link removal |

These remain as CLI commands (`nx store list`, `nx memory delete`, etc.) — only the MCP exposure is removed.

## Architecture

### Auto-Linker: No Change Needed

`store_put` calls `_catalog_auto_link()` via Python imports (`mcp_infra.py` singletons), not MCP tools. Both servers import `mcp_infra.py` independently. The catalog is backed by SQLite WAL mode, handling concurrent access from two processes. The existing mtime-check in `get_catalog()` provides cross-process consistency.

### File Layout

```
src/nexus/
  mcp_infra.py        # MODIFY: remove mcp = FastMCP("nexus") line
  mcp_server.py       # KEEP: backward-compat shim re-exporting from mcp/
  mcp/
    __init__.py        # Re-export injection helpers for tests
    core.py            # FastMCP("nexus"), 13 core tools, main()
    catalog.py         # FastMCP("nexus-catalog"), 10 catalog tools, main()
```

### Plugin Configuration

`nx/.mcp.json` registers both servers:
```json
{
  "nexus": { "command": "nx-mcp" },
  "nexus-catalog": { "command": "nx-mcp-catalog" }
}
```

### Tool Name Migration

Catalog tools change prefix from `mcp__plugin_nx_nexus__catalog_*` to `mcp__plugin_nx_nexus-catalog__catalog_*`. This affects ~100 occurrences across ~24 agent/skill/hook files.

## Implementation Plan

### Phase 1: Extract (single PR)

1. Create `src/nexus/mcp/` package with `core.py` and `catalog.py`
2. Move `FastMCP` instantiation from `mcp_infra.py` into each server module
3. Move core tools → `core.py`, catalog tools → `catalog.py`
4. Drop 8 demoted tools from MCP entirely (CLI commands unchanged)
5. Update `mcp_server.py` as backward-compat shim for test imports
6. Update `pyproject.toml` entry points
7. Update `nx/.mcp.json`, auto-approve hook, subagent-start hook
8. Mechanical sed across agent/skill `.md` files for new tool prefix

### Phase 1 Additional Items (from gate critique)

9. Update `test_mcp_server_round_trip` integration test: remove demoted tools from expected set, add catalog server round-trip test
10. Remove demoted tool references from `_shared/CONTEXT_PROTOCOL.md`, `subagent-start.sh`, `nx/skills/nexus/SKILL.md` — these require content deletion, not prefix rename
11. Update `mcp_server.py` shim to re-export all 23 tool functions (13 core + 10 catalog) plus injection helpers — demoted functions remain importable as Python callables
12. `result_used` tool (RDR-061) is a forward dependency — include in core server only after RDR-061 branch merges. Core server is 13 tools without it, 14 with it.

### Verification

- `uv run pytest` — full suite passes (MCP tests via shim)
- `uv run pytest -m integration` — round-trip tests pass for both servers
- Both `nx-mcp` and `nx-mcp-catalog` start and register correct tool counts
- Auto-approve hook accepts both prefixes
- `grep -r 'mcp__plugin_nx_nexus__catalog' nx/` returns 0 matches
- No demoted tool names appear in injected agent guidance (`subagent-start.sh`, `CONTEXT_PROTOCOL.md`)

## Key Files

| File | Action |
|------|--------|
| `src/nexus/mcp_infra.py` | Remove `mcp = FastMCP("nexus")` |
| `src/nexus/mcp_server.py` | Replace with re-export shim |
| `src/nexus/mcp/__init__.py` | NEW — re-exports for test compat |
| `src/nexus/mcp/core.py` | NEW — 14 core tools + FastMCP |
| `src/nexus/mcp/catalog.py` | NEW — 10 catalog tools + FastMCP |
| `pyproject.toml` | Add `nx-mcp-catalog` entry point |
| `nx/.mcp.json` | Add `nexus-catalog` server |
| `nx/hooks/scripts/auto-approve-nx-mcp.sh` | Update allow list |
| `nx/agents/*.md` (~24 files) | Update catalog tool prefixes |

## Research Findings

### RF-062-1: Agent usage data shows clear tool tier separation

**Source**: Empirical analysis of 16 agent `.md` files in `nx/agents/`.

9 of 16 agents (56%) reference zero catalog tools. Only 7 agents use catalog tools: `debugger`, `codebase-deep-analyzer`, `deep-analyst`, `developer`, `deep-research-synthesizer`, `architect-planner`, `knowledge-tidier`. All 16 agents reference core tools (search, store, memory, scratch). This validates the two-tier split — most agents would benefit from a smaller tool surface.

### RF-062-2: Catalog tool references are concentrated in linker/research agents

**Source**: `grep -rc` across `nx/` directory.

104 catalog tool occurrences across 24 files. The heaviest users: `auto-approve-nx-mcp.sh` (13 — the allow list), `nexus/SKILL.md` (10 — the cheat sheet), `deep-research-synthesizer.md` (10 — citation graph traversal), `subagent-start.sh` (9 — injected guidance). Agent files average 5-6 catalog refs each. This concentration means the migration impact is bounded and mechanical.

### RF-062-3: Auto-linker uses Python imports, not MCP calls

**Source**: Analysis of `mcp_infra.py` lines 130-152.

`catalog_auto_link()` calls `cat.link_if_absent()` directly via the `Catalog` Python class, not through MCP tools. The `store_put` → auto-link callback is entirely in-process. Splitting MCP servers does not affect auto-linking — both servers can safely instantiate independent `Catalog` singletons because:
- SQLite WAL mode handles concurrent writes
- `get_catalog()` checks JSONL mtime on every access and rebuilds when files change externally (lines 109-118)

### RF-062-4: mcp_infra.py singleton pattern is per-process safe

**Source**: Analysis of `mcp_infra.py` lazy singleton pattern.

Each singleton (`_t1_instance`, `_t3_instance`, `_catalog_instance`) uses a `threading.Lock` for thread safety within a process. Two separate MCP server processes each get their own singletons — no shared-memory coordination needed. ChromaDB Cloud is a shared service (safe for multi-client), SQLite uses WAL mode (safe for multi-process readers/writers), and the catalog JSONL files are append-only with git-backed integrity.

### RF-062-5: Demoted tools have CLI equivalents — no functionality lost

**Source**: Cross-reference of MCP tools with `src/nexus/commands/` CLI modules.

All 8 demoted tools have CLI equivalents: `nx store list`, `nx store delete`, `nx memory delete`, `nx collection info`, `nx collection verify`, `nx catalog link-audit`, `nx catalog link-bulk-delete`, `nx catalog unlink`. The CLI versions often have richer features (confirmation prompts, `--dry-run`, batch mode). Removing MCP exposure loses nothing — these are admin operations that benefit from human confirmation.

## Risks

**Low**: Purely organizational — no business logic changes. Both servers share singletons module and database files. SQLite WAL handles concurrent access.

**Medium**: ~100 tool name reference updates across 24 files. Mostly mechanical prefix rename, but 4 files (`subagent-start.sh`, `CONTEXT_PROTOCOL.md`, `nexus/SKILL.md`, `knowledge-tidier.md`) require content removal for demoted tools — not a uniform sed pass.

**Medium**: Two MCP server processes = ~2x memory baseline (each loads nexus package, chromadb, structlog). Acceptable for development workstations; monitor if deploying to constrained environments.

## Decision Log

- **Two servers, not three**: Admin tools are too few (8) to justify a third server process. Demoting to CLI-only is cleaner.
- **No MCP for taxonomy/feedback**: Per interface sprawl discussion — these are internal machinery that enhances existing operations, not new agent-facing tools. Taxonomy is CLI-only. Feedback logging is implicit (fires inside `store_put`/`catalog_link`).
- **`collection_list` stays in core**: Agents need to discover what collections exist before searching. Lightweight enough for the core set.
- **`store_list` stays in core**: Gate critique (RF-062-C1) showed knowledge-tidier and deep-research-synthesizer depend on it for full collection enumeration — `search` is not equivalent (requires a query string).
- **`memory_delete` stays in core**: Gate critique showed it's taught to all subagents via CONTEXT_PROTOCOL.md as a standard T2 operation paired with `memory_put`.
- **`result_used` is conditional**: Forward dependency on RDR-061 branch. Include when that branch merges; core server is 13 tools without it.
