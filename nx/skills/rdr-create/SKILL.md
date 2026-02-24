---
name: rdr-create
description: >
  Scaffold a new RDR from template, assign sequential ID, register in T2, add to index.
  Triggers: user says "create an RDR", "new RDR", or /rdr-create
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

# RDR Create Skill

## When This Skill Activates

- User says "create an RDR", "new RDR", "start an RDR"
- User invokes `/rdr-create`
- User wants to document a technical decision before implementation

## Inputs

Prompt the user for:
- **Title** (required): e.g., "Bulk PDF Indexing with OCR Support"
- **Type** (optional, default: Feature): Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
- **Priority** (optional, default: Medium): High | Medium | Low
- **Related issues** (optional): bead IDs or URLs

## Behavior

### Step 1: Bootstrap (first use only)

If `docs/rdr/` does not exist:
1. Create `docs/rdr/` and `docs/rdr/post-mortem/` directories
2. Copy RDR template from `$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md` to `docs/rdr/TEMPLATE.md`
3. Copy post-mortem template from `$CLAUDE_PLUGIN_ROOT/resources/rdr/post-mortem/TEMPLATE.md` to `docs/rdr/post-mortem/TEMPLATE.md`
4. Create `docs/rdr/README.md` from `$CLAUDE_PLUGIN_ROOT/resources/rdr/README-TEMPLATE.md`

If `$CLAUDE_PLUGIN_ROOT` is not available, use the templates inline (they are embedded below in the Templates section).

### Step 2: Assign ID

Scan `docs/rdr/` for files matching `[0-9][0-9][0-9]-*.md`. Find the highest number. Next ID = max + 1, zero-padded to 3 digits. If no files exist, start at `001`.

Derive project prefix from repo name:
```bash
basename $(git rev-parse --show-toplevel) | tr '[:lower:]' '[:upper:]' | head -c 3
```
Example: `nexus` → `NEX`, `arcaneum` → `ARC`

### Step 3: Create RDR file

Create `docs/rdr/NNN-kebab-case-title.md` from the template with these metadata fields pre-filled:
- **Date**: today's date (YYYY-MM-DD)
- **Status**: Draft
- **Type**: user's choice
- **Priority**: user's choice
- **Related Issues**: user's input (if any)

Replace `[NUMBER]` with the assigned ID and `[TITLE]` with the user's title.

### Step 4: Write T2 record

```bash
nx memory put - --project {repo}_rdr --title {NNN} --ttl permanent --tags rdr,{type} <<'EOF'
id: "NNN"
prefix: "PREFIX"
title: "User's Title"
status: "Draft"
type: "Feature"
priority: "Medium"
created: "YYYY-MM-DD"
gated: ""
closed: ""
close_reason: ""
superseded_by: ""
epic_bead: ""
archived: true
file_path: "docs/rdr/NNN-kebab-title.md"
EOF
```

### Step 5: Regenerate README index

Read all T2 records for `{repo}_rdr` project via `nx memory list --project {repo}_rdr`. If T2 is empty (first create before T2 write completes), also scan filesystem frontmatter. Generate the index table and update `docs/rdr/README.md`.

### Step 6: Stage files

```bash
git add docs/rdr/NNN-kebab-title.md docs/rdr/README.md
```

If bootstrap ran, also stage: `docs/rdr/TEMPLATE.md`, `docs/rdr/post-mortem/TEMPLATE.md`

### Output

Print:
```
Created RDR PREFIX-NNN: "Title"
File: docs/rdr/NNN-kebab-title.md
Status: Draft

Next: Fill in Problem Statement and Context, then use /rdr-research to add findings.
```

## Does NOT

- Create beads (that happens at `/rdr-close`)
- Run validation (that happens at `/rdr-gate`)
- Commit (user decides when to commit)
- Run `nx index rdr` (user can do this manually or it happens at gate/close)
