---
title: "T2 Status Synchronization"
id: RDR-002
type: Technical Debt
status: accepted
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
- **Cross-RDR dependency checking** is unreliable if status is wrong

### Architectural Principle

**T2 is the process authority; files are the human-editable persistence
layer.** Agent interactions with RDR process state (status, gate results,
acceptance, reviewed-by) should flow through `nx` and the nx plugin. Files
are the git-versioned merge state — editable by humans, diffable, reviewable
— but Claude's primary interface for process metadata is T2.

This means:
- **Agent reads**: T2 first. File content for the design body.
- **Agent writes**: T2 first, then propagate to file frontmatter.
- **Human edits**: File frontmatter directly. Reconciled into T2 on
  next session start.
- **Source of truth**: T2 for process state during a session. Files for
  content and for persistence across sessions (git-versioned). When they
  diverge, reconciliation always advances to the more advanced status
  (monotonic-advance rule). Deliberate status regression goes through
  `/rdr-close --force`, not direct frontmatter edits.

### Technical Environment

- RDR tooling: nexus plugin skill commands (`/rdr-create`, `/rdr-gate`,
  `/rdr-close`, `/rdr-research`, `/rdr-list`, `/rdr-show`)
- T2 storage: `nx memory put/get` with project `{repo}_rdr`
- Current T2 update points: `/rdr-create` (initial write), `/rdr-close`
  (final write)
- SessionStart hook: `nx/hooks/scripts/rdr_hook.py` — runs at session
  start, currently reports RDR count and indexing status

## Research Findings

### Investigation

Reviewed all RDR skill commands to identify which ones read/write T2
(pre-implementation state):

| Command | Reads T2? | Writes T2? | Status change? |
|---------|-----------|------------|----------------|
| `/rdr-create` | No | Yes (initial record) | Sets Draft |
| `/rdr-research` | Yes (findings) | Yes (findings) | No status change |
| `/rdr-gate` | Yes (metadata + findings) | No | No (gate outcomes are BLOCKED/PASSED, not status) |
| `/rdr-close` | Yes (metadata) | Yes (close record) | Sets terminal status |
| `/rdr-list` | Yes (all records) | No | No |
| `/rdr-show` | Yes (single record) | No | No |

**Gap**: No command writes T2 for the draft → accepted transition. The gate
reports BLOCKED/PASSED but does not store its outcome. Acceptance is a manual
frontmatter edit with no T2 update.

**Note on `/rdr-list`**: The workflow doc says it "reads from T2 metadata for
fast response" but the actual implementation (`rdr-list.md`) parses markdown
files via `parse_frontmatter()` and `get_all_rdrs()` for the status table,
calling `nx memory list` only to display raw T2 records as a secondary
section. This divergence is addressed in the proposed solution.

### Key Discoveries

- **Documented**: `parse_frontmatter()` in every command reads current status
  from the file. T2 and file status can diverge silently.
- **Documented**: `/rdr-list` currently reads files, not T2, for its primary
  status display (contradicts workflow doc claim).
- **Assumed**: Most status transitions happen during interactive sessions
  where the user is present. Batch/CI status changes are not a current
  use case.

### Critical Assumptions

- [x] `parse_frontmatter()` reliably extracts status from both YAML
  frontmatter (new standard per RDR-001 P4) and legacy markdown-list
  metadata (`## Metadata` with `- **Key**: Value` items) — **Status**:
  Verified — **Method**: Source Search (reviewed all command implementations)
- [ ] SessionStart hook reconciliation won't create noticeable latency
  for repos with many RDRs — **Status**: Unverified
  — **Method**: Docs Only (no repos with >20 RDRs exist yet)

## Proposed Solution

### Approach

**T2-primary with SessionStart reconciliation.** Three complementary pieces:

1. **`/rdr-accept` command**: Writes T2 first (status, accepted_date,
   reviewed-by, gate verification), then propagates to file frontmatter.

2. **Gate result storage**: `/rdr-gate` writes outcome to T2 after each
   run. Enables `/rdr-accept` to verify gate passed.

3. **SessionStart reconciliation**: Extend the existing `rdr_hook.py` to
   compare file frontmatter against T2 on session start. Catches human
   edits made between sessions (direct frontmatter edits, `git pull`).

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
The record is overwritten on each gate/re-gate run (last-write-wins,
no history — the current gate state is what matters for acceptance).

The gate outcome is written by Claude as part of the gate action
instructions (the `## Action` section of `rdr-gate.md`), not by the
pre-loaded Python script. Implementation is a prompt/instruction update.

#### `/rdr-accept` command

```
/rdr-accept <id> [--reviewed-by <name>]
```

