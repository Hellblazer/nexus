# RDR-034: MCP Server for Agent Storage Operations — Implementation Plan

**Epic**: nexus-wgc4
**RDR**: [docs/rdr/rdr-034-mcp-server-agent-storage.md](../rdr/rdr-034-mcp-server-agent-storage.md)
**Status**: Accepted (2026-03-11)

## Dependency Graph

```
P1 (MCP server + infra)     nexus-bl76
  ├── P2 (shared docs)      nexus-ql0a
  │     ├── P4 (agents)     nexus-466r
  │     ├── P5 (skills)     nexus-eksi
  │     └── P6 (commands)   nexus-ohua
  ├── P3 (nexus skill)      nexus-8nas
  └── P7 (verification)     nexus-bgzj  [depends on P4, P5, P6]
```

**Parallelizable**: P2 ∥ P3 (both depend only on P1). P4 ∥ P5 ∥ P6 (all depend on P2).

---

## Phase 1: MCP Server + Infrastructure (nexus-bl76)

### 1.1 Add `mcp` dependency to pyproject.toml

**File**: `pyproject.toml`

- Add `"mcp>=1.0"` to `dependencies` list
- Add `nx-mcp` entry point under `[project.scripts]`:
  ```toml
  nx-mcp = "nexus.mcp_server:main"
  ```
- Run `uv sync` to update lockfile

**Test**: `uv run python -c "import mcp; print(mcp.__version__)"`

### 1.2 Create MCP server (`src/nexus/mcp_server.py`)

**File**: `src/nexus/mcp_server.py` (new, ~250 lines)

8 tools using `mcp.server.fastmcp.FastMCP`:

| Tool | T1/T2/T3 | Implementation |
|------|----------|----------------|
| `search` | T3 | `search_cross_corpus()` via lazy `T3Database` singleton |
| `store_put` | T3 | `T3Database.put()` with `t3_collection_name()` |
| `store_list` | T3 | `T3Database.list_store()` |
| `memory_put` | T2 | `T2Database.put()` via per-call context manager |
| `memory_get` | T2 | `T2Database.get()` or `list_entries()` when title empty |
| `memory_search` | T2 | `T2Database.search()` |
| `scratch` | T1 | `T1Database.put/search/list_entries()` via lazy singleton |
| `scratch_manage` | T1+T2 | `T1Database.flag/promote()` |

**Architecture decisions to implement**:
- Lazy `T1Database` singleton (first call, not import time — SessionStart hook timing)
- Lazy `T3Database` singleton (1-2s ChromaDB Cloud init, reuse across session)
- Per-call `T2Database` context manager (SQLite WAL, microsecond open)
- `[T1 isolated]` prefix when T1 falls back to EphemeralClient (S-04)
- All errors return `"Error: {message}"` strings, never exceptions
- No ANSI codes, compact plain text output
- `main()` function: `mcp.run(transport="stdio")`

**Key imports**:
```python
from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.db import make_t3
from nexus.search_engine import search_cross_corpus
from nexus.corpus import resolve_corpus, t3_collection_name
from nexus.commands._helpers import default_db_path
from nexus.ttl import parse_ttl
```

**T3 construction** (from `commands/store.py:_t3()` pattern):
```python
from nexus.config import get_credential
# Use make_t3() which reads load_config() internally
```

**Test**: Write failing test first → then implement.

### 1.3 Register in `.mcp.json`

**File**: `nx/.mcp.json`

Add `"nexus"` server entry:
```json
{
  "sequential-thinking": { ... },
  "nexus": {
    "command": "nx-mcp",
    "args": []
  }
}
```

**Verification**: After `uv tool install .`, run `nx-mcp` and verify it starts (stdio transport, no output until JSON-RPC). Kill with Ctrl-C.

**CRITICAL**: Verify actual MCP tool name prefix by registering server, calling a tool from Claude Code, and inspecting the tool name. The RDR assumes `mcp__plugin_nx_nexus__<name>` — this must be confirmed before migrating 63 files.

### 1.4 Permission hook update

**File**: `nx/hooks/scripts/permission-request-stdin.sh`

Add after line 48 (sequential-thinking auto-approve):
```bash
# Auto-approve all nexus MCP tools (storage tiers, search)
if [[ "$TOOL" =~ ^mcp__plugin_nx_nexus__ ]]; then
  echo "allow"
  exit 0
fi
```

