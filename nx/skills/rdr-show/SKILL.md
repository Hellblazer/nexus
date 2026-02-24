---
name: rdr-show
description: >
  Display detailed RDR information including content, status, research findings,
  and linked beads. Triggers: user says "show RDR", "RDR details", or /rdr-show
allowed-tools: Read, Glob, Grep, Bash
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

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- Research findings show both classification and verification method.
- Bead information comes from `bd show` for the linked epic bead (if `epic_bead` is set in T2).
