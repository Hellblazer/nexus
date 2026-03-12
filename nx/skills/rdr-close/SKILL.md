---
name: rdr-close
description: Use when an RDR is done — close it with optional post-mortem, bead status advisory, and T3 archival
---

# RDR Close Skill

Reports bead status as advisory. Optionally delegates post-mortem archival to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "close this RDR", "RDR done", "finish RDR"
- User invokes `/rdr-close`
- Implementation is complete and the RDR should be finalized

## Inputs

- **RDR ID** (required) — e.g., `003`
- **Reason** (required): Implemented | Reverted | Abandoned | Superseded

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`. All file paths below use `$RDR_DIR` in place of `docs/rdr`.

## Pre-Check

1. Read T2 record: Use memory_get tool: project="{repo}_rdr", title="NNN"
2. If status is not "accepted" (or "final") and reason is "Implemented":
   - Warn: "RDR NNN status is '{current_status}' — expected 'accepted'. Close anyway?"
   - Require `--force` or explicit user confirmation to proceed
3. If T2 record not found, check filesystem for the markdown file

## Flow: Implemented

### Step 1: Divergence Notes

Ask: "Did implementation diverge from the plan? If so, describe the divergences."

If diverged:

### Step 2: Create Post-Mortem

Create `$RDR_DIR/post-mortem/NNN-kebab-title.md` from the post-mortem template. Populate:

- **RDR Summary**: Extract from the RDR's Problem Statement
- **Implementation Status**: "Implemented"
- **What Diverged**: User's divergence notes
- **Drift Classification**: Prompt user to classify each divergence into categories:
  - Unvalidated assumption
  - Framework API detail
  - Missing failure mode
  - Missing Day 2 operation
  - Deferred critical constraint
  - Over-specified code
  - Under-specified architecture
  - Scope underestimation
  - Internal contradiction
  - Missing cross-cutting concern

### Step 3: Bead Status Advisory

If T2 record has an `epic_bead` field (set during accept-time planning):
1. Read epic bead ID from T2: Use memory_get tool: project="{repo}_rdr", title="NNN"
2. Run `bd show <epic-id>` to get child bead statuses
3. Display bead status table to user:
   - Bead ID, title, status (open/in_progress/closed)
   - Highlight any unclosed beads
4. **Advisory only** — the human decides which beads to close. Do NOT automatically mark beads complete.

If T2 record has no `epic_bead` field (user skipped planning at accept time):
- Skip this step entirely. Print: "No planning beads found for this RDR."

### Step 4: Update State

1. Update T2 record: Use memory_put tool: content="... (same fields, status: Implemented, closed: YYYY-MM-DD, close_reason: Implemented, archived: true)", project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,{type},closed"
   If T3 archive fails, set `archived: false` — retryable by re-running `/rdr-close`

2. Update status in RDR markdown metadata
3. Regenerate `docs/rdr/README.md` index
4. Run `nx index rdr` to update T3 semantic index

### Step 5: T3 Archive (post-mortem only)

The main RDR is already semantically indexed by Step 4's `nx index rdr` (CCE embeddings, section-level chunks). Do **not** duplicate it with store_put tool — that would create voyage-4 blob entries in the same collection, degrading search quality.

If a post-mortem exists, archive it to a separate collection (using the exact file path from Step 2, not a glob): Use store_put tool: content=(contents of $RDR_DIR/post-mortem/NNN-kebab-title.md), collection="knowledge__rdr_postmortem__{repo}", title="PREFIX-NNN Title (post-mortem)", tags="rdr,post-mortem,{drift-categories}"

Dispatch `knowledge-tidier` agent for post-mortem archival if the post-mortem contains substantial divergence analysis that benefits from knowledge organization.

## Flow: Reverted or Abandoned

1. Prompt for reason (free text)
2. Offer post-mortem (useful for capturing what was learned, even from abandoned work)
3. Update T2 record with close reason
4. Update markdown metadata
5. Run `nx index rdr` to update T3 semantic index (research findings are valuable even for failed RDRs)
6. Archive post-mortem to `knowledge__rdr_postmortem__{repo}` (if created)
7. Regenerate index

## Flow: Superseded

1. Prompt for superseding RDR ID
2. Cross-link both RDRs (bidirectional):
   - **Old RDR**: In T2, set `superseded_by: "NNN"`. In markdown, add "Superseded by RDR-NNN" note
   - **New RDR**: In T2, set `supersedes: "MMM"`. In markdown, add "Supersedes RDR-MMM" note
3. Run `nx index rdr` to update T3 semantic index
4. Regenerate index

## Failure Handling

The close operation performs multiple state mutations. If any step fails:
- Each step emits clear status (e.g., "T2 updated ✓", "Bead advisory ✓", "T3 archive ✗ FAILED")
- T2 `archived` flag tracks whether T3 archival succeeded
- Re-running `/rdr-close` is idempotent: checks T2 state and skips completed steps

## Relay Template (Use This Format)

When dispatching the knowledge-tidier agent via Task tool for post-mortem archival, use this exact structure:

```markdown
## Relay: knowledge-tidier

**Task**: Archive RDR NNN post-mortem to T3 with drift classification metadata.
**Bead**: none

### Input Artifacts
- nx store: [prior archived RDRs or "none"]
- nx memory: {repo}_rdr/NNN (status, research records, close metadata)
- nx scratch: [scratch IDs or "none"]
- Files: docs/rdr/post-mortem/NNN-kebab-title.md

### Deliverable
Post-mortem archived to `knowledge__rdr_postmortem__{repo}` with drift categories as tags.

### Quality Criteria
- [ ] Post-mortem content fully archived to T3
- [ ] Tags include divergence/drift categories
- [ ] Title includes RDR prefix and ID
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Success Criteria

- [ ] RDR directory resolved from `.nexus.yml` `indexing.rdr_paths[0]` (default `docs/rdr`)
- [ ] Pre-check completed (status verified, warnings issued for non-Final RDRs)
- [ ] Divergence notes captured from user (if implementation diverged)
- [ ] Post-mortem created with drift classification (if diverged or reverted/abandoned)
- [ ] Bead status advisory displayed (if epic_bead exists in T2)
- [ ] Beads NOT auto-closed — human decides
- [ ] T2 record updated with close reason, date, epic bead ID, and archived flag
- [ ] T3 semantic index updated via `nx index rdr`
- [ ] Post-mortem archived to `knowledge__rdr_postmortem__{repo}` (if exists)
- [ ] README index regenerated
- [ ] Idempotent: re-running skips completed steps

## Agent-Specific PRODUCE

Outputs produced by this skill directly:

- **Console output**: Bead status advisory table (if epic_bead in T2)
- **T2 memory**: Close metadata via memory_put tool: project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,{type},closed"
- **T3 semantic index**: Updated via `nx index rdr` (CCE embeddings, section-level chunks)
- **Filesystem**: Post-mortem at `$RDR_DIR/post-mortem/NNN-kebab-title.md`, updated README

Outputs generated by the knowledge-tidier agent (post-mortem archival only):

- **T3 knowledge**: Post-mortem archive via store_put tool: content=(post-mortem contents), collection="knowledge__rdr_postmortem__{repo}", title="PREFIX-NNN Title (post-mortem)"
- **T1 scratch**: Working notes via scratch tool: action="put", content="RDR NNN close: archiving post-mortem", tags="rdr,close"

## Does NOT

- Force close if gate hasn't passed (warns, allows override)
- Delete the markdown file (it stays in the repo permanently)
- Auto-commit (user decides when to commit)
