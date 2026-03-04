# RDR: Template Reference

**Right-size to the decision.** Fill what helps; skip what doesn't. A quick
bug-fix RDR may need only Problem Statement, Research Findings, and Proposed
Solution. A high-stakes architecture decision warrants everything.

---

## Minimal RDR Example

A complete bug-fix RDR at its leanest:

````markdown
---
title: "Fix: chunker emits full-file line range for every AST chunk"
id: RDR-016
type: Bug Fix
status: draft
priority: high
author: Hal Hildebrand
created: 2026-03-03
related_issues: []
related_tests: [test_indexer_chunk_flow.py]
---

# RDR-016: Fix: chunker emits full-file line range for every AST chunk

## Problem Statement

Every code chunk shows `Lines: 1-780` regardless of actual position.
`CodeSplitter` returns empty metadata; the fallback assigns full-file extent.
Result: context prefixes are useless — all chunks from a file look identical.

## Research Findings

**Root cause** (Verified — source search `chunker.py:210`):
`setdefault("line_start", 1)` / `setdefault("line_end", len(lines))` fires
for every node. `CodeSplitter` never populates `line_start`/`line_end`.

**Fix available** (Verified — llama-index source):
`TextNode.start_char_idx` / `end_char_idx` are populated.
Convert: `content[:start_char_idx].count('\n') + 1` → exact 1-indexed line.

## Proposed Solution

Replace the `setdefault` fallback at `chunker.py:210–212` with character-to-line
conversion using `start_char_idx`/`end_char_idx`. No dependency changes needed.

## Implementation Plan

1. Fix `chunker.py:210–212`
2. Add `test_ast_chunk_line_range_populated` in `test_chunker.py`
3. Re-index affected collections (`code__*`)
````

---

## RDR Template

Location: `docs/rdr/TEMPLATE.md` (copied from `nx/resources/rdr/TEMPLATE.md` on first use)

### Metadata

```yaml
---
title: "RDR Title"
id: RDR-NNN
type: Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
status: draft | accepted | implemented | reverted | abandoned | superseded
priority: high | medium | low
author: Author Name
reviewed-by: self | reviewer name(s)
created: YYYY-MM-DD
accepted_date: # YYYY-MM-DD, set by /rdr-accept
related_issues: []
related_tests: []
implementation_notes: ""
---
```

`reviewed-by: self` is acceptable for solo projects. Collaborative projects
require at least one reviewer other than the author.

`related_tests` lists test files or test names that validate this RDR's
implementation. Populated at close time.

`implementation_notes` captures deviations from the plan — filled in at close
time. Empty string if the implementation matched the RDR exactly.

### Sections

| Section | Purpose |
|---------|---------|
| **Problem Statement** | The specific challenge or requirement |
| **Context** | Background and technical environment |
| **Research Findings** | Investigation, source verification, key discoveries, critical assumptions |
| **Proposed Solution** | Approach, technical design, existing infrastructure audit, decision rationale |
| **Alternatives Considered** | Full analysis for serious alternatives; one-sentence rejection for trivial ones |
| **Trade-offs** | Consequences, risks and mitigations, failure modes |
| **Implementation Plan** | Prerequisites, minimum viable validation, phased steps, Day 2 operations, new dependencies |
| **Test Plan** | Specific scenarios covering edge cases and failure modes |
| **Validation** | Testing strategy and performance expectations |
| **Finalization Gate** | Contradiction check, assumption verification, scope check, cross-cutting concerns, proportionality |
| **References** | Requirements, dependency docs, related issues |
| **Revision History** | Gate findings appendix — keeps design sections clean |

### Critical Assumptions

Each load-bearing assumption is a checkbox with verification status:

```markdown
- [ ] [Assumption 1] — **Status**: Verified | Unverified
  — **Method**: Source Search | Spike | Docs Only
```

| Method | Description |
|--------|-------------|
| **Source Search** | API verified against dependency source code |
| **Spike** | Behavior verified by running code against a live service |
| **Docs Only** | Documentation reading only — insufficient for load-bearing assumptions |

---

## Post-Mortem Template

Location: `docs/rdr/post-mortem/TEMPLATE.md` (copied from
`nx/resources/rdr/post-mortem/TEMPLATE.md`)

Created automatically by `/rdr-close`. Fill it after implementation to analyze
drift between what was decided and what was built.

### Sections

| Section | Purpose |
|---------|---------|
| **RDR Summary** | 2–3 sentence summary of the proposal |
| **Implementation Status** | Implemented / Partially Implemented / Not Implemented / Reverted |
| **Implementation vs. Plan** | What matched, diverged, was reused, added, or skipped |
| **Drift Classification** | Categorized divergences for cross-RDR pattern analysis |
| **RDR Quality Assessment** | What the RDR got right, missed, or over-specified |
| **Key Takeaways** | Actionable improvements to the RDR process |

### Drift Classification

| Category | Description |
|----------|-------------|
| Unvalidated assumption | A claim presented as fact but never verified |
| Framework API detail | Method signatures, interface contracts, or config syntax wrong |
| Missing failure mode | What breaks or fails silently was not considered |
| Missing Day 2 operation | Bootstrap, CI/CD, removal, rollback, or migration not planned |
| Deferred critical constraint | Downstream use case that validates the approach was out of scope |
| Over-specified code | Implementation code substantially rewritten |
| Under-specified architecture | Architectural decision that should have been made but wasn't |
| Scope underestimation | Sub-feature that grew into its own major effort |
| Internal contradiction | Research findings conflicting with the proposal |
| Missing cross-cutting concern | Versioning, licensing, config, deployment model, etc. |

Each category has a **Count**, **Examples**, and **Preventable?** column
(values: `Yes -- source search`, `Yes -- spike`, or `No`).

### RDR Quality Assessment

Three dimensions:
- **What the RDR got right** — research and decisions worth repeating
- **What the RDR missed** — wrong assumptions, overlooked constraints
- **What the RDR over-specified** — code rewritten, features deferred, config never used
