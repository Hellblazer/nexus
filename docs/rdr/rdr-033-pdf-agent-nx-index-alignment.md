---
title: "PDF Processing Agent Should Delegate to nx index pdf"
id: RDR-033
type: Architecture
status: draft
priority: P1
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues:
  - "RDR-011 - PDF Ingest Test Coverage (closed)"
  - "RDR-012 - pdfplumber Extraction Tier (closed, superseded by RDR-021)"
  - "RDR-015 - Indexing Pipeline Rethink"
  - "RDR-021 - Docling PDF Extraction (accepted)"
  - "RDR-032 - Indexer Decomposition"
---

# RDR-033: PDF Processing Agent Should Delegate to nx index pdf

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The `pdf-chromadb-processor` agent and `pdf-processing` skill contain a 234-line system prompt that reinvents PDF processing from first principles — calling `pdftotext`, `pdfinfo`, `ghostscript`, and `pdftk` directly, manually chunking text, and storing chunks one-by-one via `nx store put`. Meanwhile, the `nx index pdf` CLI command already handles the entire pipeline in a single invocation:

```bash
nx index pdf /path/to/paper.pdf --corpus my-corpus --monitor
# → Indexed 83 chunk(s) in seconds
```

In practice, the agent fails completely: it hits sandbox restrictions on CLI tools, falls back to asking for permissions, and produces zero indexed content. The human operator then has to discover `nx index pdf` themselves and run it manually — defeating the purpose of the agent entirely.

## Context

### Observed Failure (2026-03-08, Kramer project)

1. User asked to ingest a research PDF into T3
2. `/pdf-process` skill activated, which spawned `pdf-chromadb-processor` agent
3. Agent attempted to run `pdfinfo`, `pdftotext` — all denied by sandbox
4. Agent produced zero output, asked for permission escalation
5. Human operator fell back to manual `nx store put` chunking (wrong approach)
6. Eventually discovered `nx index pdf` — completed in one command, 83 chunks

### Root Cause Analysis

The agent's system prompt was written before `nx index pdf` existed (or before it was mature). It describes a manual 4-phase process:
- Phase 0: Discovery/validation (pdfinfo, extraction method testing)
- Phase 1: Setup (working directories, progress tracking, chunk calculation)
- Phase 2: Parallel extraction (pdftotext per page range, manual nx store put)
- Phase 3: Verification (store list, search testing)

This is exactly what `nx index pdf` does internally — but the agent reimplements it badly in bash, hitting sandbox restrictions and context limits that `nx index pdf` avoids by running as a native Python pipeline.

### The nx index pdf Command

```
nx index pdf [OPTIONS] PATH

Options:
  --corpus TEXT      Corpus name for docs__ collection
  --collection TEXT  Fully-qualified T3 collection name
  --dry-run          Extract and embed locally (preview before indexing)
  --force            Force re-indexing, bypassing staleness check
  --monitor          Print chunking metadata after indexing
```

Capabilities:
- Automatic text extraction (Docling primary with neural layout analysis, PyMuPDF normalized fallback — per RDR-021)
- Context-safe chunking with page boundary awareness
- Embedding generation (local ONNX or API)
- Staleness detection (skip already-indexed PDFs)
- Metadata extraction (title, author, page count)
- Single atomic operation — no partial state

## Research Findings

### What the Agent Should Do vs What It Does

| Responsibility | Current Agent | Should Be |
|---|---|---|
| Text extraction | Calls pdftotext/ghostscript/pdftk | Delegate to `nx index pdf` |
| Chunking | Manual page-range splitting | Delegate to `nx index pdf` |
| Storage | Individual `nx store put` per chunk | Delegate to `nx index pdf` |
| Progress tracking | Manual T2 memory updates | `--monitor` flag |
| Verification | Manual `nx store list` + `nx search` | `nx search` post-indexing |
| Quality assessment | Manual character ratio checks | Built into nx extraction pipeline |
| Resume/checkpoint | Manual T2 progress file | `--force` flag for re-index |
| Collection naming | Manual pattern generation | `--corpus` argument |

### What the Agent Should Actually Do

The agent's value-add should be at a higher level than raw extraction:

1. **URL handling**: Download PDFs from URLs to temp files before indexing
2. **Corpus naming**: Suggest appropriate corpus names from context
3. **Batch processing**: Index multiple PDFs with appropriate corpus organization
4. **Post-indexing verification**: Run `nx search` with representative queries
5. **Error reporting**: If `nx index pdf` fails, provide actionable diagnostics
6. **Dry-run guidance**: Use `--dry-run` first for large/unknown PDFs
7. **Integration with beads**: Track batch processing jobs

