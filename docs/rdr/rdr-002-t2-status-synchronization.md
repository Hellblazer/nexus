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

The root cause: the pilot RDRs were accepted and implemented but never
`/rdr-close`d — they remained open. Status transitions happened by editing
YAML frontmatter (draft → accepted, accepted → implemented) with no
corresponding T2 update. Only `/rdr-create` and `/rdr-close` write T2;
every intermediate status change is invisible.

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
- **Audit trail has holes** — no record of when acceptance happened
  (sync-on-read cannot recover this; only an explicit accept command
  can record the actual acceptance timestamp)
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

Reviewed all RDR skill commands to identify which ones read/write T2
(pre-implementation state — Phase 2 will add T2 writes to read commands):

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
  frontmatter (new standard per RDR-001 P4) and legacy markdown-list
  metadata (`## Metadata` with `- **Key**: Value` items) — **Status**:
  Verified — **Method**: Source Search (reviewed all command implementations)
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

#### Gate result storage

Gate outcomes must be stored to enable `/rdr-accept` verification. The
gate command (`/rdr-gate`) will write a T2 record after each gate run:

```bash
nx memory put - --project {repo}_rdr --title {id}-gate-latest <<'EOF'
outcome: "PASSED"  # or "BLOCKED"
date: "YYYY-MM-DD"
critical_count: 0
significant_count: 2
EOF
```

This is a new T2 write in `/rdr-gate` (currently it writes nothing).
The record is overwritten on each gate/re-gate run.

#### `/rdr-accept` command

```
/rdr-accept <id> [--reviewed-by <name>]
```

- Reads the RDR file
- Reads T2 gate record (`{id}-gate-latest`). If no gate record exists
  or outcome is BLOCKED, **refuse to accept** — no `--force` override.
  The gate must pass before acceptance. This preserves the invariant
  that RDR-001 P1 relies on: accepted status means gated.
- Updates frontmatter: `status: accepted`, `reviewed-by: <name|self>`,
  `accepted_date: YYYY-MM-DD`
- Writes T2: status, accepted_date, reviewed-by
- Prints confirmation
- **Idempotency**: If already accepted, print current acceptance info
  and exit. To change `reviewed-by`, edit frontmatter directly — the
  accept ceremony is one-time.

If frontmatter write succeeds but T2 write fails, print a warning:
`T2 update failed — acceptance recorded in frontmatter only. Next
/rdr-show or /rdr-list will sync T2.` This ensures the user knows
the state is divergent, and sync-on-read will eventually fix it.

#### Sync-on-read (defensive)

Add to `parse_frontmatter()` callers in `/rdr-gate`, `/rdr-show`,
`/rdr-list`. When file status diverges from T2, update T2 to match
file and **print a visible notice**:

```python
# After reading file and T2:
if file_status != t2_status:
    # Update T2 to match file (file is source of truth)
    subprocess.run(['nx', 'memory', 'put', updated_record,
        '--project', f'{repo}_rdr', '--title', t2_key])
    print(f"> T2 synced: {t2_status} → {file_status}")
```

For `/rdr-list` with multiple stale records, print a summary line:
`> Synced N stale T2 records`

**Limitation**: Sync-on-read corrects status but cannot recover audit
timestamps (accepted_date, etc.). It stamps T2 with the current time.
This is an acknowledged gap — the accept command is the authoritative
source for acceptance timestamps. Sync-on-read is a status cache
correction, not an audit trail recovery mechanism.

### Decision Rationale

The accept command addresses the primary gap: gate enforcement at the
acceptance moment, automatic T2 sync with correct timing, and ceremonial
visibility of the acceptance event. Sync-on-read is defensive — it
catches status divergence from manual edits or future transitions we
haven't anticipated, but does not fabricate audit timestamps. File
frontmatter is always the source of truth; T2 is a queryable cache.

## Alternatives Considered

### Alternative 1: Sync-on-read only (no accept command)

**Pros**: No new command to learn

**Cons**: Acceptance still has no ceremony, no reviewed-by record,
no audit trail of when acceptance happened