**Test**: Verify hook script syntax (`bash -n permission-request-stdin.sh`)

### 1.5 Update architecture docs

**File**: `docs/architecture.md`

Add to Module Map table:
```
| **MCP Server** | `mcp_server.py` | FastMCP server exposing T1/T2/T3 storage APIs as MCP tools |
```

### 1.6 Unit tests (`tests/test_mcp_server.py`)

**File**: `tests/test_mcp_server.py` (new)

All 8 tools tested with injected clients (no API keys):
- T1: `chromadb.EphemeralClient()` (bundled ONNX MiniLM)
- T2: In-memory SQLite via `T2Database(Path(":memory:"))` — note: T2Database expects a Path, so use temp file
- T3: `chromadb.EphemeralClient()` with `DefaultEmbeddingFunction` override

Test cases:
1. `test_search` — happy path, returns results
2. `test_store_put` — stores content, returns ID
3. `test_store_list` — lists entries after put
4. `test_memory_put` — stores memory entry
5. `test_memory_get_by_title` — retrieves by project+title
6. `test_memory_get_empty_title_lists` — empty title returns listing
7. `test_memory_search` — FTS5 search returns results
8. `test_scratch_put` — put action stores and returns ID
9. `test_scratch_search` — search action returns results
10. `test_scratch_list` — list action returns entries
11. `test_scratch_manage_flag` — flag action marks entry
12. `test_scratch_manage_promote` — promote action copies to T2
13. `test_error_missing_params` — returns `"Error: ..."` string
14. `test_no_ansi_in_output` — no ANSI escape codes
15. `test_t1_isolated_prefix` — EphemeralClient fallback shows `[T1 isolated]`

**Pattern**: Import MCP tool functions directly, inject test clients via module-level overrides or parameterized fixtures.

### 1.7 Session tests (`tests/test_mcp_session.py`)

**File**: `tests/test_mcp_session.py` (new)

1. Create temp sessions dir with session file keyed to test PID
2. Monkeypatch `nexus.db.t1.SESSIONS_DIR` to temp dir
3. Instantiate `T1Database` (MCP server context)
4. Write scratch entry
5. Verify visible from second `T1Database` instance
6. Validate `find_ancestor_session()` resolved correct address

### 1.8 Concurrency tests (`tests/test_mcp_concurrency.py`)

**File**: `tests/test_mcp_concurrency.py` (new)

Multi-process tests (simulating multiple Claude Code sessions):
1. T1 isolation: Spawn 2+ processes with separate session identities, verify writes in one don't appear in the other
2. T2 concurrent writes: N processes writing to same SQLite file via `multiprocessing`, verify all entries persisted (SQLite WAL)
3. T2 concurrent reads during writes: reader processes don't block or crash
4. T3 concurrent reads: Multiple parallel search calls don't interfere (use EphemeralClient for unit test)

**Note**: Use `multiprocessing.Process` (not threads) to validate actual cross-process SQLite WAL contention — this matches the real deployment scenario of multiple `nx-mcp` processes.

### 1.9 Integration test (`tests/test_mcp_integration.py`)

**File**: `tests/test_mcp_integration.py` (new, `@pytest.mark.integration`)

1. Start `nx-mcp` as subprocess via entry point
2. Connect via MCP client SDK (`mcp.client`)
3. Call each of the 8 tools, verify round-trip
4. Verify T1 entries written via MCP are visible within same session
5. Verify T1 entries are NOT visible from a separate `nx-mcp` process

### 1.10 Cross-reference RDR-023

**File**: `docs/rdr/rdr-023-agent-tool-permissions-audit.md`

Add cross-reference note: RDR-034 supersedes RDR-023 Q3 ("no need for dedicated MCP wrappers"). The assumption that Bash auto-approval was sufficient was incorrect for background agents.

### Success Criteria (Phase 1)
- [ ] `uv run pytest tests/test_mcp_server.py` — all green
- [ ] `uv run pytest tests/test_mcp_session.py` — all green
- [ ] `uv run pytest tests/test_mcp_concurrency.py` — all green
- [ ] `uv run pytest tests/test_mcp_integration.py -m integration` — all green (requires API keys)
- [ ] `nx-mcp` starts via entry point
- [ ] MCP tool name prefix verified against Claude Code
- [ ] Permission hook syntax valid

---