- Reads T2 gate record (`{id}-gate-latest`). If no gate record exists
  or outcome is BLOCKED, **refuse to accept** — no `--force` override.
  The gate must pass before acceptance. This preserves the invariant
  that RDR-001 P1 relies on: accepted status means gated.
- Writes T2 first: status=accepted, accepted_date, reviewed-by
- Then updates file frontmatter: `status: accepted`,
  `reviewed-by: <name|self>`, `accepted_date: YYYY-MM-DD`
- Prints confirmation
- **Idempotency**: If T2 already shows accepted and file also shows
  accepted, print current acceptance info and exit. If T2 shows accepted
  but file does not (prior partial write failure), repair the file
  frontmatter and report what was fixed. The accept ceremony is one-time
  but self-healing.

**Partial write failure**: If T2 write succeeds but frontmatter update
fails, print a warning. The SessionStart reconciliation will propagate
T2 state to the file on next session. If T2 write fails, do not proceed
to frontmatter — report the error. T2 is the process authority.

#### SessionStart reconciliation

Extend `nx/hooks/scripts/rdr_hook.py` to reconcile file ↔ T2 on every
session start:

```python
# For each RDR file in docs/rdr/:
#   1. Parse frontmatter status from file
#   2. Read T2 status for this RDR
#   3. If they differ:
#      - If file has a more advanced status (e.g., file=accepted, T2=draft),
#        update T2 to match file (human edited between sessions)
#      - If T2 has a more advanced status (e.g., T2=accepted, file=draft),
#        update file frontmatter to match T2 (rare — T2 write succeeded
#        but file write failed in a previous session)
#   4. Print summary: "RDR sync: N records reconciled"
```

Status ordering for "more advanced" comparison:
draft < accepted < implemented < {reverted, abandoned, superseded}

Terminal states (reverted, abandoned, superseded) are all equivalent in
advancement. If both sides carry different terminal states (e.g.,
T2=abandoned, file=superseded), favor the file and emit a warning:
`"RDR NNN: terminal state conflict (T2=abandoned, file=superseded) —
using file."` This preserves human intent while making the override
visible.

**Output**: The hook prints a summary line only when reconciliation
happens. Silent when everything is in sync.

#### `/rdr-close --force` and the gate invariant

RDR-001 P1 added `--force` to `/rdr-close` to allow closing without
accepted/final status. This creates a path that bypasses both
`/rdr-accept` and the gate: a user can go directly to
`/rdr-close --force` on a draft RDR.

This is an intentional escape hatch, not a bug. The `--force` flag
creates an explicit paper trail (the override is visible in the close
output). The invariant "accepted means gated" holds for the normal path;
`--force` is the documented override for exceptional cases (e.g.,
abandoning an RDR that was never gated). Removing `--force` from close
would prevent abandoning or superseding ungated RDRs, which are
legitimate operations.

### Decision Rationale

T2-primary aligns with the nx architecture: agents interact through
`nx`, humans interact through files. The accept command provides gate
enforcement, accurate timestamps, and ceremonial visibility. SessionStart
reconciliation catches human edits without file watchers or git hooks —
it runs in the existing hook infrastructure, is portable across clones,
and fails gracefully (if the hook fails, commands still work from
whichever source they can reach).

## Alternatives Considered

### Alternative 1: File-primary with T2 as cache

**Pros**: Simple mental model — file is always truth

**Cons**: Every command must read files, T2 is perpetually stale
between writes, agents can't trust T2 without cross-checking files

**Reason for rejection**: This was the original RDR-002 design. The
re-gate revealed it creates complexity: `/rdr-list` already reads files
(contradicting the workflow doc), sync-on-read in read commands violates
expectations, and timestamp fabrication corrupts audit trails. T2-primary
with reconciliation is cleaner.

### Alternative 2: Sync-on-read only (no accept command)

**Pros**: No new command to learn

**Cons**: No gate enforcement at acceptance time, no accurate acceptance
timestamps, no ceremonial visibility of the acceptance decision

**Reason for rejection**: Missed the core problem — acceptance needs
ceremony and gate verification, not just status propagation

### Alternative 3: File watcher / git hook

**Pros**: Fully automatic, catches every change in real time

**Cons**: File watchers fail silently, git hooks are per-clone (not
portable), adds infrastructure complexity

**Reason for rejection**: Over-engineered. SessionStart reconciliation
achieves the same goal using existing hook infrastructure with no new
daemons or per-clone setup.

### Briefly Rejected

- **Periodic batch sync**: Adds a cron-like dependency for a problem
  that only matters during interactive sessions

## Trade-offs

### Consequences

