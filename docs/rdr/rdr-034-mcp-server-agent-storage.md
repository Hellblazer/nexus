---
title: "MCP Server for Agent Storage Operations"
id: RDR-034
type: Architecture
status: closed
accepted_date: 2026-03-11
closed_date: 2026-03-19
close_reason: implemented
priority: P0
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-11
related_issues:
  - "RDR-023 - Permission Hook Auto-Approval"
  - "RDR-032 - Indexer Decomposition"
---

# RDR-034: MCP Server for Agent Storage Operations

## Problem Statement

Agents spawned in certain Claude Code invocation contexts (background agents via `run_in_background: true`, restricted permission modes) cannot use the Bash tool, causing all `nx` CLI commands to fail. The permission hook already auto-approves all `nx` commands (lines 120-124 of `permission-request-stdin.sh`), but when Bash itself is unavailable, the hook never fires.

**Observed failure** (2026-03-11): The knowledge-tidier agent, dispatched as a background agent, could not run `nx store put` to persist consolidated knowledge to T3. The agent completed its analysis work but failed at the persistence step. The user had to intervene and run the command manually from the foreground session. This is not a one-time incident — it affects any agent dispatched in a context where Bash is gated.

**Root cause**: Bash is a gated tool type in Claude Code's agent framework. Depending on invocation context (background dispatch, permission mode, tool-list restrictions), agents may not have Bash access regardless of hook configuration. MCP tools, by contrast, are first-class tool calls that are always available when the MCP server is registered and the tool appears in the agent's `tools:` frontmatter.

**Scope clarification**: Not all agents are always affected. Foreground agents with Bash access can use `nx` CLI commands without issue. The problem is that the current architecture has a single path for nx operations (Bash), and that path is unreliable across invocation contexts. MCP tools provide a second, reliable path specifically for the critical storage operations (T1/T2/T3 read/write/search). Non-nx Bash operations (`git`, `bd`, `pytest`) remain Bash-only and may also be unavailable in restricted contexts — but those operations are less critical than data persistence and are out of scope for this RDR.

**Impact**: 13 of 14 agents persist state via nx commands. When these agents run in Bash-restricted contexts, they silently lose their output. The orchestrator (agent 14) delegates without direct nx calls and is unaffected.

## Proposed Solution

Create a thin MCP server (`src/nexus/mcp_server.py`) that exposes nexus storage tier APIs as MCP tools. MCP tools are first-class tool calls that bypass Bash permissions entirely.

### Architecture

The MCP server imports nexus Python modules directly — `T1Database`, `T2Database`, `T3Database`, `search_cross_corpus` — and calls their APIs without subprocess invocation.

```
Claude Code Process
    │
    ├── Agent (foreground or background)
    │     └── calls mcp__plugin_nx_nexus__search (MCP tool)
    │           └── JSON-RPC → MCP server process
    │                 └── T3Database.search() (direct Python)
    │
    └── MCP Server Process (nx-mcp, stdio transport)
          ├── T1Database (PPID chain → session ChromaDB server)
          ├── T2Database (SQLite WAL, per-call context manager)
          └── T3Database (lazy singleton, ChromaDB Cloud + Voyage AI)
```

### MCP Tool Set (8 tools, ~750 context tokens)

Server name: `"nexus"` in `.mcp.json` → tools are `mcp__plugin_nx_nexus__<name>`

| Tool | Parameters | Replaces |
|------|-----------|----------|
| `search` | query, corpus, n, hybrid | `nx search` |
| `store_put` | content, title, collection, tags, ttl | `nx store put` / `echo ... \| nx store put -` |
| `store_list` | collection, limit | `nx store list` |
| `memory_put` | content, project, title, tags, ttl | `nx memory put` |
| `memory_get` | project, title (empty title = list entries) | `nx memory get` / `nx memory list` |
| `memory_search` | query, project | `nx memory search` |
| `scratch` | action (put/search/list), content, tags, query, n | `nx scratch put/search/list` |
| `scratch_manage` | action (flag/promote), id, project, title | `nx scratch flag/promote` |

### Design Decisions

**8 tools, not 13 or 4:**
- `memory_get` doubles as `memory_list` when title is empty — eliminates a tool
- `scratch` consolidates 3 primary operations (`put/search/list`) behind an action discriminator
- `scratch_manage` handles lifecycle operations (`flag/promote`) separately — these require `id` and are structurally different from primary ops
- `store_get` omitted — agents rarely look up T3 by ID; search covers retrieval. Can be added as tool #9 if needed.
- `unflag` omitted — agents use `persist=True` at write time instead
- Enough tools for model clarity; few enough for token efficiency (~850 context tokens)