## Phase 2: Shared Agent Docs (nexus-ql0a)

**Files** (4 files, ~72 references):
- `nx/agents/_shared/CONTEXT_PROTOCOL.md` — 41 refs
- `nx/agents/_shared/ERROR_HANDLING.md` — 12 refs
- `nx/agents/_shared/RELAY_TEMPLATE.md` — 13 refs
- `nx/agents/_shared/MAINTENANCE.md` — 6 refs

**Migration pattern** (from RDR-034 §Migration Notes):

Before:
```bash
nx search "topic" --corpus knowledge --n 5
nx memory put "content" --project {project} --title findings.md
echo "content" | nx store put - --collection knowledge --title "title" --tags "tag"
nx scratch put "hypothesis" --tags "debug"
```

After:
```
Use search tool: query="topic", corpus="knowledge", n=5
Use memory_put tool: content="content", project="{project}", title="findings.md"
Use store_put tool: content="content", collection="knowledge", title="title", tags="tag"
Use scratch tool: action="put", content="hypothesis", tags="debug"
```

**Additional for CONTEXT_PROTOCOL.md**:
- Add "Degraded Mode" note (S-05): if nexus MCP tools unavailable, fall back to `nx` CLI via Bash
- Update tier descriptions to reference MCP tools as primary, CLI as fallback

**Test**: `grep -c 'nx search\|nx memory\|nx store\|nx scratch' nx/agents/_shared/*.md` → should be 0 (only MCP references remain)

### Success Criteria (Phase 2)
- [ ] Zero CLI nx references in shared agent docs
- [ ] CONTEXT_PROTOCOL includes degraded mode note
- [ ] All MCP tool names match verified prefix from P1

---

## Phase 3: Nexus Skill Docs (nexus-8nas)

**Files** (2 files, ~58 references):
- `nx/skills/nexus/SKILL.md` — 13 refs
- `nx/skills/nexus/reference.md` — 45 refs

These are the "how to use nx" quick-reference docs consumed by agents. Replace all CLI examples with MCP tool equivalents.

**Note**: These files are agent-consumed (part of `nx/` plugin directory), not human-facing docs (`docs/`). The `docs/cli-reference.md` keeps CLI syntax.

### Success Criteria (Phase 3)
- [ ] Zero CLI nx references in nexus skill docs
- [ ] Reference includes all 8 MCP tools with parameter descriptions

---

## Phase 4: Agent Files (nexus-466r)

**Files** (14 agents, ~287 references + 14 frontmatter updates):

| Agent | CLI Refs | Notes |
|-------|----------|-------|
| knowledge-tidier.md | 27 | Heaviest user — critical path |
| deep-research-synthesizer.md | 33 | Heavy search + store |
| strategic-planner.md | 29 | Memory + search |
| plan-auditor.md | 23 | Memory + search |
| substantive-critic.md | 22 | Memory + search |
| developer.md | 22 | Memory + scratch |
| architect-planner.md | 20 | Memory + search |
| codebase-deep-analyzer.md | 20 | Memory + search |
| deep-analyst.md | 18 | Search + scratch |
| debugger.md | 17 | Scratch + memory |
| orchestrator.md | 11 | Delegation refs |
| code-review-expert.md | 10 | Memory + scratch |
| test-validator.md | 9 | Memory + scratch |
| pdf-chromadb-processor.md | 7 | Store + search |

**Two changes per agent**:

1. **Frontmatter `tools:` list** — add MCP tools:
```yaml
tools: ["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_nexus__search", "mcp__plugin_nx_nexus__store_put", "mcp__plugin_nx_nexus__store_list", "mcp__plugin_nx_nexus__memory_put", "mcp__plugin_nx_nexus__memory_get", "mcp__plugin_nx_nexus__memory_search", "mcp__plugin_nx_nexus__scratch", "mcp__plugin_nx_nexus__scratch_manage", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]
```
Only add the MCP tools each agent actually uses (check body references).

2. **Body CLI→MCP** — replace all `nx search/memory/store/scratch` command examples

**Note**: Bash stays in tools list for non-nx operations (git, bd, pytest, etc.)

### Success Criteria (Phase 4)
- [ ] All 14 agents have MCP tools in frontmatter
- [ ] Zero CLI nx references in agent bodies
- [ ] Each agent's MCP tool list matches its actual usage

---

