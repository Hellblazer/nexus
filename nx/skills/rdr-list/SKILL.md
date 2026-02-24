---
name: rdr-list
description: >
  List all RDRs in the current project with status, type, and priority.
  Triggers: user says "list RDRs", "show all RDRs", or /rdr-list
allowed-tools: Read, Glob, Grep, Bash
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

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- If T2 records exist but the corresponding markdown file is missing, warn about drift.
- If markdown files exist but no T2 records, display from frontmatter only.