**FastMCP decorator pattern:**
- Uses `mcp.server.fastmcp.FastMCP` — auto-generates schemas from type hints + docstrings
- Docstrings become tool descriptions (keep short for token efficiency)
- `Annotated[type, Field(description="...")]` for per-parameter docs

**Lazy T3 singleton:**
- ChromaDB Cloud + Voyage AI initialization takes 1-2 seconds
- Reuse across the session lifetime (MCP server stays alive)
- T2 uses per-call context manager (SQLite opens in microseconds)

**Return format:**
- Compact plain text (no ANSI colors, no formatting)
- Errors return `"Error: {message}"` strings — no exceptions that surface as framework errors

### T1 Session Sharing (Solved by Construction)

Each Claude Code session spawns exactly one `nx-mcp` process at plugin load. All agents within the session — foreground, background, and subagents — share the same MCP server process via JSON-RPC. This means:

1. `T1Database` is instantiated once (lazily, on first tool call) inside the MCP server process
2. The MCP server's PPID chain resolves to the Claude Code session PID
3. `find_ancestor_session()` finds the session file → connects to the session's ChromaDB HTTP server
4. All agents share the same T1 scratch through the single MCP server process — no per-agent PPID resolution needed

This is superior to the CLI approach where every `nx scratch put` Bash call spawned a new Python process that independently re-resolved the PPID chain.

**Multi-session isolation**: Different Claude Code windows spawn separate `nx-mcp` processes with separate PPID chains, so T1 scratch is correctly isolated between sessions. T2 and T3 are shared across sessions (correct — they are persistent storage).

**Timing constraint**: MCP servers start at plugin load, but the `SessionStart` hook creates the session's ChromaDB HTTP server. `T1Database` must be lazily initialized (on first tool call, not at process start) to ensure the session file exists when resolution occurs. `T3Database` already uses this lazy singleton pattern.

**Fallback**: If PPID chain resolution fails, `T1Database` falls back to a local `EphemeralClient` (existing behavior). This preserves functionality but isolates the MCP server's scratch from any CLI-based scratch.

### Multi-Process Concurrency

Multiple Claude Code sessions may run concurrently, each with its own `nx-mcp` process:

- **T1**: Isolated by design (separate session servers, separate PPID chains). No concurrency concern.
- **T2**: Multiple processes write to the same `~/.config/nexus/memory.db`. SQLite WAL mode supports concurrent readers and one writer. Contention is possible under heavy parallel writes but WAL handles it with retry. Testing must verify behavior under concurrent access.
- **T3**: Multiple processes hit ChromaDB Cloud simultaneously. Cloud service handles concurrency server-side. No client-side concern beyond API rate limits (existing retry logic in `nexus.retry` handles transient errors).

### .mcp.json Registration

```json
{
  "sequential-thinking": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]
  },
  "nexus": {
    "command": "nx-mcp",
    "args": []
  }
}
```

The server key `"nexus"` determines the tool name prefix: `mcp__plugin_nx_nexus__<tool_name>`. This naming convention must be verified against Claude Code's actual tool-name construction logic before committing to it in 63 files. The verification should be part of Phase 1 (create server, register, call a tool, inspect the actual tool name).

### Permission Hook Integration

Add pre-approval for all nexus MCP tools to `nx/hooks/scripts/permission-request-stdin.sh`:

```bash
# Auto-approve all nexus MCP tools (storage tiers, search)
if [[ "$TOOL" =~ ^mcp__plugin_nx_nexus__ ]]; then
  echo "allow"
  exit 0
fi
```

Keep existing Bash `nx` auto-approval for human interactive use and hook scripts.

## Scope of Changes

### New Files (3)

| File | Purpose |
|------|---------|
| `src/nexus/mcp_server.py` | MCP server (~200 lines, 8 tools) |
| `tests/test_mcp_server.py` | Unit tests with ephemeral/mock clients |
| `docs/rdr/rdr-034-mcp-server-agent-storage.md` | This document |

### Modified Files — Infrastructure (4)

| File | Change |
|------|--------|
| `pyproject.toml` | Add `mcp>=1.0` dependency, `nx-mcp` entry point |
| `nx/.mcp.json` | Register `"nexus"` MCP server |
| `nx/hooks/scripts/permission-request-stdin.sh` | Add MCP tool auto-approval block |
| `docs/architecture.md` | Add `mcp_server.py` to module map |

### Modified Files — Agent-Consumed Documentation (63)

All CLI `nx` command references in agent-consumed files are replaced with MCP tool calls. Human-facing docs (`docs/`, `nx/README.md`, `CHANGELOG.md`) and hook scripts retain CLI syntax.

