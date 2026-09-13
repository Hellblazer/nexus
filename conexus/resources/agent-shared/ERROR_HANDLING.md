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

**Write reported an error**:
- Error: the tool returns a string beginning `Error: ` from a memory/store/scratch tool
- Cause: the service backing that tier is unreachable, or the session's token
  was rejected (a T1 401 says so explicitly and names reconnecting the MCP
  server as the remedy)
- Fix: retry once; if it persists, treat the write as NOT landed and say so in
  your report rather than reporting the work as stored
- **A failed write is never a silent condition to route around.** Do not fall
  back to another tier and move on — findings that exist only in a tier nobody
  will read are lost. State the failure in your final message and restate the
  findings inline.

  (Historical: this section described `database is locked` and SQLite WAL
  retry semantics. There is no SQLite — T1/T2/T3 are all Postgres behind the
  engine, RDR-158 P4 — so that error cannot occur and its retry advice was
  guidance for a condition that no longer exists.)

**TTL expiry edge case (permanent entries)**:
- `memory_put`'s `ttl` is `int | None`. `None` (the default — omit the parameter) means
  PERMANENT; the row is never swept. There is no `expires_at` field visible at the MCP/HTTP
  layer, no empty-string sentinel, and no SQL guard for a caller to write — this is a service
  behind `HttpMemoryStore`/`POST /v1/memory/put`, not a table an agent touches directly.
- `ttl=0` and any negative `ttl` are REJECTED with a 400 by the engine (RDR-194 D5,
  nexus-tk070.p6a) — there is no coercion of `0` to permanent, or to anything else.

**Memory entry not found**:
- Error: memory_get returns "Not found"
- Fix: Verify exact project and title values; use memory_get with empty title to list entries
- Fallback: Use memory_search tool for fuzzy retrieval

**TTL format errors**:
- Valid `ttl` values for `memory_put`: omit for permanent, or a positive integer number of
  days for a row meant to expire (extended on read: `effective_ttl = ttl * (1 + ln(access_count
  + 1))`, nexus-473mx). `ttl=30` for every write is the retired default, not a convention to
  reproduce — pass an explicit TTL only when the content genuinely should expire.
- `ttl<=0` (including `ttl=0`) is refused outright — pass no `ttl` at all instead.

### T3 Store Errors

**TTL model (T3 has no `expires_at` column)**: both `T3Database.put` and `HttpVectorClient.put`
compute expiry as `indexed_at + ttl_days`, so there is no stored expiry timestamp to guard in
SQL. `ttl_days=0` is rejected outright by both (RDR-194 D5, nexus-tk070.p6b) — never coerced to
permanent. `store_put`'s `ttl` parameter is a string (`"permanent"`, `"Nd"`, `"Nw"`), not the
integer-or-None shape `memory_put`/`plan_save` use.

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
- Collection names use `__` as separator (NOT `::`).
- Conformant shape (RDR-103): `<content_type>__<owner>__<embedding_model>__v<n>`, e.g. `knowledge__myproject-1-1__voyage-context-3__v1`, `code__nexus-1-1__voyage-code-3__v1`.
- Operators may type the short form `knowledge__myproject` to MCP / `nx` commands; `t3_collection_name` auto-promotes it to the conformant 4-segment shape before any T3 write.
- Pre-existing legacy 2-segment collections remain readable; only NEW non-conformant creations are blocked at `T3Database.get_or_create_collection`.
- Invalid: `knowledge::myproject` (colons are not permitted in ChromaDB collection names).

**Duplicate document ID**:
- Error: `Document ID already exists`
- Fix: Append timestamp suffix: `insight-developer-topic-20260311`
