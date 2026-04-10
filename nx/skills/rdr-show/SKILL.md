---
name: rdr-show
description: Use when needing detailed information about a specific RDR including content, research findings, and linked beads
effort: low
---

# RDR Show Skill

## When This Skill Activates

- User says "show RDR 003", "RDR details", "what's in RDR 5"
- User invokes `/nx:rdr-show`
- User asks about a specific RDR's content or status

## Behavior

1. **Resolve RDR directory**: Read from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`.
2. **Determine RDR ID**: From user's argument, or default to most recently modified RDR in `$RDR_DIR/`
3. **Read the markdown file**: `$RDR_DIR/NNN-*.md`
4. **Read T2 metadata** (if available): mcp__plugin_nx_nexus__memory_get(project="{repo}_rdr", title="NNN"
5. **Read research findings** (if available): mcp__plugin_nx_nexus__memory_get(project="{repo}_rdr", title="" and filter titles matching `NNN-research-*`
6. **Catalog links** (if catalog initialized): Search for this RDR in the catalog and display graph relationships:
   ```
   mcp__plugin_nx_nexus-catalog__search(query="<rdr-title>", content_type="rdr")
   ```
   If found, call: `mcp__plugin_nx_nexus-catalog__links(tumbler="<tumbler>", depth=1)`
   Display inbound and outbound links from the result. Skip silently if catalog not initialized.

7. **Display unified view**:

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

### Catalog Links (from link graph)
- ← catalog.py (implements-heuristic, by index_hook)
- ← tumbler.py (implements-heuristic, by index_hook)
- → "Delos: Theory of Schema Mappings" (cites, by rdr-close)
(or: "No catalog entry — run `nx index rdr` to populate")

### Post-Mortem Drift Categories
(not closed yet)
```

7. If the RDR ID is not found, list available RDRs (delegate to `/nx:rdr-list` behavior).

## Success Criteria

- [ ] RDR content displayed with correct metadata (Status, Type, Priority, dates)
- [ ] Research findings summarized by classification (Verified, Documented, Assumed)
- [ ] Linked beads shown with status (if `epic_bead` is set in T2)
- [ ] Supersedes/Superseded-by relationships displayed
- [ ] Catalog links displayed (if catalog initialized and RDR indexed)
- [ ] Fallback to `/nx:rdr-list` behavior if RDR ID not found

## Agent-Specific PRODUCE

This skill produces outputs directly (no agent delegation). It is read-only and does not write to any storage tier:

- **T3 knowledge**: Not produced (read-only operation)
- **T2 memory**: Not produced (reads T2 records but does not write)
- **T1 scratch**: Not produced; may optionally use scratch tool: action="put", content="RDR NNN show details", tags="rdr,show" for capturing display snapshots during review sessions

**Session Scratch (T1)**: Use scratch tool for ephemeral notes if the user is comparing multiple RDRs. Flagged items auto-promote to T2 at session end.

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- Research findings show both classification and verification method.
- Bead information comes from `/beads:show` for the linked epic bead (if `epic_bead` is set in T2).