**Reason for rejection**: Misses gate enforcement at acceptance time
(sync-on-read can't verify gate state retroactively), doesn't record
accurate acceptance timestamps, and provides no ceremonial visibility
of the acceptance decision

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

- **Sync-on-read T2 unavailable**: Read commands still work from file
  data. T2 stays stale until next successful sync. Correction is
  visible when it happens ("T2 synced: ...").
- **`/rdr-accept` skipped**: Sync-on-read catches status divergence on
  next gate/show/list. Status is corrected but accepted_date is stamped
  with current time (not actual acceptance time) — this is an acknowledged
  limitation, not a silent corruption.
- **`/rdr-accept` partial write**: Frontmatter succeeds but T2 fails.
  User sees explicit warning. Sync-on-read fixes T2 on next read command.
- **`/rdr-accept` on already-accepted RDR**: No-op, prints current
  acceptance info.

## Implementation Plan

### Phase 0: Verify Root Cause

0. Confirm that `/rdr-close` correctly updates T2 by running it on a
   test RDR. Rule out a bug in close itself — the pilot RDRs were never
   closed, so this should work, but verify before building on the
   assumption.

### Phase 1: Gate Result Storage + Accept Command

1. Update `/rdr-gate` to write T2 gate result record (`{id}-gate-latest`)
   after each gate run with outcome, date, and finding counts
2. Update `/rdr-gate` to print accept prompt when gate returns PASSED:
   "Run `/rdr-accept <id>` to accept this RDR."
3. Create `/rdr-accept` command (`nx/commands/rdr-accept.md`) — reads
   gate result from T2, blocks if no PASSED gate, updates frontmatter
   (`status`, `reviewed-by`, `accepted_date`) and T2, handles partial
   write failure with explicit warning
4. Register command in skill definitions

### Phase 2: Sync-on-Read

5. Add T2 freshness check to `/rdr-show` — visible notice on correction
6. Add T2 freshness check to `/rdr-list` — summary line for batch corrections
7. Add T2 freshness check to `/rdr-gate` — visible notice on correction

### Phase 3: Documentation

8. Add `/rdr-accept` to `docs/rdr/workflow.md` as an explicit lifecycle step
9. Document `accepted_date` frontmatter field in `docs/rdr/templates.md`

## Test Plan

- Phase 0: Run `/rdr-close` on a test RDR — verify T2 updates correctly
- Run `/rdr-accept` on a draft RDR without gate — verify it blocks (no `--force` available)
- Run `/rdr-gate`, verify T2 gate result record written (`{id}-gate-latest`)
- Run `/rdr-accept` on a gated RDR — verify frontmatter and T2 both update, `accepted_date` set
- Run `/rdr-accept` on already-accepted RDR — verify no-op, prints current info
- Run `/rdr-accept` with `--reviewed-by alice` — verify field is set in frontmatter and T2
- Manually edit frontmatter status, then run `/rdr-show` — verify visible "T2 synced" notice
- Run `/rdr-list` with multiple stale records — verify "Synced N stale T2 records" summary

## Validation

### Testing Strategy

1. **Scenario**: Accept a gated RDR
   **Expected**: Frontmatter status = accepted, T2 status = accepted,
   reviewed-by field populated, accepted_date set to today

2. **Scenario**: Query T2 after manual frontmatter edit
   **Expected**: Next read command corrects T2 with visible notice

3. **Scenario**: Accept without gate
   **Expected**: Command refuses — no override available

## References

- nexus RDR-001: RDR Process Validation (P4 reviewed-by, P5 status model)
- BFDB pilot session: 3 RDRs with stale T2 records

## Revision History

### Gate Review (2026-02-27)

### Critical — Resolved

**C1. `--force` undermines RDR-001 P1 gate enforcement — RESOLVED.** If
`/rdr-accept --force` bypasses gate verification, then accepted status no
longer implies gated, and RDR-001 P1's hard-block on close is invalidated.
Fixed: removed `--force` from `/rdr-accept` entirely. Gate must pass before
acceptance — no override.

**C2. Gate verification mechanism unspecified — RESOLVED.** The design said
"verifies gate has passed" but never defined where gate results are stored
or how they are checked. Fixed: `/rdr-gate` now writes a T2 record
(`{id}-gate-latest`) with outcome, date, and finding counts. `/rdr-accept`
reads this record to verify.

**C3. Sync-on-read fabricates audit timestamps — RESOLVED.** Sync-on-read
stamps T2 with current time, not actual acceptance time, creating a false
audit trail. Fixed: acknowledged as a limitation — sync-on-read is a status
cache correction, not an audit trail recovery. Added `accepted_date` to
frontmatter so the accept command records the real timestamp. Removed
audit trail recovery from sync-on-read's claimed benefits.

### Significant — Resolved

**S1. Partial write failure during `/rdr-accept` unhandled — RESOLVED.**
Fixed: if frontmatter succeeds but T2 fails, print explicit warning.
Sync-on-read will eventually correct T2 on next read command.

**S2. Idempotency undefined — RESOLVED.** Fixed: `/rdr-accept` on an
already-accepted RDR is a no-op that prints current acceptance info.
Added test case.

**S3. Silent T2 mutation in `/rdr-list` — RESOLVED.** Fixed: all
sync-on-read corrections print visible notices. `/rdr-list` prints
"Synced N stale T2 records" summary line for batch corrections.

**S4. Root cause assumed, not verified — RESOLVED.** Fixed: clarified
that the pilot RDRs were never `/rdr-close`d (they remained open after
acceptance). Added Phase 0 to verify `/rdr-close` works correctly
before building on that assumption.

### Observations — Applied

- O1: Investigation table labeled "pre-implementation state"
- O2: "Both formats" clarified as YAML frontmatter (new) and markdown-list metadata (legacy)
- O3: Phase 3 step 8 corrected from "update status model" to "document accepted_date field"
- O4: Alternative 1 rejection rationale restated — gate enforcement, accurate timestamps, and ceremonial visibility, not just reviewed-by tracking