| Category | Files | References | Description |
|----------|-------|------------|-------------|
| Shared agent docs | 4 | 72 | CONTEXT_PROTOCOL, ERROR_HANDLING, RELAY_TEMPLATE, MAINTENANCE |
| Agents | 14 | 287 | Frontmatter tools list + body CLI→MCP |
| Skills | 28 | 155 | Body CLI→MCP examples |
| Commands | 17 | 50 | Body CLI→MCP delegation references |

### Not Modified

| Category | Files | Reason |
|----------|-------|--------|
| Hook scripts | 2 | Shell processes, not Claude Code agents |
| Human docs | ~5 | `docs/` directory stays CLI for human reference |
| `nx/README.md` | 1 | Human-facing project overview |
| `CHANGELOG.md` | 1 | Release notes |

## Testing Strategy

### Unit Tests (`tests/test_mcp_server.py`)

All 8 tools tested with injected clients:
- T1: `chromadb.EphemeralClient` (bundled ONNX MiniLM, no API keys)
- T2: In-memory SQLite via `T2Database(":memory:")`
- T3: `chromadb.EphemeralClient` with `DefaultEmbeddingFunction` override

Test coverage:
- Happy path for all 8 tools
- Error cases (missing required params, connection failures)
- Return format validation (plain text, no ANSI)
- `scratch` action discriminator (all 5 actions)
- `memory_get` with empty title returns listing

### T1 PPID Chain Verification (`tests/test_mcp_session.py`)

Note: `find_ancestor_session()` in `session.py` already accepts a `sessions_dir` parameter. `T1Database.__init__()` passes the module-level `SESSIONS_DIR` constant. Tests will monkeypatch `nexus.db.t1.SESSIONS_DIR` (or `nexus.session.SESSIONS_DIR`) to point to a temp directory, avoiding dependency on live session state.

1. Create a temp sessions directory with a session file keyed to the test process PID
2. Monkeypatch `SESSIONS_DIR` to the temp directory
3. Instantiate `T1Database` (simulating MCP server context)
4. Write a scratch entry
5. Verify the entry is visible via a separate `T1Database` instance (simulating CLI)
6. Validate `find_ancestor_session()` resolved the correct server address

### Multi-Process Concurrency Tests (`tests/test_mcp_concurrency.py`)

