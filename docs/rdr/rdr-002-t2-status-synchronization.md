---
title: "T2 Status Synchronization"
id: RDR-002
type: Technical Debt
status: draft
priority: high
author: Hal Hildebrand
reviewed-by:
created: 2026-02-27
related_issues:
  - RDR-001
---

# RDR-002: T2 Status Synchronization

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

T2 metadata records for RDRs go stale when status changes happen outside of
`/rdr-close`. In a pilot session with 3 RDRs, all three were accepted and
implemented but their T2 records still showed "DRAFT" — discovered only when
querying T2 after the session.

The root cause: status transitions that happen by editing YAML frontmatter
(draft → accepted, accepted → implemented) have no corresponding T2 update.
Only `/rdr-close` writes back to T2. Every other status change is invisible.

## Context

### Background

Discovered during the BFDB pilot session that validated the RDR process
(nexus RDR-001). After accepting and implementing all three pilot RDRs, a
T2 query revealed all records still showed their initial draft status.

### Impact

- **Agents querying T2 get stale data** — any agent using `nx memory get`
  to check RDR status will act on wrong information
- **`/rdr-list` shows wrong statuses** — it reads T2 for fast response
  without parsing markdown files, so it reports stale statuses
- **Audit trail has holes** — no record of when acceptance or
  implementation happened
- **Cross-RDR dependency checking** is unreliable if status is wrong

### Technical Environment

- RDR tooling: nexus plugin skill commands (`/rdr-create`, `/rdr-gate`,
  `/rdr-close`, `/rdr-research`, `/rdr-list`, `/rdr-show`)
- T2 storage: `nx memory put/get` with project `{repo}_rdr`
- Status source of truth: YAML frontmatter in the RDR markdown file
- Current T2 update points: `/rdr-create` (initial write), `/rdr-close`
  (final write)

## Research Findings

### Investigation

Reviewed all RDR skill commands to identify which ones read/write T2:

| Command | Reads T2? | Writes T2? | Status change? |
|---------|-----------|------------|----------------|
| `/rdr-create` | No | Yes (initial record) | Sets Draft |
| `/rdr-research` | Yes (findings) | Yes (findings) | No status change |
| `/rdr-gate` | Yes (metadata + findings) | No | No (gate outcomes are BLOCKED/PASSED, not status) |
| `/rdr-close` | Yes (metadata) | Yes (close record) | Sets terminal status |
| `/rdr-list` | Yes (all records) | No | No |
| `/rdr-show` | Yes (single record) | No | No |

Gap: no command writes T2 for the **draft → accepted** transition. The gate
reports BLOCKED/PASSED but does not change the RDR's status. Acceptance is a
manual frontmatter edit with no T2 update.

### Key Discoveries

- **Documented**: The `parse_frontmatter()` function in every command already
  reads the current status from the file. T2 and file status can diverge
  silently.
- **Documented**: `/rdr-list` reads T2 for speed but could fall back to
  filesystem parsing. Currently it only uses T2.
- **Assumed**: Most status transitions happen during interactive sessions
  where the user is present. Batch/CI status changes are not a current
  use case.

### Critical Assumptions

- [x] `parse_frontmatter()` reliably extracts status from both YAML
  frontmatter and markdown-list metadata — **Status**: Verified
  — **Method**: Source Search (reviewed all command implementations)
- [ ] A sync-on-read approach won't create performance issues for repos
  with many RDRs — **Status**: Unverified
  — **Method**: Docs Only (no repos with >20 RDRs exist yet)

## Proposed Solution

### Approach

**Sync-on-read with explicit accept command.** Two complementary fixes:

1. **`/rdr-accept` command**: New command that sets status to `accepted` in
   both frontmatter and T2. Records acceptance date and reviewer.

2. **T2 freshness check in `/rdr-gate` and `/rdr-show`**: When these
   commands read T2 metadata, compare `status` against the file's
   frontmatter. If they diverge, update T2 silently and note the
   correction in output.

### Technical Design

#### `/rdr-accept` command

