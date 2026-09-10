---
name: rdr-fix
description: Use when a gated RDR has findings to fix — prints the findings with their sites, the diff since the gated commit, the pre-edit research title, and the fix rules
effort: medium
---

# RDR Fix Skill

The fix step of the RDR lifecycle: between a gate critique and the next gate. Delegates the diff-scoped fix check to the **substantive-critic** agent. See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "fix the gate findings", "address the critique", "fix RDR NNN"
- User invokes `/conexus:rdr-fix`
- A gate returned findings and the author is about to edit the RDR

## Input

- RDR ID (required) — e.g., `204`

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`.

## Behavior

1. Run `nx rdr preamble rdr-fix -- <id>`. It prints the latest gate's outcome and round, every Critical and Significant with its `Sites:` list, the diff range and fix commits since the gated commit, whether a fix-check record exists for the current tip, the research title the pre-edit entry will get, and the rules below.
2. No gate record: stop; findings from a review go through `/conexus:rdr-research add <id>`. Past the gate (status not draft/open): stop; post-accept edits are not gate fixes.
3. For each finding, before the edit: read the source and quote it with a tool (`sed -n`, `grep -n`, a T2 read); record the research entry with `nx rdr preamble rdr-research -- add <id> <finding tokens>`. A file:line from a T3 search hit is a lead, not a citation; re-read it from the working tree.
4. Edit every site in the finding's `Sites:` list; where a finding has none, grep the RDR for the refuted phrasing AND the corrected one. A fact lives in Problem Statement, Research Findings, Technical Design and the Implementation Plan at once.
5. Commit by explicit path. Run `nx rdr preamble rdr-gate -- <id>`; it prints the Fix check section (range, fix commits, T2 title `{id}-fix-check-<sha>`). For every identifier whose meaning, bound, or owning phase this change alters (a column, a caller-supplied parameter, a typed error, a setting, a phase or step number), list every other occurrence in the file and say whether each still holds. Dispatch the fix check (relay below) and store the verdict under that title. Any FAIL: fix, re-run on the new diff. The fix check and the gate critique are never dispatched against the same commit in parallel: fix, then check, then Layer 1 and Layer 3.
6. Do not run the gate. The user drives lifecycle transitions.

## Rules

- A fix changes the fact the critic named and nothing else. A gloss, rationale, parenthetical or count is a separate commit with its own fix check.
- Every clause a fix adds carries a tool-produced quote from its source or the marker "inferred, not read".
- A count or a universal (never / always / only / nothing / every / the one / all) needs a census of the whole surface, captured in the research entry as an enumeration; two sites that agree are not a source.
- The research entry is written before the edit; its `commits:` field is filled after the commit exists.
- From round 3, the fix closes only findings marked `Ship-blocker: yes`; every other Critical and Significant is a residual, recorded and dispositioned at accept — never re-gated for this change.
- A Criterion 6 readability WARN is never closed inside a fix commit.
- For every identifier whose meaning, bound, or owning phase this change alters (a column, a caller-supplied parameter, a typed error, a setting, a phase or step number), list every other occurrence in the file and say whether each still holds.
- The fix check and the gate critique are never dispatched against the same commit in parallel: fix, then check, then Layer 1 and Layer 3.

## Relay Template (Use This Format)

```markdown
## Relay: substantive-critic

**Task**: Fix check for RDR NNN: verify ONLY the diff `git diff <gated>..HEAD -- $RDR_DIR/NNN-*.md` against its sources and the rest of the file.
**Bead**: none

### Input Artifacts
- nx store: none
- nx memory: {repo}_rdr/NNN-research-* (the pre-edit entries the fix cites)
- Files: $RDR_DIR/NNN-*.md and the diff range above

### Deliverable
One row per ADDED or CHANGED clause, PASS or FAIL with line numbers: (1) contradicted by any other line in this file; (2) an attribution, count or universal without an enumeration or quoted source, in the diff or in the research entry it cites; (3) a cited source that does not carry the claim as stated; (4) a `file:line` taken from a T3 search or query hit is a lead, not a citation: the store carries the line as of index time, and the clause passes only when the line was re-read from the working tree; (5) For every identifier whose meaning, bound, or owning phase this change alters (a column, a caller-supplied parameter, a typed error, a setting, a phase or step number), list every other occurrence in the file and say whether each still holds. Standard Verdict block.

### Quality Criteria
- [ ] Every FAIL cites both lines or the source read
- [ ] Verdict block present
```

**Required**: All fields must be present. Agent will validate relay before starting.

## Success Criteria

- [ ] Preamble run; findings and Sites lists read
- [ ] Research entry recorded before each edit, with a quote or enumeration
- [ ] Every site of each finding edited; nothing else changed
- [ ] Fix check dispatched on the diff and its verdict stored as `{id}-fix-check-<sha>`
- [ ] No gate run by this skill

## Agent-Specific PRODUCE

Outputs generated by the substantive-critic agent (fix check):

- **T2 memory**: fix-check verdict via memory_put tool: project="{repo}_rdr", title="{id}-fix-check-<sha>", ttl="permanent", tags="rdr,gate,fix-check"
- **T1 scratch**: working notes during the fix via scratch tool: action="put", content="RDR NNN fix: <finding>", tags="rdr,fix"

**Session Scratch (T1)**: Use scratch tool for ephemeral notes while fixing. Flagged items auto-promote to T2 at session end.
