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
- Connection failures: verify nx CLI is available (`nx doctor`)
- Write failures: check nx store permissions
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
- Fix: Use T2 memory (`nx memory put/get`) for cross-agent relay within the same project
- Note: T1 scratch IDs are only valid within the session that created them

**Scratch entry not found after session restart**:
- Error: `nx scratch get <id>` returns error after session restart
- Cause: T1 is ephemeral — wiped at SessionEnd unless flagged
- Fix: Use `nx scratch flag <id>` BEFORE session ends to auto-promote to T2
- Prevention: Flag valuable scratch entries immediately after creation

**Scratch promote fails (missing project)**:
- Error: `nx scratch promote <id>` fails without `--project` and `--title`
- Fix: Always specify both flags: `nx scratch promote <id> --project {project} --title notes.md`

### T2 Memory Errors

**SQLite locked**:
- Error: `database is locked` from `nx memory` commands
- Cause: Another process holds a write lock
- Fix: Wait 1-2 seconds and retry; SQLite WAL mode minimizes this
- Fallback: Use T1 scratch for writes, promote to T2 when lock clears

**TTL expiry edge case (permanent entries)**:
- The `expires_at` field for permanent entries is `""` (empty string), not NULL
- The mandatory TTL guard is: `ttl_days > 0 AND expires_at != "" AND expires_at < now`
- The `expires_at != ""` guard is MANDATORY — permanent entries use `""` which sorts before ISO timestamps
- A 2-condition guard (without `!= ""`) would incorrectly delete permanent entries

**Memory entry not found**:
- Error: `nx memory get` returns nothing
- Fix: Verify exact `--project` and `--title` values; use `nx memory list --project {project}` to see available entries
- Fallback: Use `nx memory search "topic" --project {project}` for fuzzy retrieval

**TTL format errors**:
- Valid formats: `30d`, `4w`, `permanent`, `never` (`permanent` and `never` are both aliases for no-expiry)
- Invalid: `30`, `"30 days"`, `30days`
- Always use the short-form: `--ttl 30d`

### T3 Store Errors

**TTL guard pattern (MANDATORY)**:
```bash
# Always use 3-condition guard — 2-condition guard deletes permanent entries!
# CORRECT (3 conditions):
ttl_days > 0 AND expires_at != "" AND expires_at < now
# WRONG (2 conditions — deletes permanent entries):
ttl_days > 0 AND expires_at < now
```

**ChromaDB connectivity failure**:
- Error: `nx store put` or `nx search` fails with connection error
- Fix: Check `nx doctor` for ChromaDB + Voyage AI API status
- Fallback: Write to T2 memory with note to promote to T3 later:
  `nx memory put "content" --project {project} --title pending-t3-promotion.md`

**Voyage AI API limit**:
- Error: Rate limit or quota exceeded during embedding
- Fix: Reduce batch size; wait and retry
- Fallback: Store in T2 with `--tags "pending-t3-promotion"` for later batch upload

**Collection name validation**:
- Collection names use `__` as separator (NOT `::`)
- Valid: `knowledge__myproject`, `code__nexus`
- Invalid: `knowledge::myproject` (colons are invalid in ChromaDB collection names)

**Duplicate document ID**:
- Error: `Document ID already exists`
- Fix: Append timestamp suffix: `insight-developer-topic-$(date +%Y%m%d)`

