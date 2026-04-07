---
name: rdr-close
description: Use when an RDR is done — close it with optional post-mortem, bead status gate, and T3 archival
effort: medium
---

# RDR Close Skill

Gates on open bead status before closing. Optionally delegates post-mortem archival to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "close this RDR", "RDR done", "finish RDR"
- User invokes `/nx:rdr-close`
- Implementation is complete and the RDR should be finalized

## Inputs

- **RDR ID** (required) — e.g., `003`
- **Reason** (required): Implemented | Reverted | Abandoned | Superseded

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`. All file paths below use `$RDR_DIR` in place of `docs/rdr`.

## Pre-Check

1. Read T2 record: mcp__plugin_nx_nexus__memory_get(project="{repo}_rdr", title="NNN"
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

### Step 3: Bead Status Gate

If T2 record has an `epic_bead` field (set during accept-time planning):
1. Read epic bead ID from T2: mcp__plugin_nx_nexus__memory_get(project="{repo}_rdr", title="NNN"
2. Run `/beads:show <epic-id>` to get child bead statuses
3. Display bead status table to user:
   - Bead ID, title, status (open/in_progress/closed)
   - Highlight any unclosed beads
4. Do NOT automatically mark beads complete — the human decides which beads to close.

If T2 record has no `epic_bead` field (user skipped planning at accept time):
- Check the command output for open beads listed by the pre-check script.

**HARD GATE — if ANY open or in-progress beads exist:**
- Display the open beads to the user
- Ask explicitly: "These beads are still open. Close this RDR anyway?"
- **Do NOT proceed until the user confirms.** This is not advisory — it is a gate.
- If the user says no, stop and let them resolve the beads first.

### Step 4: Update State

1. Update T2 record: mcp__plugin_nx_nexus__memory_put(content="... (same fields, status: Implemented, closed: YYYY-MM-DD, close_reason: Implemented, archived: true)", project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,{type},closed"
   If T3 archive fails, set `archived: false` — retryable by re-running `/nx:rdr-close`

2. Update status in RDR markdown metadata
3. Regenerate `docs/rdr/README.md` index
4. Run `nx index rdr` to update T3 semantic index

### Step 5: Catalog Links (if catalog initialized)

After `nx index rdr` in Step 4, the RDR has a catalog entry. Create links to capture implementation provenance:

1. **Code→RDR links**: The indexer hook auto-generates `implements-heuristic` links via title substring matching. These are created automatically — no action needed here.

2. **RDR→prior-RDR links**: If the RDR's T2 record has a `supersedes` field, create the catalog link:
   ```
   mcp__plugin_nx_nexus__catalog_link(from_tumbler="<this-rdr-title>", to_tumbler="<superseded-rdr-title>", link_type="supersedes", created_by="rdr-close")
   ```

3. **RDR→research links**: If research findings reference indexed papers, create `cites` links:
   - Read T2 research findings for this RDR
   - For each finding with a URL or paper title as source, search catalog: `mcp__plugin_nx_nexus__catalog_search(query="<source>")`
   - If found, create: `mcp__plugin_nx_nexus__catalog_link(from_tumbler="<rdr-title>", to_tumbler="<paper-tumbler>", link_type="cites", created_by="rdr-close")`

Skip all catalog steps silently if catalog is not initialized. The T2 record and markdown are the authorities — catalog links are supplementary graph enrichment.

### Step 6: T3 Archive (post-mortem only)

The main RDR is already semantically indexed by Step 4's `nx index rdr` (CCE embeddings, section-level chunks). Do **not** duplicate it with store_put tool — that would create non-CCE blob entries in the same collection, degrading search quality.

If a post-mortem exists, archive it to a separate collection (using the exact file path from Step 2, not a glob): mcp__plugin_nx_nexus__store_put(content=(contents of $RDR_DIR/post-mortem/NNN-kebab-title.md), collection="knowledge__rdr_postmortem__{repo}", title="PREFIX-NNN Title (post-mortem)", tags="rdr,post-mortem,{drift-categories}"

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
4. **Catalog link** (if catalog initialized): Create `supersedes` link in the catalog so the graph reflects the relationship:
   ```
   # Find both RDRs by title in catalog
   mcp__plugin_nx_nexus__catalog_link(from_tumbler="<new-rdr-title>", to_tumbler="<old-rdr-title>", link_type="supersedes", created_by="rdr-close")
   ```
   If catalog is not initialized or either RDR is not found, skip silently — the T2 record is the authority.
5. Regenerate index

## Failure Handling

The close operation performs multiple state mutations. If any step fails:
- Each step emits clear status (e.g., "T2 updated ✓", "Bead gate ✓", "T3 archive ✗ FAILED")
- T2 `archived` flag tracks whether T3 archival succeeded
- Re-running `/nx:rdr-close` is idempotent: checks T2 state and skips completed steps

## Relay Template (Use This Format)

When dispatching the knowledge-tidier agent via Agent tool for post-mortem archival, use this exact structure:

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
- [ ] Open beads displayed and user asked for explicit confirmation before proceeding
- [ ] Beads NOT auto-closed — human decides
- [ ] T2 record updated with close reason, date, epic bead ID, and archived flag
- [ ] T3 semantic index updated via `nx index rdr`
- [ ] Post-mortem archived to `knowledge__rdr_postmortem__{repo}` (if exists)
- [ ] README index regenerated
- [ ] Idempotent: re-running skips completed steps

## Agent-Specific PRODUCE

Outputs produced by this skill directly:

- **Console output**: Bead status gate table (if epic_bead in T2)
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
