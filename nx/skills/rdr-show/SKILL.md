---
name: rdr-show
description: >
  Display detailed RDR information including content, status, research findings,
  and linked beads. Triggers: user says "show RDR", "RDR details", or /rdr-show
---

# RDR Show Skill

## When This Skill Activates

- User says "show RDR 003", "RDR details", "what's in RDR 5"
- User invokes `/rdr-show`
- User asks about a specific RDR's content or status

## Behavior

1. **Determine RDR ID**: From user's argument, or default to most recently modified RDR in `docs/rdr/`
2. **Read the markdown file**: `docs/rdr/NNN-*.md`
3. **Read T2 metadata** (if available): `nx memory get --project {repo}_rdr --title NNN`
4. **Read research findings** (if available): `nx memory list --project {repo}_rdr` and filter titles matching `NNN-research-*`
5. **Display unified view**:

### Output Format

```
## RDR NX-003: Semantic Search Pipeline

**Status:** Draft → **Type:** Feature → **Priority:** Medium
**Created:** 2026-02-23 | **Gated:** — | **Closed:** —

### Research Summary
- Verified (✅): 3 findings (2 source search, 1 spike)
- Documented (⚠️): 1 finding (1 docs only)
- Assumed (❓): 2 findings — ⚠ unresolved risks

### Linked Beads
- Epic: NX-abc12 "Semantic Search Pipeline" (open)
  - NX-def34 "Phase 1: Indexer" (in_progress)
  - NX-ghi56 "Phase 2: Query API" (open)

### Supersedes / Superseded By
(none)

### Post-Mortem Drift Categories
(not closed yet)
```

6. If the RDR ID is not found, list available RDRs (delegate to `/rdr-list` behavior).

## Relay Template (Use This Format)

This skill does not dispatch agents (no Task tool). The relay template is included for consistency with the standard skill format. If future changes add agent delegation, use this structure:

```markdown
## Relay: {agent-name}

**Task**: [1-2 sentence summary of what needs to be done]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- nx scratch: [scratch IDs or "none"]
- Files: [key files or "none"]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
```

## Success Criteria

- [ ] RDR content displayed with correct metadata (Status, Type, Priority, dates)
- [ ] Research findings summarized by classification (Verified, Documented, Assumed)
- [ ] Linked beads shown with status (if `epic_bead` is set in T2)
- [ ] Supersedes/Superseded-by relationships displayed
- [ ] Fallback to `/rdr-list` behavior if RDR ID not found

## Agent-Specific PRODUCE

This skill produces outputs directly (no agent delegation). It is read-only and does not write to any storage tier:

- **T3 knowledge**: Not produced (read-only operation)
- **T2 memory**: Not produced (reads T2 records but does not write)
- **T1 scratch**: Not produced; may optionally use `nx scratch put "RDR NNN show details" --tags "rdr,show"` for capturing display snapshots during review sessions

**Session Scratch (T1)**: Use `nx scratch` for ephemeral notes if the user is comparing multiple RDRs. Flagged items auto-promote to T2 at session end.

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- Research findings show both classification and verification method.
- Bead information comes from `bd show` for the linked epic bead (if `epic_bead` is set in T2).
