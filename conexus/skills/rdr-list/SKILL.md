---
name: rdr-list
description: Use when needing to see all RDRs in the project with their status, type, and priority
effort: low
---

# RDR List Skill

## When This Skill Activates

- User says "list RDRs", "show RDRs", "what RDRs exist"
- User invokes `/nx:rdr-list`
- User asks about the state of planning documents

## Behavior

**All data is pre-loaded in the command context.** Do NOT make additional Bash, Read, or tool calls — the `/nx:rdr-list` command already gathered:

- Repo name and RDR directory (from `.nexus.yml` or default `docs/rdr`)
- All RDR files with parsed metadata (title, status, type, priority)
- T2 records for the `{repo}_rdr` project

**Your job is to format and filter the pre-gathered data.**

1. **Display index table** using the RDR files table from context:

```
| File | Title | Status | Type | Priority |
|------|-------|--------|------|----------|
| RDR-001-project-structure.md | Project Structure | Recommendation | Architecture | High |
| RDR-002-qdrant-server-setup.md | Qdrant Server Setup | Final | Infrastructure | Medium |
```

2. **Apply filters** if specified in `$ARGUMENTS`:
   - `--status=draft` — only show rows where Status matches (case-insensitive)
   - `--type=feature` — only show rows where Type matches
   - `--has-assumptions` — only show RDRs that have T2 records in context

3. **Emit drift warnings** if:
   - T2 records reference a file not in the filesystem table
   - Files exist in the filesystem table with no T2 record (informational, not a warning)

## Success Criteria

- [ ] Index table displayed with File, Title, Status, Type, Priority columns
- [ ] Filters applied correctly if user specified `--status`, `--type`, or `--has-assumptions`
- [ ] Drift warnings emitted when T2 and filesystem disagree
- [ ] No additional Bash or Read tool calls made (data is pre-loaded)

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- RDR files may use `RDR-NNN-name.md` naming (Arcaneum style) or `NNN-name.md` or other conventions — the command handles all patterns.
- Metadata may come from YAML frontmatter (`---`) or a `## Metadata` section with `- **Key**: Value` lines — both are parsed by the command.
