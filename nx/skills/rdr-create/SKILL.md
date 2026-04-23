---
name: rdr-create
description: Use when starting a new research-design-review document to think through a technical decision
effort: medium
---

# RDR Create Skill

## When This Skill Activates

- User says "create an RDR", "new RDR", "start an RDR"
- User invokes `/nx:rdr-create`
- User wants to think through a technical decision — before, during, or after building

## Inputs

Prompt the user for:
- **Title** (required): e.g., "Bulk PDF Indexing with OCR Support"
- **Type** (optional, default: Feature): Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
- **Priority** (optional, default: Medium): High | Medium | Low
- **Related issues** (optional): bead IDs or URLs

## Behavior

### Step 0: Resolve RDR directory

Read the RDR base directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`:

```bash
python3 -c "
import os, re, sys
f = '.nexus.yml'
if not os.path.exists(f): print('docs/rdr'); sys.exit()
t = open(f).read()
try:
    import yaml; d = yaml.safe_load(t) or {}; paths = (d.get('indexing') or {}).get('rdr_paths', ['docs/rdr']); print(paths[0] if paths else 'docs/rdr')
except ImportError:
    m = re.search(r'rdr_paths[^\[]*\[([^\]]+)\]', t) or re.search(r'rdr_paths:\s*\n\s+-\s*(.+)', t)
    v = m.group(1) if m else ''; parts = re.findall(r'[a-z][a-z0-9/_-]+', v)
    print(parts[0] if parts else 'docs/rdr')
" 2>/dev/null || echo "docs/rdr"
```

Use this value as `RDR_DIR` throughout all steps below (wherever `docs/rdr` appears).

### Step 1: Bootstrap (first use only)

If `$RDR_DIR` does not exist:
1. Create `$RDR_DIR/` and `$RDR_DIR/post-mortem/` directories
2. Copy RDR template from `$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md` to `$RDR_DIR/TEMPLATE.md`
3. Copy post-mortem template from `$CLAUDE_PLUGIN_ROOT/resources/rdr/post-mortem/TEMPLATE.md` to `$RDR_DIR/post-mortem/TEMPLATE.md`
4. Create `$RDR_DIR/README.md` from `$CLAUDE_PLUGIN_ROOT/resources/rdr/README-TEMPLATE.md`

If `$CLAUDE_PLUGIN_ROOT` is not available, use the templates inline (they are embedded below in the Templates section).

> **Gap convention** (enforced at `/nx:rdr-gate` Layer 1 and `/nx:rdr-close` for post-65 RDRs): the `## Problem Statement` (or `## Problem`) section must contain one or more `#### Gap N: <title>` headings (regex: `^#{3,5} Gap \d+:`). Fill these in during drafting — the template scaffolds `Gap 1` and `Gap 2` placeholders. Replacing the Problem Statement with free-form prose and removing the gap headings will fail the gate at accept time, not just at close time. Authors can override with `/nx:rdr-gate <id> --skip-gaps` when the gap structure truly does not fit (audit-trail escape only; prefer adding real gap headings).

### Step 2: Assign ID

Scan `$RDR_DIR/` for files matching `[0-9][0-9][0-9]-*.md`. Find the highest number. Next ID = max + 1, zero-padded to 3 digits. If no files exist, start at `001`.

Derive project prefix from repo name:
```bash
basename $(git rev-parse --show-toplevel) | tr '[:lower:]' '[:upper:]' | tr -cd '[:alnum:]' | head -c 3
```
Example: `nexus` → `NEX`, `arcaneum` → `ARC`, `nx-tools` → `NXT` (hyphens stripped)

### Step 3: Create RDR file

Create `$RDR_DIR/NNN-kebab-case-title.md` from the template with these metadata fields pre-filled:
- **Date**: today's date (YYYY-MM-DD)
- **Status**: Draft
- **Type**: user's choice
- **Priority**: user's choice
- **Related Issues**: user's input (if any)

Replace `[NUMBER]` with the assigned ID and `[TITLE]` with the user's title.

### Step 4: Write T2 record

mcp__plugin_nx_nexus__memory_put(content="id: NNN\nprefix: PREFIX\ntitle: User's Title\nstatus: Draft\ntype: Feature\npriority: Medium\ncreated: YYYY-MM-DD\ngated: \nclosed: \nclose_reason: \nsuperseded_by: \nsupersedes: \nepic_bead: \narchived: false\nfile_path: $RDR_DIR/NNN-kebab-title.md", project="{repo}_rdr", title="{NNN}", ttl="permanent", tags="rdr,{type}"

### Step 5: Regenerate README index

Read all T2 records for `{repo}_rdr` project via memory_get tool: project="{repo}_rdr", title="". If T2 is empty (first create before T2 write completes), also scan filesystem frontmatter. Generate the index table and update `$RDR_DIR/README.md`.

### Step 6: Stage files

```bash
git add $RDR_DIR/NNN-kebab-title.md $RDR_DIR/README.md
```

If bootstrap ran, also stage: `$RDR_DIR/TEMPLATE.md`, `$RDR_DIR/post-mortem/TEMPLATE.md`

### Output

Print:
```
Created RDR PREFIX-NNN: "Title"
File: docs/rdr/NNN-kebab-title.md
Status: Draft

Next: Fill in Problem Statement and Context, then use /nx:rdr-research to add findings.
```

## Success Criteria

- [ ] RDR directory resolved from `.nexus.yml` `indexing.rdr_paths[0]` (default `docs/rdr`)
- [ ] RDR file created at `$RDR_DIR/NNN-kebab-title.md` with correct metadata
- [ ] Sequential ID assigned (no collisions with existing RDRs)
- [ ] T2 record written to `{repo}_rdr` project with all required fields
- [ ] `$RDR_DIR/README.md` index regenerated with new entry
- [ ] Files staged via `git add`
- [ ] Bootstrap completed on first use (template and directory structure)

## Agent-Specific PRODUCE

This skill produces outputs directly (no agent delegation):

- **T3 knowledge**: Not produced at create time (archival happens at `/nx:rdr-close`)
- **T2 memory**: RDR metadata record via memory_put tool: project="{repo}_rdr", title="{NNN}", ttl="permanent", tags="rdr,{type}"
- **T1 scratch**: Working notes during creation via scratch tool: action="put", content="RDR NNN: scaffolding", tags="rdr,create" (optional, for tracking multi-step creation)
- **Filesystem**: `docs/rdr/NNN-kebab-title.md`, updated `docs/rdr/README.md`

**Session Scratch (T1)**: Use scratch tool for ephemeral working notes if the creation involves multiple prompts or complex ID assignment. Flagged items auto-promote to T2 at session end.

## Does NOT

- Create beads (that happens at `/nx:rdr-close`)
- Run validation (that happens at `/nx:rdr-gate`)
- Commit (user decides when to commit)
- Run `nx index rdr` (user can do this manually or it happens at gate/close)
