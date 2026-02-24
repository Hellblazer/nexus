---
name: rdr-list
description: Use when needing to see all RDRs in the project with their status, type, and priority
---

# RDR List Skill

## When This Skill Activates

- User says "list RDRs", "show RDRs", "what RDRs exist"
- User invokes `/rdr-list`
- User asks about the state of planning documents

## Behavior

1. **Detect repo root**: `git rev-parse --show-toplevel`
2. **Check for docs/rdr/**: If absent, report "No RDRs found in this project."
3. **Scan RDR files**: Glob `docs/rdr/[0-9]*.md` (excludes README.md, TEMPLATE.md)
4. **Parse metadata**: Read each file's YAML frontmatter for: Status, Type, Priority
5. **Check T2 for structured data**: `nx memory list --project {repo}_rdr`
   - If T2 has records, merge with filesystem data (T2 takes precedence for status)
   - If T2 is empty, use frontmatter only
6. **Display index table**:

```
| ID  | Title                  | Status      | Type        | Priority |
|-----|------------------------|-------------|-------------|----------|
| 001 | Project Structure      | Draft       | Feature     | High     |
| 002 | Semantic Search        | Final       | Architecture| Medium   |
```

## Filters

If the user specifies filters, apply them:
- `--status=draft` — only show RDRs with matching status
- `--type=feature` — only show RDRs with matching type
- `--has-assumptions` — only show RDRs that have Assumed research findings in T2

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

- [ ] All RDR files in `docs/rdr/` discovered and parsed
- [ ] T2 records merged with filesystem data (T2 takes precedence for status)
- [ ] Index table displayed with ID, Title, Status, Type, Priority columns
- [ ] Filters applied correctly if user specified `--status`, `--type`, or `--has-assumptions`
- [ ] Drift warnings emitted when T2 and filesystem disagree

## Agent-Specific PRODUCE

This skill produces outputs directly (no agent delegation). It is read-only and does not write to any storage tier:

- **T3 knowledge**: Not produced (read-only operation)
- **T2 memory**: Not produced (reads T2 records but does not write)
- **T1 scratch**: Not produced; may optionally use `nx scratch put "RDR list query" --tags "rdr,list"` for tracking complex filtered queries across sessions

**Session Scratch (T1)**: Use `nx scratch` for ephemeral notes if the user is iterating on filter criteria. Flagged items auto-promote to T2 at session end.

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- If T2 records exist but the corresponding markdown file is missing, warn about drift.
- If markdown files exist but no T2 records, display from frontmatter only.
