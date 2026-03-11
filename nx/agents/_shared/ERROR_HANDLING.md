# Shared Error Handling Patterns

This file documents common error handling patterns for agents.

## General Principles

- Try primary method first
- Provide fallback options when available
- Log detailed error information
- Don't fail entire operation for single failure
- Document recovery options

## Common Error Categories

### Tool Execution Errors
- If primary tool fails: try alternative tool
- If all tools fail: report error with details
- Never silently swallow errors

### Knowledge Base Errors
- Connection failures: verify nexus MCP tools are available; fall back to `nx` CLI via Bash
- Write failures: check permissions and collection names
- Duplicate ID errors: append timestamp suffix

### Context Management Errors
- Missing context: use RECOVER protocol from Context Protocol
- Overflow prevention: chunk work appropriately
- Interruption recovery: resume from checkpoints when possible

## Storage Tier Errors

### T1 Scratch Errors

**Session scope confusion** (accessing scratch from wrong session):
- Error: scratch get returns "not found" even though you just wrote it
- Cause: T1 is session-scoped; subagents each have their own T1 scope
- Fix: Use T2 memory (memory_put/memory_get tools) for cross-agent relay within the same project
- Note: T1 scratch IDs are only valid within the session that created them

**Scratch entry not found after session restart**:
- Error: scratch `action="get"` returns error after session restart
- Cause: T1 is ephemeral — wiped at SessionEnd unless flagged
- Fix: Use scratch_manage `action="flag"` BEFORE session ends to auto-promote to T2
- Prevention: Flag valuable scratch entries immediately after creation

**Scratch promote fails (missing project)**:
- Error: scratch_manage `action="promote"` fails without project and title
- Fix: Always specify both: scratch_manage `action="promote", entry_id="<id>", project="{project}", title="notes.md"`

### T2 Memory Errors

**SQLite locked**:
- Error: `database is locked` from memory tools
- Cause: Another process holds a write lock
- Fix: Wait 1-2 seconds and retry; SQLite WAL mode minimizes this
- Fallback: Use T1 scratch for writes, promote to T2 when lock clears

**TTL expiry edge case (permanent entries)**:
- The `expires_at` field for permanent entries is `""` (empty string), not NULL
- The mandatory TTL guard is: `ttl_days > 0 AND expires_at != "" AND expires_at < now`
- The `expires_at != ""` guard is MANDATORY — permanent entries use `""` which sorts before ISO timestamps
- A 2-condition guard (without `!= ""`) would incorrectly delete permanent entries

**Memory entry not found**:
- Error: memory_get returns "Not found"
- Fix: Verify exact project and title values; use memory_get with empty title to list entries
- Fallback: Use memory_search tool for fuzzy retrieval

**TTL format errors**:
- Valid ttl parameter values for memory_put: integer days (e.g., `ttl=30`)
- Use `ttl=0` for permanent entries

### T3 Store Errors

**TTL guard pattern (MANDATORY)**:
```
# Always use 3-condition guard — 2-condition guard deletes permanent entries!
# CORRECT (3 conditions):
ttl_days > 0 AND expires_at != "" AND expires_at < now
# WRONG (2 conditions — deletes permanent entries):
ttl_days > 0 AND expires_at < now
```

**ChromaDB connectivity failure**:
- Error: search or store_put fails with connection error
- Fix: Fall back to `nx` CLI via Bash tool (degraded mode)
- Fallback: Write to T2 memory with note to promote to T3 later:
  Use memory_put tool: `content="content", project="{project}", title="pending-t3-promotion.md"`

**Voyage AI API limit**:
- Error: Rate limit or quota exceeded during embedding
- Fix: Reduce batch size; wait and retry
- Fallback: Store in T2 with `tags="pending-t3-promotion"` for later batch upload

**Collection name validation**:
- Collection names use `__` as separator (NOT `::`)
- Valid: `knowledge__myproject`, `code__nexus`
- Invalid: `knowledge::myproject` (colons are invalid in ChromaDB collection names)

**Duplicate document ID**:
- Error: `Document ID already exists`
- Fix: Append timestamp suffix: `insight-developer-topic-20260311`
