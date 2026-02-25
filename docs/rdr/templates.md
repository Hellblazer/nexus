# RDR: Template Reference

This document describes the two templates used in the RDR process: the main
RDR document template and the post-mortem template.

**Right-size to the decision.** The template contains every section you might
need, not every section you must fill. A quick decision might use only Problem
Statement, Research Findings, and Proposed Solution. A high-stakes architectural
choice might fill everything including the Finalization Gate. Use what helps;
skip what doesn't.

---

## RDR Template

Location: `docs/rdr/TEMPLATE.md` (copied from `nx/resources/rdr/TEMPLATE.md` on first use)

### Metadata

The template uses a `## Metadata` section with markdown list items (not YAML frontmatter):

```markdown
## Metadata

- **Date**: YYYY-MM-DD
- **Status**: Draft | Final | Implemented | Reverted | Abandoned | Superseded
- **Type**: Feature | Bug Fix | Technical Debt | Framework Workaround | Architecture
- **Priority**: High | Medium | Low
- **Related Issues**: [Links to related issues/tickets]
```

### Sections

The template contains these sections, each with guidance comments:

| Section | Purpose |
|---|---|
| **Problem Statement** | What is the specific challenge or requirement? |
| **Context** | Background and technical environment |
| **Research Findings** | Investigation, dependency source verification, key discoveries, critical assumptions |
| **Proposed Solution** | Approach, technical design, existing infrastructure audit, decision rationale |
| **Alternatives Considered** | Full analysis for serious alternatives, one-sentence rejection for trivial ones |
| **Trade-offs** | Consequences, risks and mitigations, failure modes |
| **Implementation Plan** | Prerequisites, minimum viable validation, phased steps, Day 2 operations, new dependencies |
| **Validation** | Testing strategy and performance expectations |
| **Finalization Gate** | Contradiction check, assumption verification, scope verification, cross-cutting concerns, proportionality |
| **References** | Requirements, dependency docs, related issues |

### Critical Assumptions

Each load-bearing assumption is tracked as a checkbox with verification status:

```markdown
- [ ] [Assumption 1] — **Status**: Verified | Unverified
  — **Method**: Source Search | Spike | Docs Only
```

Method definitions:
- **Source Search**: API verified against dependency source code
- **Spike**: behavior verified by running code against a live service
- **Docs Only**: based on documentation reading alone (insufficient for load-bearing assumptions)

---

## Post-Mortem Template

Location: `docs/rdr/post-mortem/TEMPLATE.md` (copied from `nx/resources/rdr/post-mortem/TEMPLATE.md`)

Created automatically by `/rdr-close`. Filled in after implementation to enable
drift analysis between what was decided and what was built.

### Sections

| Section | Purpose |
|---|---|
| **RDR Summary** | 2-3 sentence summary of the proposal |
| **Implementation Status** | Implemented / Partially Implemented / Not Implemented / Reverted |
| **Implementation vs. Plan** | What matched, what diverged, what was reused, added, or skipped |
| **Drift Classification** | Categorized divergences for pattern analysis |
| **RDR Quality Assessment** | What the RDR got right, missed, or over-specified |
| **Key Takeaways** | Actionable improvements to the RDR process |

### Drift Classification

Divergences are classified into 10 categories (not a simple binary) to enable
cross-RDR pattern analysis:

| Category | Description |
|---|---|
| Unvalidated assumption | A claim presented as fact but never verified |
| Framework API detail | Method signatures, interface contracts, or config syntax wrong |
| Missing failure mode | What breaks or fails silently was not considered |
| Missing Day 2 operation | Bootstrap, CI/CD, removal, rollback, migration not planned |
| Deferred critical constraint | Downstream use case that validates the approach was out of scope |
| Over-specified code | Implementation code that was substantially rewritten |
| Under-specified architecture | Architectural decision that should have been made but wasn't |
| Scope underestimation | Sub-feature that grew into its own major effort |
| Internal contradiction | Research findings conflicting with the proposal |
| Missing cross-cutting concern | Versioning, licensing, config, deployment model, etc. |

Each category has a **Count**, **Examples**, and **Preventable?** column
(values: "Yes -- source search", "Yes -- spike", or "No").

### RDR Quality Assessment

The post-mortem evaluates three dimensions:
- **What the RDR got right** — valuable research and decisions to repeat
- **What the RDR missed** — concerns, constraints, or wrong assumptions
- **What the RDR over-specified** — code samples rewritten, deferred features unused, config never implemented