### Skill Chain Issues

The current skill → agent chain has multiple problems:

1. **Skill invocation overhead**: `/pdf-process` skill spawns a haiku agent, which then tries to run bash commands that get sandboxed
2. **No awareness of nx index pdf**: Neither the skill nor the agent mention `nx index pdf` as the primary tool (it's buried as an afterthought in step 13)
3. **Redundant with CLI**: For single-PDF ingestion, the user could just run `nx index pdf` directly — the agent adds negative value by failing
4. **Wrong model tier**: PDF indexing is a deterministic pipeline operation, not a reasoning task — haiku is appropriate but the task doesn't need an LLM at all for the common case

## Proposed Solution

### Option A: Thin Wrapper (Recommended)

Rewrite the agent to be a thin orchestration layer around `nx index pdf`:

```markdown
## Core Loop

1. For each PDF in the input:
   a. If URL, download to /tmp
   b. Run: nx index pdf {path} --corpus {corpus} --monitor
   c. Verify: nx search "representative query" --corpus {corpus} -m 3
   d. Report results

2. For batch jobs, track via beads
```

The `--corpus` argument maps to `docs__{corpus}` — corpus names like `grossberg-1978` are correct; full collection names like `docs__grossberg-1978` should not be passed as `--corpus`.

The agent system prompt shrinks from 234 lines to ~40 lines. The agent's job becomes:
- Parse user intent (which PDFs, what corpus name)
- Call `nx index pdf` for each
- Verify and report

### Option B: Skill-Only (No Agent)

Replace the agent entirely with an expanded skill that provides inline instructions:

```markdown
## PDF Processing

To index a PDF into T3:
  nx index pdf /path/to/file.pdf --corpus {corpus-name} --monitor

To verify:
  nx search "query" --corpus docs__{corpus-name} -m 3

For dry-run preview:
  nx index pdf /path/to/file.pdf --corpus {corpus-name} --dry-run
```

This eliminates the agent spawn overhead entirely. The parent agent (or user) runs the commands directly.

### Option C: Hybrid

Keep the agent for batch/complex scenarios (multiple PDFs, URL downloads, corpus organization), but have the skill handle single-PDF cases inline without spawning an agent.

## Alternatives Considered

### Keep Current Agent, Fix Sandbox

Grant the agent bypass permissions for pdftotext/pdfinfo. This fixes the immediate failure but doesn't address the fundamental problem: the agent reimplements what `nx index pdf` already does, adding complexity and failure modes.

**Rejected**: Treats symptom, not cause.

### Migrate Agent to Use nx index pdf But Keep Full Prompt

Update step 13 to be the primary path but keep all the fallback extraction logic.

**Rejected**: Unnecessary complexity. The fallback extraction tiers are already in `nx index pdf`'s Python implementation.

## Trade-offs

### Consequences

- **Positive**: PDF processing actually works reliably
- **Positive**: 234-line agent prompt → ~40 lines (Option A) or eliminated (Option B)
- **Positive**: No sandbox permission issues — `nx index pdf` runs as a native CLI tool
- **Positive**: Consistent behavior — same chunking/extraction whether invoked via agent or CLI
- **Negative**: Agent loses ability to use alternative extraction tools not in nx pipeline
- **Negative**: Option B removes the ability to do complex multi-PDF orchestration without a parent agent

### Risks and Mitigations

- **Risk**: `nx index pdf` has bugs or limitations not covered by the agent's fallbacks
  **Mitigation**: Fix them in `nx index pdf` — that's where extraction logic belongs. The extraction stack already has a two-tier fallback (Docling primary, PyMuPDF normalized fallback — per RDR-021), and any new extraction improvements belong in `pdf_extractor.py`, not in the agent prompt.

- **Risk**: Removing agent breaks existing workflows that reference `pdf-chromadb-processor`
  **Mitigation**: Keep the agent name, just rewrite the prompt. Skill invocation path unchanged.

## Implementation Plan

### Phase 1: Rewrite Agent Prompt

1. Replace the 4-phase manual extraction system prompt with a thin `nx index pdf` wrapper
2. Keep URL download capability (curl/wget to /tmp, then `nx index pdf`)
3. Keep batch processing and beads integration
4. Keep verification step (`nx search` after indexing)
5. Remove all references to pdftotext, pdfinfo, ghostscript, pdftk as direct tools
6. Update tools list: remove dependency on Bash for extraction, keep for `nx` CLI

### Phase 2: Update Skill

1. Add inline single-PDF path that doesn't spawn an agent
2. Reserve agent spawn for batch/complex scenarios
3. Document `nx index pdf` as the primary command in skill content
4. Update skill's PRODUCE section to reference `nx index pdf` output format (collection name, chunk count, searchability), not `nx store put` schema — the current PRODUCE describes "Indexed PDF content stored via `nx store put`" which becomes stale after Phase 1

### Phase 3: Test

1. Index a PDF via the updated skill/agent flow
2. Verify sandbox compatibility (no pdftotext/pdfinfo calls)
3. Verify searchability of indexed content
4. Test URL download path
5. Test batch processing path

## Test Plan

- **Scenario**: Single PDF via skill → **Verify**: `nx index pdf` called, chunks indexed, searchable
- **Scenario**: PDF from URL → **Verify**: Downloaded to /tmp, indexed, searchable
- **Scenario**: Batch of 3 PDFs → **Verify**: All indexed with appropriate corpus names
- **Scenario**: Already-indexed PDF → **Verify**: Staleness check prevents re-indexing (or `--force` re-indexes)
- **Scenario**: Corrupt/encrypted PDF → **Verify**: Clear error message, no partial state

## Finalization Gate

### Contradiction Check

No internal contradictions found. The problem statement (agent reimplements extraction, fails in sandbox) is consistent with the proposed solution (delegate to `nx index pdf`). The extraction stack description now correctly references Docling/PyMuPDF (RDR-021), consistent with the actual `pdf_extractor.py` implementation. Option A (thin wrapper) and Option B (skill-only) are presented as alternatives, not as conflicting requirements.

### Assumption Verification

1. **`nx index pdf` covers all agent extraction scenarios**: Verified. The command handles single-PDF ingestion with Docling primary extraction, PyMuPDF normalized fallback, staleness detection, and atomic operation. The only gap is URL download (curl to /tmp), which the thin wrapper agent retains.
2. **Sandbox blocks the agent's CLI tools**: Confirmed by the 2026-03-08 Kramer project failure. `pdftotext`, `pdfinfo`, `ghostscript`, and `pdftk` are external binaries that Claude Code's sandbox restricts. `nx index pdf` runs as a Python subprocess via the `nx` CLI, which is already in the tool allowlist.
3. **`--corpus` maps to `docs__` collections**: Verified in `doc_indexer.py`. The `--corpus` flag prepends `docs__` automatically; passing `docs__grossberg-1978` would produce the malformed collection name `docs__docs__grossberg-1978`.

### Scope Verification

This RDR is scoped to the agent prompt and skill file — no changes to `nx index pdf`, `pdf_extractor.py`, or any Python source. The implementation plan has three phases: (1) rewrite agent prompt, (2) update skill, (3) test. All phases are documentation/config changes except testing. The RDR does not propose changes to the extraction pipeline itself; extraction improvements belong in RDR-021 and its successors.

### Cross-Cutting Concerns

- **Naming conventions**: The agent currently generates corpus names like `author-year-short-title` which is correct for the `--corpus` argument. The updated agent prompt must document that `--corpus` auto-prepends `docs__`, so the agent should not include the prefix.
- **Skill PRODUCE section**: Currently references `nx store put`, which Phase 2 updates to reflect `nx index pdf` output. This is a documentation-only change but affects downstream agents that read the skill's PRODUCE contract.
- **Backward compatibility**: The agent name (`pdf-chromadb-processor`) and skill invocation path (`/pdf-process`) remain unchanged. Existing workflows that reference these names continue to work.

### Proportionality

The effort is proportional to the problem. The 234-line agent prompt is provably broken (zero successful indexing in the observed failure) and the fix is a prompt rewrite to ~40 lines plus a skill update. No new dependencies, no new code, no migration. The risk of the change is low — `nx index pdf` is already the working path that the human operator discovered manually. This RDR simply makes the agent use the same path.

## References

- `nx index pdf` implementation: `src/nexus/doc_indexer.py` (CLI: `src/nexus/commands/index.py`)
- Current agent: `nx/agents/pdf-chromadb-processor.md`
- Current skill: `nx/skills/pdf-processing/SKILL.md`
- RDR-011: PDF Ingest Test Coverage
- RDR-012: pdfplumber Extraction Tier (superseded by RDR-021)
- RDR-015: Indexing Pipeline Rethink
- RDR-021: Docling PDF Extraction (current extraction stack)
- RDR-032: Indexer Module Decomposition