- Positive: T2 is authoritative for agents — reliable, fast reads
- Positive: Acceptance has an explicit ceremony with gate enforcement
- Positive: Human edits caught on session start — no manual sync needed
- Negative: One more command to remember (mitigated by gate output
  prompting the user)
- Negative: Brief window where file and T2 can diverge (between human
  edit and next session start) — acceptable for the current scale

### Risks and Mitigations

- **Risk**: User forgets `/rdr-accept` just like they forgot
  `/rdr-research`
  **Mitigation**: When gate returns PASSED, print: "Run `/rdr-accept <id>`
  to accept this RDR."
- **Risk**: SessionStart reconciliation has wrong "more advanced" logic
  **Mitigation**: Status ordering is explicit and simple. Edge cases
  (conflicting terminal states) favor the file as the human-editable layer.

### Failure Modes

- **T2 unavailable at session start**: Reconciliation skipped, hook
  prints warning. Commands fall back to file reads. T2 stays stale until
  next successful session start.
- **`/rdr-accept` T2 write fails**: Command reports error, does not
  update file. User retries or proceeds manually.
- **`/rdr-accept` skipped**: SessionStart reconciliation catches the
  divergence on next session. Status is corrected but accepted_date is
  set to reconciliation time, not actual acceptance time — acknowledged
  limitation.
- **`/rdr-accept` on already-accepted RDR**: No-op, prints current
  acceptance info.
- **Conflicting edits**: User edits file to "accepted" between sessions,
  but T2 still shows "draft" (no gate). Reconciliation advances T2 to
  match file. This bypasses gate enforcement — an accepted limitation
  of the file-editable model. Unlike `/rdr-close --force`, this leaves
  no explicit paper trail in command output.
- **Human edits file mid-session**: T2 reflects pre-edit state until
  next SessionStart. Agent commands reading T2 will see stale status
  for the remainder of the session.

## Implementation Plan

### Phase 0: Verify Root Cause

0. Confirm that `/rdr-close` correctly updates T2 by running it on a
   test RDR. Rule out a bug in close itself.

### Phase 1: Gate Result Storage + Accept Command

1. Update `/rdr-gate` action instructions to write T2 gate result record
   (`{id}-gate-latest`) after each gate run — this is a prompt change in
   the `## Action` section, not a Python code change
2. Update `/rdr-gate` action instructions to print accept prompt when
   gate returns PASSED: "Run `/rdr-accept <id>` to accept this RDR."
3. Create `/rdr-accept` command file (`nx/commands/rdr-accept.md`) and
   corresponding skill definition (`nx/skills/rdr-accept/SKILL.md`) —
   reads gate result from T2, blocks if no PASSED gate, writes T2 first
   (`status`, `reviewed-by`, `accepted_date`), then updates file
   frontmatter, self-heals on re-run if file update failed previously

### Phase 2: SessionStart Reconciliation

5. Extend `nx/hooks/scripts/rdr_hook.py` to reconcile file ↔ T2 on
   session start — compare statuses, update the less-advanced side,
   print summary only when changes occur
6. Update `/rdr-list` to read process metadata from T2 (not files) for
   its primary status display, matching the workflow doc's stated behavior

### Phase 3: Documentation

7. Add `/rdr-accept` to `docs/rdr/workflow.md` as an explicit lifecycle
   step between Gate and Close
8. Document `accepted_date` frontmatter field in `docs/rdr/templates.md`
9. Update workflow doc's `/rdr-list` description to accurately reflect
   its T2-primary behavior after Phase 2

## Test Plan

### Phase 0

- Run `/rdr-close` on a test RDR — verify T2 updates correctly

### Phase 1

- Run `/rdr-gate`, verify T2 gate result record written (`{id}-gate-latest`)
- Run `/rdr-accept` on a draft RDR without gate — verify it blocks
- Run `/rdr-accept` on a gated RDR — verify T2 updated first, then
  frontmatter updated, `accepted_date` set
- Run `/rdr-accept` on already-accepted RDR (T2=accepted, file=accepted)
  — verify no-op
- Run `/rdr-accept` on already-accepted RDR (T2=accepted, file=draft)
  — verify file repaired
- Run `/rdr-accept` with `--reviewed-by alice` — verify field set in
  both T2 and frontmatter

### Phase 2

- Edit file frontmatter between sessions, start new session — verify
  SessionStart reconciliation updates T2 and prints summary
- Edit T2 status (simulating failed frontmatter write), start new
  session — verify reconciliation updates file to match T2
- Set T2=abandoned and file=superseded, start session — verify file
  wins with warning
- Run `/rdr-list` — verify status column populated from T2

## Validation

### Testing Strategy