## Phase 5: Skill Files (nexus-eksi)

**Files** (26 skills, ~135 references — 2 nexus skill files done in P3):

Skills with highest reference counts:
- rdr-gate/SKILL.md (13), knowledge-tidying/SKILL.md (13), rdr-research/SKILL.md (12), rdr-close/SKILL.md (10)

**Pattern**: Same CLI→MCP text replacement. Skills don't have frontmatter tools lists — only body text changes.

**Note**: Skill files do NOT contain `!{...}` bash blocks — those only exist in command files (Phase 6). All `nx` CLI references in skill bodies are prose/code-example text and should be fully migrated to MCP tool syntax.

### Success Criteria (Phase 5)
- [ ] Zero CLI nx references in skill body text
- [ ] All 26 skill files migrated (28 total minus 2 nexus skill files in P3)

---

## Phase 6: Command Files (nexus-ohua)

**Files** (17 commands, ~49 references):

Commands are slash-command definitions. The body text instructs agents on what to do — these references should use MCP tools.

**Exception**: Commands that contain `!{...}` bash blocks execute as shell processes and should retain CLI syntax.

### Success Criteria (Phase 6)
- [ ] Zero CLI nx references in command body text (outside `!{...}` blocks)

---

## Phase 7: End-to-End Verification (nexus-bgzj)

### 7.1 Full test suite
```bash
uv run pytest
```
All tests must pass (no regressions).

### 7.2 Grep verification
```bash
# Agents and skills: should have ZERO nx CLI references (skills have no shell blocks)
grep -rn 'nx search\|nx memory\|nx store\|nx scratch' nx/agents/ nx/skills/ | wc -l
# Expected: 0

# Commands: filter out !{...} shell blocks (these are shell processes, retain CLI)
# Use a script to check only lines outside !{...} blocks
python3 -c "
import re, pathlib
for f in pathlib.Path('nx/commands').glob('*.md'):
    text = f.read_text()
    # Remove !{...} blocks
    clean = re.sub(r'!\{.*?\}', '', text, flags=re.DOTALL)
    matches = re.findall(r'nx (search|memory|store|scratch)', clean)
    if matches:
        print(f'{f}: {len(matches)} CLI refs outside shell blocks')
"
# Expected: no output
```

### 7.3 Permission hook test
Verify `permission-request-stdin.sh` auto-approves `mcp__plugin_nx_nexus__search`:
```bash
echo '{"tool":"mcp__plugin_nx_nexus__search"}' | bash nx/hooks/scripts/permission-request-stdin.sh
# Expected: "allow"
```

### 7.4 Background agent smoke test
Dispatch knowledge-tidier as background agent, verify it persists to T3 via MCP tools (not Bash).

### Success Criteria (Phase 7)
- [ ] `uv run pytest` — all green
- [ ] No CLI nx references in agent-consumed files (outside shell blocks)
- [ ] Permission hook approves MCP tools
- [ ] Background agent successfully persists via MCP

---

## Risk Register

| Risk | Mitigation |
|------|-----------|
| MCP tool name prefix doesn't match assumed `mcp__plugin_nx_nexus__` | Verify in P1.3 before any migration |
| `mcp` package conflicts with existing deps (chromadb, etc.) | Test `uv sync` early in P1.1 |
| T2Database path construction differs between CLI and MCP server | Use same `default_db_path()` helper |
| Shell scripts in `!{...}` blocks accidentally migrated | Explicitly exclude `!{...}` blocks in P6 (skills don't have them) |
| T1 lazy init races with first tool call | T1Database already handles this gracefully with fallback |

## File Inventory Summary

| Category | Count | References |
|----------|-------|------------|
| New files | 5 | mcp_server.py, test_mcp_server.py, test_mcp_session.py, test_mcp_concurrency.py, test_mcp_integration.py |
| Infrastructure | 4 | pyproject.toml, .mcp.json, permission hook, architecture.md |
| Shared agent docs | 4 | 72 refs |
| Nexus skill docs | 2 | 58 refs |
| Agent files | 14 | 340 refs (287 body + 14 frontmatter) |
| Skill files | 26 | 135 refs (all prose — no shell blocks in skills) |
| Command files | 17 | 49 refs (outside `!{...}` shell blocks) |
| Cross-reference | 1 | RDR-023 Q3 supersession note |
| **Total** | **73 files** | **~654 references** |