```
/rdr-accept <id> [--reviewed-by <name>]
```

- Reads the RDR file, verifies a gate has passed (gate findings exist
  with no unresolved critical issues, or `--force`)
- Updates frontmatter: `status: accepted`, `reviewed-by: <name|self>`
- Writes T2: status, accepted date, reviewed-by
- Prints confirmation

#### Sync-on-read (defensive)

Add to `parse_frontmatter()` callers in `/rdr-gate`, `/rdr-show`,
`/rdr-list`:

```python
# After reading file and T2:
if file_status != t2_status:
    # Update T2 to match file (file is source of truth)
    subprocess.run(['nx', 'memory', 'put', updated_record,
        '--project', f'{repo}_rdr', '--title', t2_key])
    print(f"> T2 status corrected: {t2_status} → {file_status}")
```

### Decision Rationale

The accept command addresses the primary gap (no ceremony for acceptance).
Sync-on-read is defensive — it catches any remaining divergence from manual
edits or future status transitions we haven't anticipated. File frontmatter
is always the source of truth; T2 is a queryable cache.

## Alternatives Considered

### Alternative 1: Sync-on-read only (no accept command)

**Pros**: No new command to learn

**Cons**: Acceptance still has no ceremony, no reviewed-by record,
no audit trail of when acceptance happened

**Reason for rejection**: Misses the reviewed-by tracking that RDR-001
P4 requires

### Alternative 2: File watcher / git hook

**Pros**: Fully automatic, catches every change

**Cons**: Fragile (file watchers fail silently), git hooks are
per-clone (not portable), adds infrastructure complexity

**Reason for rejection**: Over-engineered for the current scale

### Briefly Rejected

- **Periodic batch sync**: Adds a cron-like dependency for a problem
  that only matters during interactive sessions

## Trade-offs

### Consequences

- Positive: T2 always matches file status, agents get accurate data
- Positive: Acceptance has an explicit ceremony with audit trail
- Negative: One more command to remember (mitigated by gate output
  prompting the user)

### Risks and Mitigations

- **Risk**: User forgets `/rdr-accept` just like they forgot
  `/rdr-research`
  **Mitigation**: When gate returns PASSED, print: "Run `/rdr-accept <id>`
  to accept this RDR."

### Failure Modes

- If sync-on-read fails (T2 unavailable), commands still work — they
  just use file data directly. T2 stays stale until next successful sync.
- If `/rdr-accept` is skipped, sync-on-read catches the divergence on
  next gate/show/list.

## Implementation Plan

### Phase 1: Accept Command

1. Create `/rdr-accept` command (`nx/commands/rdr-accept.md`)
2. Update `/rdr-gate` to print accept prompt when gate returns PASSED
3. Register command in skill definitions

### Phase 2: Sync-on-Read

4. Add T2 freshness check to `/rdr-show`
5. Add T2 freshness check to `/rdr-list`
6. Add T2 freshness check to `/rdr-gate`

### Phase 3: Documentation

7. Add `/rdr-accept` to `docs/rdr/workflow.md`
8. Update status model to show accept as an explicit step

## Test Plan

- Run `/rdr-accept` on a draft RDR without gate — verify it blocks
- Run `/rdr-accept` on a gated RDR — verify frontmatter and T2 both update
- Manually edit frontmatter status, then run `/rdr-show` — verify T2 corrects
- Run `/rdr-list` after manual edit — verify corrected status appears
- Run `/rdr-accept` with `--reviewed-by alice` — verify field is set

## Validation

### Testing Strategy

1. **Scenario**: Accept a gated RDR
   **Expected**: Frontmatter status = accepted, T2 status = accepted,
   reviewed-by field populated

2. **Scenario**: Query T2 after manual frontmatter edit
   **Expected**: Next read command corrects T2 silently

## References

- nexus RDR-001: RDR Process Validation (P4 reviewed-by, P5 status model)
- BFDB pilot session: 3 RDRs with stale T2 records

## Revision History

[Gate findings will be appended here.]