1. **Scenario**: Accept a gated RDR
   **Expected**: T2 status = accepted (written first), frontmatter
   status = accepted, reviewed-by populated, accepted_date set to today

2. **Scenario**: Human edits frontmatter between sessions
   **Expected**: SessionStart reconciliation updates T2, prints
   "RDR sync: 1 record reconciled"

3. **Scenario**: Accept without gate
   **Expected**: Command refuses — no override available

4. **Scenario**: `/rdr-list` after Phase 2
   **Expected**: Status column populated from T2, not file parsing

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
Fixed: if T2 write fails, do not proceed to frontmatter — report error.
T2 is the process authority.

**S2. Idempotency undefined — RESOLVED.** Fixed: `/rdr-accept` on an
already-accepted RDR is a no-op that prints current acceptance info.

**S3. Silent T2 mutation in `/rdr-list` — RESOLVED.** Fixed: replaced
sync-on-read with SessionStart reconciliation. `/rdr-list` will read T2
directly after Phase 2, no longer mutating T2 as a side effect.

**S4. Root cause assumed, not verified — RESOLVED.** Fixed: clarified
that the pilot RDRs were never `/rdr-close`d (they remained open after
acceptance). Added Phase 0 to verify `/rdr-close` works correctly.

### Observations — Applied

- O1: Investigation table labeled "pre-implementation state"
- O2: "Both formats" clarified as YAML frontmatter (new) and markdown-list metadata (legacy)
- O3: Phase 3 corrected — documents `accepted_date` field, not "update status model"
- O4: Alternative 1 rejection rationale restated accurately

### Re-gate (2026-02-27)

All prior findings (C1–C3, S1–S4, O1–O4) verified resolved.

**Architectural revision**: Flipped from "file-primary, T2 as cache" to
"T2-primary, file as human-editable persistence." Agent reads/writes flow
through T2; human edits reconciled on SessionStart. This resolved S-NEW-1
(no more sync-on-read in read commands) and simplified the failure model.

### Significant — Resolved

**S-NEW-1. `/rdr-list` sync-on-read requires behavioral change not
reflected in design — RESOLVED.** The original sync-on-read design required
`/rdr-list` to start reading T2 per-record, which contradicted its actual
implementation (it reads files). Fixed: replaced sync-on-read with
SessionStart reconciliation. `/rdr-list` will read T2 directly (Phase 2,
step 6), matching the workflow doc's stated behavior.

**S-NEW-2. `/rdr-close --force` bypasses gate invariant — RESOLVED.**
`/rdr-close --force` can close an ungated RDR, bypassing both `/rdr-accept`
and the gate. Fixed: documented as an intentional escape hatch with explicit
paper trail, not a bug. Removing it would prevent abandoning/superseding
ungated RDRs, which are legitimate operations.

### Observations — Applied

- O-NEW-1: Gate-latest record documented as intentionally last-write-wins
  with no history
- O-NEW-2: Gate result write and accept prompt clarified as prompt/instruction
  changes to `rdr-gate.md` `## Action` section, not Python code changes
- O-NEW-3: Reconciliation handles re-gate of accepted RDRs — status ordering
  makes this a no-op (accepted is already advanced past draft)

### Second re-gate (2026-02-27)

All prior findings verified resolved. Architectural revision (T2-primary)
reviewed for internal consistency.

### Significant — Resolved

**S-NEW-3. Reconciliation policy contradiction — RESOLVED.** Architectural
Principle said "favor more recent write" but Technical Design said "update
the less-advanced side." These are different policies that diverge on status
regression. Fixed: committed to monotonic-advance rule throughout. Deliberate
regression goes through `/rdr-close --force`.

**S-NEW-4. Terminal state conflict handling incomplete — RESOLVED.** Both
sides carrying different terminal states (e.g., T2=abandoned, file=superseded)
was unhandled. Fixed: favor file and emit warning for terminal-vs-terminal
conflicts.

**S-NEW-5. Idempotency skips file repair — RESOLVED.** If T2=accepted but
file=draft (prior partial write), re-running `/rdr-accept` exited with no
repair. Fixed: idempotency now checks both sides — repairs file if T2 is
ahead, true no-op only when both agree.

### Observations — Applied

- O-NEW-4: "Same class as --force" corrected — direct file edits leave no
  paper trail unlike --force. Documented as accepted limitation of
  file-editable model.
- O-NEW-5: Mid-session file edits documented — T2 reflects pre-edit state
  until next SessionStart.
- O-NEW-6: Implementation plan step 3 now explicitly names both command file
  and skill definition creation.
- O-NEW-7: Test plan organized by phase to prevent cross-phase test confusion.
