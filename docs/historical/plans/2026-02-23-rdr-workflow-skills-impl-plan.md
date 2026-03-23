# RDR Workflow Skills — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the RDR workflow automation system: `nx index rdr` CLI command, SessionStart hook, and 6 Claude Code slash command skills (`/nx:rdr-create`, `/nx:rdr-list`, `/nx:rdr-show`, `/nx:rdr-research`, `/nx:rdr-gate`, `/nx:rdr-close`).

**Architecture:** Python CLI command + hook script for infrastructure; markdown skill files for workflow orchestration. Skills invoke `nx` CLI and `bd` for state management. Storage: filesystem (docs/rdr/*.md) + T2 (structured metadata) + T3 (semantic index via `docs__rdr__{repo}`).

**Tech Stack:** Python 3.12+, Click (CLI), pytest (testing), markdown skill files (Claude Code plugin)

**Design Doc:** `docs/plans/2026-02-23-rdr-workflow-skills-design.md`

---

## Phase 1: `nx index rdr` Command (Python, TDD)

### Task 1: Write failing test for `nx index rdr`

**Files:**
- Create: `tests/test_index_rdr_cmd.py`

**Step 1: Write the test file**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx index rdr`` command."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def rdr_repo(tmp_path: Path) -> Path:
    """Create a fake repo with docs/rdr/ containing markdown files."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001-project-structure.md").write_text(
        "---\ntitle: Project Structure\nstatus: Draft\n---\n\n# Problem\n\nTest RDR content.\n"
    )
    (rdr_dir / "002-semantic-search.md").write_text(
        "---\ntitle: Semantic Search\nstatus: Final\n---\n\n# Problem\n\nAnother RDR.\n"
    )
    (rdr_dir / "README.md").write_text("# RDR Index\n")
    (rdr_dir / "TEMPLATE.md").write_text("# Template\n")
    return tmp_path


def test_index_rdr_discovers_markdown_files(runner: CliRunner, rdr_repo: Path) -> None:
    """nx index rdr should discover .md files in docs/rdr/, excluding README and TEMPLATE."""
    with patch("nexus.commands.index.batch_index_markdowns") as mock_batch:
        mock_batch.return_value = {
            str(rdr_repo / "docs/rdr/001-project-structure.md"): "indexed",
            str(rdr_repo / "docs/rdr/002-semantic-search.md"): "indexed",
        }
        result = runner.invoke(main, ["index", "rdr", str(rdr_repo)])

    assert result.exit_code == 0, result.output
    assert "2" in result.output  # 2 files indexed
    # Verify only RDR files passed (not README, TEMPLATE)
    paths_arg = mock_batch.call_args[0][0]
    filenames = {p.name for p in paths_arg}
    assert "001-project-structure.md" in filenames
    assert "002-semantic-search.md" in filenames
    assert "README.md" not in filenames
    assert "TEMPLATE.md" not in filenames


def test_index_rdr_uses_correct_corpus(runner: CliRunner, rdr_repo: Path) -> None:
    """Corpus should be rdr__{repo_name} so collection becomes docs__rdr__{repo_name}."""
    with patch("nexus.commands.index.batch_index_markdowns") as mock_batch:
        mock_batch.return_value = {}
        result = runner.invoke(main, ["index", "rdr", str(rdr_repo)])

    assert result.exit_code == 0, result.output
    corpus_arg = mock_batch.call_args[0][1]
    assert corpus_arg == f"rdr__{rdr_repo.name}"


def test_index_rdr_no_rdr_dir(runner: CliRunner, tmp_path: Path) -> None:
    """Should exit cleanly when docs/rdr/ does not exist."""
    result = runner.invoke(main, ["index", "rdr", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No docs/rdr/" in result.output


def test_index_rdr_empty_rdr_dir(runner: CliRunner, tmp_path: Path) -> None:
    """Should report 0 indexed when docs/rdr/ exists but has no .md files."""
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    with patch("nexus.commands.index.batch_index_markdowns") as mock_batch:
        mock_batch.return_value = {}
        result = runner.invoke(main, ["index", "rdr", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "0" in result.output


def test_index_rdr_excludes_postmortem_dir(runner: CliRunner, rdr_repo: Path) -> None:
    """Files in docs/rdr/post-mortem/ should be indexed separately, not mixed with RDRs."""
    pm_dir = rdr_repo / "docs" / "rdr" / "post-mortem"
    pm_dir.mkdir()
    (pm_dir / "001-project-structure.md").write_text("# Post-Mortem\n")

    with patch("nexus.commands.index.batch_index_markdowns") as mock_batch:
        mock_batch.return_value = {
            str(rdr_repo / "docs/rdr/001-project-structure.md"): "indexed",
            str(rdr_repo / "docs/rdr/002-semantic-search.md"): "indexed",
        }
        result = runner.invoke(main, ["index", "rdr", str(rdr_repo)])

    paths_arg = mock_batch.call_args[0][0]
    for p in paths_arg:
        assert "post-mortem" not in str(p)
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hal.hildebrand/git/nexus && uv run pytest tests/test_index_rdr_cmd.py -v`
Expected: FAIL — `batch_index_markdowns` not importable from `nexus.commands.index`, no `rdr` subcommand

---

### Task 2: Implement `nx index rdr` command

**Files:**
- Modify: `src/nexus/commands/index.py`

**Step 1: Add the rdr subcommand**

Add after the existing `index_md_cmd` function in `src/nexus/commands/index.py`:

```python
@index.command("rdr")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".")
def index_rdr_cmd(path: Path) -> None:
    """Discover and index RDR documents in docs/rdr/ into T3 docs__rdr__REPO."""
    from nexus.doc_indexer import batch_index_markdowns

    path = path.resolve()
    rdr_dir = path / "docs" / "rdr"
    if not rdr_dir.exists():
        click.echo(f"No docs/rdr/ directory found in {path}.")
        return

    # Discover RDR .md files (exclude README, TEMPLATE, post-mortem/)
    exclude = {"README.md", "TEMPLATE.md"}
    paths = sorted(
        p for p in rdr_dir.glob("*.md")
        if p.name not in exclude
    )

    if not paths:
        click.echo("No RDR documents found. Indexed 0 file(s).")
        return

    corpus = f"rdr__{path.name}"
    click.echo(f"Indexing {len(paths)} RDR document(s) into docs__{corpus}…")
    results = batch_index_markdowns(paths, corpus)
    indexed = sum(1 for v in results.values() if v == "indexed")
    click.echo(f"Indexed {indexed} of {len(paths)} RDR document(s).")
```

**Step 2: Run tests to verify they pass**

Run: `cd /Users/hal.hildebrand/git/nexus && uv run pytest tests/test_index_rdr_cmd.py -v`
Expected: All 5 tests PASS

**Step 3: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add tests/test_index_rdr_cmd.py src/nexus/commands/index.py
git commit -m "feat: add nx index rdr command for RDR document indexing

Discovers docs/rdr/*.md, indexes via batch_index_markdowns into
docs__rdr__{repo} collection with voyage-context-3 CCE embeddings
and semantic heading-aware chunking."
```

---

## Phase 2: SessionStart Hook (Python)

### Task 3: Create RDR detection hook script

**Files:**
- Create: `nx/hooks/scripts/rdr_hook.py`
- Modify: `nx/hooks/hooks.json`

**Step 1: Write the hook script**

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: detect docs/rdr/ and report RDR indexing status."""
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path | None:
    """Get git repo root, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _collection_exists(repo_name: str) -> bool:
    """Check if docs__rdr__{repo_name} collection exists in T3."""
    try:
        result = subprocess.run(
            ["nx", "collection", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            target = f"docs__rdr__{repo_name}"
            return target in result.stdout
    except Exception:
        pass
    return False


def main() -> None:
    root = _repo_root()
    if root is None:
        sys.exit(0)

    rdr_dir = root / "docs" / "rdr"
    if not rdr_dir.exists():
        sys.exit(0)

    # Count RDR files (exclude README, TEMPLATE)
    exclude = {"README.md", "TEMPLATE.md"}
    rdr_files = [p for p in rdr_dir.glob("*.md") if p.name not in exclude]
    if not rdr_files:
        sys.exit(0)

    repo_name = root.name
    indexed = _collection_exists(repo_name)

    if indexed:
        print(f"RDR: {len(rdr_files)} document(s) in docs/rdr/, indexed in docs__rdr__{repo_name}")
    else:
        print(f"RDR: {len(rdr_files)} document(s) found in docs/rdr/ but NOT indexed.")
        print(f"     Run: nx index rdr {root}")

    sys.exit(0)


if __name__ == "__main__":
    main()
```

**Step 2: Register the hook in hooks.json**

Add to the SessionStart array in `nx/hooks/hooks.json`, after the existing `session_start_hook.py` entry:

```json
{
  "type": "command",
  "command": "python3 $CLAUDE_PLUGIN_ROOT/hooks/scripts/rdr_hook.py"
}
```

**Step 3: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/hooks/scripts/rdr_hook.py nx/hooks/hooks.json
git commit -m "feat: add SessionStart hook for RDR auto-detection

Detects docs/rdr/ in current repo, checks T3 for existing index,
prompts user to run nx index rdr if not yet indexed."
```

---

## Phase 3: RDR Templates (Resource Files)

### Task 4: Bundle RDR templates into the nx plugin

**Files:**
- Create: `nx/resources/rdr/TEMPLATE.md`
- Create: `nx/resources/rdr/post-mortem/TEMPLATE.md`
- Create: `nx/resources/rdr/README-TEMPLATE.md`

**Step 1: Copy the RDR template**

Copy `/Users/hal.hildebrand/git/rdr/TEMPLATE.md` to `nx/resources/rdr/TEMPLATE.md` (exact content, no modifications).

**Step 2: Copy the post-mortem template**

Copy `/Users/hal.hildebrand/git/rdr/post-mortem/TEMPLATE.md` to `nx/resources/rdr/post-mortem/TEMPLATE.md` (exact content, no modifications).

**Step 3: Create the README index template**

```markdown
# Recommendation Decisioning Records (RDRs)

RDRs are specification prompts built through iterative research and refinement.

See the [RDR process documentation](https://github.com/cwensel/rdr) for the full workflow.

## Index

| ID | Title | Status | Type | Priority |
|----|-------|--------|------|----------|
```

**Step 4: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/resources/rdr/
git commit -m "feat: bundle RDR and post-mortem templates as plugin resources"
```

---

## Phase 4: Skills — Read-Only First (`/nx:rdr-list`, `/nx:rdr-show`)

### Task 5: Create `/nx:rdr-list` skill

**Files:**
- Create: `nx/skills/rdr-list/SKILL.md`

**Step 1: Write the skill file**

```markdown
---
name: rdr-list
description: >
  List all RDRs in the current project with status, type, and priority.
  Triggers: user says "list RDRs", "show all RDRs", or /nx:rdr-list
allowed-tools: Read, Glob, Grep, Bash
---

# RDR List Skill

## When This Skill Activates

- User says "list RDRs", "show RDRs", "what RDRs exist"
- User invokes `/nx:rdr-list`
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
```

**Step 2: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/skills/rdr-list/SKILL.md
git commit -m "feat: add /nx:rdr-list skill for listing RDRs with status"
```

---

### Task 6: Create `/nx:rdr-show` skill

**Files:**
- Create: `nx/skills/rdr-show/SKILL.md`

**Step 1: Write the skill file**

```markdown
---
name: rdr-show
description: >
  Display detailed RDR information including content, status, research findings,
  and linked beads. Triggers: user says "show RDR", "RDR details", or /nx:rdr-show
allowed-tools: Read, Glob, Grep, Bash
---

# RDR Show Skill

## When This Skill Activates

- User says "show RDR 003", "RDR details", "what's in RDR 5"
- User invokes `/nx:rdr-show`
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

6. If the RDR ID is not found, list available RDRs (delegate to `/nx:rdr-list` behavior).

## Notes

- This is a read-only skill. It does NOT modify any files or state.
- Research findings show both classification and verification method.
- Bead information comes from `bd show` for the linked epic bead (if `epic_bead` is set in T2).
```

**Step 2: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/skills/rdr-show/SKILL.md
git commit -m "feat: add /nx:rdr-show skill for detailed RDR inspection"
```

---

## Phase 5: Skills — Lifecycle (`/nx:rdr-create`, `/nx:rdr-research`)

### Task 7: Create `/nx:rdr-create` skill

**Files:**
- Create: `nx/skills/rdr-create/SKILL.md`

**Step 1: Write the skill file**

```markdown
---
name: rdr-create
description: >
  Scaffold a new RDR from template, assign sequential ID, register in T2, add to index.
  Triggers: user says "create an RDR", "new RDR", or /nx:rdr-create
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

# RDR Create Skill

## When This Skill Activates

- User says "create an RDR", "new RDR", "start an RDR"
- User invokes `/nx:rdr-create`
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

Next: Fill in Problem Statement and Context, then use /nx:rdr-research to add findings.
```

## Does NOT

- Create beads (that happens at `/nx:rdr-close`)
- Run validation (that happens at `/nx:rdr-gate`)
- Commit (user decides when to commit)
- Run `nx index rdr` (user can do this manually or it happens at gate/close)
```

**Step 2: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/skills/rdr-create/SKILL.md
git commit -m "feat: add /nx:rdr-create skill for scaffolding new RDRs"
```

---

### Task 8: Create `/nx:rdr-research` skill

**Files:**
- Create: `nx/skills/rdr-research/SKILL.md`

**Step 1: Write the skill file**

```markdown
---
name: rdr-research
description: >
  Add, track, and verify structured research findings for an RDR.
  Triggers: user says "add research finding", "RDR research", or /nx:rdr-research
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# RDR Research Skill

Optionally delegates to **deep-research-synthesizer** (sonnet) for evidence gathering, or **codebase-deep-analyzer** (sonnet) for code-specific questions.

## When This Skill Activates

- User says "add research finding", "update RDR research", "verify assumption"
- User invokes `/nx:rdr-research`
- User wants to record or classify a discovery during RDR planning

## Subcommands

### `/nx:rdr-research add <id>`

**Inputs** (prompt the user):
1. **Finding text**: What was discovered
2. **Classification**: Verified (✅) | Documented (⚠️) | Assumed (❓)
3. **Verification method**: Source Search | Spike | Docs Only
4. **Source**: Code path, URL, experiment description

**Behavior:**

1. **Determine next sequence number**:
   ```bash
   nx memory list --project {repo}_rdr
   ```
   Filter entries where title matches `NNN-research-*`. Parse sequence numbers. Next seq = max + 1. If none exist, start at 1.

2. **Write T2 record**:
   ```bash
   nx memory put - --project {repo}_rdr --title NNN-research-{seq} --ttl permanent --tags rdr,research,{classification} <<'EOF'
   rdr_id: "NNN"
   seq: {seq}
   finding: "Finding text here"
   classification: "verified"
   verification_method: "source_search"
   source: "Source description here"
   acknowledged: false
   EOF
   ```

3. **Append to RDR markdown**: Add a formatted entry to the Research Findings > Key Discoveries section:
   ```markdown
   - **✅ Verified** (source search) — Finding text here
     *Source: source description*
   ```

### `/nx:rdr-research status <id>`

1. List T2 entries: `nx memory list --project {repo}_rdr`
2. Filter titles matching `NNN-research-*`
3. Parse and display summary:
   ```
   RDR NNN Research Status:
   - Verified (✅): 3 (2 source search, 1 spike)
   - Documented (⚠️): 1 (1 docs only)
   - Assumed (❓): 2 — ⚠ unresolved risks
     - [seq 4] "Library X supports feature Y" (docs only)
     - [seq 6] "Latency under 100ms" (docs only)
   ```

### `/nx:rdr-research verify <id> <finding-seq>`

1. Read T2 record: `nx memory get --project {repo}_rdr --title NNN-research-{seq}`
2. Prompt for new classification (verified or documented) and updated verification method
3. Update T2 record (overwrite with updated content)
4. Update the emoji marker in the RDR markdown file (e.g., ❓ → ✅)

## Agent Dispatch

When the user asks to *investigate* something (not just record a finding):
- **Code questions** ("how does auth work in our codebase?"): Dispatch `codebase-deep-analyzer` agent, then record the finding
- **External research** ("what embedding models support CCE?"): Dispatch `deep-research-synthesizer` agent, then record the finding
- **Simple recording**: No agent needed — just write the T2 record and update markdown

## Notes

- The markdown document is the authoritative narrative; T2 is the queryable index
- Verification method (Source Search / Spike / Docs Only) is the most load-bearing field — it distinguishes high-confidence from low-confidence findings
- Findings marked "Docs Only" on load-bearing assumptions are the highest risk items
```

**Step 2: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/skills/rdr-research/SKILL.md
git commit -m "feat: add /nx:rdr-research skill for structured research tracking"
```

---

## Phase 6: Skills — Gate and Close (`/nx:rdr-gate`, `/nx:rdr-close`)

### Task 9: Create `/nx:rdr-gate` skill

**Files:**
- Create: `nx/skills/rdr-gate/SKILL.md`

**Step 1: Write the skill file**

```markdown
---
name: rdr-gate
description: >
  Run the RDR finalization gate: structural validation, assumption audit, and AI critique.
  Triggers: user says "gate this RDR", "finalization check", or /nx:rdr-gate
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# RDR Gate Skill

Delegates Layer 3 to the **substantive-critic** agent (sonnet). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "gate this RDR", "finalization check", "is this RDR ready?"
- User invokes `/nx:rdr-gate`
- User wants to validate an RDR before locking it as Final

## Input

- RDR ID (required) — e.g., `003`

## Three Validation Layers (run in sequence)

### Layer 1 — Structural Validation (no AI)

Read the RDR markdown file. Check that these sections are present AND non-empty (not just the heading with placeholder text):

- Problem Statement
- Context (with Background and Technical Environment subsections)
- Research Findings (with Investigation and Key Discoveries subsections)
- Proposed Solution (with Approach and Technical Design subsections)
- Alternatives Considered (at least one alternative with Pros/Cons/Rejection reason)
- Trade-offs (with Consequences and Risks subsections)
- Implementation Plan (with at least one numbered Phase/Step)
- Finalization Gate (must have written responses, not just template placeholders)

**If any section is missing or contains only placeholder text** (e.g., `[What is the specific challenge]`):
- Report which sections are incomplete
- STOP — do not proceed to Layer 2 or 3
- Status remains Draft

### Layer 2 — Assumption Audit (from T2, no AI)

```bash
nx memory list --project {repo}_rdr
```

Filter entries matching `NNN-research-*`. Analyze:

1. Count by classification: verified, documented, assumed
2. Count by verification method: source_search, spike, docs_only
3. Flag high-risk items: classification=assumed AND verification_method=docs_only

Display:
```
Assumption Audit for RDR NNN:
- 3 verified (2 source search, 1 spike)
- 1 documented (docs only)
- 2 assumed — ⚠ UNRESOLVED
  [seq 4] "Library X supports feature Y" (docs only) ← HIGH RISK
  [seq 6] "Latency under 100ms" (docs only) ← HIGH RISK
```

If assumed findings remain:
- Ask: "Proceed with 2 unverified assumptions? (recorded as acknowledged)"
- If yes: update T2 records with `acknowledged: true`
- If no: STOP — user should verify or remove assumptions first

### Layer 3 — AI Critique (substantive-critic agent)

Dispatch the `substantive-critic` agent via Task tool with this relay:

```markdown
## Relay: substantive-critic

**Task**: Critique RDR NNN for internal consistency, missing failure modes, scope creep, and proportionality.
**Bead**: none

### Input Artifacts
- nx store: none
- nx memory: {repo}_rdr/NNN (status and research records)
- Files: docs/rdr/NNN-*.md

### Deliverable
Structured critique with pass/warn/fail per finalization gate criterion:
1. Contradiction Check — pass/warn/fail
2. Assumption Verification — pass/warn/fail
3. Scope Verification — pass/warn/fail
4. Cross-Cutting Concerns — pass/warn/fail
5. Proportionality — pass/warn/fail

### Quality Criteria
- [ ] Every fail has a specific section reference and fix suggestion
- [ ] Warns are actionable but non-blocking
- [ ] Prior RDR search attempted (may return empty on cold-start)
```

**Prior-art search** (within the agent): enumerate RDR collections and search:
```bash
nx collection list | grep docs__rdr__
nx search "relevant query terms from RDR problem statement" --corpus {each_collection} --n 5
```
If no collections found: "No prior RDRs indexed. Cross-project prior-art search will improve as RDRs are indexed and closed."

### Gate Aggregation

- Any **fail** → gate fails. Status remains Draft.
- **Warns only** → gate passes. Warns surfaced to user but do not block.
- All **pass** → gate passes.

**Important**: The AI critique *supplements* but does not *replace* the author completing the Finalization Gate section with written responses. The gate should verify that the Finalization Gate section contains substantive written responses, not just "N/A" or placeholder text.

### On Pass

1. Update status to Final in T2:
   ```bash
   nx memory put - --project {repo}_rdr --title NNN --ttl permanent --tags rdr,{type} <<'EOF'
   ... (same fields, status: "Final", gated: "YYYY-MM-DD")
   EOF
   ```
2. Update status in the RDR markdown metadata section
3. Regenerate `docs/rdr/README.md` index
4. Run `nx index rdr` to update T3 semantic index

### On Fail

Display the critique with specific sections to address. Status remains Draft.
```

**Step 2: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/skills/rdr-gate/SKILL.md
git commit -m "feat: add /nx:rdr-gate skill with 3-layer validation"
```

---

### Task 10: Create `/nx:rdr-close` skill

**Files:**
- Create: `nx/skills/rdr-close/SKILL.md`

**Step 1: Write the skill file**

```markdown
---
name: rdr-close
description: >
  Close an RDR: capture divergence, create post-mortem, decompose into beads, archive to T3.
  Triggers: user says "close this RDR", "RDR done", or /nx:rdr-close
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# RDR Close Skill

Delegates decomposition and archival to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- User says "close this RDR", "RDR done", "finish RDR"
- User invokes `/nx:rdr-close`
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
   If T3 archive fails, set `archived: false` — retryable by re-running `/nx:rdr-close`

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
- Re-running `/nx:rdr-close` is idempotent: checks T2 state and skips completed steps
- If bead creation partially fails, report which beads were created and which failed

## Does NOT

- Force close if gate hasn't passed (warns, allows override)
- Delete the markdown file (it stays in the repo permanently)
- Auto-commit (user decides when to commit)
```

**Step 2: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/skills/rdr-close/SKILL.md
git commit -m "feat: add /nx:rdr-close skill with post-mortem, bead decomposition, T3 archive"
```

---

## Phase 7: Registry and Agent Updates

### Task 11: Update registry.yaml with RDR skills

**Files:**
- Modify: `nx/registry.yaml`

**Step 1: Add RDR skill entries to the agents section**

No new agents are needed — the RDR skills dispatch to existing agents (substantive-critic, knowledge-tidier, deep-research-synthesizer, codebase-deep-analyzer). Add a new `skills` section after the `agents` section:

```yaml
# =============================================================================
# RDR SKILLS (no dedicated agent — orchestration skills)
# =============================================================================

rdr_skills:
  rdr-create:
    slash_command: /nx:rdr-create
    description: "Scaffold new RDR from template, assign ID, register in T2"
    triggers:
      - "create an RDR"
      - "new RDR"
      - "start planning document"

  rdr-research:
    slash_command: /nx:rdr-research
    description: "Add, track, and verify structured research findings"
    triggers:
      - "add research finding"
      - "update RDR research"
      - "verify assumption"
    dispatches_to: [deep-research-synthesizer, codebase-deep-analyzer]

  rdr-gate:
    slash_command: /nx:rdr-gate
    description: "Run finalization gate: structural + assumption + AI critique"
    triggers:
      - "gate this RDR"
      - "finalization check"
      - "is this RDR ready"
    dispatches_to: [substantive-critic]

  rdr-close:
    slash_command: /nx:rdr-close
    description: "Close RDR with post-mortem, bead decomposition, T3 archive"
    triggers:
      - "close this RDR"
      - "RDR done"
      - "finish RDR"
    dispatches_to: [knowledge-tidier]

  rdr-list:
    slash_command: /nx:rdr-list
    description: "List all RDRs with status, type, priority"
    triggers:
      - "list RDRs"
      - "show all RDRs"

  rdr-show:
    slash_command: /nx:rdr-show
    description: "Display detailed RDR information"
    triggers:
      - "show RDR"
      - "RDR details"
```

**Step 2: Update codebase-deep-analyzer agent instructions**

Add to `nx/agents/codebase-deep-analyzer.md`, in the workflow section:

```markdown
### RDR Awareness

When analyzing a codebase, check for `docs/rdr/` directory. If present:
- Note the number of RDRs and their statuses in your analysis
- Use `--corpus docs__rdr__{repo}` for semantic search of RDR content (if indexed)
- RDR documents contain architectural decisions, trade-offs, and research — valuable context for codebase understanding
```

**Step 3: Commit**

```bash
cd /Users/hal.hildebrand/git/nexus
git add nx/registry.yaml nx/agents/codebase-deep-analyzer.md
git commit -m "feat: register RDR skills in registry, add RDR awareness to analyzer"
```

---

## Phase 8: Integration Test

### Task 12: End-to-end smoke test

This is a manual verification task, not automated.

**Step 1: Test `nx index rdr` against arcaneum**

```bash
cd /Users/hal.hildebrand/git/nexus
uv run nx index rdr /Users/hal.hildebrand/git/arcaneum
```

Expected: "Indexed N of 19 RDR document(s)." (some may be skipped if already indexed)

**Step 2: Verify semantic search works**

```bash
uv run nx search "PDF indexing OCR" --corpus docs__rdr__arcaneum --n 3
```

Expected: Results from RDR-004 (Bulk PDF Indexing with OCR Support) with section-level chunks

**Step 3: Verify cross-project search**

```bash
uv run nx search "source code indexing" --corpus docs__rdr --n 5
```

Expected: Prefix fan-out finds results across all `docs__rdr__*` collections. If only arcaneum is indexed, results come from there.

**Step 4: Run full test suite**

```bash
cd /Users/hal.hildebrand/git/nexus
uv run pytest -v
```

Expected: All existing tests pass + new `test_index_rdr_cmd.py` tests pass

**Step 5: Final commit**

If any fixes were needed during smoke testing, commit them.

---

## Summary: File Inventory

### New Files (11)

| File | Type | Phase |
|------|------|-------|
| `tests/test_index_rdr_cmd.py` | Python test | 1 |
| `nx/hooks/scripts/rdr_hook.py` | Python hook | 2 |
| `nx/resources/rdr/TEMPLATE.md` | Markdown template | 3 |
| `nx/resources/rdr/post-mortem/TEMPLATE.md` | Markdown template | 3 |
| `nx/resources/rdr/README-TEMPLATE.md` | Markdown template | 3 |
| `nx/skills/rdr-list/SKILL.md` | Skill | 4 |
| `nx/skills/rdr-show/SKILL.md` | Skill | 4 |
| `nx/skills/rdr-create/SKILL.md` | Skill | 5 |
| `nx/skills/rdr-research/SKILL.md` | Skill | 5 |
| `nx/skills/rdr-gate/SKILL.md` | Skill | 6 |
| `nx/skills/rdr-close/SKILL.md` | Skill | 6 |

### Modified Files (3)

| File | Change | Phase |
|------|--------|-------|
| `src/nexus/commands/index.py` | Add `index rdr` subcommand | 1 |
| `nx/hooks/hooks.json` | Add rdr_hook.py to SessionStart | 2 |
| `nx/registry.yaml` | Add RDR skill entries | 7 |

### Modified Agent (1)

| File | Change | Phase |
|------|--------|-------|
| `nx/agents/codebase-deep-analyzer.md` | Add RDR awareness section | 7 |

### Commits (9)

1. `feat: add nx index rdr command for RDR document indexing`
2. `feat: add SessionStart hook for RDR auto-detection`
3. `feat: bundle RDR and post-mortem templates as plugin resources`
4. `feat: add /nx:rdr-list skill for listing RDRs with status`
5. `feat: add /nx:rdr-show skill for detailed RDR inspection`
6. `feat: add /nx:rdr-create skill for scaffolding new RDRs`
7. `feat: add /nx:rdr-research skill for structured research tracking`
8. `feat: add /nx:rdr-gate skill with 3-layer validation`
9. `feat: add /nx:rdr-close skill with post-mortem, bead decomposition, T3 archive`
10. `feat: register RDR skills in registry, add RDR awareness to analyzer`
