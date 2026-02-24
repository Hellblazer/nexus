---
name: rdr-close
description: >
  Close an RDR: capture divergence, create post-mortem, decompose into beads, archive to T3.
  Triggers: user says "close this RDR", "RDR done", or /rdr-close
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# RDR Close Skill

Delegates decomposition and archival to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "close this RDR", "RDR done", "finish RDR"
- User invokes `/rdr-close`
- Implementation is complete and the RDR should be finalized

## Inputs

- **RDR ID** (required) — e.g., `003`
- **Reason** (required): Implemented | Reverted | Abandoned | Superseded

## Pre-Check

1. Read T2 record: `nx memory get --project {repo}_rdr --title NNN`
2. If status is not "Final" and reason is "Implemented":
   - Warn: "RDR NNN has not passed the finalization gate (status: Draft). Close anyway?"
   - Require `--force` or explicit user confirmation to proceed
3. If T2 record not found, check filesystem for the markdown file

## Flow: Implemented

### Step 1: Divergence Notes

Ask: "Did implementation diverge from the plan? If so, describe the divergences."

If diverged:

### Step 2: Create Post-Mortem

Create `docs/rdr/post-mortem/NNN-kebab-title.md` from the post-mortem template. Populate:

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

### Step 3: Decompose into Beads

Dispatch `knowledge-tidier` agent to parse the Implementation Plan section:

```markdown
## Relay: knowledge-tidier

**Task**: Parse RDR NNN Implementation Plan into an epic bead + child beads, then archive RDR to T3.
**Bead**: none (will create beads)

### Input Artifacts
- nx store: none
- nx memory: {repo}_rdr/NNN
- Files: docs/rdr/NNN-*.md, docs/rdr/post-mortem/NNN-*.md (if exists)

### Deliverable
1. Epic bead created via `bd create --title "PREFIX-NNN: Title" --type epic --priority {priority}`
2. Child beads for each Implementation Plan phase/step via `bd create`
3. Dependencies wired via `bd dep add <child> <epic>`
4. RDR archived to T3 via `nx store put`

### Quality Criteria
- [ ] Epic bead title includes RDR prefix and ID
- [ ] Each Implementation Plan phase has a corresponding child bead
- [ ] Dependencies correctly wired (children depend on epic)
- [ ] T3 archive includes full RDR + post-mortem content
- [ ] T3 tags include divergence categories (if any)
```

Display the bead tree for user confirmation before proceeding.

### Step 4: Update State

1. Update T2 record:
   ```bash
   nx memory put - --project {repo}_rdr --title NNN --ttl permanent --tags rdr,{type},closed <<'EOF'
   ... (same fields, status: "Implemented", closed: "YYYY-MM-DD",
        close_reason: "Implemented", epic_bead: "{bead_id}", archived: true)
   EOF
   ```
   If T3 archive fails, set `archived: false` — retryable by re-running `/rdr-close`

2. Update status in RDR markdown metadata
3. Regenerate `docs/rdr/README.md` index
4. Run `nx index rdr` to update T3 semantic index

### Step 5: T3 Archive

```bash
nx store put docs/rdr/NNN-*.md --collection docs__rdr__{repo} \
  --title "PREFIX-NNN Title" \
  --tags "rdr,{type},implemented,diverged:{category1},diverged:{category2}" \
  --category rdr-archive
```

If post-mortem exists, archive it too:
```bash
nx store put docs/rdr/post-mortem/NNN-*.md --collection docs__rdr__{repo} \
  --title "PREFIX-NNN Title (post-mortem)" \
  --tags "rdr,post-mortem,{drift-categories}" \
  --category rdr-post-mortem
```

## Flow: Reverted or Abandoned

1. Prompt for reason (free text)
2. Create post-mortem (the rdr process requires post-mortems for reverted/abandoned RDRs)
3. Update T2 record with close reason
4. Update markdown metadata
5. Archive to T3 (research findings are valuable even for failed RDRs)
6. Do NOT create beads
7. Regenerate index

## Flow: Superseded

1. Prompt for superseding RDR ID
2. Cross-link both RDRs:
   - In T2: set `superseded_by` on old RDR
   - In markdown: add "Superseded by RDR-NNN" note to old RDR's metadata
3. Archive to T3
4. Regenerate index

## Failure Handling

The close operation performs multiple state mutations. If any step fails:
- Each step emits clear status (e.g., "T2 updated ✓", "Beads created ✓", "T3 archive ✗ FAILED")
- T2 `archived` flag tracks whether T3 archival succeeded
- Re-running `/rdr-close` is idempotent: checks T2 state and skips completed steps
- If bead creation partially fails, report which beads were created and which failed

## Does NOT

- Force close if gate hasn't passed (warns, allows override)
- Delete the markdown file (it stays in the repo permanently)
- Auto-commit (user decides when to commit)