1. Spawn 2+ MCP server processes with separate session identities
2. Verify T1 scratch is isolated between them (writes in one don't appear in the other)
3. Verify T2 memory is shared — concurrent writes from multiple processes succeed (SQLite WAL)
4. Stress test: parallel T2 writes from N processes, verify all entries persisted
5. Verify T3 concurrent reads don't interfere (multiple search calls in parallel)

### Integration Test (`@pytest.mark.integration`)

1. Start MCP server as subprocess via `nx-mcp` entry point
2. Connect via MCP client SDK
3. Call each tool, verify round-trip
4. Verify T1 entries written via MCP are visible within the same session
5. Verify T1 entries are NOT visible from a separate session's MCP server

## Alternatives Considered

### A: Subprocess delegation (rejected)

MCP server shells out to `nx` CLI commands. Rejected because:
- Same Bash permission problem for the subprocess
- Shell quoting issues with content containing special characters
- No connection reuse (T3 reconnects every call, 1-2s overhead)
- Stdin piping pattern (`echo | nx store put -`) is fragile

### B: Option B documentation (rejected by user)

Keep CLI examples in agent docs, add MCP tools to frontmatter only. Agents translate CLI examples to MCP calls. Rejected because:
- Agents may still attempt Bash for nx commands when CLI examples are present
- "Fraught with peril" — user's assessment of Bash-mediated agent operations
- Cleaner to have single source of truth for agent operations

### C: Fewer tools via single dispatcher (rejected)

Single `nx` tool with command string parameter. Rejected because:
- Least discoverable for models
- Agent must know full command syntax
- No schema validation on parameters

## Gate Critique Responses

Issues raised during RDR-034 gate review (substantive-critic, 2026-03-11).

### S-02: scratch action discriminator

The critic correctly noted that a 5-action discriminator with 7 optional parameters is model-unfriendly. **Resolution**: Split scratch into two tools:

- `scratch` — primary operations: `put`, `search`, `list` (3 actions, clear param subsets)
- `scratch_manage` — lifecycle operations: `flag`, `promote` (2 actions, require `id`)

This brings the total to 8 tools (~850 tokens). The `unflag` operation (O-04) is omitted intentionally — agents can use `persist=True` at write time instead of flagging after the fact.

### S-04: EphemeralClient fallback visibility

The `scratch` tool must surface the fallback state. When `T1Database` falls back to `EphemeralClient`, the tool return string will be prefixed with `[T1 isolated] ` so agents and logs can detect degraded state. Example: `[T1 isolated] Stored: abc12345`.

### S-05: MCP server startup failure recovery

The updated `CONTEXT_PROTOCOL.md` will include a "Degraded Mode" note: if nexus MCP tools are unavailable, fall back to `nx` CLI via Bash. This preserves human-operator recovery without complicating the normal agent path.

### S-06: store_list return format

`store_list` returns: ID (16-char hex), title (truncated to 40 chars), TTL status, indexed date, and tags — matching the current `nx store list` CLI output format. Content is NOT included (use `search` for content retrieval). If full content retrieval by ID becomes needed, `store_get` can be added as tool #9 in a future iteration.

### O-01: RDR-023 Q3 supersession

RDR-023 Q3 resolved "no need for dedicated MCP wrappers" based on the assumption that Bash auto-approval was sufficient. Production observation has shown this assumption was incorrect for background agents. **RDR-034 supersedes RDR-023 Q3.** The RDR-023 document should be annotated with a cross-reference to RDR-034.

## Migration Notes

### Documentation Pattern

**Before** (CLI in agent/skill docs):
```bash
nx search "topic" --corpus knowledge --n 5
nx memory put "content" --project {project} --title findings.md
echo "content" | nx store put - --collection knowledge --title "title" --tags "tag"
nx scratch put "hypothesis" --tags "debug"
```

**After** (MCP tools in agent/skill docs):
```
Use search tool: query="topic", corpus="knowledge", n=5
Use memory_put tool: content="content", project="{project}", title="findings.md"
Use store_put tool: content="content", collection="knowledge", title="title", tags="tag"
Use scratch tool: action="put", content="hypothesis", tags="debug"
```

### Agent Frontmatter

All 14 agents that use nx commands add MCP tools to their `tools:` list:

```yaml
tools: ["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_nexus__search", "mcp__plugin_nx_nexus__store_put", "mcp__plugin_nx_nexus__store_list", "mcp__plugin_nx_nexus__memory_put", "mcp__plugin_nx_nexus__memory_get", "mcp__plugin_nx_nexus__memory_search", "mcp__plugin_nx_nexus__scratch", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]
```

Bash remains in the tools list for non-nx operations (git, bd, pytest, etc.).

## Post-Implementation Note (RDR-035, 2026-03-12)

The MCP server architecture is correct and working. However, the statement in the Problem
Statement — "MCP tools, by contrast, are first-class tool calls that are always available
when the MCP server is registered and the tool appears in the agent's `tools:` frontmatter"
— was incorrect for plugin-defined agents.

Claude Code has a confirmed bug (GitHub #13605, #21560, #25200) where explicit `tools:`
declarations in plugin agent frontmatter cause MCP tools to be filtered out of the agent's
tool inventory. The fix (RDR-035) is to remove the `tools:` field entirely, allowing agents
to inherit all tools including MCP from the parent session. The PermissionRequest hook
provides runtime enforcement.

The MCP server, tool definitions, agent body references, and permission hook are all correct
as designed. Only the `tools:` frontmatter in agent files needed to change.

See `docs/rdr/rdr-035-plugin-agent-mcp-tool-access.md`.

## Success Criteria

- [x] All 8 MCP tools pass unit tests with ephemeral clients
- [x] T1 PPID chain verification test passes
- [x] Integration test confirms round-trip MCP tool calls
- [x] All 63 agent-consumed files updated to MCP tool references
- [x] Permission hook pre-approves `mcp__plugin_nx_nexus__*` tools
- [ ] ~~No agent references `nx scratch/memory/store/search` via Bash~~ — agents may use either path
- [x] Background agent (knowledge-tidier) successfully persists to T3 via MCP — **Verified 2026-03-12 after RDR-035 fix**
- [x] `uv run pytest` passes (no regressions)

## Implementation Phases

| Phase | Files | Description |
|-------|-------|-------------|
| 1 | 3 new + 4 infra | MCP server, tests, pyproject.toml, .mcp.json, permission hook, architecture.md |
| 2 | 4 shared docs | CONTEXT_PROTOCOL, ERROR_HANDLING, RELAY_TEMPLATE, MAINTENANCE |
| 3 | 2 nexus skill | SKILL.md, reference.md |
| 4 | 14 agents | Frontmatter tools + body CLI→MCP |
| 5 | 28 skills | CLI→MCP in skill bodies |
| 6 | 17 commands | CLI→MCP in command bodies |
| 7 | Verification | End-to-end background agent test |
